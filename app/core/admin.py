from typing import ClassVar

from django.contrib import admin

from app.core.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["created_at", "action", "user", "target_type", "target_id"]
    list_filter: ClassVar[list] = ["action", "created_at"]
    search_fields: ClassVar[list] = ["user__email", "target_type", "metadata"]
    readonly_fields: ClassVar[list] = [
        "action",
        "user",
        "target_type",
        "target_id",
        "metadata",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
