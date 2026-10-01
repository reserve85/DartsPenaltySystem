from typing import ClassVar

from django.contrib import admin

from app.teams.models import Team, TeamCashier


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["name", "created_at"]
    search_fields: ClassVar[list] = ["name"]


@admin.register(TeamCashier)
class TeamCashierAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["team", "user_label", "valid_from", "valid_to", "created_by"]
    list_filter: ClassVar[list] = ["team"]
    search_fields: ClassVar[list] = ["user_label", "team__name"]
