"""Player forms — season-dependent team assignment for admins/captains.

The ``teams`` field edits the assignments of ONE season (the globally active
season, see the navbar dropdown): saving replaces that season's assignment
only — assignments of other seasons are never touched.
"""

from typing import ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from app.core.permissions import user_is_admin, user_is_captain
from app.players.models import Player
from app.players.services import assign_teams, teams_for
from app.teams.models import Team
from app.teams.services import teams_for_season


class PlayerForm(forms.ModelForm):
    """Team assignment (multi-select) for admins; own-team toggle for captains.

    The assignment is season-scoped (``season`` kwarg = active season).
    Captains see only THEIR team as (locked) checkbox: other assignments of
    this season for a multi-team player are preserved untouched on save —
    only the captain's own team membership can be added/removed.
    """

    teams = forms.ModelMultipleChoiceField(
        queryset=Team.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        label=_("Teams"),
        help_text=_("A player can belong to multiple teams."),
    )

    class Meta:
        model = Player
        fields: ClassVar[list] = ["name", "active"]

    def __init__(self, *args, user=None, season=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(["name", "teams", "active"])
        self.season = season
        # Precedence admin > captain: hybrid accounts (Admin + Captain group,
        # possible via the Django admin group editor) get the full admin form.
        self.captain_mode = user is not None and user_is_captain(user) and not user_is_admin(user)
        self.captain_team_id = user.team_id if self.captain_mode else None

        if season is not None:
            self.fields["teams"].help_text = _(
                "Assignments are season-dependent — this saves the teams of "
                "season %(season)s; other seasons keep their own assignment."
            ) % {"season": season.name}

        if self.instance.pk:
            current = [team.pk for team in teams_for(self.instance, season)]
        else:
            current = list(self.initial.get("teams") or [])

        if not self.captain_mode:
            # Only teams active in this season may be assigned; already
            # assigned teams stay selectable so a save never drops them.
            selectable = teams_for_season(season)
            if current:
                selectable = selectable | Team.objects.filter(pk__in=current)
            self.fields["teams"].queryset = selectable

        if self.captain_mode:
            self.fields["teams"].queryset = Team.objects.filter(pk=self.captain_team_id)
            self.fields["teams"].disabled = True
            # A player may be assigned to the captain's team in ANOTHER season
            # only — an empty own-season assignment must stay valid here.
            self.fields["teams"].required = False
            # Disabled fields validate against form.initial, so reduce the
            # initial value to the membership this captain may actually see.
            self.initial["teams"] = [pk for pk in current if pk == self.captain_team_id]
            if not self.instance.pk and not self.initial["teams"]:
                self.initial["teams"] = [self.captain_team_id]
        else:
            self.initial["teams"] = current

    def save(self, commit=True):
        player = super().save(commit=commit)
        if not player.pk:
            return player
        selected = list(self.cleaned_data.get("teams") or [])
        if self.captain_mode:
            # The disabled field only ever contains the captain's own team:
            # keep the player's OTHER teams of this season untouched and
            # toggle only the captain's own team membership.
            current = {team.pk for team in teams_for(player, self.season)}
            final_pks = (current - {self.captain_team_id}) | {team.pk for team in selected}
            final = [team for team in teams_for(player, self.season) if team.pk in final_pks]
            final += [team for team in selected if team.pk not in current]
            assign_teams(player, final, season=self.season)
        else:
            assign_teams(player, selected, season=self.season)
        return player
