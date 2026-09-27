"""Matchday state relative to today — drives the row marking in tables.

Both tables that list Spieltage (the matchday list and the "Matchday totals"
block of the financial overview) mark TODAY's row green and mute matchdays
that lie in the past, so the current matchday can be spotted at a glance.
"""

from django import template
from django.utils import timezone

register = template.Library()

TODAY = "today"
PAST = "past"
UPCOMING = "upcoming"


@register.simple_tag
def matchday_state(matchday) -> str:
    """``"today"`` | ``"past"`` | ``"upcoming"`` — ``matchday.date`` vs. now.

    ``timezone.localdate()`` honours TIME_ZONE (Europe/Berlin): a matchday
    counts as "today" exactly during the local calendar day it is played,
    not just while the server clock (UTC) shows that date.
    """
    today = timezone.localdate()
    if matchday.date == today:
        return TODAY
    if matchday.date < today:
        return PAST
    return UPCOMING
