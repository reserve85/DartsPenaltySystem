"""User + auth signals.

- ``user_logged_in``/``user_logged_out`` write AuditLog entries.
- ``is_staff`` is re-derived from group membership on ``User.groups`` changes
  (``m2m_changed``) and on every user save (``post_save``) so group edits made
  outside the custom forms (e.g. the Django Admin group editor) cannot leave
  ``is_staff`` stale (H1). Superusers stay staff regardless of groups.
- ``user_signed_up`` (django-allauth): audit the self-registration and notify
  the admins that a new account is waiting for approval.
"""

from allauth.account.signals import user_signed_up
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

from app.accounts.models import ApprovalStatus
from app.core.models import AuditAction
from app.core.permissions import GROUP_ADMIN, _invalidate_role_cache
from app.core.services import log_action

User = get_user_model()


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
    """Self-registration (django-allauth): audit + admin notification e-mail.

    The account is created with ``approval_status="pending"`` (model default)
    and cannot log in until an admin approves it — the admins learn about the
    new registration via e-mail (central NotificationService, never inline).
    """
    from app.notifications.services import notification_service

    log_action(
        AuditAction.USER_REGISTERED,
        user=user,
        target=user,
        metadata={"email": user.email},
    )
    notification_service.send_registration_received(user=user, request=request)


user_signed_up.connect(_on_user_signed_up, dispatch_uid="accounts.user_signed_up")
