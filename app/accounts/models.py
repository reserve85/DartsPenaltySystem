"""Custom User model — email login, no username, roles via Django Groups.

Users and Players are decoupled entities (D2); ``player_link`` is an optional
1:1 link letting a Player-role user see "own penalties". ``is_staff`` is
auto-derived from group membership (H1); see ``app/accounts/signals.py``.
"""

from typing import ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from app.accounts.managers import UserManager
from app.core.choices import ThemeChoice
from app.core.permissions import (
    _invalidate_role_cache,
    user_is_admin,
    user_is_captain,
    user_is_player,
)


class ApprovalStatus(models.TextChoices):
    """Admin approval of self-registered users (login only when approved)."""

    PENDING = "pending", _("Pending approval")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")


class User(AbstractUser):
    username = None
    email = models.EmailField(_("email address"), unique=True)

    # Approval workflow: self-registered users start as "pending" and can only
    # log in once an admin approved them (admin-created accounts are set to
    # "approved" directly, see UserCreateForm / bootstrap).
    approval_status = models.CharField(
        _("approval status"),
        max_length=16,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
        db_index=True,
        help_text=_("Pending users cannot log in until an admin approves them."),
    )

    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
        verbose_name=_("team"),
        help_text=_("Required for Captain role; ignored for Admin."),
    )
    player_link = models.OneToOneField(
        "players.Player",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="user_account",
        verbose_name=_("linked player"),
        help_text=_("Optional: lets a Player-role user see their own penalties."),
    )
    preferred_language = models.CharField(
        _("preferred language"),
        max_length=8,
        choices=settings.LANGUAGES,
        default=settings.LANGUAGE_CODE,
    )
    preferred_theme = models.CharField(
        _("preferred theme"),
        max_length=8,
        choices=ThemeChoice.choices,
        default=ThemeChoice.AUTO,
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects = UserManager()

    @property
    def role(self) -> str | None:
        """Derived role precedence: admin > captain > player > None."""
        if self.is_admin:
            return "admin"
        if self.is_captain:
            return "captain"
        if self.is_player:
            return "player"
        return None

    @property
    def is_admin(self) -> bool:
        """Admin group OR superuser (the fixed system user is always Admin).

        Backed by the per-instance role cache in ``app.core.permissions``
        (invalidated on group changes / ``refresh_from_db``).
        """
        return user_is_admin(self)

    @property
    def is_captain(self) -> bool:
        return user_is_captain(self)

    @property
    def is_player(self) -> bool:
        return user_is_player(self)

    @property
    def is_approved(self) -> bool:
        """True when the admin approval workflow allows a login."""
        return self.approval_status == ApprovalStatus.APPROVED or self.is_superuser

    def refresh_from_db(self, using=None, fields=None, **kwargs):
        """Group flags are not model fields — drop the role cache on reload."""
        _invalidate_role_cache(self)
        return super().refresh_from_db(using=using, fields=fields, **kwargs)

    def __str__(self):
        return self.email

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
