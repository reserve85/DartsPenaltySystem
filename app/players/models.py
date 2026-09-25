"""Players and their SEASON-DEPENDENT team assignments.

A player can play in MULTIPLE teams, and the assignment is per season: each
season a player may be assigned to different team(s) (transfer, second team,
…). The assignment rows live in :class:`PlayerTeam` — one row per
(player, team, season).

``season`` is NULL only in the pre-season state of a fresh install (no Season
exists yet); such rows are bound to the first created season (see
``app.players.services.bind_unassigned_memberships``).
"""

from typing import ClassVar

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class PlayerTeam(models.Model):
    """One season-scoped assignment: ``player`` belongs to ``team`` in ``season``."""

    player = models.ForeignKey(
        "players.Player",
        on_delete=models.CASCADE,
        related_name="team_assignments",
        verbose_name=_("player"),
    )
    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.CASCADE,
        related_name="player_assignments",
        verbose_name=_("team"),
    )
    # NULL only before the first season exists (fresh install / data migration
    # without seasons) — bound to the first created season.
    season = models.ForeignKey(
        "matchdays.Season",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="player_team_assignments",
        verbose_name=_("season"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    def __str__(self):
        season = self.season.name if self.season_id else _("no season")
        return f"{self.player.name} → {self.team.name} ({season})"

    class Meta:
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=["player", "team", "season"], name="unique_player_team_season"
            ),
            # season-less rows: UNIQUE treats NULLs as distinct → partial index.
            models.UniqueConstraint(
                fields=["player", "team"],
                condition=Q(season__isnull=True),
                name="unique_player_team_without_season",
            ),
        ]
        ordering: ClassVar[list] = ["team__name", "player__name"]
        verbose_name = _("player team assignment")
        verbose_name_plural = _("player team assignments")


class PlayerQuerySet(models.QuerySet):
    def in_team(self, team, season=None):
        """Players assigned to ``team`` — scoped to ``season`` when given.

        ``season=None`` (fresh install without any season) counts every
        assignment; otherwise only assignments of exactly that season, so each
        season can hold a different roster.
        """
        assignments = Q(team_assignments__team=team)
        if season is not None:
            assignments &= Q(team_assignments__season=season)
        return self.filter(assignments).distinct()


class PlayerManager(models.Manager.from_queryset(PlayerQuerySet)):
    def create(self, **kwargs):
        """Convenience: optional ``team=`` creates the first assignment.

        Bound to ``season=`` when given, otherwise to the newest season
        (NULL only before the first season exists). Use
        ``app.players.services.assign_teams`` to change assignments later —
        it replaces ONE season only and never touches the other seasons.
        """
        team = kwargs.pop("team", None)
        season = kwargs.pop("season", None)
        player = super().create(**kwargs)
        if team is not None:
            if season is None:
                from app.matchdays.models import Season

                season = Season.objects.order_by("-name").first()
            PlayerTeam.objects.create(player=player, team=team, season=season)
        return player


class Player(models.Model):
    name = models.CharField(max_length=120, verbose_name=_("name"))
    # Season-dependent assignment (see PlayerTeam): a player can play in
    # MULTIPLE teams per season; membership decides which team's matchdays
    # the player can participate in — and it may differ from season to season.
    teams = models.ManyToManyField(
        "teams.Team",
        through="players.PlayerTeam",
        related_name="players",
        verbose_name=_("teams"),
        help_text=_("A player can belong to multiple teams — per season."),
    )
    active = models.BooleanField(default=True, verbose_name=_("active"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    objects = PlayerManager()

    @property
    def user(self):
        """The linked user account (optional) — ``None`` for players without login.

        Convenience alias for the reverse one-to-one of ``User.player_link``
        (``related_name="user_account"``). Used by the notification eligibility
        check: ``player.user and player.user.email and player.user.is_active``.
        """
        try:
            return self.user_account
        except models.ObjectDoesNotExist:
            return None

    def __str__(self):
        return self.name

    class Meta:
        ordering: ClassVar[list] = ["name"]
        indexes: ClassVar[list] = [models.Index(fields=["name"])]
        verbose_name = _("player")
        verbose_name_plural = _("players")
