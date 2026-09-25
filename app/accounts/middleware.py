"""Approval gate — non-approved sessions never see the application.

The login form (``ApprovalLoginForm``) already shows a clear error message for
pending/rejected accounts; this middleware is the safety net for stale
sessions and for login paths outside django-allauth (e.g. the Django admin).
Superusers (the env-seeded system account) are never subject to the approval
workflow.
"""

from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _


class ApprovalGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not user.is_superuser
            and not user.is_approved
        ):
            logout(request)
            if user.approval_status == "rejected":
                messages.error(
                    request,
                    _("Your registration has been declined. Please contact the club."),
                )
            else:
                messages.error(
                    request,
                    _(
                        "Your account has not been approved by an administrator yet."
                        " Please contact the club."
                    ),
                )
            return redirect("account_login")
        return self.get_response(request)
