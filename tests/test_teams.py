"""Team view tests — CRUD, captain restrictions and the B2 delete guard."""

import pytest
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.players.models import Player
from app.teams.models import Team

pytestmark = pytest.mark.django_db


def test_admin_creates_team(admin_client):
    response = admin_client.post(reverse("teams:team_create"), {"name": "Wildboars 3"})
    assert response.status_code == 302
    team = Team.objects.get(name="Wildboars 3")
    assert team.name == "Wildboars 3"
    assert AuditLog.objects.filter(action=AuditAction.TEAM_CREATED, target_id=team.pk).count() == 1


def test_captain_cannot_create_team(captain_client):
    response = captain_client.get(reverse("teams:team_create"))
    assert response.status_code == 403
    response = captain_client.post(reverse("teams:team_create"), {"name": "Hack"})
    assert response.status_code == 403
    assert not Team.objects.filter(name="Hack").exists()


def test_admin_renames_team(admin_client, team):
    response = admin_client.post(reverse("teams:team_update", args=[team.pk]), {"name": "Renamed"})
    assert response.status_code == 302
    team.refresh_from_db()
    assert team.name == "Renamed"
    assert AuditLog.objects.filter(action=AuditAction.TEAM_UPDATED, target_id=team.pk).count() == 1


def test_team_list_shows_counts(admin_client, team):
    Player.objects.create(name="A", team=team)
    Player.objects.create(name="B", team=team)
    response = admin_client.get(reverse("teams:team_list"))
    assert response.status_code == 200
    listed = response.context["teams"].get(pk=team.pk)
    assert listed.player_count == 2


def test_captain_team_detail_own_ok_other_forbidden(
    captain_client, other_captain_client, team, other_team
):
    assert captain_client.get(reverse("teams:team_detail", args=[team.pk])).status_code == 200
    assert captain_client.get(reverse("teams:team_detail", args=[other_team.pk])).status_code == 403
    assert other_captain_client.get(reverse("teams:team_detail", args=[team.pk])).status_code == 403


# ---------------------------------------------------------------------------
# B2 delete guard
# ---------------------------------------------------------------------------
def test_delete_guard_team_with_players_rejected(admin_client, team):
    Player.objects.create(name="P", team=team)
    response = admin_client.post(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 302
    assert Team.objects.filter(pk=team.pk).exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_DELETED).count() == 0


def test_delete_guard_team_with_matchdays_rejected(admin_client, team, player):
    from datetime import date

    from app.matchdays.models import Matchday, MatchdayPlayer

    md = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 1, 1))
    MatchdayPlayer.objects.create(matchday=md, player=player)
    response = admin_client.post(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 302
    assert Team.objects.filter(pk=team.pk).exists()


def test_delete_guard_team_with_linked_captain_rejected(admin_client, team):
    from app.accounts.models import User

    User.objects.create_user(email="captain2@example.com", password="pw", team=team)
    response = admin_client.post(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 302
    assert Team.objects.filter(pk=team.pk).exists()


def test_empty_team_deleted_and_audited(admin_client, team):
    response = admin_client.post(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 302
    assert not Team.objects.filter(pk=team.pk).exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_DELETED, target_id=team.pk).count() == 1


def test_delete_page_renders_confirmation(admin_client, team):
    response = admin_client.get(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Team overview (Teamsübersicht): player links + balance, matchday links
# ---------------------------------------------------------------------------
def test_team_detail_links_players_with_balance(
    admin_client, team, matchday_with_players, catalog_normal, admin_user
):
    from decimal import Decimal

    from app.penalties.services import assign_penalty

    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    response = admin_client.get(reverse("teams:team_detail", args=[team.pk]))
    assert response.status_code == 200
    content = response.content.decode()

    # each player is linked to its detail page …
    assert reverse("players:player_detail", args=[players[0].pk]) in content
    assert reverse("players:player_detail", args=[players[1].pk]) in content
    # … and the balance for THIS team is shown next to the name
    rows = {row["player"].pk: row["balance"] for row in response.context["player_rows"]}
    assert rows[players[0].pk] == Decimal(5)
    assert rows[players[1].pk] == Decimal(0)

    # matchdays are linked to their detail page
    assert reverse("matchdays:matchday_detail", args=[md.pk]) in content
    assert md.display_label in content
