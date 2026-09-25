from django.db import models
from django.utils.translation import gettext_lazy as _


class ThemeChoice(models.TextChoices):
    AUTO = "auto", _("Auto")
    LIGHT = "light", _("Light")
    DARK = "dark", _("Dark")
