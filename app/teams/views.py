"""Team views — admin management + captain-visible detail page + guarded delete."""

from decimal import Decimal
from typing import ClassVar

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from app.core.models import AuditAction
from app.core.permissions import (
    GROUP_ADMIN,
    GROUP_CAPTAIN,
    GroupRequiredMixin,
    user_can_manage_team,
)
from app.core.services import log_action
from app.matchdays.services import season_for_request
from app.penalties.services import inactive_player_balances, team_balances
from app.players.models import Player
from app.players.services import player_count_annotation
from app.teams.forms import TeamForm
from app.teams.models import Team
from app.teams.services import teams_for_season


class TeamListView(GroupRequiredMixin, LoginRequiredMixin, ListView):
    groups: ClassVar[list] = [GROUP_ADMIN]
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
        season = season_for_request(self.request)
        context["season"] = season
        # True when this season explicitly selected its teams (else ALL are active).
        context["season_teams_scoped"] = bool(season and season.teams.exists())
        return context


class TeamDetailView(GroupRequiredMixin, LoginRequiredMixin, DetailView):
    groups: ClassVar[list] = [GROUP_ADMIN, GROUP_CAPTAIN]
    model = Team
    template_name = "teams/team_detail.html"
    context_object_name = "team"

    def get_object(self, queryset=None):
        team = super().get_object(queryset)
        if not user_can_manage_team(self.request.user, team):
            raise PermissionDenied
        return team

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        team = self.object
        season = season_for_request(self.request)
        # Total/paid/open per roster player, scoped to THIS team and season (M3).
        rows = list(team_balances(team, season=season)) + list(
            inactive_player_balances(team, season=season)
        )
        totals = {row.pk: row.total for row in rows}
        balances = {row.pk: row.balance for row in rows}
        paid = {row.pk: row.paid for row in rows}
        context["player_rows"] = [
            {
                "player": player,
                "total": totals.get(player.pk, Decimal(0)),
                "balance": balances.get(player.pk, Decimal(0)),
                "paid": paid.get(player.pk, Decimal(0)),
            }
            # Season-dependent roster: players assigned to THIS team in the
            # active season (not every player who ever played for it).
            for player in Player.objects.in_team(team, season)
        ]
        return context


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
            return redirect("teams:team_detail", pk=team.pk)
        log_action(AuditAction.TEAM_DELETED, user=request.user, target=team)
        name = team.name
        team.delete()
        messages.success(request, _("Team '%(name)s' deleted.") % {"name": name})
        return redirect("teams:team_list")
