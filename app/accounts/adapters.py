"""django-allauth account adapter.

Keeps authentication flows alive when the SMTP configuration is missing or
the mail server is unreachable: the failure is logged instead of raising —
the same "e-mail problems never break business flows" rule as the central
NotificationService (see ``app.notifications.services``).
"""

import logging

from allauth.account.adapter import DefaultAccountAdapter

logger = logging.getLogger("app.accounts")


class AccountAdapter(DefaultAccountAdapter):
    """Default allauth behaviour + fault-tolerant e-mail delivery."""

    def send_mail(self, template_prefix: str, email: str, context: dict) -> None:
        try:
            super().send_mail(template_prefix, email, context)
        except Exception:
            logger.exception(
                "allauth e-mail '%s' to %s could not be sent (SMTP missing or unreachable).",
                template_prefix,
                email,
            )
