"""Season-aware team queries — which teams are active in a season."""

from app.teams.models import Team


def teams_for_season(season=None):
    """Teams active in ``season``, ordered by name.

    A season only shows the teams selected for it (2 Mannschaften this year,
    4 the next). Seasons without any selection — created before this feature
    or data migrations — activate EVERY team, so their scope never shrinks.
    """
    if season is None:
        return Team.objects.all()
    active = list(season.teams.values_list("pk", flat=True))
    if not active:
        return Team.objects.all()
    return Team.objects.filter(pk__in=active)
