from typing import ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from app.matchdays.models import Season
from app.teams.models import Team


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields: ClassVar[list] = ["name"]
        widgets: ClassVar[dict] = {
            "name": forms.TextInput(attrs={"placeholder": _("e.g. Wildboars 1")}),
        }

    def __init__(self, *args, include_season: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if include_season:
            # Create-only convenience: assign the new team to a season right
            # away (editing a team offers no re-assignment — see the season
            # form's team chips).
            self.fields["season"] = forms.ModelChoiceField(
                queryset=Season.objects.all().order_by("-name"),
                required=False,
                label=_("Assign to season"),
                help_text=_("Optionally activate the new team in an existing season."),
            )
