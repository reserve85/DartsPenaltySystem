"""Accounts tests — email login, group roles, is_staff derivation, user mgmt, settings."""

import re
from datetime import time as dt_time
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.core.permissions import GROUP_ADMIN, GROUP_CAPTAIN, GROUP_PLAYER

pytestmark = pytest.mark.django_db

User = get_user_model()

MO_DE = Path(__file__).resolve().parent.parent / "locale" / "de" / "LC_MESSAGES" / "django.mo"


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


def test_user_list_shows_linked_player(admin_client, player_user, player):
    """Column 'Verknüpfter Spieler' — player name + detail link, dash if none."""
    content = admin_client.get(reverse("accounts:user_list")).content.decode()
    assert "Verknüpfter Spieler" in content  # header (de is the default language)
    assert player.name not in content  # not linked yet

    player_user.player_link = player
    player_user.save(update_fields=["player_link"])
    content = admin_client.get(reverse("accounts:user_list")).content.decode()
    assert player.name in content
    assert reverse("players:player_detail", args=[player.pk]) in content


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


def test_captain_without_team_is_allowed(admin_client):
    """The team is an OPTIONAL captaincy — the role itself needs no team."""
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
    assert response.status_code == 302
    user = User.objects.get(email="noclub@example.com")
    assert user.is_captain is True
    assert user.team_id is None


def test_admin_can_be_assigned_captain_team(admin_client, team):
    """Role decoupling: an Admin may additionally captain a team."""
    response = admin_client.post(
        reverse("accounts:user_create"),
        {
            "email": "admincap@example.com",
            "role": GROUP_ADMIN,
            "team": team.pk,
            "password1": "Strong-Pass-123!",
            "password2": "Strong-Pass-123!",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 302
    user = User.objects.get(email="admincap@example.com")
    assert user.is_admin is True
    assert user.is_staff is True
    assert user.team_id == team.pk


def test_player_role_rejects_assigned_team(admin_client, team):
    """The team field is a captaincy — the Player role can never hold one."""
    response = admin_client.post(
        reverse("accounts:user_create"),
        {
            "email": "plr@example.com",
            "role": GROUP_PLAYER,
            "team": team.pk,
            "password1": "Strong-Pass-123!",
            "password2": "Strong-Pass-123!",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 200  # form validation error
    assert not User.objects.filter(email="plr@example.com").exists()


def test_update_user_rejects_team_for_player_role(admin_client, player_user, team):
    """Same rule on the edit form: dropping to Player clears/excludes a team."""
    response = admin_client.post(
        reverse("accounts:user_update", args=[player_user.pk]),
        {
            "email": player_user.email,
            "role": GROUP_PLAYER,
            "team": team.pk,
            "is_active": "on",
            "preferred_language": "de",
        },
    )
    assert response.status_code == 200  # form re-rendered with an error
    player_user.refresh_from_db()
    assert player_user.is_player is True
    assert player_user.team_id is None


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


# ---------------------------------------------------------------------------
# Edit user form — "Captain in this team" label + role-gated team dropdown
# ---------------------------------------------------------------------------
def _team_select_tag(content):
    """The rendered <select name="team"> opening tag of the edit form."""
    match = re.search(r"<select[^>]*name=\"team\"[^>]*>", content)
    assert match, "team select not rendered"
    return match.group(0)


def test_edit_form_renames_team_to_captaincy(admin_client, player_user):
    """'Benutzer bearbeiten': the Team field is labelled 'Captain in this team'."""
    from django.utils import translation

    response = admin_client.get(reverse("accounts:user_update", args=[player_user.pk]))
    assert response.status_code == 200
    with translation.override("en"):
        label = str(response.context["form"].fields["team"].label)
    assert label == "Captain in this team"


def test_edit_form_captaincy_label_is_translated(admin_client, player_user):
    """The German catalog renders the requested 'Kapitän in diesem Team'."""
    from django.utils import translation

    if not MO_DE.exists():
        pytest.skip("compiled German catalog missing (run: python manage.py compilemessages)")
    response = admin_client.get(reverse("accounts:user_update", args=[player_user.pk]))
    with translation.override("de"):
        label = str(response.context["form"].fields["team"].label)
    assert label == "Kapitän in diesem Team"


def test_edit_form_team_dropdown_disabled_for_player(admin_client, player_user):
    """Only Captain/Admin accounts may pick a team — the Player role is locked."""
    response = admin_client.get(reverse("accounts:user_update", args=[player_user.pk]))
    assert response.status_code == 200
    attrs = response.context["form"].fields["team"].widget.attrs
    assert attrs["data-team-roles"] == "Admin,Captain"
    assert attrs.get("disabled") is True
    assert "disabled" in _team_select_tag(response.content.decode())


def test_edit_form_team_dropdown_enabled_for_captain_and_admin(
    admin_client, captain_user, admin_user
):
    for user in (captain_user, admin_user):
        response = admin_client.get(reverse("accounts:user_update", args=[user.pk]))
        assert response.status_code == 200
        attrs = response.context["form"].fields["team"].widget.attrs
        assert attrs["data-team-roles"] == "Admin,Captain"
        assert "disabled" not in attrs
        assert "disabled" not in _team_select_tag(response.content.decode())


def test_create_form_team_dropdown_unaffected(admin_client):
    """The rename/lockdown is scoped to the EDIT form — create stays untouched."""
    response = admin_client.get(reverse("accounts:user_create"))
    assert response.status_code == 200
    attrs = response.context["form"].fields["team"].widget.attrs
    assert "disabled" not in attrs
    assert "data-team-roles" not in attrs


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
        reverse("accounts:settings"),
        {
            "preferred_language": "en",
            "preferred_theme": "dark",
            "penalty_notify_mode": "immediate",
            "penalty_notify_time": "08:00",
            "repayment_notify": "False",
            "club_news_optin": "False",
        },
    )
    assert response.status_code == 302
    captain_user.refresh_from_db()
    assert captain_user.preferred_language == "en"
    assert captain_user.preferred_theme == "dark"
    assert captain_user.penalty_notify_mode == "immediate"
    assert captain_user.repayment_notify is False
    assert captain_user.club_news_optin is False


def test_settings_notification_defaults_and_digest_time(captain_client, captain_user):
    """New accounts default to the 08:00 digest; a digest needs a time."""
    captain_user.penalty_notify_mode = "daily"
    captain_user.penalty_notify_time = dt_time(8, 0)
    captain_user.save(update_fields=["penalty_notify_mode", "penalty_notify_time"])

    # Digest without a time is rejected instead of silently guessed …
    response = captain_client.post(
        reverse("accounts:settings"),
        {
            "preferred_language": "de",
            "preferred_theme": "auto",
            "penalty_notify_mode": "daily",
            "penalty_notify_time": "",
            "repayment_notify": "True",
            "club_news_optin": "True",
        },
    )
    assert response.status_code == 200
    assert response.context["form"].errors["penalty_notify_time"]

    # … and the digest time is stored when it is supplied.
    response = captain_client.post(
        reverse("accounts:settings"),
        {
            "preferred_language": "de",
            "preferred_theme": "auto",
            "penalty_notify_mode": "daily",
            "penalty_notify_time": "06:30",
            "repayment_notify": "True",
            "club_news_optin": "True",
        },
    )
    assert response.status_code == 302
    captain_user.refresh_from_db()
    assert captain_user.penalty_notify_mode == "daily"
    assert captain_user.penalty_notify_time == dt_time(6, 30)
    assert captain_user.repayment_notify is True
    assert captain_user.club_news_optin is True


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
