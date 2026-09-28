"""django-allauth account adapter.

Keeps authentication flows alive when the SMTP configuration is missing or
the mail server is unreachable: the failure is logged instead of raising —
the same "e-mail problems never break business flows" rule as the central
NotificationService (see ``app.notifications.services``).

Every send (success or failure) additionally writes a best-effort
``EMAIL_SENT``/``EMAIL_FAILED`` audit row via ``log_email_outcome`` — this
adapter is the only e-mail path outside the NotificationService.
"""

import logging

from allauth.account.adapter import DefaultAccountAdapter
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string

from app.core.services import log_email_outcome

logger = logging.getLogger("app.accounts")


class AccountAdapter(DefaultAccountAdapter):
    """Default allauth behaviour + fault-tolerant e-mail delivery + audit."""

    @staticmethod
    def _audit_subject(template_prefix: str, email: str, context: dict) -> str:
        """Best-effort subject for the audit row (rendered like allauth does)."""
        try:
            subject = render_to_string(
                f"{template_prefix}_subject.txt", {**context, "email": email}
            )
        except TemplateDoesNotExist:
            return template_prefix
        return " ".join(subject.splitlines()).strip() or template_prefix

    def send_mail(self, template_prefix: str, email: str, context: dict) -> None:
        subject = self._audit_subject(template_prefix, email, context)
        try:
            super().send_mail(template_prefix, email, context)
        except Exception as exc:
            logger.exception(
                "allauth e-mail '%s' to %s could not be sent (SMTP missing or unreachable).",
                template_prefix,
                email,
            )
            log_email_outcome(
                success=False,
                recipients=[email],
                template=f"allauth:{template_prefix}",
                subject=subject,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            log_email_outcome(
                success=True,
                recipients=[email],
                template=f"allauth:{template_prefix}",
                subject=subject,
            )
