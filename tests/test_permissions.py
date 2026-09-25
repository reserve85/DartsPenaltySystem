"""Role matrix across the views that exist so far (expanded each phase)."""

from datetime import date

import pytest
from django.urls import reverse

from app.core.permissions import GROUP_CAPTAIN, user_can_manage_team

pytestmark = pytest.mark.django_db

REPRESENTATIVE_URLS = [
    reverse("dashboard:index"),
    reverse("accounts:settings"),
    reverse("accounts:user_list"),
    reverse("audit_list"),
]


@pytest.mark.parametrize("url", REPRESENTATIVE_URLS)
def test_anonymous_redirected_to_login(client, url):
    response = client.get(url)
    assert response.status_code == 302
    assert "accounts/login" in response.url


def test_admin_matrix(admin_client):
    assert admin_client.get(reverse("dashboard:index")).status_code == 200
    assert admin_client.get(reverse("accounts:settings")).status_code == 200
    assert admin_client.get(reverse("accounts:user_list")).status_code == 200
    assert admin_client.get(reverse("audit_list")).status_code == 200


def test_captain_matrix(captain_client, other_captain_client):
    assert captain_client.get(reverse("dashboard:index")).status_code == 200
    assert captain_client.get(reverse("accounts:settings")).status_code == 200
    assert captain_client.get(reverse("accounts:user_list")).status_code == 403
    assert captain_client.get(reverse("audit_list")).status_code == 403


def test_player_matrix(player_client):
    assert player_client.get(reverse("dashboard:index")).status_code == 200
    assert player_client.get(reverse("accounts:settings")).status_code == 200
    assert player_client.get(reverse("accounts:user_list")).status_code == 403
    assert player_client.get(reverse("audit_list")).status_code == 403


def test_hybrid_admin_captain_sees_all_matchdays(db, role_groups, team, other_team):
    """Review fix (L2): role precedence admin > captain must hold in list views.

    A hybrid account (Admin + Captain groups — possible via the Django admin
    group editor, which the signals explicitly support) must NOT be silently
    scoped to its own team.
    """
    from django.test import Client

    from app.accounts.models import User
    from app.core.permissions import GROUP_ADMIN
    from app.matchdays.models import Matchday

    hybrid = User.objects.create_user(
        email="hybrid@example.com", password="pw", team=team, approval_status="approved"
    )
    hybrid.groups.add(role_groups[GROUP_ADMIN], role_groups[GROUP_CAPTAIN])
    own = Matchday.objects.create(team=team, opponent="A", venue="home", date=date(2026, 3, 1))
    foreign = Matchday.objects.create(
        team=other_team, opponent="B", venue="home", date=date(2026, 3, 2)
    )

    client = Client()
    client.force_login(hybrid)
    response = client.get(reverse("matchdays:matchday_list"))

    assert response.status_code == 200
    object_list = list(response.context["page_obj"].object_list)
    assert own in object_list
    assert foreign in object_list  # admin precedence: NO team scoping


# ---------------------------------------------------------------------------
# user_can_* permission helpers
# ---------------------------------------------------------------------------
def test_user_can_manage_team(
    admin_user, captain_user, other_captain_user, player_user, team, other_team
):
    assert user_can_manage_team(admin_user, team) is True
    assert user_can_manage_team(admin_user, other_team) is True
    assert user_can_manage_team(captain_user, team) is True
    assert user_can_manage_team(captain_user, other_team) is False
    assert user_can_manage_team(other_captain_user, team) is False
    assert user_can_manage_team(player_user, team) is False


def test_captain_without_team_cannot_manage(db, role_groups, team):
    from app.accounts.models import User
    from app.core.permissions import user_can_manage_team

    captain = User.objects.create_user(email="teamless@example.com", password="pw")
    captain.groups.add(role_groups[GROUP_CAPTAIN])
    assert captain.team_id is None
    assert user_can_manage_team(captain, team) is False


def test_user_can_view_penalty_by_role(
    admin_user, captain_user, player_user, other_captain_user, team, other_team
):
    from app.core.permissions import user_can_view_penalty
    from app.matchdays.models import Matchday
    from app.penalties.models import Penalty
    from app.players.models import Player

    p1 = Player.objects.create(name="P1", team=team)
    p2 = Player.objects.create(name="P2", team=other_team)
    md1 = Matchday.objects.create(team=team, opponent="A", venue="home", date=date(2026, 1, 2))
    md2 = Matchday.objects.create(
        team=other_team, opponent="B", venue="home", date=date(2026, 1, 3)
    )
    pen1 = Penalty.objects.create(matchday=md1, player=p1, description_snapshot="x", amount_eur=1)
    pen2 = Penalty.objects.create(matchday=md2, player=p2, description_snapshot="y", amount_eur=1)

    assert user_can_view_penalty(admin_user, pen1) is True
    assert user_can_view_penalty(admin_user, pen2) is True
    assert user_can_view_penalty(captain_user, pen1) is True
    assert user_can_view_penalty(captain_user, pen2) is False
    assert user_can_view_penalty(other_captain_user, pen1) is False

    player_user.player_link = p1
    player_user.save()
    assert user_can_view_penalty(player_user, pen1) is True
    assert user_can_view_penalty(player_user, pen2) is False
