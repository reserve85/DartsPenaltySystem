"""Template context — site-wide data for base.html."""

from django.conf import settings
from django.utils import timezone

from app.core.version import get_version_info
from app.matchdays.models import Season
from app.matchdays.services import season_for_request

# URL namespaces/names that belong to one navigation group (base.html).
_OVERVIEW = "overview"
_FINANCIAL = "financial"
_TEAM_AREA = "team"
_CATALOG = "catalog"
_ADMIN_AREA = "admin"


def _nav_section(request) -> str:
    """Navigation group of the current URL — drives the active highlight.

    Groups, in order: every user (``overview``/``financial``) | team area
    (``team``/``catalog`` for captains and admins) | admin area (``admin``).
    """
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    namespace = match.namespace or ""
    name = match.url_name or ""

    if namespace == "dashboard":
        return _FINANCIAL if name == "financial_overview" else _OVERVIEW
    if namespace in ("teams", "players", "matchdays"):
        return _TEAM_AREA
    if namespace == "penalties":
        if name.startswith("catalog"):
            return _CATALOG
        if name.startswith("payment"):
            return _FINANCIAL
        # penalty assign/edit belongs to the matchday it was opened from
        return _TEAM_AREA
    if namespace == "accounts" and name.startswith("user"):
        return _ADMIN_AREA
    if name in {
        "audit_list",
        "season_list",
        "season_create",
        "season_update",
        "season_delete",
        "season_set_default",
    }:
        return _ADMIN_AREA
    return ""


def site_context(request):
    theme = "auto"
    seasons = []
    active_season = None
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        theme = getattr(request.user, "preferred_theme", "auto") or "auto"
        # ONE query for the dropdown; the active season resolves from the same
        # list (session id -> default flag -> newest) without extra queries.
        seasons = list(Season.objects.order_by("-name"))
        active_season = season_for_request(request, seasons=seasons)

    return {
        "current_year": timezone.now().year,
        "current_theme": theme,
        "site_languages": settings.LANGUAGES,
        "seasons": seasons,
        "active_season": active_season,
        "nav_section": _nav_section(request),
        "cookie_consent_enabled": True,
        "IMPRINT_NAME": settings.IMPRINT_NAME,
        "IMPRINT_URL": settings.IMPRINT_URL,
        "CONTACT_COMPANY": settings.CONTACT_COMPANY,
        "CONTACT_NAME": settings.CONTACT_NAME,
        "CONTACT_STREET": settings.CONTACT_STREET,
        "CONTACT_CITY": settings.CONTACT_CITY,
        "CONTACT_EMAIL": settings.CONTACT_EMAIL,
        "version_info": get_version_info(settings.TIME_ZONE),
    }
