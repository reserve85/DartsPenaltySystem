"""Role-aware dashboard and financial overview (season-scoped money data)."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import get_object_or_404, render
from django.views import View

from app.core.permissions import (
    user_can_manage_team,
    user_is_admin,
    user_is_captain,
    user_is_player,
)
from app.core.utils import numeric_pk
from app.matchdays.models import Matchday
from app.matchdays.services import season_for_request
from app.penalties.models import Payment
from app.penalties.services import (
    assigned_penalties_qs,
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
from app.players.models import Player
from app.players.services import player_count_annotation, teams_for
from app.teams.models import Team
from app.teams.services import teams_for_season


class DashboardView(LoginRequiredMixin, View):
    template_name = "dashboard/dashboard.html"

    def get(self, request):
        season = season_for_request(request)
        context = {"role": request.user.role}
        if user_is_admin(request.user):
            context["teams"] = (
                teams_for_season(season)
                .annotate(
                    player_count=player_count_annotation(season),
                    matchday_count=Count("matchdays", distinct=True),
                )
                .order_by("name")
            )
        elif user_is_captain(request.user):
            team = request.user.team
            context["team"] = team
            if team is not None:
                balances = team_balances(team, season=season) + inactive_player_balances(
                    team, season=season
                )
                context["team_total"] = sum(row.balance for row in balances)
                context["team_penalties"] = team_penalties_total(team, season=season)
                context["team_paid"] = team_paid_total(team, season=season)
                matchday_qs = Matchday.objects.filter(team=team)
                if season is not None:
                    matchday_qs = matchday_qs.filter(season=season)
                # Season-dependent roster of the active season.
                context["player_count"] = Player.objects.in_team(team, season).count()
                context["matchday_count"] = matchday_qs.count()
        elif user_is_player(request.user):
            player = request.user.player_link
            context["player"] = player
            if player is not None:
                context["own_penalties"] = list(
                    assigned_penalties_qs(season=season).filter(player=player)[:10]
                )
                context["own_balance"] = player_balance(player, season=season)
                context["own_total"] = player_total(player, season=season)
                context["own_paid"] = player_total_paid(player, season=season)
            else:
                context["no_player_link"] = True
        return render(request, self.template_name, context)


class FinancialOverviewView(LoginRequiredMixin, View):
    """Admin: every team. Captain: own team. Player: own penalties + own team(s).

    Precedence matches ``User.role`` (admin > captain > player); any other
    approved account (no role yet) gets 403 — it must never fall through to
    the admin branch.
    """

    template_name = "dashboard/financial_overview.html"

    def get(self, request):
        season = season_for_request(request)
        if user_is_admin(request.user):
            # Only the teams active in the shown season are selectable.
            teams = list(teams_for_season(season).order_by("name"))
            team_id = request.GET.get("team")
            if not team_id and teams:
                team = teams[0]
            elif team_id:
                team = get_object_or_404(teams_for_season(season), pk=numeric_pk(team_id))
            else:
                team = None
        elif user_is_captain(request.user):
            if request.user.team_id is None:
                raise PermissionDenied
            teams = None
            team = request.user.team
        elif user_is_player(request.user):
            return self._render_player(request, season)
        else:
            raise PermissionDenied

        if team is None:
            return render(request, self.template_name, {"teams": teams, "selected_team": None})
        return render(request, self.template_name, self._context_for_team(team, teams, season))

    def _render_player(self, request, season):
        """Own penalties plus a READ-ONLY overview of the player's own team(s).

        ``?team=`` can only select among the teams the linked player belongs to
        in the shown season — anything else is a 404 (no foreign team names).
        """
        player = request.user.player_link
        if player is None:
            return render(request, self.template_name, {"own_mode": True, "no_player_link": True})
        context = {
            "own_mode": True,
            "player": player,
            "own_penalties": assigned_penalties_qs(season=season).filter(player=player),
            "own_balance": player_balance(player, season=season),
            "own_total": player_total(player, season=season),
            "own_paid": player_total_paid(player, season=season),
        }
        player_teams = teams_for(player, season)
        if player_teams:
            team_id = request.GET.get("team")
            if team_id:
                team = get_object_or_404(
                    Team, pk=numeric_pk(team_id), pk__in=[t.pk for t in player_teams]
                )
            else:
                team = player_teams[0]
            # Selector only when there is a choice (mirrors the captain behaviour).
            teams = player_teams if len(player_teams) > 1 else None
            context.update(self._context_for_team(team, teams, season))
        return render(request, self.template_name, context)

    def _context_for_team(self, team, teams, season):
        balances = team_balances(team, season=season)
        inactive = inactive_player_balances(team, season=season)
        return {
            "teams": teams,
            "selected_team": team,
            "team_balances": balances,
            # Team trio: total penalties, settled, still open (net = sum of rows)
            "team_penalties": team_penalties_total(team, season=season),
            "team_total": sum(row.balance for row in balances)
            + sum(row.balance for row in inactive),
            "team_paid": team_paid_total(team, season=season),
            # Write actions (record/revert payment) are Admin/Captain-only —
            # the Player role gets the very same overview read-only.
            "can_manage": user_can_manage_team(self.request.user, team),
            "payments": _season_payments(
                Payment.objects.filter(team=team).select_related("player"), season
            ),
            "inactive_balances": inactive,
            "matchday_totals": matchday_totals(team, season=season),
            "most_common": most_common_penalties(team, season=season),
            "assigned_penalties": assigned_penalties_qs(team, season=season),
        }


def _season_payments(queryset, season):
    if season is None:
        return queryset
    return queryset.filter(season=season)
