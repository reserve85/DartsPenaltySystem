"""Theme tests — endpoint persistence + server-rendered data attributes (dark mode)."""

import pytest
from django.test import Client
from django.urls import reverse

from app.core.choices import THEME_COOKIE_NAME

pytestmark = pytest.mark.django_db


def test_theme_endpoint_updates_preference(admin_client, admin_user):
    response = admin_client.post(reverse("accounts:settings_theme"), {"theme": "dark"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "theme": "dark"}
    admin_user.refresh_from_db()
    assert admin_user.preferred_theme == "dark"


def test_theme_endpoint_accepts_all_choices(admin_client, admin_user):
    for value in ("auto", "light", "dark"):
        response = admin_client.post(reverse("accounts:settings_theme"), {"theme": value})
        assert response.status_code == 200
        admin_user.refresh_from_db()
        assert admin_user.preferred_theme == value


def test_theme_endpoint_rejects_invalid(admin_client, admin_user):
    response = admin_client.post(reverse("accounts:settings_theme"), {"theme": "neon"})
    assert response.status_code == 400
    assert response.json()["ok"] is False
    admin_user.refresh_from_db()
    assert admin_user.preferred_theme != "neon"


def test_theme_endpoint_requires_login(db):
    response = Client().post(reverse("accounts:settings_theme"), {"theme": "dark"})
    assert response.status_code == 302
    assert "login" in response.url


def test_theme_endpoint_get_not_allowed(admin_client):
    assert admin_client.get(reverse("accounts:settings_theme")).status_code == 405


def test_rendered_theme_follows_preference(admin_user):
    admin_user.preferred_theme = "dark"
    admin_user.save(update_fields=["preferred_theme"])
    client = Client()
    client.force_login(admin_user)
    html = client.get(reverse("dashboard:index")).content.decode()
    assert 'data-bs-theme="dark"' in html
    assert 'data-theme-choice="dark"' in html
    assert f'data-theme-url="{reverse("accounts:settings_theme")}"' in html


def test_theme_switcher_buttons_rendered(admin_client):
    html = admin_client.get(reverse("dashboard:index")).content.decode()
    assert 'data-theme-choice-btn="auto"' in html
    assert 'data-theme-choice-btn="light"' in html
    assert 'data-theme-choice-btn="dark"' in html


def test_anonymous_has_no_theme_url(db):
    html = Client().get(reverse("account_login")).content.decode()
    assert "data-theme-url" not in html  # no POST endpoint for anonymous users
    assert 'data-bs-theme="auto"' in html  # context-processor default
    # switcher itself still renders (session-only visual toggle)
    assert 'data-theme-choice-btn="dark"' in html


def test_language_switcher_renders_for_everyone(db):
    html = Client().get(reverse("account_login")).content.decode()
    assert reverse("set_language") in html
    assert 'name="language" value="de"' in html
    assert 'name="language" value="en"' in html


# ---------------------------------------------------------------------------
# Flash-free rendering (dark mode must be dark from the first painted frame)
# ---------------------------------------------------------------------------


def test_head_resolves_theme_before_stylesheet(db):
    """The choice must be resolved inline in <head>, before the stylesheet.

    Bootstrap only knows ``light``/``dark``, so a server-rendered ``auto``
    would paint the LIGHT theme first and flash bright on every page change
    until theme.js (end of <body>) runs.
    """
    html = Client().get(reverse("account_login")).content.decode()
    head = html.split("</head>")[0]
    # Earliest bytes carry the dark-canvas hints (meta for Chrome/FF, inline
    # style for browsers that ignore the meta — e.g. Safari).
    assert head.index('name="color-scheme"') < head.index('name="viewport"')
    assert ":root { color-scheme: dark light; }" in head
    resolver = 'root.getAttribute("data-theme-choice")'
    assert resolver in head  # inline pre-paint resolver is present
    assert head.index(resolver) < head.index("bootstrap.min.css")
    # …and it paints the canvas explicitly while the CSS is still loading.
    assert "root.style.backgroundColor" in head


def test_anonymous_theme_cookie_is_rendered(db):
    """The dpm_theme cookie keeps an anonymous toggle across page changes."""
    client = Client()
    client.cookies[THEME_COOKIE_NAME] = "dark"
    html = client.get(reverse("account_login")).content.decode()
    assert 'data-bs-theme="dark"' in html
    assert 'data-theme-choice="dark"' in html


def test_invalid_theme_cookie_falls_back_to_auto(db):
    client = Client()
    client.cookies[THEME_COOKIE_NAME] = "neon"
    html = client.get(reverse("account_login")).content.decode()
    assert 'data-bs-theme="auto"' in html
    assert 'data-theme-choice="auto"' in html


def test_account_preference_wins_over_theme_cookie(admin_user):
    admin_user.preferred_theme = "light"
    admin_user.save(update_fields=["preferred_theme"])
    client = Client()
    client.force_login(admin_user)
    client.cookies[THEME_COOKIE_NAME] = "dark"
    html = client.get(reverse("dashboard:index")).content.decode()
    assert 'data-bs-theme="light"' in html
    assert 'data-theme-choice="light"' in html
