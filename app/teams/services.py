"""Season-aware team queries — which teams are active in a season, who captains them.

Also the home of the Kasse (cashier) rules: the cashier data
(``TeamCashier``) and its permission rule (``user_is_cashier``) live in the
same module so no core <-> teams import cycle is created (review L1) —
``app.core.permissions`` stays untouched.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from app.core.models import AuditAction
from app.core.permissions import GROUP_ADMIN, GROUP_CAPTAIN
from app.core.services import log_action
from app.teams.models import Team, TeamCashier


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


# ---------------------------------------------------------------------------
# Kasse — cashier assignment (one open row per team, history preserved)
# ---------------------------------------------------------------------------
def cashier_label_for(user) -> str:
    """Display label of ``user`` — the linked player's name, else the e-mail.

    Used both for the ``TeamCashier.user_label`` snapshot and in the UI.
    """
    if user is None:
        return ""
    player = getattr(user, "player_link", None)
    name = getattr(player, "name", "") if player is not None else ""
    return name or user.email


def cashier_user_for(team):
    """The ACCOUNT that may record payments for ``team``, or ``None``.

    Requires an OPEN row (``valid_to IS NULL``) with a linked ``user`` whose
    account is still active; an open row whose account was deleted or
    deactivated counts as "no cashier". The Kasse page reads open rows
    directly through its own queryset — intentionally there is no second
    lookup helper (review L6).
    """
    if team is None:
        return None
    row = (
        TeamCashier.objects.filter(team=team, valid_to__isnull=True)
        .select_related("user__player_link")
        .first()
    )
    if row is None or row.user_id is None or not row.user.is_active:
        return None
    return row.user


def user_is_cashier(user, team) -> bool:
    """True ONLY for that active account currently assigned to ``team``.

    The STRICT payment right (decision 7): admins included — an admin who
    wants to record payments must first set themselves as cashier on the
    Kasse page. Separate from ``user_can_manage_team`` (matchdays/penalties).
    """
    if not user or not user.is_authenticated or not user.is_active or team is None:
        return False
    return TeamCashier.objects.filter(team=team, valid_to__isnull=True, user=user).exists()


def set_cashier(*, team, user, actor) -> TeamCashier:
    """Close the team's open row and open a new one for ``user`` (history kept).

    Only approved, active accounts may be assigned (decision 1). The partial
    unique constraint race (two admins saving at once) is converted into a
    ValidationError instead of a 500 (review M5).
    """
    from app.accounts.models import ApprovalStatus

    if user is None:
        raise ValidationError(_("Please select a cashier."))
    if not user.is_active or user.approval_status != ApprovalStatus.APPROVED:
        raise ValidationError(_("Only active, approved accounts may be assigned as cashier."))
    now = timezone.now()
    try:
        with transaction.atomic():
            TeamCashier.objects.filter(team=team, valid_to__isnull=True).update(valid_to=now)
            row = TeamCashier.objects.create(
                team=team, user=user, user_label=cashier_label_for(user), created_by=actor
            )
            log_action(
                AuditAction.CASHIER_SET,
                user=actor,
                target=row,
                metadata={
                    "team_id": team.pk,
                    "user_id": user.pk,
                    "user_email": user.email,
                },
            )
    except IntegrityError as exc:
        raise ValidationError(
            _("The cashier was just changed by someone else — please reload the page.")
        ) from exc
    return row


def clear_cashier(*, team, actor) -> None:
    """Close the open row without opening a new one (bound to the empty option)."""
    row = TeamCashier.objects.filter(team=team, valid_to__isnull=True).first()
    if row is None:
        return
    now = timezone.now()
    with transaction.atomic():
        row.valid_to = now
        row.save(update_fields=["valid_to"])
        log_action(
            AuditAction.CASHIER_CLEARED,
            user=actor,
            target=row,
            metadata={
                "team_id": team.pk,
                "user_id": row.user_id,
                "user_label": row.user_label,
            },
        )
