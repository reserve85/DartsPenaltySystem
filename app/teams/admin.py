from typing import ClassVar

from django.contrib import admin

from app.teams.models import Team


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["name", "created_at"]
    search_fields: ClassVar[list] = ["name"]
