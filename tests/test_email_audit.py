"""EMAIL_SENT / EMAIL_FAILED audit rows — NotificationService + allauth adapter.

Every outgoing e-mail writes one best-effort audit row (success or failure)
while "no eligible recipient" skips stay logger-only. The audit write itself
can never break a mail — or the business transaction behind it.
"""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.notifications.services import notification_service
from app.penalties.models import Penalty
from app.penalties.services import assign_penalty

pytestmark = pytest.mark.django_db

User = get_user_model()

EMAIL_ACTIONS = [AuditAction.EMAIL_SENT, AuditAction.EMAIL_FAILED]


def _link_user(player, email="member@example.com"):
    """Create a user account linked to ``player`` (approved + active).

    ``immediate`` (not the production default ``daily``) so the historic
    "assign -> one e-mail is sent AND audited right now" contract is testable.
    """
    user = User.objects.create_user(
        email=email, password="pw", approval_status="approved", penalty_notify_mode="immediate"
    )
    user.player_link = player
    user.save()
    return user


def _signup(client, email="fresh@example.com"):
    """POST a signup and isolate it from ALLAUTH's confirmation cooldown.

    allauth throttles confirmation mails per address ("confirm_email":
    1/180s/key) in the Django cache — the LocMem cache persists across tests,
    so an earlier test's signup with the SAME address would silently suppress
    the mail (and its audit row). Clearing the cache isolates the test.
    """
    from django.core.cache import cache

    cache.clear()
    return client.post(
        reverse("account_signup"),
        {
            "email": email,
            "password1": "Strong-Pass-123!",
            "password2": "Strong-Pass-123!",
        },
    )


# ---------------------------------------------------------------------------
# 1) Successful delivery
# ---------------------------------------------------------------------------
def test_successful_send_writes_email_sent_row(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _link_user(players[0])

    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )

    assert len(mail.outbox) == 1
    entry = AuditLog.objects.get(action=AuditAction.EMAIL_SENT)
    meta = entry.metadata
    assert meta["success"] is True
    assert meta["recipients"] == ["member@example.com"]
    assert meta["recipient_count"] == 1
    assert meta["template"] == "emails/penalty_created"
    assert meta["subject"]  # non-empty
    assert entry.user is None  # no actor is threaded through the mail service


# ---------------------------------------------------------------------------
# 2) SMTP failure — audited, but the business transaction completes
# ---------------------------------------------------------------------------
def test_smtp_failure_writes_email_failed_but_completes_transaction(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _link_user(players[0])

    with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("boom")):
        assign_penalty(
            matchday=md,
            player=players[0],
            catalog_item=catalog_normal,
            amount_eur=5,
            description_snapshot="Late arrival",
            actor=admin_user,
        )

    assert Penalty.objects.count() == 1  # the penalty was still created
    entry = AuditLog.objects.get(action=AuditAction.EMAIL_FAILED)
    assert entry.metadata["success"] is False
    assert "OSError" in entry.metadata["error"]
    assert entry.metadata["template"] == "emails/penalty_created"


# ---------------------------------------------------------------------------
# 3) Missing template
# ---------------------------------------------------------------------------
def test_missing_template_writes_email_failed(db):
    sent = notification_service.send_templated(
        recipients=["someone@example.com"],
        template_base="emails/does_not_exist_xyz",
        subject="Test",
    )
    assert sent == 0
    assert len(mail.outbox) == 0  # nothing was sent
    entry = AuditLog.objects.get(action=AuditAction.EMAIL_FAILED)
    assert entry.metadata["success"] is False
    assert "does_not_exist_xyz" in entry.metadata["error"]


# ---------------------------------------------------------------------------
# 4) Unconfigured SMTP
# ---------------------------------------------------------------------------
def test_unconfigured_smtp_writes_email_failed(db):
    with override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST=""
    ):
        sent = notification_service.send_templated(
            recipients=["someone@example.com"],
            template_base="emails/penalty_created",
            subject="Hi",
        )
    assert sent == 0
    assert len(mail.outbox) == 0
    entry = AuditLog.objects.get(action=AuditAction.EMAIL_FAILED)
    assert entry.metadata["error"] == "SMTP backend not configured"


# ---------------------------------------------------------------------------
# 5) "No eligible recipient" stays logger-only (no audit noise)
# ---------------------------------------------------------------------------
def test_no_eligible_recipient_writes_no_audit_row(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)  # player has NO user account
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    assert len(mail.outbox) == 0
    assert not AuditLog.objects.filter(action__in=EMAIL_ACTIONS).exists()


# ---------------------------------------------------------------------------
# 6) Allauth adapter path (success + failure)
# ---------------------------------------------------------------------------
def test_allauth_signup_writes_adapter_email_sent_row(db):
    assert _signup(Client()).status_code in (200, 302)

    adapter_entries = [
        entry
        for entry in AuditLog.objects.filter(action=AuditAction.EMAIL_SENT)
        if entry.metadata.get("template", "").startswith("allauth:")
    ]
    assert adapter_entries  # the verification e-mail went through our adapter
    meta = adapter_entries[0].metadata
    assert meta["template"].startswith("allauth:account/")  # allauth template prefix
    assert meta["recipients"] == ["fresh@example.com"]
    assert meta["success"] is True
    assert meta["subject"]  # non-empty


def test_allauth_adapter_failure_writes_email_failed_row(db):
    with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("boom")):
        assert _signup(Client(), email="broken@example.com").status_code in (200, 302)

    failed = [
        entry
        for entry in AuditLog.objects.filter(action=AuditAction.EMAIL_FAILED)
        if entry.metadata.get("template", "").startswith("allauth:")
    ]
    assert failed
    assert "OSError" in failed[0].metadata["error"]
    # the flow survived the mail failure (fault-tolerant adapter)
    assert User.objects.filter(email="broken@example.com").exists()


# ---------------------------------------------------------------------------
# 7) Audit writes are best-effort — a failing audit never breaks the e-mail
# ---------------------------------------------------------------------------
def test_audit_write_failure_never_breaks_mail(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _link_user(players[0])

    with mock.patch("app.core.services.log_action", side_effect=RuntimeError("audit down")):
        assign_penalty(
            matchday=md,
            player=players[0],
            catalog_item=catalog_normal,
            amount_eur=5,
            description_snapshot="Late arrival",
            actor=admin_user,
        )

    assert len(mail.outbox) == 1  # the mail still went out, no exception raised
    assert not AuditLog.objects.filter(action__in=EMAIL_ACTIONS).exists()
