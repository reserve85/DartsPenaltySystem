"""Team views — combined Teams & Players page (matrix) + admin CRUD + guarded delete + Kasse."""

from typing import ClassVar

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Value
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, ListView, RedirectView, UpdateView

from app.core.models import AuditAction
from app.core.pagination import PAGE_SIZE
from app.core.permissions import (
    GROUP_ADMIN,
    GROUP_CAPTAIN,
    GroupRequiredMixin,
    user_can_edit_player,
    user_can_manage_team,
    user_is_admin,
)
from app.core.services import log_action
from app.matchdays.services import season_for_request
from app.penalties.models import PenaltyCatalogItem, TeamCatalogAmount
from app.players.models import Player
from app.players.services import assign_teams, player_count_annotation, prefetch_season_assignments
from app.teams.forms import CashierForm, TeamForm, approved_active_users_queryset
from app.teams.models import Team, TeamCashier
from app.teams.services import (
    captains_for_teams,
    clear_cashier,
    set_cashier,
    teams_for_season,
)


class TeamListView(GroupRequiredMixin, LoginRequiredMixin, ListView):
    """Combined **Teams & Players** management page (admin + captain).

    Top: the team table — create/edit/delete are ADMIN-only, captains see it
    read-only. Bottom: the player list with an assignment MATRIX (players as
    rows, the active season's teams as columns). Checking a box assigns the
    player to that team FOR THE ACTIVE SEASON only.

    Captains see every player but may only toggle THEIR OWN team column —
    enforced server-side (disabled checkboxes are never trusted).
    """

    groups: ClassVar[list] = [GROUP_ADMIN, GROUP_CAPTAIN]
    model = Team
    template_name = "teams/team_list.html"
    context_object_name = "teams"

    def get_queryset(self):
        # The top table lists ALL teams (highlighted per season); the matrix
        # columns below stay season-scoped (context key ``matrix_teams``).
        # Player count is season-dependent (active season from the navbar).
        season = season_for_request(self.request)
        if season is None or not season.teams.exists():
            # No season / no explicit selection => every team is active.
            active_in_season = Value(True)
        else:
            active_in_season = Exists(season.teams.filter(pk=OuterRef("pk")))
        return Team.objects.annotate(
            active_in_season=active_in_season,
            captain_count=Count("users", distinct=True),
            player_count=player_count_annotation(season),
        ).order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        season = season_for_request(request)
        context["season"] = season
        # True when this season explicitly selected its teams (else ALL are active).
        context["season_teams_scoped"] = bool(season and season.teams.exists())

        # --- assignment matrix -------------------------------------------------
        # Columns = the season-active subset only (per decision); the top team
        # table above shows every team with its active-state badge.
        context["matrix_teams"] = [team for team in context["teams"] if team.active_in_season]
        column_pks = {team.pk for team in context["matrix_teams"]}
        if user_is_admin(request.user):
            context["editable_team_pks"] = column_pks
        elif request.user.team_id:
            context["editable_team_pks"] = {request.user.team_id} & column_pks
        else:
            context["editable_team_pks"] = set()

        # The matrix holds ALL players (captains assign to their own team only).
        players = Player.objects.prefetch_related(prefetch_season_assignments(season)).order_by(
            "name", "pk"
        )
        page = Paginator(players, PAGE_SIZE).get_page(request.GET.get("page"))
        for player in page:
            # Flat pk list — the template checks membership per column cell.
            player.assigned_team_pks = [a.team_id for a in player.season_assignments]
            # WRITE gate for the "Edit" button (READ of the page is open anyway).
            player.can_edit = user_can_edit_player(request.user, player)
        context["page_obj"] = page
        return context

    def post(self, request):
        """Save the checkbox matrix for the active season (diff per row)."""
        season = season_for_request(request)
        columns = {t.pk: t for t in teams_for_season(season)}
        column_pks = set(columns)
        if user_is_admin(request.user):
            editable = column_pks
        elif request.user.team_id:
            editable = {request.user.team_id} & column_pks
        else:
            editable = set()

        checked: set[tuple[int, int]] = set()
        for value in request.POST.getlist("assign"):
            try:
                player_pk, team_pk = (int(part) for part in value.split(":", 1))
            except ValueError:
                continue
            checked.add((player_pk, team_pk))

        player_pks = []
        for value in request.POST.getlist("players"):
            try:
                player_pks.append(int(value))
            except ValueError:
                continue

        changed = 0
        queryset = Player.objects.prefetch_related(prefetch_season_assignments(season)).filter(
            pk__in=player_pks
        )
        for player in queryset:
            current = {a.team_id for a in player.season_assignments}
            desired = {t_pk for (p_pk, t_pk) in checked if p_pk == player.pk}
            # Only THIS user's editable columns change; everything else
            # (other teams of the season, teams outside the season scope)
            # stays exactly as it is.
            final_pks = (current - editable) | (desired & editable)
            if final_pks == current:
                continue
            final = [a.team for a in player.season_assignments if a.team_id in final_pks]
            final += [columns[t_pk] for t_pk in sorted(final_pks - current)]
            assign_teams(player, final, season=season)
            changed += 1

        if changed:
            log_action(
                AuditAction.PLAYER_UPDATED,
                user=request.user,
                metadata={
                    "season_id": season.pk if season else None,
                    "changed_players": changed,
                },
            )
            messages.success(
                request,
                _("Assignments saved for %(count)d player(s).") % {"count": changed},
            )
        else:
            messages.success(request, _("No assignment changes."))
        return redirect("teams:team_list")


class TeamDetailRedirectView(GroupRequiredMixin, LoginRequiredMixin, RedirectView):
    """The standalone team page was merged into the financial overview."""

    groups: ClassVar[list] = [GROUP_ADMIN, GROUP_CAPTAIN]
    permanent = False

    def get_redirect_url(self, *args, **kwargs):
        return f"{reverse('dashboard:financial_overview')}?team={kwargs['pk']}"


class TeamCreateView(GroupRequiredMixin, LoginRequiredMixin, CreateView):
    groups: ClassVar[list] = [GROUP_ADMIN]
    model = Team
    form_class = TeamForm
    template_name = "teams/team_form.html"
    success_url = reverse_lazy("teams:team_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["include_season"] = True  # create-only season assignment field
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        season = form.cleaned_data.get("season")
        if season is not None:
            season.teams.add(self.object)
        # A NEW team gets the standard fee assigned for every existing catalog
        # entry (own row per item — from then on each team carries its own fee).
        catalog_items = list(PenaltyCatalogItem.objects.all())
        TeamCatalogAmount.objects.bulk_create(
            [
                TeamCatalogAmount(
                    team=self.object,
                    catalog_item=item,
                    active=True,
                    amount_eur=item.amount_eur,
                    updated_by=self.request.user,
                )
                for item in catalog_items
            ],
            ignore_conflicts=True,
        )
        log_action(
            AuditAction.TEAM_CREATED,
            user=self.request.user,
            target=self.object,
            metadata={
                "season_id": season.pk if season is not None else None,
                "catalog_fees_assigned": len(catalog_items),
            },
        )
        messages.success(self.request, _("Team created."))
        return response


class TeamUpdateView(GroupRequiredMixin, LoginRequiredMixin, UpdateView):
    groups: ClassVar[list] = [GROUP_ADMIN]
    model = Team
    form_class = TeamForm
    template_name = "teams/team_form.html"
    success_url = reverse_lazy("teams:team_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(AuditAction.TEAM_UPDATED, user=self.request.user, target=self.object)
        messages.success(self.request, _("Team updated."))
        return response


class TeamDeleteView(GroupRequiredMixin, LoginRequiredMixin, View):
    """Hard delete only for empty teams (B2) — otherwise force-reject."""

    groups: ClassVar[list] = [GROUP_ADMIN]
    template_name = "teams/team_confirm_delete.html"

    def get_object(self):
        return get_object_or_404(Team, pk=self.kwargs["pk"])

    def get(self, request, pk):
        return render(request, self.template_name, {"team": self.get_object()})

    def post(self, request, pk):
        team = self.get_object()
        if not team.deletable():
            messages.error(
                request,
                _(
                    "This team cannot be deleted because it still contains players, "
                    "matchdays or linked captain accounts."
                ),
            )
            return redirect("teams:team_list")
        log_action(AuditAction.TEAM_DELETED, user=request.user, target=team)
        name = team.name
        team.delete()
        messages.success(request, _("Team '%(name)s' deleted.") % {"name": name})
        return redirect("teams:team_list")


# ---------------------------------------------------------------------------
# Kasse — cashier assignment (admin + captain of the respective team)
# ---------------------------------------------------------------------------
class CashierListView(GroupRequiredMixin, LoginRequiredMixin, ListView):
    """Cash box page: current cashier + full history + assign form per team.

    Admins may edit every team, captains only their own (decision 8,
    enforced server-side in ``CashierUpdateView``); everyone else gets 403.
    Option labels are privacy-aware (review L4): captains never see the
    plain e-mail of an account that has a ``player_link``.
    """

    groups: ClassVar[list] = [GROUP_ADMIN, GROUP_CAPTAIN]
    model = Team
    template_name = "teams/cashier_list.html"
    context_object_name = "teams"
    queryset = Team.objects.all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        teams = list(context["teams"])

        # ONE query for the open rows, ONE for the history (review L6: the
        # page reads open rows directly — no second lookup helper).
        open_rows = list(
            TeamCashier.objects.filter(valid_to__isnull=True).select_related("user__player_link")
        )
        current = {row.team_id: row.user for row in open_rows}
        history_by_team: dict = {team.pk: [] for team in teams}
        for row in TeamCashier.objects.select_related("user__player_link", "created_by", "team"):
            history_by_team.setdefault(row.team_id, []).append(row)

        is_admin = user_is_admin(user)
        captains_by_team = captains_for_teams(teams)
        # Template convenience attributes (DTL cannot subscript dicts with a
        # variable key) — the named context dicts stay the contract for tests.
        for team in teams:
            team.current_cashier = current.get(team.pk)
            team.can_edit = is_admin or user.team_id == team.pk
            team.history = history_by_team.get(team.pk, [])
            # Marker rule (👑/💲): ids of THIS team's captains, so the template
            # can stack the crown whenever an account holds BOTH roles.
            team.captain_ids = {captain.pk for captain in captains_by_team.get(team.pk, [])}
        context.update(
            {
                "current_cashier_by_team": current,
                "history_by_team": history_by_team,
                "assignable_users": list(approved_active_users_queryset()),
                "can_edit_by_team": {
                    team.pk: is_admin or user.team_id == team.pk for team in teams
                },
                "is_admin": is_admin,
                "form": CashierForm(user=user),
            }
        )
        return context


class CashierUpdateView(LoginRequiredMixin, View):
    """POST-only: assign (or clear) the cashier of one team.

    Permission: ``user_can_manage_team`` (admins any team, captains their
    own) — HTTP 403 otherwise. The unique-constraint race is surfaced as a
    validation message, never a 500 (review M5).
    """

    http_method_names: ClassVar[list] = ["post"]

    def post(self, request):
        # Permission FIRST on the RAW team id: a captain posting a foreign
        # team gets 403 (the narrowed form queryset would only yield a
        # validation message for the same crafted POST).
        raw_team = request.POST.get("team") or ""
        if raw_team.isdigit():
            team_obj = Team.objects.filter(pk=int(raw_team)).first()
            if team_obj is not None and not user_can_manage_team(request.user, team_obj):
                raise PermissionDenied
        form = CashierForm(request.POST, user=request.user)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect("teams:cashier_list")
        team = form.cleaned_data["team"]
        cashier = form.cleaned_data["user"]
        if not user_can_manage_team(request.user, team):
            raise PermissionDenied
        try:
            if cashier is None:
                clear_cashier(team=team, actor=request.user)
                messages.success(request, _("Cashier removed."))
            else:
                set_cashier(team=team, user=cashier, actor=request.user)
                messages.success(request, _("Cashier updated."))
        except ValidationError as error:
            messages.error(request, "; ".join(error.messages))
        return redirect("teams:cashier_list")
