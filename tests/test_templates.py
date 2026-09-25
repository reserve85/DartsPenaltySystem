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
    "players:player_list",
    "players:player_create",
    "matchdays:matchday_list",
    "matchdays:matchday_create",
    "penalties:catalog_list",
    "penalties:catalog_create",
    "accounts:user_list",
    "accounts:user_create",
    "accounts:settings",
    "account_change_password",
    "audit_list",
]

CAPTAIN_URLS = [
    "dashboard:index",
    "dashboard:financial_overview",
    "players:player_list",
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
    assert admin_client.get(reverse("teams:team_detail", args=[team.pk])).status_code == 200
    assert (
        admin_client.get(reverse("matchdays:matchday_detail", args=[matchday.pk])).status_code
        == 200
    )
    assert (
        admin_client.get(reverse("penalties:catalog_update", args=[catalog_normal.pk])).status_code
        == 200
    )


def test_captain_detail_pages_200(captain_client, team, matchday):
    assert captain_client.get(reverse("teams:team_detail", args=[team.pk])).status_code == 200
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
    assert reverse("dashboard:financial_overview") in content
    assert reverse("players:player_list") in content  # team area
    assert reverse("accounts:user_list") in content  # admin area
    assert reverse("season_list") in content


def test_captain_nav_has_team_area_without_admin_area(captain_client, team):
    content = captain_client.get(reverse("dashboard:index")).content.decode()
    assert "nav-sep" in content
    assert reverse("teams:team_detail", args=[team.pk]) in content
    assert reverse("accounts:user_list") not in content
    assert reverse("season_list") not in content


def test_player_nav_only_shows_user_pages(player_client):
    content = player_client.get(reverse("dashboard:index")).content.decode()
    assert reverse("dashboard:financial_overview") in content
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


def test_dashboard_links_team_name_to_financial_overview(admin_client, team):
    """No 'Open' button — the team name itself opens the financial overview."""
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    assert f"{reverse('dashboard:financial_overview')}?team={team.pk}" in content
    assert "Öffnen" not in content and ">Open<" not in content
