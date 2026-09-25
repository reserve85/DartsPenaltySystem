from typing import ClassVar

from django.contrib import admin

from app.penalties.models import Payment, Penalty, PenaltyCatalogItem, TeamCatalogAmount


@admin.register(PenaltyCatalogItem)
class PenaltyCatalogItemAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["description", "amount_eur", "type", "active"]
    list_filter: ClassVar[list] = ["type", "active"]
    search_fields: ClassVar[list] = ["description"]


@admin.register(TeamCatalogAmount)
class TeamCatalogAmountAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = [
        "catalog_item",
        "team",
        "amount_eur",
        "updated_by",
        "updated_at",
    ]
    list_filter: ClassVar[list] = ["team"]
    search_fields: ClassVar[list] = ["catalog_item__description", "team__name"]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["player", "team", "amount_eur", "created_by", "created_at"]
    list_filter: ClassVar[list] = ["team"]
    search_fields: ClassVar[list] = ["player__name"]


@admin.register(Penalty)
class PenaltyAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = [
        "description_snapshot",
        "player",
        "matchday",
        "amount_eur",
        "group_id",
        "deleted_at",
        "created_at",
    ]
    list_filter: ClassVar[list] = ["matchday__team", "deleted_at"]
    search_fields: ClassVar[list] = ["description_snapshot", "player__name"]

    def get_queryset(self, request):
        # Audit/administration must see soft-deleted penalties too.
        return Penalty.all_objects().select_related("player", "matchday")
