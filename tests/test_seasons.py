"""Seasons — closed cash boxes, global selector, no carry-over of open amounts."""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.matchdays.models import Matchday, Season
from app.penalties.models import Payment
from app.penalties.services import (
    assign_penalty,
    player_balance,
    player_total,
    player_total_paid,
    record_payment,
)
from app.players.models import Player

pytestmark = pytest.mark.django_db
User = get_user_model()


def _set_active_season(client, season):
    session = client.session
    session["season_id"] = season.pk
    session.save()


# ---------------------------------------------------------------------------
# Model + bootstrap + matchday form
# ---------------------------------------------------------------------------
def test_season_name_unique(db):
    Season.objects.create(name="2025/2026")
    with pytest.raises(IntegrityError):
        Season.objects.create(name="2025/2026")


def test_default_season_is_newest_by_name(db):
    Season.objects.create(name="2024/2025")
    Season.objects.create(name="2025/2026")
    assert Season.objects.order_by("-name").first().name == "2025/2026"


def test_bootstrap_seeds_initial_season(db):
    call_command("bootstrap")
    assert Season.objects.count() == 1
    call_command("bootstrap")  # idempotent
    assert Season.objects.count() == 1


def test_bootstrap_demo_assigns_season(db):
    call_command("bootstrap", "--with-demo")
    season = Season.objects.get()
    assert Matchday.objects.filter(season=season).count() == 3


def test_matchday_create_defaults_to_newest_season(admin_client, team, season):
    """New matchdays are assigned to the active (newest) season."""
    Season.objects.create(name="2026/2027")  # becomes the active default
    player = Player.objects.create(name="P", team=team)

    response = admin_client.post(
        reverse("matchdays:matchday_create"),
        {
            "team": team.pk,
            "opponent": "SV X",
            "venue": "home",
            "date": "2026-10-01",
            "participants": [player.pk],
        },
    )
    assert response.status_code == 302
    matchday = Matchday.objects.get(opponent="SV X")
    assert matchday.season.name == "2026/2027"


def test_matchday_form_offers_season_field(admin_client, season):
    response = admin_client.get(reverse("matchdays:matchday_create"))
    assert response.status_code == 200
    assert "season" in response.context["form"].fields
    # new matchdays preselect the globally active season
    assert response.context["form"].initial["season"] == season.pk


# ---------------------------------------------------------------------------
# Management views (admin) + global selector
# ---------------------------------------------------------------------------
def test_season_list_admin_only(admin_client, captain_client, player_client):
    assert admin_client.get(reverse("season_list")).status_code == 200
    assert captain_client.get(reverse("season_list")).status_code == 403
    assert player_client.get(reverse("season_list")).status_code == 403


def test_admin_creates_season(admin_client):
    response = admin_client.post(reverse("season_create"), {"name": "2027/2028"})
    assert response.status_code == 302
    assert Season.objects.filter(name="2027/2028").exists()
    assert AuditLog.objects.filter(action=AuditAction.SEASON_CREATED).count() == 1


def test_season_duplicate_name_rejected(admin_client):
    Season.objects.create(name="2027/2028")
    response = admin_client.post(reverse("season_create"), {"name": "2027/2028"})
    assert response.status_code == 302  # back with an error message
    assert Season.objects.filter(name="2027/2028").count() == 1


def test_delete_empty_season(admin_client, season):
    response = admin_client.post(reverse("season_delete", args=[season.pk]))
    assert response.status_code == 302
    assert Season.objects.count() == 0
    assert AuditLog.objects.filter(action=AuditAction.SEASON_DELETED).count() == 1


def test_delete_season_with_matchdays_blocked(admin_client, season, matchday):
    matchday.season = season
    matchday.save(update_fields=["season"])
    admin_client.post(reverse("season_delete", args=[season.pk]))
    assert Season.objects.filter(pk=season.pk).exists()


def test_delete_season_with_payments_blocked(
    admin_client, season, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2, season=season)
    record_payment(player=players[0], team=md.team, amount_eur=1, actor=admin_user, season=season)
    admin_client.post(reverse("season_delete", args=[season.pk]))
    assert Season.objects.filter(pk=season.pk).exists()


def test_delete_season_with_open_amounts_blocked(
    admin_client, season, matchday_with_players, catalog_normal, admin_user
):
    """Review fix: a season that still owes/is owed money must not be deletable."""
    md, players = matchday_with_players(2, season=season)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )  # 5.00 € open — season is NOT settled
    response = admin_client.post(reverse("season_delete", args=[season.pk]), follow=True)
    assert Season.objects.filter(pk=season.pk).exists()
    text = response.content.decode()
    assert "open amounts" in text or "offene Beträge" in text  # en / de catalog
    assert not AuditLog.objects.filter(action=AuditAction.SEASON_DELETED).exists()


def test_delete_season_with_player_assignments_blocked(admin_client, season, team):
    """Review fix: roster history (PlayerTeam) must survive a season delete."""
    Player.objects.create(name="Rostered", team=team, season=season)
    assert season.player_team_assignments.exists()
    response = admin_client.post(reverse("season_delete", args=[season.pk]), follow=True)
    assert Season.objects.filter(pk=season.pk).exists()
    text = response.content.decode()
    assert "assigned to it" in text or "zugeordnet" in text  # en / de catalog


def test_season_set_rejects_non_numeric_id(admin_client):
    """Review fix: crafted/empty ``season`` values are 404s, not ValueErrors (500)."""
    for bad in ("abc", ""):
        response = admin_client.post(reverse("season_set"), {"season": bad, "next": "/"})
        assert response.status_code == 404, f"got {response.status_code} for {bad!r}"


def test_season_dropdown_rendered_and_switchable(admin_client, season):
    newer = Season.objects.create(name="2026/2027")  # active by default (newest)

    response = admin_client.get(reverse("dashboard:index"))
    assert response.context["active_season"] == newer
    content = response.content.decode()
    assert reverse("season_set") in content
    assert season.name in content and newer.name in content

    response = admin_client.post(reverse("season_set"), {"season": season.pk, "next": "/"})
    assert response.status_code == 302
    assert admin_client.session["season_id"] == season.pk
    response = admin_client.get(reverse("dashboard:index"))
    assert response.context["active_season"] == season


def test_matchday_list_filtered_by_active_season(admin_client, season, team):
    newer = Season.objects.create(name="2026/2027")  # active by default
    md_old = Matchday.objects.create(
        team=team, opponent="Old", venue="home", date=date(2025, 10, 1), season=season
    )
    md_new = Matchday.objects.create(
        team=team, opponent="New", venue="home", date=date(2026, 10, 1), season=newer
    )

    response = admin_client.get(reverse("matchdays:matchday_list"))
    shown = {m.pk for m in response.context["page_obj"].object_list}
    assert shown == {md_new.pk}

    _set_active_season(admin_client, season)
    response = admin_client.get(reverse("matchdays:matchday_list"))
    shown = {m.pk for m in response.context["page_obj"].object_list}
    assert shown == {md_old.pk}


# ---------------------------------------------------------------------------
# Financial scoping — the trio + NO carry-over between seasons
# ---------------------------------------------------------------------------
def test_balance_trio_in_financial_overview(
    admin_client, season, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2, season=season)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )  # 5.00 € total
    record_payment(
        player=players[0], team=md.team, amount_eur=2, actor=admin_user, season=season
    )  # 2.00 € settled

    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.context["team_penalties"] == Decimal(5)  # Strafe gesamt
    assert response.context["team_paid"] == Decimal(2)  # Strafe getilgt
    assert response.context["team_total"] == Decimal(3)  # Strafe noch offen

    rows = {row.pk: row for row in response.context["team_balances"]}
    assert rows[players[0].pk].total == Decimal(5)
    assert rows[players[0].pk].paid == Decimal(2)
    assert rows[players[0].pk].balance == Decimal(3)


def test_no_carry_over_between_seasons(season, matchday_with_players, catalog_normal, admin_user):
    """Open amounts of one closed season never leak into the next one."""
    newer = Season.objects.create(name="2026/2027")
    md, players = matchday_with_players(2, season=season)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )  # 5 € open
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user, season=season)

    assert player_total(players[0], season=season) == Decimal(5)
    assert player_total_paid(players[0], season=season) == Decimal(2)
    assert player_balance(players[0], season=season) == Decimal(3)
    # the next season starts at zero
    assert player_total(players[0], season=newer) == Decimal(0)
    assert player_balance(players[0], season=newer) == Decimal(0)


def test_financial_view_scoped_to_active_season(
    admin_client, season, matchday_with_players, catalog_normal, admin_user
):
    Season.objects.create(name="2026/2027")  # active by default (newest)
    md, players = matchday_with_players(2, season=season)  # data in the OLD season
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )

    # active season is the new (empty) one -> nothing shows
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.context["team_penalties"] == Decimal(0)
    assert response.context["team_total"] == Decimal(0)

    # switch the global selector back -> the old season's cash box appears
    _set_active_season(admin_client, season)
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert response.context["team_penalties"] == Decimal(5)
    assert response.context["team_total"] == Decimal(5)


def test_payment_is_recorded_in_active_season(
    admin_client, season, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2, season=season)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    response = admin_client.post(
        reverse("penalties:payment_create"),
        {"team": md.team.pk, "player": players[0].pk, "amount_eur": "5"},
    )
    assert response.status_code == 302
    payment = Payment.objects.get()
    assert payment.season == season  # recorded into the globally active season
    assert player_balance(players[0], season=season) == Decimal(0)


def test_own_trio_for_player_role(
    player_client, player_user, season, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2, season=season)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user, season=season)
    player_user.player_link = players[0]
    player_user.save()

    response = player_client.get(reverse("dashboard:financial_overview"))
    assert response.status_code == 200
    assert response.context["own_total"] == Decimal(5)
    assert response.context["own_paid"] == Decimal(2)
    assert response.context["own_balance"] == Decimal(3)


# ---------------------------------------------------------------------------
# Admin-set DEFAULT season vs. personal (session) view choice
# ---------------------------------------------------------------------------
def test_default_season_beats_newest_when_no_session_choice(admin_client, season):
    Season.objects.create(name="2026/2027")  # newest, but not the default
    season.is_default = True
    season.save()

    response = admin_client.get(reverse("dashboard:index"))
    assert response.context["active_season"] == season


def test_only_one_default_season_exists(admin_client, season, team):
    newer = Season.objects.create(name="2026/2027")

    admin_client.post(reverse("season_set_default", args=[newer.pk]))
    assert Season.objects.filter(is_default=True).count() == 1
    assert Season.default().pk == newer.pk

    admin_client.post(reverse("season_set_default", args=[season.pk]))
    assert Season.objects.filter(is_default=True).count() == 1
    assert Season.default().pk == season.pk
    newer.refresh_from_db()
    assert newer.is_default is False


def test_session_choice_never_changes_the_default(admin_user, season):
    """Switching in the navbar is personal and lasts for the session only."""
    from django.test import Client

    newer = Season.objects.create(name="2026/2027")
    season.is_default = True
    season.save()

    first = Client()
    first.force_login(admin_user)
    first.post(reverse("season_set"), {"season": newer.pk, "next": "/"})
    assert first.session["season_id"] == newer.pk
    assert Season.default().pk == season.pk  # global default untouched

    # a second session (same user) falls back to the admin default
    second = Client()
    second.force_login(admin_user)
    response = second.get(reverse("dashboard:index"))
    assert response.context["active_season"] == season


def test_season_create_get_shows_form(admin_client, team):
    response = admin_client.get(reverse("season_create"))
    assert response.status_code == 200
    assert "teams" in response.context["form"].fields
    assert "is_default" in response.context["form"].fields


def test_admin_creates_season_with_active_teams(admin_client, team, other_team):
    response = admin_client.post(
        reverse("season_create"), {"name": "2030/2031", "teams": [team.pk, other_team.pk]}
    )
    assert response.status_code == 302
    created = Season.objects.get(name="2030/2031")
    assert set(created.teams.all()) == {team, other_team}


def test_admin_edits_season_name_and_teams(admin_client, season, team, other_team):
    season.teams.set([team])
    url = reverse("season_update", args=[season.pk])
    assert admin_client.get(url).status_code == 200

    response = admin_client.post(
        url, {"name": "2025/2026 renamed", "teams": [other_team.pk], "is_default": "on"}
    )
    assert response.status_code == 302
    season.refresh_from_db()
    assert season.name == "2025/2026 renamed"
    assert list(season.teams.all()) == [other_team]
    assert season.is_default is True
    assert AuditLog.objects.filter(action=AuditAction.SEASON_UPDATED).exists()


def test_season_list_offers_edit_and_default_actions(admin_client, season):
    content = admin_client.get(reverse("season_list")).content.decode()
    assert reverse("season_update", args=[season.pk]) in content
    assert reverse("season_set_default", args=[season.pk]) in content


def test_navbar_marks_the_default_season(admin_client, season):
    season.is_default = True
    season.save()
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    # "2025/2026 (Standard)" — translated label falls back to "(default)"
    assert "(Standard)" in content or "(default)" in content


# ---------------------------------------------------------------------------
# Teams active per season
# ---------------------------------------------------------------------------
def test_team_list_only_shows_season_teams(admin_client, season, team, other_team):
    season.teams.set([team])
    response = admin_client.get(reverse("teams:team_list"))
    assert list(response.context["teams"]) == [team]


def test_team_list_falls_back_to_all_teams_without_selection(
    admin_client, season, team, other_team
):
    response = admin_client.get(reverse("teams:team_list"))
    assert {t.pk for t in response.context["teams"]} == {team.pk, other_team.pk}


def test_financial_overview_selects_only_season_teams(admin_client, season, team, other_team):
    season.teams.set([other_team])
    response = admin_client.get(reverse("dashboard:financial_overview"))
    assert [t.pk for t in response.context["teams"]] == [other_team.pk]
    assert response.context["selected_team"] == other_team


def test_matchday_form_only_offers_season_teams(admin_client, season, team, other_team):
    season.teams.set([team])
    response = admin_client.get(reverse("matchdays:matchday_create"))
    assert list(response.context["form"].fields["team"].queryset) == [team]
