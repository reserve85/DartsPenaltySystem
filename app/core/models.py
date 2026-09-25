"""Audit log model + action enumeration.

Every user-initiated state change writes one ``AuditLog`` entry. Penalties
(group penalties included) write one entry per affected row so historical
aggregates stay fully auditable.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class AuditAction(models.TextChoices):
    LOGIN = "login", _("Login")
    LOGOUT = "logout", _("Logout")
    TEAM_CREATED = "team_created", _("Team created")
    TEAM_UPDATED = "team_updated", _("Team updated")
    TEAM_DELETED = "team_deleted", _("Team deleted")  # empty-team hard delete (B2)
    PLAYER_CREATED = "player_created", _("Player created")
    PLAYER_UPDATED = "player_updated", _("Player updated")
    PLAYER_DEACTIVATED = "player_deactivated", _("Player deactivated")
    MATCHDAY_CREATED = "matchday_created", _("Matchday created")
    MATCHDAY_UPDATED = "matchday_updated", _("Matchday updated")
    MATCHDAY_DELETED = "matchday_deleted", _("Matchday deleted")
    SEASON_CREATED = "season_created", _("Season created")
    SEASON_UPDATED = "season_updated", _("Season updated")
    SEASON_DELETED = "season_deleted", _("Season deleted")
    PENALTY_ASSIGNED = "penalty_assigned", _("Penalty assigned")
    PENALTY_EDITED = "penalty_edited", _("Penalty edited")
    PENALTY_DELETED = "penalty_deleted", _("Penalty deleted")
    # Legacy (kept for historical rows): per-row payments before the Payment model.
    PENALTY_PAID = "penalty_paid", _("Penalty paid")
    PENALTY_PAYMENT_REVERTED = "penalty_payment_reverted", _("Penalty payment reverted")
    PAYMENT_RECORDED = "payment_recorded", _("Payment recorded")
    PAYMENT_DELETED = "payment_deleted", _("Payment reverted")
    USER_CREATED = "user_created", _("User created")
    USER_UPDATED = "user_updated", _("User updated")
    USER_DEACTIVATED = "user_deactivated", _("User deactivated")
    USER_REGISTERED = "user_registered", _("User self-registered")
    USER_APPROVED = "user_approved", _("User approved")
    USER_REJECTED = "user_rejected", _("User rejected")


class AuditLog(models.Model):
    action = models.CharField(
        max_length=64, choices=AuditAction.choices, db_index=True, verbose_name=_("action")
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("user"),
    )
    target_type = models.CharField(max_length=120, blank=True, verbose_name=_("target type"))
    target_id = models.PositiveBigIntegerField(null=True, blank=True, verbose_name=_("target id"))
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_("metadata"))
    created_at = models.DateTimeField(
        auto_now_add=True, db_index=True, verbose_name=_("created at")
    )

    def __str__(self):
        actor = self.user.email if self.user else "system"
        return f"{self.action} | {self.target_type}#{self.target_id} | {actor} | {self.created_at:%d.%m.%Y %H:%M}"

    class Meta:
        ordering: ClassVar[list] = ["-created_at"]
        verbose_name = _("audit log entry")
        verbose_name_plural = _("audit log entries")
