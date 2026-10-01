"""Payments — partial payments against a player's team debt (service, views, UI)."""

import re
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
def test_partial_payment_reduces_balance(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)  # 5.00 € owed

    payment = record_payment(player=players[0], team=md.team, amount_eur=3, actor=admin_user)

    assert player_balance(players[0]) == Decimal(2)
    assert player_total_paid(players[0]) == Decimal(3)
    assert Payment.objects.count() == 1
    assert payment.created_by == admin_user
    assert payment.received_by == cashier  # snapshot: the team's cashier
    entry = AuditLog.objects.get(action=AuditAction.PAYMENT_RECORDED, target_id=payment.pk)
    assert entry.user == admin_user
    assert entry.metadata["amount_eur"] == "3"
    assert entry.metadata["received_by_id"] == cashier.pk


def test_full_payment_clears_balance(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    record_payment(player=players[0], team=md.team, amount_eur=5, actor=admin_user)
    assert player_balance(players[0]) == Decimal(0)


def test_several_partial_payments_stack(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)  # 5 €
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)
    assert player_balance(players[0]) == Decimal(1)
    assert player_total_paid(players[0]) == Decimal(4)


def test_overpayment_creates_credit(matchday_with_players, catalog_normal, admin_user, cashier):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)  # 5 €
    record_payment(player=players[0], team=md.team, amount_eur=7, actor=admin_user)
    assert player_balance(players[0]) == Decimal(-2)


def test_record_payment_without_cashier_is_refused(team, admin_user):
    """No payment without a receiver: no usable cashier -> ValidationError."""
    from app.players.models import Player

    player = Player.objects.create(name="P", team=team)
    with pytest.raises(ValidationError, match="cashier|Kassier"):
        record_payment(player=player, team=team, amount_eur=3, actor=admin_user)
    assert Payment.objects.count() == 0


def test_record_payment_requires_positive_amount(team, admin_user, cashier):
    from app.players.models import Player

    player = Player.objects.create(name="P", team=team)
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            record_payment(player=player, team=team, amount_eur=bad, actor=admin_user)
    assert Payment.objects.count() == 0


def test_payment_is_team_scoped(
    team, other_team, catalog_normal, admin_user, matchday_with_players
):
    from app.teams.services import set_cashier

    set_cashier(team=other_team, user=admin_user, actor=admin_user)  # receiver for the payment
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)  # debt on ``team`` matchdays
    # Payment recorded against the OTHER team's pot …
    record_payment(player=players[0], team=other_team, amount_eur=5, actor=admin_user)
    # … does NOT reduce the scoped debt for ``team`` …
    assert player_team_balance(players[0], team) == Decimal(5)
    # … but the global balance nets it out.
    assert player_balance(players[0]) == Decimal(0)


def test_delete_payment_restores_balance(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    payment = record_payment(player=players[0], team=md.team, amount_eur=5, actor=admin_user)
    assert player_balance(players[0]) == Decimal(0)

    delete_payment(payment=payment, actor=admin_user)

    assert Payment.objects.count() == 0
    assert player_balance(players[0]) == Decimal(5)
    assert AuditLog.objects.filter(action=AuditAction.PAYMENT_DELETED).count() == 1


def test_team_balances_carry_paid_attribute(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)

    rows = {row.pk: row for row in team_balances(md.team)}
    assert rows[players[0].pk].balance == Decimal(3)
    assert rows[players[0].pk].paid == Decimal(2)


def test_matchday_totals_stay_gross(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    """Matchday totals are historical gross sums — payments do not shrink them."""
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    record_payment(player=players[0], team=md.team, amount_eur=5, actor=admin_user)
    assert {m.pk: m.total for m in matchday_totals(md.team)}[md.pk] == Decimal(5)


# ---------------------------------------------------------------------------
# Views + permission matrix
# ---------------------------------------------------------------------------
def test_admin_records_payment_via_view(
    admin_client, matchday_with_players, catalog_normal, admin_user, cashier
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


def test_admin_without_cashier_role_gets_403(
    admin_client, matchday_with_players, catalog_normal, admin_user, captain_user
):
    """Decision 7: admins included — somebody else is the cashier -> 403."""
    from app.teams.services import set_cashier

    md, players = matchday_with_players(2)
    set_cashier(team=md.team, user=captain_user, actor=captain_user)
    _make_penalty(md, players[0], catalog_normal, admin_user)

    response = admin_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": players[0].pk, "amount_eur": "4"},
    )
    assert response.status_code == 403
    assert Payment.objects.count() == 0


def test_captain_records_payment_for_own_team(
    captain_client, captain_user, matchday_with_players, catalog_normal, admin_user
):
    from app.teams.services import set_cashier

    md, players = matchday_with_players(2)
    set_cashier(team=md.team, user=captain_user, actor=captain_user)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    response = captain_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": players[0].pk, "amount_eur": "5"},
    )
    assert response.status_code == 302
    assert Payment.objects.count() == 1
    assert player_balance(players[0]) == Decimal(0)


def test_captain_who_is_not_cashier_gets_403(
    captain_client, matchday_with_players, catalog_normal, admin_user, cashier
):
    """A manager who is NOT the cashier is refused (the friendly 403 path)."""
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    response = captain_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": players[0].pk, "amount_eur": "5"},
    )
    assert response.status_code == 403
    assert Payment.objects.count() == 0


def test_payment_create_without_cashier_answers_302_with_message(
    admin_client, matchday_with_players, catalog_normal, admin_user
):
    """Review M1: friendly failure path — message + redirect, NOT 403."""
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    assert admin_client.get(reverse("dashboard:financial_overview")).status_code == 200

    response = admin_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": players[0].pk, "amount_eur": "5"},
    )
    assert response.status_code == 302
    assert Payment.objects.count() == 0
    follow = admin_client.get(response.url)
    text = follow.content.decode()
    assert "cashier" in text.lower() or "Kassier" in text or "Kasse" in text


def test_cannot_record_payment_for_foreign_team(
    other_captain_client,
    player_client,
    matchday_with_players,
    catalog_normal,
    admin_user,
    cashier,
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
    admin_client, matchday_with_players, catalog_normal, admin_user, cashier
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
    captain_client,
    other_captain_client,
    matchday_with_players,
    catalog_normal,
    admin_user,
    captain_user,
):
    from app.teams.services import set_cashier

    md, players = matchday_with_players(2)
    set_cashier(team=md.team, user=captain_user, actor=captain_user)  # captain IS the cashier
    _make_penalty(md, players[0], catalog_normal, admin_user)
    payment = record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)
    url = reverse("penalties:payment_delete", args=[payment.pk])

    assert other_captain_client.post(url).status_code == 403  # foreign captain
    assert Payment.objects.filter(pk=payment.pk).exists()
    assert captain_client.post(url).status_code == 302  # own team + current cashier
    assert Payment.objects.count() == 0
    assert player_balance(players[0]) == Decimal(5)


def test_payment_delete_denied_for_admin_who_is_not_cashier(
    admin_client, matchday_with_players, catalog_normal, admin_user, captain_user
):
    """Only the CURRENT cashier may revert — the admin needs the role too."""
    from app.teams.services import set_cashier

    md, players = matchday_with_players(2)
    set_cashier(team=md.team, user=captain_user, actor=captain_user)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    payment = record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)

    response = admin_client.post(reverse("penalties:payment_delete", args=[payment.pk]))
    assert response.status_code == 403
    assert Payment.objects.filter(pk=payment.pk).exists()


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
    admin_client, matchday_with_players, catalog_normal, admin_user, cashier
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


def test_financial_overview_uses_one_payment_modal(
    admin_client, matchday_with_players, catalog_normal, admin_user, cashier
):
    """The amount is entered in ONE shared modal — no tiny per-row inputs."""
    md, players = matchday_with_players(2)
    _make_penalty(md, players[0], catalog_normal, admin_user)
    _make_penalty(md, players[1], catalog_normal, admin_user)

    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()

    assert 'id="paymentModal"' in content
    assert 'data-bs-target="#paymentModal"' in content
    # exactly ONE amount field for all rows (the row forms are gone)
    assert content.count('name="amount_eur"') == 1
    # every row opens the modal for ITS player, carrying the open balance
    for player in players:
        assert f'data-player="{player.pk}"' in content
    assert f'data-player-name="{players[0].name}"' in content
    balance = re.search(
        rf'data-player="{players[0].pk}"\s+data-player-name="[^"]*"\s+data-balance="([^"]+)"',
        content,
    )
    assert balance is not None
    assert Decimal(balance.group(1)) == Decimal(5)  # 5 € penalty, nothing paid
    # no-JS fallback: the modal form still posts to the payment endpoint
    assert reverse("penalties:payment_create") in content


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
    player_client, player_user, matchday_with_players, catalog_normal, admin_user, cashier
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
