"""NotificationService — the ONE place where system e-mails are sent.

All outgoing e-mails (registration, approval, penalties, repayments, support,
future club information / round mails) go through
:class:`NotificationService`. Views, models and forms never contain e-mail
logic; they call ``notification_service.send_…(…)`` and nothing else.

Eligibility rules (players may exist WITHOUT a user account — accounts are
optional)::

    if (player.user and player.user.email and player.user.is_active):
        notification_service.send(…)

* players without a user account        -> no e-mail, only a log message,
* user accounts without a valid address -> no e-mail, only a log message,
* inactive user accounts                -> no e-mail, only a log message,
* missing/broken SMTP configuration     -> logged, never raised
  (an e-mail problem must never break a business transaction).

Adding a new e-mail type (club info, reminders, dunning, newsletter, …):
add a ``send_…`` method that builds a context and delegates to
``send_templated()`` (or ``send_to_users()`` for round mails) with a template
pair ``app/templates/emails/<name>.txt`` + ``.html``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import F, Max
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _

from app.core.choices import PenaltyNotifyChoice
from app.core.services import log_email_outcome
from app.notifications.models import NotificationOutbox

if TYPE_CHECKING:  # pragma: no cover — typing only
    from django.contrib.auth.models import AbstractBaseUser
    from django.http import HttpRequest

    from app.penalties.models import Payment, Penalty
    from app.players.models import Player

logger = logging.getLogger("app.notifications")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def email_configured() -> bool:
    """True when the active e-mail backend is able to deliver messages.

    Logs a clear warning when the SMTP backend is selected but ``EMAIL_HOST``
    is missing (misconfigured deployment, see ``.env.example``).
    """
    backend = (settings.EMAIL_BACKEND or "").lower()
    if "smtp" in backend and not settings.EMAIL_HOST:
        logger.warning(
            "E-mail not sent: EMAIL_BACKEND is SMTP but EMAIL_HOST is empty. "
            "Configure the SMTP block in your .env (see .env.example)."
        )
        return False
    return True


def eligible_user_for(player: Player | None) -> AbstractBaseUser | None:
    """Return the player's user account when it may receive e-mails.

    Implements the project-wide eligibility check::

        if (player.user and player.user.email and player.user.is_active):
            notification_service.send(…)

    Missing prerequisites are only logged — never raised.
    """
    if player is None:
        logger.info("Notification skipped: no player given.")
        return None
    user = player.user
    if user is None:
        logger.info(
            "Notification skipped: player '%s' has no linked user account (accounts are optional).",
            player,
        )
        return None
    if not user.email:
        logger.info("Notification skipped: user pk=%s has no e-mail address.", user.pk)
        return None
    if not user.is_active:
        logger.info("Notification skipped: user %s is inactive.", user.email or f"pk={user.pk}")
        return None
    return user


def absolute_url(path: str, request: HttpRequest | None = None) -> str:
    """Absolute URL for e-mail links: request > PUBLIC_SITE_URL > relative."""
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, "PUBLIC_SITE_URL", "")
    if base:
        return f"{base}{path}"
    logger.debug(
        "PUBLIC_SITE_URL is not set — using the relative link '%s' inside the e-mail.", path
    )
    return path


# ---------------------------------------------------------------------------
# Outbox — coalescing window, daily digest, quiet window
# ---------------------------------------------------------------------------
MAX_SEND_ATTEMPTS = 3

# Delivered rows older than this are deleted by ``prune_outbox()`` (called by
# the scheduler tick and ``manage.py flush_notification_outbox``) so the outbox
# table stays bounded. PENDING/failed rows are never pruned.
SENT_RETENTION_DAYS = 30


def _parse_quiet_window(raw: str | None) -> tuple[dt_time, dt_time] | None:
    """``"22:00-07:00"`` -> ``(22:00, 07:00)``; anything unusable -> ``None``."""
    text = (raw or "").strip().replace("–", "-")
    if not text:
        return None
    parts = text.split("-")
    if len(parts) != 2:
        logger.warning("NOTIFICATION_QUIET_HOURS %r is not 'HH:MM-HH:MM' — ignored.", raw)
        return None
    try:
        start = dt_time(*[int(value) for value in parts[0].strip().split(":")])
        end = dt_time(*[int(value) for value in parts[1].strip().split(":")])
    except (TypeError, ValueError):
        logger.warning("NOTIFICATION_QUIET_HOURS %r is not 'HH:MM-HH:MM' — ignored.", raw)
        return None
    if start == end:
        return None  # a window that starts when it ends is no window at all
    return start, end


def _quiet_window_end(now: datetime | None = None) -> datetime | None:
    """End of the CURRENT quiet window (local, aware) — or ``None`` outside it.

    The window may wrap midnight (``22:00-07:00``): the end is always the next
    occurrence of the end time, so a message due at 23:10 waits until 07:00.
    """
    window = _parse_quiet_window(getattr(settings, "NOTIFICATION_QUIET_HOURS", ""))
    if window is None:
        return None
    start, end = window
    local = timezone.localtime(now or timezone.now())
    current = local.time()
    if start < end:
        inside = start <= current < end
    else:  # wraps midnight
        inside = current >= start or current < end
    if not inside:
        return None
    window_end = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if window_end <= local:
        window_end += timedelta(days=1)
    return window_end


def _next_digest_send_after(user, now: datetime | None = None) -> datetime:
    """Next occurrence of the user's digest time (local, aware)."""
    local = timezone.localtime(now or timezone.now())
    digest_at = getattr(user, "penalty_notify_time", None) or dt_time(8, 0)
    target = local.replace(hour=digest_at.hour, minute=digest_at.minute, second=0, microsecond=0)
    if target <= local:  # today's slot has passed -> tomorrow
        target += timedelta(days=1)
    return target


def _send_after_for(user, mode: str, now: datetime) -> datetime:
    """When the queued batch of THIS user may go out (see ``queue_penalties``)."""
    if mode != PenaltyNotifyChoice.IMMEDIATE:
        return _next_digest_send_after(user, now)
    coalesce = max(0, int(getattr(settings, "NOTIFICATION_COALESCE_SECONDS", 90) or 0))
    send_after = now + timedelta(seconds=coalesce)
    quiet_end = _quiet_window_end(send_after)
    if quiet_end is not None:
        send_after = max(send_after, quiet_end)
    return send_after


def flush_outbox(*, now: datetime | None = None) -> int:
    """Deliver every due outbox row — ONE e-mail per user, never more.

    Called by the in-process scheduler thread, by
    ``manage.py flush_notification_outbox`` and right after queueing (a
    coalescing window of 0 seconds therefore mails immediately, which is how
    the test suite pins the historic "assign a penalty -> one e-mail"
    contract). Returns the number of messages actually sent. Never raises.
    """
    now = now or timezone.now()
    due = list(
        NotificationOutbox.objects.filter(sent_at__isnull=True, send_after__lte=now)
        .select_related("user")
        .order_by("send_after", "id")
    )
    if not due:
        return 0
    groups: dict[int, list[NotificationOutbox]] = {}
    for row in due:
        groups.setdefault(row.user_id, []).append(row)

    sent = 0
    for rows in groups.values():
        try:
            if _deliver_outbox_group(rows, now) == "sent":
                sent += 1
        except Exception:  # pragma: no cover — one bad batch must not stop the rest
            logger.exception(
                "Outbox flush failed for user pk=%s (rows %s).",
                rows[0].user_id,
                [row.pk for row in rows],
            )
    return sent


def prune_outbox(*, now: datetime | None = None) -> int:
    """Delete DELIVERED rows older than ``SENT_RETENTION_DAYS`` (review L4).

    Keeps the outbox table bounded without touching pending rows (they still
    owe a delivery) or recently sent ones (useful as a short-term audit trail).
    The returned count is the number of OUTBOX ROWS — counted explicitly,
    because ``QuerySet.delete()`` would also count cascade-deleted M2M
    through-rows of the attached penalties. Idempotent — called by the
    scheduler tick and ``flush_notification_outbox``.
    """
    cutoff = (now or timezone.now()) - timedelta(days=SENT_RETENTION_DAYS)
    stale = NotificationOutbox.objects.filter(sent_at__isnull=False, sent_at__lt=cutoff)
    count = stale.count()
    if count:
        stale.delete()
        logger.info(
            "Pruned %s delivered outbox row(s) older than %s days.", count, SENT_RETENTION_DAYS
        )
    return count


def _deliver_outbox_group(rows: list[NotificationOutbox], now: datetime) -> str:
    """Claim one user's due rows and turn them into ONE e-mail.

    ``sent_at`` is set BEFORE rendering/sending in a single atomic
    ``UPDATE … WHERE sent_at IS NULL`` — that claim makes the scheduler thread
    and an external cron mutually exclusive. A failed delivery unclaims the
    rows again so the next flush retries (up to ``MAX_SEND_ATTEMPTS``).
    """
    row_ids = [row.pk for row in rows]
    claimed = NotificationOutbox.objects.filter(pk__in=row_ids, sent_at__isnull=True).update(
        sent_at=now, attempts=F("attempts") + 1
    )
    if not claimed:
        return "claimed"  # another flusher got there first — never send twice

    user = rows[0].user
    through = NotificationOutbox.penalties.through
    penalty_ids = list(
        through.objects.filter(notificationoutbox_id__in=row_ids).values_list(
            "penalty_id", flat=True
        )
    )
    try:
        outcome = _send_outbox_group(user=user, penalty_ids=penalty_ids, language=rows[0].language)
    except Exception:
        # Review B4: a rendering/context crash AFTER the claim must follow the
        # SAME path as a failed send — unclaim (retry up to MAX_SEND_ATTEMPTS)
        # instead of leaving the rows marked "sent" forever. The traceback goes
        # to the log; the row keeps the bounded generic last_error below.
        logger.exception(
            "Outbox delivery crashed for user pk=%s (rows %s) — retry path.",
            user.pk,
            row_ids,
        )
        outcome = "failed"

    if outcome != "failed":
        return outcome

    attempts = (
        NotificationOutbox.objects.filter(pk__in=row_ids).aggregate(attempts_max=Max("attempts"))[
            "attempts_max"
        ]
        or 0
    )
    if attempts >= MAX_SEND_ATTEMPTS:
        NotificationOutbox.objects.filter(pk__in=row_ids).update(
            last_error=f"delivery failed after {attempts} attempts"
        )
        logger.error(
            "Queued notification abandoned for %s after %s failed attempts.",
            user.email,
            attempts,
        )
        return "failed"
    # Unclaim: the next flush (scheduler tick / cron) retries the batch.
    NotificationOutbox.objects.filter(pk__in=row_ids).update(
        sent_at=None, last_error="delivery failed"
    )
    return "failed"


def _send_outbox_group(*, user, penalty_ids: Iterable, language: str) -> str:
    """Render + send ONE message for the claimed rows: ``sent``/``skipped``/``failed``."""
    address = getattr(user, "email", "") or ""
    if not getattr(user, "is_active", False) or "@" not in address:
        logger.info(
            "Queued notification skipped: %s is no longer an eligible recipient.",
            getattr(user, "pk", address),
        )
        return "skipped"
    if getattr(user, "penalty_notify_mode", PenaltyNotifyChoice.DAILY) == PenaltyNotifyChoice.OFF:
        logger.info("Queued notification skipped: %s opted out of penalty e-mails.", address)
        return "skipped"

    from app.penalties.models import Penalty

    # Default manager: soft-deleted rows vanish from the batch automatically.
    penalties = list(
        Penalty.objects.filter(pk__in=list(penalty_ids))
        .select_related("player", "matchday", "matchday__team", "matchday__season")
        .order_by("created_at", "id")
    )
    if not penalties:
        logger.info("Queued notification skipped: nothing left to report (all rows deleted).")
        return "skipped"

    if len(penalties) == 1:
        # One penalty = the familiar single-penalty mail (subject + content).
        sent = notification_service.send_penalty_created(
            user=user, penalty=penalties[0], language=language
        )
    else:
        sent = notification_service.send_penalties_digest(
            user=user, penalties=penalties, language=language
        )
    return "sent" if sent else "failed"


def _user_display(user) -> str:
    """Human label for an account in e-mail context: linked player name, else e-mail.

    ``None`` (e.g. a deleted ``created_by``) renders as an en dash.
    """
    if user is None:
        return "–"
    player = getattr(user, "player_link", None)
    name = getattr(player, "name", "") if player is not None else ""
    return name or user.email


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class NotificationService:
    """Central, extensible e-mail service (HTML + plain text)."""

    # -- generic core -----------------------------------------------------
    def send_templated(
        self,
        *,
        recipients: Iterable,
        template_base: str,
        subject: str,
        context: dict | None = None,
        request: HttpRequest | None = None,
        language: str | None = None,
        bcc: Iterable | None = None,
    ) -> int:
        """Render ``<template_base>.txt`` + ``.html`` and send to ``recipients``.

        Recipients may be user objects or plain addresses; inactive users and
        empty/invalid addresses are filtered out. ``bcc`` receives the same
        message without exposing the addresses to the primary recipients
        (privacy: admin round mails). Returns the number of primary recipients
        the message was sent to. Never raises — delivery problems are logged
        instead so business flows are never interrupted.

        With ``settings.ASYNC_NOTIFICATIONS`` (production default) rendering +
        sending run in a background thread so SMTP latency never blocks the
        single web worker; tests force synchronous delivery (see settings).
        """
        to = self._valid_recipients(recipients)
        if not to:
            logger.info("Notification '%s' skipped: no eligible recipient.", template_base)
            return 0
        to_keys = {address.lower() for address in to}
        bcc_list = [
            address
            for address in self._valid_recipients(bcc or [])
            if address.lower() not in to_keys
        ]

        # Subject and language are fixed in the CALLER's context (request
        # language active) before any background thread starts.
        subject_line = " ".join(str(subject).splitlines()).strip()
        lang = language or translation.get_language()

        if not email_configured():
            log_email_outcome(
                success=False,
                recipients=to,
                bcc=bcc_list,
                template=template_base,
                subject=subject_line,
                language=lang,
                error="SMTP backend not configured",
            )
            return 0

        ctx = {
            "support_email": settings.SUPPORT_EMAIL,
            "site_name": "Darts Penalty Manager",
        }
        ctx.update(context or {})

        if getattr(settings, "ASYNC_NOTIFICATIONS", False):
            worker = threading.Thread(
                target=self._deliver,
                args=(to, bcc_list, template_base, subject_line, ctx, lang),
                daemon=True,
                name=f"notify-{template_base}",
            )
            worker.start()
            logger.info("Notification '%s' queued for %s (async delivery).", template_base, to)
            return len(to)
        return self._deliver(to, bcc_list, template_base, subject_line, ctx, lang)

    @staticmethod
    def _deliver(to, bcc_list, template_base, subject_line, ctx, lang) -> int:
        """Render + send — runs inline (tests) or in a background thread.

        Every outcome (success, template missing, SMTP failure) is audited
        best-effort via ``log_email_outcome`` — the audit write itself can
        never break the delivery (and delivery never breaks the business
        transaction that triggered it).
        """
        try:
            try:
                with translation.override(lang):
                    text_body = render_to_string(f"{template_base}.txt", ctx)
                    html_body = render_to_string(f"{template_base}.html", ctx)
            except TemplateDoesNotExist:
                logger.exception(
                    "Notification '%s' not sent: template missing (%s.txt/.html).",
                    template_base,
                    template_base,
                )
                log_email_outcome(
                    success=False,
                    recipients=to,
                    bcc=bcc_list,
                    template=template_base,
                    subject=subject_line,
                    language=lang,
                    error=f"template missing: {template_base}",
                )
                return 0
            message = EmailMultiAlternatives(
                subject_line, text_body, settings.DEFAULT_FROM_EMAIL, to, bcc=bcc_list
            )
            message.attach_alternative(html_body, "text/html")
            try:
                message.send(fail_silently=False)
            except Exception as exc:
                logger.exception("Sending notification '%s' to %s failed.", template_base, to)
                log_email_outcome(
                    success=False,
                    recipients=to,
                    bcc=bcc_list,
                    template=template_base,
                    subject=subject_line,
                    language=lang,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return 0
            log_email_outcome(
                success=True,
                recipients=to,
                bcc=bcc_list,
                template=template_base,
                subject=subject_line,
                language=lang,
            )
            logger.info("Notification '%s' sent to %s.", template_base, to)
            return len(to)
        finally:
            if threading.current_thread() is not threading.main_thread():
                # Background thread: template rendering may have opened DB
                # connections — close them so the pool does not leak.
                from django.db import connections

                connections.close_all()

    def send_to_users(
        self,
        users: Iterable,
        *,
        template_base: str,
        subject: str,
        context: dict | None = None,
        request: HttpRequest | None = None,
    ) -> int:
        """Round mail (club information, reminders, dunning, newsletter, …).

        Extension point: only eligible (active, valid address) recipients
        receive the message.
        """
        recipients = []
        language = None
        for user in users:
            if user is None:
                continue
            if getattr(user, "is_active", True) and getattr(user, "email", ""):
                recipients.append(user)
                language = language or getattr(user, "preferred_language", None)
        return self.send_templated(
            recipients=recipients,
            template_base=template_base,
            subject=subject,
            context=context,
            request=request,
            language=language,
        )

    @staticmethod
    def _valid_recipients(recipients: Iterable) -> list:
        """User objects / addresses with a plausible, deliverable target."""
        valid: list = []
        seen: set[str] = set()
        for recipient in recipients or []:
            if recipient is None:
                continue
            is_user = hasattr(recipient, "email")
            if is_user:
                if not getattr(recipient, "is_active", True):
                    logger.info(
                        "Notification recipient skipped (inactive): %s",
                        getattr(recipient, "email", recipient),
                    )
                    continue
                address = recipient.email
            else:
                address = str(recipient)
            address = (address or "").strip()
            if not address or "@" not in address:
                logger.info("Notification recipient skipped (no valid address): %r", address)
                continue
            key = address.lower()
            if key not in seen:
                seen.add(key)
                valid.append(address)
        return valid

    # -- account approval workflow ---------------------------------------
    def send_registration_received(self, user, *, request: HttpRequest | None = None) -> int:
        """New self-registration -> inform the admins (support + Admin group).

        ``SUPPORT_EMAIL`` is the primary recipient; the admins go to BCC so
        the round mail never exposes every admin's address to the others.
        """
        from django.urls import reverse

        from app.accounts.models import User
        from app.core.permissions import GROUP_ADMIN

        admin_emails = list(
            User.objects.filter(groups__name=GROUP_ADMIN, is_active=True)
            .exclude(email="")
            .values_list("email", flat=True)
        )
        if settings.SUPPORT_EMAIL:
            recipients: list = [settings.SUPPORT_EMAIL]
            bcc = [email for email in admin_emails if email != settings.SUPPORT_EMAIL]
        else:  # no support mailbox configured -> admins are the primary target
            recipients, bcc = admin_emails, []
        return self.send_templated(
            recipients=recipients,
            bcc=bcc,
            template_base="emails/account_registered",
            subject=_("New registration: {email}").format(email=user.email),
            context={
                "new_user": user,
                "approval_url": absolute_url(reverse("accounts:approval", args=[user.pk]), request),
                "registered_at": user.date_joined,
            },
            request=request,
            language=getattr(user, "preferred_language", None),
        )

    def send_account_approved(self, user, *, request: HttpRequest | None = None) -> int:
        """Activation e-mail sent automatically when an admin approves a user."""
        from django.urls import reverse

        return self.send_templated(
            recipients=[user],
            template_base="emails/account_approved",
            subject=_("Your account has been activated"),
            context={
                "approved_user": user,
                "login_url": absolute_url(reverse("account_login"), request),
            },
            request=request,
            language=getattr(user, "preferred_language", None),
        )

    def send_account_rejected(self, user, *, request: HttpRequest | None = None) -> int:
        """Rejection e-mail sent automatically when an admin declines a user."""
        return self.send_templated(
            recipients=[user],
            template_base="emails/account_rejected",
            subject=_("Your registration has been declined"),
            context={"rejected_user": user},
            request=request,
            language=getattr(user, "preferred_language", None),
        )

    # -- invitations ------------------------------------------------------
    def send_invitation(self, invitation, *, request: HttpRequest | None = None) -> int:
        """Admin-sent invitation to the invitee: accept link, expiry, sender.

        Content rules: prominent accept button, expiry date, who sent it, and
        the anti-phishing line ("If you don't know this club, ignore this
        message."). Delivery failures surface as the view's warning message —
        this method returns the recipient count like every ``send_…``.
        """
        from django.urls import reverse

        user = invitation.user
        return self.send_templated(
            recipients=[user],
            template_base="emails/invitation",
            subject=_("You have been invited to {site}").format(site="Darts Penalty Manager"),
            context={
                "invitation": invitation,
                "invitee_email": user.email,
                "accept_url": absolute_url(
                    reverse("accounts:invite_accept", args=[invitation.token]), request
                ),
                "expires_at": invitation.expires_at,
                "invited_by": invitation.invited_by,
            },
            request=request,
            language=getattr(user, "preferred_language", None),
        )

    def send_invitation_completed(self, user, *, request: HttpRequest | None = None) -> int:
        """Round mail after acceptance — same pattern as ``send_registration_received``.

        ``SUPPORT_EMAIL`` is the primary recipient; the admins go to BCC so
        the round mail never exposes every admin's address to the others.
        Never raises (delegates to ``send_templated``).
        """
        from django.urls import reverse

        from app.accounts.models import User
        from app.core.permissions import GROUP_ADMIN

        admin_emails = list(
            User.objects.filter(groups__name=GROUP_ADMIN, is_active=True)
            .exclude(email="")
            .values_list("email", flat=True)
        )
        if settings.SUPPORT_EMAIL:
            recipients: list = [settings.SUPPORT_EMAIL]
            bcc = [email for email in admin_emails if email != settings.SUPPORT_EMAIL]
        else:  # no support mailbox configured -> admins are the primary target
            recipients, bcc = admin_emails, []
        invitation = user.pending_invitation
        return self.send_templated(
            recipients=recipients,
            bcc=bcc,
            template_base="emails/invitation_completed",
            subject=_("Invited user completed registration: {email}").format(email=user.email),
            context={
                "new_user": user,
                "users_url": absolute_url(reverse("accounts:user_list"), request),
                "completed_at": invitation.accepted_at if invitation else None,
            },
            request=request,
            language=getattr(user, "preferred_language", None),
        )

    # -- penalties / repayments ------------------------------------------
    def send_penalty_created(
        self,
        *,
        user,
        penalty: Penalty,
        request: HttpRequest | None = None,
        language: str | None = None,
    ) -> int:
        """New penalty: amount, reason, matchday, team, season + open total.

        The open total is scoped to the penalty's own season (seasons are
        closed cash boxes) so the mail matches what the UI shows. ``language``
        lets the outbox flusher render with the snapshot taken at queue time
        (falling back to the user's current preference).
        """
        from app.penalties.services import player_balance

        matchday = penalty.matchday
        season = matchday.season
        team = matchday.team
        return self.send_templated(
            recipients=[user],
            template_base="emails/penalty_created",
            subject=_("New penalty: {amount} €").format(amount=f"{penalty.amount_eur:.2f}"),
            context={
                "recipient_user": user,
                "penalty": penalty,
                "player": penalty.player,
                "amount": penalty.amount_eur,
                "reason": penalty.display_description(),
                "penalty_date": penalty.created_at,
                "matchday": matchday,
                "team": team,
                "season": season,
                "open_total": player_balance(penalty.player, season=season),
            },
            request=request,
            language=language or getattr(user, "preferred_language", None),
        )

    def send_penalty_repayment(
        self,
        *,
        user,
        repayment: Payment,
        request: HttpRequest | None = None,
        recipient_role: str = "payer",
    ) -> int:
        """Payment confirmation ("Bestätigung für die Zahlung") for ONE recipient.

        Remaining balance and the full/partial verdict use the PLAYER's own
        season balance (all teams) — the exact number the payer sees in the
        "own penalties" section of the financial overview. The per-team table
        of the overview is team-scoped; single-team clubs see identical numbers.
        """
        from app.penalties.services import player_balance

        season = repayment.season
        remaining = player_balance(repayment.player, season=season)
        # remaining < 0 = the player paid more than they owed — the mail then
        # shows the credit next to the "nothing is owed anymore" status
        # instead of hiding the negative remainder entirely.
        credit = -remaining if remaining < 0 else Decimal(0)
        return self.send_templated(
            recipients=[user],
            template_base="emails/penalty_repayment",
            subject=_("Payment confirmation: {amount} €").format(
                amount=f"{repayment.amount_eur:.2f}"
            ),
            context={
                "recipient_user": user,
                "repayment": repayment,
                "player": repayment.player,
                "amount": repayment.amount_eur,
                "repayment_date": repayment.created_at,
                "team": repayment.team,
                "season": season,
                "remaining": remaining,
                "credit": credit,
                "is_full_repayment": remaining <= 0,
                "recipient_role": recipient_role,
                "is_payer": recipient_role == "payer",
                "received_by_label": _user_display(repayment.received_by),
                "recorded_by_label": _user_display(repayment.created_by),
            },
            request=request,
            language=getattr(user, "preferred_language", None),
        )

    def send_penalty_payout(
        self,
        *,
        user,
        payout: Payment,
        request: HttpRequest | None = None,
        recipient_role: str = "payer",
    ) -> int:
        """Payout confirmation ("Bestätigung für die Auszahlung") for ONE recipient.

        ``payout.amount_eur`` is negative by design — the mail shows the
        paid-out amount as a positive number. The remaining credit is
        TEAM-scoped (``player_team_balance`` for the payout's own team/season),
        exactly the pot the payout was capped against — credit the player
        still holds with OTHER teams belongs to those pots and never appears
        in this mail (review L2).
        """
        from app.penalties.services import player_team_balance

        season = payout.season
        team_credit = player_team_balance(payout.player, payout.team, season=season)
        # Team credit left after the payout; 0 (falsy) = THIS pot's credit is
        # fully paid out, so the template branches on ``credit`` alone.
        credit = -team_credit if team_credit < 0 else Decimal(0)
        return self.send_templated(
            recipients=[user],
            template_base="emails/penalty_payout",
            subject=_("Payout confirmation: {amount} €").format(amount=f"{-payout.amount_eur:.2f}"),
            context={
                "recipient_user": user,
                "payout": payout,
                "player": payout.player,
                "amount": -payout.amount_eur,
                "payout_date": payout.created_at,
                "team": payout.team,
                "season": season,
                "credit": credit,
                "recipient_role": recipient_role,
                "is_payer": recipient_role == "payer",
                "received_by_label": _user_display(payout.received_by),
                "recorded_by_label": _user_display(payout.created_by),
            },
            request=request,
            language=getattr(user, "preferred_language", None),
        )

    # -- outbox: queue + digest --------------------------------------------
    def queue_penalties(self, penalties: Iterable) -> int:
        """Queue ONE outbox row per eligible user for a batch of NEW penalties.

        This replaces the old "one e-mail per penalty row" loop: whatever a
        trainer enters in one go (or within the coalescing window) is grouped
        per player and delivered as ONE message by ``flush_outbox()``.

        Returns the number of queued rows (0 when nobody is eligible or every
        recipient opted out).
        """
        users: dict[int, object] = {}
        grouped: dict[int, list] = {}
        for penalty in penalties or []:
            user = eligible_user_for(penalty.player)
            if user is None:
                continue
            users.setdefault(user.pk, user)
            grouped.setdefault(user.pk, []).append(penalty)
        if not grouped:
            return 0

        now = timezone.now()
        queued = 0
        for user_pk, rows in grouped.items():
            user = users[user_pk]
            mode = getattr(user, "penalty_notify_mode", PenaltyNotifyChoice.DAILY)
            if mode == PenaltyNotifyChoice.OFF:
                logger.info(
                    "Penalty notification skipped: %s opted out of penalty e-mails.",
                    user.email,
                )
                continue
            entry = NotificationOutbox.objects.create(
                user=user,
                email=user.email,
                language=getattr(user, "preferred_language", "") or "",
                send_after=_send_after_for(user, mode, now),
            )
            entry.penalties.set(rows)
            queued += 1
        logger.info("Queued %s penalty notification(s) for the outbox flush.", queued)
        return queued

    def send_penalties_digest(
        self,
        *,
        user,
        penalties: Iterable,
        request: HttpRequest | None = None,
        language: str | None = None,
    ) -> int:
        """Collected e-mail listing EVERY new penalty since the last flush.

        Sent when a batch holds more than one row for the same player (the
        whole point of the outbox): a table of date / matchday / team / reason
        / amount plus the season-scoped open total per season involved.
        """
        from app.penalties.services import player_balance

        rows = sorted(penalties, key=lambda penalty: (penalty.created_at, penalty.pk))
        if not rows:
            return 0
        total = sum((row.amount_eur for row in rows), Decimal(0))

        grouped: dict[tuple, list] = {}
        for row in rows:
            grouped.setdefault((row.player_id, row.matchday.season_id), []).append(row)
        season_summaries = [
            {
                "season": group[0].matchday.season,
                "player": group[0].player,
                "count": len(group),
                "sum": sum((item.amount_eur for item in group), Decimal(0)),
                "open_total": player_balance(group[0].player, season=group[0].matchday.season),
            }
            for group in grouped.values()
        ]

        return self.send_templated(
            recipients=[user],
            template_base="emails/penalty_digest",
            subject=_("New penalties: {count} ({total} €)").format(
                count=len(rows), total=f"{total:.2f}"
            ),
            context={
                "recipient_user": user,
                "player": rows[0].player,
                "penalties": rows,
                "count": len(rows),
                "total": total,
                "season_summaries": season_summaries,
                "period_start": rows[0].created_at,
                "period_end": rows[-1].created_at,
            },
            request=request,
            language=language or getattr(user, "preferred_language", None),
        )

    def club_news_recipients(self) -> list:
        """Users subscribed to round mails / club information (Settings card).

        Extension point for the future round mail (``send_to_users()``): filter
        first, then send — nobody receives club news who unchecked the box.
        """
        from app.accounts.models import User

        return list(
            User.objects.filter(is_active=True, club_news_optin=True)
            .exclude(email="")
            .order_by("email")
        )

    # -- support ----------------------------------------------------------
    def send_support_request(
        self, *, name: str, email: str, message: str, request: HttpRequest | None = None
    ) -> int:
        """Forward a support request to SUPPORT_EMAIL."""
        return self.send_templated(
            recipients=[settings.SUPPORT_EMAIL],
            template_base="emails/support_request",
            subject=_("Support request from {name}").format(name=name),
            context={"sender_name": name, "sender_email": email, "message": message},
            request=request,
        )


# Module-level singleton used across the codebase:
# ``notification_service.send_account_approved(user)``
notification_service = NotificationService()
