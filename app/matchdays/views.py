"""Matchday views — admin + captain scoping, delete guard (B2)."""

from typing import ClassVar

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, DetailView, UpdateView

from app.core.models import AuditAction
from app.core.permissions import (
    assert_admin_or_captain,
    user_can_manage_team,
    user_is_admin,
    user_is_captain,
)
from app.core.services import log_action
from app.core.utils import numeric_pk
from app.matchdays.forms import MatchdayForm
from app.matchdays.models import Matchday
from app.matchdays.services import season_for_request
from app.penalties.models import Penalty
from app.teams.services import teams_for_season


class MatchdayListView(LoginRequiredMixin, View):
    """Admin: all matchdays of the active season, optional ``?team=`` filter.
    Captain: own team only (the team parameter is ignored for them).

    Columns are sortable via ``?sort=date|venue|opponent|team&dir=asc|desc``
    (default: date ascending — oldest entries first).
    """

    template_name = "matchdays/matchday_list.html"
    paginate_by = 25
    sort_fields: ClassVar[dict[str, str]] = {
        "date": "date",
        "venue": "venue",
        "opponent": "opponent",
        "team": "team__name",
    }

    def get(self, request):
        assert_admin_or_captain(request.user)
        season = season_for_request(request)
        queryset = Matchday.objects.select_related("team")
        if season is not None:
            queryset = queryset.filter(season=season)

        # Team selector: admins pick any season team (mirrors the financial
        # overview); captains stay locked to their own team.
        teams = None
        selected_team = None
        if user_is_admin(request.user):
            teams = list(teams_for_season(season).order_by("name"))
            team_id = request.GET.get("team", "")
            if team_id:
                selected_team = get_object_or_404(teams_for_season(season), pk=numeric_pk(team_id))
                queryset = queryset.filter(team=selected_team)
        elif user_is_captain(request.user):
            # Precedence admin > captain: hybrids take the admin branch above.
            if request.user.team_id is not None:
                queryset = queryset.filter(team_id=request.user.team_id)
            else:
                queryset = queryset.none()

        sort = request.GET.get("sort", "date")
        if sort not in self.sort_fields:
            sort = "date"
        # Default ASC — oldest matchdays first; only an explicit ?dir=desc flips it.
        direction = "desc" if request.GET.get("dir") == "desc" else "asc"
        prefix = "" if direction == "asc" else "-"
        queryset = queryset.order_by(f"{prefix}{self.sort_fields[sort]}", f"{prefix}created_at")

        page = Paginator(queryset, self.paginate_by).get_page(request.GET.get("page"))

        # Next direction per column: the active column toggles, a new column
        # starts with ascending order.
        sort_next = {
            key: ("desc" if key == sort and direction == "asc" else "asc")
            for key in self.sort_fields
        }
        query = request.GET.copy()
        query.pop("page", None)
        return render(
            request,
            self.template_name,
            {
                "page_obj": page,
                "teams": teams,
                "selected_team": selected_team,
                "sort": sort,
                "dir": direction,
                "sort_next": sort_next,
                "querystring": query.urlencode(),
            },
        )


class MatchdayCreateView(LoginRequiredMixin, CreateView):
    model = Matchday
    form_class = MatchdayForm
    template_name = "matchdays/matchday_form.html"
    success_url = reverse_lazy("matchdays:matchday_list")

    def dispatch(self, request, *args, **kwargs):
        assert_admin_or_captain(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["season"] = season_for_request(self.request)
        return kwargs

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        log_action(AuditAction.MATCHDAY_CREATED, user=self.request.user, target=self.object)
        messages.success(self.request, _("Matchday created."))
        return response


class MatchdayUpdateView(LoginRequiredMixin, UpdateView):
    model = Matchday
    form_class = MatchdayForm
    template_name = "matchdays/matchday_form.html"
    success_url = reverse_lazy("matchdays:matchday_list")

    def dispatch(self, request, *args, **kwargs):
        assert_admin_or_captain(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        matchday = super().get_object(queryset)
        if not user_can_manage_team(self.request.user, matchday.team):
            raise PermissionDenied
        return matchday

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["season"] = season_for_request(self.request)
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(AuditAction.MATCHDAY_UPDATED, user=self.request.user, target=self.object)
        messages.success(self.request, _("Matchday updated."))
        return response


class MatchdayDetailView(LoginRequiredMixin, DetailView):
    model = Matchday
    template_name = "matchdays/matchday_detail.html"
    context_object_name = "matchday"

    def dispatch(self, request, *args, **kwargs):
        assert_admin_or_captain(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        matchday = super().get_object(queryset)
        if not user_can_manage_team(self.request.user, matchday.team):
            raise PermissionDenied
        return matchday

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["participants"] = self.object.participants.select_related("player").order_by(
            "player__name"
        )
        context["penalties"] = self.object.penalties.select_related(
            "player", "catalog_item"
        ).order_by("-created_at")
        return context


class MatchdayDeleteView(LoginRequiredMixin, View):
    """Hard delete blocked when ANY penalty rows exist — incl. soft-deleted (B2)."""

    template_name = "matchdays/matchday_confirm_delete.html"

    def get_object(self):
        return get_object_or_404(Matchday, pk=self.kwargs["pk"])

    def dispatch(self, request, *args, **kwargs):
        assert_admin_or_captain(request.user)
        return super().dispatch(request, *args, **kwargs)

    def _check_permission(self, matchday):
        if not user_can_manage_team(self.request.user, matchday.team):
            raise PermissionDenied

    def get(self, request, pk):
        matchday = self.get_object()
        self._check_permission(matchday)
        return render(request, self.template_name, {"matchday": matchday})

    def post(self, request, pk):
        matchday = self.get_object()
        self._check_permission(matchday)
        has_penalties = Penalty.all_objects().filter(matchday=matchday).exists()
        if has_penalties:
            messages.error(
                request,
                _(
                    "This matchday cannot be deleted because it already has penalties "
                    "(including deleted ones). Delete the penalties first."
                ),
            )
            return redirect("matchdays:matchday_detail", pk=matchday.pk)
        log_action(AuditAction.MATCHDAY_DELETED, user=request.user, target=matchday)
        matchday.delete()
        messages.success(request, _("Matchday deleted."))
        return redirect("matchdays:matchday_list")
