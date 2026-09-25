"""Security hardening tests (Phase 9) — env helpers, headers, SSL redirect, spam protection."""

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from app.accounts.models import User
from config.settings import env_bool, env_int

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# env() helpers used by the security settings
# ---------------------------------------------------------------------------
def test_env_bool_parsing(monkeypatch):
    for raw, expected in (
        ("True", True),
        ("1", True),
        ("yes", True),
        ("False", False),
        ("0", False),
        ("no", False),
    ):
        monkeypatch.setenv("T_FLAG", raw)
        assert env_bool("T_FLAG") is expected
    monkeypatch.delenv("T_FLAG", raising=False)
    assert env_bool("T_FLAG") is False
    assert env_bool("T_FLAG", "True") is True


def test_env_int_parsing(monkeypatch):
    monkeypatch.setenv("T_NUM", "31536000")
    assert env_int("T_NUM") == 31536000
    monkeypatch.setenv("T_NUM", "not-a-number")
    assert env_int("T_NUM", 42) == 42
    monkeypatch.delenv("T_NUM", raising=False)
    assert env_int("T_NUM", 7) == 7


# ---------------------------------------------------------------------------
# Always-on hardening
# ---------------------------------------------------------------------------
def test_security_settings_defaults():
    from django.conf import settings

    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.X_FRAME_OPTIONS == "DENY"
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert settings.SECURE_REFERRER_POLICY == "same-origin"
    assert settings.SECURE_SSL_REDIRECT is False  # opt-in via env (dev default)
    assert settings.SECURE_HSTS_SECONDS == 0


def test_clickjacking_header_on_responses():
    response = Client().get(reverse("account_login"))
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@override_settings(SECURE_SSL_REDIRECT=True)
def test_ssl_redirect_when_enabled():
    response = Client().get(reverse("account_login"))
    assert response.status_code == 301
    assert response["Location"].startswith("https://")


# ---------------------------------------------------------------------------
# Spam protection for self-registration: honeypot + signup rate limit
# ---------------------------------------------------------------------------


def test_signup_page_renders_invisible_honeypot_field(db):
    """allauth's honeypot is an off-screen input WITHOUT a label (no hint for bots)."""
    from allauth.account import app_settings

    assert app_settings.SIGNUP_FORM_HONEYPOT_FIELD == "website"
    content = Client().get(reverse("account_signup")).content.decode()
    assert 'name="website"' in content
    assert "right: -99999px" in content  # off-screen for humans
    assert '<label for="id_website"' not in content


def test_signup_with_filled_honeypot_creates_no_account(db):
    """Bots that fill the invisible field get a fake success — and nothing else.

    The response must look EXACTLY like a real signup (redirect to the
    verification-sent page) so the bot cannot adapt, but no account, no
    e-mail and no audit row may be produced.
    """
    from django.core import mail

    from app.core.models import AuditAction, AuditLog

    client = Client()
    response = client.post(
        reverse("account_signup"),
        {
            "email": "bot@example.com",
            "password1": "Strong-Pass-123!",
            "password2": "Strong-Pass-123!",
            "website": "http://spam.example",  # honeypot filled in
        },
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("account_email_verification_sent")
    # No account, no e-mails (verification OR admin notification), no audit entry.
    assert not User.objects.filter(email="bot@example.com").exists()
    assert len(mail.outbox) == 0
    assert not AuditLog.objects.filter(action=AuditAction.USER_REGISTERED).exists()


def test_signup_rate_limit_returns_429_and_renders_friendly_page(db):
    """Tightening ACCOUNT_RATE_LIMITS limits signups per IP (allauth built-in)."""
    from allauth.account import app_settings
    from django.core.cache import cache

    assert "signup" in app_settings.RATE_LIMITS  # configured in settings.py

    url = reverse("account_signup")
    payload = {
        "email": "flood@example.com",
        "password1": "Strong-Pass-123!",
        "password2": "Strong-Pass-123!",
    }
    cache.clear()  # isolate from other tests (shared LocMem cache)
    try:
        with override_settings(ACCOUNT_RATE_LIMITS={"signup": "1/m/ip"}):
            first = Client().post(url, payload)
            assert first.status_code in (200, 302)  # the one allowed signup
            second = Client().post(url, {**payload, "email": "flood2@example.com"})
            assert second.status_code == 429
            # Our own friendly 429 page renders (not allauth's bare fallback).
            assert any(t.name == "429.html" for t in second.templates)
    finally:
        cache.clear()  # never leak rate-limit state into other tests


# ---------------------------------------------------------------------------
# Mutation audit coverage (Phase 9 review): spot-check that every user-
# initiated mutation from the view layer writes an AuditLog row.
# ---------------------------------------------------------------------------
def test_all_audit_actions_have_translated_labels():
    """Audit UI must never show raw enum keys (labels are gettext_lazy)."""
    from app.core.models import AuditAction

    for _value, label in AuditAction.choices:
        assert str(label)  # lazy proxy renders without error
        assert not str(label).startswith("AuditAction.")  # not the raw enum


def test_no_mass_assignment_through_forms():
    """Forms expose explicit field lists only (no ``fields = \"__all__\"``)."""
    from app.accounts.forms import SettingsForm, UserCreateForm, UserUpdateForm
    from app.matchdays.forms import MatchdayForm
    from app.penalties.forms import CatalogItemForm

    for form in (SettingsForm, UserCreateForm, UserUpdateForm, MatchdayForm, CatalogItemForm):
        declared = form._meta.fields if hasattr(form, "_meta") else None
        assert declared is not None
        assert "__all__" not in declared
