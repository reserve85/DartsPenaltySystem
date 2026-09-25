from django.db import models
from django.utils.translation import gettext_lazy as _


class ThemeChoice(models.TextChoices):
    AUTO = "auto", _("Auto")
    LIGHT = "light", _("Light")
    DARK = "dark", _("Dark")


# Cookie that keeps the theme choice of visitors without an account across
# page changes. Written by app/static/js/theme.js — keep the name in sync.
THEME_COOKIE_NAME = "dpm_theme"
