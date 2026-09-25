"""Season helpers — active season resolution (navbar dropdown, session).

Resolution order: personal session override → admin-defined default season →
newest season by name. The session value is a PER-USER view choice: it only
lives as long as the session and never changes the global default.
"""

from app.matchdays.models import Season

SESSION_KEY = "season_id"


def default_season() -> Season | None:
    """The season an admin marked as the default (newest one wins)."""
    return Season.default()


def season_for_request(request, *, seasons: list[Season] | None = None) -> Season | None:
    """Session-selected season, else the admin default, else the newest one.

    Returns ``None`` when no season exists yet — views then skip season
    filtering entirely (fresh test DBs / brand-new installs before bootstrap).

    ``seasons`` may pass an already-loaded list (ordered by ``-name``, e.g. the
    navbar dropdown data in the context processor) to resolve the active season
    without further queries.
    """
    season_id = request.session.get(SESSION_KEY)
    if seasons is None:
        if season_id is not None:
            season = Season.objects.filter(pk=season_id).first()
            if season is not None:
                return season
            request.session.pop(SESSION_KEY, None)  # deleted season -> fall back
        return default_season() or Season.objects.order_by("-name").first()

    # Preloaded path — identical precedence, zero additional queries.
    if season_id is not None:
        for season in seasons:
            if season.pk == season_id:
                return season
        request.session.pop(SESSION_KEY, None)  # deleted season -> fall back
    for season in seasons:
        if season.is_default:
            return season
    return seasons[0] if seasons else None


def set_active_season(request, season: Season) -> None:
    request.session[SESSION_KEY] = season.pk
