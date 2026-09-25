"""Shared view/HTTP helpers — the single home for cross-app URL utilities."""

from django.http import Http404


def safe_next_url(request, fallback: str) -> str:
    """Same-host path from a hidden ``next`` field; anything else -> fallback.

    Rejects absolute URLs and protocol-relative references (``//host``,
    ``/\\host``) so a crafted ``next`` value can never leave this site.
    """
    next_url = request.POST.get("next", "")
    if next_url.startswith("/") and not next_url.startswith(("//", "/\\")):
        return next_url
    return fallback


def numeric_pk(raw) -> int:
    """``?team=``/``season`` must be a positive integer — anything else is a 404.

    ``get_object_or_404(pk="abc")`` / ``Model.objects.filter(pk="abc")`` would
    raise ``ValueError`` (Django expects a number for the pk field) instead of
    ``Http404`` — i.e. a 500 error page for the user.
    """
    raw = raw or ""
    if not (str(raw).isdigit() and int(raw) > 0):
        raise Http404
    return int(raw)
