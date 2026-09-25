"""Matchday tests — create/edit/delete guards, chips validation, scoping."""

import re
from datetime import date

import pytest
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.matchdays.models import Matchday, MatchdayPlayer
from app.penalties.models import Penalty
from app.players.models import Player

pytestmark = pytest.mark.django_db


def _form_data(team, players, opponent="SV Eichenberg", venue="home", md_date="2026-09-24"):
    return {
        "team": str(team.pk),
        "opponent": opponent,
        "venue": venue,
        "date": md_date,
        "description": "",
        "participants": [str(p.pk) for p in players],
    }


def test_admin_creates_matchday_with_participants(admin_client, team):
    p1 = Player.objects.create(name="A", team=team)
    p2 = Player.objects.create(name="B", team=team)
    response = admin_client.post(reverse("matchdays:matchday_create"), _form_data(team, [p1, p2]))
    assert response.status_code == 302
    matchday = Matchday.objects.get(team=team, opponent="SV Eichenberg")
    assert matchday.participants.count() == 2
    assert matchday.created_by is not None
    assert (
        AuditLog.objects.filter(action=AuditAction.MATCHDAY_CREATED, target_id=matchday.pk).count()
        == 1
    )


def test_matchday_requires_at_least_one_participant(admin_client, team):
    response = admin_client.post(reverse("matchdays:matchday_create"), _form_data(team, []))
    assert response.status_code == 200
    assert not Matchday.objects.filter(team=team).exists()


def test_matchday_opponent_required(admin_client, team):
    p = Player.objects.create(name="A", team=team)
    data = _form_data(team, [p])
    data["opponent"] = ""
    response = admin_client.post(reverse("matchdays:matchday_create"), data)
    assert response.status_code == 200
    assert not Matchday.objects.filter(team=team).exists()


def test_matchday_venue_choices_home_away(admin_client, team):
    p = Player.objects.create(name="A", team=team)
    for venue in ("home", "away"):
        response = admin_client.post(
            reverse("matchdays:matchday_create"), _form_data(team, [p], venue=venue)
        )
        assert response.status_code == 302
    assert Matchday.objects.filter(venue="home").count() == 1
    assert Matchday.objects.filter(venue="away").count() == 1


def test_same_player_cannot_be_added_twice(team):
    p = Player.objects.create(name="A", team=team)
    md = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 1, 1))
    MatchdayPlayer.objects.create(matchday=md, player=p)
    p2 = Player.objects.create(name="B", team=team)
    MatchdayPlayer.objects.create(matchday=md, player=p2)
    assert md.participants.count() == 2


def test_captain_creates_matchday_for_own_team(captain_client, team, other_team):
    own = Player.objects.create(name="Own", team=team)
    response = captain_client.post(reverse("matchdays:matchday_create"), _form_data(team, [own]))
    assert response.status_code == 302
    assert Matchday.objects.get(team=team).participants.count() == 1
    assert not Matchday.objects.filter(team=other_team).exists()


def test_captain_list_only_own_team(captain_client, team, other_team):
    Matchday.objects.create(team=team, opponent="A", venue="home", date=date(2026, 1, 1))
    Matchday.objects.create(team=other_team, opponent="B", venue="home", date=date(2026, 1, 2))
    response = captain_client.get(reverse("matchdays:matchday_list"))
    assert response.status_code == 200
    opponents = {m.opponent for m in response.context["page_obj"]}
    assert opponents == {"A"}


def test_admin_team_filter(admin_client, team, other_team):
    """Admin picks a team in the dropdown -> only that team's matchdays."""
    md_a = Matchday.objects.create(team=team, opponent="A", venue="home", date=date(2026, 1, 1))
    md_b = Matchday.objects.create(
        team=other_team, opponent="B", venue="away", date=date(2026, 1, 2)
    )
    url = reverse("matchdays:matchday_list")

    shown = {m.pk for m in admin_client.get(url, {"team": other_team.pk}).context["page_obj"]}
    assert shown == {md_b.pk}

    shown = {m.pk for m in admin_client.get(url, {"team": ""}).context["page_obj"]}
    assert shown == {md_a.pk, md_b.pk}  # empty value = all teams

    assert admin_client.get(url, {"team": "abc"}).status_code == 404  # not a 500


def test_admin_sorting(admin_client, team, other_team):
    """Column headers sort via ?sort=…&dir=…; unknown keys fall back to date (ASC)."""
    Matchday.objects.create(team=team, opponent="Zebra", venue="home", date=date(2026, 3, 1))
    Matchday.objects.create(team=team, opponent="Alpha", venue="away", date=date(2026, 1, 1))
    Matchday.objects.create(team=other_team, opponent="Mid", venue="home", date=date(2026, 2, 1))
    url = reverse("matchdays:matchday_list")

    opponents = [
        m.opponent
        for m in admin_client.get(url, {"sort": "opponent", "dir": "asc"}).context["page_obj"]
    ]
    assert opponents == ["Alpha", "Mid", "Zebra"]

    dates = [
        m.date for m in admin_client.get(url, {"sort": "date", "dir": "asc"}).context["page_obj"]
    ]
    assert dates == sorted(dates)

    # default / unknown sort key: date ascending (oldest first)
    dates = [m.date for m in admin_client.get(url, {"sort": "bogus"}).context["page_obj"]]
    assert dates == sorted(dates)


def test_matchday_list_default_sort_is_ascending(admin_client, team):
    """Without any query params the oldest matchdays come first (ASC)."""
    Matchday.objects.create(team=team, opponent="New", venue="home", date=date(2026, 6, 1))
    Matchday.objects.create(team=team, opponent="Old", venue="home", date=date(2026, 1, 1))
    page = admin_client.get(reverse("matchdays:matchday_list")).context["page_obj"]
    assert [m.opponent for m in page] == ["Old", "New"]


def test_captain_team_filter_ignored(captain_client, team, other_team):
    """Captains are locked to their own team — ?team= never widens the scope."""
    Matchday.objects.create(team=team, opponent="A", venue="home", date=date(2026, 1, 1))
    Matchday.objects.create(team=other_team, opponent="B", venue="home", date=date(2026, 1, 2))
    response = captain_client.get(reverse("matchdays:matchday_list"), {"team": other_team.pk})
    assert response.status_code == 200
    assert {m.opponent for m in response.context["page_obj"]} == {"A"}


def test_team_selector_only_offered_to_admin(admin_client, captain_client, team):
    content = admin_client.get(reverse("matchdays:matchday_list")).content.decode()
    assert 'name="team"' in content
    content = captain_client.get(reverse("matchdays:matchday_list")).content.decode()
    assert 'name="team"' not in content


def test_captain_cannot_manage_other_team_matchday(captain_client, other_team):
    p = Player.objects.create(name="F", team=other_team)
    md = Matchday.objects.create(team=other_team, opponent="X", venue="home", date=date(2026, 1, 1))
    MatchdayPlayer.objects.create(matchday=md, player=p)
    assert captain_client.get(reverse("matchdays:matchday_detail", args=[md.pk])).status_code == 403
    assert captain_client.get(reverse("matchdays:matchday_update", args=[md.pk])).status_code == 403
    assert (
        captain_client.post(reverse("matchdays:matchday_delete", args=[md.pk])).status_code == 403
    )


def test_player_role_forbidden_on_matchdays(player_client, team):
    assert player_client.get(reverse("matchdays:matchday_list")).status_code == 403
    assert player_client.get(reverse("matchdays:matchday_create")).status_code == 403


def test_inactive_players_not_selectable(admin_client, team):
    active = Player.objects.create(name="Active", team=team)
    Player.objects.create(name="Inactive", team=team, active=False)

    md = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 1, 1))
    MatchdayPlayer.objects.create(matchday=md, player=active)

    response = admin_client.get(reverse("matchdays:matchday_update", args=[md.pk]))
    assert response.status_code == 200
    participant_qs = response.context["form"].fields["participants"].queryset
    available_names = set(participant_qs.values_list("name", flat=True))
    assert available_names == {"Active"}


def test_matchday_edit_prefills_existing_date(admin_client, matchday):
    """type="date" must receive an ISO value.

    The German locale format (dd.mm.yyyy) is unparseable for the date input,
    so the browser showed an EMPTY field although the date was stored.
    """
    response = admin_client.get(reverse("matchdays:matchday_update", args=[matchday.pk]))
    assert response.status_code == 200
    assert 'value="2026-09-24"' in str(response.context["form"]["date"])


def test_matchday_edit_keeps_participants_checked_after_error(admin_client, matchday_with_players):
    """Chips must stay checked when the save fails (e.g. empty date).

    Otherwise the next submit sends no participants and dies with
    "This field is required." while the user only wanted to assign players.
    """
    md, players = matchday_with_players(3)
    data = _form_data(md.team, players, md_date="")  # date missing -> form error
    response = admin_client.post(reverse("matchdays:matchday_update", args=[md.pk]), data)
    assert response.status_code == 200
    assert "date" in response.context["form"].errors
    html = response.content.decode()
    for p in players:
        assert re.search(rf'value="{p.pk}"[^>]*checked', html), f"chip for {p.name} lost its state"


# ---------------------------------------------------------------------------
# B2 delete guard
# ---------------------------------------------------------------------------
def test_matchday_delete_guard_with_penalties(admin_client, matchday_with_players, catalog_normal):
    md, players = matchday_with_players(2)
    Penalty.objects.create(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late",
        amount_eur=5,
    )
    response = admin_client.post(reverse("matchdays:matchday_delete", args=[md.pk]))
    assert response.status_code == 302
    assert Matchday.objects.filter(pk=md.pk).exists()
    assert AuditLog.objects.filter(action=AuditAction.MATCHDAY_DELETED).count() == 0


def test_matchday_delete_guard_with_soft_deleted_penalties(
    admin_client, matchday_with_players, catalog_normal
):
    from django.utils import timezone

    md, players = matchday_with_players(2)
    penalty = Penalty.objects.create(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late",
        amount_eur=5,
    )
    penalty.deleted_at = timezone.now()
    penalty.save()
    response = admin_client.post(reverse("matchdays:matchday_delete", args=[md.pk]))
    assert response.status_code == 302
    assert Matchday.objects.filter(pk=md.pk).exists()


def test_empty_matchday_deleted_and_audited(captain_client, team):
    md = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 1, 1))
    response = captain_client.post(reverse("matchdays:matchday_delete", args=[md.pk]))
    assert response.status_code == 302
    assert not Matchday.objects.filter(pk=md.pk).exists()
    assert (
        AuditLog.objects.filter(action=AuditAction.MATCHDAY_DELETED, target_id=md.pk).count() == 1
    )


def test_matchday_update_reflects_in_audit(admin_client, team, matchday):
    p = Player.objects.create(name="P", team=team)
    data = _form_data(team, [p], opponent="New Opponent")
    response = admin_client.post(reverse("matchdays:matchday_update", args=[matchday.pk]), data)
    assert response.status_code == 302
    matchday.refresh_from_db()
    assert matchday.opponent == "New Opponent"
    assert (
        AuditLog.objects.filter(action=AuditAction.MATCHDAY_UPDATED, target_id=matchday.pk).count()
        == 1
    )
