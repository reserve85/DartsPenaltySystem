"""Core views shared across the project (health, imprint, audit, seasons)."""

from decimal import Decimal
from typing import ClassVar

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from app.core.models import AuditAction, AuditLog
from app.core.permissions import GROUP_ADMIN, GroupRequiredMixin
from app.core.services import log_action
from app.core.utils import numeric_pk, safe_next_url
from app.matchdays.forms import SeasonForm
from app.matchdays.models import Season
from app.matchdays.services import set_active_season
from app.penalties.services import season_open_balance
from app.players.services import bind_unassigned_memberships


class HealthView(View):
    """Docker healthcheck endpoint — plain URL, outside i18n patterns."""

    def get(self, request):
        return JsonResponse({"status": "ok"})


class ImprintView(View):
    """Imprint page driven by CONTACT_* / IMPRINT_* settings (public)."""

    def get(self, request):
        return render(request, "core/imprint.html")


class PrivacyView(View):
    """Privacy Policy page driven by CONTACT_* settings (public)."""

    def get(self, request):
        return render(request, "core/privacy.html")


class AuditLogListView(GroupRequiredMixin, LoginRequiredMixin, View):
    """Admin-only, paginated, filterable audit trail."""

    groups: ClassVar[list] = [GROUP_ADMIN]
    template_name = "core/audit_log_list.html"
    paginate_by = 25

    def get(self, request):
        queryset = AuditLog.objects.select_related("user").order_by("-created_at")

        action = request.GET.get("action", "").strip()
        if action:
            queryset = queryset.filter(action=action)
        actor = request.GET.get("actor", "").strip()
        if actor:
            queryset = queryset.filter(user__email__icontains=actor)

        page = Paginator(queryset, self.paginate_by).get_page(request.GET.get("page"))

        return render(
            request,
            self.template_name,
            {
                "page_obj": page,
                "actions": AuditAction.choices,
                "selected_action": action,
                "actor": actor,
            },
        )


class SeasonListView(GroupRequiredMixin, LoginRequiredMixin, View):
    """Admin: manage seasons — each one a closed cash box."""

    groups: ClassVar[list] = [GROUP_ADMIN]
    template_name = "core/season_list.html"

    def get(self, request):
        seasons = Season.objects.annotate(
            matchday_count=Count("matchdays", distinct=True),
            payment_count=Count("payments", distinct=True),
            team_count=Count("teams", distinct=True),
        ).order_by("-name")
        return render(
            request,
            self.template_name,
            {"seasons": seasons, "default_season": Season.default()},
        )


class SeasonCreateView(GroupRequiredMixin, LoginRequiredMixin, View):
    """GET: empty season form. POST: create a new season."""

    groups: ClassVar[list] = [GROUP_ADMIN]
    template_name = "core/season_form.html"

    def get(self, request):
        return render(request, self.template_name, {"form": SeasonForm()})

    def post(self, request):
        form = SeasonForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                first_season = not Season.objects.exists()
                season = form.save()
                if first_season:
                    # Assignments created before the first season existed (fresh
                    # install) become that season's roster — every later season
                    # starts EMPTY and gets its assignment per season.
                    bind_unassigned_memberships(season)
                log_action(AuditAction.SEASON_CREATED, user=request.user, target=season)
            messages.success(request, _("Season created."))
        else:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
        return redirect("season_list")


class SeasonUpdateView(GroupRequiredMixin, LoginRequiredMixin, View):
    """POST/GET: rename a season, pick its active teams, toggle the default."""

    groups: ClassVar[list] = [GROUP_ADMIN]
    template_name = "core/season_form.html"

    def get_object(self):
        return get_object_or_404(Season, pk=self.kwargs["pk"])

    def get(self, request, pk):
        season = self.get_object()
        return render(
            request, self.template_name, {"form": SeasonForm(instance=season), "season": season}
        )

    def post(self, request, pk):
        season = self.get_object()
        form = SeasonForm(request.POST, instance=season)
        if form.is_valid():
            form.save()
            log_action(
                AuditAction.SEASON_UPDATED,
                user=request.user,
                target=season,
                metadata={"is_default": season.is_default},
            )
            messages.success(request, _("Season updated."))
        else:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
        return redirect("season_list")


class SeasonSetDefaultView(GroupRequiredMixin, LoginRequiredMixin, View):
    """POST-only: mark one season as the GLOBAL default for every user.

    This is the only place that changes the default of all users — the navbar
    dropdown never touches it (it only stores a session-scoped view choice).
    """

    groups: ClassVar[list] = [GROUP_ADMIN]

    def post(self, request, pk):
        season = get_object_or_404(Season, pk=pk)
        season.is_default = True
        season.save()  # clears the flag of every other season
        log_action(
            AuditAction.SEASON_UPDATED,
            user=request.user,
            target=season,
            metadata={"default": True},
        )
        messages.success(
            request, _("Season '%(name)s' is now the default season.") % {"name": season.name}
        )
        return redirect("season_list")


class SeasonDeleteView(GroupRequiredMixin, LoginRequiredMixin, View):
    """POST-only: only truly empty seasons can go.

    Blocked when the season still holds **open amounts** (Σ penalties ≠ Σ
    payments), matchdays, payments or player-team assignments (roster
    history would be cascade-deleted otherwise).
    """

    groups: ClassVar[list] = [GROUP_ADMIN]

    def post(self, request, pk):
        season = get_object_or_404(Season, pk=pk)
        if season_open_balance(season) != Decimal(0):
            messages.error(
                request,
                _(
                    "This season cannot be deleted because it still has open amounts. "
                    "Settle all balances first."
                ),
            )
            return redirect("season_list")
        if season.matchdays.exists() or season.payments.exists():
            messages.error(
                request,
                _("This season cannot be deleted because it still has matchdays or payments."),
            )
            return redirect("season_list")
        if season.player_team_assignments.exists():
            messages.error(
                request,
                _("This season cannot be deleted because players are still assigned to it."),
            )
            return redirect("season_list")
        log_action(AuditAction.SEASON_DELETED, user=request.user, target=season)
        season.delete()
        messages.success(request, _("Season deleted."))
        return redirect("season_list")


class SeasonSetView(LoginRequiredMixin, View):
    """POST-only: pick the season shown by THIS user (session only).

    Purely personal: it never changes the admin-set default season, so other
    users keep seeing the default. The choice lasts as long as the session.
    """

    def post(self, request):
        fallback = safe_next_url(request, "/")
        # Non-numeric / empty ids are 404s, not unhandled ValueErrors (500).
        season = Season.objects.filter(pk=numeric_pk(request.POST.get("season"))).first()
        if season is None:
            messages.error(request, _("Season not found."))
            return redirect(fallback)
        set_active_season(request, season)
        return redirect(fallback)
