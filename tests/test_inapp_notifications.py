"""In-app notifications (django-notifications-hq) for new approval requests.

Covers: signup -> one in-app notification per admin (never captains/players),
navbar bell badge + notification list with a Review shortcut, approve/reject
marks the request read, mark-all-as-read — and the regression guard for the
two-line ``{# … #}`` comment leaking into the user list table.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from notifications.models import Notification

from app.core.permissions import GROUP_ADMIN

pytestmark = pytest.mark.django_db

User = get_user_model()

VERBS = ("New approval request", "Neue Freigabe-Anfrage")  # language-tolerant


def _signup(client, email="fresh@example.com"):
    """Perform a django-allauth self-registration (creates a pending user)."""
    return client.post(
        reverse("account_signup"),
        {
            "email": email,
            "password1": "Strong-Pass-123!",
            "password2": "Strong-Pass-123!",
        },
    )


# ---------------------------------------------------------------------------
# Creation (signal on user_signed_up)
# ---------------------------------------------------------------------------
def test_signup_notifies_admin_in_app(db, admin_user):
    assert _signup(Client()).status_code in (200, 302)

    pending = User.objects.get(email="fresh@example.com")
    notification = Notification.objects.get(recipient=admin_user)
    assert notification.unread is True
    assert notification.level == "info"
    assert notification.target == pending
    assert notification.verb in VERBS


def test_signup_notifies_every_admin_separately(db, admin_user, role_groups):
    second = User.objects.create_user(
        email="admin2@example.com", password="pw", approval_status="approved"
    )
    second.groups.add(role_groups[GROUP_ADMIN])

    _signup(Client())

    recipients = set(Notification.objects.values_list("recipient", flat=True))
    assert recipients == {admin_user.pk, second.pk}


def test_signup_never_notifies_captains_or_players(db, captain_user, player_user):
    _signup(Client())
    assert not Notification.objects.filter(recipient__in=[captain_user, player_user]).exists()


def test_missing_admin_recipients_never_break_signup(db):
    """No admin at all -> the signup still succeeds (notification rule)."""
    assert _signup(Client()).status_code in (200, 302)
    assert User.objects.filter(email="fresh@example.com").exists()
    assert Notification.objects.count() == 0


# ---------------------------------------------------------------------------
# Display (navbar bell + notification list)
# ---------------------------------------------------------------------------
def test_admin_sees_bell_with_unread_badge(db, admin_client, admin_user):
    _signup(Client())

    content = admin_client.get(reverse("dashboard:index")).content.decode()
    assert reverse("notifications:unread") in content
    assert "🔔" in content
    assert '<span class="badge text-bg-warning">1</span>' in content


def test_every_signed_in_account_sees_the_bell(captain_client, player_client):
    """Read is universal: the bell moved out of the admin block (decision 5)."""
    for client in (captain_client, player_client):
        content = client.get(reverse("dashboard:index")).content.decode()
        assert reverse("notifications:unread") in content
        assert "🔔" in content


def test_anonymous_visitor_never_sees_the_bell(db):
    content = Client().get(reverse("account_login")).content.decode()
    assert reverse("notifications:unread") not in content


def test_notification_list_shows_request_with_review_shortcut(db, admin_client, admin_user):
    _signup(Client())
    pending = User.objects.get(email="fresh@example.com")

    response = admin_client.get(reverse("notifications:unread"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "fresh@example.com" in content  # actor of the notification
    assert reverse("accounts:approval", args=[pending.pk]) in content


def test_notification_list_requires_login(client):
    response = client.get(reverse("notifications:unread"))
    assert response.status_code == 302
    assert str(reverse("account_login")) in response.url


def test_empty_list_renders_friendly_hint(admin_client):
    response = admin_client.get(reverse("notifications:unread"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "No notifications yet." in content or "Noch keine Benachrichtigungen." in content


# ---------------------------------------------------------------------------
# Decisions + mark-as-read
# ---------------------------------------------------------------------------
def test_reject_marks_notifications_read(db, admin_client, admin_user):
    _signup(Client())
    pending = User.objects.get(email="fresh@example.com")

    url = reverse("accounts:approval", args=[pending.pk])
    response = admin_client.post(url, {"action": "reject"})
    assert response.status_code == 302
    # The admins' request rows are read; the REJECTED user's fresh decision
    # row (created after clear_approval_notifications) stays unread for them.
    assert not Notification.objects.filter(recipient=admin_user, unread=True).exists()
    assert Notification.objects.filter(recipient=pending, unread=True).exists()


def test_approve_marks_notifications_read(db, admin_client, admin_user, team):
    from app.players.models import Player

    player = Player.objects.create(name="Approve Me", team=team)
    _signup(Client())
    pending = User.objects.get(email="fresh@example.com")

    url = reverse("accounts:approval", args=[pending.pk])
    response = admin_client.post(url, {"action": "approve", "player": player.pk})
    assert response.status_code == 302
    # Admin rows are read; the approved user's own decision row is unread.
    assert not Notification.objects.filter(recipient=admin_user, unread=True).exists()
    assert Notification.objects.filter(recipient=pending, unread=True).exists()


def test_mark_all_as_read_clears_the_badge(db, admin_client, admin_user):
    _signup(Client())
    assert Notification.objects.filter(recipient=admin_user, unread=True).exists()

    response = admin_client.get(reverse("notifications:mark_all_as_read"))
    assert response.status_code == 302
    assert not Notification.objects.filter(unread=True).exists()


# ---------------------------------------------------------------------------
# Invitations: acceptance raises a SUCCESS bell (no approval-request bell)
# ---------------------------------------------------------------------------
INVITED_VERBS = (
    "Invited user completed registration",
    "Eingeladener Nutzer hat die Registrierung abgeschlossen",
)  # language-tolerant


def _accept_invitation(db, admin_user):
    """Admin sends an invitation, the invitee accepts it (full view flow)."""
    from app.accounts.models import Invitation
    from app.accounts.services import create_invitation

    invitation = create_invitation(
        email="invitee@example.com",
        role="Player",
        team=None,
        player=None,
        language="de",
        invited_by=admin_user,
    )
    assert Notification.objects.count() == 0  # sending an invite raises NO bell
    response = Client().post(
        reverse("accounts:invite_accept", args=[invitation.token]),
        {"password1": "Strong-Pass-123!", "password2": "Strong-Pass-123!"},
    )
    assert response.status_code == 302
    return Invitation.objects.get(pk=invitation.pk)


def test_invited_user_completion_notifies_admin_in_app(db, admin_user):
    invitation = _accept_invitation(db, admin_user)

    notification = Notification.objects.get(recipient=admin_user)
    assert notification.unread is True
    assert notification.level == "success"
    assert notification.target == invitation.user
    assert notification.verb in INVITED_VERBS


def test_invited_completion_never_notifies_captains(db, admin_user, captain_user):
    _accept_invitation(db, admin_user)
    assert not Notification.objects.filter(recipient=captain_user).exists()


# ---------------------------------------------------------------------------
# Regression: the {# … #} comment must never leak into the user table
# ---------------------------------------------------------------------------
def test_user_list_contains_no_raw_template_comment(admin_client, pending_user):
    content = admin_client.get(reverse("accounts:user_list")).content.decode()
    assert "is NOT a button anymore" not in content
    assert "the edit form's" not in content
    assert pending_user.email in content  # the table itself still renders
