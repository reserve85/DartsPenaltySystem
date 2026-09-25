"""Player ↔ team assignment — season-dependent reads and the ONLY write path.

A player's team assignment belongs to ONE season: changing it for the active
season never touches the assignments of any other season (transfers between
seasons stay visible in history).
"""

from django.db import transaction
from django.db.models import Count, Prefetch, Q

from app.players.models import PlayerTeam


def season_assignments(season=None):
    """All assignment rows — scoped to ``season`` when given."""
    queryset = PlayerTeam.objects.select_related("player", "team", "season")
    if season is not None:
        queryset = queryset.filter(season=season)
    return queryset


def teams_for(player, season=None):
    """Teams of ``player`` in ``season`` (season-dependent assignment)."""
    return [assignment.team for assignment in season_assignments(season).filter(player=player)]


def assign_teams(player, teams, *, season=None) -> None:
    """Replace ONLY the assignments of ``player`` for ``season``.

    Assignments of other seasons stay untouched — each season can hold a
    different team set. ``season=None`` replaces the season-less rows of a
    fresh install (no season created yet).
    """
    unique = list(dict.fromkeys(team.pk for team in teams))
    with transaction.atomic():
        PlayerTeam.objects.filter(player=player, season=season).delete()
        PlayerTeam.objects.bulk_create(
            [PlayerTeam(player=player, team_id=team_pk, season=season) for team_pk in unique]
        )


def bind_unassigned_memberships(season) -> None:
    """Bind season-less assignments to the first created season.

    Fresh installs (and databases migrated before any season existed) hold
    assignments without a season — they become that first season's roster so
    no membership is lost. Later seasons start EMPTY on purpose.
    """
    PlayerTeam.objects.filter(season__isnull=True).update(season=season)


def prefetch_season_assignments(season=None) -> Prefetch:
    """Expose ``player.season_assignments`` (team assignments of one season)."""
    queryset = PlayerTeam.objects.select_related("team")
    if season is not None:
        queryset = queryset.filter(season=season)
    return Prefetch("team_assignments", queryset=queryset, to_attr="season_assignments")


def player_count_annotation(season=None):
    """``Count`` expression for team player counts, scoped to ``season``.

    Usage: ``Team.objects.annotate(player_count=player_count_annotation(season))``.
    """
    if season is None:
        return Count("player_assignments__player", distinct=True)
    return Count(
        "player_assignments__player",
        filter=Q(player_assignments__season=season),
        distinct=True,
    )
