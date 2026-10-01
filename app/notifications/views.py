"""In-app mailbox actions — delete one / delete read (POST-only).

django-notifications-hq ships a GET-based, single-row ``delete`` view and has
NO bulk "delete read" endpoint, so both destructive actions of this mailbox
are custom views: POST + CSRF (the pattern of ``penalties:payment_delete``),
never GET. ``?next=`` (POST field ``next``) is validated with
``url_has_allowed_host_and_scheme`` before redirecting.
"""

from typing import ClassVar

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from notifications.models import Notification


def _redirect_after(request, fallback_name: str):
    """Validated redirect to ``next`` (POST field) — else the fallback view."""
    next_url = request.POST.get("next") or ""
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(next_url)
    return redirect(reverse(fallback_name))


class NotificationDeleteView(LoginRequiredMixin, View):
    """Delete ONE row of the caller's own mailbox (hard delete, POST-only).

    Scoped to ``recipient=request.user`` — deleting somebody else's row is a
    404, never a silent success. GET answers 405 (the package's GET-based
    ``delete`` view is deliberately NOT reused for destructive actions).
    """

    http_method_names: ClassVar[list] = ["post"]

    def post(self, request, pk):
        notification = get_object_or_404(Notification, recipient=request.user, pk=pk)
        notification.delete()
        return _redirect_after(request, "notifications:all")


class NotificationDeleteReadView(LoginRequiredMixin, View):
    """Delete every READ row of the caller's mailbox — unread rows survive.

    The missing bulk counterpart of the package (review: no "delete read"
    API); never touches another user's rows (scoped through the recipient
    related manager).
    """

    http_method_names: ClassVar[list] = ["post"]

    def post(self, request):
        request.user.notifications.filter(unread=False).delete()
        return _redirect_after(request, "notifications:all")
