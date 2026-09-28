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
# Team fees + active flags — edited in the integrated catalog form
# (the dedicated fee page is gone; everything retargeted to catalog_update)
# ---------------------------------------------------------------------------
def test_admin_sees_all_teams_on_edit_form(admin_client, catalog_normal, team, other_team):
    response = admin_client.get(reverse("penalties:catalog_update", args=[catalog_normal.pk]))
    assert response.status_code == 200
    form = response.context["form"]
    for pk in (team.pk, other_team.pk):
        assert f"team_active_{pk}" in form.fields
        amount_field = form.fields[f"team_amount_{pk}"]
        assert amount_field.required  # every fee row must be filled
        assert amount_field.initial == catalog_normal.amount_eur  # prefilled default
    assert "apply_all" not in form.fields  # checkbox removed — JS auto-copies
    assert "active" not in form.fields  # the global switch is gone


def test_captain_sees_and_edits_all_teams(captain_client, catalog_normal, team, other_team):
    """Captains have the same catalog scope as admins: every team.

    Only the CREATE defaults differ (own team active, others pre-unchecked).
    """
    TeamCatalogAmount.objects.create(team=other_team, catalog_item=catalog_normal, amount_eur=4)
    url = reverse("penalties:catalog_update", args=[catalog_normal.pk])

    response = captain_client.get(url)
    assert response.status_code == 200
    form = response.context["form"]
    # EVERY team is visible/editable for a captain …
    for pk in (team.pk, other_team.pk):
        assert f"team_active_{pk}" in form.fields
        assert f"team_amount_{pk}" in form.fields

    # … and the captain may edit fees + flags for every team.
    response = captain_client.post(
        url,
        {
            "description": catalog_normal.description,
            "amount_eur": "5.00",
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_amount_{team.pk}": "1",
            f"team_amount_{other_team.pk}": "3",  # foreign fee edited too
            # other team's active flag intentionally not posted -> inactive
        },
    )
    assert response.status_code == 302
    own = TeamCatalogAmount.objects.get(catalog_item=catalog_normal, team=team)
    assert own.amount_eur == Decimal(1)
    assert own.active is True
    foreign = TeamCatalogAmount.objects.get(catalog_item=catalog_normal, team=other_team)
    assert foreign.amount_eur == Decimal(3)  # captain may edit other teams
    assert foreign.active is False


def test_captain_create_form_defaults_other_teams_inactive(captain_client, team, other_team):
    """On create a captain sees ALL teams: own pre-checked, others pre-unchecked."""
    response = captain_client.get(reverse("penalties:catalog_create"))
    assert response.status_code == 200
    form = response.context["form"]
    assert form.fields[f"team_active_{team.pk}"].initial is True
    assert form.fields[f"team_active_{other_team.pk}"].initial is False
    assert f"team_amount_{other_team.pk}" in form.fields  # still visible + required


def test_admin_create_form_defaults_all_teams_active(admin_client, team, other_team):
    """An admin's create form starts with every team active."""
    response = admin_client.get(reverse("penalties:catalog_create"))
    assert response.status_code == 200
    form = response.context["form"]
    assert form.fields[f"team_active_{team.pk}"].initial is True
    assert form.fields[f"team_active_{other_team.pk}"].initial is True


def test_blank_fee_is_rejected_and_row_unchanged(admin_client, catalog_normal, team):
    """No row may ever be blank — the save is blocked, the row stays intact."""
    TeamCatalogAmount.objects.create(team=team, catalog_item=catalog_normal, amount_eur=1)
    url = reverse("penalties:catalog_update", args=[catalog_normal.pk])
    response = admin_client.post(
        url,
        {
            "description": catalog_normal.description,
            "amount_eur": "5.00",
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_amount_{team.pk}": "",  # blank -> required error
        },
    )
    assert response.status_code == 200  # re-rendered with an error
    row = TeamCatalogAmount.objects.get(catalog_item=catalog_normal, team=team)
    assert row.amount_eur == Decimal(1)  # unchanged
    assert catalog_normal.amount_for_team(team) == Decimal(1)


def test_invalid_amount_is_rejected(admin_client, catalog_normal, team):
    url = reverse("penalties:catalog_update", args=[catalog_normal.pk])
    response = admin_client.post(
        url,
        {
            "description": catalog_normal.description,
            "amount_eur": "5.00",
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_amount_{team.pk}": "-2",
        },
    )
    assert response.status_code == 200  # re-rendered with error
    assert not TeamCatalogAmount.objects.filter(catalog_item=catalog_normal).exists()


def test_player_cannot_open_edit_form(player_client, catalog_normal):
    url = reverse("penalties:catalog_update", args=[catalog_normal.pk])
    assert player_client.get(url).status_code == 403
    assert (
        player_client.post(
            url,
            {"description": "Hacked", "amount_eur": "1.00", "type": "NORMAL"},
        ).status_code
        == 403
    )


def test_anonymous_redirected_from_edit_form(db, catalog_normal):
    from django.test import Client

    response = Client().get(reverse("penalties:catalog_update", args=[catalog_normal.pk]))
    assert response.status_code == 302
    assert "login" in response.url


def test_catalog_list_shows_team_fee_overrides(admin_client, catalog_normal, team, other_team):
    import re

    TeamCatalogAmount.objects.create(team=team, catalog_item=catalog_normal, amount_eur=1)
    content = admin_client.get(reverse("penalties:catalog_list")).content.decode()
    # amounts are wrapped in a color span (penalties are red) -> compare plain text
    plain = re.sub(r"<[^>]+>", "", content)
    assert f"{team.name}: 1.00 €" in plain or f"{team.name}: 1,00 €" in plain
    # EVERY team shows its real fee — no "Default"/"Standard" placeholder,
    # even for a team without an explicit row yet (legacy: default amount).
    assert f"{other_team.name}: 5.00 €" in plain or f"{other_team.name}: 5,00 €" in plain
    # a single action button per row — the dedicated fee page is gone
    edit_url = reverse("penalties:catalog_update", args=[catalog_normal.pk])
    assert content.count(edit_url) == 1
    assert "team-amounts" not in content
