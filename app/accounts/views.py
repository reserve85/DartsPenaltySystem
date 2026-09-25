"""Account views — admin user management, approval workflow, settings, theme.

Login/logout/password flows are provided by django-allauth (mounted in
``config/urls.py``); this module never sends e-mails itself — all
notifications go through ``app.notifications.services.notification_service``.
"""

from typing import ClassVar

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import Group
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from app.accounts.forms import SettingsForm, UserApprovalForm, UserCreateForm, UserUpdateForm
from app.accounts.models import ApprovalStatus, User
from app.accounts.services import mark_emails_verified
from app.core.choices import ThemeChoice
from app.core.models import AuditAction
from app.core.permissions import GROUP_ADMIN, GROUP_PLAYER, GroupRequiredMixin
from app.core.services import log_action
from app.notifications.services import notification_service
from app.players.models import Player


class UserListView(GroupRequiredMixin, LoginRequiredMixin, ListView):
    groups: ClassVar[list] = [GROUP_ADMIN]
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        return User.objects.select_related("team", "player_link").order_by("email")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pending_count"] = User.objects.filter(
            approval_status=ApprovalStatus.PENDING
        ).count()
        return context


class PendingUserListView(GroupRequiredMixin, LoginRequiredMixin, ListView):
    """All self-registered accounts waiting for the admin decision."""

    groups: ClassVar[list] = [GROUP_ADMIN]
    model = User
    template_name = "accounts/pending_list.html"
    context_object_name = "pending_users"

    def get_queryset(self):
        return (
            User.objects.filter(approval_status=ApprovalStatus.PENDING)
            .select_related("player_link")
            .order_by("-date_joined")
        )


class UserApprovalView(GroupRequiredMixin, LoginRequiredMixin, View):
    """Review ONE pending user: approve (+ link/create player) or reject.

    Approve and player assignment happen in a SINGLE save operation — the
    admin needs exactly one click plus (optionally) the player selection.
    """

    groups: ClassVar[list] = [GROUP_ADMIN]
    template_name = "accounts/approval_form.html"

    def get_object(self, pk) -> User:
        return get_object_or_404(User, pk=pk)

    def get(self, request, pk):
        user = self.get_object(pk)
        return render(
            request, self.template_name, {"pending_user": user, "form": UserApprovalForm()}
        )

    def post(self, request, pk):
        user = self.get_object(pk)
        action = request.POST.get("action")

        if action == "reject":
            with transaction.atomic():
                user.approval_status = ApprovalStatus.REJECTED
                user.save(update_fields=["approval_status"])
                log_action(
                    AuditAction.USER_REJECTED,
                    user=request.user,
                    target=user,
                    metadata={"email": user.email},
                )
            notification_service.send_account_rejected(user=user, request=request)
            messages.success(request, _("User rejected."))
            return redirect("accounts:approval_list")

        if action != "approve":
            messages.error(request, _("Please choose an action."))
            return render(
                request,
                self.template_name,
                {"pending_user": user, "form": UserApprovalForm()},
            )

        form = UserApprovalForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"pending_user": user, "form": form})

        player = form.cleaned_data.get("player")
        new_player_name = (form.cleaned_data.get("new_player_name") or "").strip()
        # Approval = status + optional player creation/link + role + audit —
        # one transaction, then the e-mail fires after the commit.
        with transaction.atomic():
            if new_player_name:
                player = Player.objects.create(name=new_player_name)

            # ONE save: approval status + player link (single save operation).
            user.approval_status = ApprovalStatus.APPROVED
            if player is not None:
                user.player_link = player
            user.save()

            # Approval vouches for the address: skip the extra verify-by-link wall.
            mark_emails_verified(user)

            # Freshly approved self-registered users become players unless an
            # admin already assigned a role.
            if player is not None and not user.groups.exists():
                player_group, _created = Group.objects.get_or_create(name=GROUP_PLAYER)
                user.groups.add(player_group)

            log_action(
                AuditAction.USER_APPROVED,
                user=request.user,
                target=user,
                metadata={
                    "email": user.email,
                    "player_id": player.pk if player else None,
                },
            )
        notification_service.send_account_approved(user=user, request=request)
        messages.success(request, _("User approved."))
        return redirect("accounts:approval_list")


class UserCreateView(GroupRequiredMixin, LoginRequiredMixin, CreateView):
    groups: ClassVar[list] = [GROUP_ADMIN]
    model = User
    form_class = UserCreateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("accounts:user_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(AuditAction.USER_CREATED, user=self.request.user, target=self.object)
        messages.success(self.request, _("User created."))
        return response


class UserUpdateView(GroupRequiredMixin, LoginRequiredMixin, UpdateView):
    groups: ClassVar[list] = [GROUP_ADMIN]
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("accounts:user_list")

    def _other_admins_exist(self) -> bool:
        """True when another account could still manage the system."""
        from django.db.models import Q

        return (
            User.objects.filter(Q(is_superuser=True) | Q(groups__name=GROUP_ADMIN))
            .exclude(pk=self.request.user.pk)
            .exists()
        )

    def form_valid(self, form):
        # Lockout protection: an admin may edit themselves, but must not
        # accidentally remove the last Admin rights or deactivate themself.
        if form.instance.pk == self.request.user.pk:
            if not form.cleaned_data.get("is_active", True):
                form.add_error("is_active", _("You cannot deactivate your own account."))
                return self.form_invalid(form)
            if (
                form.cleaned_data.get("role") != GROUP_ADMIN
                and not self.request.user.is_superuser
                and not self._other_admins_exist()
            ):
                form.add_error(
                    "role",
                    _("You are the only Admin. Give the Admin role to another user first."),
                )
                return self.form_invalid(form)
        response = super().form_valid(form)
        log_action(AuditAction.USER_UPDATED, user=self.request.user, target=self.object)
        messages.success(self.request, _("User updated."))
        return response


class UserDeactivateView(GroupRequiredMixin, LoginRequiredMixin, View):
    groups: ClassVar[list] = [GROUP_ADMIN]

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        if user == request.user:
            messages.error(request, _("You cannot deactivate your own account."))
            return redirect("accounts:user_list")
        user.is_active = False
        user.save(update_fields=["is_active"])
        log_action(AuditAction.USER_DEACTIVATED, user=request.user, target=user)
        messages.success(request, _("User deactivated."))
        return redirect("accounts:user_list")


class SettingsView(LoginRequiredMixin, View):
    """Language + theme preferences (password change lives in django-allauth:
    ``account_change_password``)."""

    template_name = "accounts/settings.html"

    def get(self, request):
        return render(request, self.template_name, {"form": SettingsForm(instance=request.user)})

    def post(self, request):
        form = SettingsForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            log_action(AuditAction.USER_UPDATED, user=request.user, target=request.user)
            messages.success(request, _("Settings saved."))
            return redirect("accounts:settings")
        return render(request, self.template_name, {"form": form})


class ThemeUpdateView(LoginRequiredMixin, View):
    """POST-only JSON endpoint used by ``theme.js`` via ``X-CSRFToken``."""

    def post(self, request):
        theme = request.POST.get("theme", "")
        if theme not in ThemeChoice.values:
            return JsonResponse({"ok": False, "error": "invalid theme"}, status=400)
        request.user.preferred_theme = theme
        request.user.save(update_fields=["preferred_theme"])
        return JsonResponse({"ok": True, "theme": theme})
