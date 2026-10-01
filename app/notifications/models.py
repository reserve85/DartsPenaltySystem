"""Outbox model — the ONE place where pending e-mails are stored.

Every penalty notification is queued here first and turned into at most ONE
e-mail per user by ``app.notifications.services.flush_outbox()``:

* ``penalty_notify_mode="immediate"`` -> ``send_after`` = queue time + the
  coalescing window (``NOTIFICATION_COALESCE_SECONDS``, default 90 s) so a
  batch of entries made in quick succession arrives as ONE message,
* ``penalty_notify_mode="daily"``     -> ``send_after`` = the next occurrence
  of the user's ``penalty_notify_time`` (default 08:00).

``sent_at`` doubles as the claim marker: the flusher sets it in ONE atomic
UPDATE before sending (``WHERE sent_at IS NULL``), which keeps the in-process
scheduler thread and ``manage.py flush_notification_outbox`` from mailing the
same rows twice. A failed delivery unclaims the rows again (up to
``MAX_SEND_ATTEMPTS``), so a dead SMTP never silently eats a notification.

The app label is ``app_notifications`` (the bare ``notifications`` label
belongs to django-notifications-hq).
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class NotificationOutbox(models.Model):
    """One queued, not-yet-delivered notification batch for ONE user."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_outbox",
        verbose_name=_("user"),
    )
    email = models.EmailField(
        _("e-mail address"),
        help_text=_("Snapshot at queue time — the audit trail keeps the original target."),
    )
    language = models.CharField(
        _("language"),
        max_length=8,
        blank=True,
        help_text=_("Recipient language at queue time (rendering must not depend on a request)."),
    )
    kind = models.CharField(
        _("kind"),
        max_length=32,
        default="penalty_created",
        db_index=True,
        help_text=_("Template family of the queued message."),
    )
    penalties = models.ManyToManyField(
        "penalties.Penalty",
        blank=True,
        related_name="outbox_entries",
        verbose_name=_("penalties"),
        help_text=_("Soft-deleted rows are dropped when the batch is flushed."),
    )
    queued_at = models.DateTimeField(_("queued at"), auto_now_add=True, db_index=True)
    send_after = models.DateTimeField(
        _("send after"),
        db_index=True,
        help_text=_("Coalescing deadline (quick mode) or the next digest time."),
    )
    sent_at = models.DateTimeField(
        _("sent at"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("NULL = pending; set BEFORE delivery as the flush claim."),
    )
    attempts = models.PositiveSmallIntegerField(_("attempts"), default=0)
    last_error = models.TextField(_("last error"), blank=True)

    def __str__(self) -> str:
        state = "sent" if self.sent_at is not None else f"due {self.send_after:%Y-%m-%d %H:%M}"
        return f"outbox {self.kind} for {self.email} ({state})"

    class Meta:
        ordering: ClassVar[list] = ["send_after", "id"]
        indexes: ClassVar[list] = [
            models.Index(fields=["sent_at", "send_after"]),
        ]
        verbose_name = _("queued notification")
        verbose_name_plural = _("queued notifications")
