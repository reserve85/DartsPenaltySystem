"""Per-team catalog activation, guarded delete, data migration, catalog audit.

Covers the plan's new contracts:

* effective availability is purely per-team (no row = active),
* captain-created entries only activate the own team,
* every fee row is always filled (required) and new entries copy the default
  amount into each team row automatically (inline JS),
* a team created later gets the standard fee assigned,
* a team created after an existing item is active by default,
* the team form can assign a season on create,
* deletion is guarded by the linked-penalty check (incl. soft-deleted rows),
* the 0007 data migration backfills globally-inactive items,
* catalog create/update/delete write complete audit entries.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse

from app.core.models import AuditAction, AuditLog
from app.matchdays.models import Matchday
from app.penalties.forms import PenaltyAssignForm
from app.penalties.models import (
    Penalty,
    PenaltyCatalogItem,
    PenaltyType,
    TeamCatalogAmount,
    active_for_team,
)
from app.penalties.services import assign_penalty, soft_delete_penalty
from app.teams.models import Team

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# 1) PenaltyAssignForm — per-team catalog scoping
# ---------------------------------------------------------------------------
def test_penalty_assign_form_is_scoped_per_team(team, other_team):
    deactivated = PenaltyCatalogItem.objects.create(
        description="ForA", amount_eur=1, type=PenaltyType.NORMAL
    )
    only_b = PenaltyCatalogItem.objects.create(
        description="ForB", amount_eur=2, type=PenaltyType.NORMAL
    )
    no_rows = PenaltyCatalogItem.objects.create(
        description="Everyone", amount_eur=3, type=PenaltyType.NORMAL
    )
    TeamCatalogAmount.objects.create(
        team=team, catalog_item=deactivated, active=False, amount_eur=1
    )
    TeamCatalogAmount.objects.create(
        team=other_team, catalog_item=only_b, active=True, amount_eur=2
    )

    md_a = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 1, 1))
    md_b = Matchday.objects.create(
        team=other_team, opponent="X", venue="home", date=date(2026, 1, 1)
    )

    def pks_for(matchday):
        form = PenaltyAssignForm(matchday=matchday)
        return set(form.fields["catalog_item"].queryset.values_list("pk", flat=True))

    pks_a, pks_b = pks_for(md_a), pks_for(md_b)

    # explicit inactive row for A -> excluded for A, included for B (no row)
    assert deactivated.pk not in pks_a
    assert deactivated.pk in pks_b
    # a row for ANOTHER team never affects this team
    assert only_b.pk in pks_a
    assert only_b.pk in pks_b
    # missing rows -> active for every team
    assert no_rows.pk in pks_a
    assert no_rows.pk in pks_b


# ---------------------------------------------------------------------------
# 2) Server-side enforcement on the assignment view
# ---------------------------------------------------------------------------
def test_penalty_assign_form_with_rows_for_every_team(team, other_team):
    """Rows for EVERY team — the exact pattern the catalog form writes.

    Regression: the old ``~Q(team_amounts__team=…, team_amounts__active=False)``
    compiled into ``NOT (any inactive row exists AND any row for the team
    exists)`` — an item whose OTHER team's row was inactive was therefore
    hidden from its OWN active team, so captains only saw row-less items
    (e.g. "Manual") in the "Penalty type" dropdown.
    """
    item = PenaltyCatalogItem.objects.create(
        description="Mixed", amount_eur=2, type=PenaltyType.NORMAL
    )
    TeamCatalogAmount.objects.create(team=team, catalog_item=item, active=True, amount_eur=2)
    TeamCatalogAmount.objects.create(team=other_team, catalog_item=item, active=False, amount_eur=2)
    no_rows = PenaltyCatalogItem.objects.create(
        description="No rows", amount_eur=1, type=PenaltyType.MANUAL
    )

    md_a = Matchday.objects.create(team=team, opponent="X", venue="home", date=date(2026, 1, 1))
    md_b = Matchday.objects.create(
        team=other_team, opponent="X", venue="home", date=date(2026, 1, 1)
    )

    def pks_for(matchday):
        form = PenaltyAssignForm(matchday=matchday)
        return set(form.fields["catalog_item"].queryset.values_list("pk", flat=True))

    # active for A although the row of the OTHER team is inactive
    assert item.pk in pks_for(md_a)
    assert no_rows.pk in pks_for(md_a)
    # deactivated for B
    assert item.pk not in pks_for(md_b)
    assert no_rows.pk in pks_for(md_b)


def test_captain_created_entry_selectable_on_own_matchday(
    captain_client, team, other_team, matchday_with_players
):
    """End-to-end of the report: a captain adds a catalog entry and must then
    find it in "Penalty type" when assigning a penalty on their own matchday."""
    response = captain_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Everyone pays",
            "amount_eur": "4",
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",  # own team pre-checked in the UI
            f"team_amount_{team.pk}": "4",
            f"team_amount_{other_team.pk}": "4",
        },
    )
    assert response.status_code == 302
    created = PenaltyCatalogItem.objects.get(description="Everyone pays")

    md, _players = matchday_with_players(2)  # matchday of the captain's team
    response = captain_client.get(reverse("penalties:penalty_create", args=[md.pk]))
    assert response.status_code == 200
    offered = set(
        response.context["form"].fields["catalog_item"].queryset.values_list("pk", flat=True)
    )
    assert created.pk in offered


def test_penalty_create_view_rejects_deactivated_item(
    admin_client, matchday_with_players, catalog_normal
):
    md, players = matchday_with_players(2)
    TeamCatalogAmount.objects.create(
        team=md.team, catalog_item=catalog_normal, active=False, amount_eur=5
    )

    response = admin_client.post(
        reverse("penalties:penalty_create", args=[md.pk]),
        {"catalog_item": catalog_normal.pk, "player": players[0].pk},
    )
    assert response.status_code == 200  # re-rendered with a validation error
    assert "catalog_item" in response.context["form"].errors
    assert Penalty.objects.count() == 0


# ---------------------------------------------------------------------------
# 3) Captain-created entry: own team active, all others explicitly inactive
# ---------------------------------------------------------------------------
def test_captain_create_activates_only_own_team(captain_client, team, other_team):
    response = captain_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Captain scope",
            "amount_eur": 3,
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_amount_{team.pk}": "3",  # own row (auto-copied by JS in the UI)
            f"team_amount_{other_team.pk}": "3",  # all rows are visible + required
            # other teams' active flags not posted -> default INACTIVE
        },
    )
    assert response.status_code == 302
    item = PenaltyCatalogItem.objects.get(description="Captain scope")
    assert item.is_active_for_team(team) is True
    assert item.is_active_for_team(other_team) is False
    other_row = item.team_amounts.get(team=other_team)
    assert other_row.active is False
    assert other_row.amount_eur == Decimal(3)  # filled row (fee always required)


# ---------------------------------------------------------------------------
# 4) New entries: the default amount is copied into every team row
#    (inline JS on create — see catalog_form.html; required rows server-side)
# ---------------------------------------------------------------------------
def test_admin_create_stores_individual_team_fees(admin_client, team, other_team):
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Loud",
            "amount_eur": "7",
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_active_{other_team.pk}": "on",
            f"team_amount_{team.pk}": "7",
            f"team_amount_{other_team.pk}": "6.50",  # adjusted individually
        },
    )
    assert response.status_code == 302
    item = PenaltyCatalogItem.objects.get(description="Loud")
    rows = {row.team_id: row for row in item.team_amounts.all()}
    assert rows[team.pk].amount_eur == Decimal(7)
    assert rows[other_team.pk].amount_eur == Decimal("6.50")


def test_create_form_ships_auto_copy_script(admin_client, team):
    """The create page auto-copies the typed default into every fee row (JS)."""
    content = admin_client.get(reverse("penalties:catalog_create")).content.decode()
    assert "input[id^='id_team_amount_']" in content  # auto-copy selector
    assert f"id_team_amount_{team.pk}" in content  # one input per team
    # no "apply all" checkbox anymore
    assert "id_apply_all" not in content


# ---------------------------------------------------------------------------
# 5) A team created later is active by default (no row = active)
# ---------------------------------------------------------------------------
def test_new_team_is_active_by_default(catalog_normal, team):
    assert catalog_normal.is_active_for_team(team) is True  # no row yet
    newcomer = Team.objects.create(name="Newcomers")
    assert catalog_normal.is_active_for_team(newcomer) is True
    assert (
        PenaltyCatalogItem.objects.filter(active_for_team(newcomer))
        .filter(pk=catalog_normal.pk)
        .exists()
    )


# ---------------------------------------------------------------------------
# 6) Team form: season assignment on create only
# ---------------------------------------------------------------------------
def test_team_create_can_assign_season(admin_client, season):
    response = admin_client.post(
        reverse("teams:team_create"), {"name": "Fresh", "season": season.pk}
    )
    assert response.status_code == 302
    fresh = Team.objects.get(name="Fresh")
    assert fresh in season.teams.all()

    # audit metadata carries the chosen season
    entry = AuditLog.objects.get(action=AuditAction.TEAM_CREATED, target_id=fresh.pk)
    assert entry.metadata["season_id"] == season.pk

    # without a season -> no assignment
    response = admin_client.post(reverse("teams:team_create"), {"name": "NoSeason"})
    assert response.status_code == 302
    assert Team.objects.get(name="NoSeason") not in season.teams.all()

    # the season field is create-only (edit form has no season field)
    response = admin_client.get(reverse("teams:team_update", args=[fresh.pk]))
    assert response.status_code == 200
    assert "season" not in response.context["form"].fields


def test_team_create_assigns_standard_fee_to_every_catalog_item(
    admin_client, catalog_normal, catalog_group
):
    """A NEW team gets the standard fee assigned for every catalog entry."""
    response = admin_client.post(reverse("teams:team_create"), {"name": "Fresh Fee Team"})
    assert response.status_code == 302
    team = Team.objects.get(name="Fresh Fee Team")
    rows = {row.catalog_item_id: row for row in team.catalog_amounts.all()}
    assert set(rows) == {catalog_normal.pk, catalog_group.pk}
    assert rows[catalog_normal.pk].amount_eur == catalog_normal.amount_eur
    assert rows[catalog_group.pk].amount_eur == catalog_group.amount_eur
    assert all(row.active for row in rows.values())
    entry = AuditLog.objects.get(action=AuditAction.TEAM_CREATED, target_id=team.pk)
    assert entry.metadata["catalog_fees_assigned"] == 2


# ---------------------------------------------------------------------------
# 7) Guarded delete (Admin + Captain, only while unused)
# ---------------------------------------------------------------------------
def test_admin_deletes_unused_item_and_audits(admin_client, catalog_normal):
    url = reverse("penalties:catalog_delete", args=[catalog_normal.pk])
    response = admin_client.post(url)
    assert response.status_code == 302
    assert not PenaltyCatalogItem.objects.filter(pk=catalog_normal.pk).exists()
    entry = AuditLog.objects.get(action=AuditAction.CATALOG_ITEM_DELETED)
    assert entry.target_type == "PenaltyCatalogItem"
    assert entry.target_id == catalog_normal.pk
    assert entry.metadata["description"] == catalog_normal.description


def test_delete_blocked_while_penalty_references_item(
    admin_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late",
        actor=admin_user,
    )
    response = admin_client.post(reverse("penalties:catalog_delete", args=[catalog_normal.pk]))
    assert response.status_code == 302  # redirects back with an error message
    assert PenaltyCatalogItem.objects.filter(pk=catalog_normal.pk).exists()
    assert AuditLog.objects.filter(action=AuditAction.CATALOG_ITEM_DELETED).count() == 0


def test_delete_blocked_even_for_soft_deleted_penalty(
    admin_client, matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        amount_eur=5,
        description_snapshot="Late",
        actor=admin_user,
    )
    soft_delete_penalty(penalty=penalty, actor=admin_user)  # hidden from the default manager
    assert Penalty.objects.count() == 0

    response = admin_client.post(reverse("penalties:catalog_delete", args=[catalog_normal.pk]))
    assert response.status_code == 302
    assert PenaltyCatalogItem.objects.filter(pk=catalog_normal.pk).exists()  # still blocked


def test_captain_deletes_unused_item(captain_client, catalog_normal):
    url = reverse("penalties:catalog_delete", args=[catalog_normal.pk])
    response = captain_client.post(url)
    assert response.status_code == 302
    assert not PenaltyCatalogItem.objects.filter(pk=catalog_normal.pk).exists()
    assert AuditLog.objects.filter(action=AuditAction.CATALOG_ITEM_DELETED).count() == 1


def test_player_forbidden_and_get_not_served(player_client, catalog_normal):
    url = reverse("penalties:catalog_delete", args=[catalog_normal.pk])
    assert player_client.post(url).status_code == 403
    assert player_client.get(url).status_code == 405  # POST-only, no GET handler


def test_anonymous_delete_redirects_to_login(db, catalog_normal):
    response = Client().post(reverse("penalties:catalog_delete", args=[catalog_normal.pk]))
    assert response.status_code == 302
    assert "login" in response.url


# ---------------------------------------------------------------------------
# 8) Data migration: globally-inactive items -> per-team rows
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_migration_backfills_globally_inactive_items():
    """Item with active=False before 0007 => active=False row for every team."""
    executor = MigrationExecutor(connection)
    old_node = ("penalties", "0006_payment_season")

    # (a) migrate DOWN one step: the global `active` column exists again
    executor.migrate([old_node])
    old_apps = executor.loader.project_state([old_node]).apps
    OldItem = old_apps.get_model("penalties", "PenaltyCatalogItem")
    OldTeam = old_apps.get_model("teams", "Team")

    inactive = OldItem.objects.create(description="Globally off", amount_eur=1, active=False)
    active = OldItem.objects.create(description="Globally on", amount_eur=2, active=True)
    team_a = OldTeam.objects.create(name="Backfill A")
    team_b = OldTeam.objects.create(name="Backfill B")

    # (b) migrate UP again: runs the RunPython backfill + drops the column
    executor.loader.build_graph()
    leaf = executor.loader.graph.leaf_nodes("penalties")
    executor.migrate(leaf)
    new_apps = executor.loader.project_state(leaf).apps
    NewItem = new_apps.get_model("penalties", "PenaltyCatalogItem")
    Row = new_apps.get_model("penalties", "TeamCatalogAmount")

    # globally inactive -> an explicit inactive row for EVERY existing team,
    # seeded with the item's default amount (every row is always filled)
    for team_pk in (team_a.pk, team_b.pk):
        row = Row.objects.get(catalog_item_id=inactive.pk, team_id=team_pk)
        assert row.active is False
        assert row.amount_eur == Decimal(1)
    # active item -> NO rows created (missing row already means active)
    assert not Row.objects.filter(catalog_item_id=active.pk).exists()
    # and the global column is gone from the new state
    from django.core.exceptions import FieldDoesNotExist

    with pytest.raises(FieldDoesNotExist):
        NewItem._meta.get_field("active")


# ---------------------------------------------------------------------------
# 9) Catalog audit entries
# ---------------------------------------------------------------------------
def test_catalog_create_writes_audit_metadata(admin_user, admin_client, team, other_team):
    response = admin_client.post(
        reverse("penalties:catalog_create"),
        {
            "description": "Noisy",
            "amount_eur": "4",
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",  # other team stays inactive
            f"team_amount_{team.pk}": "4",
            f"team_amount_{other_team.pk}": "3",  # individual fee per team
        },
    )
    assert response.status_code == 302
    entry = AuditLog.objects.get(action=AuditAction.CATALOG_ITEM_CREATED)
    assert entry.user == admin_user
    assert entry.target_type == "PenaltyCatalogItem"
    assert entry.metadata["description"] == "Noisy"
    assert entry.metadata["amount_eur"] == "4.00"
    assert entry.metadata["type"] == "NORMAL"
    assert entry.metadata["active_teams"] == [team.pk]
    assert entry.metadata["team_amounts"] == {str(team.pk): "4.00", str(other_team.pk): "3.00"}


def test_catalog_update_diff_includes_team_fee(admin_client, catalog_normal, team, other_team):
    TeamCatalogAmount.objects.create(team=team, catalog_item=catalog_normal, amount_eur=1)
    TeamCatalogAmount.objects.create(team=other_team, catalog_item=catalog_normal, amount_eur=5)
    response = admin_client.post(
        reverse("penalties:catalog_update", args=[catalog_normal.pk]),
        {
            "description": "Late arrival renamed",
            "amount_eur": "5.00",
            "type": "NORMAL",
            f"team_active_{team.pk}": "on",
            f"team_active_{other_team.pk}": "on",
            f"team_amount_{team.pk}": "2.50",
            f"team_amount_{other_team.pk}": "5.00",
        },
    )
    assert response.status_code == 302
    entry = AuditLog.objects.get(action=AuditAction.CATALOG_ITEM_UPDATED)
    changes = entry.metadata["changes"]
    # core field diff …
    assert changes["description"] == {"from": "Late arrival", "to": "Late arrival renamed"}
    # … plus the per-team fee diff in the same entry
    assert changes[f"team_{team.pk}_amount"] == {"from": "1.00", "to": "2.50"}
    # unchanged fields stay out of the diff
    assert "amount_eur" not in changes
    assert "type" not in changes


def test_audit_list_offers_new_actions_in_filter(admin_client):
    response = admin_client.get(reverse("audit_list"))
    assert response.status_code == 200
    actions = dict(response.context["actions"])
    for value in (
        "catalog_item_created",
        "catalog_item_updated",
        "catalog_item_deleted",
        "email_sent",
        "email_failed",
    ):
        assert value in actions
