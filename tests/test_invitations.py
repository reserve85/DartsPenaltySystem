"""Invitation system — admin lifecycle, acceptance, obsolescence, notifications.

Covers the full feature: create (pre-assignment + invite mail + audit),
availability of the reserved player for every other registration form,
acceptance (password + payload + verified e-mail + admin round mail/bell,
NO approval step, no registration-flow e-mails), cancel (hard delete),
resend (new token + timer + defensive player check), the obsolete rule
(linking a player deletes competing requests), login gate for ``requested``,
Django-admin integration, e-mail failure UX, copy-link/badges and nav.

Style: language-tolerant text assertions (new strings fall back to English
until CI compiles fresh ``.mo`` files from the committed ``.po``).
"""

from datetime import timedelta
from unittest import mock

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from notifications.models import Notification

from app.accounts.models import ApprovalStatus, Invitation
from app.accounts.services import create_invitation, resend_invitation
from app.core.models import AuditAction, AuditLog
from app.core.permissions import GROUP_PLAYER

pytestmark = pytest.mark.django_db

User = get_user_model()

PW = "Strong-Pass-123!"
INVITEE = "invitee@example.com"

APPROVAL_VERBS = ("New approval request", "Neue Freigabe-Anfrage")
COMPLETED_VERBS = (
    "Invited user completed registration",
    "Eingeladener Nutzer hat die Registrierung abgeschlossen",
)


def _invite(*, email=INVITEE, role=GROUP_PLAYER, team=None, player=None, invited_by=None):
    """Create an invitation directly (service level — no e-mail sent)."""
    return create_invitation(
        email=email,
        role=role,
        team=team,
        player=player,
        language="de",
        invited_by=invited_by,
    )


def _accept_url(invitation):
    return reverse("accounts:invite_accept", args=[invitation.token])


def _create_via_view(client, *, email=INVITEE, role=GROUP_PLAYER, team=None, player=None):
    """POST the real admin form (also sends the invitation e-mail)."""
    return client.post(
        reverse("accounts:invite_create"),
        {
            "email": email,
            "role": role,
            "team": team.pk if team else "",
            "player": player.pk if player else "",
            "preferred_language": "de",
        },
    )


def _accept(client, invitation, password=PW):
    return client.post(
        _accept_url(invitation),
        {"password1": password, "password2": password},
    )


# ---------------------------------------------------------------------------
# 1) Permissions
# ---------------------------------------------------------------------------
def test_list_and_create_permissions(admin_client, captain_client, client):
    assert admin_client.get(reverse("accounts:invite_list")).status_code == 200
    assert admin_client.get(reverse("accounts:invite_create")).status_code == 200
    assert captain_client.get(reverse("accounts:invite_list")).status_code == 403
    assert captain_client.get(reverse("accounts:invite_create")).status_code == 403
    assert client.get(reverse("accounts:invite_list")).status_code == 302
    assert client.get(reverse("accounts:invite_create")).status_code == 302


def test_resend_and_cancel_are_post_only_and_admin_only(admin_client, captain_client, client):
    invitation = _invite()
    resend_url = reverse("accounts:invite_resend", args=[invitation.pk])
    cancel_url = reverse("accounts:invite_cancel", args=[invitation.pk])
    # GET is not an action -> 405 (POST-only views)
    assert admin_client.get(resend_url).status_code == 405
    assert admin_client.get(cancel_url).status_code == 405
    # non-admins are locked out before anything happens
    assert captain_client.post(resend_url).status_code == 403
    assert captain_client.post(cancel_url).status_code == 403
    assert client.post(resend_url).status_code == 302
    assert client.post(cancel_url).status_code == 302
    assert Invitation.objects.filter(pk=invitation.pk).exists()  # untouched


def test_accept_view_is_public(admin_client, admin_user):
    invitation = _invite(invited_by=admin_user)
    assert Client().get(_accept_url(invitation)).status_code == 200
    # authenticated visitors are sent to the application instead
    response = admin_client.get(_accept_url(invitation))
    assert response.status_code == 302
    assert response.url == reverse("dashboard:index")


def test_unknown_token_is_a_plain_404(client):
    assert Client().get(reverse("accounts:invite_accept", args=["nope-nope"])).status_code == 404


# ---------------------------------------------------------------------------
# 2) Create
# ---------------------------------------------------------------------------
def test_create_via_form_builds_requested_user_and_sends_mail(
    admin_client, admin_user, team, player
):
    response = _create_via_view(
        admin_client, email="New@Example.com", role="Captain", team=team, player=player
    )
    assert response.status_code == 302
    assert response.url == reverse("accounts:invite_list")

    user = User.objects.get(email="new@example.com")  # normalized lowercase
    assert user.approval_status == ApprovalStatus.REQUESTED
    assert not user.has_usable_password()  # cannot log in before acceptance
    assert user.preferred_language == "de"

    invitation = Invitation.objects.get(user=user)
    assert len(invitation.token) >= 40  # secrets.token_urlsafe(32)
    assert invitation.invited_by == admin_user
    assert invitation.role == "Captain"
    assert invitation.team == team
    assert invitation.player == player
    expected = timezone.now() + timedelta(days=settings.INVITE_EXPIRE_DAYS)
    assert abs(invitation.expires_at - expected) < timedelta(minutes=1)

    # invite mail: absolute accept link + expiry
    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    accept_url = f"http://testserver{_accept_url(invitation)}"
    assert accept_url in body
    assert timezone.localtime(invitation.expires_at).strftime("%d.%m.%Y %H:%M") in body

    # audit: invitation + e-mail
    assert AuditLog.objects.filter(action=AuditAction.USER_INVITE_SENT, target_id=user.pk).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.EMAIL_SENT, metadata__template="emails/invitation"
    ).exists()


def test_create_rejects_duplicate_email_registered_or_requested(admin_client):
    User.objects.create_user(email="taken@example.com", password=PW, approval_status="approved")
    response = _create_via_view(admin_client, email="taken@example.com")
    assert response.status_code == 200  # re-rendered with a field error
    assert "email" in response.context["form"].errors

    _invite()  # requested INVITEE
    response = _create_via_view(admin_client, email=INVITEE)
    assert response.status_code == 200
    assert "email" in response.context["form"].errors
    assert User.objects.filter(email=INVITEE).count() == 1  # no second row


def test_create_rejects_team_for_player_role(admin_client, team):
    response = _create_via_view(admin_client, role=GROUP_PLAYER, team=team)
    assert response.status_code == 200
    assert "team" in response.context["form"].errors
    assert not User.objects.filter(email=INVITEE).exists()


def test_reserved_player_is_not_offered_and_crafted_post_is_rejected(
    admin_client, admin_user, player
):
    _invite(player=player, invited_by=admin_user)
    # not offered in the form …
    page = admin_client.get(reverse("accounts:invite_create"))
    assert player not in page.context["form"].fields["player"].queryset
    # … and rejected server-side even when the POST is crafted
    response = _create_via_view(admin_client, email="second@example.com", player=player)
    assert response.status_code == 200
    assert "player" in response.context["form"].errors
    assert not User.objects.filter(email="second@example.com").exists()


# ---------------------------------------------------------------------------
# 3) Availability (core spec): the reserved player stays free everywhere
# ---------------------------------------------------------------------------
def test_preassigned_player_still_offered_by_user_create_and_approval(
    admin_client, pending_user, player
):
    _invite(player=player)
    create_page = admin_client.get(reverse("accounts:user_create"))
    assert player in create_page.context["form"].fields["player_link"].queryset

    approval_page = admin_client.get(reverse("accounts:approval", args=[pending_user.pk]))
    assert player in approval_page.context["form"].fields["player"].queryset


# ---------------------------------------------------------------------------
# 12) Copy link / badges
# ---------------------------------------------------------------------------
def test_copy_link_for_open_rows_hidden_for_expired(admin_client, admin_user):
    invitation = _invite(invited_by=admin_user)
    accept_path = _accept_url(invitation)
    content = admin_client.get(reverse("accounts:invite_list")).content.decode()
    assert accept_path in content  # link incl. token for open rows

    Invitation.objects.filter(pk=invitation.pk).update(
        expires_at=timezone.now() - timedelta(days=1)
    )
    content = admin_client.get(reverse("accounts:invite_list")).content.decode()
    assert accept_path not in content  # expired: no dead link
    assert "Resend first to get a new link." in content or "Zuerst erneut senden" in content


def test_expiring_soon_and_accepted_badges(admin_client, admin_user, role_groups):
    invitation = _invite(invited_by=admin_user)
    Invitation.objects.filter(pk=invitation.pk).update(
        expires_at=timezone.now() + timedelta(days=2)
    )
    content = admin_client.get(reverse("accounts:invite_list")).content.decode()
    assert "Expires soon" in content or "Läuft bald ab" in content

    _accept(Client(), invitation)  # -> accepted
    content = admin_client.get(reverse("accounts:invite_list")).content.decode()
    assert "Accepted" in content or "Angenommen" in content
    invitation.refresh_from_db()
    assert invitation.status == "accepted"


# ---------------------------------------------------------------------------
# 4) Accept
# ---------------------------------------------------------------------------
def test_accept_renders_email_and_form(admin_client, admin_user, player):
    invitation = _invite(player=player, invited_by=admin_user)
    page = Client().get(_accept_url(invitation))
    assert page.status_code == 200
    text = page.content.decode()
    assert INVITEE in text  # shows the address as plain text
    assert "password1" in text  # the set-password form is present


def test_accept_happy_path_applies_payload_and_notifies_admins(
    admin_client, admin_user, role_groups, team, player
):
    invitation = _invite(role="Captain", team=team, player=player, invited_by=admin_user)
    url = _accept_url(invitation)  # captured BEFORE the token is blanked
    assert Notification.objects.count() == 0  # creating an invite raises no bell

    response = Client().post(url, {"password1": PW, "password2": PW})
    assert response.status_code == 302
    assert response.url == reverse("account_login")  # no auto-login

    invitation.refresh_from_db()
    user = invitation.user
    user.refresh_from_db()
    assert user.approval_status == ApprovalStatus.APPROVED
    assert user.check_password(PW)
    assert user.player_link == player  # pre-assignment applied
    assert user.team == team
    assert user.groups.filter(name="Captain").exists()
    assert invitation.accepted_at is not None
    assert invitation.token == ""  # single use
    # acceptance vouches for the address (mandatory verification would block login)
    assert EmailAddress.objects.filter(user=user, verified=True).exists()
    # audit
    assert AuditLog.objects.filter(
        action=AuditAction.USER_INVITE_ACCEPTED, target_id=user.pk
    ).exists()

    # second POST with the (now blank) token -> plain 404
    assert Client().post(url, {"password1": PW, "password2": PW}).status_code == 404

    # the invitee can log in right away
    login_client = Client()
    response = login_client.post(reverse("account_login"), {"login": user.email, "password": PW})
    assert response.status_code == 302
    assert "_auth_user_id" in login_client.session

    # admin round mail: SUPPORT To + admins BCC
    completed = [
        m
        for m in mail.outbox
        if "completed registration" in m.subject or "Registrierung abgeschlossen" in m.subject
    ]
    assert len(completed) == 1
    assert settings.SUPPORT_EMAIL in completed[0].to
    assert admin_user.email in completed[0].bcc
    assert AuditLog.objects.filter(
        action=AuditAction.EMAIL_SENT, metadata__template="emails/invitation_completed"
    ).exists()

    # in-app bell for the admin
    notification = Notification.objects.get(recipient=admin_user)
    assert notification.unread is True
    assert notification.level == "success"
    assert notification.target == user
    assert notification.verb in COMPLETED_VERBS


def test_accept_expired_token_renders_error_without_form(client, admin_user):
    invitation = _invite(invited_by=admin_user)
    Invitation.objects.filter(pk=invitation.pk).update(
        expires_at=timezone.now() - timedelta(hours=1)
    )
    page = Client().get(_accept_url(invitation))
    assert page.status_code == 200
    text = page.content.decode()
    assert "expired" in text.lower() or "abgelaufen" in text.lower()
    assert "password1" not in text  # NO form on a dead link

    # a crafted POST cannot activate it either
    response = _accept(Client(), invitation)
    assert response.status_code == 200
    invitation.refresh_from_db()
    invitation.user.refresh_from_db()
    assert invitation.accepted_at is None
    assert invitation.user.approval_status == ApprovalStatus.REQUESTED


def test_accept_race_deleted_invitation_is_404(client):
    invitation = _invite()
    url = _accept_url(invitation)  # captured while the row existed
    Invitation.objects.filter(pk=invitation.pk).delete()  # meanwhile (e.g. obsolete)
    assert Client().get(url).status_code == 404
    assert Client().post(url, {"password1": PW, "password2": PW}).status_code == 404


# ---------------------------------------------------------------------------
# 10) Non-triggers: no registration-flow e-mails, no approval-request bell
# ---------------------------------------------------------------------------
def test_acceptance_fires_only_the_invitation_flow(admin_client, admin_user, role_groups, player):
    assert _create_via_view(admin_client, player=player).status_code == 302
    invitation = Invitation.objects.get(user__email=INVITEE)

    _accept(Client(), invitation)

    templates = set(
        AuditLog.objects.filter(action=AuditAction.EMAIL_SENT).values_list(
            "metadata__template", flat=True
        )
    )
    assert templates == {"emails/invitation", "emails/invitation_completed"}
    # no "waiting for approval" round mail and no approval step at all
    assert not AuditLog.objects.filter(action=AuditAction.USER_APPROVED).exists()
    # no "new approval request" bell — only the completed bell
    assert not Notification.objects.filter(verb__in=APPROVAL_VERBS).exists()
    assert Notification.objects.filter(verb__in=COMPLETED_VERBS).exists()


# ---------------------------------------------------------------------------
# 5) Cancel
# ---------------------------------------------------------------------------
def test_cancel_deletes_user_frees_player_and_allows_later_signup(
    admin_client, admin_user, player, role_groups
):
    invitation = _invite(player=player, invited_by=admin_user)
    response = admin_client.post(reverse("accounts:invite_cancel", args=[invitation.pk]))
    assert response.status_code == 302

    assert not User.objects.filter(email=INVITEE).exists()  # hard delete
    assert not Invitation.objects.filter(pk=invitation.pk).exists()  # cascade
    assert User.objects.filter(player_link=player).exists() is False  # player free again
    assert AuditLog.objects.filter(action=AuditAction.USER_INVITE_CANCELLED).exists()

    # the address can self-register again -> normal pending approval flow
    cache.clear()  # isolate from allauth confirmation cooldowns
    response = Client().post(
        reverse("account_signup"),
        {"email": INVITEE, "password1": PW, "password2": PW},
    )
    assert response.status_code in (200, 302)
    user = User.objects.get(email=INVITEE)
    assert user.approval_status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# 6) Obsolete rule
# ---------------------------------------------------------------------------
def test_obsolete_on_approval_deletes_competing_request(admin_client, pending_user, player):
    invitation = _invite(player=player)
    response = admin_client.post(
        reverse("accounts:approval", args=[pending_user.pk]),
        {"action": "approve", "player": player.pk},
    )
    assert response.status_code == 302  # approval itself still succeeds

    # the requested user + invitation are gone
    assert not User.objects.filter(email=INVITEE).exists()
    assert not Invitation.objects.filter(pk=invitation.pk).exists()
    entry = AuditLog.objects.get(action=AuditAction.USER_INVITE_OBSOLETED)
    assert entry.metadata["email"] == INVITEE
    # the approved user got the player as intended (no IntegrityError)
    pending_user.refresh_from_db()
    assert pending_user.approval_status == ApprovalStatus.APPROVED
    assert pending_user.player_link == player


def test_obsolete_on_user_create(admin_client, player, role_groups):
    invitation = _invite(player=player)
    response = admin_client.post(
        reverse("accounts:user_create"),
        {
            "email": "direct@example.com",
            "role": GROUP_PLAYER,
            "team": "",
            "player_link": player.pk,
            "preferred_language": "de",
            "password1": PW,
            "password2": PW,
        },
    )
    assert response.status_code == 302
    assert not User.objects.filter(email=INVITEE).exists()
    assert not Invitation.objects.filter(pk=invitation.pk).exists()
    assert AuditLog.objects.filter(action=AuditAction.USER_INVITE_OBSOLETED).exists()
    assert User.objects.get(email="direct@example.com").player_link == player


# ---------------------------------------------------------------------------
# 7) Resend + 13) defensive check
# ---------------------------------------------------------------------------
def test_resend_rotates_token_resets_timer_and_resends_mail(admin_client, admin_user):
    invitation = _invite(invited_by=admin_user)
    old_token = invitation.token
    old_expiry = invitation.expires_at

    response = admin_client.post(reverse("accounts:invite_resend", args=[invitation.pk]))
    assert response.status_code == 302

    invitation.refresh_from_db()
    assert invitation.token != old_token
    assert invitation.expires_at > old_expiry
    assert AuditLog.objects.filter(action=AuditAction.USER_INVITE_RESENT).exists()

    # the OLD link dies immediately, the new one works
    assert Client().get(reverse("accounts:invite_accept", args=[old_token])).status_code == 404
    assert Client().get(_accept_url(invitation)).status_code == 200


def test_resend_defensive_check_when_player_linked_by_bypass(admin_client, admin_user, player):
    invitation = _invite(player=player, invited_by=admin_user)
    other = User.objects.create_user(
        email="other@example.com", password=PW, approval_status="approved"
    )
    # bulk update() bypasses pre_save -> the obsolete hook never ran
    User.objects.filter(pk=other.pk).update(player_link=player)

    # service level: clear validation error instead of an unkeepable promise
    invitation.refresh_from_db()
    old_token = invitation.token
    with pytest.raises(ValidationError) as exc:
        resend_invitation(invitation, actor=admin_user)
    # en / de catalog — mirrors the view-level assertion below.
    message = str(exc.value).lower()
    assert "cancel this invitation" in message or "storniere diese einladung" in message

    # view level: error message, no new token, no second mail
    response = admin_client.post(reverse("accounts:invite_resend", args=[invitation.pk]))
    assert response.status_code == 302
    invitation.refresh_from_db()
    assert invitation.token == old_token
    assert len(mail.outbox) == 0
    followed = admin_client.get(response.url)
    text = followed.content.decode()
    assert "meanwhile been linked" in text or "inzwischen" in text


# ---------------------------------------------------------------------------
# 8) Login gate
# ---------------------------------------------------------------------------
def test_invited_user_cannot_login_and_gets_invitation_message(admin_user):
    invitation = _invite(invited_by=admin_user)
    user = invitation.user
    # the real account holds an UNUSABLE password; give it a matching one so
    # the credentials check passes and the approval gate message is reached
    user.set_password(PW)
    user.save()

    response = Client().post(reverse("account_login"), {"login": user.email, "password": PW})
    assert response.status_code == 200
    text = response.content.decode()
    assert "invitation e-mail" in text or "Einladungs-E-Mail" in text


def test_stale_requested_session_is_kicked_with_invitation_message(admin_user):
    invitation = _invite(invited_by=admin_user)
    client = Client()
    client.force_login(invitation.user)  # unusable password, session forced
    response = client.get(reverse("dashboard:index"))
    assert response.status_code == 302
    assert response.url == reverse("account_login")
    assert "_auth_user_id" not in client.session
    follow = client.get(response.url)
    text = follow.content.decode()
    assert "invitation e-mail" in text or "Einladungs-E-Mail" in text


# ---------------------------------------------------------------------------
# Requested users are not editable (fix with Cancel)
# ---------------------------------------------------------------------------
def test_requested_user_cannot_be_edited_or_deactivated(admin_client):
    invitation = _invite()
    user = invitation.user

    response = admin_client.get(reverse("accounts:user_update", args=[user.pk]))
    assert response.status_code == 302
    assert response.url == reverse("accounts:invite_list")

    response = admin_client.post(reverse("accounts:user_deactivate", args=[user.pk]), {})
    assert response.status_code == 302
    assert response.url == reverse("accounts:invite_list")
    user.refresh_from_db()
    assert user.is_active  # untouched
    assert user.approval_status == ApprovalStatus.REQUESTED


# ---------------------------------------------------------------------------
# 9) Django admin integration
# ---------------------------------------------------------------------------
def _django_admin_client(db, role_groups):
    """Superuser client — the regular admin_user has no Django-admin perms."""
    superuser = User.objects.create_superuser(email="root@example.com", password=PW)
    client = Client()
    client.force_login(superuser)
    return client


def test_admin_bulk_actions_skip_requested_users(db, role_groups):
    django_admin = _django_admin_client(db, role_groups)
    invitation = _invite()
    pending = User.objects.create_user(email="pending@example.com", password=PW)

    response = django_admin.post(
        reverse("admin:accounts_user_changelist"),
        {
            "action": "approve_users",
            "_selected_action": [str(invitation.user_id), str(pending.pk)],
        },
    )
    assert response.status_code == 302
    invitation.user.refresh_from_db()
    pending.refresh_from_db()
    assert invitation.user.approval_status == ApprovalStatus.REQUESTED  # skipped
    assert pending.approval_status == ApprovalStatus.APPROVED  # pending handled

    response = django_admin.post(
        reverse("admin:accounts_user_changelist"),
        {"action": "reject_users", "_selected_action": [str(invitation.user_id)]},
    )
    assert response.status_code == 302
    invitation.user.refresh_from_db()
    assert invitation.user.approval_status == ApprovalStatus.REQUESTED  # still skipped


def test_invitation_list_renders_in_django_admin(db, role_groups):
    django_admin = _django_admin_client(db, role_groups)
    _invite()
    response = django_admin.get(reverse("admin:accounts_invitation_changelist"))
    assert response.status_code == 200
    assert INVITEE in response.content.decode()


# ---------------------------------------------------------------------------
# 11) E-mail failure UX
# ---------------------------------------------------------------------------
def test_smtp_failure_still_creates_request_and_shows_warning(admin_client):
    with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("boom")):
        response = _create_via_view(admin_client)

    assert response.status_code == 302  # the request was created anyway
    assert User.objects.filter(email=INVITEE, approval_status="requested").exists()
    entry = AuditLog.objects.get(action=AuditAction.EMAIL_FAILED)
    assert entry.metadata["template"] == "emails/invitation"
    assert "OSError" in entry.metadata["error"]
    # the warning points at Resend instead of failing silently
    followed = admin_client.get(response.url)
    text = followed.content.decode()
    assert "could not be sent" in text or "nicht gesendet" in text


# ---------------------------------------------------------------------------
# 14) Nav highlight
# ---------------------------------------------------------------------------
def test_invite_pages_highlight_administration_section(admin_client):
    for name in ("invite_list", "invite_create"):
        response = admin_client.get(reverse(f"accounts:{name}"))
        assert response.context["nav_section"] == "admin", name


# ---------------------------------------------------------------------------
# 15) pending_invitation accessor safety (user list renders mixed rows)
# ---------------------------------------------------------------------------
def test_user_list_renders_mixed_users_without_related_errors(admin_client, pending_user, player):
    invited = _invite(player=player)
    _invite(email="nopayload@example.com")

    response = admin_client.get(reverse("accounts:user_list"))
    assert response.status_code == 200
    content = response.content.decode()
    assert INVITEE in content
    assert pending_user.email in content
    # invited row: pre-assigned player visible, no Edit link, Invited badge
    assert player.name in content
    assert reverse("accounts:user_update", args=[invited.user_id]) not in content
    assert "Invited" in content or "Eingeladen" in content
    # the invitations entry point is on the list header
    assert reverse("accounts:invite_list") in content
