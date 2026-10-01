"""Penalty views — catalog management + assign/edit/delete (service layer only)."""

from typing import ClassVar

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, UpdateView

from app.core.models import AuditAction
from app.core.permissions import (
    GROUP_ADMIN,
    GROUP_CAPTAIN,
    GroupRequiredMixin,
    assert_admin_or_captain,
    user_can_manage_team,
    user_is_admin,
    user_is_captain,
)
from app.core.services import log_action
from app.core.utils import safe_next_url
from app.matchdays.models import Matchday
from app.matchdays.services import season_for_request
from app.penalties.forms import (
    CatalogItemForm,
    PaymentForm,
    PayoutForm,
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
    record_payout,
    soft_delete_penalty,
)
from app.teams.models import Team
from app.teams.services import cashier_user_for, user_is_cashier


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class CatalogListView(LoginRequiredMixin, View):
    """Admin + Captain: manage the catalog (players get 403).

    The list shows every team's effective fee/active state per item (read-only
    status column) and a single "Edit" button — team fees and active flags are
    edited in the integrated catalog form.
    """

    template_name = "penalties/catalog_list.html"

    def get(self, request):
        assert_admin_or_captain(request.user)
        items = list(PenaltyCatalogItem.objects.order_by("type", "description"))
        teams = list(Team.objects.order_by("name"))
        rows = {
            (row.catalog_item_id, row.team_id): row
            for row in TeamCatalogAmount.objects.filter(catalog_item__in=items)
        }
        for item in items:
            # Missing row = active with the default amount (legacy safety);
            # normally every team row exists and carries its own fee.
            item.team_config_list = []
            for team in teams:
                row = rows.get((item.pk, team.pk))
                amount = row.amount_eur if row is not None else None
                item.team_config_list.append(
                    {
                        "team": team,
                        "active": True if row is None else row.active,
                        "amount": amount if amount is not None else item.amount_eur,
                    }
                )
        can_create = user_is_admin(request.user) or user_is_captain(request.user)
        return render(
            request,
            self.template_name,
            {"items": items, "can_create": can_create},
        )


class CatalogCreateView(GroupRequiredMixin, LoginRequiredMixin, CreateView):
    """Admin + Captain create — the form also owns the per-team fees/flags."""

    groups: ClassVar[list] = [GROUP_ADMIN, GROUP_CAPTAIN]
    model = PenaltyCatalogItem
    form_class = CatalogItemForm
    template_name = "penalties/catalog_form.html"
    success_url = reverse_lazy("penalties:catalog_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.CATALOG_ITEM_CREATED,
            user=self.request.user,
            target=self.object,
            metadata=form.created_metadata(),
        )
        messages.success(self.request, _("Catalog item created."))
        return response


class CatalogUpdateView(GroupRequiredMixin, LoginRequiredMixin, UpdateView):
    """Admin + Captain edit — core fields + per-team fees/flags + delete entry."""

    groups: ClassVar[list] = [GROUP_ADMIN, GROUP_CAPTAIN]
    model = PenaltyCatalogItem
    form_class = CatalogItemForm
    template_name = "penalties/catalog_form.html"
    success_url = reverse_lazy("penalties:catalog_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Pristine pre-save state: ModelForm.is_valid() writes the cleaned
        # core fields INTO the instance, so the snapshot must happen here.
        if form.instance is not None and form.instance.pk is not None:
            self._before = CatalogItemForm.snapshot_state(form.instance)
        return form

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.CATALOG_ITEM_UPDATED,
            user=self.request.user,
            target=self.object,
            metadata=form.updated_metadata(before=self._before),
        )
        messages.success(self.request, _("Catalog item updated."))
        return response


class CatalogDeleteView(LoginRequiredMixin, View):
    """POST-only guarded delete (Admin + Captain) — only while unused.

    ``Penalty.all_objects()`` includes soft-deleted rows: once ANY penalty row
    ever referenced the item, deletion is blocked so audit history (the
    ``SET_NULL`` catalog reference) stays intact.
    """

    def post(self, request, pk):
        assert_admin_or_captain(request.user)
        item = get_object_or_404(PenaltyCatalogItem, pk=pk)
        if Penalty.all_objects().filter(catalog_item=item).exists():
            messages.error(
                request,
                _(
                    "This catalog item cannot be deleted because it is still "
                    "referenced by penalties."
                ),
            )
            return redirect("penalties:catalog_list")
        with transaction.atomic():
            log_action(
                AuditAction.CATALOG_ITEM_DELETED,
                user=request.user,
                target=item,
                metadata={"description": item.description},
            )
            item.delete()  # cascades the per-team rows
        messages.success(request, _("Catalog item deleted."))
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
                "both_ids": getattr(form, "both_ids", []),
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
            # Optional (flagged items only) — ``clean()`` cleared it otherwise.
            double_partner = form.cleaned_data.get("double_partner")
            try:
                if catalog_item.type == PenaltyType.PER_ALL_OTHER_MATCHDAY_PLAYERS:
                    created = assign_group_penalty(
                        matchday=matchday,
                        trigger_player=player,
                        catalog_item=catalog_item,
                        double_partner=double_partner,
                        # Optional comment/reason — stored behind the causers.
                        description=form.cleaned_data.get("description"),
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
                        double_partner=double_partner,
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
            elif double_partner is not None:
                messages.success(request, _("Penalty assigned to both players."))
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

    @staticmethod
    def _context(penalty, form) -> dict:
        """Render context incl. the group headline data.

        The template shows the NAMES of the affected group players — never the
        raw ``group_id`` hex string (meaningless to captains/players).
        ``group_count > 1`` is the template's "this is a group penalty with
        siblings" flag and drives the single-vs-group delete choice.
        """
        rows = (
            list(Penalty.objects.filter(group_id=penalty.group_id).select_related("player"))
            if penalty.group_id
            else [penalty]
        )
        return {
            "form": form,
            "matchday": penalty.matchday,
            "editing": penalty,
            "group_count": len(rows),
            "group_players": ", ".join(sorted({row.player.name for row in rows})),
        }

    def get(self, request, pk):
        penalty = self.get_penalty()
        form = PenaltyEditForm(instance=penalty)
        return render(request, self.template_name, self._context(penalty, form))

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
        return render(request, self.template_name, self._context(penalty, form))


class PenaltyDeleteView(LoginRequiredMixin, View):
    """POST-only soft delete — audit history is preserved (B2).

    For a GROUP penalty the edit page asks which scope applies:
    ``scope=single`` deletes only this player's row, ``scope=group`` (default)
    every row sharing the ``group_id``. Guards: Admin or the matchday team's
    captain (``assert_can_manage_penalty``); an unknown scope falls back to
    the group semantics instead of failing.
    """

    def post(self, request, pk):
        penalty = get_object_or_404(Penalty, pk=pk)
        assert_can_manage_penalty(self.request.user, penalty)
        scope = request.POST.get("scope", "group")
        if scope not in ("single", "group"):
            scope = "group"
        soft_delete_penalty(penalty=penalty, actor=request.user, scope=scope)
        if scope == "group" and penalty.group_id:
            messages.success(request, _("Penalty deleted for all players of the group."))
        else:
            messages.success(request, _("Penalty deleted."))
        return redirect("matchdays:matchday_detail", pk=penalty.matchday.pk)


# ---------------------------------------------------------------------------
# Payments (partial payments against a player's team debt)
# ---------------------------------------------------------------------------


class PaymentCreateView(LoginRequiredMixin, View):
    """POST: record a (partial) payment — STRICTLY the team's cashier.

    Two-step guard (review M1): a POST without any usable cashier gets a
    friendly message + redirect (crafted/stale submissions never see a bare
    403 and the service-level ValidationError stays unreachable via HTTP);
    only then is the strict cashier rule enforced (admins included).
    """

    def post(self, request):
        fallback = safe_next_url(request, reverse("dashboard:financial_overview"))
        form = PaymentForm(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect(fallback)
        team = form.cleaned_data["team"]
        if cashier_user_for(team) is None:
            messages.error(request, _("No usable cashier is set for this team yet."))
            return redirect(fallback)
        if not user_is_cashier(request.user, team):
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


class PayoutCreateView(LoginRequiredMixin, View):
    """POST: pay out a player's credit — STRICTLY the team's cashier.

    Same two-step guard as ``PaymentCreateView`` (review M1): a POST without
    any usable cashier gets a friendly message + redirect, only then is the
    strict cashier rule enforced (admins included). Unlike a payment, the
    service can still refuse (credit missing / exceeded — e.g. from a stale
    modal); that ValidationError becomes a friendly message + redirect too,
    never a bare 500.
    """

    def post(self, request):
        fallback = safe_next_url(request, reverse("dashboard:financial_overview"))
        form = PayoutForm(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect(fallback)
        team = form.cleaned_data["team"]
        if cashier_user_for(team) is None:
            messages.error(request, _("No usable cashier is set for this team yet."))
            return redirect(fallback)
        if not user_is_cashier(request.user, team):
            raise PermissionDenied
        try:
            record_payout(
                player=form.cleaned_data["player"],
                team=team,
                amount_eur=form.cleaned_data["amount_eur"],
                actor=request.user,
                season=season_for_request(request),
            )
        except ValidationError as exc:
            for error in exc.messages:
                messages.error(request, error)
            return redirect(fallback)
        messages.success(request, _("Payout recorded."))
        return redirect(fallback)


class PaymentDeleteView(LoginRequiredMixin, View):
    """POST: revert a mis-recorded payment — STRICTLY the team's current cashier.

    Only the CURRENT cashier may revert (deliberate tradeoff of decision 7:
    after a cashier change the new cashier fixes old rows, or an admin
    switches the cashier back).
    """

    def post(self, request, pk):
        payment = get_object_or_404(Payment, pk=pk)
        if not user_is_cashier(request.user, payment.team):
            raise PermissionDenied
        delete_payment(payment=payment, actor=request.user)
        messages.success(request, _("Payment reverted."))
        return redirect(safe_next_url(request, reverse("dashboard:financial_overview")))
