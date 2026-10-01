"""Balance display helpers — debt (positive) vs. credit (negative).

A negative balance is money the player has OVERPAID (credit / "Guthaben").
These tags keep the UI honest about it: the amount itself turns green and
the summary labels can switch from "Penalties open" to "Credit" with the
absolute value instead of showing a confusing "-6.00 €" in red.
"""

from django import template

register = template.Library()


@register.filter
def abs(value):
    """Absolute value — e.g. a credit of −6.00 € shown as ``6.00 €``."""
    try:
        return -value if value < 0 else value
    except TypeError:
        return value


@register.filter
def balance_class(value):
    """Bootstrap text class for a balance: green credit, red debt."""
    try:
        return "text-success" if value < 0 else "text-danger"
    except TypeError:
        return "text-danger"
