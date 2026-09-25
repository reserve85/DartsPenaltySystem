from typing import ClassVar

from django.contrib import admin, messages

from app.accounts.models import ApprovalStatus, User
from app.accounts.services import mark_emails_verified
from app.core.models import AuditAction
from app.core.services import log_action
from app.notifications.services import notification_service


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display: ClassVar[list] = ["email", "approval_status", "is_active", "is_superuser", "role"]
    list_filter: ClassVar[list] = ["approval_status", "is_active", "groups"]
    search_fields: ClassVar[list] = ["email"]
    ordering: ClassVar[list] = ["email"]
    filter_horizontal: ClassVar[list] = ["groups", "user_permissions"]
    actions: ClassVar[list] = ["approve_users", "reject_users"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Approval",
            {
                "fields": ("approval_status",),
                "description": (
                    "Self-registered users start as 'pending' and can only log in "
                    "after approval. The dedicated approval screen also links a player."
                ),
            },
        ),
        ("Personal", {"fields": ("team", "player_link", "preferred_language", "preferred_theme")}),
        (
            "Permissions",
            {
                "fields": ("is_active", "is_superuser", "groups", "user_permissions"),
                "description": "is_staff is derived automatically from the Admin group (H1).",
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {"classes": ("wide",), "fields": ("email", "password1", "password2")},
        ),
    )
    readonly_fields: ClassVar[list] = ["last_login", "date_joined"]

    @admin.action(description="Approve selected users (e-mail is sent)")
    def approve_users(self, request, queryset):
        count = 0
        for user in queryset.exclude(approval_status=ApprovalStatus.APPROVED):
            user.approval_status = ApprovalStatus.APPROVED
            user.save(update_fields=["approval_status"])
            mark_emails_verified(user)
            log_action(AuditAction.USER_APPROVED, user=request.user, target=user)
            notification_service.send_account_approved(user=user, request=request)
            count += 1
        self.message_user(request, f"{count} user(s) approved.", messages.SUCCESS)

    @admin.action(description="Reject selected users (e-mail is sent)")
    def reject_users(self, request, queryset):
        count = 0
        for user in queryset.exclude(approval_status=ApprovalStatus.REJECTED):
            user.approval_status = ApprovalStatus.REJECTED
            user.save(update_fields=["approval_status"])
            log_action(AuditAction.USER_REJECTED, user=request.user, target=user)
            notification_service.send_account_rejected(user=user, request=request)
            count += 1
        self.message_user(request, f"{count} user(s) rejected.", messages.SUCCESS)
