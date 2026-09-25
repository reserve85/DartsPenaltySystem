"""Role constants, derived role helpers and view mixins.

Roles are Django Groups (``Admin``/``Captain``/``Player``) — no ``role``
column. Derived precedence: admin > captain > player > none. A Captain with
``team=None`` can manage nothing (server-side guard).

Superusers (the env-seeded system/hosting account, see ``bootstrap``) always
count as Admin regardless of group membership — that fixed "operator" account
can never lock itself out of the system.

Note: this module must NOT import ``django.contrib.auth.mixins/views/forms``
at module level — ``accounts.models`` imports it during app population and
``get_user_model()`` is not yet available at that point.
"""

from typing import ClassVar

from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.urls import reverse

GROUP_ADMIN = "Admin"
GROUP_CAPTAIN = "Captain"
GROUP_PLAYER = "Player"
ADMIN_GROUP = GROUP_ADMIN
CAPTAIN_GROUP = GROUP_CAPTAIN
PLAYER_GROUP = GROUP_PLAYER
ROLE_GROUPS = [GROUP_ADMIN, GROUP_CAPTAIN, GROUP_PLAYER]

# Instance-local cache attributes for the group lookups below (one query per
# role per user instance instead of one per call — templates call these
# repeatedly). The cache is invalidated by ``app.accounts.signals`` on every
# group change / user save, and by ``User.refresh_from_db()``.
_CACHE_ADMIN = "_role_cache_admin"
_CACHE_CAPTAIN = "_role_cache_captain"
_CACHE_PLAYER = "_role_cache_player"


def _invalidate_role_cache(user) -> None:
    """Drop the cached group flags of ``user`` (called on group/user changes)."""
    for attr in (_CACHE_ADMIN, _CACHE_CAPTAIN, _CACHE_PLAYER):
        try:
            delattr(user, attr)
        except AttributeError:
            pass


def _group_flag(user, group: str, cache_attr: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    cached = getattr(user, cache_attr, None)
    if cached is not None:
        return cached
    value = bool(user.groups.filter(name=group).exists())
    try:
        setattr(user, cache_attr, value)
    except AttributeError:  # pragma: no cover — exotic user backends
        return value
    return value


def user_is_admin(user) -> bool:
    """Admin-group member OR superuser (the un-lockable system account)."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return _group_flag(user, GROUP_ADMIN, _CACHE_ADMIN)


def user_is_captain(user) -> bool:
    return _group_flag(user, GROUP_CAPTAIN, _CACHE_CAPTAIN)


def user_is_player(user) -> bool:
    return _group_flag(user, GROUP_PLAYER, _CACHE_PLAYER)


def user_can_manage_team(user, team) -> bool:
    """True for Admins (any team) and for Captains of exactly this team."""
    if not user or not user.is_authenticated:
        return False
    if user_is_admin(user):
        return True
    return bool(
        user_is_captain(user)
        and user.team_id is not None
        and team is not None
        and user.team_id == team.pk
    )


def user_can_manage_matchday(user, matchday) -> bool:
    return user_can_manage_team(user, matchday.team)


def user_can_view_penalty(user, penalty) -> bool:
    """Admins see everything; Captains their own team; Players their own rows."""
    if not user or not user.is_authenticated:
        return False
    if user_is_admin(user):
        return True
    if user_is_captain(user) and user.team_id is not None:
        return penalty.matchday.team_id == user.team_id
    if user_is_player(user) and user.player_link_id is not None:
        return penalty.player_id == user.player_link_id
    return False


def user_can_view_player(user, player) -> bool:
    """Admins see every player; Captains players of one of their teams.

    The check is intentionally SEASON-AGNOSTIC: a captain may still open the
    page of a player who played for their team in an earlier season (history
    stays visible) — the roster *listings*, however, are season-scoped.
    """
    if not user or not user.is_authenticated:
        return False
    if user_is_admin(user):
        return True
    return bool(
        user_is_captain(user)
        and user.team_id is not None
        and player.teams.filter(pk=user.team_id).exists()
    )


def assert_can_manage_team(user, team) -> None:
    if not user_can_manage_team(user, team):
        raise PermissionDenied


def assert_can_view_player(user, player) -> None:
    if not user_can_view_player(user, player):
        raise PermissionDenied


class GroupRequiredMixin:
    """Require membership in at least one of ``groups`` (role = Django Groups).

    Anonymous users are redirected to login; authenticated users without the
    required group get HTTP 403.
    """

    groups: ClassVar[list[str]] = []

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseRedirect(reverse("account_login"))
        if request.user.is_superuser:
            return super().dispatch(request, *args, **kwargs)  # system account: always allowed
        if not any(request.user.groups.filter(name=group).exists() for group in self.groups):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


def assert_admin_or_captain(user) -> None:
    """Raise PermissionDenied unless the user is Admin or Captain.

    Single home for the role gate that views used to repeat inline (the
    superuser is covered via ``user_is_admin``).
    """
    if not (user_is_admin(user) or user_is_captain(user)):
        raise PermissionDenied
