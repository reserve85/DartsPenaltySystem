"""Season-aware team queries — which teams are active in a season, who captains them."""

from app.core.permissions import GROUP_ADMIN, GROUP_CAPTAIN
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


def captains_for_teams(teams) -> dict:
    """``{team_pk: [accounts]}`` — the captains assigned to the given teams.

    The team assignment is DECOUPLED from the role (decision): ``User.team`` is
    an optional captaincy — a Captain may not have a team yet, while an Admin
    may additionally be assigned as captain of one team. Both roles therefore
    count as captain of their assigned team; Player-role accounts never do
    (the user forms reject a team for that role).

    Inactive accounts are left out; accounts are ordered by email, teams
    without a captain get an empty list so template lookups never fail.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    teams = list(teams)
    if not teams:
        return {}
    User = get_user_model()
    assigned = (
        User.objects.filter(is_active=True, team_id__in=[t.pk for t in teams])
        .filter(Q(groups__name__in=[GROUP_CAPTAIN, GROUP_ADMIN]) | Q(is_superuser=True))
        .distinct()
        .select_related("team", "player_link")
        .order_by("email")
    )
    captains: dict = {team.pk: [] for team in teams}
    for account in assigned:
        captains[account.team_id].append(account)
    return captains
