"""Audit services — the only place that writes AuditLog rows."""

from app.core.models import AuditLog


def log_action(action, *, user=None, target=None, metadata=None) -> AuditLog:
    """Create an audit entry for a user-initiated state change.

    ``target`` may be any model instance; ``target_type``/``target_id`` are
    derived from ``type(obj).__name__`` and ``obj.pk``.
    """
    entry = AuditLog(action=action, user=user, metadata=metadata or {})
    if target is not None:
        entry.target_type = type(target).__name__
        entry.target_id = target.pk
    entry.save()
    return entry
