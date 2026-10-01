"""Dashboard + financial overview — role matrix and ``player_link`` handling."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from app.core.permissions import GROUP_CAPTAIN
from app.matchdays.models import Matchday
from app.penalties.services import assign_penalty

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------------------------------------------------------------------------
# Dashboard role matrix
# ---------------------------------------------------------------------------
def test_dashboard_requires_login(db):
    response = Client().get(reverse("dashboard:index"))
    assert response.status_code == 302
    assert "login" in response.url


def test_start_page_is_financial_overview_for_admin(admin_client, team, other_team):
    """The old Dashboard/Übersicht is gone — the start page is the financial
    overview, defaulting to the first team of the active season."""
    response = admin_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    context = response.context
    assert {t.name for t in context["teams"]} == {team.name, other_team.name}
    assert context["selected_team"] == min(context["teams"], key=lambda t: t.name)
    content = response.content.decode()
    assert "Financial overview" in content or "Finanzübersicht" in content


def test_start_page_is_financial_overview_for_captain(
    captain_client, team, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    response = captain_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    assert response.context["selected_team"] == team  # default: the OWN team
    assert response.context["team_total"] == Decimal(5)


def test_financial_overview_defaults_to_own_team_not_first(other_captain_client, team, other_team):
    """The default is the account's OWN team — even when another team would
    come first alphabetically (the old default)."""
    response = other_captain_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    assert response.context["teams"][0] == team  # proof: not the alphabetical pick
    assert response.context["selected_team"] == other_team


def test_financial_player_defaults_to_own_roster_team(
    player_client, player_user, team, other_team, season
):
    """A Player-role account has NO captaincy field — its assignment to a team
    lives in the ROSTER of the linked player, so the default selection must
    come from there instead of falling back to the first team alphabetically."""
    from app.players.models import Player

    mine = Player.objects.create(name="Roster One", team=other_team, season=season)
    player_user.player_link = mine
    player_user.save(update_fields=["player_link"])

    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.context["teams"][0] == team  # proof: not the alphabetical pick
    assert response.context["selected_team"] == other_team


def test_financial_default_prefers_active_season_roster(
    player_client, player_user, team, other_team, season
):
    """The roster lookup is season-scoped: the ACTIVE season beats an older one."""
    from app.matchdays.models import Season
    from app.players.models import Player, PlayerTeam

    old_season = Season.objects.create(name="2024/2025")
    mine = Player.objects.create(name="Roster One")
    PlayerTeam.objects.create(player=mine, team=team, season=old_season)
    PlayerTeam.objects.create(player=mine, team=other_team, season=season)
    player_user.player_link = mine
    player_user.save(update_fields=["player_link"])

    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.context["selected_team"] == other_team


def test_start_page_is_financial_overview_for_player_without_link(player_client):
    response = player_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    assert response.context["own_mode"] is True
    assert response.context["no_player_link"] is True


def test_dashboard_player_with_link_shows_own_penalties(
    player_client, player_user, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    player_user.player_link = players[0]
    player_user.save()

    response = player_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    assert response.context["player"] == players[0]
    assert response.context["own_balance"] == Decimal(5)
    assert len(response.context["own_penalties"]) == 1
    assert "no_player_link" not in response.context


# ---------------------------------------------------------------------------
# Financial overview role matrix
# ---------------------------------------------------------------------------
def test_financial_requires_login(db):
    response = Client().get(reverse("dashboard:financial_overview"))
    assert response.status_code == 302
    assert "login" in response.url


def test_financial_admin_can_select_any_team(admin_client, team, other_team):
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert len(response.context["teams"]) == 2
    assert response.context["selected_team"] == team  # default: first alphabetically

    response = admin_client.get(reverse("dashboard:financial_overview"), {"team": other_team.pk})
    assert response.context["selected_team"] == other_team


def test_financial_captain_reads_all_teams_manages_own(
    captain_client, team, other_team, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    response = captain_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert response.context["selected_team"] == team  # default: the OWN team
    # READ is universal: the selector offers EVERY team of the season …
    assert {t.pk for t in response.context["teams"]} == {team.pk, other_team.pk}
    assert response.context["team_total"] == Decimal(5)
    assert response.context["can_manage"] is True  # … while WRITE stays own-team
    # … but the STRICT payment gate stays closed: the captain is not the cashier.
    assert response.context["is_cashier"] is False

    # … and a foreign team can be opened read-only (no payment affordances).
    response = captain_client.get(reverse("dashboard:financial_overview"), {"team": other_team.pk})
    assert response.status_code == 200
    assert response.context["selected_team"] == other_team
    assert response.context["can_manage"] is False
    assert response.context["is_cashier"] is False
    assert reverse("penalties:payment_create") not in response.content.decode()


def test_financial_captain_without_team_can_read(db, role_groups):
    """A teamless captain has no team to manage but may still read."""
    user = User.objects.create_user(
        email="noteam@example.com", password="pw", approval_status="approved"
    )
    user.groups.add(Group.objects.get(name=GROUP_CAPTAIN))
    client = Client()
    client.force_login(user)
    response = client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert response.context["can_manage"] is False
    assert response.context["is_cashier"] is False


def test_financial_roleless_user_can_read(db, role_groups, team):
    """Accounts without any role used to get 403 — read access is universal now."""
    user = User.objects.create_user(
        email="plain@example.com", password="pw", approval_status="approved"
    )
    client = Client()
    client.force_login(user)
    response = client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert response.context["can_manage"] is False
    assert response.context["is_cashier"] is False
    # never write affordances
    assert reverse("penalties:payment_create") not in response.content.decode()


def test_financial_player_sees_own_penalties_and_all_teams_read_only(
    player_client, player_user, team, other_team, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(3)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    assign_penalty(
        matchday=md,
        player=players[1],
        catalog_item=catalog_normal,
        description_snapshot="Keks",
        actor=admin_user,
    )
    player_user.player_link = players[0]
    player_user.save()

    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    # Own section: only the player's own rows.
    assert response.context["own_mode"] is True
    assert response.context["own_balance"] == Decimal(5)
    assert list(response.context["own_penalties"]) == list(md.penalties.filter(player=players[0]))
    # Team section: read-only, first team by default.
    assert response.context["selected_team"] == team
    assert response.context["can_manage"] is False  # no payment forms/buttons
    assert response.context["is_cashier"] is False
    content = response.content.decode()
    assert "Keks" in content
    assert reverse("penalties:payment_create") not in content
    # READ is universal: foreign teams are selectable (read-only).
    response = player_client.get(reverse("dashboard:financial_overview"), {"team": other_team.pk})
    assert response.status_code == 200
    assert response.context["selected_team"] == other_team
    assert response.context["can_manage"] is False
    assert response.context["is_cashier"] is False


def test_financial_overview_cashier_gets_payment_ui(
    admin_client, admin_user, team, matchday_with_players, catalog_normal, cashier
):
    """The team's cashier (admin here) opens the strict payment gate."""
    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.context["cashier"] == admin_user
    assert response.context["is_cashier"] is True
    content = response.content.decode()
    assert reverse("penalties:payment_create") in content
    assert 'id="paymentModal"' in content
    assert "Cashier" in content or "Kassier" in content  # the dedicated line


def test_financial_overview_shows_no_cashier_hint(admin_client, team, admin_user):
    """No cashier yet -> managers get the hint, never a silent missing button."""
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.context["cashier"] is None
    assert response.context["can_manage"] is True
    assert response.context["is_cashier"] is False
    content = response.content.decode()
    assert reverse("penalties:payment_create") not in content
    assert reverse("teams:cashier_list") in content  # link to the Kasse page


def test_financial_player_team_param_must_be_numeric(player_client, player_user, team):
    player_user.player_link = None
    player_user.save()
    # no player link -> own section only, no team lookup at all
    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.context["no_player_link"] is True

    from app.players.models import Player

    player = Player.objects.create(name="MPX", team=team)
    player_user.player_link = player
    player_user.save()
    response = player_client.get(reverse("dashboard:financial_overview"), {"team": "abc"})
    assert response.status_code == 404  # not a 500


def test_financial_player_without_link_shows_message(player_client):
    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert response.context["own_mode"] is True
    assert response.context["no_player_link"] is True


def test_financial_sections_rendered_for_admin(
    admin_client, team, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(3)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    # one active player becomes inactive with a debt
    players[1].active = False
    players[1].save()
    assign_penalty(
        matchday=md,
        player=players[1],
        catalog_item=catalog_normal,
        description_snapshot="Keks",
        actor=admin_user,
    )

    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    context = response.context
    assert context["team_total"] == Decimal(10)
    assert context["inactive_balances"][0] == players[1]
    assert context["inactive_balances"][0].balance == Decimal(5)
    assert {m.pk for m in context["matchday_totals"]} == {md.pk}
    counts = {c["description_snapshot"]: c["count"] for c in context["most_common"]}
    assert counts == {"Late arrival": 1, "Keks": 1}
    assert context["assigned_penalties"].count() == 2

    # regression: player names (not only amounts) are rendered in the tables
    content = response.content.decode()
    for row_player in players:
        assert row_player.name in content


def test_financial_overview_shows_inactive_player_names(
    admin_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    players[0].active = False
    players[0].save()
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=4,
        description_snapshot="Keks",
        actor=admin_user,
    )
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    assert players[0].name in content


# ---------------------------------------------------------------------------
# Captain marking (👑) in the financial overview
# ---------------------------------------------------------------------------
def test_financial_overview_marks_captains_in_heading_without_selector_marker(
    admin_client, team, other_team, captain_user
):
    """The selector lists plain team names (no captain marker); the selected
    team lists its captain account(s) with a 👑 crown under the heading."""
    response = admin_client.get(reverse("dashboard:financial_overview"), {"team": team.pk})
    assert response.status_code == 200
    assert response.context["team_captains"] == [captain_user]
    content = response.content.decode()
    assert f"{team.name}</option>" in content  # plain option — no marker …
    assert f"{other_team.name} ©" not in content  # never a © in the dropdown
    assert "©</option>" not in content
    assert f"{captain_user.email} 👑" in content  # crown next to the captain name


def test_financial_overview_marks_captain_player_row(admin_client, team, captain_user, player):
    """A captain account linked to a player shows a 👑 crown in that row."""
    captain_user.player_link = player
    captain_user.save(update_fields=["player_link"])

    response = admin_client.get(reverse("dashboard:financial_overview"), {"team": team.pk})
    content = response.content.decode()
    row = next(r for r in re.findall(r"<tr.*?</tr>", content, flags=re.DOTALL) if player.name in r)
    assert "👑" in row
    assert "badge text-bg-info" not in row  # no blue badge background
    # the captains line prefers the linked player's name over the login email
    assert f"{player.name} 👑" in content


def test_financial_overview_marks_admin_assigned_as_captain(
    admin_client, admin_user, team, other_team
):
    """Role decoupling: an Admin with a team counts as that team's captain."""
    admin_user.team = team
    admin_user.save(update_fields=["team"])

    response = admin_client.get(reverse("dashboard:financial_overview"), {"team": team.pk})
    assert response.context["team_captains"] == [admin_user]
    content = response.content.decode()
    assert f"{admin_user.email} 👑" in content
    assert other_team.name in content  # still listed in the selector

    # … and the admin's own team becomes the default selection
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.context["selected_team"] == team


def test_financial_overview_hides_inactive_captain(admin_client, team, captain_user):
    """Deactivated accounts are never marked as captains."""
    captain_user.is_active = False
    captain_user.save(update_fields=["is_active"])

    response = admin_client.get(reverse("dashboard:financial_overview"), {"team": team.pk})
    assert response.context["team_captains"] == []
    assert "👑" not in response.content.decode()  # no crown without a captain


def test_matchday_totals_marks_today_and_mutes_past(admin_client, team):
    """The 'Matchday totals' (Spieltagssummen) table marks rows like the list.

    Today's Spieltag gets the green row + badge, an already played one is
    muted — same visual language in both tables that list Spieltage.
    """
    today = timezone.localdate()
    Matchday.objects.create(team=team, opponent="Today FC", venue="home", date=today)
    Matchday.objects.create(
        team=team, opponent="Past FC", venue="home", date=today - timedelta(days=14)
    )

    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    rows = re.findall(r"<tr.*?</tr>", content, flags=re.DOTALL)

    def row_for(opponent):
        return next(row for row in rows if opponent in row)

    assert 'class="matchday-today"' in row_for("Today FC")
    assert "badge text-bg-success" in row_for("Today FC")
    assert 'class="matchday-past"' in row_for("Past FC")
    assert "text-bg-success" not in row_for("Past FC")
    assert "matchday-past" not in row_for("Today FC")
