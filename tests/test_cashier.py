"""Kasse — cashier assignment: model, services, views, UI (strict payment right)."""

import re
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.players.models import Player
from app.teams.models import TeamCashier
from app.teams.services import (
    cashier_label_for,
    cashier_user_for,
    clear_cashier,
    set_cashier,
    user_is_cashier,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------------------------------------------------------------------------
# set_cashier / clear_cashier — history + constraint + audit
# ---------------------------------------------------------------------------
def test_set_cashier_closes_previous_row_and_opens_one(team, admin_user, captain_user):
    first = set_cashier(team=team, user=admin_user, actor=admin_user)
    second = set_cashier(team=team, user=captain_user, actor=admin_user)

    first.refresh_from_db()
    assert first.valid_to is not None  # closed, history preserved
    assert second.valid_to is None  # the new open row
    assert TeamCashier.objects.filter(team=team, valid_to__isnull=True).count() == 1


def test_partial_unique_constraint_rejects_second_open_row(team, admin_user, captain_user):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    with pytest.raises(IntegrityError), transaction.atomic():
        TeamCashier.objects.create(team=team, user=captain_user, user_label="X")


def test_set_cashier_rejects_pending_user(team, pending_user, admin_user):
    with pytest.raises(ValidationError):
        set_cashier(team=team, user=pending_user, actor=admin_user)
    assert TeamCashier.objects.filter(team=team).count() == 0


def test_set_cashier_rejects_inactive_user(team, admin_user):
    admin_user.is_active = False
    admin_user.save(update_fields=["is_active"])
    with pytest.raises(ValidationError):
        set_cashier(team=team, user=admin_user, actor=admin_user)
    assert TeamCashier.objects.filter(team=team).count() == 0


def test_cashier_user_for_never_assigned(team):
    assert cashier_user_for(team) is None
    assert cashier_user_for(None) is None


def test_cashier_user_for_after_clear(team, admin_user):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    clear_cashier(team=team, actor=admin_user)
    assert cashier_user_for(team) is None
    assert TeamCashier.objects.filter(team=team, valid_to__isnull=True).count() == 0


def test_cashier_user_for_deactivated_account(team, admin_user):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    admin_user.is_active = False
    admin_user.save(update_fields=["is_active"])
    assert cashier_user_for(team) is None  # open row, but the account is dead


def test_cashier_user_for_deleted_account_keeps_snapshot(team, admin_user):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    User.objects.filter(pk=admin_user.pk).delete()

    assert cashier_user_for(team) is None  # SET_NULL: no usable cashier
    row = TeamCashier.objects.get(team=team)
    assert row.user_id is None
    assert row.user_label  # the snapshot survives the account deletion


def test_clear_cashier_is_a_noop_without_open_row(team):
    clear_cashier(team=team, actor=None)
    assert AuditLog.objects.filter(action=AuditAction.CASHIER_CLEARED).count() == 0


def test_set_and_clear_cashier_write_audit_entries(team, admin_user, captain_user):
    row = set_cashier(team=team, user=admin_user, actor=captain_user)
    entry = AuditLog.objects.get(action=AuditAction.CASHIER_SET, target_id=row.pk)
    assert entry.user == captain_user
    assert entry.metadata["team_id"] == team.pk
    assert entry.metadata["user_id"] == admin_user.pk
    assert entry.metadata["user_email"] == admin_user.email

    clear_cashier(team=team, actor=captain_user)
    row.refresh_from_db()
    cleared = AuditLog.objects.get(action=AuditAction.CASHIER_CLEARED, target_id=row.pk)
    assert cleared.user == captain_user
    assert cleared.metadata["team_id"] == team.pk


def test_set_cashier_race_surfaces_validation_error_not_integrity_error(
    team, admin_user, captain_user, monkeypatch
):
    """Review M5: two admins saving at once -> a message, never a 500."""
    set_cashier(team=team, user=admin_user, actor=admin_user)  # open row exists

    # Simulate the race: the closing UPDATE misses the row another admin just
    # wrote, so the INSERT hits the partial unique constraint.
    noop = mock.Mock(update=mock.Mock(return_value=0))
    monkeypatch.setattr(TeamCashier.objects, "filter", lambda *args, **kwargs: noop)

    with pytest.raises(ValidationError) as exc:
        set_cashier(team=team, user=captain_user, actor=admin_user)
    assert "reload" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# user_is_cashier — the strict money-write rule (review L1: lives here)
# ---------------------------------------------------------------------------
def test_user_is_cashier_matrix(admin_user, captain_user, other_captain_user, player_user, team):
    # Nobody before any assignment — an admin is NOT implicitly the cashier.
    assert user_is_cashier(admin_user, team) is False

    set_cashier(team=team, user=admin_user, actor=admin_user)
    assert user_is_cashier(admin_user, team) is True
    assert user_is_cashier(admin_user, None) is False
    assert user_is_cashier(captain_user, team) is False

    set_cashier(team=team, user=captain_user, actor=admin_user)
    assert user_is_cashier(captain_user, team) is True
    assert user_is_cashier(admin_user, team) is False  # replaced
    assert user_is_cashier(other_captain_user, team) is False
    assert user_is_cashier(player_user, team) is False
    assert user_is_cashier(AnonymousUser(), team) is False
    assert user_is_cashier(None, team) is False


def test_user_is_cashier_requires_active_account(team, admin_user):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    admin_user.is_active = False
    admin_user.save(update_fields=["is_active"])
    assert user_is_cashier(admin_user, team) is False


# ---------------------------------------------------------------------------
# Views — access matrix
# ---------------------------------------------------------------------------
def test_cashier_list_role_access(admin_client, captain_client, player_client, team):
    url = reverse("teams:cashier_list")
    assert admin_client.get(url).status_code == 200
    assert captain_client.get(url).status_code == 200  # own row editable, others read-only
    assert player_client.get(url).status_code == 403
    assert Client().get(url).status_code == 302  # anonymous -> login


def test_cashier_set_is_post_only(admin_client):
    assert admin_client.get(reverse("teams:cashier_set")).status_code == 405


def test_captain_cannot_assign_foreign_team(other_captain_client, team, admin_user):
    response = other_captain_client.post(
        reverse("teams:cashier_set"), {"team": team.pk, "user": admin_user.pk}
    )
    assert response.status_code == 403
    assert TeamCashier.objects.filter(team=team).count() == 0


def test_captain_assigns_own_team(captain_client, captain_user, team, admin_user):
    response = captain_client.post(
        reverse("teams:cashier_set"), {"team": team.pk, "user": admin_user.pk}
    )
    assert response.status_code == 302
    row = TeamCashier.objects.get(team=team)
    assert row.user == admin_user
    assert row.created_by == captain_user


def test_admin_clears_cashier(admin_client, team, admin_user):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    response = admin_client.post(reverse("teams:cashier_set"), {"team": team.pk, "user": ""})
    assert response.status_code == 302
    assert cashier_user_for(team) is None


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def test_cashier_list_shows_current_cashier_with_dollar_emoji(admin_client, team, admin_user):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    content = admin_client.get(reverse("teams:cashier_list")).content.decode()

    assert admin_user.email in content
    assert "💲" in content  # U+1F4B2 — the money marker
    assert "$" not in content  # NEVER the ASCII character
    # history table carries the "assigned by" column
    assert "assigned by" in content.lower() or "zugewiesen" in content.lower()


def test_cashier_list_renders_history_rows(admin_client, team, admin_user, captain_user):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    set_cashier(team=team, user=captain_user, actor=admin_user)

    content = admin_client.get(reverse("teams:cashier_list")).content.decode()
    assert TeamCashier.objects.filter(team=team).count() == 2
    assert admin_user.email in content  # closed row still listed (label snapshot)
    assert captain_user.email in content  # open row


def test_captain_gets_no_save_button_for_foreign_team(
    other_captain_client, team, other_team, admin_user
):
    """Decision 8 / review L4: the form is only rendered for editable teams."""
    set_cashier(team=team, user=admin_user, actor=admin_user)
    content = other_captain_client.get(reverse("teams:cashier_list")).content.decode()

    set_url = reverse("teams:cashier_set")
    assert content.count(set_url) == 1  # only the own team's row has a form


def test_cashier_list_option_labels_are_privacy_aware(
    admin_client, other_captain_client, team, other_team
):
    """Review L4: captains never see an e-mail as an option label."""
    linked = User.objects.create_user(
        email="secret@example.com", password="pw", approval_status="approved"
    )
    linked.player_link = Player.objects.create(name="Public Name", team=team)
    linked.save(update_fields=["player_link"])
    User.objects.create_user(
        email="plain@example.com", password="pw", approval_status="approved"
    )

    captain_html = other_captain_client.get(reverse("teams:cashier_list")).content.decode()
    assert "Public Name" in captain_html  # the player name is the label …
    assert "secret@example.com" not in captain_html  # … never the e-mail
    assert "plain@example.com" not in captain_html  # fallback is "—" for captains

    admin_html = admin_client.get(reverse("teams:cashier_list")).content.decode()
    assert "plain@example.com" in admin_html  # admins get the e-mail fallback
    assert "Public Name" in admin_html


# ---------------------------------------------------------------------------
# Team deletion guard + financial overview lines
# ---------------------------------------------------------------------------
def test_team_with_cashier_history_is_not_deletable(team, admin_user):
    assert team.deletable() is True
    set_cashier(team=team, user=admin_user, actor=admin_user)
    clear_cashier(team=team, actor=admin_user)
    assert TeamCashier.objects.filter(team=team).exists()
    assert team.deletable() is False


def test_kassier_line_renders_for_every_role(admin_user, team, request):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    for client_name in ("admin_client", "captain_client", "player_client"):
        client = request.getfixturevalue(client_name)
        content = client.get(reverse("dashboard:financial_overview")).content.decode()
        assert "Cashier" in content or "Kassier" in content, client_name
        assert "💲" in content, client_name
        assert "$" not in content, client_name


def test_kassier_line_shows_dash_without_cashier(admin_client, team):
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    assert re.search(r"(Cashier|Kassier):</span>\s*<ul[^>]*>\s*<li[^>]*>\s*–", content)
    assert "💲" not in content  # no marker without a cashier


def test_cashier_is_a_list_entry_under_its_own_label(admin_client, team, admin_user):
    """Same shape as the captains: 'Cashier:' on its own line, the name as the
    list item BELOW it — label and name must never share one line."""
    set_cashier(team=team, user=admin_user, actor=admin_user)
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()

    block = re.search(r"(Cashier|Kassier):</span>(.*?)</ul>", content, flags=re.DOTALL)
    assert block, "cashier block with its own list not rendered"
    assert "<li" in block.group(2)
    assert admin_user.email in block.group(2)  # the name sits inside the <li>

    label_line = next(
        (line for line in content.splitlines() if re.search(r"(Cashier|Kassier):</span>", line)),
        "",
    )
    assert label_line and admin_user.email not in label_line  # never "Cashier: name"


def test_invalid_form_redirects_with_message(admin_client):
    response = admin_client.post(reverse("teams:cashier_set"), {"team": "", "user": ""})
    assert response.status_code == 302
    assert TeamCashier.objects.count() == 0


def test_roleless_account_sees_kassier_line(db, role_groups, team, admin_user):
    user = User.objects.create_user(
        email="plainviewer@example.com", password="pw", approval_status="approved"
    )
    set_cashier(team=team, user=admin_user, actor=admin_user)
    client = Client()
    client.force_login(user)
    content = client.get(reverse("dashboard:financial_overview")).content.decode()
    assert "Cashier" in content or "Kassier" in content
    assert "💲" in content


# ---------------------------------------------------------------------------
# Marker stacking (decision 13) — Option A
# ---------------------------------------------------------------------------
def _captain_li_line(html: str, captain) -> str:
    """The rendered <li> of the captain in the captains block."""
    lines = [line for line in html.splitlines() if captain.email in line and "👑" in line]
    assert lines, "captains entry not rendered"
    return lines[0]


def test_captain_cashier_stacks_both_emojis(admin_client, team, captain_user, admin_user):
    set_cashier(team=team, user=captain_user, actor=admin_user)
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()

    line = _captain_li_line(content, captain_user)
    assert "👑" in line and "💲" in line  # one line: 👑 💲 (decision 13)
    assert "$" not in line
    # Option A: the dedicated Cashier line is present REGARDLESS of stacking.
    assert re.search(r"(Cashier|Kassier):</span>", content)


def test_non_cashier_captain_gets_crown_only(admin_client, team, captain_user, admin_user):
    set_cashier(team=team, user=admin_user, actor=admin_user)  # admin is the cashier
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()

    line = _captain_li_line(content, captain_user)
    assert "👑" in line
    assert "💲" not in line  # stacked marker only for the cashier
    # … and the dedicated line still names the (non-captain) cashier.
    assert admin_user.email in content


def test_non_captain_cashier_renders_only_the_kassier_line(
    admin_client, team, admin_user, captain_user
):
    set_cashier(team=team, user=admin_user, actor=admin_user)
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()

    line = _captain_li_line(content, captain_user)
    assert "👑" in line
    assert "💲" not in line  # the admin is no captain -> no stacking in that list
    # … and the dedicated Cashier block still names the (non-captain) cashier,
    # as a list entry under the label (never inline: "Cashier: name").
    assert re.search(
        rf"(Cashier|Kassier):</span>\s*<ul[^>]*>\s*<li[^>]*>\s*{re.escape(admin_user.email)}",
        content,
    )


# ---------------------------------------------------------------------------
# cashier_label_for
# ---------------------------------------------------------------------------
def test_cashier_label_for_prefers_player_name(db, team):
    user = User.objects.create_user(
        email="labels@example.com", password="pw", approval_status="approved"
    )
    assert cashier_label_for(user) == "labels@example.com"  # no link -> e-mail
    user.player_link = Player.objects.create(name="Erika Muster", team=team)
    user.save(update_fields=["player_link"])
    user = User.objects.get(pk=user.pk)
    assert cashier_label_for(user) == "Erika Muster"
