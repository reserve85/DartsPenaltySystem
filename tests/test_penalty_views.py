"""Penalty view + catalog tests — CRUD, toggle, dispatch, permission scoping."""

from decimal import Decimal

import pytest
from django.urls import NoReverseMatch, reverse

from app.core.models import AuditAction, AuditLog
from app.matchdays.models import MatchdayPlayer
from app.penalties.forms import PenaltyAssignForm
from app.penalties.models import Penalty, PenaltyCatalogItem

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def test_catalog_list_admin_and_captain(admin_client, captain_client, player_client):
    assert admin_client.get(reverse("penalties:catalog_list")).status_code == 200
    assert captain_client.get(reverse("penalties:catalog_list")).status_code == 200
    assert player_client.get(reverse("penalties:catalog_list")).status_code == 403


def test_catalog_manage_player_forbidden_captain_allowed(
    player_client, captain_client, team, other_team
):
    """Players stay locked out; captains create/edit catalog entries."""
    assert player_client.get(reverse("penalties:catalog_create")).status_code == 403
    assert (
        player_client.post(
            reverse("penalties:catalog_create"),
            {"description": "X", "amount_eur": 1, "type": "NORMAL"},
        ).status_code
        == 403
    )

    assert captain_client.get(reverse("penalties:catalog_create")).status_code == 200
    response = captain_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Captain entry",
            "amount_eur": 2,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",  # own team ticked (others default off)
            f"team_amount_{team.pk}": "2",  # every fee row must be filled
            f"team_amount_{other_team.pk}": "2",
        },
    )
    assert response.status_code == 302
    item = PenaltyCatalogItem.objects.get(description="Captain entry")
    # Only the captain's own team is active — every other team is inactive
    # (the create form shows ALL teams; others are pre-unchecked).
    own = item.team_amounts.get(team=team)
    assert own.active is True
    other = item.team_amounts.get(team=other_team)
    assert other.active is False
    assert other.amount_eur == Decimal(2)  # filled row (own fee per team)


def test_catalog_admin_crud(admin_client, team, other_team):
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Too loud",
            "amount_eur": 2,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_active_{other_team.pk}": "on",
            f"team_amount_{team.pk}": "2",
            f"team_amount_{other_team.pk}": "2",
        },
    )
    assert response.status_code == 302
    item = PenaltyCatalogItem.objects.get(description="Too loud")
    assert all(row.active for row in item.team_amounts.all())

    # No global "active" field anymore — per-team flags come from the POST.
    response = admin_client.post(
        reverse("penalties:catalog_update", args=[item.pk]),
        {
            "description": "Too loud",
            "amount_eur": 3,
            "type": "NORMAL",
            f"team_amount_{team.pk}": "3",
            f"team_amount_{other_team.pk}": "3",
        },
    )
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.amount_eur == Decimal(3)
    assert not any(row.active for row in item.team_amounts.all())


def test_catalog_list_has_single_edit_action_and_edit_page_has_delete(admin_client, catalog_normal):
    """No toggle URL anymore; one action per row; delete lives on the edit page."""
    with pytest.raises(NoReverseMatch):
        reverse("penalties:catalog_toggle_active", args=[catalog_normal.pk])
    with pytest.raises(NoReverseMatch):
        reverse("penalties:catalog_team_amounts", args=[catalog_normal.pk])

    content = admin_client.get(reverse("penalties:catalog_list")).content.decode()
    edit_url = reverse("penalties:catalog_update", args=[catalog_normal.pk])
    assert edit_url in content
    assert content.count(edit_url) == 1  # exactly ONE action button per row

    edit = admin_client.get(edit_url).content.decode()
    assert reverse("penalties:catalog_delete", args=[catalog_normal.pk]) in edit


def test_catalog_form_rejects_non_positive(admin_client, team):
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Bad",
            "amount_eur": 0,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_amount_{team.pk}": "1",
        },
    )
    assert response.status_code == 200
    assert not PenaltyCatalogItem.objects.filter(description="Bad").exists()


def test_create_requires_filled_team_rows(admin_client, team, other_team):
    """Every fee row must be filled — a blank row blocks the save."""
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Half filled",
            "amount_eur": 5,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_active_{other_team.pk}": "on",
            f"team_amount_{team.pk}": "5",
            # other team's row intentionally left blank
        },
    )
    assert response.status_code == 200  # re-rendered with a required error
    assert not PenaltyCatalogItem.objects.filter(description="Half filled").exists()


def test_assign_form_excludes_inactive_items(matchday_with_players, catalog_normal, other_team):
    """Deactivation is per team: only rows for the matchday's team count."""
    from datetime import date

    from app.matchdays.models import Matchday
    from app.penalties.models import PenaltyType, TeamCatalogAmount

    md, _players = matchday_with_players(2)
    inactive = PenaltyCatalogItem.objects.create(
        description="Old", amount_eur=1, type=PenaltyType.NORMAL
    )
    TeamCatalogAmount.objects.create(
        team=md.team, catalog_item=inactive, active=False, amount_eur=1
    )

    form = PenaltyAssignForm(matchday=md)
    pks = list(form.fields["catalog_item"].queryset.values_list("pk", flat=True))
    assert inactive.pk not in pks
    assert catalog_normal.pk in pks  # no row -> active by default

    # Another team's matchday is unaffected (no row for that team).
    other_md = Matchday.objects.create(
        team=other_team, opponent="X", venue="home", date=date(2026, 1, 1)
    )
    other_pks = list(
        PenaltyAssignForm(matchday=other_md)
        .fields["catalog_item"]
        .queryset.values_list("pk", flat=True)
    )
    assert inactive.pk in other_pks


# ---------------------------------------------------------------------------
# Assignment via views
# ---------------------------------------------------------------------------
def test_assign_normal_via_view(admin_client, matchday_with_players, catalog_normal):
    md, players = matchday_with_players(2)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_normal.pk,
            "player": players[0].pk,
            "amount_eur": "99",  # no amount input anymore — must be ignored
            "description": "Custom desc",
        },
    )
    assert response.status_code == 302
    penalty = Penalty.objects.get()
    assert penalty.player_id == players[0].pk
    assert penalty.amount_eur == Decimal(5)  # the catalog amount
    assert penalty.description_snapshot == "Custom desc"
    assert AuditLog.objects.filter(action=AuditAction.PENALTY_ASSIGNED).count() == 1


def test_assign_group_via_view(admin_client, matchday_with_players, catalog_group):
    md, players = matchday_with_players(4)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_group.pk, "player": players[0].pk},
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 3
    assert not Penalty.objects.filter(player_id=players[0].pk).exists()


def test_assign_via_captain_own_team_ok(captain_client, matchday_with_players, catalog_normal):
    md, players = matchday_with_players(2)
    response = captain_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_normal.pk, "player": players[0].pk, "amount_eur": "5"},
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 1


def test_assign_forbidden_for_other_team_and_player(
    captain_client, player_client, other_team, catalog_normal
):
    from datetime import date

    from app.matchdays.models import Matchday
    from app.players.models import Player

    foreign_player = Player.objects.create(name="F", team=other_team)
    md = Matchday.objects.create(team=other_team, opponent="X", venue="home", date=date(2026, 1, 1))
    MatchdayPlayer.objects.create(matchday=md, player=foreign_player)

    assert captain_client.get(reverse("penalties:penalty_create", args=[md.pk])).status_code == 403
    assert player_client.get(reverse("penalties:penalty_create", args=[md.pk])).status_code == 403


def test_edit_via_view_group(admin_client, matchday_with_players, catalog_group):
    md, players = matchday_with_players(3)
    from app.penalties.services import assign_group_penalty

    assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=None
    )
    penalty = Penalty.objects.first()
    response = admin_client.post(
        reverse("penalties:penalty_update", args=[penalty.pk]),
        {"description_snapshot": "Ediert", "amount_eur": "2.50"},
    )
    assert response.status_code == 302
    assert all(p.description_snapshot == "Ediert" for p in Penalty.objects.all())
    assert all(p.amount_eur == Decimal("2.50") for p in Penalty.objects.all())
    assert AuditLog.objects.filter(action=AuditAction.PENALTY_EDITED).count() == 2


def test_delete_via_view_soft_deletes(admin_client, matchday_with_players, catalog_normal):
    md, players = matchday_with_players(2)
    from app.penalties.services import assign_penalty

    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late",
        actor=None,
    )
    response = admin_client.post(reverse("penalties:penalty_delete", args=[penalty.pk]))
    assert response.status_code == 302
    assert Penalty.objects.count() == 0
    assert Penalty.all_objects().count() == 1
    assert AuditLog.objects.filter(action=AuditAction.PENALTY_DELETED).count() == 1


def test_update_deleted_penalty_404(admin_client, matchday_with_players, catalog_normal):
    from app.penalties.services import assign_penalty, soft_delete_penalty

    md, players = matchday_with_players(2)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late",
        actor=None,
    )
    soft_delete_penalty(penalty=penalty, actor=None)
    assert (
        admin_client.get(reverse("penalties:penalty_update", args=[penalty.pk])).status_code == 404
    )


def test_group_penalty_view_single_participant_shows_form_error(
    admin_client, matchday_with_players, catalog_group
):
    """Review fix (M1): 1-participant matchday -> friendly form error, never 500.

    ``assign_group_penalty`` raises a ValidationError the view used to swallow
    into an unhandled 500; the form now validates it and the view is a
    second line of defence.
    """
    md, players = matchday_with_players(1)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_group.pk, "player": players[0].pk},
    )
    assert response.status_code == 200  # form re-rendered, not a 500
    errors = response.context["form"].errors
    flat = " ".join(str(error) for group in errors.values() for error in group)
    assert "participants" in flat or "Teilnehmer" in flat  # en / de catalog
    assert Penalty.objects.count() == 0
