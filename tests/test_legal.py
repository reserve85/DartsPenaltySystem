"""Legal pages (Imprint, Privacy Policy) + footer links + version info."""

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from app.core.version import GITHUB_REPO_URL, get_version_info

pytestmark = pytest.mark.django_db


CONTACT = {
    "CONTACT_COMPANY": "Dart Club XY e.V.",
    "CONTACT_NAME": "Max Mustermann",
    "CONTACT_STREET": "Musterstrasse 1",
    "CONTACT_CITY": "11111 Musterstadt",
    "CONTACT_EMAIL": "max@example.com",
}


def test_imprint_accessible_without_login():
    response = Client().get(reverse("imprint"))
    assert response.status_code == 200


@override_settings(**CONTACT)
def test_imprint_shows_contact_block():
    content = Client().get(reverse("imprint")).content.decode()
    assert "Dart Club XY e.V." in content
    assert "Max Mustermann" in content
    assert "Musterstrasse 1" in content
    assert "11111 Musterstadt" in content
    assert "max@example.com" in content


def test_imprint_without_config_shows_hint():
    with override_settings(
        CONTACT_COMPANY="", CONTACT_NAME="", IMPRINT_NAME=None, IMPRINT_URL=None
    ):
        client = Client()
        client.post(reverse("set_language"), {"language": "en", "next": "/"})
        content = client.get(reverse("imprint"), follow=True).content.decode()
        assert "No imprint configured." in content


def test_privacy_accessible_without_login():
    response = Client().get(reverse("privacy"))
    assert response.status_code == 200


@override_settings(**CONTACT)
def test_privacy_shows_responsible_party_and_rights():
    client = Client()
    # English source strings so assertions do not depend on the catalog.
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    content = client.get(reverse("privacy"), follow=True).content.decode()
    assert "Privacy Policy" in content
    assert "Responsible Party" in content
    assert "Max Mustermann" in content
    assert "max@example.com" in content
    assert "GDPR" in content
    assert "Cookies" in content


def test_footer_links_imprint_privacy_and_github():
    content = Client().get(reverse("account_login")).content.decode()
    assert reverse("imprint") in content
    assert "Impressum" in content or "Imprint" in content
    assert reverse("privacy") in content
    assert GITHUB_REPO_URL in content
    # default build metadata for local development
    assert "v0.1.0" in content
    assert "(dev)" in content


def test_footer_version_reflects_build_env(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v1.0.38")
    monkeypatch.setenv("GIT_COMMIT", "a718e32c9d1f4b2e8a11")
    monkeypatch.setenv("BUILD_DATE", "2026-09-11T08:11:34+02:00")

    content = Client().get(reverse("account_login")).content.decode()
    assert "v1.0.38" in content
    assert "(a718e32)" in content
    assert "2026-09-11" in content
    assert GITHUB_REPO_URL in content


def test_version_info_strips_tag_and_truncates_commit(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v1.2.3")
    monkeypatch.setenv("GIT_COMMIT", "abcdef1234567890")
    monkeypatch.setenv("BUILD_DATE", "development")

    info = get_version_info("Europe/Berlin")
    assert info["version"] == "1.2.3"
    assert info["git_commit"] == "abcdef1"
    assert info["build_date"] == "development"
    assert info["github_url"] == GITHUB_REPO_URL
    assert info["release_url"].endswith("/releases/tag/v1.2.3")


def test_version_info_falls_back_on_invalid_build_date(monkeypatch):
    monkeypatch.setenv("BUILD_DATE", "not-a-date")
    assert get_version_info()["build_date"] == "not-a-date"
