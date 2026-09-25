from typing import ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from app.teams.models import Team


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields: ClassVar[list] = ["name"]
        widgets: ClassVar[dict] = {
            "name": forms.TextInput(attrs={"placeholder": _("e.g. Wildboars 1")}),
        }
