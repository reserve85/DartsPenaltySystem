"""Dashboard + financial overview — role matrix and ``player_link`` handling."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from app.core.permissions import GROUP_CAPTAIN
from app.penalties.services import assign_penalty

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------------------------------------------------------------------------
# Dashboard role matrix
# ---------------------------------------------------------------------------
def test_dashboard_requires_login(db):
    response = Client().get(reverse("dashboard:index"))
    assert response.status_code == 302
    assert "login" in response.url


def test_dashboard_admin_shows_all_teams(admin_client, team, other_team, matchday_with_players):
    matchday_with_players(2)  # players on ``team``
    response = admin_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    assert response.context["role"] == "admin"
    teams = {t.name: t for t in response.context["teams"]}
    assert set(teams) == {team.name, other_team.name}
    # join-safe counts: 2 players created by the factory
    assert teams[team.name].player_count == 2
    assert teams[team.name].matchday_count == 1
    assert teams[other_team.name].player_count == 0


def test_dashboard_captain_shows_own_team_totals(
    captain_client, team, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    response = captain_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    assert response.context["role"] == "captain"
    assert response.context["team"] == team
    assert response.context["player_count"] == 2
    assert response.context["matchday_count"] == 1
    assert response.context["team_total"] == Decimal(5)


def test_dashboard_player_without_link_shows_info(player_client):
    response = player_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    assert response.context["role"] == "player"
    assert response.context["player"] is None
    assert response.context["no_player_link"] is True


def test_dashboard_player_with_link_shows_own_penalties(
    player_client, player_user, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    player_user.player_link = players[0]
    player_user.save()

    response = player_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    assert response.context["player"] == players[0]
    assert response.context["own_balance"] == Decimal(5)
    assert len(response.context["own_penalties"]) == 1
    assert "no_player_link" not in response.context


# ---------------------------------------------------------------------------
# Financial overview role matrix
# ---------------------------------------------------------------------------
def test_financial_requires_login(db):
    response = Client().get(reverse("dashboard:financial_overview"))
    assert response.status_code == 302
    assert "login" in response.url


def test_financial_admin_can_select_any_team(admin_client, team, other_team):
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert len(response.context["teams"]) == 2
    assert response.context["selected_team"] == team  # default: first alphabetically

    response = admin_client.get(reverse("dashboard:financial_overview"), {"team": other_team.pk})
    assert response.context["selected_team"] == other_team


def test_financial_captain_only_sees_own_team(
    captain_client, team, other_team, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    response = captain_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert response.context["selected_team"] == team
    assert response.context["teams"] is None  # no selector for captains
    assert response.context["team_total"] == Decimal(5)
    assert other_team.name not in response.content.decode()

    # The ?team= parameter is ignored for captains (own team only)
    response = captain_client.get(reverse("dashboard:financial_overview"), {"team": other_team.pk})
    assert response.context["selected_team"] == team


def test_financial_captain_without_team_gets_403(db, role_groups):
    user = User.objects.create_user(
        email="noteam@example.com", password="pw", approval_status="approved"
    )
    user.groups.add(Group.objects.get(name=GROUP_CAPTAIN))
    client = Client()
    client.force_login(user)
    response = client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 403


def test_financial_player_sees_own_penalties_and_own_team_overview(
    player_client, player_user, team, other_team, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(3)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    assign_penalty(
        matchday=md,
        player=players[1],
        catalog_item=catalog_normal,
        description_snapshot="Keks",
        actor=admin_user,
    )
    player_user.player_link = players[0]
    player_user.save()

    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    # Own section: only the player's own rows.
    assert response.context["own_mode"] is True
    assert response.context["own_balance"] == Decimal(5)
    assert list(response.context["own_penalties"]) == list(md.penalties.filter(player=players[0]))
    # Team section: read-only overview of the OWN team (teammates visible now).
    assert response.context["selected_team"] == team
    assert response.context["can_manage"] is False  # no payment forms/buttons
    content = response.content.decode()
    assert "Keks" in content
    assert reverse("penalties:payment_create") not in content
    # A foreign team stays invisible — ?team= of another team is a 404.
    assert other_team.name not in content
    response = player_client.get(reverse("dashboard:financial_overview"), {"team": other_team.pk})
    assert response.status_code == 404


def test_financial_player_team_param_must_be_numeric(player_client, player_user, team):
    player_user.player_link = None
    player_user.save()
    # no player link -> own section only, no team lookup at all
    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.context["no_player_link"] is True

    from app.players.models import Player

    player = Player.objects.create(name="MPX", team=team)
    player_user.player_link = player
    player_user.save()
    response = player_client.get(reverse("dashboard:financial_overview"), {"team": "abc"})
    assert response.status_code == 404  # not a 500


def test_financial_player_without_link_shows_message(player_client):
    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert response.context["own_mode"] is True
    assert response.context["no_player_link"] is True


def test_financial_sections_rendered_for_admin(
    admin_client, team, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(3)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    # one active player becomes inactive with a debt
    players[1].active = False
    players[1].save()
    assign_penalty(
        matchday=md,
        player=players[1],
        catalog_item=catalog_normal,
        description_snapshot="Keks",
        actor=admin_user,
    )

    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    context = response.context
    assert context["team_total"] == Decimal(10)
    assert context["inactive_balances"][0] == players[1]
    assert context["inactive_balances"][0].balance == Decimal(5)
    assert {m.pk for m in context["matchday_totals"]} == {md.pk}
    counts = {c["description_snapshot"]: c["count"] for c in context["most_common"]}
    assert counts == {"Late arrival": 1, "Keks": 1}
    assert context["assigned_penalties"].count() == 2

    # regression: player names (not only amounts) are rendered in the tables
    content = response.content.decode()
    for row_player in players:
        assert row_player.name in content


def test_financial_overview_shows_inactive_player_names(
    admin_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    players[0].active = False
    players[0].save()
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=4,
        description_snapshot="Keks",
        actor=admin_user,
    )
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    assert players[0].name in content
