"""Team view tests — CRUD, captain restrictions and the B2 delete guard."""

import pytest
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.players.models import Player
from app.teams.models import Team

pytestmark = pytest.mark.django_db


def test_admin_creates_team(admin_client):
    response = admin_client.post(reverse("teams:team_create"), {"name": "Wildboars 3"})
    assert response.status_code == 302
    team = Team.objects.get(name="Wildboars 3")
    assert team.name == "Wildboars 3"
    assert AuditLog.objects.filter(action=AuditAction.TEAM_CREATED, target_id=team.pk).count() == 1


def test_captain_cannot_create_team(captain_client):
    response = captain_client.get(reverse("teams:team_create"))
    assert response.status_code == 403
    response = captain_client.post(reverse("teams:team_create"), {"name": "Hack"})
    assert response.status_code == 403
    assert not Team.objects.filter(name="Hack").exists()


def test_admin_renames_team(admin_client, team):
    response = admin_client.post(reverse("teams:team_update", args=[team.pk]), {"name": "Renamed"})
    assert response.status_code == 302
    team.refresh_from_db()
    assert team.name == "Renamed"
    assert AuditLog.objects.filter(action=AuditAction.TEAM_UPDATED, target_id=team.pk).count() == 1


def test_team_list_shows_counts(admin_client, team):
    Player.objects.create(name="A", team=team)
    Player.objects.create(name="B", team=team)
    response = admin_client.get(reverse("teams:team_list"))
    assert response.status_code == 200
    listed = response.context["teams"].get(pk=team.pk)
    assert listed.player_count == 2


def test_team_detail_redirects_to_financial_overview(
    captain_client, other_captain_client, player_client, team, other_team
):
    """The old team page is gone — it redirects; access rules live in the target."""
    expected = f"{reverse('dashboard:financial_overview')}?team={team.pk}"
    response = captain_client.get(reverse("teams:team_detail", args=[team.pk]))
    assert response.status_code == 302
    assert response.url == expected
    # A foreign team link never leaks data: the financial overview ignores
    # ?team= for captains and shows their OWN team.
    assert captain_client.get(reverse("teams:team_detail", args=[other_team.pk])).status_code == 302
    assert other_captain_client.get(reverse("teams:team_detail", args=[team.pk])).status_code == 302
    # Player role stays locked out entirely.
    assert player_client.get(reverse("teams:team_detail", args=[team.pk])).status_code == 403


# ---------------------------------------------------------------------------
# B2 delete guard
# ---------------------------------------------------------------------------
def test_delete_guard_team_with_players_rejected(admin_client, team):
    Player.objects.create(name="P", team=team)
    response = admin_client.post(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 302
    assert Team.objects.filter(pk=team.pk).exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_DELETED).count() == 0


def test_delete_guard_team_with_matchdays_rejected(admin_client, team, player):
    from datetime import date

    from app.matchdays.models import Matchday, MatchdayPlayer

    md = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 1, 1))
    MatchdayPlayer.objects.create(matchday=md, player=player)
    response = admin_client.post(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 302
    assert Team.objects.filter(pk=team.pk).exists()


def test_delete_guard_team_with_linked_captain_rejected(admin_client, team):
    from app.accounts.models import User

    User.objects.create_user(email="captain2@example.com", password="pw", team=team)
    response = admin_client.post(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 302
    assert Team.objects.filter(pk=team.pk).exists()


def test_empty_team_deleted_and_audited(admin_client, team):
    response = admin_client.post(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 302
    assert not Team.objects.filter(pk=team.pk).exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_DELETED, target_id=team.pk).count() == 1


def test_delete_page_renders_confirmation(admin_client, team):
    response = admin_client.get(reverse("teams:team_delete", args=[team.pk]))
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Combined Teams & Players page: roster balances moved to the financial
# overview, plus the new assignment matrix
# ---------------------------------------------------------------------------
def test_financial_overview_shows_roster_balances_and_links(
    admin_client, team, matchday_with_players, catalog_normal, admin_user
):
    from decimal import Decimal

    from app.penalties.services import assign_penalty

    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    response = admin_client.get(reverse("dashboard:financial_overview"), {"team": team.pk})
    assert response.status_code == 200
    content = response.content.decode()

    # each player is linked to its detail page …
    assert reverse("players:player_detail", args=[players[0].pk]) in content
    assert reverse("players:player_detail", args=[players[1].pk]) in content
    # … and the balance for THIS team is shown next to the name
    rows = {row.pk: row.balance for row in response.context["team_balances"]}
    assert rows[players[0].pk] == Decimal(5)
    assert rows[players[1].pk] == Decimal(0)

    # matchdays are linked to their detail page
    assert reverse("matchdays:matchday_detail", args=[md.pk]) in content
    assert md.display_label in content


def test_admin_matrix_saves_assignments_for_active_season(admin_client, team, other_team, season):
    from app.players.services import teams_for

    player = Player.objects.create(name="P1", team=team, season=season)

    # tick BOTH team cells
    response = admin_client.post(
        reverse("teams:team_list"),
        {
            "players": [str(player.pk)],
            "assign": [f"{player.pk}:{team.pk}", f"{player.pk}:{other_team.pk}"],
        },
    )
    assert response.status_code == 302
    assert set(teams_for(player, season)) == {team, other_team}
    assert AuditLog.objects.filter(action=AuditAction.PLAYER_UPDATED).count() == 1

    # untick the first cell -> only other_team remains
    response = admin_client.post(
        reverse("teams:team_list"),
        {"players": [str(player.pk)], "assign": [f"{player.pk}:{other_team.pk}"]},
    )
    assert response.status_code == 302
    assert set(teams_for(player, season)) == {other_team}


def test_captain_matrix_only_affects_own_team(captain_client, team, other_team, season, player):
    """A forged POST with foreign team columns is ignored server-side."""
    from app.players.services import teams_for

    response = captain_client.post(
        reverse("teams:team_list"),
        {
            "players": [str(player.pk)],
            "assign": [f"{player.pk}:{team.pk}", f"{player.pk}:{other_team.pk}"],
        },
    )
    assert response.status_code == 302
    assert teams_for(player, season) == [team]  # own team kept, foreign ignored

    # Unchecking the OWN column does remove the membership — the foreign
    # column stays ignored in the very same request.
    response = captain_client.post(
        reverse("teams:team_list"),
        {"players": [str(player.pk)], "assign": [f"{player.pk}:{other_team.pk}"]},
    )
    assert response.status_code == 302
    assert teams_for(player, season) == []


def test_combined_page_forbidden_for_player_role(player_client, team):
    assert player_client.get(reverse("teams:team_list")).status_code == 403


def test_captain_sees_team_table_read_only(captain_client, team):
    content = captain_client.get(reverse("teams:team_list")).content.decode()
    assert reverse("teams:team_create") not in content
    assert reverse("teams:team_update", args=[team.pk]) not in content
    assert reverse("teams:team_delete", args=[team.pk]) not in content


def test_team_name_links_to_financial_overview(admin_client, team):
    """No 'Open' button — the team name itself opens the financial overview."""
    content = admin_client.get(reverse("teams:team_list")).content.decode()
    assert f"{reverse('dashboard:financial_overview')}?team={team.pk}" in content
    assert "Öffnen" not in content and ">Open<" not in content
