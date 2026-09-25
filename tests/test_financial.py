"""Financial read functions — sign/scope (B1/M3), grouping (M1), soft delete."""

from datetime import date
from decimal import Decimal

import pytest

from app.matchdays.models import Matchday, MatchdayPlayer
from app.penalties.services import (
    assign_penalty,
    assigned_penalties_qs,
    inactive_player_balances,
    matchday_totals,
    most_common_penalties,
    player_balance,
    soft_delete_penalty,
    team_balances,
)
from app.players.models import Player

pytestmark = pytest.mark.django_db


def _balances_by_name(rows) -> dict[str, Decimal]:
    return {row.name: row.balance for row in rows}


# ---------------------------------------------------------------------------
# Sign + scope (B1 + M3)
# ---------------------------------------------------------------------------
def test_team_balances_positive_means_debt_and_zero_rows_included(
    matchday_with_players, catalog_normal, admin_user
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
    balances = _balances_by_name(team_balances(md.team))
    # B1: positive balance = the player owes this to the club pot
    assert balances[players[0].name] == Decimal(5)
    # zero-balance active players of the team are still listed
    assert balances[players[1].name] == Decimal(0)
    assert balances[players[2].name] == Decimal(0)
    assert player_balance(players[0]) == Decimal(5)


def test_multi_team_player_in_both_balance_sections(team, other_team):
    both = Player.objects.create(name="Both")
    both.teams.add(team, other_team)
    active_team = {p.name for p in team_balances(team)}
    active_other = {p.name for p in team_balances(other_team)}
    assert "Both" in active_team
    assert "Both" in active_other


def test_totals_scoped_to_matchday_team_after_reassignment(
    team, other_team, catalog_normal, admin_user
):
    """M3: money belongs to the team whose matchday it was (B1 sign scope)."""
    player = Player.objects.create(name="Reassignee", team=team)
    matchday = Matchday.objects.create(
        team=team, opponent="SV X", venue="home", date=date(2026, 1, 10)
    )
    MatchdayPlayer.objects.create(matchday=matchday, player=player)
    assign_penalty(
        matchday=matchday,
        player=player,
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )

    # Player moves to another team — old team keeps the debt
    player.teams.set([other_team])

    assert _balances_by_name(team_balances(team)) == {player.name: Decimal(5)}
    # The new team shows the player with a zero scoped balance (no own matchday debt)
    assert _balances_by_name(team_balances(other_team)) == {player.name: Decimal(0)}

    totals = {md.pk: md.total for md in matchday_totals(team)}
    assert totals[matchday.pk] == Decimal(5)
    assert matchday_totals(other_team) == []
    # Description grouping stays with the originating team
    common = most_common_penalties(team)
    assert common[0]["description_snapshot"] == "Late arrival"
    assert common[0]["count"] == 1
    assert most_common_penalties(other_team) == []


# ---------------------------------------------------------------------------
# Matchday totals + most common (M1)
# ---------------------------------------------------------------------------
def test_matchday_totals_and_most_common_grouping(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(6)
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
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    assign_penalty(
        matchday=md,
        player=players[2],
        catalog_item=catalog_normal,
        description_snapshot="Keks",
        actor=admin_user,
    )

    totals = {m.pk: m.total for m in matchday_totals(md.team)}
    assert totals[md.pk] == Decimal(15)

    common = most_common_penalties(md.team)
    assert common[0] == {"description_snapshot": "Late arrival", "count": 2, "total": Decimal(10)}
    assert common[1] == {"description_snapshot": "Keks", "count": 1, "total": Decimal(5)}
    assert len(most_common_penalties(md.team, limit=1)) == 1


def test_most_common_and_totals_scoped_per_team(team, other_team, catalog_normal, admin_user):
    player_a = Player.objects.create(name="PA", team=team)
    player_b = Player.objects.create(name="PB", team=other_team)
    md_a = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 2, 1))
    md_b = Matchday.objects.create(
        team=other_team, opponent="Y", venue="away", date=date(2026, 2, 2)
    )
    MatchdayPlayer.objects.create(matchday=md_a, player=player_a)
    MatchdayPlayer.objects.create(matchday=md_b, player=player_b)
    for matchday, player in ((md_a, player_a), (md_b, player_b)):
        assign_penalty(
            matchday=matchday,
            player=player,
            catalog_item=catalog_normal,
            amount_eur=5,
            description_snapshot="Late arrival",
            actor=admin_user,
        )

    assert most_common_penalties(team)[0]["count"] == 1
    assert most_common_penalties(other_team)[0]["count"] == 1

    qs_team = assigned_penalties_qs(team)
    assert {p.matchday_id for p in qs_team} == {md_a.pk}
    assert assigned_penalties_qs().count() == 2


# ---------------------------------------------------------------------------
# Soft delete excluded from all financial reads
# ---------------------------------------------------------------------------
def test_soft_deleted_penalties_excluded_from_financial_reads(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    first = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    soft_delete_penalty(penalty=first, actor=admin_user)

    assert player_balance(players[0]) == Decimal(5)
    assert _balances_by_name(team_balances(md.team))[players[0].name] == Decimal(5)
    assert {m.pk: m.total for m in matchday_totals(md.team)}[md.pk] == Decimal(5)
    common = most_common_penalties(md.team)
    assert common[0]["count"] == 1
    assert assigned_penalties_qs(md.team).count() == 1


# ---------------------------------------------------------------------------
# Active / inactive sections (M2)
# ---------------------------------------------------------------------------
def test_inactive_players_move_to_their_own_section(
    matchday_with_players, catalog_normal, admin_user
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
    players[0].active = False
    players[0].save()

    active = _balances_by_name(team_balances(md.team))
    inactive = _balances_by_name(inactive_player_balances(md.team))
    assert players[0].name not in active
    assert players[1].name in active
    assert inactive == {players[0].name: Decimal(5)}
