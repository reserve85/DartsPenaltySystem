from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class VenueChoice(models.TextChoices):
    HOME = "home", _("Home")  # Heimspiel
    AWAY = "away", _("Away")  # Auswärtsspiel


class Season(models.Model):
    """One closed cash box.

    Balances and payments only ever count WITHIN one season — open amounts
    are not carried over from season to season.

    The season shown to a user is resolved in this order (see
    ``app.matchdays.services.season_for_request``):

    1. the season picked in the navbar dropdown — a PERSONAL override that
       only lives in the user's session and never changes the default of
       anybody else,
    2. the admin-defined default season (``is_default``),
    3. the newest season by name (before any default was ever set).

    Each season additionally holds its own roster of ACTIVE teams: a season
    with two Mannschaften only shows those two, next season there may be four.
    An empty ``teams`` selection means "every team is active" (seasons created
    before this feature keep their old scope).
    """

    name = models.CharField(
        max_length=32,
        unique=True,
        verbose_name=_("name"),
        help_text=_('Convention YYYY/YYYY, e.g. "2026/2027".'),
    )
    teams = models.ManyToManyField(
        "teams.Team",
        blank=True,
        related_name="seasons",
        verbose_name=_("teams"),
        help_text=_(
            "Only these teams are active in this season. Select no team to activate every team."
        ),
    )
    is_default = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("default season"),
        help_text=_(
            "The default season is shown to every user who has not picked a "
            "season themselves. A personal choice in the navbar only lasts "
            "for the session and never changes this default."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    @classmethod
    def default(cls) -> "Season | None":
        """The admin-defined default season; ``None`` when none was set yet."""
        return cls.objects.filter(is_default=True).order_by("-name").first()

    def save(self, *args, **kwargs):
        """Only ONE season may be the default at a time."""
        if self.is_default:
            others = Season.objects.filter(is_default=True)
            if self.pk:
                others = others.exclude(pk=self.pk)
            others.update(is_default=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering: ClassVar[list] = ["-name"]
        verbose_name = _("season")
        verbose_name_plural = _("seasons")


class Matchday(models.Model):
    team = models.ForeignKey(
        "teams.Team", on_delete=models.CASCADE, related_name="matchdays", verbose_name=_("team")
    )
    season = models.ForeignKey(
        Season,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="matchdays",
        verbose_name=_("season"),
        help_text=_("Every matchday belongs to one season (a closed cash box)."),
    )
    opponent = models.CharField(
        max_length=255, verbose_name=_("opponent"), help_text=_('Free text, e.g. "SV Eichenberg".')
    )
    venue = models.CharField(
        max_length=8, choices=VenueChoice.choices, default=VenueChoice.HOME, verbose_name=_("venue")
    )
    date = models.DateField(db_index=True, verbose_name=_("date"))
    description = models.CharField(max_length=255, blank=True, verbose_name=_("description"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    @property
    def display_label(self) -> str:
        """Explicit ``d.m.Y`` date, home/away ordering (M6).

        home -> ``24.09.2026 - Wildboars 1 - SV Eichenberg``
        away -> ``29.09.2026 - SV Burghausen - Wildboars 1``
        """
        date = self.date.strftime("%d.%m.%Y")
        if self.venue == VenueChoice.HOME:
            return f"{date} - {self.team.name} - {self.opponent}"
        return f"{date} - {self.opponent} - {self.team.name}"

    def __str__(self):
        return self.display_label

    class Meta:
        ordering: ClassVar[list] = ["-date", "-created_at"]
        verbose_name = _("matchday")
        verbose_name_plural = _("matchdays")


class MatchdayPlayer(models.Model):
    matchday = models.ForeignKey(
        Matchday, on_delete=models.CASCADE, related_name="participants", verbose_name=_("matchday")
    )
    player = models.ForeignKey(
        "players.Player",
        on_delete=models.CASCADE,
        related_name="matchday_participations",
        verbose_name=_("player"),
    )

    def __str__(self):
        return f"{self.player.name} @ {self.matchday.display_label}"

    class Meta:
        constraints: ClassVar[list] = [
            models.UniqueConstraint(fields=["matchday", "player"], name="unique_matchday_player")
        ]
        verbose_name = _("matchday player")
        verbose_name_plural = _("matchday players")
