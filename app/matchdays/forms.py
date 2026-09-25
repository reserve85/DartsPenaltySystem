"""Matchday form — opponent required, participants as checkbox chips (M5)."""

from typing import ClassVar

from django import forms
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from app.core.permissions import user_is_admin, user_is_captain
from app.matchdays.models import Matchday, MatchdayPlayer, Season
from app.players.models import Player
from app.teams.models import Team
from app.teams.services import teams_for_season


class SeasonForm(forms.ModelForm):
    """Admin: create or edit a season — name, active teams, default flag.

    ``teams`` is optional: an empty selection activates EVERY team (that is
    how seasons behave that were created before per-season teams existed).
    """

    teams = forms.ModelMultipleChoiceField(
        queryset=Team.objects.all(),
        required=False,
        label=_("Active teams"),
        help_text=_(
            "Only these teams are active in this season. Select no team to activate every team."
        ),
    )

    class Meta:
        model = Season
        fields: ClassVar[list] = ["name", "teams", "is_default"]
        widgets: ClassVar[dict] = {"name": forms.TextInput(attrs={"placeholder": "2026/2027"})}
        labels: ClassVar[dict] = {"is_default": _("Default season")}
        help_texts: ClassVar[dict] = {
            "is_default": _(
                "The default season is shown to every user who has not picked "
                "a season themselves. A personal choice in the navbar only "
                "lasts for the session and never changes this default."
            )
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # The chip template compares primary keys, so expose pks (the
            # automatic initial would be a Team queryset).
            self.initial["teams"] = list(self.instance.teams.values_list("pk", flat=True))


class MatchdayForm(forms.ModelForm):
    """Only ACTIVE players of the matchday's team are selectable (M7)."""

    team = forms.ModelChoiceField(queryset=Team.objects.all(), label=_("Team"))
    season = forms.ModelChoiceField(
        queryset=Season.objects.all().order_by("-name"),
        required=False,
        label=_("Season"),
        help_text=_("Every matchday belongs to one season (a closed cash box)."),
    )
    participants = forms.ModelMultipleChoiceField(
        queryset=Player.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=True,
        label=_("Participants"),
        help_text=_("Select at least one participant."),
    )

    class Meta:
        model = Matchday
        fields: ClassVar[list] = [
            "team",
            "season",
            "opponent",
            "venue",
            "date",
            "description",
            "participants",
        ]
        widgets: ClassVar[dict] = {
            # type="date" strictly requires ISO (YYYY-MM-DD). Without an explicit
            # format the locale decides (de: dd.mm.yyyy), which the browser cannot
            # map onto the date input -> it shows an EMPTY field although the date
            # is stored, and every save then fails with "This field is required."
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, user=None, season=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.team_locked = False
        # Precedence admin > captain: an Admin who also sits in the Captain
        # group keeps the free team choice (roles are single-valued by design,
        # but the Django admin group editor can produce hybrid accounts).
        if user is not None and user_is_captain(user) and not user_is_admin(user):
            self.team_locked = True
            self.fields["team"].queryset = Team.objects.filter(pk=user.team_id)
            self.fields["team"].initial = user.team_id
            self.fields["team"].disabled = True
        if not self.instance.pk and season is not None and self.initial.get("season") is None:
            # New matchdays default to the globally active season.
            self.initial["season"] = season.pk

        if not self.team_locked:
            # Only teams active in the matchday's season can be picked; an
            # existing matchday keeps its own team even when it is inactive.
            selectable = teams_for_season(self._roster_season())
            if self.instance.pk:
                selectable = selectable | Team.objects.filter(pk=self.instance.team_id)
            self.fields["team"].queryset = selectable

        team_id = None
        if self.instance.pk:
            team_id = self.instance.team_id
        elif self.is_bound and self.data.get("team"):
            team_id = self.data.get("team")
        elif self.fields["team"].initial:
            team_id = self.fields["team"].initial
        self.participant_team_id = team_id

        roster_season = self._roster_season()
        if team_id:
            # Roster is season-dependent: only players assigned to THIS team
            # in the matchday's season are eligible (multi-team players for
            # each of their teams of that season).
            self.fields["participants"].queryset = (
                Player.objects.in_team(team_id, roster_season).filter(active=True).order_by("name")
            )
        if self.instance.pk:
            self.fields["participants"].initial = self.instance.participants.values_list(
                "player_id", flat=True
            )

    def _roster_season(self):
        """Season the participant roster is built for (same rules as ``clean``)."""
        season = None
        if self.is_bound:
            season_pk = self.data.get("season")
            if season_pk:
                season = Season.objects.filter(pk=season_pk).first()
        elif self.initial.get("season"):
            season = Season.objects.filter(pk=self.initial["season"]).first()
        if season is None and self.instance.pk:
            season = self.instance.season
        if season is None:
            season = Season.objects.order_by("-name").first()
        return season

    def clean(self):
        cleaned = super().clean()
        season = cleaned.get("season")
        if season is None:
            if self.instance.pk:
                # Editing keeps the stored season when the field is empty.
                cleaned["season"] = self.instance.season
            else:
                # New matchdays fall back to the newest season (if any exist).
                cleaned["season"] = Season.objects.order_by("-name").first()
        participants = cleaned.get("participants")
        if participants is not None and len(participants) < 1:
            self.add_error("participants", _("Select at least one participant."))
        team = cleaned.get("team")
        if team is None and (self.instance and self.instance.pk):
            team = self.instance.team
        if team is not None and participants:
            # Season-dependent membership: a player must be assigned to the
            # team in the matchday's season to participate.
            allowed = set(
                Player.objects.in_team(team, cleaned.get("season")).values_list("pk", flat=True)
            )
            if any(player.pk not in allowed for player in participants):
                raise forms.ValidationError(_("All participants must belong to the selected team."))
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.team_id is None and self.team_locked and self.fields["team"].initial:
            # Disabled field never posts for captains; restore from initial.
            instance.team = Team.objects.get(pk=self.fields["team"].initial)
        if commit:
            # Matchday + its participant list change together — one unit of work.
            with transaction.atomic():
                instance.save()
                self._save_participants(instance)
        return instance

    def _save_participants(self, instance):
        instance.participants.all().delete()
        for player in self.cleaned_data["participants"]:
            MatchdayPlayer.objects.create(matchday=instance, player=player)
