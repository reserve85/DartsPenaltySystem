"""Penalty forms — assignment (participants + active catalog), catalog item, payments."""

from typing import ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from app.core.permissions import user_is_admin
from app.matchdays.models import MatchdayPlayer
from app.penalties.models import (
    Penalty,
    PenaltyCatalogItem,
    PenaltyType,
    TeamCatalogAmount,
    active_for_team,
)
from app.players.models import Player
from app.teams.models import Team


def _format_amount(amount) -> str | None:
    """JSON-safe representation of an (optional) Decimal amount."""
    return None if amount is None else f"{amount:.2f}"


class CatalogItemForm(forms.ModelForm):
    """Create/edit a catalog item — including the per-team fees + active flags.

    The former dedicated "Team fees" page is merged into this form. Dynamic
    fields (one pair per team):

    * ``team_active_<pk>`` — per-team active checkbox (missing in POST = off),
    * ``team_amount_<pk>`` — that team's OWN fee (required, always filled),

    Admins AND captains see and edit EVERY team (same scope, decision). Only
    the CREATE defaults differ: an admin's entry starts with every team
    active, a captain's entry starts with the own team active and all other
    teams INACTIVE (still visible/editable). On create the default amount is
    copied into every fee field automatically (inline JS).
    """

    class Meta:
        model = PenaltyCatalogItem
        fields: ClassVar[list] = ["description", "amount_eur", "type", "affects_both_players"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # No user (system/fixture usage) counts as admin scope.
        self._admin_scope = user is None or user_is_admin(user)
        self._teams = list(self._editable_teams())
        is_create = self.instance.pk is None

        rows = {}
        if self.instance.pk is not None:
            rows = {row.team_id: row for row in self.instance.team_amounts.all()}
        for team in self._teams:
            row = rows.get(team.pk)
            if row is not None:
                default_active = row.active
            elif is_create and not self._admin_scope:
                # Captain creates: only MY team starts active — every other
                # team defaults to INACTIVE (but stays visible/editable).
                default_active = self.user is not None and team.pk == self.user.team_id
            else:
                default_active = True  # no row yet = active (legacy/effective)
            self.fields[f"team_active_{team.pk}"] = forms.BooleanField(
                required=False,
                label=_("Active"),
                initial=default_active,
                widget=forms.CheckboxInput(
                    attrs={"aria-label": f"{_('Active')}: {team.name}"},
                ),
            )
            # Every row must be filled: existing amount, else the default
            # (legacy/no-row) — on create the JS copies the typed default.
            self.fields[f"team_amount_{team.pk}"] = forms.DecimalField(
                max_digits=8,
                decimal_places=2,
                required=True,
                label=_("Fee (EUR)"),
                initial=(
                    row.amount_eur
                    if row is not None and row.amount_eur is not None
                    else self.instance.amount_eur  # None on create -> JS fills it
                ),
                widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            )

        # BoundFields consumed by the template (model fields vs. team table).
        self.core_fields = [
            self[name] for name in ("description", "amount_eur", "type", "affects_both_players")
        ]
        self.team_field_rows = [
            {
                "team": team,
                "active": self[f"team_active_{team.pk}"],
                "amount": self[f"team_amount_{team.pk}"],
            }
            for team in self._teams
        ]

    def _editable_teams(self):
        # Admins AND captains work on every team's fee/active flags (decision);
        # only the CREATE defaults differ (see __init__).
        return Team.objects.all().order_by("name")

    def clean_amount_eur(self):
        amount = self.cleaned_data.get("amount_eur")
        if amount is not None and amount <= 0:
            raise forms.ValidationError(_("The amount must be a positive number."))
        return amount

    def clean_description(self):
        """One catalog entry per description — a duplicate is rejected.

        Compared trimmed and case-insensitively; editing an item may keep its
        OWN description (the instance is excluded from the lookup).
        """
        description = (self.cleaned_data.get("description") or "").strip()
        if description:
            duplicates = PenaltyCatalogItem.objects.filter(description__iexact=description)
            if self.instance.pk is not None:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise forms.ValidationError(
                    _("A penalty with this description already exists."),
                    code="unique",
                )
        return description

    def clean(self):
        cleaned = super().clean()
        for team in self._teams:
            key = f"team_amount_{team.pk}"
            amount = cleaned.get(key)
            if amount is not None and amount <= 0:
                self.add_error(key, _("The amount must be a positive number."))
        return cleaned

    def save(self, commit=True):
        """Persist the item and its per-team rows (former fee-page logic)."""
        item = super().save(commit=commit)
        if not commit:
            return item
        # One row per team — admins and captains both edit ALL teams; the
        # create-time checkbox defaults decide active/inactive per team.
        for team in self._teams:
            TeamCatalogAmount.objects.update_or_create(
                team=team,
                catalog_item=item,
                defaults={
                    "active": bool(self.cleaned_data.get(f"team_active_{team.pk}", False)),
                    "amount_eur": self.cleaned_data.get(f"team_amount_{team.pk}"),
                    "updated_by": self.user,
                },
            )
        return item

    # -- audit metadata ---------------------------------------------------
    @staticmethod
    def snapshot_state(item) -> dict:
        """Pre-save state of the item incl. its per-team rows (audit diff base)."""
        return {
            "description": item.description,
            "amount_eur": item.amount_eur,
            "type": item.type,
            "affects_both_players": item.affects_both_players,
            "teams": {
                row.team_id: {"active": row.active, "amount": row.amount_eur}
                for row in item.team_amounts.all()
            },
        }

    def created_metadata(self) -> dict:
        item = self.instance
        rows = list(item.team_amounts.all())
        row_by_team = {row.team_id: row for row in rows}
        active_teams = sorted(
            pk
            for pk in Team.objects.values_list("pk", flat=True)
            if pk not in row_by_team or row_by_team[pk].active
        )
        return {
            "description": item.description,
            "amount_eur": _format_amount(item.amount_eur),
            "type": item.type,
            "affects_both_players": item.affects_both_players,
            "active_teams": active_teams,
            "team_amounts": {str(row.team_id): _format_amount(row.amount_eur) for row in rows},
        }

    def updated_metadata(self, *, before: dict) -> dict:
        """Created snapshot plus a ``changes`` diff (core fields + per-team rows)."""
        meta = self.created_metadata()
        item = self.instance
        changes: dict = {}
        if before["description"] != item.description:
            changes["description"] = {"from": before["description"], "to": item.description}
        if before["amount_eur"] != item.amount_eur:
            changes["amount_eur"] = {
                "from": _format_amount(before["amount_eur"]),
                "to": _format_amount(item.amount_eur),
            }
        if before["type"] != item.type:
            changes["type"] = {"from": before["type"], "to": item.type}
        if before["affects_both_players"] != item.affects_both_players:
            changes["affects_both_players"] = {
                "from": before["affects_both_players"],
                "to": item.affects_both_players,
            }
        after_rows = {row.team_id: row for row in item.team_amounts.all()}
        for team_pk in sorted(set(before["teams"]) | set(after_rows)):
            old = before["teams"].get(team_pk)
            new = after_rows.get(team_pk)
            old_active = old["active"] if old is not None else True
            new_active = new.active if new is not None else True
            if old_active != new_active:
                changes[f"team_{team_pk}_active"] = {"from": old_active, "to": new_active}
            old_amount = old["amount"] if old is not None else None
            new_amount = new.amount_eur if new is not None else None
            if old_amount != new_amount:
                changes[f"team_{team_pk}_amount"] = {
                    "from": _format_amount(old_amount),
                    "to": _format_amount(new_amount),
                }
        meta["changes"] = changes
        return meta


class PenaltyAssignForm(forms.Form):
    """Assign a penalty on a matchday.

    NORMAL items always take the effective (team-specific) catalog amount —
    no amount is asked for. MANUAL items ask for an individual amount and a
    required comment (shown/hidden client-side via ``manual_ids``; enforced
    server-side below). For group items the selected player is the TRIGGER
    player (e.g. the 180 thrower); amount always comes from the catalog item.

    ``description`` is the COMMENT/REASON behind the penalty type:

    * MANUAL — required (it is the whole reason of the charge),
    * NORMAL — optional (extra info, e.g. the points actually scored),
    * group  — optional (appended behind the automatically stored causers).

    Whatever is typed is LISTED behind the penalty type in brackets, e.g.
    ``Late arrival (9 Punkte)`` — see ``Penalty.display_description()``.
    """

    catalog_item = forms.ModelChoiceField(
        queryset=PenaltyCatalogItem.objects.all().order_by("type", "description"),
        label=_("Penalty type"),
        empty_label=None,
    )
    player = forms.ModelChoiceField(
        queryset=Player.objects.none(),
        label=_("Player"),
        help_text=_("For group penalties this player is the trigger (e.g. the 180 thrower)."),
    )
    # Optional second player of a DOUBLES penalty — only offered when the
    # selected catalog item is flagged ``affects_both_players`` (JS toggles the
    # field, ``clean()`` drops it otherwise). Empty = NO doubles partner.
    double_partner = forms.ModelChoiceField(
        queryset=Player.objects.none(),
        required=False,
        label=_("Doubles partner"),
        empty_label=_("No doubles partner"),
        help_text=_(
            "Optional — only offered when the catalog item applies to both "
            "doubles players (e.g. a low dart achieved together)."
        ),
    )
    amount_eur = forms.DecimalField(
        max_digits=8,
        decimal_places=2,
        required=False,
        label=_("Amount (EUR)"),
        help_text=_("Only for manual penalties — entered individually each time."),
    )
    # The comment/reason: REQUIRED for MANUAL items, optional for NORMAL and
    # group items (the form-level ``required`` flag is toggled client-side by
    # the template JS, ``clean()`` enforces it server-side).
    description = forms.CharField(
        required=False,
        max_length=255,
        label=_("Comment / reason"),
        help_text=_(
            "Manual penalty: required (comment/reason) — normal and group "
            "penalties: optional. Always listed as: penalty type (comment)."
        ),
    )

    def __init__(self, *args, matchday=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.matchday = matchday
        if matchday is not None:
            participant_ids = matchday.participants.values_list("player_id", flat=True)
            participant_qs = Player.objects.filter(pk__in=participant_ids).order_by("name")
            self.fields["player"].queryset = participant_qs
            # Same roster for the optional doubles partner (independent clone).
            self.fields["double_partner"].queryset = participant_qs.all()
            if matchday.team_id is not None:
                # Only items active for THIS team's matchday (per-team state —
                # a missing row means active, an explicit row decides alone).
                self.fields["catalog_item"].queryset = (
                    PenaltyCatalogItem.objects.filter(active_for_team(matchday.team))
                    .order_by("type", "description")
                    .distinct()
                )
                # Option labels must show THIS team's own fee: str(item) (the
                # default ModelChoiceField label) renders the DEFAULT catalog
                # amount — the seed value that usually still equals the first
                # team's fee. Teams with their own override (e.g. 3.00 € while
                # the default reads 5.00 €) therefore saw the WRONG amount in
                # the dropdown, while the assignment itself always charged the
                # right one (live display bug 10/2026). The label mirrors
                # PenaltyCatalogItem.__str__ verbatim, only the amount is the
                # effective per-team one.
                team = matchday.team
                self.fields["catalog_item"].label_from_instance = lambda item: (
                    f"{item.description} ({item.amount_for_team(team)} €)"
                )
        # pks of the MANUAL items — the template toggles the amount field.
        self.manual_ids = list(
            self.fields["catalog_item"]
            .queryset.filter(type=PenaltyType.MANUAL)
            .values_list("pk", flat=True)
        )
        # pks of the items flagged "applies to both doubles players" — the
        # template only offers the doubles-partner select for those.
        self.both_ids = list(
            self.fields["catalog_item"]
            .queryset.filter(affects_both_players=True)
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
        player = cleaned.get("player")
        if player is None:
            self.add_error("player", _("Select a player."))
            return cleaned

        # Doubles partner: only meaningful (and only offered) for catalog items
        # flagged ``affects_both_players`` — a stale tab / disabled select may
        # still post a value, which is silently dropped instead of failing.
        partner = cleaned.get("double_partner")
        if partner is not None:
            if catalog_item is None or not catalog_item.affects_both_players:
                cleaned["double_partner"] = None
            elif partner.pk == player.pk:
                self.add_error("double_partner", _("The doubles partner must be another player."))
            elif (
                catalog_item.type == PenaltyType.PER_ALL_OTHER_MATCHDAY_PLAYERS
                and self.matchday is not None
            ):
                # Trigger + partner are BOTH excluded — somebody must remain.
                remaining = (
                    MatchdayPlayer.objects.filter(matchday=self.matchday)
                    .exclude(player=player)
                    .exclude(player=partner)
                    .count()
                )
                if remaining < 1:
                    self.add_error(
                        "double_partner",
                        _(
                            "A group penalty with a doubles partner needs at least "
                            "three participants."
                        ),
                    )
        return cleaned


class PaymentForm(forms.Form):
    """A (partial) payment: which player pays how much into which team's pot.

    Cross-team pairs are ALLOWED by design (see ``test_payment_is_team_scoped``):
    a player may settle a debt owed to another team's pot — the global player
    balance nets penalties and payments across teams. The permission check —
    STRICTLY the team's current cashier, admins included (decision 7) — happens
    in the view (``PaymentCreateView``).
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


class PayoutForm(forms.Form):
    """Pay out a credit — the negative twin of ``PaymentForm``.

    The amount is entered POSITIVE (how much leaves the club pot);
    ``record_payout`` stores it as a negative ``Payment`` row. The cap (the
    player's credit for that team + season) is enforced server-side in the
    service — a stale modal may post more than what is available. Just like
    a payment, the permission check (STRICTLY the team's cashier) happens in
    the view.
    """

    team = forms.ModelChoiceField(queryset=Team.objects.all(), label=_("Team"))
    player = forms.ModelChoiceField(queryset=Player.objects.all(), label=_("Player"))
    amount_eur = forms.DecimalField(
        max_digits=8,
        decimal_places=2,
        label=_("Amount (EUR)"),
        help_text=_("Payout of the credit — any amount up to the available credit."),
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
