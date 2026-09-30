"""Penalty service-layer tests — assignments, group/180 semantics, edit, soft delete (M4)."""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError

from app.core.models import AuditAction, AuditLog
from app.penalties.models import Penalty
from app.penalties.services import (
    assert_can_manage_penalty,
    assign_group_penalty,
    assign_penalty,
    edit_penalty,
    player_balance,
    soft_delete_penalty,
)
from app.players.models import Player

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# NORMAL assignment
# ---------------------------------------------------------------------------
def test_assign_normal_creates_one_row(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    assert Penalty.objects.count() == 1
    assert penalty.amount_eur == Decimal(5)
    assert penalty.description_snapshot == "Late arrival"
    assert penalty.group_id is None
    assert penalty.created_by == admin_user
    assert penalty.matchday == md

    entry = AuditLog.objects.get(action=AuditAction.PENALTY_ASSIGNED, target_id=penalty.pk)
    assert entry.user == admin_user
    assert entry.target_type == "Penalty"
    assert entry.created_at is not None


def test_assign_requires_participant(matchday, player, catalog_normal, admin_user):
    with pytest.raises(DjangoValidationError):
        assign_penalty(
            matchday=matchday,
            player=player,
            catalog_item=catalog_normal,
            amount_eur=5,
            description_snapshot="Late",
            actor=admin_user,
        )


def test_assign_normal_ignores_any_passed_amount(matchday_with_players, catalog_normal, admin_user):
    """Only the catalog amount may be used for normal penalties (UI has no input)."""
    md, players = matchday_with_players(2)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=999,  # ignored
        description_snapshot="Late",
        actor=admin_user,
    )
    assert penalty.amount_eur == Decimal(5)


# ---------------------------------------------------------------------------
# Group / 180 behaviour (B1 + M4)
# ---------------------------------------------------------------------------
def test_group_180_expands_to_other_participants(matchday_with_players, catalog_group, admin_user):
    md, players = matchday_with_players(6)
    trigger = players[0]
    created = assign_group_penalty(
        matchday=md, trigger_player=trigger, catalog_item=catalog_group, actor=admin_user
    )

    assert len(created) == 5
    assert all(p.group_id == created[0].group_id for p in created)
    assert all(p.amount_eur == Decimal(1) for p in created)
    expected = f"{catalog_group.description} — {trigger.name}"
    assert all(p.description_snapshot == expected for p in created)

    created_players = {p.player_id for p in created}
    assert created_players == {player.pk for player in players[1:]}
    # trigger player receives NO row
    assert not Penalty.objects.filter(player=trigger).exists()

    # Net balances: P1=0, others = 1
    assert player_balance(trigger) == Decimal(0)
    for player in players[1:]:
        assert player_balance(player) == Decimal(1)

    # One PENALTY_ASSIGNED audit entry per generated row (M4)
    entries = list(AuditLog.objects.filter(action=AuditAction.PENALTY_ASSIGNED))
    assert len(entries) == 5
    for entry in entries:
        assert entry.metadata["group_id"] == created[0].group_id
        assert entry.metadata["trigger_player_pk"] == trigger.pk


def test_group_requires_two_participants(matchday_with_players, catalog_group, admin_user):
    md, players = matchday_with_players(1)
    with pytest.raises(DjangoValidationError):
        assign_group_penalty(
            matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=admin_user
        )
    assert Penalty.objects.count() == 0


def test_group_never_crosses_teams(matchday_with_players, other_team, catalog_group, admin_user):
    md, players = matchday_with_players(4)
    foreign = Player.objects.create(name="Foreign", team=other_team)
    created = assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=admin_user
    )
    assert len(created) == 3
    assert all(p.player_id != foreign.pk for p in created)
    assert not Penalty.objects.filter(player=foreign).exists()


def test_edit_group_updates_all_rows_with_per_row_audit(
    matchday_with_players, catalog_group, admin_user
):
    md, players = matchday_with_players(3)
    assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=admin_user
    )
    rows = list(Penalty.objects.all())

    edit_penalty(penalty=rows[0], actor=admin_user, amount_eur=2, description_snapshot="Neuer Text")

    refreshed = list(Penalty.objects.all())
    assert len(refreshed) == 2
    assert all(p.amount_eur == Decimal(2) for p in refreshed)
    assert all(p.description_snapshot == "Neuer Text" for p in refreshed)
    entries = list(AuditLog.objects.filter(action=AuditAction.PENALTY_EDITED))
    assert len(entries) == 2
    assert all(entry.metadata["group_id"] == rows[0].group_id for entry in entries)


def test_edit_single_normal_penalty(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late",
        actor=admin_user,
    )
    edit_penalty(penalty=penalty, actor=admin_user, amount_eur=10)
    penalty.refresh_from_db()
    assert penalty.amount_eur == Decimal(10)
    assert penalty.description_snapshot == "Late"
    assert (
        AuditLog.objects.filter(action=AuditAction.PENALTY_EDITED, target_id=penalty.pk).count()
        == 1
    )


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------
def test_soft_delete_group_rows_and_siblings(matchday_with_players, catalog_group, admin_user):
    md, players = matchday_with_players(3)
    assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=admin_user
    )
    rows = list(Penalty.objects.all())

    soft_delete_penalty(penalty=rows[0], actor=admin_user)

    assert Penalty.objects.count() == 0  # default manager hides everything
    assert Penalty.all_objects().count() == 2  # history survives
    all_rows = list(Penalty.all_objects())
    assert all(r.deleted_by == admin_user for r in all_rows)
    assert all(r.deleted_at is not None for r in all_rows)
    entries = list(AuditLog.objects.filter(action=AuditAction.PENALTY_DELETED))
    assert len(entries) == 2
    assert all(entry.metadata["group_id"] == rows[0].group_id for entry in entries)


def test_soft_delete_single(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late",
        actor=admin_user,
    )
    soft_delete_penalty(penalty=penalty, actor=admin_user)
    penalty.refresh_from_db()
    assert penalty.deleted_at is not None
    assert penalty.deleted_by == admin_user
    assert (
        AuditLog.objects.filter(action=AuditAction.PENALTY_DELETED, target_id=penalty.pk).count()
        == 1
    )


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def test_assert_can_manage_penalty(captain_user, other_captain_user, player_user, team):
    from datetime import date

    from app.matchdays.models import Matchday, MatchdayPlayer

    p1 = Player.objects.create(name="P1", team=team)
    md = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 1, 1))
    MatchdayPlayer.objects.create(matchday=md, player=p1)
    penalty = Penalty.objects.create(matchday=md, player=p1, description_snapshot="X", amount_eur=1)
    # Captain of the matchday's team may manage it (no exception)
    assert_can_manage_penalty(captain_user, penalty)
    with pytest.raises(PermissionDenied):
        assert_can_manage_penalty(other_captain_user, penalty)
    with pytest.raises(PermissionDenied):
        assert_can_manage_penalty(player_user, penalty)


# ---------------------------------------------------------------------------
# DOUBLES (Doppelspiel) — optional second player on flagged catalog items
# ---------------------------------------------------------------------------
def test_normal_with_doubles_partner_charges_both(
    matchday_with_players, catalog_doubles, admin_user
):
    md, players = matchday_with_players(3)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_doubles,
        description_snapshot="Lowdart",
        double_partner=players[1],
        actor=admin_user,
    )

    assert Penalty.objects.count() == 2
    assert {row.player_id for row in Penalty.objects.all()} == {
        players[0].pk,
        players[1].pk,
    }
    partner_row = Penalty.objects.get(player=players[1])
    # Both rows form ONE group — edit/soft delete act on the pair (M4).
    assert penalty.group_id is not None
    assert partner_row.group_id == penalty.group_id
    assert partner_row.amount_eur == penalty.amount_eur == Decimal(5)
    assert partner_row.description_snapshot == "Lowdart"

    entries = list(AuditLog.objects.filter(action=AuditAction.PENALTY_ASSIGNED))
    assert len(entries) == 2
    assert all(entry.metadata["double_partner_pk"] == players[1].pk for entry in entries)


def test_normal_flagged_without_partner_stays_single_row(
    matchday_with_players, catalog_doubles, admin_user
):
    """Choosing NO doubles partner is always allowed (optional field)."""
    md, players = matchday_with_players(3)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_doubles,
        double_partner=None,
        actor=admin_user,
    )
    assert Penalty.objects.count() == 1
    assert penalty.group_id is None
    assert not any(
        entry.metadata.get("double_partner_pk")
        for entry in AuditLog.objects.filter(action=AuditAction.PENALTY_ASSIGNED)
    )


def test_doubles_partner_rejected_without_flag(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(3)
    with pytest.raises(DjangoValidationError):
        assign_penalty(
            matchday=md,
            player=players[0],
            catalog_item=catalog_normal,
            double_partner=players[1],
            actor=admin_user,
        )
    assert Penalty.objects.count() == 0


def test_doubles_partner_must_be_another_participant(
    matchday_with_players, catalog_doubles, team, admin_user
):
    md, players = matchday_with_players(3)
    with pytest.raises(DjangoValidationError):  # same player as the lead
        assign_penalty(
            matchday=md,
            player=players[0],
            catalog_item=catalog_doubles,
            double_partner=players[0],
            actor=admin_user,
        )
    outsider = Player.objects.create(name="Outsider", team=team)
    with pytest.raises(DjangoValidationError):  # not on the matchday roster
        assign_penalty(
            matchday=md,
            player=players[0],
            catalog_item=catalog_doubles,
            double_partner=outsider,
            actor=admin_user,
        )
    assert Penalty.objects.count() == 0


def test_group_penalty_excludes_both_doubles_players(
    matchday_with_players, catalog_group_doubles, admin_user
):
    md, players = matchday_with_players(6)
    created = assign_group_penalty(
        matchday=md,
        trigger_player=players[0],
        catalog_item=catalog_group_doubles,
        double_partner=players[1],
        actor=admin_user,
    )

    assert len(created) == 4  # trigger AND partner excluded
    charged = {row.player_id for row in Penalty.objects.all()}
    assert charged == {player.pk for player in players[2:]}
    assert all(row.group_id == created[0].group_id for row in Penalty.objects.all())
    # Every row names BOTH causing players (they are why the others pay).
    expected = f"{catalog_group_doubles.description} — {players[0].name} & {players[1].name}"
    assert all(row.description_snapshot == expected for row in Penalty.objects.all())
    entries = list(AuditLog.objects.filter(action=AuditAction.PENALTY_ASSIGNED))
    assert len(entries) == 4
    assert all(entry.metadata["double_partner_pk"] == players[1].pk for entry in entries)


def test_group_penalty_with_partner_needs_three_participants(
    matchday_with_players, catalog_group_doubles, admin_user
):
    """Two players = trigger + partner — nobody remains to be charged."""
    md, players = matchday_with_players(2)
    with pytest.raises(DjangoValidationError):
        assign_group_penalty(
            matchday=md,
            trigger_player=players[0],
            catalog_item=catalog_group_doubles,
            double_partner=players[1],
            actor=admin_user,
        )
    assert Penalty.objects.count() == 0


def test_group_snapshot_names_the_causing_player(matchday_with_players, catalog_group, admin_user):
    """The rows state WHO caused the penalty — the others pay because of them."""
    md, players = matchday_with_players(3)
    assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=admin_user
    )
    expected = f"{catalog_group.description} — {players[0].name}"
    assert all(row.description_snapshot == expected for row in Penalty.objects.all())
    # The thrower themself is NOT charged — only named.
    assert not Penalty.objects.filter(player=players[0]).exists()


def test_group_snapshot_keeps_names_when_catalog_text_is_long(matchday_with_players, admin_user):
    """description_snapshot is CharField(255): trim the text, never the names."""
    from app.penalties.models import PenaltyCatalogItem, PenaltyType

    md, players = matchday_with_players(3)
    item = PenaltyCatalogItem.objects.create(
        description="H" * 250,
        amount_eur=1,
        type=PenaltyType.PER_ALL_OTHER_MATCHDAY_PLAYERS,
    )
    assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=item, actor=admin_user
    )

    texts = set(Penalty.objects.values_list("description_snapshot", flat=True))
    assert len(texts) == 1  # every row carries the same snapshot
    text = texts.pop()
    assert len(text) <= 255
    assert text.endswith(f"— {players[0].name}")


def test_doubles_pair_edit_and_delete_hit_both_rows(
    matchday_with_players, catalog_doubles, admin_user
):
    md, players = matchday_with_players(3)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_doubles,
        description_snapshot="Lowdart",
        double_partner=players[1],
        actor=admin_user,
    )

    edit_penalty(penalty=penalty, actor=admin_user, amount_eur=10)
    assert all(row.amount_eur == Decimal(10) for row in Penalty.objects.all())

    soft_delete_penalty(penalty=penalty, actor=admin_user)
    assert Penalty.objects.count() == 0
    assert Penalty.all_objects().count() == 2
