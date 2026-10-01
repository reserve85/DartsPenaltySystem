from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Team(models.Model):
    name = models.CharField(max_length=120, unique=True, verbose_name=_("name"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    def deletable(self) -> bool:
        """B2: a team may be hard-deleted only when it holds no history.

        Players, matchdays, linked captain accounts and cashier history all
        block deletion.
        """
        from django.contrib.auth import get_user_model

        User = get_user_model()
        return not (
            self.players.exists()
            or self.matchdays.exists()
            or User.objects.filter(team=self).exists()
            or self.cashiers.exists()
        )

    def __str__(self):
        return self.name

    class Meta:
        ordering: ClassVar[list] = ["name"]
        verbose_name = _("team")
        verbose_name_plural = _("teams")


class TeamCashier(models.Model):
    """One cashier assignment period for a team ("Kasse").

    History is preserved: ``valid_to=NULL`` marks the currently open
    assignment, closing a row and opening the next one happens in
    ``app.teams.services.set_cashier``. ``user`` is ``SET_NULL`` plus a
    ``user_label`` snapshot so the Kasse history survives account deletion;
    the partial unique constraint guarantees at most ONE open row per team.
    """

    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="cashiers", verbose_name=_("team")
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cashier_assignments",
        verbose_name=_("cashier"),
    )
    user_label = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("cashier (snapshot)"),
        help_text=_("Display name at assignment time — survives account deletion."),
    )
    valid_from = models.DateTimeField(
        default=timezone.now, db_index=True, verbose_name=_("valid from")
    )
    valid_to = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name=_("valid to")
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_open(self) -> bool:
        return self.valid_to is None

    def __str__(self):
        return f"{self.user_label} @ {self.team} ({self.valid_from:%Y-%m-%d})"

    class Meta:
        ordering: ClassVar[list] = ["team__name", "-valid_from"]
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=["team"],
                condition=Q(valid_to__isnull=True),
                name="teamcashier_one_open_per_team",
            )
        ]
        indexes: ClassVar[list] = [
            models.Index(fields=["team", "valid_to"]),
        ]
        verbose_name = _("cashier assignment")
        verbose_name_plural = _("cashier assignments")
