"""Player views — admin manages all teams, captains only their own team."""

from typing import ClassVar

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, DetailView, RedirectView, UpdateView

from app.core.models import AuditAction
from app.core.permissions import (
    GROUP_ADMIN,
    GROUP_CAPTAIN,
    GroupRequiredMixin,
    assert_admin_or_captain,
    user_can_view_player,
    user_is_player,
)
from app.core.services import log_action
from app.matchdays.services import season_for_request
from app.penalties.services import (
    assigned_penalties_qs,
    player_balance,
    player_team_balance,
    player_team_paid,
    player_team_total,
    player_total,
    player_total_paid,
)
from app.players.forms import PlayerForm
from app.players.models import Player
from app.players.services import teams_for


class PlayerDetailView(LoginRequiredMixin, DetailView):
    """Read-only player page: teams with per-team balances + penalty history."""

    model = Player
    template_name = "players/player_detail.html"
    context_object_name = "player"

    def get_object(self, queryset=None):
        player = super().get_object(queryset)
        user = self.request.user
        if not (
            user_can_view_player(user, player)
            or (user_is_player(user) and user.player_link_id == player.pk)
        ):
            raise PermissionDenied
        return player

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        player = self.object
        season = season_for_request(self.request)
        context["balance"] = player_balance(player, season=season)
        context["total"] = player_total(player, season=season)
        context["paid"] = player_total_paid(player, season=season)
        context["team_rows"] = [
            {
                "team": team,
                "total": player_team_total(player, team, season=season),
                "balance": player_team_balance(player, team, season=season),
                "paid": player_team_paid(player, team, season=season),
            }
            # Season-dependent assignment: only the active season's teams.
            for team in teams_for(player, season)
        ]
        context["penalties"] = assigned_penalties_qs(season=season).filter(player=player)[:50]
        return context


class PlayerListRedirectView(GroupRequiredMixin, LoginRequiredMixin, RedirectView):
    """The standalone players list was merged into the Teams & Players page."""

    groups: ClassVar[list] = [GROUP_ADMIN, GROUP_CAPTAIN]
    permanent = False
    url = reverse_lazy("teams:team_list")


class PlayerCreateView(LoginRequiredMixin, CreateView):
    model = Player
    form_class = PlayerForm
    template_name = "players/player_form.html"
    success_url = reverse_lazy("teams:team_list")

    def dispatch(self, request, *args, **kwargs):
        assert_admin_or_captain(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["season"] = season_for_request(self.request)
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.PLAYER_CREATED,
            user=self.request.user,
            target=self.object,
            metadata={"season_id": form.season.pk if getattr(form, "season", None) else None},
        )
        messages.success(self.request, _("Player created."))
        return response


class PlayerUpdateView(LoginRequiredMixin, UpdateView):
    model = Player
    form_class = PlayerForm
    template_name = "players/player_form.html"
    success_url = reverse_lazy("teams:team_list")

    def dispatch(self, request, *args, **kwargs):
        assert_admin_or_captain(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["season"] = season_for_request(self.request)
        return kwargs

    def get_object(self, queryset=None):
        player = super().get_object(queryset)
        if not user_can_view_player(self.request.user, player):
            raise PermissionDenied
        return player

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.PLAYER_UPDATED,
            user=self.request.user,
            target=self.object,
            metadata={"season_id": form.season.pk if getattr(form, "season", None) else None},
        )
        messages.success(self.request, _("Player updated."))
        return response


class PlayerDeactivateView(LoginRequiredMixin, View):
    """No hard delete — deactivation only. Keeps history and penalties."""

    def post(self, request, pk):
        assert_admin_or_captain(request.user)
        player = get_object_or_404(Player, pk=pk)
        if not user_can_view_player(request.user, player):
            raise PermissionDenied
        player.active = False
        player.save(update_fields=["active"])
        log_action(AuditAction.PLAYER_DEACTIVATED, user=request.user, target=player)
        messages.success(request, _("Player '%(name)s' deactivated.") % {"name": player.name})
        return redirect("teams:team_list")
