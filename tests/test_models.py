"""Model sanity tests for the Phase 1 data model."""

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.utils import timezone

from app.matchdays.models import Matchday, MatchdayPlayer, VenueChoice
from app.penalties.models import Penalty, PenaltyCatalogItem
from app.players.models import Player
from app.teams.models import Team

pytestmark = pytest.mark.django_db

User = get_user_model()


def test_email_is_unique_login_identifier():
    user = User.objects.create_user(email="first@example.com", password="pw")
    assert user.USERNAME_FIELD == "email"
    assert user.get_username() == "first@example.com"
    assert "username" not in [f.name for f in User._meta.get_fields()]

    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user(email="first@example.com", password="other")


def test_email_normalized_to_lowercase():
    user = User.objects.create_user(email="Bob@X.com", password="pw")
    assert user.email == "bob@x.com"


def test_create_superuser_without_username():
    superuser = User.objects.create_superuser(email="root@example.com", password="pw")
    assert superuser.is_superuser is True
    assert superuser.is_staff is True
    assert superuser.email == "root@example.com"


def test_required_fields_empty():
    assert User.REQUIRED_FIELDS == []


def test_role_precedence(admin_user, captain_user, player_user):
    assert admin_user.role == "admin"
    assert admin_user.is_admin is True
    assert captain_user.role == "captain"
    assert captain_user.is_captain is True
    assert player_user.role == "player"
    assert player_user.is_player is True

    # Captain + Player membership -> captain wins; Admin wins over both
    captain_user.groups.add(Group.objects.get(name="Player"))
    assert captain_user.role == "captain"
    captain_user.groups.add(Group.objects.get(name="Admin"))
    assert captain_user.role == "admin"


def test_user_without_groups_has_none_role():
    user = User.objects.create_user(email="none@example.com", password="pw")
    assert user.role is None
    assert user.is_admin is False
    assert user.is_captain is False
    assert user.is_player is False


def test_team_unique_and_deletable(team):
    with pytest.raises(IntegrityError), transaction.atomic():
        Team.objects.create(name=team.name)
    assert team.deletable() is True


def test_team_not_deletable_with_players(team):
    Player.objects.create(name="P", team=team)
    assert team.deletable() is False


def test_team_not_deletable_with_matchdays(team):
    Matchday.objects.create(team=team, opponent="X", venue=VenueChoice.HOME, date=date(2026, 1, 1))
    assert team.deletable() is False


def test_team_not_deletable_with_linked_captain(team):
    User.objects.create_user(email="c@example.com", password="pw", team=team)
    assert team.deletable() is False


def test_player_ordering_and_active():
    team = Team.objects.create(name="T")
    p1 = Player.objects.create(name="B", team=team, active=False)
    p2 = Player.objects.create(name="A", team=team)
    assert list(Player.objects.filter(teams=team)) == [p2, p1]


def test_player_can_belong_to_multiple_teams(team, other_team):
    both = Player.objects.create(name="Both")
    both.teams.add(team, other_team)
    assert set(both.teams.all()) == {team, other_team}
    assert both in team.players.all()
    assert both in other_team.players.all()


def test_matchday_display_label_home(team):
    md = Matchday.objects.create(
        team=team, opponent="SV Eichenberg", venue=VenueChoice.HOME, date=date(2026, 9, 24)
    )
    assert md.display_label == "24.09.2026 - Wildboars 1 - SV Eichenberg"
    assert str(md) == md.display_label


def test_matchday_display_label_away(team):
    md = Matchday.objects.create(
        team=team, opponent="SV Burghausen", venue=VenueChoice.AWAY, date=date(2026, 9, 29)
    )
    assert md.display_label == "29.09.2026 - SV Burghausen - Wildboars 1"


def test_matchday_player_unique_constraint(matchday, player):
    MatchdayPlayer.objects.create(matchday=matchday, player=player)
    with pytest.raises(IntegrityError), transaction.atomic():
        MatchdayPlayer.objects.create(matchday=matchday, player=player)


def test_catalog_item_rejects_non_positive_amount():
    with pytest.raises(IntegrityError), transaction.atomic():
        PenaltyCatalogItem.objects.create(description="Neg", amount_eur=0)


def test_penalty_rejects_non_positive_amount(matchday, player, catalog_normal):
    with pytest.raises(IntegrityError), transaction.atomic():
        Penalty.objects.create(
            matchday=matchday,
            player=player,
            catalog_item=catalog_normal,
            description_snapshot="x",
            amount_eur=0,
        )


def test_penalty_default_manager_excludes_soft_deleted(matchday, player, catalog_normal):
    penalty = Penalty.objects.create(
        matchday=matchday,
        player=player,
        catalog_item=catalog_normal,
        description_snapshot="Late",
        amount_eur=5,
    )
    assert Penalty.objects.count() == 1
    penalty.deleted_at = timezone.now()
    penalty.save()
    assert Penalty.objects.count() == 0
    assert Penalty.all_objects().count() == 1
