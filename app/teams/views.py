"""Team views — combined Teams & Players page (matrix) + admin CRUD + guarded delete."""

from typing import ClassVar

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, ListView, RedirectView, UpdateView

from app.core.models import AuditAction
from app.core.permissions import (
    GROUP_ADMIN,
    GROUP_CAPTAIN,
    GroupRequiredMixin,
    user_is_admin,
)
from app.core.services import log_action
from app.matchdays.services import season_for_request
from app.players.models import Player
from app.players.services import assign_teams, player_count_annotation, prefetch_season_assignments
from app.teams.forms import TeamForm
from app.teams.models import Team
from app.teams.services import teams_for_season


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
        # Player count is season-dependent (active season from the navbar) and
        # only teams active in that season are listed (per-season teams).
        season = season_for_request(self.request)
        return (
            teams_for_season(season)
            .annotate(
                captain_count=Count("users", distinct=True),
                player_count=player_count_annotation(season),
            )
            .order_by("name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        season = season_for_request(request)
        context["season"] = season
        # True when this season explicitly selected its teams (else ALL are active).
        context["season_teams_scoped"] = bool(season and season.teams.exists())

        # --- assignment matrix -------------------------------------------------
        columns = list(context["teams"])
        column_pks = {t.pk for t in columns}
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
        page = Paginator(players, 25).get_page(request.GET.get("page"))
        for player in page:
            # Flat pk list — the template checks membership per column cell.
            player.assigned_team_pks = [a.team_id for a in player.season_assignments]
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

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(AuditAction.TEAM_CREATED, user=self.request.user, target=self.object)
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
