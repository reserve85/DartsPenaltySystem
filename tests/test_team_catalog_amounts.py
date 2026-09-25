"""Team-specific catalog fees — override model, assignment, view permissions."""

from decimal import Decimal

import pytest
from django.urls import reverse

from app.penalties.models import PenaltyType, TeamCatalogAmount
from app.penalties.services import assign_group_penalty

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Model / effective amount
# ---------------------------------------------------------------------------
def test_amount_for_team_falls_back_to_default(catalog_normal, team, other_team):
    assert catalog_normal.amount_for_team(team) == Decimal(5)
    assert catalog_normal.amount_for_team(other_team) == Decimal(5)
    assert catalog_normal.amount_for_team(None) == Decimal(5)


def test_amount_for_team_uses_override_only_for_that_team(catalog_normal, team, other_team):
    TeamCatalogAmount.objects.create(team=team, catalog_item=catalog_normal, amount_eur=1)
    assert catalog_normal.amount_for_team(team) == Decimal(1)
    assert catalog_normal.amount_for_team(other_team) == Decimal(5)


def test_catalog_type_still_valid_choices(catalog_normal):
    assert catalog_normal.type == PenaltyType.NORMAL


# ---------------------------------------------------------------------------
# Assignment uses the effective team amount
# ---------------------------------------------------------------------------
def test_normal_assignment_uses_team_override_via_view(
    admin_client, matchday_with_players, catalog_normal, team
):
    TeamCatalogAmount.objects.create(team=team, catalog_item=catalog_normal, amount_eur=1)
    md, players = matchday_with_players(2)

    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_normal.pk, "player": players[0].pk},
    )
    assert response.status_code == 302
    penalty = md.penalties.get(player=players[0])
    assert penalty.amount_eur == Decimal(1)


def test_normal_assignment_uses_default_without_override(
    admin_client, matchday_with_players, catalog_normal
):
    md, players = matchday_with_players(2)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_normal.pk, "player": players[0].pk},
    )
    assert response.status_code == 302
    assert md.penalties.get(player=players[0]).amount_eur == Decimal(5)


def test_group_assignment_uses_team_override(
    matchday_with_players, catalog_group, admin_user, team
):
    TeamCatalogAmount.objects.create(team=team, catalog_item=catalog_group, amount_eur=2)
    md, players = matchday_with_players(3)

    created = assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=admin_user
    )
    assert created
    assert all(row.amount_eur == Decimal(2) for row in created)


# ---------------------------------------------------------------------------
# Team fee page — who may set what
# ---------------------------------------------------------------------------
def test_admin_sees_all_teams_on_fee_page(admin_client, catalog_normal, team, other_team):
    response = admin_client.get(reverse("penalties:catalog_team_amounts", args=[catalog_normal.pk]))
    assert response.status_code == 200
    rows = response.context["rows"]
    assert {row["team"].pk for row in rows} == {team.pk, other_team.pk}
    assert response.context["default_amount"] == catalog_normal.amount_eur


def test_captain_sees_and_sets_only_own_team(captain_client, catalog_normal, team, other_team):
    url = reverse("penalties:catalog_team_amounts", args=[catalog_normal.pk])
    response = captain_client.get(url)
    assert response.status_code == 200
    assert [row["team"].pk for row in response.context["rows"]] == [team.pk]

    # own team id sets the override …
    response = captain_client.post(url, {f"team_{team.pk}": "1"})
    assert response.status_code == 302
    override = TeamCatalogAmount.objects.get(catalog_item=catalog_normal)
    assert override.team == team
    assert override.amount_eur == Decimal(1)

    # … a foreign team id injected into POST is ignored
    response = captain_client.post(url, {f"team_{other_team.pk}": "3"})
    assert response.status_code == 302
    assert not TeamCatalogAmount.objects.filter(team=other_team).exists()


def test_blank_input_clears_override_back_to_default(admin_client, catalog_normal, team):
    TeamCatalogAmount.objects.create(team=team, catalog_item=catalog_normal, amount_eur=1)
    url = reverse("penalties:catalog_team_amounts", args=[catalog_normal.pk])
    response = admin_client.post(url, {f"team_{team.pk}": ""})
    assert response.status_code == 302
    assert not TeamCatalogAmount.objects.filter(catalog_item=catalog_normal).exists()
    assert catalog_normal.amount_for_team(team) == Decimal(5)


def test_invalid_amount_is_rejected(admin_client, catalog_normal, team):
    url = reverse("penalties:catalog_team_amounts", args=[catalog_normal.pk])
    response = admin_client.post(url, {f"team_{team.pk}": "-2"})
    assert response.status_code == 200  # re-rendered with error
    assert not TeamCatalogAmount.objects.filter(catalog_item=catalog_normal).exists()


def test_player_cannot_open_fee_page(player_client, catalog_normal):
    response = player_client.get(
        reverse("penalties:catalog_team_amounts", args=[catalog_normal.pk])
    )
    assert response.status_code == 403


def test_anonymous_redirected_from_fee_page(db, catalog_normal):
    from django.test import Client

    response = Client().get(reverse("penalties:catalog_team_amounts", args=[catalog_normal.pk]))
    assert response.status_code == 302
    assert "login" in response.url


def test_catalog_list_shows_team_fee_overrides(admin_client, catalog_normal, team):
    import re

    TeamCatalogAmount.objects.create(team=team, catalog_item=catalog_normal, amount_eur=1)
    content = admin_client.get(reverse("penalties:catalog_list")).content.decode()
    # language-independent: the team fee button links to the fee page
    assert reverse("penalties:catalog_team_amounts", args=[catalog_normal.pk]) in content
    # amounts are wrapped in a color span (penalties are red) -> compare plain text
    plain = re.sub(r"<[^>]+>", "", content)
    assert f"{team.name}: 1.00 €" in plain or f"{team.name}: 1,00 €" in plain
