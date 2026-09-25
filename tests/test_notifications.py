"""Central NotificationService — eligibility rules, content, fault tolerance.

The entire e-mail logic of the project lives in
``app.notifications.services``; these tests pin down the contract:

* players WITHOUT a user account are fully managed but never get e-mails,
* users without a valid/active address are skipped (logged, not raised),
* penalty/repayment e-mails carry the required content,
* missing SMTP configuration is reported instead of breaking a flow.
"""

import logging
import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings

from app.notifications.services import (
    NotificationService,
    eligible_user_for,
    notification_service,
)
from app.penalties.services import assign_penalty, record_payment
from app.players.models import Player

pytestmark = pytest.mark.django_db

User = get_user_model()


def _link_user(player, email="member@example.com", **kwargs):
    """Create a user account linked to ``player`` (approved + active by default)."""
    kwargs.setdefault("approval_status", "approved")
    user = User.objects.create_user(email=email, password="pw", **kwargs)
    user.player_link = player
    user.save()
    return user


# ---------------------------------------------------------------------------
# Eligibility — the project-wide check
# ---------------------------------------------------------------------------
def test_eligible_user_for_player_without_account_returns_none(db, team):
    player = Player.objects.create(name="No Account", team=team)
    assert eligible_user_for(player) is None


def test_eligible_user_for_inactive_user_returns_none(db, team):
    player = Player.objects.create(name="Inactive", team=team)
    user = _link_user(player, is_active=False)
    assert eligible_user_for(player) is None
    assert user.is_active is False


def test_eligible_user_for_user_without_email_returns_none(db, team):
    player = Player.objects.create(name="No Address", team=team)
    user = _link_user(player)
    User.objects.filter(pk=user.pk).update(email="")
    # Fresh instance: queryset .update() bypasses the in-memory relation cache.
    player = Player.objects.get(pk=player.pk)
    assert eligible_user_for(player) is None


def test_eligible_user_for_linked_active_user_returns_user(db, team):
    player = Player.objects.create(name="Has Account", team=team)
    user = _link_user(player)
    assert eligible_user_for(player) == user


def test_missing_prerequisites_are_only_logged(db, team, caplog):
    """No e-mail, no exception — just a log message (spec requirement)."""
    player = Player.objects.create(name="Silent", team=team)
    with caplog.at_level(logging.INFO, logger="app.notifications"):
        assert eligible_user_for(player) is None
    assert any("no linked user account" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Penalty notifications
# ---------------------------------------------------------------------------
def test_penalty_assignment_sends_email_with_required_content(
    matchday_with_players, catalog_normal, admin_user, team
):
    md, players = matchday_with_players(2)
    player = players[0]
    _link_user(player)

    assign_penalty(
        matchday=md,
        player=player,
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["member@example.com"]
    # Subject follows the caller's language (de/en catalog).
    assert "New penalty: 5.00 €" in message.subject or "Neue Strafe: 5.00 €" in message.subject

    body = message.body
    assert re.search(r"5[.,]00", body)  # Betrag + offener Gesamtbetrag (locale-aware)
    assert "Late arrival" in body  # Grund
    # Row label is catalog-localized (de: "Aktueller offener Betrag").
    assert "Current open total" in body or "Aktueller offener Betrag" in body
    assert "24.09.2026" in body  # Datum (matchday fixture date)
    # HTML alternative attached (HTML + plain text required)
    assert any(mimetype == "text/html" for _, mimetype in message.alternatives)
    assert "Late arrival" in message.alternatives[0][0]


def test_penalty_assignment_without_user_account_sends_no_email(
    matchday_with_players, catalog_normal, admin_user, caplog
):
    md, players = matchday_with_players(2)  # player has NO user account
    with caplog.at_level(logging.INFO, logger="app.notifications"):
        assign_penalty(
            matchday=md,
            player=players[0],
            catalog_item=catalog_normal,
            amount_eur=5,
            description_snapshot="Late arrival",
            actor=admin_user,
        )
    assert len(mail.outbox) == 0  # players without accounts stay manageable


def test_penalty_assignment_for_inactive_user_sends_no_email(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _link_user(players[0], is_active=False)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    assert len(mail.outbox) == 0


def test_group_penalty_notifies_every_affected_linked_player(
    matchday_with_players, catalog_group, admin_user, team
):
    md, players = matchday_with_players(3)
    _link_user(players[1], email="one@example.com")
    _link_user(players[2], email="two@example.com")

    from app.penalties.services import assign_group_penalty

    assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=admin_user
    )

    recipients = {addr for m in mail.outbox for addr in m.to}
    assert recipients == {"one@example.com", "two@example.com"}


# ---------------------------------------------------------------------------
# Repayment notifications
# ---------------------------------------------------------------------------
def test_partial_repayment_email_content(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _link_user(players[0])
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )  # 5.00 € owed
    mail.outbox.clear()

    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["member@example.com"]
    # Subject follows the caller's language (de/en catalog).
    assert (
        "Payment received: 2.00 €" in message.subject
        or "Zahlung eingegangen: 2.00 €" in message.subject
    )

    body = message.body
    assert re.search(r"2[.,]00", body)  # getilgter Betrag
    assert "Partial repayment" in body or "Teilzahlung" in body  # Teiltilgung
    assert re.search(r"(Remaining balance|Restbetrag): 3[.,]00", body)  # Restbetrag
    assert "24.09.2026" not in body  # date of the payment, not of the matchday


def test_full_repayment_email_content(matchday_with_players, catalog_normal, admin_user):
    md, players = matchday_with_players(2)
    _link_user(players[0])
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    mail.outbox.clear()

    record_payment(player=players[0], team=md.team, amount_eur=5, actor=admin_user)

    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    assert "Full repayment" in body or "Vollständig beglichen" in body
    assert "Partial repayment" not in body and "Teilzahlung" not in body


def test_repayment_without_user_account_sends_no_email(
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
    mail.outbox.clear()
    record_payment(player=players[0], team=md.team, amount_eur=5, actor=admin_user)
    assert len(mail.outbox) == 0  # no account -> no e-mail, no error


# ---------------------------------------------------------------------------
# Fault tolerance / configuration
# ---------------------------------------------------------------------------
@override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST="")
def test_missing_smtp_configuration_is_logged_and_never_raises(caplog, team):
    player = Player.objects.create(name="Config Test", team=team)
    user = _link_user(player)
    with caplog.at_level(logging.WARNING, logger="app.notifications"):
        sent = notification_service.send_account_approved(user)
    assert sent == 0
    assert len(mail.outbox) == 0
    assert any("EMAIL_HOST" in record.message for record in caplog.records)


def test_round_mail_skips_ineligible_recipients(db, team):
    """Extension point for club info / round mails (future features)."""
    active = User.objects.create_user(
        email="active@example.com", password="pw", approval_status="approved"
    )
    inactive = User.objects.create_user(
        email="inactive@example.com",
        password="pw",
        approval_status="approved",
        is_active=False,
    )
    sent = notification_service.send_to_users(
        [active, inactive, None],
        template_base="emails/account_approved",
        subject="Club info",
        context={"approved_user": active, "login_url": "/accounts/login/"},
    )
    assert sent == 1
    assert [message.to for message in mail.outbox] == [["active@example.com"]]


def test_support_request_goes_to_support_address(db, settings):
    settings.SUPPORT_EMAIL = "support@example.org"
    sent = notification_service.send_support_request(
        name="Max Mustermann", email="max@example.com", message="I cannot log in."
    )
    assert sent == 1
    assert mail.outbox[0].to == ["support@example.org"]
    assert "Max Mustermann" in mail.outbox[0].subject
    assert "I cannot log in." in mail.outbox[0].body


def test_send_templated_rejects_invalid_addresses(db):
    sent = NotificationService().send_templated(
        recipients=["", "not-an-address", None],
        template_base="emails/account_rejected",
        subject="Nope",
        context={"rejected_user": None},
    )
    assert sent == 0
    assert len(mail.outbox) == 0


# ---------------------------------------------------------------------------
# Review fixes: season/team context in mails, season-scoped totals, async flag
# ---------------------------------------------------------------------------
def test_penalty_email_carries_team_and_season(
    matchday_with_players, catalog_normal, admin_user, season
):
    """The recipient must see WHICH team and season a penalty belongs to."""
    md, players = matchday_with_players(2, season=season)
    _link_user(players[0])

    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )

    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    assert md.team.name in body  # "Wildboars 1"
    assert season.name in body  # "2025/2026"


def test_repayment_email_carries_team_and_season(
    matchday_with_players, catalog_normal, admin_user, season
):
    md, players = matchday_with_players(2, season=season)
    _link_user(players[0])
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    mail.outbox.clear()

    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user, season=season)

    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    assert md.team.name in body
    assert season.name in body


def test_penalty_email_open_total_is_season_scoped(
    matchday_with_players, catalog_normal, admin_user, season
):
    """Review fix (M3): the 'open total' only counts the penalty's own season."""
    from datetime import date

    from app.matchdays.models import Matchday, MatchdayPlayer, Season

    other_season = Season.objects.create(name="2026/2027")
    md_a, players = matchday_with_players(2, season=season)
    player = players[0]
    _link_user(player)
    # 5 € open in the OLD season …
    assign_penalty(
        matchday=md_a,
        player=player,
        catalog_item=catalog_normal,
        description_snapshot="Old debt",
        actor=admin_user,
    )
    # … plus a new 5 € penalty in the new season.
    md_b = Matchday.objects.create(
        team=md_a.team, opponent="Next", venue="home", date=date(2026, 9, 25), season=other_season
    )
    MatchdayPlayer.objects.create(matchday=md_b, player=player)
    mail.outbox.clear()

    assign_penalty(
        matchday=md_b,
        player=player,
        catalog_item=catalog_normal,
        description_snapshot="New debt",
        actor=admin_user,
    )

    body = mail.outbox[-1].body
    assert other_season.name in body
    # Season-scoped open total is 5.00 — an unscoped total would show 10.00.
    assert not re.search(r"10[.,]00", body), body


def test_notifications_run_synchronously_under_test():
    """TESTING forces sync delivery so mail.outbox assertions stay reliable."""
    from django.conf import settings

    assert settings.ASYNC_NOTIFICATIONS is False


def test_async_dispatch_still_delivers(monkeypatch, db, team, settings):
    """The ASYNC code path queues the same delivery (thread runs inline here)."""
    from app.notifications.services import notification_service

    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(
        "app.notifications.services.threading.Thread",
        _InlineThread,
    )
    settings.ASYNC_NOTIFICATIONS = True
    player = Player.objects.create(name="Async", team=team)
    user = _link_user(player)

    sent = notification_service.send_account_rejected(user)

    assert sent == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [user.email]
