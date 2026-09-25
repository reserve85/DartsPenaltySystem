"""Player view tests — admin/captain scoping, deactivation."""

import pytest
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.players.models import Player

pytestmark = pytest.mark.django_db


def test_admin_creates_player_any_team(admin_client, team, other_team):
    response = admin_client.post(
        reverse("players:player_create"),
        {"name": "Neuer", "teams": [other_team.pk], "active": "on"},
    )
    assert response.status_code == 302
    player = Player.objects.get(name="Neuer")
    assert list(player.teams.all()) == [other_team]
    assert (
        AuditLog.objects.filter(action=AuditAction.PLAYER_CREATED, target_id=player.pk).count() == 1
    )


def test_admin_creates_multi_team_player(admin_client, team, other_team):
    response = admin_client.post(
        reverse("players:player_create"),
        {"name": "Both", "teams": [team.pk, other_team.pk], "active": "on"},
    )
    assert response.status_code == 302
    player = Player.objects.get(name="Both")
    assert set(player.teams.all()) == {team, other_team}


def test_admin_requires_at_least_one_team(admin_client, team):
    response = admin_client.post(
        reverse("players:player_create"), {"name": "NoTeam", "active": "on"}
    )
    assert response.status_code == 200  # form validation error
    assert not Player.objects.filter(name="NoTeam").exists()


def test_captain_creates_player_in_own_team(captain_client, team, other_team):
    response = captain_client.post(
        reverse("players:player_create"),
        {"name": "Own", "teams": [other_team.pk], "active": "on"},
    )
    # teams field is disabled for captains -> posted value is ignored
    assert response.status_code == 302
    player = Player.objects.get(name="Own")
    assert list(player.teams.all()) == [team]


def test_captain_create_page_team_locked(captain_client, team, other_team):
    response = captain_client.get(reverse("players:player_create"))
    assert response.status_code == 200
    form = response.context["form"]
    assert list(form.fields["teams"].queryset) == [team]
    assert form.fields["teams"].disabled is True


def test_captain_list_only_own_team(captain_client, team, other_team):
    Player.objects.create(name="Mine", team=team)
    Player.objects.create(name="Theirs", team=other_team)
    response = captain_client.get(reverse("players:player_list"))
    assert response.status_code == 200
    names = {p.name for p in response.context["page_obj"]}
    assert names == {"Mine"}


def test_captain_cannot_edit_other_team_player(captain_client, team, other_team):
    foreign = Player.objects.create(name="Theirs", team=other_team)
    assert (
        captain_client.get(reverse("players:player_update", args=[foreign.pk])).status_code == 403
    )
    response = captain_client.post(
        reverse("players:player_update", args=[foreign.pk]),
        {"name": "Hacked", "teams": [other_team.pk], "active": "on"},
    )
    assert response.status_code == 403
    foreign.refresh_from_db()
    assert foreign.name == "Theirs"


def test_multi_team_player_visible_and_editable_by_both_captains(
    captain_client, other_captain_client, team, other_team
):
    both = Player.objects.create(name="Shared")
    both.teams.add(team, other_team)

    for client in (captain_client, other_captain_client):
        listing = client.get(reverse("players:player_list"))
        assert {p.name for p in listing.context["page_obj"]} == {"Shared"}
        assert client.get(reverse("players:player_update", args=[both.pk])).status_code == 200


def test_captain_edit_preserves_other_team_memberships(captain_client, team, other_team):
    """Captain of W1 edits a W1+W2 player -> the W2 membership must survive."""
    both = Player.objects.create(name="Shared")
    both.teams.add(team, other_team)

    response = captain_client.post(
        reverse("players:player_update", args=[both.pk]),
        {"name": "Shared renamed", "active": "on"},  # teams disabled for captains
    )
    assert response.status_code == 302
    both.refresh_from_db()
    assert both.name == "Shared renamed"
    assert set(both.teams.all()) == {team, other_team}  # neither lost nor added


def test_admin_can_remove_a_membership(admin_client, team, other_team):
    both = Player.objects.create(name="Shared")
    both.teams.add(team, other_team)
    response = admin_client.post(
        reverse("players:player_update", args=[both.pk]),
        {"name": "Shared", "teams": [team.pk], "active": "on"},
    )
    assert response.status_code == 302
    both.refresh_from_db()
    assert list(both.teams.all()) == [team]


def test_player_role_forbidden(captain_client, player_client):
    assert player_client.get(reverse("players:player_list")).status_code == 403
    assert player_client.get(reverse("players:player_create")).status_code == 403


def test_django_admin_player_pages_render(db):
    """The admin player form (name/active + PlayerTeam season inline) must render."""
    from django.contrib.auth import get_user_model
    from django.test import Client

    from app.players.models import Player

    root = get_user_model().objects.create_superuser(email="root@example.com", password="pw")
    player = Player.objects.create(name="Admin View")
    client = Client()
    client.force_login(root)

    assert client.get(reverse("admin:players_player_changelist")).status_code == 200
    assert client.get(reverse("admin:players_player_add")).status_code == 200
    assert client.get(reverse("admin:players_player_change", args=[player.pk])).status_code == 200


def test_admin_updates_player(admin_client, team):
    player = Player.objects.create(name="Old", team=team)
    response = admin_client.post(
        reverse("players:player_update", args=[player.pk]),
        {"name": "New", "teams": [team.pk], "active": "on"},
    )
    assert response.status_code == 302
    player.refresh_from_db()
    assert player.name == "New"
    assert (
        AuditLog.objects.filter(action=AuditAction.PLAYER_UPDATED, target_id=player.pk).count() == 1
    )


def test_deactivate_player(admin_client, team):
    player = Player.objects.create(name="P", team=team)
    response = admin_client.post(reverse("players:player_deactivate", args=[player.pk]))
    assert response.status_code == 302
    player.refresh_from_db()
    assert player.active is False
    assert (
        AuditLog.objects.filter(action=AuditAction.PLAYER_DEACTIVATED, target_id=player.pk).count()
        == 1
    )


def test_captain_deactivates_own_player_but_not_foreign(captain_client, team, other_team):
    own = Player.objects.create(name="Own", team=team)
    foreign = Player.objects.create(name="Foreign", team=other_team)
    assert (
        captain_client.post(reverse("players:player_deactivate", args=[own.pk])).status_code == 302
    )
    assert (
        captain_client.post(reverse("players:player_deactivate", args=[foreign.pk])).status_code
        == 403
    )
    own.refresh_from_db()
    foreign.refresh_from_db()
    assert own.active is False
    assert foreign.active is True


# ---------------------------------------------------------------------------
# Player detail page
# ---------------------------------------------------------------------------
def test_player_detail_permission_matrix(
    admin_client, captain_client, other_captain_client, player_client, player_user, team, player
):
    from django.test import Client

    url = reverse("players:player_detail", args=[player.pk])
    assert admin_client.get(url).status_code == 200
    assert captain_client.get(url).status_code == 200  # captain of the player's team
    assert other_captain_client.get(url).status_code == 403
    assert player_client.get(url).status_code == 403  # not linked yet
    assert Client().get(url).status_code == 302  # anonymous -> login

    player_user.player_link = player
    player_user.save()
    assert player_client.get(url).status_code == 200  # own page after linking


def test_player_detail_shows_per_team_balance(
    admin_client, team, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    from app.penalties.services import assign_penalty

    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    response = admin_client.get(reverse("players:player_detail", args=[players[0].pk]))
    assert response.status_code == 200
    assert response.context["balance"] == 5
    team_rows = {row["team"].pk: row["balance"] for row in response.context["team_rows"]}
    assert team_rows[md.team.pk] == 5
    content = response.content.decode()
    assert players[0].name in content
    assert reverse("teams:team_detail", args=[md.team.pk]) in content
