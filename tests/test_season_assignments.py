"""Season-dependent player <-> team assignment — one roster per season.

A player can be assigned to DIFFERENT team(s) in each season (transfer,
second team, …). The globally active season (navbar dropdown) decides which
assignment is shown and edited; a NEW season starts EMPTY (no carry-over).
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from app.matchdays.models import Matchday, Season
from app.penalties.services import team_balances
from app.players.models import Player, PlayerTeam
from app.players.services import assign_teams, teams_for

pytestmark = pytest.mark.django_db


def _set_active_season(client, season):
    session = client.session
    session["season_id"] = season.pk
    session.save()


# ---------------------------------------------------------------------------
# Model / service level
# ---------------------------------------------------------------------------
def test_assignment_is_season_dependent(team, other_team, season):
    """The same player can play for a DIFFERENT team per season."""
    newer = Season.objects.create(name="2026/2027")
    player = Player.objects.create(name="Wechsler")

    assign_teams(player, [team], season=season)
    assign_teams(player, [other_team], season=newer)

    assert teams_for(player, season) == [team]
    assert teams_for(player, newer) == [other_team]
    assert list(Player.objects.in_team(team, season)) == [player]
    assert list(Player.objects.in_team(other_team, season)) == []
    assert list(Player.objects.in_team(team, newer)) == []
    assert list(Player.objects.in_team(other_team, newer)) == [player]


def test_assign_teams_touches_only_the_given_season(team, other_team, season):
    newer = Season.objects.create(name="2026/2027")
    player = Player.objects.create(name="P", team=team, season=season)

    assign_teams(player, [other_team], season=newer)

    assert teams_for(player, season) == [team]  # old season untouched
    assert teams_for(player, newer) == [other_team]


def test_new_season_starts_empty_without_carry_over(team, season):
    """Decision: a new season has NO assignment until it is given per season."""
    player = Player.objects.create(name="P", team=team, season=season)
    newer = Season.objects.create(name="2026/2027")

    assert teams_for(player, newer) == []
    assert list(Player.objects.in_team(team, newer)) == []


def test_multi_team_player_per_season(team, other_team, season):
    newer = Season.objects.create(name="2026/2027")
    player = Player.objects.create(name="Doppel")

    assign_teams(player, [team, other_team], season=season)
    assign_teams(player, [team], season=newer)

    assert set(Player.objects.in_team(team, season)) == {player}
    assert set(Player.objects.in_team(other_team, season)) == {player}
    assert set(Player.objects.in_team(team, newer)) == {player}
    assert Player.objects.in_team(other_team, newer).count() == 0


def test_first_created_season_binds_seasonless_assignments(admin_client, team):
    """Fresh install: assignments made before any season exists become the
    roster of the FIRST season — every later season starts empty."""
    player = Player.objects.create(name="P", team=team)  # no season yet
    assert PlayerTeam.objects.filter(season__isnull=True).exists()

    response = admin_client.post(reverse("season_create"), {"name": "2025/2026"})
    assert response.status_code == 302
    first = Season.objects.get(name="2025/2026")
    assert teams_for(player, first) == [team]

    response = admin_client.post(reverse("season_create"), {"name": "2026/2027"})
    assert response.status_code == 302
    second = Season.objects.get(name="2026/2027")
    assert teams_for(player, second) == []


# ---------------------------------------------------------------------------
# Views — the active season decides what is shown and edited
# ---------------------------------------------------------------------------
def test_player_form_saves_only_the_active_season(admin_client, team, other_team, season):
    newer = Season.objects.create(name="2026/2027")  # active by default (newest)
    player = Player.objects.create(name="Wechsler", team=team, season=season)

    response = admin_client.post(
        reverse("players:player_update", args=[player.pk]),
        {"name": "Wechsler", "teams": [other_team.pk], "active": "on"},
    )
    assert response.status_code == 302
    assert teams_for(player, newer) == [other_team]  # saved for the ACTIVE season
    assert teams_for(player, season) == [team]  # old season keeps its team


def test_player_form_shows_active_season_assignment(admin_client, team, other_team, season):
    newer = Season.objects.create(name="2026/2027")
    player = Player.objects.create(name="P", team=team, season=season)
    assign_teams(player, [other_team], season=newer)

    response = admin_client.get(reverse("players:player_update", args=[player.pk]))
    assert response.status_code == 200
    assert response.context["form"].initial["teams"] == [other_team.pk]


def test_player_list_shows_active_season_teams(admin_client, team, season):
    Season.objects.create(name="2026/2027")  # becomes the active season
    player = Player.objects.create(name="P", team=team, season=season)

    response = admin_client.get(reverse("players:player_list"))
    row = next(p for p in response.context["page_obj"] if p.pk == player.pk)
    assert list(row.season_assignments) == []  # newest (empty) season active

    _set_active_season(admin_client, season)
    response = admin_client.get(reverse("players:player_list"))
    row = next(p for p in response.context["page_obj"] if p.pk == player.pk)
    assert [a.team for a in row.season_assignments] == [team]


def test_captain_player_list_is_season_scoped(captain_client, team, season):
    newer = Season.objects.create(name="2026/2027")
    Player.objects.create(name="Alt", team=team, season=season)
    Player.objects.create(name="Neu", team=team, season=newer)

    response = captain_client.get(reverse("players:player_list"))
    assert {p.name for p in response.context["page_obj"]} == {"Neu"}

    _set_active_season(captain_client, season)
    response = captain_client.get(reverse("players:player_list"))
    assert {p.name for p in response.context["page_obj"]} == {"Alt"}


def test_team_detail_roster_is_season_scoped(admin_client, team, season):
    newer = Season.objects.create(name="2026/2027")
    Player.objects.create(name="Alt", team=team, season=season)
    Player.objects.create(name="Neu", team=team, season=newer)

    response = admin_client.get(reverse("teams:team_detail", args=[team.pk]))
    names = {row["player"].name for row in response.context["player_rows"]}
    assert names == {"Neu"}

    _set_active_season(admin_client, season)
    response = admin_client.get(reverse("teams:team_detail", args=[team.pk]))
    names = {row["player"].name for row in response.context["player_rows"]}
    assert names == {"Alt"}


def test_team_list_player_count_is_season_scoped(admin_client, team, season):
    newer = Season.objects.create(name="2026/2027")
    Player.objects.create(name="Alt1", team=team, season=season)
    Player.objects.create(name="Alt2", team=team, season=season)
    Player.objects.create(name="Neu", team=team, season=newer)

    response = admin_client.get(reverse("teams:team_list"))
    assert response.context["teams"].get(pk=team.pk).player_count == 1

    _set_active_season(admin_client, season)
    response = admin_client.get(reverse("teams:team_list"))
    assert response.context["teams"].get(pk=team.pk).player_count == 2


def test_team_balances_roster_is_season_scoped(team, season):
    newer = Season.objects.create(name="2026/2027")
    Player.objects.create(name="Alt", team=team, season=season)
    Player.objects.create(name="Neu", team=team, season=newer)

    assert [p.name for p in team_balances(team, season=season)] == ["Alt"]
    assert [p.name for p in team_balances(team, season=newer)] == ["Neu"]


# ---------------------------------------------------------------------------
# Matchday participants — eligibility follows the matchday's season
# ---------------------------------------------------------------------------
def test_matchday_form_offers_only_the_matchdays_season_roster(captain_client, team, season):
    newer = Season.objects.create(name="2026/2027")
    Player.objects.create(name="Alt", team=team, season=season)
    new_player = Player.objects.create(name="Neu", team=team, season=newer)

    # Captains get their team preselected -> the roster is rendered directly;
    # new matchdays default to the active (newest) season.
    response = captain_client.get(reverse("matchdays:matchday_create"))
    assert response.status_code == 200
    offered = set(response.context["form"].fields["participants"].queryset)
    assert offered == {new_player}


def test_matchday_rejects_player_of_another_season(admin_client, team, season):
    newer = Season.objects.create(name="2026/2027")
    old_player = Player.objects.create(name="Alt", team=team, season=season)
    new_player = Player.objects.create(name="Neu", team=team, season=newer)

    payload = {
        "team": team.pk,
        "season": season.pk,  # matchday belongs to the OLD season
        "opponent": "SV X",
        "venue": "home",
        "date": "2026-10-01",
    }
    # player only assigned in the OTHER season -> rejected
    response = admin_client.post(
        reverse("matchdays:matchday_create"), {**payload, "participants": [new_player.pk]}
    )
    assert response.status_code == 200  # form validation error
    assert not Matchday.objects.filter(opponent="SV X").exists()

    # the player of THAT season -> accepted
    response = admin_client.post(
        reverse("matchdays:matchday_create"), {**payload, "participants": [old_player.pk]}
    )
    assert response.status_code == 302
    assert Matchday.objects.filter(opponent="SV X", season=season).exists()


# ---------------------------------------------------------------------------
# Financial reads — money rows stay with their team/season
# ---------------------------------------------------------------------------
def test_financial_roster_follows_the_selected_season(
    admin_client, team, season, matchday_with_players, catalog_normal, admin_user
):
    from app.penalties.services import assign_penalty

    Season.objects.create(name="2026/2027")  # becomes the active season
    md, players = matchday_with_players(2, season=season)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )  # 5 € open in the OLD season

    # active = new season: no penalties there
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.context["team_penalties"] == Decimal(0)

    _set_active_season(admin_client, season)
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.context["team_penalties"] == Decimal(5)
    shown = {row.name for row in response.context["team_balances"]}
    assert players[0].name in shown
