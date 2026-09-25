"""Penalty views — catalog management + assign/edit/delete (service layer only)."""

from decimal import Decimal, InvalidOperation
from typing import ClassVar

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, UpdateView

from app.core.permissions import (
    GROUP_ADMIN,
    GroupRequiredMixin,
    assert_admin_or_captain,
    user_can_manage_team,
    user_is_admin,
    user_is_captain,
)
from app.core.utils import safe_next_url
from app.matchdays.models import Matchday
from app.matchdays.services import season_for_request
from app.penalties.forms import (
    CatalogItemForm,
    PaymentForm,
    PenaltyAssignForm,
    PenaltyEditForm,
)
from app.penalties.models import (
    Payment,
    Penalty,
    PenaltyCatalogItem,
    PenaltyType,
    TeamCatalogAmount,
)
from app.penalties.services import (
    assert_can_manage_penalty,
    assign_group_penalty,
    assign_penalty,
    delete_payment,
    edit_penalty,
    record_payment,
    soft_delete_penalty,
)
from app.teams.models import Team
from app.teams.services import teams_for_season


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class CatalogListView(LoginRequiredMixin, View):
    """Admin: manage. Captain: view + own team fees. Player: 403."""

    template_name = "penalties/catalog_list.html"

    def get(self, request):
        assert_admin_or_captain(request.user)
        items = list(PenaltyCatalogItem.objects.order_by("type", "description"))
        overrides: dict[int, list] = {}
        for team_amount in TeamCatalogAmount.objects.select_related("team").filter(
            catalog_item__in=items
        ):
            overrides.setdefault(team_amount.catalog_item_id, []).append(team_amount)
        for item in items:
            item.team_amount_list = sorted(
                overrides.get(item.pk, []), key=lambda row: row.team.name
            )
        return render(
            request,
            self.template_name,
            {"items": items, "can_manage": user_is_admin(request.user)},
        )


class CatalogTeamAmountsView(LoginRequiredMixin, View):
    """Per-team fee overrides for ONE catalog item.

    Admin: every team. Captain: own team only (foreign team ids are ignored).
    A blank input clears the override so the team falls back to the default.
    """

    template_name = "penalties/catalog_team_amounts.html"

    def get_item(self):
        return get_object_or_404(PenaltyCatalogItem, pk=self.kwargs["pk"])

    def _teams(self):
        user = self.request.user
        if user_is_admin(user):
            # Only teams active in the shown season carry a fee row.
            season = season_for_request(self.request)
            return list(teams_for_season(season).order_by("name"))
        if user_is_captain(user) and user.team_id is not None:
            return list(Team.objects.filter(pk=user.team_id))
        raise PermissionDenied

    def _render(self, request, item, rows):
        return render(
            request,
            self.template_name,
            {"item": item, "rows": rows, "default_amount": item.amount_eur},
        )

    def get(self, request, pk):
        assert_admin_or_captain(request.user)
        item = self.get_item()
        teams = self._teams()
        current = {row.team_id: row.amount_eur for row in item.team_amounts.filter(team__in=teams)}
        rows = [{"team": team, "value": current.get(team.pk, "")} for team in teams]
        return self._render(request, item, rows)

    def post(self, request, pk):
        assert_admin_or_captain(request.user)
        item = self.get_item()
        teams = self._teams()

        parsed: dict = {}
        errors: list = []
        for team in teams:
            raw = (request.POST.get(f"team_{team.pk}") or "").strip()
            if raw == "":
                parsed[team.pk] = None  # back to default
                continue
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                errors.append(
                    _("'%(value)s' is not a valid amount for team '%(team)s'.")
                    % {"value": raw, "team": team.name}
                )
                continue
            if amount <= 0:
                errors.append(
                    _("The amount for team '%(team)s' must be a positive number or empty.")
                    % {"team": team.name}
                )
                continue
            parsed[team.pk] = amount

        if errors:
            for error in errors:
                messages.error(request, error)
            rows = [
                {"team": team, "value": request.POST.get(f"team_{team.pk}", "")} for team in teams
            ]
            return self._render(request, item, rows)

        for team in teams:
            amount = parsed.get(team.pk)
            if amount is None:
                item.team_amounts.filter(team=team).delete()
            else:
                TeamCatalogAmount.objects.update_or_create(
                    team=team,
                    catalog_item=item,
                    defaults={"amount_eur": amount, "updated_by": request.user},
                )
        messages.success(request, _("Team fees saved."))
        return redirect("penalties:catalog_list")


class CatalogCreateView(GroupRequiredMixin, LoginRequiredMixin, CreateView):
    groups: ClassVar[list] = [GROUP_ADMIN]
    model = PenaltyCatalogItem
    form_class = CatalogItemForm
    template_name = "penalties/catalog_form.html"
    success_url = reverse_lazy("penalties:catalog_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("Catalog item created."))
        return response


class CatalogUpdateView(GroupRequiredMixin, LoginRequiredMixin, UpdateView):
    groups: ClassVar[list] = [GROUP_ADMIN]
    model = PenaltyCatalogItem
    form_class = CatalogItemForm
    template_name = "penalties/catalog_form.html"
    success_url = reverse_lazy("penalties:catalog_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("Catalog item updated."))
        return response


class CatalogToggleActiveView(GroupRequiredMixin, LoginRequiredMixin, View):
    groups: ClassVar[list] = [GROUP_ADMIN]

    def post(self, request, pk):
        item = get_object_or_404(PenaltyCatalogItem, pk=pk)
        item.active = not item.active
        item.save(update_fields=["active"])
        state = _("activated") if item.active else _("deactivated")
        messages.success(
            request,
            _("Catalog item '%(name)s' %(state)s.") % {"name": item.description, "state": state},
        )
        return redirect("penalties:catalog_list")


# ---------------------------------------------------------------------------
# Assignment / edit / delete
# ---------------------------------------------------------------------------
class PenaltyCreateView(LoginRequiredMixin, View):
    """Dispatch by catalog type: NORMAL -> assign_penalty, group -> assign_group_penalty."""

    template_name = "penalties/penalty_form.html"

    def get_matchday(self):
        matchday = get_object_or_404(Matchday, pk=self.kwargs["matchday_pk"])
        if not user_can_manage_team(self.request.user, matchday.team):
            raise PermissionDenied
        return matchday

    def _render(self, request, matchday, form):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "matchday": matchday,
                "manual_ids": getattr(form, "manual_ids", []),
            },
        )

    def get(self, request, matchday_pk):
        matchday = self.get_matchday()
        return self._render(request, matchday, PenaltyAssignForm(matchday=matchday))

    def post(self, request, matchday_pk):
        matchday = self.get_matchday()
        form = PenaltyAssignForm(request.POST, matchday=matchday)
        if form.is_valid():
            catalog_item = form.cleaned_data["catalog_item"]
            player = form.cleaned_data["player"]
            try:
                if catalog_item.type == PenaltyType.PER_ALL_OTHER_MATCHDAY_PLAYERS:
                    created = assign_group_penalty(
                        matchday=matchday,
                        trigger_player=player,
                        catalog_item=catalog_item,
                        actor=request.user,
                    )
                else:
                    # NORMAL: catalog/team amount; MANUAL: individual amount + comment.
                    assign_penalty(
                        matchday=matchday,
                        player=player,
                        catalog_item=catalog_item,
                        amount_eur=form.cleaned_data.get("amount_eur"),
                        description_snapshot=form.cleaned_data.get("description"),
                        actor=request.user,
                    )
            except ValidationError as exc:
                # Service guard (e.g. group penalty on a matchday whose roster
                # shrank since page load) — show it as a form error, never 500.
                for error in exc.messages:
                    form.add_error(None, error)
                return self._render(request, matchday, form)
            if catalog_item.type == PenaltyType.PER_ALL_OTHER_MATCHDAY_PLAYERS:
                messages.success(
                    request,
                    _("%(count)s penalties assigned to the other participants.")
                    % {"count": len(created)},
                )
            else:
                messages.success(request, _("Penalty assigned."))
            return redirect("matchdays:matchday_detail", pk=matchday.pk)
        return self._render(request, matchday, form)


class PenaltyUpdateView(LoginRequiredMixin, View):
    template_name = "penalties/penalty_form.html"

    def get_penalty(self):
        # Default manager -> operating on a soft-deleted penalty is a 404.
        penalty = get_object_or_404(Penalty, pk=self.kwargs["pk"])
        assert_can_manage_penalty(self.request.user, penalty)
        return penalty

    def get(self, request, pk):
        penalty = self.get_penalty()
        form = PenaltyEditForm(instance=penalty)
        return render(
            request,
            self.template_name,
            {"form": form, "matchday": penalty.matchday, "editing": penalty},
        )

    def post(self, request, pk):
        penalty = self.get_penalty()
        form = PenaltyEditForm(request.POST, instance=penalty)
        if form.is_valid():
            edit_penalty(
                penalty=penalty,
                actor=request.user,
                amount_eur=form.cleaned_data["amount_eur"],
                description_snapshot=form.cleaned_data["description_snapshot"],
            )
            messages.success(request, _("Penalty updated (all rows of the group, if any)."))
            return redirect("matchdays:matchday_detail", pk=penalty.matchday.pk)
        return render(
            request,
            self.template_name,
            {"form": form, "matchday": penalty.matchday, "editing": penalty},
        )


class PenaltyDeleteView(LoginRequiredMixin, View):
    """POST-only soft delete — audit history is preserved (B2)."""

    def post(self, request, pk):
        penalty = get_object_or_404(Penalty, pk=pk)
        assert_can_manage_penalty(self.request.user, penalty)
        soft_delete_penalty(penalty=penalty, actor=request.user)
        messages.success(request, _("Penalty deleted."))
        return redirect("matchdays:matchday_detail", pk=penalty.matchday.pk)


# ---------------------------------------------------------------------------
# Payments (partial payments against a player's team debt)
# ---------------------------------------------------------------------------


class PaymentCreateView(LoginRequiredMixin, View):
    """POST: record a (partial) payment — Admin or Captain of THAT team."""

    def post(self, request):
        fallback = safe_next_url(request, reverse("dashboard:financial_overview"))
        form = PaymentForm(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect(fallback)
        team = form.cleaned_data["team"]
        if not user_can_manage_team(request.user, team):
            raise PermissionDenied
        record_payment(
            player=form.cleaned_data["player"],
            team=team,
            amount_eur=form.cleaned_data["amount_eur"],
            actor=request.user,
            season=season_for_request(request),
        )
        messages.success(request, _("Payment recorded."))
        return redirect(fallback)


class PaymentDeleteView(LoginRequiredMixin, View):
    """POST: revert a mis-recorded payment (Admin / Captain of the team)."""

    def post(self, request, pk):
        payment = get_object_or_404(Payment, pk=pk)
        if not user_can_manage_team(request.user, payment.team):
            raise PermissionDenied
        delete_payment(payment=payment, actor=request.user)
        messages.success(request, _("Payment reverted."))
        return redirect(safe_next_url(request, reverse("dashboard:financial_overview")))
