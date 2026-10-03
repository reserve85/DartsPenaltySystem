"""Role-aware financial overview (season-scoped money data) — the start page."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, render
from django.views import View

from app.core.permissions import (
    user_can_manage_team,
    user_is_player,
)
from app.core.utils import numeric_pk
from app.matchdays.services import season_for_request
from app.penalties.models import Payment, PenaltyType
from app.penalties.services import (
    assigned_penalties_qs,
    catalog_overview,
    inactive_player_balances,
    matchday_totals,
    most_common_penalties,
    player_balance,
    player_total,
    player_total_paid,
    team_balances,
    team_paid_total,
    team_penalties_total,
)
from app.teams.services import (
    captains_for_teams,
    cashier_user_for,
    teams_for_season,
    user_is_cashier,
)


def _own_team(user, teams, season):
    """The account's OWN team for the default selection, or ``None``.

    Resolution order:
    1. ``User.team`` — the optional captaincy link (Captain/Admin accounts);
    2. the ROSTER membership of the linked player (``PlayerTeam``), preferring
       the ACTIVE season, then any other season — a Player-role account has no
       captaincy field, its "assignment to a team" lives in the roster;
    3. ``None`` → the caller falls back to the first team alphabetically.

    Only teams from ``teams`` (the active season's teams) are considered.
    """
    by_pk = {team.pk: team for team in teams}
    own = by_pk.get(user.team_id)
    if own is not None:
        return own
    player = getattr(user, "player_link", None)
    if player is None:
        return None
    assignments = player.team_assignments.all()
    if season is not None:
        ordered = list(assignments.filter(season=season)) + list(assignments.exclude(season=season))
    else:
        ordered = list(assignments)
    for assignment in ordered:
        team = by_pk.get(assignment.team_id)
        if team is not None:
            return team
    return None


class FinancialOverviewView(LoginRequiredMixin, View):
    """READ is open to every signed-in account: all teams, all seasons.

    Universal read access (decision): Admin, Captain, Player and role-less
    accounts all get the full team selector and money data. Only WRITE
    actions stay role-scoped — ``can_manage`` (per selected team) gates the
    matchday links, while ``is_cashier`` gates ALL payment UI (strict
    cashier rule, decision 7); Players and role-less accounts never get
    write affordances.
    """

    template_name = "dashboard/financial_overview.html"

    def get(self, request):
        season = season_for_request(request)
        teams = list(teams_for_season(season).order_by("name"))
        # Captains of EVERY team of the season — Captain and Admin accounts
        # assigned to a team, see captains_for_teams.
        captains_by_team = captains_for_teams(teams)
        team_id = request.GET.get("team")
        if team_id:
            team = get_object_or_404(teams_for_season(season), pk=numeric_pk(team_id))
        elif teams:
            # Default: the account's OWN team — the captaincy link (captains
            # and admins alike) or, for Player accounts, their roster team of
            # the active season. Accounts without any team (e.g. the system
            # admin) fall back to the first team alphabetically.
            team = _own_team(request.user, teams, season) or teams[0]
        else:
            team = None
        context: dict = {
            "teams": teams,
            "selected_team": team,
            "can_manage": False,
            "cashier": None,
            "is_cashier": False,
        }

        # Player role: extra "own penalties" section on top of the shared view.
        if user_is_player(request.user):
            context.update(self._own_section(request, season))

        if team is None:
            return render(request, self.template_name, context)
        context.update(self._context_for_team(team, teams, season, captains_by_team))
        return render(request, self.template_name, context)

    @staticmethod
    def _own_section(request, season) -> dict:
        """Own penalties summary for the Player role (read-only extras)."""
        context: dict = {"own_mode": True}
        player = request.user.player_link
        if player is None:
            context["no_player_link"] = True
            return context
        context.update(
            {
                "player": player,
                "own_penalties": assigned_penalties_qs(season=season).filter(player=player),
                "own_balance": player_balance(player, season=season),
                "own_total": player_total(player, season=season),
                "own_paid": player_total_paid(player, season=season),
            }
        )
        return context

    def _context_for_team(self, team, teams, season, captains_by_team):
        balances = team_balances(team, season=season)
        inactive = inactive_player_balances(team, season=season)
        captains = captains_by_team.get(team.pk, [])
        catalog_rows = catalog_overview(team)
        return {
            "teams": teams,
            "selected_team": team,
            # Captain marking in the overview: the accounts that lead this
            # team (Captain OR Admin — the assignment is decoupled from the
            # role) plus the player rows linked to them.
            "team_captains": captains,
            "captain_player_ids": [
                captain.player_link_id for captain in captains if captain.player_link_id
            ],
            "team_balances": balances,
            # Team trio: total penalties, settled, still open (net = sum of rows)
            "team_penalties": team_penalties_total(team, season=season),
            "team_total": sum(row.balance for row in balances)
            + sum(row.balance for row in inactive),
            "team_paid": team_paid_total(team, season=season),
            # Write actions (matchday links, totals) stay Admin/Captain-only —
            # the Player role gets the very same overview read-only.
            "can_manage": user_can_manage_team(self.request.user, team),
            # STRICT payment gate (decision 7): only the team's current
            # cashier records/reverts payments — admins included.
            "cashier": cashier_user_for(team),
            "is_cashier": user_is_cashier(self.request.user, team),
            "payments": _season_payments(
                Payment.objects.filter(team=team).select_related("player"), season
            ),
            "inactive_balances": inactive,
            "matchday_totals": matchday_totals(team, season=season),
            "most_common": most_common_penalties(team, season=season),
            # Read-only price list, visible to EVERY role (embedded twin of
            # the 403 management catalog): effective team fee per active item.
            "catalog_overview": catalog_rows,
            # MANUAL items are assigned with an individually typed amount —
            # the shown fee is only their default, hence the footnote.
            "catalog_has_manual": any(item.type == PenaltyType.MANUAL for item in catalog_rows),
            "assigned_penalties": assigned_penalties_qs(team, season=season),
        }


def _season_payments(queryset, season):
    if season is None:
        return queryset
    return queryset.filter(season=season)
