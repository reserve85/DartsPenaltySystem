"""Matchday form — opponent, team, season, venue, date. NO participants.

Participants are not part of this form anymore: they are assigned
exclusively with the "Add players" editor on the matchday detail page.
"""

from typing import ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from app.core.permissions import user_is_admin, user_is_captain
from app.matchdays.models import Matchday, Season
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
    """Team, season, opponent, venue, date, description — no participant picker.

    Who played is decided on the detail page ("Add players"): this form only
    edits the matchday itself and never touches the participant list.
    """

    team = forms.ModelChoiceField(queryset=Team.objects.all(), label=_("Team"))
    season = forms.ModelChoiceField(
        queryset=Season.objects.all().order_by("-name"),
        required=False,
        label=_("Season"),
        help_text=_("Every matchday belongs to one season (a closed cash box)."),
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

    def _roster_season(self):
        """Season whose active teams may be picked (same fallback as ``clean``)."""
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
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.team_id is None and self.team_locked and self.fields["team"].initial:
            # Disabled field never posts for captains; restore from initial.
            instance.team = Team.objects.get(pk=self.fields["team"].initial)
        if commit:
            # Participants are NEVER written here — they belong exclusively to
            # the "Add players" editor on the matchday detail page.
            instance.save()
        return instance
