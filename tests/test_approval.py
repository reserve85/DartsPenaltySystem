"""Approval workflow — pending/rejected/approved logins, admin review, signup.

Covers the requirement "Login only for approved users" end-to-end:
registration (django-allauth) -> pending -> admin notification e-mail ->
review screen (approve + player link in ONE save) -> activation e-mail ->
login possible.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.urls import reverse

from app.accounts.models import ApprovalStatus
from app.core.models import AuditAction, AuditLog
from app.core.permissions import GROUP_PLAYER
from app.players.models import Player

pytestmark = pytest.mark.django_db

User = get_user_model()


def _login(client, email, password="pw"):
    return client.post(reverse("account_login"), {"login": email, "password": password})


# ---------------------------------------------------------------------------
# Login gate
# ---------------------------------------------------------------------------
def test_pending_user_cannot_login_and_gets_clear_message(pending_user):
    response = _login(Client(), "newbie@example.com")
    assert response.status_code == 200
    text = response.content.decode()
    # Language-tolerant: the login error renders in the active catalog (de/en).
    assert "not been approved by an administrator yet" in text or "freigegeben" in text


def test_rejected_user_cannot_login_and_gets_clear_message(db):
    user = User.objects.create_user(
        email="no@example.com", password="pw", approval_status=ApprovalStatus.REJECTED
    )
    response = _login(Client(), user.email)
    assert response.status_code == 200
    # Language-tolerant: renders in the active catalog (de/en).
    text = response.content.decode()
    assert "registration has been declined" in text or "Registrierung wurde abgelehnt" in text


def test_approved_user_can_login(admin_user):
    client = Client()
    response = _login(client, "admin@example.com")
    assert response.status_code == 302
    assert "_auth_user_id" in client.session


def test_stale_pending_session_is_kicked_by_middleware(pending_user):
    """Safety net: force_logged-in pending users never reach the application."""
    client = Client()
    client.force_login(pending_user)
    response = client.get(reverse("dashboard:index"))
    assert response.status_code == 302
    assert response.url == reverse("account_login")
    assert "_auth_user_id" not in client.session


# ---------------------------------------------------------------------------
# Admin review screens
# ---------------------------------------------------------------------------
def test_approval_list_permissions(admin_client, captain_client, client, pending_user):
    assert admin_client.get(reverse("accounts:approval_list")).status_code == 200
    assert captain_client.get(reverse("accounts:approval_list")).status_code == 403
    assert client.get(reverse("accounts:approval_list")).status_code == 302


def test_pending_user_shows_review_button_in_user_list(admin_client, pending_user):
    content = admin_client.get(reverse("accounts:user_list")).content.decode()
    assert pending_user.email in content
    assert reverse("accounts:approval", args=[pending_user.pk]) in content


def test_approval_screen_renders_user_and_form(admin_client, pending_user, team):
    Player.objects.create(name="Free Player", team=team)
    response = admin_client.get(reverse("accounts:approval", args=[pending_user.pk]))
    assert response.status_code == 200
    assert pending_user.email in response.content.decode()
    assert "Free Player" in response.content.decode()


def test_approval_requires_admin(admin_client, captain_client, player_client, pending_user):
    url = reverse("accounts:approval", args=[pending_user.pk])
    assert admin_client.get(url).status_code == 200
    assert captain_client.get(url).status_code == 403
    assert player_client.get(url).status_code == 403
    assert Client().get(url).status_code == 302


# ---------------------------------------------------------------------------
# Approve + player link in ONE save
# ---------------------------------------------------------------------------
def test_approve_with_existing_player_single_post(admin_client, pending_user, team, role_groups):
    player = Player.objects.create(name="Andi Beispiel", team=team)
    response = admin_client.post(
        reverse("accounts:approval", args=[pending_user.pk]),
        {"action": "approve", "player": player.pk},
    )
    assert response.status_code == 302

    pending_user.refresh_from_db()
    assert pending_user.approval_status == ApprovalStatus.APPROVED
    assert pending_user.player_link == player  # linked in the SAME save
    # Freshly approved self-registered users become players (if no role yet).
    assert pending_user.groups.filter(name=GROUP_PLAYER).exists()
    assert (
        AuditLog.objects.filter(action=AuditAction.USER_APPROVED, target_id=pending_user.pk).count()
        == 1
    )
    # Activation e-mail went out to the new user (subject is catalog-localized).
    assert any("activated" in m.subject or "aktiviert" in m.subject for m in mail.outbox)
    assert any("newbie@example.com" in m.to for m in mail.outbox)


def test_approve_creates_new_player_when_requested(admin_client, pending_user):
    response = admin_client.post(
        reverse("accounts:approval", args=[pending_user.pk]),
        {"action": "approve", "new_player_name": "Neu Angelegen"},
    )
    assert response.status_code == 302
    pending_user.refresh_from_db()
    assert pending_user.approval_status == ApprovalStatus.APPROVED
    assert pending_user.player_link.name == "Neu Angelegen"


def test_approve_without_player_is_allowed(admin_client, pending_user):
    response = admin_client.post(
        reverse("accounts:approval", args=[pending_user.pk]), {"action": "approve"}
    )
    assert response.status_code == 302
    pending_user.refresh_from_db()
    assert pending_user.approval_status == ApprovalStatus.APPROVED
    assert pending_user.player_link is None


def test_approve_rejects_player_and_new_name_at_once(admin_client, pending_user, team):
    player = Player.objects.create(name="Taken", team=team)
    response = admin_client.post(
        reverse("accounts:approval", args=[pending_user.pk]),
        {"action": "approve", "player": player.pk, "new_player_name": "Doppelt"},
    )
    assert response.status_code == 200  # form re-rendered with an error
    pending_user.refresh_from_db()
    assert pending_user.approval_status == ApprovalStatus.PENDING  # nothing saved


def test_reject_sends_rejection_email_and_audit(admin_client, pending_user):
    response = admin_client.post(
        reverse("accounts:approval", args=[pending_user.pk]), {"action": "reject"}
    )
    assert response.status_code == 302
    pending_user.refresh_from_db()
    assert pending_user.approval_status == ApprovalStatus.REJECTED
    assert (
        AuditLog.objects.filter(action=AuditAction.USER_REJECTED, target_id=pending_user.pk).count()
        == 1
    )
    assert any("declined" in m.subject or "abgelehnt" in m.subject for m in mail.outbox)
    assert any("newbie@example.com" in m.to for m in mail.outbox)


def test_approved_user_can_login_after_approval(admin_client, pending_user, team):
    player = Player.objects.create(name="Approve Me", team=team)
    admin_client.post(
        reverse("accounts:approval", args=[pending_user.pk]),
        {"action": "approve", "player": player.pk},
    )
    client = Client()
    response = _login(client, "newbie@example.com")
    assert response.status_code == 302
    assert "_auth_user_id" in client.session


# ---------------------------------------------------------------------------
# Self-registration (django-allauth) -> pending + admin notification
# ---------------------------------------------------------------------------
def test_signup_creates_pending_user_and_notifies_admins(db, admin_user, settings):
    settings.SUPPORT_EMAIL = "support@example.org"
    client = Client()
    response = client.post(
        reverse("account_signup"),
        {
            "email": "fresh@example.com",
            "password1": "Strong-Pass-123!",
            "password2": "Strong-Pass-123!",
        },
    )
    assert response.status_code in (200, 302)

    user = User.objects.get(email="fresh@example.com")
    assert user.approval_status == ApprovalStatus.PENDING  # NOT active for login

    # Admin notification e-mail (support in To, admins in BCC) went out.
    recipients = {addr for m in mail.outbox for addr in [*m.to, *m.bcc]}
    assert "support@example.org" in recipients
    assert "admin@example.com" in recipients
    # Admins are BCC'd — the round mail never exposes their addresses to
    # each other (privacy fix from the code review).
    registration = next(m for m in mail.outbox if "fresh@example.com" in m.subject)
    assert "admin@example.com" in registration.bcc
    assert any("fresh@example.com" in m.subject for m in mail.outbox)
    # django-allauth sent the registration/verification e-mail to the user.
    assert any("fresh@example.com" in m.to for m in mail.outbox)
    from allauth.account.models import EmailAddress

    assert EmailAddress.objects.filter(user=user, verified=False).exists()
    # Registration is audited.
    assert (
        AuditLog.objects.filter(action=AuditAction.USER_REGISTERED, target_id=user.pk).count() == 1
    )

    # ...and the new user still cannot log in.
    assert _login(Client(), "fresh@example.com").status_code == 200


def test_admin_created_user_is_approved_and_verified_immediately(db, role_groups):
    from allauth.account.models import EmailAddress

    user = User.objects.create_user(
        email="direct@example.com", password="pw", approval_status="approved"
    )
    address = EmailAddress.objects.get(user=user)
    assert address.verified is True  # mandatory verification must not block them
    assert _login(Client(), "direct@example.com", "pw").status_code == 302
