"""AuditLog behaviour via the core service layer."""

import pytest
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.core.services import log_action

pytestmark = pytest.mark.django_db


def test_log_action_writes_entry(admin_user, player):
    entry = log_action(AuditAction.PLAYER_CREATED, user=admin_user, target=player)
    assert entry.action == AuditAction.PLAYER_CREATED
    assert entry.user == admin_user
    assert entry.target_type == "Player"
    assert entry.target_id == player.pk
    assert entry.created_at is not None
    assert AuditLog.objects.count() == 1


def test_log_action_without_user_or_target(admin_user):
    entry = log_action(AuditAction.LOGOUT)
    assert entry.user is None
    assert entry.target_type == ""
    assert entry.target_id is None


def test_log_action_stores_metadata(admin_user, player):
    entry = log_action(
        AuditAction.PENALTY_ASSIGNED,
        user=admin_user,
        target=player,
        metadata={"group_id": "abc123"},
    )
    assert entry.metadata == {"group_id": "abc123"}


def test_audit_action_labels_are_translated_pairs():
    choices = AuditAction.choices
    assert isinstance(choices, list)
    assert all(len(pair) == 2 for pair in choices)
    assert any(value == "login" for value, _ in choices)
    assert any(value == "penalty_assigned" for value, _ in choices)
    assert all(str(label) for _, label in choices)


# ---------------------------------------------------------------------------
# Audit log list view
# ---------------------------------------------------------------------------
def test_audit_list_admin(admin_client):
    response = admin_client.get(reverse("audit_list"))
    assert response.status_code == 200


def test_audit_list_forbidden_for_captain_and_player(captain_client, player_client):
    assert captain_client.get(reverse("audit_list")).status_code == 403
    assert player_client.get(reverse("audit_list")).status_code == 403


def test_audit_list_pagination_and_filter(admin_client):
    from app.players.models import Player
    from app.teams.models import Team

    team = Team.objects.create(name="T")
    for i in range(30):
        player = Player.objects.create(name=f"P{i}", team=team)
        log_action(AuditAction.PLAYER_CREATED, target=player)
    response = admin_client.get(reverse("audit_list"))
    assert response.status_code == 200
    assert response.context["page_obj"].paginator.num_pages == 2
