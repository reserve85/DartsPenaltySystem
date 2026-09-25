"""Bootstrap command tests — groups, superuser from env, is_staff sync, catalog."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command

from app.core.permissions import ROLE_GROUPS
from app.penalties.models import PenaltyCatalogItem, PenaltyType

User = get_user_model()

pytestmark = pytest.mark.django_db


def test_bootstrap_creates_role_groups(db):
    call_command("bootstrap")
    assert set(Group.objects.values_list("name", flat=True)) >= set(ROLE_GROUPS)


def test_bootstrap_is_idempotent(db):
    call_command("bootstrap")
    call_command("bootstrap")
    assert Group.objects.count() == 3
    assert PenaltyCatalogItem.objects.count() == 1  # catalog seeded exactly once


def test_bootstrap_superuser_from_env(db, monkeypatch):
    monkeypatch.setenv("DJANGO_SUPERUSER_EMAIL", "Root@Example.com")
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "s3cret-pass!")
    call_command("bootstrap")

    user = User.objects.get()  # email normalized to lowercase
    assert user.email == "root@example.com"
    assert user.is_superuser is True
    assert user.is_staff is True  # H1
    assert user.check_password("s3cret-pass!")
    assert user.groups.filter(name="Admin").exists()

    call_command("bootstrap")  # second run: no duplicates, no password reset
    assert User.objects.count() == 1


def test_bootstrap_skips_superuser_without_password(db, monkeypatch):
    monkeypatch.setenv("DJANGO_SUPERUSER_EMAIL", "root@example.com")
    monkeypatch.delenv("DJANGO_SUPERUSER_PASSWORD", raising=False)
    call_command("bootstrap")
    assert User.objects.count() == 0


def test_bootstrap_resyncs_is_staff(db, role_groups):
    admin = User.objects.create_user(email="admin@x.com")
    admin.groups.add(role_groups["Admin"])
    captain = User.objects.create_user(email="captain@x.com")
    captain.groups.add(role_groups["Captain"])
    # Simulate stale state written outside forms/signals:
    User.objects.filter(pk=admin.pk).update(is_staff=False)
    User.objects.filter(pk=captain.pk).update(is_staff=True)

    call_command("bootstrap")

    admin.refresh_from_db()
    captain.refresh_from_db()
    assert admin.is_staff is True  # Admin group -> staff
    assert captain.is_staff is False  # Captain group -> not staff


def test_bootstrap_seeds_default_catalog(db):
    """Fresh installs start with ONLY the Manual entry."""
    call_command("bootstrap")
    assert PenaltyCatalogItem.objects.count() == 1
    item = PenaltyCatalogItem.objects.get()
    assert item.type == PenaltyType.MANUAL
    assert item.description == "Manual"


def test_bootstrap_adds_manual_to_existing_catalog(db):
    """Older installs keep their items and gain the Manual entry once."""
    PenaltyCatalogItem.objects.create(
        description="Late arrival", amount_eur=5, type=PenaltyType.NORMAL
    )
    call_command("bootstrap")
    call_command("bootstrap")  # idempotent
    assert PenaltyCatalogItem.objects.count() == 2
    assert PenaltyCatalogItem.objects.filter(type=PenaltyType.MANUAL).count() == 1


def test_bootstrap_without_demo_creates_no_club_data(db):
    call_command("bootstrap")
    from app.matchdays.models import Matchday
    from app.players.models import Player
    from app.teams.models import Team

    assert Team.objects.count() == 0
    assert Player.objects.count() == 0
    assert Matchday.objects.count() == 0
