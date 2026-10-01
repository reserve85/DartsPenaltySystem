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


def notify_invitation_completed(user) -> None:
    """An invited user completed registration -> one in-app notification per admin.

    Mirrors ``notify_new_approval_request`` (actor + target = the user, so the
    list links straight to the account; per-recipient language override;
    failures logged never raised) — but level ``success`` and sent ONLY from
    the acceptance path: allauth's ``user_signed_up`` never fires there, so
    there is no "new approval request" bell and no "waiting for approval" mail
    for invitations.
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
                    verb=_("Invited user completed registration"),
                    target=user,
                    level="success",
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


# ---------------------------------------------------------------------------
# Business events — additive to e-mail, never switchable off (decision 5)
# ---------------------------------------------------------------------------
def notify_user(*, recipient, verb, sender=None, target=None, level="info") -> None:
    """ONE in-app row for ``recipient`` — the single write path for events.

    ``verb`` may be a plain string OR a zero-arg callable that builds the
    translated string; callables are invoked INSIDE the recipient's language
    override, so the row is rendered in the recipient's preferred language
    even when a different account triggered the event.

    ``sender`` must be a model instance (the package resolves the actor as a
    GenericForeignKey and does not accept ``None``) — when no actor is at
    hand, the recipient becomes their own actor so the row still renders.

    Project rule: a notification problem never breaks a business transaction —
    failures are logged, never raised; inactive accounts are skipped.
    """
    from notifications.signals import notify

    if recipient is None or not getattr(recipient, "is_active", False):
        return
    try:
        language = getattr(recipient, "preferred_language", None) or settings.LANGUAGE_CODE
        with translation.override(language):
            text = verb() if callable(verb) else verb
            notify.send(
                sender=sender if sender is not None else recipient,
                recipient=recipient,
                verb=str(text),
                target=target,
                level=level,
            )
    except Exception:
        logger.exception(
            "In-app notification for recipient pk=%s could not be created.",
            getattr(recipient, "pk", None),
        )


def _inapp_recipient_for(player):
    """Active user account of ``player`` — the IN-App rule (no e-mail needed).

    Deliberately NOT ``eligible_user_for`` (the MAIL rule): an in-app row
    needs no e-mail address, only an existing, active account. Players
    without an account are skipped (logged).
    """
    if player is None:
        return None
    user = player.user  # property: None when no account is linked
    if user is None:
        logger.info(
            "In-app notification skipped: player '%s' has no linked user account.", player
        )
        return None
    if not user.is_active:
        logger.info("In-app notification skipped: user %s is inactive.", user.email or user.pk)
        return None
    return user


def notify_penalties_assigned(penalties) -> None:
    """One warning row per (active user, penalty) for NEW penalties."""
    for penalty in penalties or []:
        recipient = _inapp_recipient_for(penalty.player)
        if recipient is None:
            continue
        amount = f"{penalty.amount_eur:.2f}"
        notify_user(
            recipient=recipient,
            # default-arg binding: each row must capture ITS amount (B023).
            verb=lambda amount=amount: _("New penalty: {amount} €").format(amount=amount),
            sender=penalty.created_by,
            target=penalty,
            level="warning",
        )


def notify_payment_recorded(payment) -> None:
    """Payer AND cashier learn about a recorded payment (always, in-app).

    Deduplicated when both are the same account; the payment row (with
    ``received_by``) resolves through ``_base_manager`` on the target side.
    """
    payer = _inapp_recipient_for(payment.player)
    cashier = payment.received_by
    amount = f"{payment.amount_eur:.2f}"
    player_name = payment.player.name
    sender = payment.created_by
    seen: set = set()
    if payer is not None:
        seen.add(payer.pk)
        notify_user(
            recipient=payer,
            verb=lambda: _("Your payment has been recorded: {amount} €").format(amount=amount),
            sender=sender,
            target=payment,
            level="success",
        )
    if cashier is not None and cashier.is_active and cashier.pk not in seen:
        notify_user(
            recipient=cashier,
            verb=lambda: _("Payment from {player}: {amount} €").format(
                player=player_name, amount=amount
            ),
            sender=sender,
            target=payment,
            level="success",
        )


def notify_approval_decided(user, *, approved: bool) -> None:
    """One row FOR the affected user after an admin's approve/reject decision.

    Called AFTER ``clear_approval_notifications`` — first the admins' request
    rows are marked read, then the decision lands in the affected user's
    mailbox (order matters, accounts/views).
    """
    notify_user(
        recipient=user,
        verb=(
            (lambda: _("Your account has been approved"))
            if approved
            else (lambda: _("Your account has been rejected"))
        ),
        sender=user,
        target=user,
        level="success" if approved else "warning",
    )


def notify_payment_reverted(*, payer, cashier_user, player, amount, team) -> None:
    """In-app trace of a REVERTED payment (decision 10 — in-app only, no e-mail).

    Called with the SNAPSHOT captured BEFORE the hard delete of the Payment:
    the row is gone afterwards, so there is NO target (a GenericForeignKey
    would resolve to None). Deduplicated when payer == cashier; inactive
    accounts are skipped by ``notify_user``.
    """
    amount_text = f"{amount:.2f}"
    player_name = player.name
    seen: set = set()
    if payer is not None:
        seen.add(payer.pk)
        notify_user(
            recipient=payer,
            verb=lambda: _("Your payment was reverted: {amount} €").format(
                amount=amount_text
            ),
            level="warning",
        )
    if cashier_user is not None and cashier_user.pk not in seen:
        notify_user(
            recipient=cashier_user,
            verb=lambda: _("Reverted payment from {player}: {amount} €").format(
                player=player_name, amount=amount_text
            ),
            level="warning",
        )


def notify_penalties_changed(penalties, *, action) -> None:
    """One row per (active user, penalty) for EDITED / DELETED penalty rows.

    ``action`` is ``"edited"`` or ``"deleted"``; every affected row of a group
    produces its own row (carrying the NEW amount after an edit).
    ``target=penalty`` — a soft-deleted row still resolves through
    ``_base_manager``. In-app only: no new e-mail type is added (decision 10).
    """
    if action == "edited":

        def make_verb(amount_text):
            return lambda: _("Penalty changed: {amount} €").format(amount=amount_text)

    elif action == "deleted":

        def make_verb(amount_text):
            return lambda: _("Penalty deleted: {amount} €").format(amount=amount_text)

    else:
        # Programmer error, not a notification problem — log, never raise
        # (this helper runs after the business transaction committed).
        logger.error("notify_penalties_changed called with unknown action %r.", action)
        return

    for penalty in penalties or []:
        recipient = _inapp_recipient_for(penalty.player)
        if recipient is None:
            continue
        notify_user(
            recipient=recipient,
            verb=make_verb(f"{penalty.amount_eur:.2f}"),
            sender=penalty.created_by,
            target=penalty,
            level="warning",
        )
