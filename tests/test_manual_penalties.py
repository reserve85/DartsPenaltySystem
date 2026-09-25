"""Manual penalties — the MANUAL catalog item (individual amount + comment)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import NoReverseMatch, reverse

from app.core.models import AuditAction, AuditLog
from app.penalties.forms import PenaltyAssignForm
from app.penalties.models import Penalty
from app.penalties.services import assign_penalty, player_balance
from app.players.models import Player

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------
def test_manual_penalty_service_creates_one_row(matchday_with_players, catalog_manual, admin_user):
    md, players = matchday_with_players(2)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_manual,
        amount_eur=3,
        description_snapshot="said stupid stuff",
        actor=admin_user,
    )
    assert Penalty.objects.count() == 1
    assert penalty.catalog_item == catalog_manual
    assert penalty.description_snapshot == "said stupid stuff"
    assert penalty.amount_eur == Decimal(3)
    assert penalty.group_id is None
    assert penalty.created_by == admin_user

    entry = AuditLog.objects.get(action=AuditAction.PENALTY_ASSIGNED, target_id=penalty.pk)
    assert entry.user == admin_user
    assert entry.metadata == {"manual": True}

    # Balance includes the manual charge (B1: positive = debt)
    assert player_balance(players[0]) == Decimal(3)


def test_manual_penalty_requires_comment(matchday_with_players, catalog_manual, admin_user):
    md, players = matchday_with_players(2)
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            assign_penalty(
                matchday=md,
                player=players[0],
                catalog_item=catalog_manual,
                amount_eur=3,
                description_snapshot=bad,
                actor=admin_user,
            )
    assert Penalty.objects.count() == 0


def test_manual_penalty_requires_amount(matchday_with_players, catalog_manual, admin_user):
    md, players = matchday_with_players(2)
    for bad in (None, 0):
        with pytest.raises(ValidationError):
            assign_penalty(
                matchday=md,
                player=players[0],
                catalog_item=catalog_manual,
                amount_eur=bad,
                description_snapshot="x",
                actor=admin_user,
            )
    assert Penalty.objects.count() == 0


def test_manual_penalty_requires_participant(matchday, player, catalog_manual, admin_user):
    # `matchday` fixture creates no participants — `player` must be rejected.
    with pytest.raises(ValidationError):
        assign_penalty(
            matchday=matchday,
            player=player,
            catalog_item=catalog_manual,
            amount_eur=3,
            description_snapshot="x",
            actor=admin_user,
        )
    assert Penalty.objects.count() == 0


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------
def test_form_exposes_manual_item_pks(matchday_with_players, catalog_manual):
    md, _players = matchday_with_players(2)
    form = PenaltyAssignForm(matchday=md)
    assert catalog_manual.pk in form.manual_ids


def test_form_only_offers_participants(matchday_with_players, catalog_manual):
    md, players = matchday_with_players(2)
    outsider = Player.objects.create(name="Outsider", team=md.team)
    form = PenaltyAssignForm(matchday=md)
    offered = set(form.fields["player"].queryset.values_list("pk", flat=True))
    assert offered == {p.pk for p in players}
    assert outsider.pk not in offered


def test_form_requires_amount_and_comment_for_manual(matchday_with_players, catalog_manual):
    md, players = matchday_with_players(2)
    form = PenaltyAssignForm(
        data={"catalog_item": catalog_manual.pk, "player": players[0].pk},
        matchday=md,
    )
    assert not form.is_valid()
    assert "amount_eur" in form.errors
    assert "description" in form.errors


def test_form_manual_valid(matchday_with_players, catalog_manual):
    md, players = matchday_with_players(2)
    form = PenaltyAssignForm(
        data={
            "catalog_item": catalog_manual.pk,
            "player": players[0].pk,
            "amount_eur": "3.50",
            "description": "blödes Gelaber",
        },
        matchday=md,
    )
    assert form.is_valid()


def test_form_normal_needs_no_amount(matchday_with_players, catalog_normal):
    md, players = matchday_with_players(2)
    form = PenaltyAssignForm(
        data={"catalog_item": catalog_normal.pk, "player": players[0].pk},
        matchday=md,
    )
    assert form.is_valid()
    assert form.cleaned_data["amount_eur"] is None


# ---------------------------------------------------------------------------
# Views — the unified "Add penalty" flow (the old manual page is gone)
# ---------------------------------------------------------------------------
def test_manual_via_view_creates(admin_client, matchday_with_players, catalog_manual):
    md, players = matchday_with_players(2)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_manual.pk,
            "player": players[1].pk,
            "amount_eur": "3.00",
            "description": "said stupid stuff",
        },
    )
    assert response.status_code == 302
    penalty = Penalty.objects.get()
    assert penalty.player == players[1]
    assert penalty.amount_eur == Decimal("3.00")
    assert penalty.description_snapshot == "said stupid stuff"
    assert penalty.catalog_item == catalog_manual
    assert (
        AuditLog.objects.filter(
            action=AuditAction.PENALTY_ASSIGNED, target_id=penalty.pk, metadata__manual=True
        ).count()
        == 1
    )


def test_manual_view_validation_renders_without_row(
    admin_client, matchday_with_players, catalog_manual
):
    md, players = matchday_with_players(2)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_manual.pk,
            "player": players[0].pk,
            "amount_eur": "3",
            "description": "   ",
        },
    )
    assert response.status_code == 200
    assert "description" in response.context["form"].errors
    assert Penalty.objects.count() == 0


def test_manual_via_view_captain_own_team_ok(captain_client, matchday_with_players, catalog_manual):
    md, players = matchday_with_players(2)
    response = captain_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_manual.pk,
            "player": players[0].pk,
            "amount_eur": "3",
            "description": "blödes Gelaber",
        },
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 1


def test_manual_forbidden_for_other_captain_and_player(
    other_captain_client, player_client, matchday_with_players, catalog_manual
):
    md, players = matchday_with_players(2)
    url = reverse("penalties:penalty_create", args=[md.pk])
    data = {
        "catalog_item": catalog_manual.pk,
        "player": players[0].pk,
        "amount_eur": "3",
        "description": "x",
    }
    assert other_captain_client.post(url, data).status_code == 403
    assert player_client.post(url, data).status_code == 403
    assert Penalty.objects.count() == 0


def test_manual_view_rejects_non_participant_player(
    admin_client, matchday_with_players, team, catalog_manual
):
    md, _players = matchday_with_players(2)
    outsider = Player.objects.create(name="Outsider", team=team)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_manual.pk,
            "player": outsider.pk,
            "amount_eur": "3",
            "description": "not here",
        },
    )
    assert response.status_code == 200  # form validation error, no row
    assert Penalty.objects.count() == 0


def test_old_manual_url_and_button_are_gone(admin_client, matchday_with_players):
    md, _players = matchday_with_players(2)
    with pytest.raises(NoReverseMatch):
        reverse("penalties:penalty_manual", args=[md.pk])
    content = admin_client.get(reverse("matchdays:matchday_detail", args=[md.pk])).content.decode()
    assert "penalties/manual" not in content


def test_manual_penalty_blocks_matchday_hard_delete(
    admin_client, matchday_with_players, catalog_manual
):
    """B2: a manual penalty also blocks matchday deletion (soft-delete guard)."""
    from app.matchdays.models import Matchday
    from app.penalties.services import soft_delete_penalty

    md, players = matchday_with_players(2)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_manual,
        amount_eur=3,
        description_snapshot="x",
        actor=None,
    )
    soft_delete_penalty(penalty=penalty, actor=None)
    response = admin_client.post(reverse("matchdays:matchday_delete", args=[md.pk]))
    assert response.status_code == 302
    assert Matchday.objects.filter(pk=md.pk).exists()
