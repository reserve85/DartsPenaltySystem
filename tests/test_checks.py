"""Custom system checks (darts.W001 — insecure SECRET_KEY in production)."""

import pytest
from django.test import override_settings

from app.core.checks import secret_key_check

STRONG_KEY = "x" * 60


def test_warns_on_django_default_key():
    with override_settings(
        DEBUG=False, SECRET_KEY="django-insecure-dev-only-key-do-not-use-in-production"
    ):
        problems = secret_key_check(None)
    assert len(problems) == 1
    assert problems[0].id == "darts.W001"


def test_warns_on_env_example_key():
    with override_settings(DEBUG=False, SECRET_KEY="change-me"):
        problems = secret_key_check(None)
    assert [problem.id for problem in problems] == ["darts.W001"]


def test_warns_on_short_key():
    with override_settings(DEBUG=False, SECRET_KEY="short"):
        problems = secret_key_check(None)
    assert [problem.id for problem in problems] == ["darts.W001"]


def test_passes_with_strong_key():
    with override_settings(DEBUG=False, SECRET_KEY=STRONG_KEY):
        assert secret_key_check(None) == []


def test_ignored_during_local_debug():
    with override_settings(
        DEBUG=True, SECRET_KEY="django-insecure-dev-only-key-do-not-use-in-production"
    ):
        assert secret_key_check(None) == []


@pytest.mark.parametrize(
    "key", ["change-me", "django-insecure-dev-only-key-do-not-use-in-production"]
)
def test_insecure_keys_are_recognized(key):
    from app.core.checks import INSECURE_SECRET_KEYS

    assert key in INSECURE_SECRET_KEYS
