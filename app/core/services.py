"""Audit services — the only place that writes AuditLog rows."""

import logging

from app.core.models import AuditAction, AuditLog

logger = logging.getLogger("app.core")


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


def log_email_outcome(
    *,
    success: bool,
    recipients,
    template: str,
    subject: str,
    bcc=None,
    language: str | None = None,
    error: str | None = None,
) -> None:
    """Best-effort ``EMAIL_SENT``/``EMAIL_FAILED`` audit row for one e-mail.

    Mirrors the project rule "mail problems never break business
    transactions": a failing audit write is only logged — it can never raise
    into the delivery path. Email rows carry ``user=None`` (the actor is not
    threaded through the async mail service); the addresses live in the
    metadata instead.
    """
    recipients = [str(address) for address in (recipients or [])]
    metadata = {
        "success": bool(success),
        "recipients": recipients,
        "recipient_count": len(recipients),
        "bcc": [str(address) for address in (bcc or [])],
        "template": template,
        "subject": str(subject),
        "language": language,
    }
    if error is not None:
        metadata["error"] = str(error)
    try:
        log_action(
            AuditAction.EMAIL_SENT if success else AuditAction.EMAIL_FAILED,
            user=None,
            metadata=metadata,
        )
    except Exception:  # pragma: no cover — defensive: audit must never break mail
        logger.warning("E-mail audit write failed (%s to %s).", template, recipients, exc_info=True)
