from typing import ClassVar

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from app.players.models import Player, PlayerTeam


class PlayerTeamInline(admin.TabularInline):
    """Raw season-dependent assignments (team + season per row)."""

    model = PlayerTeam
    extra = 0
    fields: ClassVar[list] = ["team", "season"]


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["name", "teams_list", "active", "created_at"]
    list_filter: ClassVar[list] = ["active"]
    search_fields: ClassVar[list] = ["name"]
    # Teams are edited via the inline: the assignment needs a season (the
    # plain M2M widget would write season-less rows invisible in the UI).
    fields: ClassVar[list] = ["name", "active"]
    inlines: ClassVar[list] = [PlayerTeamInline]

    @admin.display(description=_("teams"))
    def teams_list(self, obj):
        return ", ".join(
            sorted({assignment.team.name for assignment in obj.team_assignments.all()})
        )
