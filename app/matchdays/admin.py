from typing import ClassVar

from django.contrib import admin

from app.matchdays.models import Matchday, MatchdayPlayer, Season


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["name", "created_at"]
    search_fields: ClassVar[list] = ["name"]


class MatchdayPlayerInline(admin.TabularInline):
    model = MatchdayPlayer
    extra = 0


@admin.register(Matchday)
class MatchdayAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["date", "season", "team", "opponent", "venue"]
    list_filter: ClassVar[list] = ["season", "team", "venue"]
    search_fields: ClassVar[list] = ["opponent", "team__name"]
    inlines: ClassVar[list] = [MatchdayPlayerInline]
