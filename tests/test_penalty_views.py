"""Penalty view + catalog tests — CRUD, toggle, dispatch, permission scoping."""

from decimal import Decimal

import pytest
from django.urls import NoReverseMatch, reverse

from app.core.models import AuditAction, AuditLog
from app.matchdays.models import MatchdayPlayer
from app.penalties.forms import PenaltyAssignForm
from app.penalties.models import Penalty, PenaltyCatalogItem

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def test_catalog_list_admin_and_captain(admin_client, captain_client, player_client):
    assert admin_client.get(reverse("penalties:catalog_list")).status_code == 200
    assert captain_client.get(reverse("penalties:catalog_list")).status_code == 200
    assert player_client.get(reverse("penalties:catalog_list")).status_code == 403


def test_catalog_manage_player_forbidden_captain_allowed(
    player_client, captain_client, team, other_team
):
    """Players stay locked out; captains create/edit catalog entries."""
    assert player_client.get(reverse("penalties:catalog_create")).status_code == 403
    assert (
        player_client.post(
            reverse("penalties:catalog_create"),
            {"description": "X", "amount_eur": 1, "type": "NORMAL"},
        ).status_code
        == 403
    )

    assert captain_client.get(reverse("penalties:catalog_create")).status_code == 200
    response = captain_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Captain entry",
            "amount_eur": 2,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",  # own team ticked (others default off)
            f"team_amount_{team.pk}": "2",  # every fee row must be filled
            f"team_amount_{other_team.pk}": "2",
        },
    )
    assert response.status_code == 302
    item = PenaltyCatalogItem.objects.get(description="Captain entry")
    # Only the captain's own team is active — every other team is inactive
    # (the create form shows ALL teams; others are pre-unchecked).
    own = item.team_amounts.get(team=team)
    assert own.active is True
    other = item.team_amounts.get(team=other_team)
    assert other.active is False
    assert other.amount_eur == Decimal(2)  # filled row (own fee per team)


def test_catalog_admin_crud(admin_client, team, other_team):
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Too loud",
            "amount_eur": 2,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_active_{other_team.pk}": "on",
            f"team_amount_{team.pk}": "2",
            f"team_amount_{other_team.pk}": "2",
        },
    )
    assert response.status_code == 302
    item = PenaltyCatalogItem.objects.get(description="Too loud")
    assert all(row.active for row in item.team_amounts.all())

    # No global "active" field anymore — per-team flags come from the POST.
    response = admin_client.post(
        reverse("penalties:catalog_update", args=[item.pk]),
        {
            "description": "Too loud",
            "amount_eur": 3,
            "type": "NORMAL",
            f"team_amount_{team.pk}": "3",
            f"team_amount_{other_team.pk}": "3",
        },
    )
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.amount_eur == Decimal(3)
    assert not any(row.active for row in item.team_amounts.all())


def test_catalog_list_has_single_edit_action_and_edit_page_has_delete(admin_client, catalog_normal):
    """No toggle URL anymore; one action per row; delete lives on the edit page."""
    with pytest.raises(NoReverseMatch):
        reverse("penalties:catalog_toggle_active", args=[catalog_normal.pk])
    with pytest.raises(NoReverseMatch):
        reverse("penalties:catalog_team_amounts", args=[catalog_normal.pk])

    content = admin_client.get(reverse("penalties:catalog_list")).content.decode()
    edit_url = reverse("penalties:catalog_update", args=[catalog_normal.pk])
    assert edit_url in content
    assert content.count(edit_url) == 1  # exactly ONE action button per row

    edit = admin_client.get(edit_url).content.decode()
    assert reverse("penalties:catalog_delete", args=[catalog_normal.pk]) in edit


def test_catalog_form_rejects_non_positive(admin_client, team):
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Bad",
            "amount_eur": 0,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_amount_{team.pk}": "1",
        },
    )
    assert response.status_code == 200
    assert not PenaltyCatalogItem.objects.filter(description="Bad").exists()


def test_create_requires_filled_team_rows(admin_client, team, other_team):
    """Every fee row must be filled — a blank row blocks the save."""
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Half filled",
            "amount_eur": 5,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_active_{other_team.pk}": "on",
            f"team_amount_{team.pk}": "5",
            # other team's row intentionally left blank
        },
    )
    assert response.status_code == 200  # re-rendered with a required error
    assert not PenaltyCatalogItem.objects.filter(description="Half filled").exists()


def test_assign_form_excludes_inactive_items(matchday_with_players, catalog_normal, other_team):
    """Deactivation is per team: only rows for the matchday's team count."""
    from datetime import date

    from app.matchdays.models import Matchday
    from app.penalties.models import PenaltyType, TeamCatalogAmount

    md, _players = matchday_with_players(2)
    inactive = PenaltyCatalogItem.objects.create(
        description="Old", amount_eur=1, type=PenaltyType.NORMAL
    )
    TeamCatalogAmount.objects.create(
        team=md.team, catalog_item=inactive, active=False, amount_eur=1
    )

    form = PenaltyAssignForm(matchday=md)
    pks = list(form.fields["catalog_item"].queryset.values_list("pk", flat=True))
    assert inactive.pk not in pks
    assert catalog_normal.pk in pks  # no row -> active by default

    # Another team's matchday is unaffected (no row for that team).
    other_md = Matchday.objects.create(
        team=other_team, opponent="X", venue="home", date=date(2026, 1, 1)
    )
    other_pks = list(
        PenaltyAssignForm(matchday=other_md)
        .fields["catalog_item"]
        .queryset.values_list("pk", flat=True)
    )
    assert inactive.pk in other_pks


# ---------------------------------------------------------------------------
# Assignment via views
# ---------------------------------------------------------------------------
def test_assign_normal_via_view(admin_client, matchday_with_players, catalog_normal):
    md, players = matchday_with_players(2)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_normal.pk,
            "player": players[0].pk,
            "amount_eur": "99",  # no amount input anymore — must be ignored
            "description": "Custom desc",
        },
    )
    assert response.status_code == 302
    penalty = Penalty.objects.get()
    assert penalty.player_id == players[0].pk
    assert penalty.amount_eur == Decimal(5)  # the catalog amount
    assert penalty.description_snapshot == "Custom desc"
    assert AuditLog.objects.filter(action=AuditAction.PENALTY_ASSIGNED).count() == 1


def test_assign_group_via_view(admin_client, matchday_with_players, catalog_group):
    md, players = matchday_with_players(4)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_group.pk, "player": players[0].pk},
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 3
    assert not Penalty.objects.filter(player_id=players[0].pk).exists()


def test_assign_group_with_comment_via_view(admin_client, matchday_with_players, catalog_group):
    """The optional comment/reason is stored behind the causers (group penalty)."""
    md, players = matchday_with_players(4)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_group.pk,
            "player": players[0].pk,
            "description": "too late",
        },
    )
    assert response.status_code == 302
    row = Penalty.objects.exclude(player_id=players[0].pk).first()
    assert row.description_snapshot == f"180 — {players[0].name} — too late"
    assert row.display_description() == f"180 ({players[0].name} — too late)"


def test_assign_form_labels_the_comment_field(admin_client, matchday_with_players, catalog_normal):
    """The entry form names the field "Comment / reason" and explains when it is required."""
    md, _players = matchday_with_players(2)
    # Force English: the hand-maintained .mo may be stale, so only the msgid
    # (EN catalog carries empty msgstr entries) is a stable assertion.
    admin_client.post(reverse("set_language"), {"language": "en", "next": "/"})
    # follow=True: the middleware redirects unprefixed URLs to /en/...
    response = admin_client.get(reverse("penalties:penalty_create", args=[md.pk]), follow=True)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Comment / reason" in content
    assert "penalty type (comment)" in content


def test_assign_via_captain_own_team_ok(captain_client, matchday_with_players, catalog_normal):
    md, players = matchday_with_players(2)
    response = captain_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_normal.pk, "player": players[0].pk, "amount_eur": "5"},
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 1


def test_assign_forbidden_for_other_team_and_player(
    captain_client, player_client, other_team, catalog_normal
):
    from datetime import date

    from app.matchdays.models import Matchday
    from app.players.models import Player

    foreign_player = Player.objects.create(name="F", team=other_team)
    md = Matchday.objects.create(team=other_team, opponent="X", venue="home", date=date(2026, 1, 1))
    MatchdayPlayer.objects.create(matchday=md, player=foreign_player)

    assert captain_client.get(reverse("penalties:penalty_create", args=[md.pk])).status_code == 403
    assert player_client.get(reverse("penalties:penalty_create", args=[md.pk])).status_code == 403


def test_edit_via_view_group(admin_client, matchday_with_players, catalog_group):
    md, players = matchday_with_players(3)
    from app.penalties.services import assign_group_penalty

    assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=None
    )
    penalty = Penalty.objects.first()
    response = admin_client.post(
        reverse("penalties:penalty_update", args=[penalty.pk]),
        {"description_snapshot": "Ediert", "amount_eur": "2.50"},
    )
    assert response.status_code == 302
    assert all(p.description_snapshot == "Ediert" for p in Penalty.objects.all())
    assert all(p.amount_eur == Decimal("2.50") for p in Penalty.objects.all())
    assert AuditLog.objects.filter(action=AuditAction.PENALTY_EDITED).count() == 2


def test_delete_via_view_soft_deletes(admin_client, matchday_with_players, catalog_normal):
    md, players = matchday_with_players(2)
    from app.penalties.services import assign_penalty

    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late",
        actor=None,
    )
    response = admin_client.post(reverse("penalties:penalty_delete", args=[penalty.pk]))
    assert response.status_code == 302
    assert Penalty.objects.count() == 0
    assert Penalty.all_objects().count() == 1
    assert AuditLog.objects.filter(action=AuditAction.PENALTY_DELETED).count() == 1


def test_update_deleted_penalty_404(admin_client, matchday_with_players, catalog_normal):
    from app.penalties.services import assign_penalty, soft_delete_penalty

    md, players = matchday_with_players(2)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late",
        actor=None,
    )
    soft_delete_penalty(penalty=penalty, actor=None)
    assert (
        admin_client.get(reverse("penalties:penalty_update", args=[penalty.pk])).status_code == 404
    )


def test_group_penalty_view_single_participant_shows_form_error(
    admin_client, matchday_with_players, catalog_group
):
    """Review fix (M1): 1-participant matchday -> friendly form error, never 500.

    ``assign_group_penalty`` raises a ValidationError the view used to swallow
    into an unhandled 500; the form now validates it and the view is a
    second line of defence.
    """
    md, players = matchday_with_players(1)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_group.pk, "player": players[0].pk},
    )
    assert response.status_code == 200  # form re-rendered, not a 500
    errors = response.context["form"].errors
    flat = " ".join(str(error) for group in errors.values() for error in group)
    assert "participants" in flat or "Teilnehmer" in flat  # en / de catalog
    assert Penalty.objects.count() == 0


# ---------------------------------------------------------------------------
# DOUBLES (Doppelspiel) — catalog flag + optional partner on assignment
# ---------------------------------------------------------------------------
def test_catalog_create_and_edit_with_doubles_flag(admin_client, team):
    """The doubles checkbox is written on create AND on edit (Bearbeiten)."""
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Lowdart",
            "amount_eur": 5,
            "type": "NORMAL",
            "affects_both_players": "on",
            f"team_active_{team.pk}": "on",
            f"team_amount_{team.pk}": "5",
        },
    )
    assert response.status_code == 302
    item = PenaltyCatalogItem.objects.get(description="Lowdart")
    assert item.affects_both_players is True
    created = AuditLog.objects.get(action=AuditAction.CATALOG_ITEM_CREATED, target_id=item.pk)
    assert created.metadata["affects_both_players"] is True

    response = admin_client.post(
        reverse("penalties:catalog_update", args=[item.pk]),
        {
            "description": "Lowdart",
            "amount_eur": 5,
            "type": "NORMAL",
            # checkbox left out -> off again
            f"team_amount_{team.pk}": "5",
        },
    )
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.affects_both_players is False
    updated = AuditLog.objects.get(action=AuditAction.CATALOG_ITEM_UPDATED, target_id=item.pk)
    assert updated.metadata["changes"]["affects_both_players"] == {"from": True, "to": False}


def test_assign_form_offers_partner_only_for_flagged_items(
    matchday_with_players, catalog_normal, catalog_doubles
):
    md, players = matchday_with_players(3)
    form = PenaltyAssignForm(matchday=md)
    assert form.both_ids == [catalog_doubles.pk]
    # The partner select offers exactly the matchday's participants.
    assert set(form.fields["double_partner"].queryset.values_list("pk", flat=True)) == {
        player.pk for player in players
    }

    # A posted partner for an UNFLAGGED item is dropped by clean().
    bound = PenaltyAssignForm(
        data={
            "catalog_item": catalog_normal.pk,
            "player": players[0].pk,
            "double_partner": players[1].pk,
        },
        matchday=md,
    )
    assert bound.is_valid(), bound.errors
    assert bound.cleaned_data["double_partner"] is None


def test_assign_normal_with_partner_via_view(admin_client, matchday_with_players, catalog_doubles):
    md, players = matchday_with_players(4)
    url = reverse("penalties:penalty_create", args=[md.pk])
    response = admin_client.post(
        url,
        {
            "catalog_item": catalog_doubles.pk,
            "player": players[0].pk,
            "double_partner": players[1].pk,
        },
    )
    assert response.status_code == 302
    assert {row.player_id for row in Penalty.objects.all()} == {
        players[0].pk,
        players[1].pk,
    }

    # No partner selected -> only the lead player is charged.
    response = admin_client.post(url, {"catalog_item": catalog_doubles.pk, "player": players[2].pk})
    assert response.status_code == 302
    assert Penalty.objects.filter(player=players[2].pk).count() == 1
    assert Penalty.objects.count() == 3


def test_assign_group_with_partner_via_view(
    admin_client, matchday_with_players, catalog_group_doubles
):
    md, players = matchday_with_players(5)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_group_doubles.pk,
            "player": players[0].pk,
            "double_partner": players[1].pk,
        },
    )
    assert response.status_code == 302
    charged = {row.player_id for row in Penalty.objects.all()}
    assert len(charged) == 3
    assert players[0].pk not in charged and players[1].pk not in charged
    # Both causing players are named in the rows.
    expected = f"{catalog_group_doubles.description} — {players[0].name} & {players[1].name}"
    assert all(row.description_snapshot == expected for row in Penalty.objects.all())


def test_partner_ignored_for_unflagged_item_via_view(
    admin_client, matchday_with_players, catalog_normal
):
    md, players = matchday_with_players(3)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_normal.pk,
            "player": players[0].pk,
            "double_partner": players[1].pk,
        },
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 1  # only the lead player


def test_partner_equal_to_player_shows_form_error(
    admin_client, matchday_with_players, catalog_doubles
):
    md, players = matchday_with_players(3)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_doubles.pk,
            "player": players[0].pk,
            "double_partner": players[0].pk,
        },
    )
    assert response.status_code == 200  # re-rendered with an error, no row
    assert "double_partner" in response.context["form"].errors
    assert Penalty.objects.count() == 0


def test_group_partner_needs_three_participants_via_view(
    admin_client, matchday_with_players, catalog_group_doubles
):
    md, players = matchday_with_players(2)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {
            "catalog_item": catalog_group_doubles.pk,
            "player": players[0].pk,
            "double_partner": players[1].pk,
        },
    )
    assert response.status_code == 200  # form error, never a 500
    errors = response.context["form"].errors
    flat = " ".join(str(error) for group in errors.values() for error in group)
    assert "participants" in flat or "Teilnehmer" in flat  # en / de catalog
    assert Penalty.objects.count() == 0


def test_penalty_form_page_renders_partner_select(
    admin_client, matchday_with_players, catalog_doubles, catalog_normal
):
    """The assign page offers the partner select + the JS gating list (both_ids)."""
    md, _players = matchday_with_players(3)
    content = admin_client.get(reverse("penalties:penalty_create", args=[md.pk])).content.decode()

    assert 'id="id_double_partner"' in content
    # Label + the always-available "no partner" option (en / de catalog).
    assert "Doubles partner" in content or "Doppelspieler" in content
    assert "No doubles partner" in content or "Kein Doppelspieler" in content
    # JS only shows the select for flagged items: exactly catalog_doubles.
    both_ids_line = next(line for line in content.splitlines() if "const bothIds" in line)
    assert str(catalog_doubles.pk) in both_ids_line
    assert str(catalog_normal.pk) not in both_ids_line


def test_group_penalty_row_names_the_trigger_on_matchday_page(
    admin_client, matchday_with_players, catalog_group
):
    """The matchday page shows WHO caused the group penalty (the thrower)."""
    md, players = matchday_with_players(3)
    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_group.pk, "player": players[0].pk},
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 2

    content = admin_client.get(reverse("matchdays:matchday_detail", args=[md.pk])).content.decode()
    # listed as "180 (MP1)" — type first, the causers in brackets
    assert f"{catalog_group.description} ({players[0].name})" in content
    # ...while the thrower themself has NO penalty row.
    assert Penalty.objects.filter(player=players[0]).exists() is False


# ---------------------------------------------------------------------------
# Catalog: duplicate description guard (1. Strafkatalog)
# ---------------------------------------------------------------------------
def test_catalog_create_rejects_duplicate_description(admin_client, team, catalog_normal):
    """Same description (trimmed, case-insensitive) -> form error, no 2nd row."""
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "  late arrival ",  # matches catalog_normal
            "amount_eur": 7,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_amount_{team.pk}": "7",
        },
    )
    assert response.status_code == 200  # re-rendered with the error
    assert "description" in response.context["form"].errors
    assert PenaltyCatalogItem.objects.count() == 1


def test_catalog_update_keeps_own_description(admin_client, team, catalog_normal):
    """Renaming is optional: keeping the OWN description must save (no false hit)."""
    response = admin_client.post(
        reverse("penalties:catalog_update", args=[catalog_normal.pk]),
        {
            "description": catalog_normal.description,
            "amount_eur": 6,
            "type": "NORMAL",
            f"team_amount_{team.pk}": "6",
        },
    )
    assert response.status_code == 302
    catalog_normal.refresh_from_db()
    assert catalog_normal.amount_eur == Decimal(6)


def test_catalog_update_rejects_description_of_other_item(
    admin_client, team, catalog_manual, catalog_normal
):
    response = admin_client.post(
        reverse("penalties:catalog_update", args=[catalog_manual.pk]),
        {
            "description": catalog_normal.description,  # owned by the OTHER item
            "amount_eur": 1,
            "type": "MANUAL",
            f"team_amount_{team.pk}": "1",
        },
    )
    assert response.status_code == 200
    assert "description" in response.context["form"].errors
    catalog_manual.refresh_from_db()
    assert catalog_manual.description == "Manual"  # unchanged


# ---------------------------------------------------------------------------
# DOUBLES: the selected player is not offered in the other dropdown (2.)
# ---------------------------------------------------------------------------
def test_penalty_form_ships_mutual_partner_exclusion(
    admin_client, matchday_with_players, catalog_doubles
):
    """Player -> partner AND partner -> player: the option is taken out of the offer."""
    md, _players = matchday_with_players(3)
    content = admin_client.get(reverse("penalties:penalty_create", args=[md.pk])).content.decode()

    assert "function excludeOption" in content
    assert "opt.disabled = excluded" in content
    assert "opt.hidden = excluded" in content  # not shown, not selectable
    # Both directions are wired (selected player out of the partner list and back).
    assert "excludeOption(partner, player" in content
    assert "excludeOption(player," in content


# ---------------------------------------------------------------------------
# Group penalty delete choice (3.) + readable group headline (4.)
# ---------------------------------------------------------------------------
def _assign_group(matchday_with_players, catalog_group, count=4):
    md, players = matchday_with_players(count)
    from app.penalties.services import assign_group_penalty

    rows = assign_group_penalty(
        matchday=md, trigger_player=players[0], catalog_item=catalog_group, actor=None
    )
    return md, players, rows


def test_delete_group_penalty_single_scope_keeps_other_rows(
    admin_client, matchday_with_players, catalog_group
):
    _md, _players, rows = _assign_group(matchday_with_players, catalog_group)
    response = admin_client.post(
        reverse("penalties:penalty_delete", args=[rows[0].pk]), {"scope": "single"}
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 2  # siblings survive
    assert Penalty.all_objects().count() == 3  # history survives
    assert AuditLog.objects.filter(action=AuditAction.PENALTY_DELETED).count() == 1


def test_delete_group_penalty_group_scope_removes_all_rows(
    admin_client, matchday_with_players, catalog_group
):
    _md, _players, rows = _assign_group(matchday_with_players, catalog_group)
    response = admin_client.post(
        reverse("penalties:penalty_delete", args=[rows[0].pk]), {"scope": "group"}
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 0
    assert Penalty.all_objects().count() == 3
    assert AuditLog.objects.filter(action=AuditAction.PENALTY_DELETED).count() == 3


def test_delete_unknown_scope_falls_back_to_group(
    admin_client, matchday_with_players, catalog_group
):
    _md, _players, rows = _assign_group(matchday_with_players, catalog_group)
    response = admin_client.post(
        reverse("penalties:penalty_delete", args=[rows[0].pk]), {"scope": "bogus"}
    )
    assert response.status_code == 302
    assert Penalty.objects.count() == 0


def test_group_penalty_edit_page_offers_delete_choice_and_names_players(
    admin_client, matchday_with_players, catalog_group
):
    """Headline = affected player names (no raw hex id) + single/group choice."""
    _md, _players, rows = _assign_group(matchday_with_players, catalog_group)
    content = admin_client.get(
        reverse("penalties:penalty_update", args=[rows[0].pk])
    ).content.decode()

    # The choice: ONLY this row vs. the WHOLE group (each with its own confirm).
    assert 'name="scope" value="single"' in content
    assert 'name="scope" value="group"' in content
    # Readable headline: names of the charged players, never the group-id hex.
    assert rows[0].group_id not in content
    for row in rows:
        assert row.player.name in content


def test_single_penalty_edit_page_keeps_plain_delete(
    admin_client, matchday_with_players, catalog_normal
):
    md, players = matchday_with_players(2)
    from app.penalties.services import assign_penalty

    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late",
        actor=None,
    )
    content = admin_client.get(
        reverse("penalties:penalty_update", args=[penalty.pk])
    ).content.decode()

    assert 'name="scope"' not in content  # no group choice without siblings
    assert reverse("penalties:penalty_delete", args=[penalty.pk]) in content
    assert "Editing penalty" in content or "Strafe bearbeiten" in content  # en / de catalog


def test_catalog_list_affects_column_and_scope_labels(
    admin_client, catalog_normal, catalog_group, catalog_manual
):
    """Column header 'Affects' + the scope labels (en / de catalog).

    Old labels were 'Normal' / 'Manual' / 'Per all other matchday players' —
    the type now answers WHO the penalty hits; MANUAL carries the
    'special penalty' tag (its amount is typed individually).
    """
    content = admin_client.get(reverse("penalties:catalog_list")).content.decode()
    assert "<th>Affects</th>" in content or "<th>Betrifft</th>" in content
    assert "<th>Type</th>" not in content and "<th>Typ</th>" not in content
    assert "Affects the player" in content or "Betrifft den Spieler" in content
    assert (
        "Affects all other matchday players" in content
        or "Betrifft alle anderen Spieltagsteilnehmer" in content
    )
    assert (
        "Affects the player – special penalty" in content
        or "Betrifft den Spieler – Sonderstrafe" in content
    )
    # the DB values are untouched (labels are display-only)
    assert {item.type for item in PenaltyCatalogItem.objects.all()} == {
        "NORMAL",
        "PER_ALL_OTHER_MATCHDAY_PLAYERS",
        "MANUAL",
    }
