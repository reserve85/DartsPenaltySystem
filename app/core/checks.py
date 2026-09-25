"""Custom system checks — registered from ``CoreConfig.ready()``.

Run with ``python manage.py check`` (the container entrypoint runs it before
gunicorn starts) so misconfiguration is visible at boot, not in production.
"""

from django.conf import settings
from django.core.checks import Tags, Warning, register

# Keys that must never secure a deployed instance. ``change-me`` is what
# ``.env.example`` ships with — a leftover copy is as bad as the Django
# development default.
INSECURE_SECRET_KEYS = frozenset(
    {
        "django-insecure-dev-only-key-do-not-use-in-production",
        "change-me",
    }
)

MIN_SECRET_KEY_LENGTH = 50


@register(Tags.security)
def secret_key_check(app_configs, **kwargs):
    """A production instance (DEBUG=False) needs a real SECRET_KEY.

    Django's own ``--deploy`` checks only warn below 50 characters, so the
    well-known 53-char development default slips through — this check closes
    that gap (WARNING level: development with the defaults must keep working).
    """
    if settings.DEBUG:
        return []
    key = settings.SECRET_KEY or ""
    if key in INSECURE_SECRET_KEYS or len(key) < MIN_SECRET_KEY_LENGTH:
        return [
            Warning(
                "DJANGO_SECRET_KEY is missing, known-insecure or too short.",
                hint=(
                    "Set DJANGO_SECRET_KEY in the environment before deploying: "
                    'python -c "import secrets; print(secrets.token_urlsafe(50))"'
                ),
                id="darts.W001",
            )
        ]
    return []
