"""Smoke tests — every URL renders 200 for its intended role, 403/302 otherwise."""

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


PUBLIC_URLS = ["health", "imprint", "privacy", "account_login"]

ADMIN_URLS = [
    "dashboard:index",
    "dashboard:financial_overview",
    "teams:team_list",
    "teams:team_create",
    "players:player_create",
    "matchdays:matchday_list",
    "matchdays:matchday_create",
    "penalties:catalog_list",
    "penalties:catalog_create",
    "accounts:user_list",
    "accounts:user_create",
    "accounts:invite_list",
    "accounts:invite_create",
    "accounts:settings",
    "account_change_password",
    "audit_list",
]

CAPTAIN_URLS = [
    "dashboard:index",
    "dashboard:financial_overview",
    "teams:team_list",
    "matchdays:matchday_list",
    "matchdays:matchday_create",
    "penalties:catalog_list",
    "accounts:settings",
]

PLAYER_URLS = [
    "dashboard:index",
    "dashboard:financial_overview",
    "accounts:settings",
]


@pytest.mark.parametrize("url_name", PUBLIC_URLS)
def test_public_urls_200(url_name):
    assert Client().get(reverse(url_name)).status_code == 200


@pytest.mark.parametrize("url_name", ADMIN_URLS)
def test_admin_urls_200(admin_client, url_name):
    assert admin_client.get(reverse(url_name)).status_code == 200


@pytest.mark.parametrize("url_name", CAPTAIN_URLS)
def test_captain_urls_200(captain_client, url_name):
    assert captain_client.get(reverse(url_name)).status_code == 200


@pytest.mark.parametrize("url_name", PLAYER_URLS)
def test_player_urls_200(player_client, url_name):
    assert player_client.get(reverse(url_name)).status_code == 200


def test_detail_pages_200(admin_client, team, matchday, catalog_normal, player):
    assert (
        admin_client.get(reverse("matchdays:matchday_detail", args=[matchday.pk])).status_code
        == 200
    )
    assert (
        admin_client.get(reverse("penalties:catalog_update", args=[catalog_normal.pk])).status_code
        == 200
    )


def test_catalog_list_drops_amount_and_global_active_columns(admin_client, catalog_normal):
    """Columns are Description | Type | Team fees | actions — no global Amount/Active."""
    content = admin_client.get(reverse("penalties:catalog_list")).content.decode()
    # base.html carries no table headers, so a page-wide count is exact:
    # 4x bare <th> + 1x <thead> is NOT counted (<th> without attributes only).
    assert content.count("<th>") == 4
    assert content.count("<thead>") == 1


def test_old_team_detail_redirects_to_financial_overview(admin_client, team):
    response = admin_client.get(reverse("teams:team_detail", args=[team.pk]))
    assert response.status_code == 302
    assert response.url == f"{reverse('dashboard:financial_overview')}?team={team.pk}"


def test_old_player_list_redirects_to_combined_page(admin_client, captain_client):
    for client in (admin_client, captain_client):
        response = client.get(reverse("players:player_list"))
        assert response.status_code == 302
        assert response.url == reverse("teams:team_list")


def test_captain_detail_pages_200(captain_client, matchday):
    assert (
        captain_client.get(reverse("matchdays:matchday_detail", args=[matchday.pk])).status_code
        == 200
    )


def test_captain_cannot_open_admin_only_pages(captain_client):
    assert captain_client.get(reverse("accounts:user_list")).status_code == 403
    assert captain_client.get(reverse("audit_list")).status_code == 403


def test_player_redirected_from_management_pages(player_client):
    for url_name in ["teams:team_list", "players:player_list", "accounts:user_list"]:
        response = player_client.get(reverse(url_name))
        assert response.status_code in (302, 403), url_name


def test_anonymous_redirected_to_login(db):
    for url_name in ["dashboard:index", "accounts:settings", "teams:team_list"]:
        response = Client().get(reverse(url_name))
        assert response.status_code == 302
        assert "login" in response.url


# ---------------------------------------------------------------------------
# Navigation: Übersicht/Finanzen | Team area | Administration
# ---------------------------------------------------------------------------
def test_admin_sees_all_nav_groups(admin_client):
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    assert "nav-sep" in content  # group separators
    # One single entry: the start page IS the financial overview now.
    assert f'href="{reverse("dashboard:index")}"' in content
    assert reverse("dashboard:financial_overview") not in content
    assert reverse("teams:team_list") in content  # team area (combined page)
    assert reverse("accounts:user_list") in content  # admin area
    assert reverse("season_list") in content


def test_admin_menu_order_seasons_users_audit(admin_client):
    """Administration dropdown: Seasons first, Users second, Audit last."""
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    seasons = content.index(reverse("season_list"))
    users = content.index(reverse("accounts:user_list"))
    audit = content.index(reverse("audit_list"))
    assert seasons < users < audit


def test_header_order_financial_matchdays_administration(admin_client):
    """Header: Finanzübersicht – Spieltage – Verwaltung (no team folder anymore)."""
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    financial = content.index(f'href="{reverse("dashboard:index")}"')
    matchdays = content.index(f'href="{reverse("matchdays:matchday_list")}"')
    administration = content.index(f'href="{reverse("season_list")}"')
    assert financial < matchdays < administration
    # Mannschaften & Spieler + Strafkatalog moved INTO the Verwaltung dropdown …
    assert reverse("teams:team_list") in content
    assert reverse("penalties:catalog_list") in content
    # … and the old "Mannschaft" folder (team area dropdown) is gone:
    # only ONE group separator remains (financial | Spieltage + Verwaltung).
    assert content.count("nav-sep") == 1
    assert 'href="#" role="button" data-bs-toggle="dropdown"' in content  # Verwaltung


def test_delete_lives_on_the_edit_page_deactivation_is_the_checkbox(admin_client, team, player):
    """The list only shows 'Bearbeiten' — Löschen sits on the edit page.

    Deactivation has no button anymore: the 'Active' checkbox of the edit form
    is the single switch (a separate 'Deactivate' button was redundant).
    """
    content = admin_client.get(reverse("teams:team_list")).content.decode()
    assert reverse("teams:team_delete", args=[team.pk]) not in content
    assert reverse("players:player_deactivate", args=[player.pk]) not in content

    content = admin_client.get(reverse("teams:team_update", args=[team.pk])).content.decode()
    assert reverse("teams:team_delete", args=[team.pk]) in content

    response = admin_client.get(reverse("players:player_update", args=[player.pk]))
    assert reverse("players:player_deactivate", args=[player.pk]) not in response.content.decode()
    assert "active" in response.context["form"].fields  # …the checkbox does the job
    # …and the assignment is NOT edited here anymore (the matrix owns it).
    assert "teams" not in response.context["form"].fields


def test_delete_lives_on_the_edit_page_app_wide(
    admin_client, player_user, season, matchday_with_players, catalog_normal, admin_user
):
    """Detail/list pages only offer "Edit" — every delete sits on the edit page.

    Applies to matchdays, penalties and seasons; deactivating a user has no
    button either (the edit form's "Active" checkbox owns it, like players).
    """
    from app.penalties.services import assign_penalty

    md, players = matchday_with_players(1)
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )

    # --- detail / list pages: no delete affordance at all -------------------
    detail = admin_client.get(reverse("matchdays:matchday_detail", args=[md.pk])).content.decode()
    assert reverse("matchdays:matchday_delete", args=[md.pk]) not in detail
    assert reverse("penalties:penalty_delete", args=[penalty.pk]) not in detail

    seasons = admin_client.get(reverse("season_list")).content.decode()
    assert reverse("season_delete", args=[season.pk]) not in seasons
    assert reverse("season_update", args=[season.pk]) in seasons  # …but "Edit"

    users = admin_client.get(reverse("accounts:user_list")).content.decode()
    assert reverse("accounts:user_deactivate", args=[player_user.pk]) not in users
    assert reverse("accounts:user_update", args=[player_user.pk]) in users

    # --- edit pages: that is where the delete action lives ------------------
    edit = admin_client.get(reverse("matchdays:matchday_update", args=[md.pk])).content.decode()
    assert reverse("matchdays:matchday_delete", args=[md.pk]) in edit

    season_edit = admin_client.get(reverse("season_update", args=[season.pk])).content.decode()
    assert reverse("season_delete", args=[season.pk]) in season_edit

    penalty_edit = admin_client.get(
        reverse("penalties:penalty_update", args=[penalty.pk])
    ).content.decode()
    assert reverse("penalties:penalty_delete", args=[penalty.pk]) in penalty_edit


def test_captain_nav_has_team_area_without_admin_area(captain_client, team):
    content = captain_client.get(reverse("dashboard:index")).content.decode()
    assert "nav-sep" in content
    assert reverse("teams:team_list") in content  # combined Teams & Players page
    assert reverse("accounts:user_list") not in content
    assert reverse("season_list") not in content


def test_player_nav_only_shows_user_pages(player_client):
    content = player_client.get(reverse("dashboard:index")).content.decode()
    assert f'href="{reverse("dashboard:index")}"' in content  # financial = start page
    assert reverse("players:player_list") not in content
    assert reverse("accounts:user_list") not in content
    assert "nav-sep" not in content


def test_navbar_follows_color_mode(db):
    """The navbar must adapt to light/dark mode — no hardcoded black bar."""
    content = Client().get(reverse("account_login")).content.decode()
    assert 'class="navbar navbar-expand-lg bg-body-secondary sticky-top"' in content
    assert "navbar-dark" not in content  # was the reason for the black menu in light mode
    assert "bg-dark" not in content
    # Django strips only SINGLE-line {# ... #} comments — a multi-line one would
    # leak into the page as visible text.
    assert "{#" not in content


def test_logout_is_a_plain_nav_link_like_settings(admin_client):
    """Abmelden must have the SAME height as Einstellungen in the navbar.

    The logout posts via a form, but its button must not carry ``.btn``:
    ``style.css`` gives every ``.btn`` the 2.75rem touch-target min-height
    while plain ``.nav-link`` anchors are 2.5rem tall — the two header items
    would sit at different heights. The form must not be ``d-inline``
    either (an inline wrapper around a block button adds stray line-box
    space in the navbar).
    """
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    logout_action = f'action="{reverse("account_logout")}"'
    assert logout_action in content
    assert f'{logout_action} class="m-0"' in content  # block form, no inline strut
    assert '<button type="submit" class="nav-link text-start">' in content
    assert 'class="btn btn-link nav-link"' not in content  # .btn min-height = height mismatch
