"""Demo seed (--with-demo) tests — dataset shape, 180 balances, idempotency."""

from decimal import Decimal

import pytest
from django.core.management import call_command

from app.matchdays.models import Matchday
from app.penalties.models import Penalty
from app.penalties.services import player_balance
from app.players.models import Player
from app.teams.models import Team

pytestmark = pytest.mark.django_db


def test_with_demo_creates_dataset(db):
    call_command("bootstrap", "--with-demo")
    assert Team.objects.count() == 2
    assert Player.objects.count() == 8
    assert Matchday.objects.count() == 3
    # demo penalties incl. one group penalty (defaults only seed "Manual")
    assert Penalty.objects.filter(group_id__isnull=False).count() == 3  # 4 players - trigger
    assert Penalty.objects.filter(group_id__isnull=True).count() == 2  # normal + manual


def test_with_demo_group_penalty_balances(db):
    call_command("bootstrap", "--with-demo")
    team1 = Team.objects.get(name="Wildboars 1")
    team2 = Team.objects.get(name="Wildboars 2")
    balances = {p.name: player_balance(p) for p in Player.objects.filter(teams=team1)}

    # Multi-team showcase: Anna plays for BOTH teams (one player record).
    anna = Player.objects.get(name="Anna Beispiel")
    assert set(anna.teams.all()) == {team1, team2}
    # She shows up in both teams' rosters…
    assert anna in team1.players.all() and anna in team2.players.all()

    # Group 180: trigger Anna gets no row; Berti gets 180 + late arrival (5).
    assert balances["Anna Beispiel"] == Decimal(0)
    assert balances["Berti Beispiel"] == Decimal(6)
    assert balances["Chris Beispiel"] == Decimal(4)  # 180 (1) + manual demo (3)
    assert balances["Dana Beispiel"] == Decimal(1)

    group_rows = Penalty.objects.filter(group_id__isnull=False)
    assert all(row.amount_eur == Decimal(1) for row in group_rows)
    assert all(row.group_id == group_rows.first().group_id for row in group_rows)


def test_with_demo_is_idempotent(db):
    call_command("bootstrap", "--with-demo")
    call_command("bootstrap", "--with-demo")
    assert Team.objects.count() == 2
    assert Player.objects.count() == 8
    assert Matchday.objects.count() == 3
    assert Penalty.objects.count() == 5  # 3 from the first run, none added


def test_with_demo_group_penalty_writes_per_row_audit(db):
    call_command("bootstrap", "--with-demo")
    from app.core.models import AuditAction, AuditLog

    assert (
        AuditLog.objects.filter(action=AuditAction.PENALTY_ASSIGNED).count() == 5
    )  # one entry per generated row (M4)
