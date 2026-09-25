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
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.translation import gettext_lazy as _

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
        if not email_configured():
            return 0

        # Subject and language are fixed in the CALLER's context (request
        # language active) before any background thread starts.
        subject_line = " ".join(str(subject).splitlines()).strip()
        lang = language or translation.get_language()

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
        """Render + send — runs inline (tests) or in a background thread."""
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
                return 0
            message = EmailMultiAlternatives(
                subject_line, text_body, settings.DEFAULT_FROM_EMAIL, to, bcc=bcc_list
            )
            message.attach_alternative(html_body, "text/html")
            try:
                message.send(fail_silently=False)
            except Exception:
                logger.exception("Sending notification '%s' to %s failed.", template_base, to)
                return 0
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

    # -- penalties / repayments ------------------------------------------
    def send_penalty_created(
        self, *, user, penalty: Penalty, request: HttpRequest | None = None
    ) -> int:
        """New penalty: amount, reason, matchday, team, season + open total.

        The open total is scoped to the penalty's own season (seasons are
        closed cash boxes) so the mail matches what the UI shows.
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
                "reason": penalty.description_snapshot,
                "penalty_date": penalty.created_at,
                "matchday": matchday,
                "team": team,
                "season": season,
                "open_total": player_balance(penalty.player, season=season),
            },
            request=request,
            language=getattr(user, "preferred_language", None),
        )

    def send_penalty_repayment(
        self, *, user, repayment: Payment, request: HttpRequest | None = None
    ) -> int:
        """Recorded repayment: amount, team, season, remaining balance, date.

        Remaining balance and the full/partial verdict are scoped to the
        payment's season — exactly what the financial overview shows.
        """
        from app.penalties.services import player_balance

        season = repayment.season
        remaining = player_balance(repayment.player, season=season)
        return self.send_templated(
            recipients=[user],
            template_base="emails/penalty_repayment",
            subject=_("Payment received: {amount} €").format(amount=f"{repayment.amount_eur:.2f}"),
            context={
                "recipient_user": user,
                "repayment": repayment,
                "player": repayment.player,
                "amount": repayment.amount_eur,
                "repayment_date": repayment.created_at,
                "team": repayment.team,
                "season": season,
                "remaining": remaining,
                "is_full_repayment": remaining <= 0,
            },
            request=request,
            language=getattr(user, "preferred_language", None),
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
