"""Custom User model — email login, no username, roles via Django Groups.

Users and Players are decoupled entities (D2); ``player_link`` is an optional
1:1 link letting a Player-role user see "own penalties". ``is_staff`` is
auto-derived from group membership (H1); see ``app/accounts/signals.py``.
"""

from datetime import time as dt_time
from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from app.accounts.managers import UserManager
from app.core.choices import PenaltyNotifyChoice, RoleChoice, ThemeChoice
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
    # Admin-sent invitation: created with an unusable password; becomes
    # "approved" the moment the invitee accepts (sets a password) — no
    # approval step, see ``app.accounts.services.accept_invitation``.
    REQUESTED = "requested", _("Invited")


class User(AbstractUser):
    username = None
    email = models.EmailField(_("email address"), unique=True)

    # Name entered at self-registration ("Konto erstellen"): shown to admins
    # on the approval screen and matched against Player.name to pre-select
    # the player link. Optional at model level — invited accounts, legacy
    # pending users and admin-created accounts never go through the signup
    # form and simply carry an empty value.
    full_name = models.CharField(
        _("first and last name"),
        max_length=150,
        blank=True,
        help_text=_("Name entered at registration — used to assign the player during approval."),
    )

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
        help_text=_(
            "Optional captaincy: the team this account leads. Allowed for Captain and Admin."
        ),
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

    # -- notification preferences (opt-in/out per mail type, see Settings) --
    # Penalties are NOT mailed one row at a time: every new penalty is queued
    # (NotificationOutbox) and mailed as ONE message per player — either after
    # the coalescing window (``immediate``) or as the daily digest at
    # ``penalty_notify_time`` (``daily``). Default: digest, so entering five
    # penalties never produces five e-mails out of the box.
    penalty_notify_mode = models.CharField(
        _("penalty notifications"),
        max_length=16,
        choices=PenaltyNotifyChoice.choices,
        default=PenaltyNotifyChoice.DAILY,
        help_text=_("When newly assigned penalties are e-mailed to you."),
    )
    penalty_notify_time = models.TimeField(
        _("digest time"),
        default=dt_time(8, 0),
        help_text=_("Only used by the daily digest."),
    )
    repayment_notify = models.BooleanField(
        _("payment confirmations"),
        default=True,
        help_text=_("One e-mail per recorded repayment (receipt)."),
    )
    club_news_optin = models.BooleanField(
        _("club news"),
        default=True,
        help_text=_("Round mails / club information from your club."),
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

    @property
    def pending_invitation(self):
        """The open invitation of this user — or ``None``.

        Template-safe accessor (``user_list.html``): the reverse one-to-one
        raises ``RelatedObjectDoesNotExist`` when no row exists, which templates
        swallow silently — this property returns ``None`` instead so the
        template never depends on that behaviour. Resolved at call time (the
        ``Invitation`` model is defined below), so a plain try/except suffices.
        """
        try:
            return self.invitation
        except ObjectDoesNotExist:
            return None

    def refresh_from_db(self, using=None, fields=None, **kwargs):
        """Group flags are not model fields — drop the role cache on reload."""
        _invalidate_role_cache(self)
        return super().refresh_from_db(using=using, fields=fields, **kwargs)

    def __str__(self):
        return self.email

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")


class Invitation(models.Model):
    """One admin-sent invitation: pre-assignment payload + single-use token.

    The payload (role, team, player) lives HERE — not on ``User.player_link`` —
    so the pre-assigned player stays free (``user_account IS NULL``) for every
    other registration form while the request is open. The payload is applied
    to the ``User`` only when the invitee accepts
    (``app.accounts.services.accept_invitation``).

    The companion user row is created at invite time with
    ``approval_status="requested"`` and an unusable password (reserves the
    unique e-mail address); cancel deletes the user, cascade deletes this row.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="invitation",
        verbose_name=_("user"),
    )
    token = models.CharField(
        _("token"),
        max_length=64,
        unique=True,
        db_index=True,
        help_text=_("Single-use acceptance token — blanked on acceptance, regenerated on resend."),
    )
    role = models.CharField(
        _("role"),
        max_length=16,
        choices=RoleChoice.choices,
        default=RoleChoice.PLAYER,
    )
    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitations",
        verbose_name=_("team"),
        help_text=_("Optional captaincy pre-assignment — only valid for Captain/Admin."),
    )
    player = models.ForeignKey(
        "players.Player",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitations",
        verbose_name=_("player"),
        help_text=_("Pre-assigned player — linked to the user on acceptance."),
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invited_accounts",
        verbose_name=_("invited by"),
    )
    created_at = models.DateTimeField(_("invited at"), auto_now_add=True)
    expires_at = models.DateTimeField(_("expires at"), db_index=True)
    accepted_at = models.DateTimeField(_("accepted at"), null=True, blank=True)

    @property
    def is_open(self) -> bool:
        """Reservation filter: unaccepted AND not yet expired."""
        return self.accepted_at is None and self.expires_at > timezone.now()

    @property
    def is_expired(self) -> bool:
        return self.accepted_at is None and self.expires_at <= timezone.now()

    @property
    def expiring_soon(self) -> bool:
        """Open and less than 3 days of validity left (list badge)."""
        return self.is_open and self.expires_at <= timezone.now() + timedelta(days=3)

    @property
    def status(self) -> str:
        """List badge state: open / expiring / expired / accepted."""
        if self.accepted_at is not None:
            return "accepted"
        if self.is_expired:
            return "expired"
        if self.expiring_soon:
            return "expiring"
        return "open"

    def __str__(self):
        return f"invitation for {self.user}"

    class Meta:
        ordering: ClassVar[list] = ["-created_at"]
        indexes: ClassVar[list] = [models.Index(fields=["accepted_at", "expires_at"])]
        verbose_name = _("invitation")
        verbose_name_plural = _("invitations")
