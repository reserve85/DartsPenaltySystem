"""Accounts tests — email login, group roles, is_staff derivation, user mgmt, settings."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.core.permissions import GROUP_ADMIN, GROUP_CAPTAIN, GROUP_PLAYER

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------
def test_login_success_and_audit(db):
    user = User.objects.create_user(
        email="audit@example.com", password="pw", approval_status="approved"
    )
    client = Client()
    response = client.post(
        reverse("account_login"), {"login": "audit@example.com", "password": "pw"}
    )
    assert response.status_code == 302
    assert AuditLog.objects.filter(action=AuditAction.LOGIN, user=user).count() == 1


def test_login_failure_no_audit(db):
    user = User.objects.create_user(
        email="fail@example.com", password="pw", approval_status="approved"
    )
    client = Client()
    response = client.post(
        reverse("account_login"), {"login": "fail@example.com", "password": "wrong"}
    )
    assert response.status_code == 200
    assert AuditLog.objects.filter(action=AuditAction.LOGIN, user=user).count() == 0


def test_logout_redirects_and_audits(db, admin_user):
    client = Client()
    client.force_login(admin_user)
    response = client.post(reverse("account_logout"))
    assert response.status_code == 302
    assert AuditLog.objects.filter(action=AuditAction.LOGOUT, user=admin_user).count() == 1


# ---------------------------------------------------------------------------
# Role derivation & is_staff (H1)
# ---------------------------------------------------------------------------
def test_is_staff_derived_on_group_change(role_groups):
    user = User.objects.create_user(email="g@example.com", password="pw")
    assert user.is_staff is False
    user.groups.add(role_groups[GROUP_ADMIN])
    user.refresh_from_db()
    assert user.is_staff is True
    user.groups.remove(role_groups[GROUP_ADMIN])
    user.refresh_from_db()
    assert user.is_staff is False


def test_is_staff_derived_on_save(role_groups):
    user = User.objects.create_user(email="s@example.com", password="pw")
    user.groups.add(role_groups[GROUP_ADMIN])
    user.is_staff = False  # stale value
    user.save()
    user.refresh_from_db()
    assert user.is_staff is True


def test_captain_and_player_staff_false(role_groups):
    captain = User.objects.create_user(email="cap@example.com", password="pw")
    captain.groups.add(role_groups[GROUP_CAPTAIN])
    assert captain.is_staff is False
    player = User.objects.create_user(email="pl@example.com", password="pw")
    player.groups.add(role_groups[GROUP_PLAYER])
    assert player.is_staff is False


def test_role_mapping_and_precedence(role_groups):
    user = User.objects.create_user(email="r@example.com", password="pw")
    user.groups.add(role_groups[GROUP_CAPTAIN])
    user.groups.add(role_groups[GROUP_PLAYER])
    assert user.is_captain is True
    assert user.is_player is True
    assert user.role == "captain"
    user.groups.add(role_groups[GROUP_ADMIN])
    assert user.role == "admin"


# ---------------------------------------------------------------------------
# User management endpoints (Admin only)
# ---------------------------------------------------------------------------
def test_user_list_permissions(admin_client, captain_client, player_client, client):
    assert admin_client.get(reverse("accounts:user_list")).status_code == 200
    assert captain_client.get(reverse("accounts:user_list")).status_code == 403
    assert player_client.get(reverse("accounts:user_list")).status_code == 403
    assert client.get(reverse("accounts:user_list")).status_code == 302


def test_admin_creates_team_admin(admin_client):
    response = admin_client.post(
        reverse("accounts:user_create"),
        {
            "email": "admin2@example.com",
            "role": GROUP_ADMIN,
            "password1": "Strong-Pass-123!",
            "password2": "Strong-Pass-123!",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 302
    user = User.objects.get(email="admin2@example.com")
    assert user.is_staff is True
    assert user.groups.filter(name=GROUP_ADMIN).exists()
    assert AuditLog.objects.filter(action=AuditAction.USER_CREATED, target_id=user.pk).count() == 1


def test_admin_creates_captain_with_team(admin_client, team):
    response = admin_client.post(
        reverse("accounts:user_create"),
        {
            "email": "cap2@example.com",
            "role": GROUP_CAPTAIN,
            "team": team.pk,
            "password1": "Strong-Pass-123!",
            "password2": "Strong-Pass-123!",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 302
    user = User.objects.get(email="cap2@example.com")
    assert user.is_staff is False
    assert user.groups.filter(name=GROUP_CAPTAIN).exists()
    assert user.team_id == team.pk


def test_captain_without_team_fails_validation(admin_client):
    response = admin_client.post(
        reverse("accounts:user_create"),
        {
            "email": "noclub@example.com",
            "role": GROUP_CAPTAIN,
            "password1": "Strong-Pass-123!",
            "password2": "Strong-Pass-123!",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 200
    assert not User.objects.filter(email="noclub@example.com").exists()


def test_update_user_keeps_staff_consistent(admin_client, player_user, team):
    response = admin_client.post(
        reverse("accounts:user_update", args=[player_user.pk]),
        {
            "email": player_user.email,
            "role": GROUP_CAPTAIN,
            "team": team.pk,
            "is_active": "on",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 302
    player_user.refresh_from_db()
    assert player_user.is_staff is False
    assert player_user.is_captain is True
    assert (
        AuditLog.objects.filter(action=AuditAction.USER_UPDATED, target_id=player_user.pk).count()
        == 1
    )


def test_admin_deactivates_user(admin_client, player_user):
    response = admin_client.post(reverse("accounts:user_deactivate", args=[player_user.pk]))
    assert response.status_code == 302
    player_user.refresh_from_db()
    assert player_user.is_active is False
    assert (
        AuditLog.objects.filter(
            action=AuditAction.USER_DEACTIVATED, target_id=player_user.pk
        ).count()
        == 1
    )


def test_admin_cannot_deactivate_self(admin_client, admin_user):
    response = admin_client.post(reverse("accounts:user_deactivate", args=[admin_user.pk]))
    assert response.status_code == 302
    admin_user.refresh_from_db()
    assert admin_user.is_active is True


# ---------------------------------------------------------------------------
# Lockout protection (self-demotion / self-deactivation)
# ---------------------------------------------------------------------------
def test_last_admin_cannot_demote_self(admin_client, admin_user, team):
    response = admin_client.post(
        reverse("accounts:user_update", args=[admin_user.pk]),
        {
            "email": admin_user.email,
            "role": GROUP_CAPTAIN,
            "team": team.pk,
            "is_active": "on",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 200  # form re-rendered with an error
    admin_user.refresh_from_db()
    assert admin_user.is_admin is True
    assert not admin_user.groups.filter(name=GROUP_CAPTAIN).exists()


def test_admin_demotes_self_when_another_admin_exists(admin_client, admin_user, role_groups, team):
    other = User.objects.create_user(email="admin2@example.com", password="pw")
    other.groups.add(role_groups[GROUP_ADMIN])

    response = admin_client.post(
        reverse("accounts:user_update", args=[admin_user.pk]),
        {
            "email": admin_user.email,
            "role": GROUP_CAPTAIN,
            "team": team.pk,
            "is_active": "on",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 302
    admin_user.refresh_from_db()
    assert admin_user.is_admin is False
    assert admin_user.is_captain is True


def test_admin_cannot_deactivate_self_via_edit_form(admin_client, admin_user):
    response = admin_client.post(
        reverse("accounts:user_update", args=[admin_user.pk]),
        {
            "email": admin_user.email,
            "role": GROUP_ADMIN,
            "is_active": "",  # unchecked
            "preferred_language": "de",
        },
    )
    assert response.status_code == 200
    admin_user.refresh_from_db()
    assert admin_user.is_active is True


def test_superuser_is_always_admin_and_unlockable(db):
    """The env-seeded system/hosting account keeps Admin rights without any group."""
    from app.core.permissions import user_is_admin

    root = User.objects.create_superuser(email="root@example.com", password="pw")
    root.groups.clear()

    assert user_is_admin(root) is True
    assert root.is_admin is True
    assert root.role == "admin"

    client = Client()
    client.force_login(root)
    assert client.get(reverse("accounts:user_list")).status_code == 200


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_settings_preferences_persist(captain_client, captain_user):
    response = captain_client.post(
        reverse("accounts:settings"), {"preferred_language": "en", "preferred_theme": "dark"}
    )
    assert response.status_code == 302
    captain_user.refresh_from_db()
    assert captain_user.preferred_language == "en"
    assert captain_user.preferred_theme == "dark"


def test_settings_password_change(captain_client, captain_user):
    """Password change is served by django-allauth (account_change_password)."""
    response = captain_client.post(
        reverse("account_change_password"),
        {
            "oldpassword": "pw",
            "password1": "New-Strong-Pass-123!",
            "password2": "New-Strong-Pass-123!",
        },
    )
    assert response.status_code == 302
    captain_user.refresh_from_db()
    assert captain_user.check_password("New-Strong-Pass-123!")


def test_theme_endpoint(captain_client, captain_user):
    response = captain_client.post(reverse("accounts:settings_theme"), {"theme": "dark"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "theme": "dark"}
    captain_user.refresh_from_db()
    assert captain_user.preferred_theme == "dark"


def test_theme_endpoint_rejects_invalid(captain_client):
    response = captain_client.post(reverse("accounts:settings_theme"), {"theme": "neon"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# player_link — the dropdown only offers NOT-yet-linked players (1:1 link)
# ---------------------------------------------------------------------------
def test_user_create_form_only_offers_unlinked_players(admin_client):
    from app.players.models import Player

    free = Player.objects.create(name="Free")
    linked = Player.objects.create(name="Linked")
    taken_by = User.objects.create_user(email="t@example.com", password="pw")
    taken_by.player_link = linked
    taken_by.save()

    response = admin_client.get(reverse("accounts:user_create"))
    assert response.status_code == 200
    choices = list(response.context["form"].fields["player_link"].queryset)
    assert free in choices
    assert linked not in choices  # already linked to another user


def test_user_update_form_keeps_own_link_but_hides_others(admin_client):
    from app.players.models import Player

    mine = Player.objects.create(name="Mine")
    free = Player.objects.create(name="Free")
    other_linked = Player.objects.create(name="OtherLinked")
    user = User.objects.create_user(email="u@example.com", password="pw")
    user.player_link = mine
    user.save()
    other = User.objects.create_user(email="o@example.com", password="pw")
    other.player_link = other_linked
    other.save()

    response = admin_client.get(reverse("accounts:user_update", args=[user.pk]))
    assert response.status_code == 200
    choices = list(response.context["form"].fields["player_link"].queryset)
    assert mine in choices  # own current link stays selectable
    assert free in choices
    assert other_linked not in choices  # belongs to another user


def test_user_create_rejects_already_linked_player(admin_client):
    """Server-side: a linked player cannot be assigned to a second user."""
    from app.players.models import Player

    linked = Player.objects.create(name="Linked")
    taken_by = User.objects.create_user(email="t2@example.com", password="pw")
    taken_by.player_link = linked
    taken_by.save()

    response = admin_client.post(
        reverse("accounts:user_create"),
        {
            "email": "second@example.com",
            "role": GROUP_PLAYER,
            "player_link": linked.pk,
            "password1": "New-Strong-Pass-123!",
            "password2": "New-Strong-Pass-123!",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 200  # form validation error
    assert not User.objects.filter(email="second@example.com").exists()
