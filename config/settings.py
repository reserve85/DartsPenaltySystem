"""
Django settings for the Dart Penalty Manager (DartsPenaltySystem).

Environment-driven configuration via a small, dependency-free helper (H2).
"""

import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key: str, default: str | None = None) -> str | None:
    """Read an environment variable with an optional default."""
    return os.environ.get(key, default)


def env_list(key: str, default: str = "") -> list[str]:
    """Read a comma-separated environment variable into a list."""
    raw = env(key, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
DEBUG = (env("DJANGO_DEBUG", "False") or "False").lower() in ("1", "true", "yes")

SECRET_KEY = env("DJANGO_SECRET_KEY") or ("django-insecure-dev-only-key-do-not-use-in-production")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

CSRF_TRUSTED_ORIGINS = [
    origin.rstrip("/")
    for origin in env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
    if origin.startswith(("http://", "https://"))
]


def env_bool(key: str, default: str = "False") -> bool:
    """Read a boolean environment variable."""
    return (env(key, default) or "False").lower() in ("1", "true", "yes")


def env_int(key: str, default: int = 0) -> int:
    """Read an integer environment variable (falls back to ``default``)."""
    try:
        return int(env(key, str(default)) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# E-mail (SMTP completely env-driven — never commit credentials)
# ---------------------------------------------------------------------------
# Tests use the in-memory backend so ``django.core.mail.outbox`` can assert
# on outgoing messages without a real SMTP connection.
TESTING = (
    "test" in sys.argv or "pytest" in sys.modules or Path(sys.argv[0]).name.startswith("pytest")
)

EMAIL_BACKEND = env("EMAIL_BACKEND") or (
    "django.core.mail.backends.locmem.EmailBackend"
    if TESTING
    else "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", "True")
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", "False")
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT", 10)

# Sender of all system e-mails; SUPPORT_EMAIL receives support/admin requests.
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL") or "webmaster@localhost"
SUPPORT_EMAIL = env("SUPPORT_EMAIL") or DEFAULT_FROM_EMAIL
SERVER_EMAIL = env("SERVER_EMAIL") or SUPPORT_EMAIL

# Public base URL for absolute links inside e-mails (no request available in
# background code paths); empty -> relative links (logged by NotificationService).
PUBLIC_SITE_URL = (env("PUBLIC_SITE_URL") or "").rstrip("/")

if "smtp" in EMAIL_BACKEND.lower() and not EMAIL_HOST and not TESTING:
    logging.getLogger(__name__).warning(
        "EMAIL_BACKEND is SMTP but EMAIL_HOST is not configured — "
        "outgoing e-mails will fail. Set EMAIL_HOST (see .env.example)."
    )

# ---------------------------------------------------------------------------
# Notifications (central NotificationService)
# ---------------------------------------------------------------------------
# Deliver e-mails in a background thread so SMTP latency (connect + send per
# recipient, EMAIL_TIMEOUT each) never blocks the web worker — the container
# runs exactly ONE gunicorn worker (entrypoint.sh), so a group penalty with
# N recipients would otherwise freeze the whole site. Tests force synchronous
# sending so ``django.core.mail.outbox`` assertions stay reliable.
ASYNC_NOTIFICATIONS = (not TESTING) and env_bool("NOTIFICATIONS_ASYNC", "True")

# ---------------------------------------------------------------------------
# Logging — console output for the project's own loggers (container-friendly).
# ``app.*`` (notifications, accounts adapter, …) logs INFO+ to stdout;
# Django's built-in loggers keep their defaults (disable_existing_loggers off).
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "app": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "app_console": {"class": "logging.StreamHandler", "formatter": "app"},
    },
    "loggers": {
        "app": {
            "handlers": ["app_console"],
            "level": env("DJANGO_LOG_LEVEL", "INFO"),
            # Keep propagating: test capture (caplog) and any root handler
            # still see the records; production has no root handlers, so the
            # messages appear exactly once.
            "propagate": True,
        },
    },
}


# ---------------------------------------------------------------------------
# Security hardening (Phase 9) — enable when served behind HTTPS
# ---------------------------------------------------------------------------
# Redirects HTTP -> HTTPS and switches session/CSRF cookies to secure.
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT")
# Cookies can also be forced secure independently of the redirect (e.g. TLS
# terminated at the proxy while the app itself keeps serving HTTP internally).
SESSION_COOKIE_SECURE = env_bool(
    "DJANGO_SESSION_COOKIE_SECURE", "True" if SECURE_SSL_REDIRECT else "False"
)
CSRF_COOKIE_SECURE = env_bool(
    "DJANGO_CSRF_COOKIE_SECURE", "True" if SECURE_SSL_REDIRECT else "False"
)

# HSTS: 0 disables; set e.g. 31536000 once HTTPS is stable for the domain.
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0

# Always-on hardening (Django defaults stated explicitly for review).
SESSION_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "allauth",
    "allauth.account",
]

LOCAL_APPS = [
    "app.core",
    "app.accounts",
    "app.teams",
    "app.players",
    "app.matchdays",
    "app.penalties",
    "app.dashboard",
    "app.notifications",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    # django-allauth: e-mail based login (ACCOUNT_LOGIN_METHODS)
    "allauth.account.auth_backends.AuthenticationBackend",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise right after SecurityMiddleware so static files bypass app middleware
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # LocaleMiddleware after Session, before CommonMiddleware (per Django docs)
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # UserLanguageMiddleware: after AuthenticationMiddleware (request.user) and
    # after LocaleMiddleware (detect explicit session/cookie override)
    "app.core.middleware.UserLanguageMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # ApprovalGateMiddleware: after Messages (flash on rejected sessions) —
    # logs out authenticated users whose approval_status is not "approved".
    "app.accounts.middleware.ApprovalGateMiddleware",
    # django-allauth (requires sessions + auth; see allauth docs)
    "allauth.account.middleware.AccountMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "app" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "app.core.context_processors.site_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database (SQLite; relative SQLITE_PATH values resolve against BASE_DIR)
# ---------------------------------------------------------------------------
SQLITE_PATH = Path(env("SQLITE_PATH", "data/db.sqlite3"))
if not SQLITE_PATH.is_absolute():
    SQLITE_PATH = BASE_DIR / SQLITE_PATH
SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": SQLITE_PATH,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGES = [
    ("de", "Deutsch"),
    ("en", "English"),
]
LANGUAGE_CODE = env("DJANGO_LANGUAGE_CODE", "de")
TIME_ZONE = env("DJANGO_TIME_ZONE", "Europe/Berlin")
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]

# ---------------------------------------------------------------------------
# Static files (WhiteNoise via MIDDLEWARE + STORAGES)
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "app" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# ---------------------------------------------------------------------------
# Authentication (django-allauth for signup/login/logout/password flows)
# ---------------------------------------------------------------------------
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "dashboard:index"
LOGOUT_REDIRECT_URL = "account_login"

# Our custom User has no username column — login is e-mail only.
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = env("ACCOUNT_EMAIL_VERIFICATION", "mandatory")
ACCOUNT_PASSWORD_INPUT_RENDER_VALUE = False
# POST-only logout (the navbar already submits a form).
ACCOUNT_LOGOUT_ON_GET = False
# Gate: never log in a user whose approval_status is pending/rejected.
ACCOUNT_FORMS = {"login": "app.accounts.forms.ApprovalLoginForm"}
# Custom adapter: missing/broken SMTP must not break auth flows (logged instead).
ACCOUNT_ADAPTER = "app.accounts.adapters.AccountAdapter"

# Spam protection for self-registration (allauth built-ins, no external service):
# - Honeypot: invisible form field; bots that fill it get a fake "verification
#   sent" response and NO account is created (set empty to disable).
ACCOUNT_SIGNUP_FORM_HONEYPOT_FIELD = "website"
# - Per-IP signup rate limit (stored in the Django cache, "amount/duration[/scope]").
#   Tests get a huge limit so the suite never trips its own guard.
ACCOUNT_RATE_LIMITS = {
    "signup": "10000/m/ip" if TESTING else (env("SIGNUP_RATE_LIMIT") or "10/m/ip,50/h/ip")
}
# - Behind a reverse proxy set the number of trusted proxies (env) so the rate
#   limiter keys on the real client IP instead of the shared proxy address.
ALLAUTH_TRUSTED_PROXY_COUNT = env_int("ALLAUTH_TRUSTED_PROXY_COUNT", 0)

# ---------------------------------------------------------------------------
# Legal — Imprint / Privacy Policy (env-driven, same pattern as EloRankingSystem)
# ---------------------------------------------------------------------------
# Legacy imprint (optional): IMPRINT_NAME doubles as company fallback.
IMPRINT_NAME = env("IMPRINT_NAME")
IMPRINT_URL = env("IMPRINT_URL")

# Contact block rendered on the Imprint and Privacy Policy pages.
CONTACT_COMPANY = env("CONTACT_COMPANY", "")
CONTACT_NAME = env("CONTACT_NAME", "")
CONTACT_STREET = env("CONTACT_STREET", "")
CONTACT_CITY = env("CONTACT_CITY", "")
CONTACT_EMAIL = env("CONTACT_EMAIL", "")
