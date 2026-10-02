"""Outbox + digest — the fix for "5 penalties -> 5 e-mails".

Every NEW penalty is queued (never mailed row by row) and ``flush_outbox()``
turns everything that is due for the SAME user into ONE message. These tests
pin the whole contract:

* new accounts default to the 08:00 digest,
* daily mode waits for the digest time, quick mode coalesces in its window,
* opt-out, soft-deleted rows and inactive recipients never mail,
* a settings change (Quick / digest time) RESCHEDULES already-queued rows —
  otherwise switching to Quick left them waiting for tomorrow's digest slot,
* a skip at flush time is AUDITED (never a silent black hole: the claim
  finalizes the rows, so the audit entry is the only trace of a due batch
  that deliberately produced no e-mail),
* the atomic claim makes the scheduler thread and the management command
  idempotent (never a second copy),
* a dead SMTP retries and is abandoned after ``MAX_SEND_ATTEMPTS`` — and a
  rendering crash AFTER the claim follows the very same retry path (B4),
* ``prune_outbox()`` bounds the table: delivered rows are dropped after
  ``SENT_RETENTION_DAYS``, pending ones never (L4).

``test_notifications.py`` / ``test_email_audit.py`` keep covering the delivery
CONTENT of a single penalty (they link users as ``immediate``); this module
owns the collecting behaviour.
"""

import io
import logging
import re
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from app.core.choices import PenaltyNotifyChoice
from app.core.models import AuditAction, AuditLog
from app.notifications.models import NotificationOutbox
from app.notifications.services import (
    SENT_RETENTION_DAYS,
    _quiet_window_end,
    _send_after_for,
    flush_outbox,
    notification_service,
    prune_outbox,
)
from app.penalties.services import assign_penalty, record_payment, soft_delete_penalty

pytestmark = pytest.mark.django_db

User = get_user_model()


def _local(year, month, day, hour, minute):
    """Aware datetime on the project's wall clock (``TIME_ZONE``, Europe/Berlin).

    ``tzinfo`` is passed explicitly — naive ``datetime()`` values would be
    interpreted in UTC by ``timezone.localtime`` and break the intent of these
    time-of-day assertions.
    """
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(django_settings.TIME_ZONE))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _link(player, *, mode, email="member@example.com", **kwargs):
    """User account linked to ``player`` in the given notification mode."""
    kwargs.setdefault("approval_status", "approved")
    user = User.objects.create_user(email=email, password="pw", penalty_notify_mode=mode, **kwargs)
    user.player_link = player
    user.save()
    return user


def _assign(md, player, catalog, admin_user, description="Late arrival"):
    """Assign one penalty (a NORMAL item always charges the catalog amount)."""
    return assign_penalty(
        matchday=md,
        player=player,
        catalog_item=catalog,
        description_snapshot=description,
        actor=admin_user,
    )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def test_new_accounts_default_to_the_morning_digest(db):
    user = User.objects.create_user(email="fresh@example.com", password="pw")
    assert user.penalty_notify_mode == PenaltyNotifyChoice.DAILY
    assert user.penalty_notify_time == dt_time(8, 0)
    # Payment receipts on by default, club news subscribed by default.
    assert user.repayment_notify is True
    assert user.club_news_optin is True


# ---------------------------------------------------------------------------
# Daily digest — collect, then send ONE mail
# ---------------------------------------------------------------------------
def test_digest_mode_queues_and_sends_one_mail_for_several_penalties(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _link(players[0], mode="daily")

    # NOTE: a NORMAL catalog item always charges the catalog amount (5.00 €) —
    # a passed-in amount_eur is ignored by design.
    for _ in range(3):
        _assign(md, players[0], catalog_normal, admin_user)

    # Nothing is due before the digest time …
    assert len(mail.outbox) == 0
    pending = NotificationOutbox.objects.filter(sent_at__isnull=True)
    assert pending.count() == 3  # one row per save operation …
    assert all(row.send_after > timezone.now() for row in pending)
    assert flush_outbox() == 0
    assert len(mail.outbox) == 0

    # … and the next flush delivers ONE message with all three positions.
    assert flush_outbox(now=timezone.now() + timedelta(days=1)) == 1
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["member@example.com"]
    assert re.search(r"(New penalties|Neue Strafen): 3 \(15\.00 €\)", message.subject)
    assert message.body.count("Late arrival") == 3  # every position is listed
    assert any(
        part.count("Late arrival") == 3
        for part, mimetype in message.alternatives
        if mimetype == "text/html"
    )
    assert NotificationOutbox.objects.filter(sent_at__isnull=True).count() == 0


def test_digest_mode_uses_the_users_configured_time(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    user = _link(players[0], mode="daily")
    user.penalty_notify_time = dt_time(6, 30)
    user.save(update_fields=["penalty_notify_time"])

    _assign(md, players[0], catalog_normal, admin_user)

    row = NotificationOutbox.objects.get()
    local = timezone.localtime(row.send_after)
    assert local.time() == dt_time(6, 30)  # next slot, local wall clock
    assert row.send_after > timezone.now()


def test_past_digest_time_today_is_scheduled_for_the_next_occurrence(db):
    now = _local(2026, 3, 3, 10, 15)
    user = User.objects.create_user(
        email="morning@example.com",
        password="pw",
        approval_status="approved",
        penalty_notify_time=dt_time(8, 0),
    )
    target = timezone.localtime(_send_after_for(user, PenaltyNotifyChoice.DAILY, now))
    assert target.date() == date(2026, 3, 4)  # 08:00 already past
    assert target.time() == dt_time(8, 0)


# ---------------------------------------------------------------------------
# Quick mode — coalescing window
# ---------------------------------------------------------------------------
def test_quick_mode_collects_a_rapid_batch_into_one_mail(
    settings, matchday_with_players, catalog_normal, admin_user
):
    settings.NOTIFICATION_COALESCE_SECONDS = 90
    md, players = matchday_with_players(2)
    _link(players[0], mode="immediate")

    for _ in range(5):  # five entries made in a row
        _assign(md, players[0], catalog_normal, admin_user)

    assert len(mail.outbox) == 0  # the window is still open
    assert NotificationOutbox.objects.filter(sent_at__isnull=True).count() == 5

    assert flush_outbox(now=timezone.now() + timedelta(seconds=91)) == 1
    assert len(mail.outbox) == 1  # ONE e-mail instead of five
    assert mail.outbox[0].body.count("Late arrival") == 5


def test_quick_mode_without_a_window_sends_immediately(
    matchday_with_players, catalog_normal, admin_user
):
    """TESTING forces ``NOTIFICATION_COALESCE_SECONDS = 0`` (see settings)."""
    from django.conf import settings as django_settings

    assert django_settings.NOTIFICATION_COALESCE_SECONDS == 0
    md, players = matchday_with_players(2)
    _link(players[0], mode="immediate")

    _assign(md, players[0], catalog_normal, admin_user)

    assert len(mail.outbox) == 1
    assert NotificationOutbox.objects.filter(sent_at__isnull=True).count() == 0


def test_quiet_window_delays_quick_messages_until_morning(settings):
    settings.NOTIFICATION_QUIET_HOURS = "22:00-07:00"
    settings.NOTIFICATION_COALESCE_SECONDS = 90
    user = User.objects.create_user(
        email="night@example.com",
        password="pw",
        approval_status="approved",
        penalty_notify_mode=PenaltyNotifyChoice.IMMEDIATE,
    )
    night = _local(2026, 1, 10, 23, 10)
    send_after = timezone.localtime(_send_after_for(user, PenaltyNotifyChoice.IMMEDIATE, night))
    assert send_after.hour == 7 and send_after.day == 11  # window ends 07:00

    # Outside the window the coalescing deadline is untouched.
    noon = _local(2026, 1, 10, 12, 0)
    plain = timezone.localtime(_send_after_for(user, PenaltyNotifyChoice.IMMEDIATE, noon))
    assert plain.hour == 12 and plain.minute == 1


# ---------------------------------------------------------------------------
# Opt-outs and eligibility at flush time
# ---------------------------------------------------------------------------
def test_opted_out_player_is_neither_queued_nor_mailed(
    caplog, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _link(players[0], mode="off")

    with caplog.at_level(logging.INFO, logger="app.notifications"):
        _assign(md, players[0], catalog_normal, admin_user)

    assert NotificationOutbox.objects.count() == 0
    assert len(mail.outbox) == 0
    assert any("opted out" in record.message for record in caplog.records)


def test_soft_deleted_rows_produce_no_mail_at_flush_time(
    settings, matchday_with_players, catalog_normal, admin_user
):
    settings.NOTIFICATION_COALESCE_SECONDS = 90  # keep the batch queued for now
    md, players = matchday_with_players(2)
    _link(players[0], mode="immediate")
    first = _assign(md, players[0], catalog_normal, admin_user)
    second = _assign(md, players[0], catalog_normal, admin_user)

    soft_delete_penalty(penalty=first, actor=admin_user, scope="single")
    soft_delete_penalty(penalty=second, actor=admin_user, scope="single")

    # The coalescing window has passed, but both rows are deleted -> no mail.
    assert flush_outbox(now=timezone.now() + timedelta(seconds=91)) == 0
    assert len(mail.outbox) == 0
    # The rows are finalized — nothing retries forever.
    assert NotificationOutbox.objects.filter(sent_at__isnull=True).count() == 0


def test_recipient_deactivated_before_the_flush_is_skipped(
    settings, matchday_with_players, catalog_normal, admin_user
):
    settings.NOTIFICATION_COALESCE_SECONDS = 90
    md, players = matchday_with_players(2)
    user = _link(players[0], mode="immediate")
    _assign(md, players[0], catalog_normal, admin_user)

    user.is_active = False
    user.save(update_fields=["is_active"])

    assert flush_outbox(now=timezone.now() + timedelta(seconds=91)) == 0
    assert len(mail.outbox) == 0
    # Claimed once, then skipped — the row does not linger as "pending".
    assert NotificationOutbox.objects.filter(sent_at__isnull=True).count() == 0


def test_skipped_batch_is_audited_and_marked(
    settings, matchday_with_players, catalog_normal, admin_user
):
    """A due batch whose penalties were deleted must leave an audit trail.

    Live bug report 10/2026: both rows were flushed punctually (digest time /
    coalescing window), but every attached penalty had been soft-deleted in
    between — the flush skipped silently, so the audit log showed NOTHING at
    the digest time and the delivery pipeline looked dead. Now the claim is
    followed by an ``EMAIL_FAILED`` row (reason in the metadata) and the
    outbox rows carry the same reason in ``last_error`` — still no mail, still
    no retry loop.
    """
    settings.NOTIFICATION_COALESCE_SECONDS = 90  # keep the batch queued for now
    md, players = matchday_with_players(2)
    _link(players[0], mode="immediate")
    first = _assign(md, players[0], catalog_normal, admin_user)
    second = _assign(md, players[0], catalog_normal, admin_user)

    soft_delete_penalty(penalty=first, actor=admin_user, scope="single")
    soft_delete_penalty(penalty=second, actor=admin_user, scope="single")

    assert flush_outbox(now=timezone.now() + timedelta(seconds=91)) == 0
    assert len(mail.outbox) == 0  # still no mail — the penalties are gone

    # Claimed once (never retried), and both rows say WHY nothing went out …
    assert NotificationOutbox.objects.filter(sent_at__isnull=True).count() == 0
    for row in NotificationOutbox.objects.all():
        assert "deleted" in row.last_error
    # … and the audit log names the withheld batch (two rows -> ONE digest).
    entry = AuditLog.objects.get(action=AuditAction.EMAIL_FAILED)
    assert entry.metadata["success"] is False
    assert "deleted" in entry.metadata["error"]
    assert entry.metadata["template"] == "emails/penalty_digest"
    assert entry.metadata["recipients"] == ["member@example.com"]


# ---------------------------------------------------------------------------
# Claim / retry — the scheduler thread and cron never double-send
# ---------------------------------------------------------------------------
def test_flushing_twice_never_sends_a_second_copy(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _link(players[0], mode="immediate")
    _assign(md, players[0], catalog_normal, admin_user)

    assert len(mail.outbox) == 1
    assert flush_outbox() == 0  # already claimed
    assert len(mail.outbox) == 1


def test_failed_delivery_is_retried_then_abandoned(
    settings, matchday_with_players, catalog_normal, admin_user
):
    settings.NOTIFICATION_COALESCE_SECONDS = 90
    md, players = matchday_with_players(2)
    _link(players[0], mode="immediate")
    _assign(md, players[0], catalog_normal, admin_user)
    assert len(mail.outbox) == 0  # window still open
    NotificationOutbox.objects.update(send_after=timezone.now())  # due now

    with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("boom")):
        assert flush_outbox() == 0
    row = NotificationOutbox.objects.get()
    assert row.sent_at is None  # unclaimed -> the next flush retries
    assert row.attempts == 1
    assert row.last_error == "delivery failed"

    for _ in range(2):
        with mock.patch(
            "django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("boom")
        ):
            flush_outbox()
    row.refresh_from_db()
    assert row.attempts == 3
    assert row.sent_at is not None  # abandoned, no infinite retry loop
    assert "after 3 attempts" in row.last_error


def test_management_command_flushes_due_rows_and_is_idempotent(
    settings, matchday_with_players, catalog_normal, admin_user
):
    settings.NOTIFICATION_COALESCE_SECONDS = 90
    md, players = matchday_with_players(2)
    _link(players[0], mode="immediate")
    _assign(md, players[0], catalog_normal, admin_user)
    NotificationOutbox.objects.update(send_after=timezone.now() - timedelta(minutes=1))

    out = io.StringIO()
    call_command("flush_notification_outbox", stdout=out)
    assert "1 notification(s) sent" in out.getvalue()
    assert len(mail.outbox) == 1

    call_command("flush_notification_outbox", stdout=io.StringIO())
    assert len(mail.outbox) == 1  # second run finds nothing due


# ---------------------------------------------------------------------------
# Payment receipts + club news subscriptions
# ---------------------------------------------------------------------------
def test_payment_confirmation_can_be_switched_off(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    user = _link(players[0], mode="immediate")
    user.repayment_notify = False
    user.save(update_fields=["repayment_notify"])
    # The cashier evaluates THEIR OWN switch separately (decision 3) — both off:
    admin_user.repayment_notify = False
    admin_user.save(update_fields=["repayment_notify"])
    _assign(md, players[0], catalog_normal, admin_user)
    mail.outbox.clear()

    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)

    assert len(mail.outbox) == 0


def test_payment_confirmation_is_sent_when_subscribed(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    _link(players[0], mode="daily")  # the digest does NOT delay a receipt
    _assign(md, players[0], catalog_normal, admin_user)
    mail.outbox.clear()

    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)

    assert len(mail.outbox) == 2  # payer + cashier — one confirmation each
    assert all(
        re.search(r"(Payment confirmation|Bestätigung für die Zahlung): 2\.00 €", m.subject)
        for m in mail.outbox
    )


def test_club_news_recipients_follow_the_subscription(db):
    subscribed = User.objects.create_user(email="yes@example.com", password="pw")
    User.objects.create_user(email="no@example.com", password="pw", club_news_optin=False)
    User.objects.create_user(email="inactive@example.com", password="pw", is_active=False)
    assert [user.email for user in notification_service.club_news_recipients()] == [
        subscribed.email
    ]


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------
def test_settings_page_renders_the_notification_card(admin_client):
    html = admin_client.get(reverse("accounts:settings")).content.decode()
    assert 'name="penalty_notify_mode"' in html
    assert 'name="penalty_notify_time"' in html
    assert 'name="repayment_notify"' in html
    assert 'name="club_news_optin"' in html
    assert 'id="digest-time-row"' in html


# ---------------------------------------------------------------------------
# Settings changes RESCHEDULE what is already queued
# ---------------------------------------------------------------------------
def _settings_payload(mode: str, digest_time: str) -> dict:
    """POST payload of the Settings card (every field is required)."""
    return {
        "preferred_language": "de",
        "preferred_theme": "auto",
        "penalty_notify_mode": mode,
        "penalty_notify_time": digest_time,
        "repayment_notify": "True",
        "club_news_optin": "True",
    }


def _logged_in_client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_switching_to_quick_reschedules_the_pending_rows(
    settings, matchday_with_players, catalog_normal, admin_user
):
    """A queued digest row must FOLLOW a switch to "Quick (collected)".

    Rows are queued under the OLD mode — without ``reschedule_pending`` they
    kept waiting for tomorrow's digest slot, so switching to "sofort" touched
    nothing that was already pending and no mail ever arrived (live report
    10/2026).
    """
    settings.NOTIFICATION_COALESCE_SECONDS = 90
    md, players = matchday_with_players(2)
    user = _link(players[0], mode="daily")
    _assign(md, players[0], catalog_normal, admin_user)

    row = NotificationOutbox.objects.get()
    assert row.send_after > timezone.now() + timedelta(hours=1)  # tomorrow's slot
    assert flush_outbox() == 0  # nothing due yet — the old bug in a nutshell

    response = _logged_in_client(user).post(
        reverse("accounts:settings"), _settings_payload("immediate", "08:00")
    )
    assert response.status_code == 302

    row.refresh_from_db()
    # The row now falls into the coalescing window instead of tomorrow …
    assert row.send_after <= timezone.now() + timedelta(seconds=95)
    assert flush_outbox(now=row.send_after) == 1
    assert len(mail.outbox) == 1  # … and the batch is actually delivered


def test_moving_the_digest_time_reschedules_the_pending_rows(
    matchday_with_players, catalog_normal, admin_user
):
    """Already-queued rows follow a moved digest time — same live report.

    "I pushed the digest one minute further" used to affect only FUTURE
    queues; the pending rows kept the old (already passed) slot until the
    next day.
    """
    md, players = matchday_with_players(2)
    user = _link(players[0], mode="daily")  # default digest: 08:00
    _assign(md, players[0], catalog_normal, admin_user)

    row = NotificationOutbox.objects.get()
    assert timezone.localtime(row.send_after).time() == dt_time(8, 0)

    response = _logged_in_client(user).post(
        reverse("accounts:settings"), _settings_payload("daily", "23:30")
    )
    assert response.status_code == 302

    row.refresh_from_db()
    # Today or tomorrow — either way the NEW time-of-day, never the old 08:00.
    assert timezone.localtime(row.send_after).time() == dt_time(23, 30)
    assert row.send_after > timezone.now()


def test_switching_off_keeps_rows_queued_and_skips_them_audited(
    settings, matchday_with_players, catalog_normal, admin_user
):
    """Opting out never mails — but the due batch still leaves a trace.

    ``OFF`` deliberately does NOT reschedule (the rows stay queued and are
    skipped at their original due time), and that skip is audited instead of
    vanishing silently.
    """
    settings.NOTIFICATION_COALESCE_SECONDS = 90
    md, players = matchday_with_players(2)
    user = _link(players[0], mode="immediate")
    _assign(md, players[0], catalog_normal, admin_user)

    response = _logged_in_client(user).post(
        reverse("accounts:settings"), _settings_payload("off", "08:00")
    )
    assert response.status_code == 302

    row = NotificationOutbox.objects.get()
    assert row.sent_at is None  # still queued, due time untouched
    assert flush_outbox(now=timezone.now() + timedelta(seconds=91)) == 0

    assert len(mail.outbox) == 0  # opted out — no mail
    row.refresh_from_db()
    assert row.sent_at is not None  # claimed once, no endless retries
    assert "opted out" in row.last_error
    entry = AuditLog.objects.get(action=AuditAction.EMAIL_FAILED)
    assert entry.metadata["success"] is False
    assert "opted out" in entry.metadata["error"]


# ---------------------------------------------------------------------------
# Scheduler trigger + quiet window parsing
# ---------------------------------------------------------------------------
def test_scheduler_stays_out_of_one_shot_management_commands(monkeypatch):
    """``manage.py <cmd>`` must not spawn the 30 s flusher thread."""
    from app.notifications import scheduler

    monkeypatch.setattr(scheduler.sys, "argv", ["manage.py", "migrate"])
    assert scheduler._needs_flusher() is False
    monkeypatch.setattr(scheduler.sys, "argv", ["manage.py", "runserver"])
    assert scheduler._needs_flusher() is True  # the dev server lives on
    monkeypatch.setattr(scheduler.sys, "argv", ["gunicorn", "config.wsgi:application"])
    assert scheduler._needs_flusher() is True
    monkeypatch.setattr(scheduler.sys, "argv", [])
    assert scheduler._needs_flusher() is True

    monkeypatch.setattr(scheduler.sys, "argv", ["manage.py", "flush_notification_outbox"])
    assert scheduler.start_scheduler() is None  # no thread, no side effect


def test_malformed_quiet_window_is_ignored(settings, caplog):
    settings.NOTIFICATION_QUIET_HOURS = "quarter to seven"
    with caplog.at_level(logging.WARNING, logger="app.notifications"):
        assert _quiet_window_end() is None
    assert any("NOTIFICATION_QUIET_HOURS" in record.message for record in caplog.records)

    settings.NOTIFICATION_QUIET_HOURS = "22:00-22:00"  # start == end -> no window
    assert _quiet_window_end() is None
    settings.NOTIFICATION_QUIET_HOURS = ""
    assert _quiet_window_end() is None


def test_rendering_crash_after_claim_is_retried_not_lost(
    settings, caplog, matchday_with_players, catalog_normal, admin_user
):
    """Review B4: an exception AFTER the claim must unclaim like a failed send.

    The row may never stay marked ``sent_at`` forever — a rendering/context
    bug would otherwise silently eat the notification instead of retrying it
    up to ``MAX_SEND_ATTEMPTS``.
    """
    settings.NOTIFICATION_COALESCE_SECONDS = 90
    md, players = matchday_with_players(2)
    _link(players[0], mode="immediate")
    _assign(md, players[0], catalog_normal, admin_user)
    NotificationOutbox.objects.update(send_after=timezone.now())  # due now

    with (
        mock.patch(
            "app.notifications.services.notification_service.send_penalty_created",
            side_effect=RuntimeError("context exploded"),
        ),
        caplog.at_level(logging.ERROR, logger="app.notifications"),
    ):
        assert flush_outbox() == 0

    assert any("crashed" in record.message for record in caplog.records)
    row = NotificationOutbox.objects.get()
    assert row.sent_at is None  # unclaimed -> the next flush retries
    assert row.attempts == 1
    assert len(mail.outbox) == 0

    # the next flush delivers normally (same contract as the SMTP-failure path)
    assert flush_outbox() == 1
    row.refresh_from_db()
    assert row.sent_at is not None
    assert row.attempts == 2
    assert len(mail.outbox) == 1


def test_prune_outbox_drops_only_old_delivered_rows(
    db, matchday_with_players, catalog_normal, admin_user
):
    """Review L4: delivered rows age out, pending and fresh ones never do.

    The returned count is the number of OUTBOX ROWS even when the stale row
    carries attached penalties (whose M2M through-rows cascade-delete too).
    """
    user = User.objects.create_user(email="prune@example.com", password="pw")
    # Assign BEFORE the rows exist — the flush inside _assign must not claim
    # the pending row we want to keep untouched.
    md, players = matchday_with_players(2)
    penalty = _assign(md, players[1], catalog_normal, admin_user)
    now = timezone.now()
    old_sent = NotificationOutbox.objects.create(
        user=user,
        email=user.email,
        send_after=now - timedelta(days=SENT_RETENTION_DAYS + 5),
        sent_at=now - timedelta(days=SENT_RETENTION_DAYS + 1),
    )
    fresh_sent = NotificationOutbox.objects.create(
        user=user,
        email=user.email,
        send_after=now - timedelta(hours=1),
        sent_at=now - timedelta(hours=1),
    )
    pending_old = NotificationOutbox.objects.create(
        user=user,
        email=user.email,
        send_after=now - timedelta(days=99),  # never claimed -> must survive
    )
    old_sent.penalties.set([penalty])

    assert prune_outbox() == 1  # ONE row, not row + M2M through-rows
    assert set(NotificationOutbox.objects.values_list("pk", flat=True)) == {
        fresh_sent.pk,
        pending_old.pk,
    }
    assert old_sent.pk not in NotificationOutbox.objects.values_list("pk", flat=True)
    assert prune_outbox() == 0  # idempotent
