"""Payments — partial payments against a player's team debt (service, views, UI)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import NoReverseMatch, reverse

from app.core.models import AuditAction, AuditLog
from app.penalties.models import Payment
from app.penalties.services import (
    assign_penalty,
    delete_payment,
    matchday_totals,
    player_balance,
    player_team_balance,
    player_total_paid,
    record_payment,
    team_balances,
)

pytestmark = pytest.mark.django_db


def _make_penalty(matchday, player, catalog_item, actor, description="Late arrival"):
    return assign_penalty(
        matchday=matchday,
        player=player,
        catalog_item=catalog_item,
        description_snapshot=description,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# Service layer — partial payments (25 € owed, 20 € paid -> 5 € left)
# ---------------------------------------------------------------------------
def test_partial_payment_reduces_balance(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)  # 5.00 € owed

    payment = record_payment(player=players[0], team=md.team, amount_eur=3, actor=admin_user)

    assert player_balance(players[0]) == Decimal(2)
    assert player_total_paid(players[0]) == Decimal(3)
    assert Payment.objects.count() == 1
    assert payment.created_by == admin_user
    entry = AuditLog.objects.get(action=AuditAction.PAYMENT_RECORDED, target_id=payment.pk)
    assert entry.user == admin_user
    assert entry.metadata["amount_eur"] == "3"


def test_full_payment_clears_balance(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    record_payment(player=players[0], team=md.team, amount_eur=5, actor=admin_user)
    assert player_balance(players[0]) == Decimal(0)


def test_several_partial_payments_stack(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)  # 5 €
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)
    assert player_balance(players[0]) == Decimal(1)
    assert player_total_paid(players[0]) == Decimal(4)


def test_overpayment_creates_credit(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)  # 5 €
    record_payment(player=players[0], team=md.team, amount_eur=7, actor=admin_user)
    assert player_balance(players[0]) == Decimal(-2)


def test_record_payment_requires_positive_amount(team, admin_user):
    from app.players.models import Player

    player = Player.objects.create(name="P", team=team)
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            record_payment(player=player, team=team, amount_eur=bad, actor=admin_user)
    assert Payment.objects.count() == 0


def test_payment_is_team_scoped(
    team, other_team, catalog_normal, admin_user, matchday_with_players
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)  # debt on ``team`` matchdays
    # Payment recorded against the OTHER team's pot …
    record_payment(player=players[0], team=other_team, amount_eur=5, actor=admin_user)
    # … does NOT reduce the scoped debt for ``team`` …
    assert player_team_balance(players[0], team) == Decimal(5)
    # … but the global balance nets it out.
    assert player_balance(players[0]) == Decimal(0)


def test_delete_payment_restores_balance(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    payment = record_payment(player=players[0], team=md.team, amount_eur=5, actor=admin_user)
    assert player_balance(players[0]) == Decimal(0)

    delete_payment(payment=payment, actor=admin_user)

    assert Payment.objects.count() == 0
    assert player_balance(players[0]) == Decimal(5)
    assert AuditLog.objects.filter(action=AuditAction.PAYMENT_DELETED).count() == 1


def test_team_balances_carry_paid_attribute(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)

    rows = {row.pk: row for row in team_balances(md.team)}
    assert rows[players[0].pk].balance == Decimal(3)
    assert rows[players[0].pk].paid == Decimal(2)


def test_matchday_totals_stay_gross(matchday_with_players, catalog_normal, admin_user):
    """Matchday totals are historical gross sums — payments do not shrink them."""
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    record_payment(player=players[0], team=md.team, amount_eur=5, actor=admin_user)
    assert {m.pk: m.total for m in matchday_totals(md.team)}[md.pk] == Decimal(5)


# ---------------------------------------------------------------------------
# Views + permission matrix
# ---------------------------------------------------------------------------
def test_admin_records_payment_via_view(
    admin_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)

    response = admin_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": players[0].pk, "amount_eur": "4"},
    )
    assert response.status_code == 302
    assert Payment.objects.count() == 1
    assert player_balance(players[0]) == Decimal(1)


def test_captain_records_payment_for_own_team(
    captain_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    response = captain_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": players[0].pk, "amount_eur": "5"},
    )
    assert response.status_code == 302
    assert Payment.objects.count() == 1
    assert player_balance(players[0]) == Decimal(0)


def test_cannot_record_payment_for_foreign_team(
    other_captain_client, player_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    url = reverse("penalties:payment_create")
    data = {"team": md.team.pk, "player": players[0].pk, "amount_eur": "5"}

    assert other_captain_client.post(url, data).status_code == 403
    assert player_client.post(url, data).status_code == 403
    assert Client().post(url, data).status_code == 302  # anonymous -> login
    assert Payment.objects.count() == 0
    assert player_balance(players[0]) == Decimal(5)


def test_invalid_amount_is_rejected(
    admin_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    response = admin_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": players[0].pk, "amount_eur": "0"},
    )
    assert response.status_code == 302  # redirect with an error message
    assert Payment.objects.count() == 0
    assert player_balance(players[0]) == Decimal(5)


def test_delete_payment_via_view(
    captain_client, other_captain_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    payment = record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)
    url = reverse("penalties:payment_delete", args=[payment.pk])

    assert other_captain_client.post(url).status_code == 403  # foreign captain
    assert Payment.objects.filter(pk=payment.pk).exists()
    assert captain_client.post(url).status_code == 302  # own team
    assert Payment.objects.count() == 0
    assert player_balance(players[0]) == Decimal(5)


def test_payment_form_ignores_invalid_data(admin_client, matchday_with_players):
    md, _players = matchday_with_players(2)
    response = admin_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": 999999, "amount_eur": "5"},
    )
    assert response.status_code == 302
    assert Payment.objects.count() == 0


# ---------------------------------------------------------------------------
# UI integration
# ---------------------------------------------------------------------------
def test_financial_overview_renders_payment_form_and_list(
    admin_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)

    # language-independent: the row form posts to the payment endpoint
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    assert reverse("penalties:payment_create") in content
    assert Payment.objects.count() == 0

    admin_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": players[0].pk, "amount_eur": "5"},
    )
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    payment = Payment.objects.get()
    assert reverse("penalties:payment_delete", args=[payment.pk]) in content


def test_matchday_detail_has_no_per_row_payment_actions(
    admin_client, matchday_with_players, catalog_normal, admin_user
):
    """Per-row pay/unpay endpoints no longer exist — payments are per player."""
    md, players = matchday_with_players(2)
    penalty = _make_penalty(md, players[0], catalog_normal, admin_user)

    with pytest.raises(NoReverseMatch):
        reverse("penalties:penalty_pay", args=[penalty.pk])
    with pytest.raises(NoReverseMatch):
        reverse("penalties:penalty_unpay", args=[penalty.pk])
    content = admin_client.get(reverse("matchdays:matchday_detail", args=[md.pk])).content.decode()
    assert reverse("penalties:penalty_update", args=[penalty.pk]) in content  # edit stays


def test_player_financial_view_shows_total_paid(
    player_client, player_user, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)
    player_user.player_link = players[0]
    player_user.save()

    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert response.context["own_balance"] == Decimal(3)
    assert response.context["own_paid"] == Decimal(2)
