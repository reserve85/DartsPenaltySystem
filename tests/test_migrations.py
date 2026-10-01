"""Migrations — no missing migrations + the LIVE upgrade path (data safety)."""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", check=True, dry_run=True)


# penalties leaf BEFORE the cashier column existed (review B1/B5).
PRE_RECEIVED_BY = ("penalties", "0008_penaltycatalogitem_affects_both_players")


@pytest.mark.django_db(transaction=True)
def test_upgrade_backfills_received_by_for_existing_payments(team, player):
    """v1.2.0 -> HEAD must survive payment rows that predate ``received_by``.

    Rolls the schema back to the pre-Kasse state, writes two legacy payments
    (one with a recorder, one whose recorder was deleted) and migrates forward
    again — the exact path ``entrypoint.sh`` runs on a live system. The
    original migration crashed here with ``NOT NULL constraint failed:
    new__penalties_payment.received_by_id`` and took the container down.
    """
    from django.contrib.auth import get_user_model

    from app.penalties.models import Payment

    User = get_user_model()
    recorder = User.objects.create_user(
        email="legacy-recorder@example.com", password="pw", approval_status="approved"
    )
    with_recorder = Payment.objects.create(
        player=player,
        team=team,
        amount_eur="5.00",
        created_by=recorder,
        received_by=recorder,
    )
    orphaned_recorder = Payment.objects.create(
        player=player,
        team=team,
        amount_eur="2.00",
        created_by=None,  # recorder hard-deleted in the past (SET_NULL)
        received_by=recorder,
    )
    legacy_ids = [with_recorder.pk, orphaned_recorder.pk]

    executor = MigrationExecutor(connection)
    executor.migrate([PRE_RECEIVED_BY])  # unapplies 0011, 0010, 0009
    try:
        # The rows now sit on the pre-received_by schema (column dropped).
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())  # forward to head
    finally:
        # ALWAYS leave the test DB at the newest schema for the tests after.
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    rows = {row.pk: row for row in Payment.objects.filter(pk__in=legacy_ids)}
    assert set(rows) == set(legacy_ids)  # both rows survived the upgrade
    # backfill 1: the created_by snapshot is the historical receiver …
    assert rows[with_recorder.pk].received_by_id == recorder.pk
    # backfill 2: orphaned rows fall back to a surviving account …
    assert rows[orphaned_recorder.pk].received_by_id == recorder.pk

    # … and the model invariant is enforced at SCHEMA level again.
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA table_info(penalties_payment)")
            columns = {row[1]: row for row in cursor.fetchall()}
        assert columns["received_by_id"][3] == 1  # notnull flag
