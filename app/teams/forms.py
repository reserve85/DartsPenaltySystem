from typing import ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from app.matchdays.models import Season
from app.teams.models import Team


def approved_active_users_queryset():
    """Approved + active accounts, ordered by e-mail — cashier candidates.

    Lazy ``get_user_model()`` import keeps app-population order intact
    (``accounts.models`` must not be imported at module level here).
    """
    from django.contrib.auth import get_user_model

    from app.accounts.models import ApprovalStatus

    User = get_user_model()
    return (
        User.objects.filter(approval_status=ApprovalStatus.APPROVED, is_active=True)
        .select_related("player_link")
        .order_by("email")
    )


class CashierForm(forms.Form):
    """Assign (or clear, ``user=""``) the cashier of one team.

    The ``team`` queryset is narrowed per submitting user in ``__init__`` —
    captains may only post their OWN team (enforced server-side; disabled
    options in the template are never trusted).
    """

    team = forms.ModelChoiceField(queryset=Team.objects.all())
    user = forms.ModelChoiceField(queryset=Team.objects.none(), required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from app.core.permissions import user_is_admin

        self.fields["user"].queryset = approved_active_users_queryset()
        if user is not None and not user_is_admin(user):
            self.fields["team"].queryset = Team.objects.filter(pk=user.team_id)
        else:
            self.fields["team"].queryset = Team.objects.all()


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
