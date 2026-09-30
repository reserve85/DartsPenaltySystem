"""User + auth signals.

- ``user_logged_in``/``user_logged_out`` write AuditLog entries.
- ``is_staff`` is re-derived from group membership on ``User.groups`` changes
  (``m2m_changed``) and on every user save (``post_save``) so group edits made
  outside the custom forms (e.g. the Django Admin group editor) cannot leave
  ``is_staff`` stale (H1). Superusers stay staff regardless of groups.
- ``user_signed_up`` (django-allauth): audit the self-registration and notify
  the admins that a new account is waiting for approval — by e-mail AND as an
  in-app notification (navbar bell, django-notifications-hq).
- ``pre_save`` on User: the obsolete rule — whenever ``player_link`` gets a
  new player, every OPEN invitation reserving that player dies (its requested
  user is deleted, audit ``user_invite_obsoleted``). One hook covers every
  link path (approval, user create/edit forms, Django admin, acceptance).
"""

import logging

from allauth.account.signals import user_signed_up
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import receiver

from app.accounts.models import ApprovalStatus
from app.core.models import AuditAction
from app.core.permissions import GROUP_ADMIN, _invalidate_role_cache
from app.core.services import log_action

User = get_user_model()

logger = logging.getLogger("app.accounts")


def _sync_is_staff(user) -> None:
    if user.pk is None:
        return
    desired = user.is_superuser or user.groups.filter(name=GROUP_ADMIN).exists()
    if user.is_staff != desired:
        user.is_staff = desired
        user.save(update_fields=["is_staff"])


@receiver(post_save, sender=User)
def sync_is_staff_on_save(sender, instance, raw=False, **kwargs):
    if raw:
        return
    _invalidate_role_cache(instance)  # e.g. is_superuser changed -> fresh flags
    _sync_is_staff(instance)


@receiver(post_save, sender=User)
def verify_email_for_created_approved_users(sender, instance, created, raw=False, **kwargs):
    """Admin-created accounts (created *approved*) get a verified e-mail address.

    With ``ACCOUNT_EMAIL_VERIFICATION = "mandatory"`` allauth blocks logins of
    unverified addresses — an account an admin created directly must never hit
    that wall. Self-registrations (created *pending*) keep the normal
    verify-by-link flow.
    """
    if raw or not created:
        return
    if instance.approval_status != ApprovalStatus.APPROVED:
        return
    from app.accounts.services import mark_emails_verified

    mark_emails_verified(instance)


@receiver(pre_save, sender=User)
def obsolete_invites_on_player_link_change(sender, instance, **kwargs):
    """The obsolete rule: linking a player kills every OPEN invitation for it.

    Cheap guards first: ``update_fields``-saves that do not touch
    ``player_link`` (the frequent ``save(update_fields=["is_staff"])`` sync
    path) and unchanged values cost nothing. A fresh row (``pk is None``)
    reads no old value — an old link cannot exist, so a non-NULL
    ``player_link`` counts as the change (covers ``accounts:user_create``).

    The whole body is wrapped so a signal can never break a save (project
    rule); failures are logged instead. Lazy import keeps the service out of
    the module import graph (same pattern as
    ``verify_email_for_created_approved_users``).
    """
    update_fields = kwargs.get("update_fields")
    if kwargs.get("raw"):  # loaddata fixtures must not fire business rules
        return
    if update_fields is not None and "player_link" not in update_fields:
        return
    new_player_id = instance.player_link_id
    if new_player_id is None:
        return  # unlinking frees nobody's reservation — only links kill requests
    try:
        old_player_id = None
        if instance.pk is not None:
            old_player_id = (
                sender.objects.filter(pk=instance.pk)
                .values_list("player_link_id", flat=True)
                .first()
            )
        if new_player_id == old_player_id:
            return
        from app.accounts.services import obsolete_invitations_for_player

        obsolete_invitations_for_player(instance.player_link, exclude_user=instance)
    except Exception:  # pragma: no cover — defensive: a signal must never break a save
        logger.exception(
            "Obsolete-invitation check failed for user pk=%s (player_link_id=%s).",
            instance.pk,
            new_player_id,
        )


@receiver(m2m_changed, sender=User.groups.through)
def sync_is_staff_on_groups_change(sender, instance, action, **kwargs):
    if action.startswith("post_") and isinstance(instance, User):
        _invalidate_role_cache(instance)  # membership changed -> drop cached flags
        _sync_is_staff(instance)


@receiver(user_logged_in)
def audit_login(sender, request, user, **kwargs):
    log_action(AuditAction.LOGIN, user=user)


@receiver(user_logged_out)
def audit_logout(sender, request, user, **kwargs):
    if user is not None and getattr(user, "is_authenticated", False):
        log_action(AuditAction.LOGOUT, user=user)


def _on_user_signed_up(sender, request, user, **kwargs):
    """Self-registration (django-allauth): audit + admin notifications.

    The account is created with ``approval_status="pending"`` (model default)
    and cannot log in until an admin approves it — the admins learn about the
    new registration via e-mail (central NotificationService, never inline)
    and via an in-app notification (navbar bell, never raising).
    """
    from app.notifications.inapp import notify_new_approval_request
    from app.notifications.services import notification_service

    log_action(
        AuditAction.USER_REGISTERED,
        user=user,
        target=user,
        metadata={"email": user.email},
    )
    notification_service.send_registration_received(user=user, request=request)
    notify_new_approval_request(user)


user_signed_up.connect(_on_user_signed_up, dispatch_uid="accounts.user_signed_up")
