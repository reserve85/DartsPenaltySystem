"""In-app notifications (django-notifications-hq) — the ONE place where
in-app notifications are created.

Complements the e-mail NotificationService (``app.notifications.services``):
every new self-registration (Freigabe request) raises an in-app notification
for all admins — visible as a bell badge in the navbar
(``notifications:unread`` list).

Framework: `django-notifications-hq` (app label ``notifications``, model
``notifications.Notification``, signal ``notifications.signals.notify``).

Rules mirror the e-mail service: a notification problem must never break a
business transaction — failures are logged, never raised. The verb is
created in each recipient's preferred language (same rule as e-mails).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import translation
from django.utils.translation import gettext as _

from app.core.permissions import GROUP_ADMIN

if TYPE_CHECKING:  # pragma: no cover — typing only
    from django.contrib.auth.models import AbstractBaseUser

logger = logging.getLogger("app.notifications")


def admin_recipients() -> list[AbstractBaseUser]:
    """Every active Admin-group member and superuser (the bell audience)."""
    user_model = get_user_model()
    return list(
        user_model.objects.filter(is_active=True)
        .filter(Q(groups__name=GROUP_ADMIN) | Q(is_superuser=True))
        .distinct()
        .order_by("email")
    )


def notify_new_approval_request(user) -> None:
    """New Freigabe request -> one in-app notification per admin.

    ``user`` is the pending, self-registered account (actor + target), so the
    notification list can link straight to the review screen.
    """
    from notifications.signals import notify

    recipients = admin_recipients()
    if not recipients:
        logger.info("In-app notification skipped: no active admin recipients.")
        return
    try:
        for recipient in recipients:
            language = getattr(recipient, "preferred_language", None) or settings.LANGUAGE_CODE
            with translation.override(language):
                notify.send(
                    sender=user,
                    recipient=recipient,
                    verb=_("New approval request"),
                    target=user,
                    level="info",
                )
    except Exception:
        logger.exception("In-app notification for admins could not be created.")


def clear_approval_notifications(user) -> None:
    """Mark every in-app notification about ``user`` as read — the admin decided.

    Called after approve/reject so the navbar badge only counts requests that
    still need a decision (for ALL admins, not only the deciding one).
    """
    from notifications.models import Notification

    try:
        content_type = ContentType.objects.get_for_model(user)
        Notification.objects.filter(
            target_content_type=content_type,
            target_object_id=user.pk,
            unread=True,
        ).update(unread=False)
    except Exception:
        logger.exception("In-app notifications for user pk=%s could not be marked read.", user.pk)


def unread_count(user) -> int:
    """Unread in-app notifications for the navbar badge (0 when in doubt)."""
    try:
        return user.notifications.unread().count()
    except Exception:  # pragma: no cover — defensive (e.g. tables not migrated)
        logger.exception(
            "Unread in-app notification count failed for user pk=%s.", getattr(user, "pk", None)
        )
        return 0
