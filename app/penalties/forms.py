"""Penalty forms — assignment (participants + active catalog), catalog item, payments."""

from typing import ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from app.penalties.models import Penalty, PenaltyCatalogItem, PenaltyType
from app.players.models import Player
from app.teams.models import Team


class CatalogItemForm(forms.ModelForm):
    class Meta:
        model = PenaltyCatalogItem
        fields: ClassVar[list] = ["description", "amount_eur", "type", "active"]

    def clean_amount_eur(self):
        amount = self.cleaned_data.get("amount_eur")
        if amount is not None and amount <= 0:
            raise forms.ValidationError(_("The amount must be a positive number."))
        return amount


class PenaltyAssignForm(forms.Form):
    """Assign a penalty on a matchday.

    NORMAL items always take the effective (team-specific) catalog amount —
    no amount is asked for. MANUAL items ask for an individual amount and a
    required comment (shown/hidden client-side via ``manual_ids``; enforced
    server-side below). For group items the selected player is the TRIGGER
    player (e.g. the 180 thrower); amount and description always come from
    the catalog item.
    """

    catalog_item = forms.ModelChoiceField(
        queryset=PenaltyCatalogItem.objects.filter(active=True).order_by("type", "description"),
        label=_("Penalty type"),
        empty_label=None,
    )
    player = forms.ModelChoiceField(
        queryset=Player.objects.none(),
        label=_("Player"),
        help_text=_("For group penalties this player is the trigger (e.g. the 180 thrower)."),
    )
    amount_eur = forms.DecimalField(
        max_digits=8,
        decimal_places=2,
        required=False,
        label=_("Amount (EUR)"),
        help_text=_("Only for manual penalties — entered individually each time."),
    )
    description = forms.CharField(
        required=False,
        max_length=255,
        label=_("Description"),
        help_text=_("Only for normal penalties — leave empty to use the catalog description."),
    )

    def __init__(self, *args, matchday=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.matchday = matchday
        if matchday is not None:
            participant_ids = matchday.participants.values_list("player_id", flat=True)
            self.fields["player"].queryset = Player.objects.filter(pk__in=participant_ids).order_by(
                "name"
            )
        # pks of the MANUAL items — the template toggles the amount field.
        self.manual_ids = list(
            self.fields["catalog_item"]
            .queryset.filter(type=PenaltyType.MANUAL)
            .values_list("pk", flat=True)
        )

    def clean(self):
        cleaned = super().clean()
        catalog_item = cleaned.get("catalog_item")
        amount = cleaned.get("amount_eur")
        description = cleaned.get("description")
        if catalog_item is not None and catalog_item.type == PenaltyType.MANUAL:
            if amount is None:
                self.add_error("amount_eur", _("A manual penalty needs an individual amount."))
            elif amount <= 0:
                self.add_error("amount_eur", _("The amount must be a positive number."))
            if not (description or "").strip():
                self.add_error("description", _("A manual penalty needs a comment."))
        if (
            catalog_item is not None
            and catalog_item.type == PenaltyType.PER_ALL_OTHER_MATCHDAY_PLAYERS
            and self.matchday is not None
            and self.matchday.participants.count() < 2
        ):
            # Without a second participant there is nobody to charge — the
            # service would raise, so catch it here as a friendly form error.
            self.add_error("catalog_item", _("A group penalty needs at least two participants."))
        if cleaned.get("player") is None:
            self.add_error("player", _("Select a player."))
        return cleaned


class PaymentForm(forms.Form):
    """A (partial) payment: which player pays how much into which team's pot.

    Cross-team pairs are ALLOWED by design (see ``test_payment_is_team_scoped``):
    a player may settle a debt owed to another team's pot — the global player
    balance nets penalties and payments across teams. The permission check
    (Admin / Captain of THAT team) happens in the view.
    """

    team = forms.ModelChoiceField(queryset=Team.objects.all(), label=_("Team"))
    player = forms.ModelChoiceField(queryset=Player.objects.all(), label=_("Player"))
    amount_eur = forms.DecimalField(
        max_digits=8,
        decimal_places=2,
        label=_("Amount (EUR)"),
        help_text=_("Partial payment — any positive amount up to the full debt."),
    )

    def clean_amount_eur(self):
        amount = self.cleaned_data.get("amount_eur")
        if amount is not None and amount <= 0:
            raise forms.ValidationError(_("The amount must be a positive number."))
        return amount


class PenaltyEditForm(forms.ModelForm):
    class Meta:
        model = Penalty
        fields: ClassVar[list] = ["description_snapshot", "amount_eur"]
        labels: ClassVar[dict] = {
            "description_snapshot": _("Description"),
            "amount_eur": _("Amount (EUR)"),
        }

    def clean_amount_eur(self):
        amount = self.cleaned_data.get("amount_eur")
        if amount is not None and amount <= 0:
            raise forms.ValidationError(_("The amount must be a positive number."))
        return amount
