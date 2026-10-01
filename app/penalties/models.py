from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


def active_for_team(team) -> Q:
    """Queryset condition for catalog items that are ACTIVE for ``team``.

    Effective state is purely per-team: a missing ``TeamCatalogAmount`` row
    means "active" (default), only an explicit row with ``active=False``
    deactivates the item for exactly that team.

    NOTE: this must NOT be written as ``~Q(team_amounts__team=team,
    team_amounts__active=False)`` — Django splits a negated multi-valued
    span into one EXISTS subquery PER leaf and negates their CONJUNCTION:
    ``NOT (any inactive row exists AND any row for the team exists)``. An
    item carrying rows for every team (the pattern the catalog form writes)
    was then dropped for its OWN active team because some OTHER team's row
    is inactive — only row-less items (e.g. "Manual") stayed selectable.
    The ``pk__in`` subquery keeps the semantics exact.
    """
    if team is None:
        return Q()  # no team -> no restriction (everything active)
    inactive_ids = TeamCatalogAmount.objects.filter(team=team, active=False).values(
        "catalog_item_id"
    )
    return ~Q(pk__in=inactive_ids)


class PenaltyType(models.TextChoices):
    NORMAL = "NORMAL", _("Normal")
    PER_ALL_OTHER_MATCHDAY_PLAYERS = (
        "PER_ALL_OTHER_MATCHDAY_PLAYERS",
        _("Per all other matchday players"),
    )
    # Manual entry: individual amount + required comment per assignment.
    MANUAL = "MANUAL", _("Manual")


class PenaltyCatalogItem(models.Model):
    description = models.CharField(max_length=255, verbose_name=_("description"))
    amount_eur = models.DecimalField(max_digits=8, decimal_places=2, verbose_name=_("amount (EUR)"))
    type = models.CharField(
        max_length=32,
        choices=PenaltyType.choices,
        default=PenaltyType.NORMAL,
        verbose_name=_("type"),
    )
    # Doubles (Doppelspiel): when checked, assigning this penalty OFFERS an
    # optional second player (the doubles partner) — see PenaltyAssignForm.
    affects_both_players = models.BooleanField(
        default=False,
        verbose_name=_("Affects both doubles players?"),
        help_text=_(
            "Offer the doubles partner when this penalty is assigned — e.g. a "
            "low dart both players achieved together."
        ),
    )
    # NOTE: there is NO global "active" switch anymore — activation exists per
    # team only (``TeamCatalogAmount.active``; a missing row means active).
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    def amount_for_team(self, team):
        """The team's own fee; the default catalog amount if no row exists yet."""
        if team is not None:
            override = self.team_amounts.filter(team=team).first()
            # amount_eur is required — the None guard only heals legacy rows
            # written before the field became mandatory.
            if override is not None and override.amount_eur is not None:
                return override.amount_eur
        return self.amount_eur

    def is_active_for_team(self, team) -> bool:
        """Per-team activation: no row for the team means active (default)."""
        if team is None:
            return True
        row = self.team_amounts.filter(team=team).first()
        return row is None or row.active

    def __str__(self):
        return f"{self.description} ({self.amount_eur} €)"

    class Meta:
        ordering: ClassVar[list] = ["type", "description"]
        constraints: ClassVar[list] = [
            models.CheckConstraint(
                condition=models.Q(amount_eur__gt=0), name="catalog_item_amount_eur_positive"
            )
        ]
        verbose_name = _("penalty catalog item")
        verbose_name_plural = _("penalty catalog items")


class TeamCatalogAmount(models.Model):
    """Per-team catalog configuration row for one catalog item.

    Full per-team state lives here:

    * ``active=False`` deactivates the item for exactly that team,
    * ``amount_eur`` is that team's OWN fee — always filled (the default
      catalog amount is only the seed value that is copied here on creation;
      e.g. "3 or less" costs 3 € in the 1st team but only 1 € in the 2nd),
    * a missing row means "active with the default amount" (legacy safety —
      a NEW team and a NEW catalog entry both get explicit rows).
    """

    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.CASCADE,
        related_name="catalog_amounts",
        verbose_name=_("team"),
    )
    catalog_item = models.ForeignKey(
        PenaltyCatalogItem,
        on_delete=models.CASCADE,
        related_name="team_amounts",
        verbose_name=_("catalog item"),
    )
    active = models.BooleanField(default=True, verbose_name=_("active"))
    amount_eur = models.DecimalField(max_digits=8, decimal_places=2, verbose_name=_("amount (EUR)"))
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("updated by"),
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("updated at"))

    def __str__(self):
        return f"{self.catalog_item.description} @ {self.team.name}: {self.amount_eur} €"

    class Meta:
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=["team", "catalog_item"], name="unique_team_catalog_amount"
            ),
            models.CheckConstraint(
                condition=models.Q(amount_eur__gt=0), name="team_catalog_amount_eur_positive"
            ),
        ]
        verbose_name = _("team catalog amount")
        verbose_name_plural = _("team catalog amounts")


class Payment(models.Model):
    """A (partial) payment from a player towards ONE team's club pot — or a PAYOUT.

    Balances are ``Σ penalties − Σ payments`` per team (M3): a player owing
    25.00 € may pay e.g. 20.00 € — recorded by an Admin or the Captain of
    that team — and still owes the remaining 5.00 €. There is no per-row
    "paid" state on ``Penalty`` anymore.

    A NEGATIVE ``amount_eur`` is a **payout** (Auszahlung): real money leaves
    the club pot and reduces an existing credit (negative balance). Keeping
    the sign ON the row means every aggregate keeps working unchanged — a
    payout simply lowers Σ payments. Payments and payouts are recorded by
    their own service functions (``record_payment`` / ``record_payout``);
    ``amount_eur`` must never be 0.
    """

    player = models.ForeignKey(
        "players.Player",
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name=_("player"),
    )
    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name=_("team"),
    )
    season = models.ForeignKey(
        "matchdays.Season",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payments",
        verbose_name=_("season"),
        help_text=_("Payments only reduce debt of the same season."),
    )
    amount_eur = models.DecimalField(max_digits=8, decimal_places=2, verbose_name=_("amount (EUR)"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("recorded by"),
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="received_payments",
        verbose_name=_("received by"),
        help_text=_(
            "The cashier who physically received or paid out the money (snapshot at record time)."
        ),
    )
    created_at = models.DateTimeField(
        auto_now_add=True, db_index=True, verbose_name=_("created at")
    )

    def __str__(self):
        return f"{self.player.name}: {self.amount_eur} € @ {self.team.name}"

    class Meta:
        ordering: ClassVar[list] = ["-created_at"]
        constraints: ClassVar[list] = [
            # > 0 = payment into the pot, < 0 = payout of credit, never 0.
            models.CheckConstraint(
                condition=models.Q(amount_eur__gt=0) | models.Q(amount_eur__lt=0),
                name="payment_amount_eur_not_zero",
            )
        ]
        verbose_name = _("payment")
        verbose_name_plural = _("payments")


class PenaltyManager(models.Manager):
    def get_queryset(self):
        """Default manager hides soft-deleted rows everywhere."""
        return super().get_queryset().filter(deleted_at__isnull=True)

    def all_objects(self):
        """Include soft-deleted rows (admin / audit / delete guards)."""
        return super().get_queryset()


class Penalty(models.Model):
    matchday = models.ForeignKey(
        "matchdays.Matchday",
        on_delete=models.CASCADE,
        related_name="penalties",
        verbose_name=_("matchday"),
    )
    player = models.ForeignKey(
        "players.Player",
        on_delete=models.CASCADE,
        related_name="penalties",
        verbose_name=_("player"),
    )
    catalog_item = models.ForeignKey(
        PenaltyCatalogItem,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("catalog item"),
    )
    description_snapshot = models.CharField(
        max_length=255, verbose_name=_("description (snapshot)")
    )
    # B1: amount_eur > 0 means the player OWES this much to the club pot.
    amount_eur = models.DecimalField(max_digits=8, decimal_places=2, verbose_name=_("amount (EUR)"))
    group_id = models.CharField(
        max_length=36,
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("group id"),
        help_text=_("Shared by all rows of one group penalty."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    deleted_at = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name=_("deleted at")
    )
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("deleted by"),
    )

    objects = PenaltyManager()

    @classmethod
    def all_objects(cls):
        """Everything incl. soft-deleted rows (admin / audit / delete guards)."""
        return cls.objects.all_objects()

    def __str__(self):
        return f"{self.description_snapshot} — {self.player.name}"

    class Meta:
        ordering: ClassVar[list] = ["-created_at"]
        constraints: ClassVar[list] = [
            models.CheckConstraint(
                condition=models.Q(amount_eur__gt=0), name="penalty_amount_eur_positive"
            )
        ]
        verbose_name = _("penalty")
        verbose_name_plural = _("penalties")
