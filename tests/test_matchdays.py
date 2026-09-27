"""Matchday tests — create/edit/delete guards, chips validation, scoping."""

import re
from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from app.core.models import AuditAction, AuditLog
from app.matchdays.models import Matchday, MatchdayPlayer
from app.penalties.models import Penalty
from app.players.models import Player

pytestmark = pytest.mark.django_db


def _form_data(team, players=None, opponent="SV Eichenberg", venue="home", md_date="2026-09-24"):
    # `players` is ignored on purpose: the form has no participant field —
    # participants are assigned exclusively via the "Add players" editor.
    return {
        "team": str(team.pk),
        "opponent": opponent,
        "venue": venue,
        "date": md_date,
        "description": "",
    }


def _row_for(content, opponent):
    """The ``<tr>`` of the matchday against ``opponent`` in a rendered table."""
    return next(
        row for row in re.findall(r"<tr.*?</tr>", content, flags=re.DOTALL) if opponent in row
    )


def test_admin_creates_matchday_without_participants(admin_client, team):
    """A new matchday starts empty — posted 'participants' data is ignored …"""
    p1 = Player.objects.create(name="A", team=team)
    p2 = Player.objects.create(name="B", team=team)
    response = admin_client.post(reverse("matchdays:matchday_create"), _form_data(team, [p1, p2]))
    assert response.status_code == 302
    matchday = Matchday.objects.get(team=team, opponent="SV Eichenberg")
    assert matchday.participants.count() == 0
    assert matchday.created_by is not None
    assert (
        AuditLog.objects.filter(action=AuditAction.MATCHDAY_CREATED, target_id=matchday.pk).count()
        == 1
    )

    # … the players are added afterwards with the "Add players" button:
    response = admin_client.post(_participants_url(matchday), {"participants": [str(p1.pk)]})
    assert response.status_code == 302
    assert {row.player_id for row in matchday.participants.all()} == {p1.pk}


def test_matchday_form_has_no_participant_field(admin_client, matchday):
    """No participant picker (and no 'at least one participant' rule) in the form."""
    responses = [
        admin_client.get(reverse("matchdays:matchday_create")),
        admin_client.get(reverse("matchdays:matchday_update", args=[matchday.pk])),
    ]
    for response in responses:
        assert response.status_code == 200
        assert "participants" not in response.context["form"].fields
        assert "participant-chips" not in response.content.decode()


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
    matchday = Matchday.objects.get(team=team)
    assert matchday.participants.count() == 0  # added later via "Add players"
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


def test_matchday_list_marks_today_and_mutes_past(admin_client, team):
    """Today's row is flagged green (+ badge), past rows are muted, future plain."""
    today = timezone.localdate()
    Matchday.objects.create(team=team, opponent="Today FC", venue="home", date=today)
    Matchday.objects.create(
        team=team, opponent="Past FC", venue="home", date=today - timedelta(days=7)
    )
    Matchday.objects.create(
        team=team, opponent="Future FC", venue="home", date=today + timedelta(days=7)
    )

    content = admin_client.get(reverse("matchdays:matchday_list")).content.decode()
    assert 'class="matchday-today"' in _row_for(content, "Today FC")
    assert "badge text-bg-success" in _row_for(content, "Today FC")
    assert 'class="matchday-past"' in _row_for(content, "Past FC")
    assert "text-bg-success" not in _row_for(content, "Past FC")
    assert "matchday-past" not in _row_for(content, "Today FC")
    assert "matchday-past" not in _row_for(content, "Future FC")
    assert "matchday-today" not in _row_for(content, "Future FC")


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


def test_inactive_players_not_offered_by_add_players_editor(admin_client, team):
    """Only ACTIVE players of the team/season may be ticked in the editor."""
    active = Player.objects.create(name="Active", team=team)
    inactive = Player.objects.create(name="Inactive", team=team, active=False)

    md = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 1, 1))
    MatchdayPlayer.objects.create(matchday=md, player=active)

    response = admin_client.get(reverse("matchdays:matchday_detail", args=[md.pk]))
    assert response.status_code == 200
    assert set(response.context["eligible_players"]) == {active}

    # a forged POST with the inactive player is rejected — the list stays as is
    admin_client.post(_participants_url(md), {"participants": [str(inactive.pk)]})
    assert {row.player_id for row in md.participants.all()} == {active.pk}


def test_matchday_edit_prefills_existing_date(admin_client, matchday):
    """type="date" must receive an ISO value.

    The German locale format (dd.mm.yyyy) is unparseable for the date input,
    so the browser showed an EMPTY field although the date was stored.
    """
    response = admin_client.get(reverse("matchdays:matchday_update", args=[matchday.pk]))
    assert response.status_code == 200
    assert 'value="2026-09-24"' in str(response.context["form"]["date"])


def test_matchday_edit_never_touches_participants(admin_client, matchday_with_players):
    """The form has no participant field — editing must never wipe the list.

    Neither a successful save nor a failed one (e.g. empty date) may change
    who played: that is exclusively the job of the "Add players" editor.
    """
    md, players = matchday_with_players(3)
    expected = {p.pk for p in players}

    # failed save (date missing) — form error, participants untouched
    response = admin_client.post(
        reverse("matchdays:matchday_update", args=[md.pk]),
        _form_data(md.team, players, opponent="New Opponent", md_date=""),
    )
    assert response.status_code == 200
    assert "date" in response.context["form"].errors
    assert {row.player_id for row in md.participants.all()} == expected

    # successful save — same story
    response = admin_client.post(
        reverse("matchdays:matchday_update", args=[md.pk]),
        _form_data(md.team, players, opponent="New Opponent"),
    )
    assert response.status_code == 302
    md.refresh_from_db()
    assert md.opponent == "New Opponent"
    assert {row.player_id for row in md.participants.all()} == expected


def test_add_players_editor_tints_the_whole_row(admin_client, matchday_with_players):
    """Ticking a player highlights the WHOLE row green — like the assignment matrix."""
    md, players = matchday_with_players(2)
    content = admin_client.get(reverse("matchdays:matchday_detail", args=[md.pk])).content.decode()
    assert 'class="participant-select-row"' in content
    # every roster member is offered, the current participants are pre-checked
    assert content.count('class="participant-select-row"') == len(players)
    assert re.search(rf'value="{players[0].pk}"[^>]*checked', content), "participant not checked"


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


# ---------------------------------------------------------------------------
# Inline participant editor on the matchday detail page
# ---------------------------------------------------------------------------
def _participants_url(matchday):
    return reverse("matchdays:matchday_participants", args=[matchday.pk])


def test_detail_page_offers_inline_participant_editor(admin_client, matchday_with_players):
    md, players = matchday_with_players(2)
    response = admin_client.get(reverse("matchdays:matchday_detail", args=[md.pk]))
    assert response.status_code == 200
    # editor form + full eligible roster (active players of THIS team)
    assert _participants_url(md) in response.content.decode()
    assert {p.pk for p in response.context["eligible_players"]} == {p.pk for p in players}
    # current participants are pre-checked
    assert set(response.context["participant_ids"]) == {p.pk for p in players}


def test_admin_saves_participants_from_detail_page(admin_client, matchday_with_players):
    md, players = matchday_with_players(3)
    response = admin_client.post(_participants_url(md), {"participants": [str(players[0].pk)]})
    assert response.status_code == 302
    assert {row.player_id for row in md.participants.all()} == {players[0].pk}
    assert (
        AuditLog.objects.filter(action=AuditAction.MATCHDAY_UPDATED, target_id=md.pk).count() == 1
    )


def test_participant_editor_rejects_empty_selection(admin_client, matchday_with_players):
    md, _players = matchday_with_players(2)
    response = admin_client.post(_participants_url(md), {})
    assert response.status_code == 302  # back to the detail page with an error
    assert md.participants.count() == 2  # unchanged


def test_participant_editor_rejects_foreign_player(admin_client, other_team, matchday_with_players):
    md, players = matchday_with_players(1)
    foreign = Player.objects.create(name="Foreign", team=other_team)
    response = admin_client.post(
        _participants_url(md),
        {"participants": [str(players[0].pk), str(foreign.pk)]},
    )
    assert response.status_code == 302
    assert {row.player_id for row in md.participants.all()} == {players[0].pk}  # unchanged
    assert foreign.pk not in {row.player_id for row in md.participants.all()}


def test_participant_editor_get_not_allowed(admin_client, matchday_with_players):
    md, _ = matchday_with_players(1)
    assert admin_client.get(_participants_url(md)).status_code == 405


def test_captain_edits_only_own_teams_participants(
    captain_client, team, other_team, matchday_with_players
):
    md, players = matchday_with_players(2)
    foreign_md = Matchday.objects.create(
        team=other_team, opponent="Away", venue="away", date=date(2026, 2, 1)
    )

    response = captain_client.post(_participants_url(md), {"participants": [str(players[0].pk)]})
    assert response.status_code == 302
    assert {row.player_id for row in md.participants.all()} == {players[0].pk}

    assert captain_client.post(_participants_url(foreign_md), {}).status_code == 403


def test_player_role_cannot_edit_participants(player_client, matchday_with_players):
    md, players = matchday_with_players(1)
    response = player_client.post(_participants_url(md), {"participants": [str(players[0].pk)]})
    assert response.status_code == 403
    assert md.participants.count() == 1
