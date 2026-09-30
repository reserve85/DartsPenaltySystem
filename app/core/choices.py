from django.db import models
from django.utils.translation import gettext_lazy as _

from app.core.permissions import GROUP_ADMIN, GROUP_CAPTAIN, GROUP_PLAYER


class ThemeChoice(models.TextChoices):
    AUTO = "auto", _("Auto")
    LIGHT = "light", _("Light")
    DARK = "dark", _("Dark")


class RoleChoice(models.TextChoices):
    """Role values shared by model fields and every form.

    The values ARE the Django Group names (``GROUP_*`` from
    ``app.core.permissions``) so ``Group.objects.get(name=…)`` keeps working.
    Lives here (not in ``app.accounts.forms``) because model-level choices
    cannot import forms — that would be a circular import. Labels and values
    are the single source of truth for ``forms.ROLE_CHOICES`` too.
    """

    ADMIN = GROUP_ADMIN, _("Admin")
    CAPTAIN = GROUP_CAPTAIN, _("Captain")
    PLAYER = GROUP_PLAYER, _("Player")


# Cookie that keeps the theme choice of visitors without an account across
# page changes. Written by app/static/js/theme.js — keep the name in sync.
THEME_COOKIE_NAME = "dpm_theme"
