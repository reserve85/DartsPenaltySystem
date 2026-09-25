"""Shared pytest fixtures for the Dart Penalty Manager test suite."""

from datetime import date

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.utils import translation

from app.core.permissions import GROUP_ADMIN, GROUP_CAPTAIN, GROUP_PLAYER
from app.matchdays.models import Matchday, MatchdayPlayer
from app.penalties.models import PenaltyCatalogItem, PenaltyType
from app.players.models import Player
from app.teams.models import Team

User = get_user_model()


def pytest_configure():
    """Tests render templates via static finders (no collectstatic manifest needed)."""
    settings.DEBUG = True
    settings.STORAGES["staticfiles"]["BACKEND"] = (
        "django.contrib.staticfiles.storage.StaticFilesStorage"
    )


@pytest.fixture(autouse=True)
def _reset_active_language():
    """LocaleMiddleware leaves the active language set after each request;
    reset it so ``reverse()`` and rendering are deterministic per test."""
    translation.deactivate()
    yield
    translation.deactivate()


def _make_group(name: str) -> Group:
    return Group.objects.get_or_create(name=name)[0]


def _login_client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def role_groups(db):
    """Ensure Admin/Captain/Player Groups exist; return them keyed by name."""
    from app.core.permissions import ROLE_GROUPS

    return {name: _make_group(name) for name in ROLE_GROUPS}


@pytest.fixture
def admin_user(db, role_groups):
    user = User.objects.create_user(
        email="admin@example.com", password="pw", approval_status="approved"
    )
    user.groups.add(role_groups[GROUP_ADMIN])
    user.is_staff = True
    user.save()
    return user


@pytest.fixture
def team(db):
    return Team.objects.create(name="Wildboars 1")


@pytest.fixture
def other_team(db):
    return Team.objects.create(name="Wildboars 2")


@pytest.fixture
def captain_user(db, role_groups, team):
    user = User.objects.create_user(
        email="captain@example.com", password="pw", team=team, approval_status="approved"
    )
    user.groups.add(role_groups[GROUP_CAPTAIN])
    return user


@pytest.fixture
def other_captain_user(db, role_groups, other_team):
    user = User.objects.create_user(
        email="othercaptain@example.com",
        password="pw",
        team=other_team,
        approval_status="approved",
    )
    user.groups.add(role_groups[GROUP_CAPTAIN])
    return user


@pytest.fixture
def player_user(db, role_groups):
    user = User.objects.create_user(
        email="player@example.com", password="pw", approval_status="approved"
    )
    user.groups.add(role_groups[GROUP_PLAYER])
    return user


@pytest.fixture
def pending_user(db):
    """Self-registered account waiting for admin approval (model default)."""
    return User.objects.create_user(email="newbie@example.com", password="pw")


@pytest.fixture
def player(db, team):
    return Player.objects.create(name="Andi Beispiel", team=team)


@pytest.fixture
def season(db):
    """Newest season (= globally active by default), created on demand."""
    from app.matchdays.models import Season

    existing = Season.objects.order_by("-name").first()
    if existing is not None:
        return existing
    return Season.objects.create(name="2025/2026")


@pytest.fixture
def matchday(db, team, player):
    return Matchday.objects.create(
        team=team, opponent="SV Eichenberg", venue="home", date=date(2026, 9, 24)
    )


@pytest.fixture
def matchday_with_players(db, team):
    """Factory: returns (matchday, players) with ``count`` participants."""

    def _make(
        count=6, venue="home", date_value=date(2026, 9, 24), opponent="SV Eichenberg", season=None
    ):
        # Roster is season-dependent: the participants belong to the team for
        # the matchday's season (season=None -> newest season / fresh install).
        players = [
            Player.objects.create(name=f"MP{i}", team=team, season=season)
            for i in range(1, count + 1)
        ]
        md = Matchday.objects.create(
            team=team, opponent=opponent, venue=venue, date=date_value, season=season
        )
        for p in players:
            MatchdayPlayer.objects.create(matchday=md, player=p)
        return md, players

    return _make


@pytest.fixture
def catalog_normal(db):
    return PenaltyCatalogItem.objects.create(
        description="Late arrival", amount_eur=5, type=PenaltyType.NORMAL
    )


@pytest.fixture
def catalog_group(db):
    return PenaltyCatalogItem.objects.create(
        description="180", amount_eur=1, type=PenaltyType.PER_ALL_OTHER_MATCHDAY_PLAYERS
    )


@pytest.fixture
def catalog_manual(db):
    return PenaltyCatalogItem.objects.create(
        description="Manual", amount_eur=1, type=PenaltyType.MANUAL
    )


@pytest.fixture
def admin_client(db, admin_user):
    return _login_client(admin_user)


@pytest.fixture
def captain_client(db, captain_user):
    return _login_client(captain_user)


@pytest.fixture
def other_captain_client(db, other_captain_user):
    return _login_client(other_captain_user)


@pytest.fixture
def player_client(db, player_user):
    return _login_client(player_user)
