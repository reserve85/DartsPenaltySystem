"""Account services — approval workflow + invitation helpers.

No e-mail dispatch here: notifications live exclusively in
``app.notifications.services``. Audit rows for the invitation lifecycle are
written by the callers (views) except ``cancel``/``resend``/``obsolete``,
which run without a request context or deep inside a signal.
"""

import secrets
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from app.accounts.models import ApprovalStatus, Invitation, User
from app.core.models import AuditAction
from app.core.services import log_action


def mark_emails_verified(user) -> None:
    """Vouch for a user's address (allauth ``EmailAddress.verified = True``).

    Called when an account becomes trustworthy for login under
    ``ACCOUNT_EMAIL_VERIFICATION = "mandatory"``:

    * admin-created accounts (created as *approved*) — the admin typed the
      address themselves, see the ``post_save`` hook in ``signals.py``,
    * accounts approved through the approval workflow — the approval e-mail
      goes to the very same address,
    * accepted invitations — the invitee proved control of the address by
      opening the link that was mailed to it.

    Self-registrations stay unverified until the user clicks the link in the
    verification e-mail (or is approved).
    """
    if not user.email:
        return
    EmailAddress.objects.filter(user=user).update(verified=True)
    if not EmailAddress.objects.filter(user=user).exists():
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
def create_invitation(*, email, role, team, player, language, invited_by) -> Invitation:
    """Create the requested user row + its invitation in ONE transaction.

    The user is created with an *unusable* password (cannot log in) — that
    reserves the unique e-mail address against a second invite or a self-
    signup. The pre-assignment payload lives on the invitation, NOT on
    ``User.player_link``: the player stays selectable everywhere until the
    invitee accepts.
    """
    with transaction.atomic():
        user = User.objects.create_user(
            email=email,
            password=None,  # set_password(None) -> unusable
            approval_status=ApprovalStatus.REQUESTED,
            preferred_language=language,
        )
        invitation = Invitation.objects.create(
            user=user,
            role=role,
            team=team,
            player=player,
            invited_by=invited_by,
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(days=settings.INVITE_EXPIRE_DAYS),
        )
    return invitation


def accept_invitation(invitation: Invitation, *, password) -> User:
    """Complete the registration: set password, apply payload, become approved.

    Everything shares one transaction — any ``ValidationError`` rolls all of
    it back. Ordering is defense-in-depth: the invitation is marked consumed
    FIRST, so the obsolete ``pre_save`` hook (see ``signals.py``) sees
    ``accepted_at IS NULL`` as false and skips it even without the
    ``exclude_user`` exclusion.

    The caller logs ``USER_INVITE_ACCEPTED`` and sends the admin
    notifications — and nothing else: no allauth ``user_signed_up``, no
    "waiting for approval" mail, no approval-request bell.
    """
    with transaction.atomic():
        invitation.refresh_from_db()
        user = invitation.user
        if invitation.accepted_at is not None:
            raise ValidationError(_("This invitation has already been used."))
        if invitation.expires_at <= timezone.now():
            raise ValidationError(
                _("This invitation has expired. Please ask the club to resend it.")
            )
        if user.approval_status != ApprovalStatus.REQUESTED:
            raise ValidationError(_("This invitation is no longer valid."))
        if (
            invitation.player_id
            and User.objects.filter(player_link_id=invitation.player_id)
            .exclude(pk=user.pk)
            .exists()
        ):
            # Defensive race re-check — the obsolete hook should normally have
            # deleted this row already.
            raise ValidationError(
                _("The pre-assigned player has already been linked to another account.")
            )

        # 1) Consume the invitation (single use: token blanked, timer frozen).
        invitation.accepted_at = timezone.now()
        invitation.token = ""
        invitation.save(update_fields=["accepted_at", "token"])

        # 2) Apply the pre-assigned payload to the user.
        user.set_password(password)
        user.approval_status = ApprovalStatus.APPROVED
        if invitation.player_id:
            user.player_link_id = invitation.player_id
        if invitation.team_id:
            user.team_id = invitation.team_id
        user.save()

        user.groups.set([Group.objects.get(name=invitation.role)])
        mark_emails_verified(user)
    return user


def cancel_invitation(invitation: Invitation, *, actor) -> None:
    """Cancel a request: the user row is HARD-deleted (cascade removes the
    invitation; the pre-assigned player becomes free again).

    The person may self-register later and then goes through the normal
    pending → approval flow.
    """
    with transaction.atomic():
        log_action(
            AuditAction.USER_INVITE_CANCELLED,
            user=actor,
            target=invitation.user,
            metadata={
                "email": invitation.user.email,
                "player_id": invitation.player_id,
                "role": invitation.role,
            },
        )
        invitation.user.delete()


def resend_invitation(invitation: Invitation, *, actor) -> None:
    """New token + fresh expiry — the old link dies immediately.

    Defensive check first: a payload player meanwhile linked to another user
    can never be accepted; a clear error beats re-sending that promise
    (normally the obsolete hook already deleted this row).
    """
    if invitation.accepted_at is not None:
        raise ValidationError(_("This invitation has already been accepted."))
    if (
        invitation.player_id
        and User.objects.filter(player_link_id=invitation.player_id)
        .exclude(pk=invitation.user_id)
        .exists()
    ):
        raise ValidationError(
            _(
                "The pre-assigned player has meanwhile been linked to another account —"
                " cancel this invitation instead."
            )
        )
    invitation.token = secrets.token_urlsafe(32)
    invitation.expires_at = timezone.now() + timedelta(days=settings.INVITE_EXPIRE_DAYS)
    invitation.save(update_fields=["token", "expires_at"])
    log_action(
        AuditAction.USER_INVITE_RESENT,
        user=actor,
        target=invitation.user,
        metadata={"email": invitation.user.email, "player_id": invitation.player_id},
    )


def obsolete_invitations_for_player(player, *, exclude_user=None) -> None:
    """Delete every OPEN request that pre-assigns ``player`` — audit-only.

    Targets ``accepted_at IS NULL`` *regardless of expiry*: a dead (expired)
    request still carries a stale player promise and must die when the player
    is linked elsewhere. ``exclude_user`` is the accepting user's own request
    (it is consumed in the same transaction). The acting admin is unknown
    here (signal context), so the row is logged with ``user=None`` — the
    metadata (e-mail, invited-by, player) tells the full story.
    """
    if player is None:
        return
    qs = Invitation.objects.filter(player=player, accepted_at__isnull=True).select_related(
        "user", "invited_by"
    )
    if exclude_user is not None:
        qs = qs.exclude(user=exclude_user)
    # Fetch first, delete second — the cascade removes the invitations.
    rows = [(invitation.user_id, invitation.user.email, invitation.invited_by) for invitation in qs]
    if not rows:
        return
    for _user_pk, email, invited_by in rows:
        log_action(
            AuditAction.USER_INVITE_OBSOLETED,
            user=None,
            metadata={
                "email": email,
                "invited_by": invited_by.email if invited_by else None,
                "player_id": player.pk,
            },
        )
    User.objects.filter(pk__in=[row[0] for row in rows]).delete()
