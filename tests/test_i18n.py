"""i18n tests — language switch, UserLanguageMiddleware, German catalog, cookie banner."""

from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

MO_DE = Path(__file__).resolve().parent.parent / "locale" / "de" / "LC_MESSAGES" / "django.mo"

needs_catalog = pytest.mark.skipif(
    not MO_DE.exists(),
    reason="compiled German catalog missing (run: python manage.py compilemessages)",
)


def _switch_language(client, code):
    response = client.post(reverse("set_language"), {"language": code, "next": "/"})
    assert response.status_code == 302
    return response


# ---------------------------------------------------------------------------
# Language switch endpoint
# ---------------------------------------------------------------------------
def test_language_switch_sets_cookie_and_redirects(db):
    response = _switch_language(Client(), "en")
    assert settings.LANGUAGE_COOKIE_NAME in response.cookies
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"


def test_language_switch_rejects_unknown_language(db):
    response = Client().post(reverse("set_language"), {"language": "xy", "next": "/"})
    assert response.status_code == 302  # Django falls back, cookie not set to xy
    assert response.cookies.get(settings.LANGUAGE_COOKIE_NAME) is None


# ---------------------------------------------------------------------------
# English (source strings) / German (catalog)
# ---------------------------------------------------------------------------
def test_english_ui_renders_source_strings(admin_client):
    _switch_language(admin_client, "en")
    # Switching redirects to the translated URL (/en/...) — follow like a browser.
    content = admin_client.get(reverse("dashboard:index"), follow=True).content.decode()
    assert "Financial overview" in content
    assert "Finanzübersicht" not in content


def test_default_language_is_german_with_catalog(admin_client):
    """Without an explicit choice the UI follows DJANGO_LANGUAGE_CODE=de."""
    if not MO_DE.exists():
        pytest.skip("compiled German catalog missing (run: python manage.py compilemessages)")
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    assert "Finanzübersicht" in content
    assert "Financial overview" not in content


@needs_catalog
def test_german_login_page(admin_client):  # client type irrelevant; uses fresh client below
    client = Client()
    _switch_language(client, "de")
    content = client.get(reverse("account_login")).content.decode()
    assert "Anmelden" in content


@needs_catalog
def test_german_signup_page(db):
    """The registration page must be fully German (template strings + button)."""
    client = Client()
    _switch_language(client, "de")
    content = client.get(reverse("account_signup")).content.decode()
    assert "Konto erstellen" in content
    assert "Create an account" not in content
    assert "Registrieren" in content  # submit button
    assert "Already have an account?" not in content


@needs_catalog
def test_german_matchday_team_selector(admin_client, team):
    """The matchday team dropdown must offer the German 'All teams' option."""
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("matchdays:matchday_list")).content.decode()
    assert "Alle Mannschaften" in content
    assert "All teams" not in content


@needs_catalog
def test_german_financial_overview(admin_client):
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    assert "Finanzübersicht" in content


@needs_catalog
def test_german_new_ui_strings(admin_client, matchday):
    """Combined Teams & Players page + participants editor must be German."""
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("teams:team_list")).content.decode()
    assert "Mannschaften & Spieler" in content
    assert "Zuordnungen speichern" in content
    content = admin_client.get(
        reverse("matchdays:matchday_detail", args=[matchday.pk])
    ).content.decode()
    assert "Spieler hinzufügen" in content
    assert "Teilnehmer speichern" in content


@needs_catalog
def test_german_today_badge(admin_client, team):
    """The badge of TODAY's matchday must read 'Heute' (source: 'Today')."""
    from django.utils import timezone

    from app.matchdays.models import Matchday

    Matchday.objects.create(
        team=team, opponent="SV Punctual", venue="home", date=timezone.localdate()
    )
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("matchdays:matchday_list")).content.decode()
    assert 'badge text-bg-success">Heute</span>' in content


@needs_catalog
def test_german_cookie_banner_copy(admin_client):
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    assert "Diese Website setzt nur notwendige Cookies" in content


def test_cookie_banner_present_in_english(admin_client):
    _switch_language(admin_client, "en")
    content = admin_client.get(reverse("dashboard:index"), follow=True).content.decode()
    assert 'id="cookie-consent"' in content
    assert "This site sets only essential cookies" in content


@needs_catalog
def test_german_payment_button(admin_client, matchday_with_players, catalog_normal, admin_user):
    """The "Bezahlen" (record payment) action must be translated in German."""
    from app.penalties.services import assign_penalty

    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    assert "Bezahlen" in content


@needs_catalog
def test_german_footer_legal_links(db):
    # Anonymous client: django-allauth redirects already logged-in users away
    # from the login page — the footer must be right for visitors.
    client = Client()
    _switch_language(client, "de")
    content = client.get(reverse("account_login")).content.decode()
    assert "Impressum" in content
    assert "Datenschutzerklärung" in content


# ---------------------------------------------------------------------------
# UserLanguageMiddleware — preference & precedence
# ---------------------------------------------------------------------------
def test_preferred_language_applies_without_cookie(admin_user):
    admin_user.preferred_language = "en"
    admin_user.save(update_fields=["preferred_language"])
    client = Client()
    client.force_login(admin_user)
    # Unprefixed default URL -> redirected to the /en/ equivalent, then English.
    content = client.get(reverse("dashboard:index"), follow=True).content.decode()
    assert "Financial overview" in content


def test_explicit_cookie_overrides_user_preference(admin_user):
    admin_user.preferred_language = "de"
    admin_user.save(update_fields=["preferred_language"])
    client = Client()
    client.force_login(admin_user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    content = client.get(reverse("dashboard:index"), follow=True).content.decode()
    assert "Financial overview" in content  # cookie beats stored preference


def test_settings_language_change_writes_cookie(captain_client, captain_user):
    """The Settings page is the ONLY language lever for signed-in users.

    Saving must refresh the cookie, otherwise a previously set one would keep
    overriding the new preference (navbar switcher is gone).
    """
    response = captain_client.post(
        reverse("accounts:settings"), {"preferred_language": "en", "preferred_theme": "auto"}
    )
    assert response.status_code == 302
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"


def test_anonymous_default_language_renders(admin_user):
    content = Client().get(reverse("account_login")).content.decode()
    assert "Darts Penalty Manager" in content
