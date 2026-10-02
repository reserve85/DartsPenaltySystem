"""Account views — admin user management, approval workflow, settings, theme.

Login/logout/password flows are provided by django-allauth (mounted in
``config/urls.py``); this module never sends e-mails itself — all
notifications go through ``app.notifications.services.notification_service``.
"""

from typing import ClassVar

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, FormView, ListView, UpdateView

from app.accounts.forms import (
    InvitationAcceptForm,
    InviteCreateForm,
    SettingsForm,
    UserApprovalForm,
    UserCreateForm,
    UserUpdateForm,
)
from app.accounts.models import ApprovalStatus, Invitation, User
from app.accounts.services import (
    accept_invitation,
    cancel_invitation,
    create_invitation,
    mark_emails_verified,
    resend_invitation,
)
from app.core.choices import ThemeChoice
from app.core.models import AuditAction
from app.core.permissions import GROUP_ADMIN, GROUP_PLAYER, GroupRequiredMixin
from app.core.services import log_action
from app.notifications.inapp import (
    clear_approval_notifications,
    notify_approval_decided,
    notify_invitation_completed,
)
from app.notifications.services import notification_service, reschedule_pending
from app.players.models import Player


class UserListView(GroupRequiredMixin, LoginRequiredMixin, ListView):
    groups: ClassVar[list] = [GROUP_ADMIN]
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        # select_related("invitation") feeds the pre-assignment columns of
        # invited rows via the safe user.pending_invitation accessor.
        return User.objects.select_related("team", "player_link", "invitation").order_by("email")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pending_count"] = User.objects.filter(
            approval_status=ApprovalStatus.PENDING
        ).count()
        # Open requests INCLUDING expired ones — they need a resend/cancel
        # decision, so they must stay visible on the badge.
        context["invite_count"] = Invitation.objects.filter(accepted_at__isnull=True).count()
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
            clear_approval_notifications(user)
            notify_approval_decided(user, approved=False)
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
        clear_approval_notifications(user)
        notify_approval_decided(user, approved=True)
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


def _invite_guard(request, pk):
    """Block edit/deactivate of requested (invited, not yet accepted) users.

    A wrong entry is fixed with Cancel — editing here would bypass the
    invitation payload. Returns the redirect response, or ``None`` to proceed.
    """
    if User.objects.filter(pk=pk, approval_status=ApprovalStatus.REQUESTED).exists():
        messages.error(request, _("Cancel or resend the invitation instead."))
        return redirect("accounts:invite_list")
    return None


class UserUpdateView(GroupRequiredMixin, LoginRequiredMixin, UpdateView):
    groups: ClassVar[list] = [GROUP_ADMIN]
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("accounts:user_list")

    def get(self, request, *args, **kwargs):
        return _invite_guard(request, kwargs.get("pk")) or super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return _invite_guard(request, kwargs.get("pk")) or super().post(request, *args, **kwargs)

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
        blocked = _invite_guard(request, pk)
        if blocked is not None:
            return blocked
        user = get_object_or_404(User, pk=pk)
        if user == request.user:
            messages.error(request, _("You cannot deactivate your own account."))
            return redirect("accounts:user_list")
        user.is_active = False
        user.save(update_fields=["is_active"])
        log_action(AuditAction.USER_DEACTIVATED, user=request.user, target=user)
        messages.success(request, _("User deactivated."))
        return redirect("accounts:user_list")


# ---------------------------------------------------------------------------
# Invitations (admin lifecycle + public acceptance)
# ---------------------------------------------------------------------------
def _invite_error_text(error: ValidationError) -> str:
    """Flatten a (possibly multi-message) ValidationError for ``messages``."""
    return " ".join(str(message) for message in error.messages)


class InviteListView(GroupRequiredMixin, LoginRequiredMixin, ListView):
    """Open/expiring/expired/accepted requests with Resend + Cancel actions."""

    groups: ClassVar[list] = [GROUP_ADMIN]
    model = Invitation
    template_name = "accounts/invite_list.html"
    context_object_name = "invitations"
    paginate_by = 25

    def get_queryset(self):
        return Invitation.objects.select_related("user", "player", "team", "invited_by")


class InviteCreateView(GroupRequiredMixin, LoginRequiredMixin, FormView):
    """Send an invitation — spans two models, hence FormView (no CreateView)."""

    groups: ClassVar[list] = [GROUP_ADMIN]
    form_class = InviteCreateForm
    template_name = "accounts/invite_form.html"
    success_url = reverse_lazy("accounts:invite_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # The form template documents the link validity (env-configurable).
        context["invite_expire_days"] = settings.INVITE_EXPIRE_DAYS
        return context

    def form_valid(self, form):
        data = form.cleaned_data
        invitation = create_invitation(
            email=data["email"],
            role=data["role"],
            team=data.get("team"),
            player=data.get("player"),
            language=data["preferred_language"],
            invited_by=self.request.user,
        )
        log_action(
            AuditAction.USER_INVITE_SENT,
            user=self.request.user,
            target=invitation.user,
            metadata={
                "email": invitation.user.email,
                "role": invitation.role,
                "player_id": invitation.player_id,
                "team_id": invitation.team_id,
            },
        )
        # Mail AFTER commit; a dead SMTP must never look like a dead request —
        # the warning points at Resend instead of failing silently.
        sent = notification_service.send_invitation(invitation, request=self.request)
        if sent == 0:
            messages.warning(
                self.request,
                _("Invitation created, but the e-mail could not be sent — use Resend."),
            )
        else:
            messages.success(self.request, _("Invitation sent."))
        return super().form_valid(form)


class InviteResendView(GroupRequiredMixin, LoginRequiredMixin, View):
    """POST-only: new token + fresh expiry, then re-send the mail."""

    groups: ClassVar[list] = [GROUP_ADMIN]

    def post(self, request, pk):
        invitation = get_object_or_404(Invitation, pk=pk)
        try:
            resend_invitation(invitation, actor=request.user)
        except ValidationError as error:
            messages.error(request, _invite_error_text(error))
            return redirect("accounts:invite_list")
        sent = notification_service.send_invitation(invitation, request=request)
        if sent == 0:
            messages.warning(
                request,
                _("Invitation created, but the e-mail could not be sent — use Resend."),
            )
        else:
            messages.success(request, _("Invitation resent."))
        return redirect("accounts:invite_list")


class InviteCancelView(GroupRequiredMixin, LoginRequiredMixin, View):
    """POST-only + confirm: hard-delete the requested user (cascade)."""

    groups: ClassVar[list] = [GROUP_ADMIN]

    def post(self, request, pk):
        invitation = get_object_or_404(Invitation, pk=pk)
        try:
            cancel_invitation(invitation, actor=request.user)
        except ValidationError as error:
            # e.g. crafted POST against an already ACCEPTED invitation —
            # refused server-side (review B3), never a 500 or a silent delete.
            messages.error(request, _invite_error_text(error))
            return redirect("accounts:invite_list")
        messages.success(request, _("Invitation cancelled — the account was deleted."))
        return redirect("accounts:invite_list")


def _invite_unusable_reason(invitation) -> str | None:
    """Why the link cannot be used right now — or ``None`` when it can.

    Same wording as the service re-checks (one msgid per message); accepted
    rows never reach this (their token is blanked → plain 404).
    """
    if invitation.accepted_at is not None:
        return str(_("This invitation has already been used."))
    if invitation.expires_at <= timezone.now():
        return str(_("This invitation has expired. Please ask the club to resend it."))
    return None


class InviteAcceptView(View):
    """Public set-password page of ``invite/<token>/`` (CSRF-protected POST).

    GET: unknown token → 404; expired → explanatory error WITHOUT the form.
    Valid POST → accept (one transaction) → audit → admin notifications →
    success message → redirect to the login page (no auto-login, consistent
    with the approval flow). No allauth ``user_signed_up`` fires here — never
    "waiting for approval", never an approval-request bell.
    """

    template_name = "account/invite_accept.html"

    def _render(self, request, invitation, form=None, error=None):
        return render(
            request,
            self.template_name,
            {"invitation": invitation, "form": form, "error": error},
        )

    def get(self, request, token):
        invitation = get_object_or_404(Invitation, token=token)
        if request.user.is_authenticated:
            return redirect("dashboard:index")
        error = _invite_unusable_reason(invitation)
        if error is not None:
            return self._render(request, invitation, error=error)
        return self._render(request, invitation, form=InvitationAcceptForm())

    def post(self, request, token):
        invitation = get_object_or_404(Invitation, token=token)
        if request.user.is_authenticated:
            return redirect("dashboard:index")
        form = InvitationAcceptForm(request.POST)
        if not form.is_valid():
            return self._render(request, invitation, form=form)
        try:
            user = accept_invitation(invitation, password=form.cleaned_data["password1"])
        except ValidationError as error:
            form.add_error(None, error)
            return self._render(request, invitation, form=form)
        log_action(
            AuditAction.USER_INVITE_ACCEPTED,
            user=user,
            target=user,
            metadata={
                "email": user.email,
                "role": invitation.role,
                "player_id": invitation.player_id,
                "invited_by": invitation.invited_by.email if invitation.invited_by_id else None,
            },
        )
        # AND nothing else: no user_signed_up, no send_registration_received,
        # no notify_new_approval_request (plan: acceptance non-triggers).
        notification_service.send_invitation_completed(user, request=request)
        notify_invitation_completed(user)
        messages.success(request, _("Registration completed — you can now log in."))
        return redirect("account_login")


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
            if {"penalty_notify_mode", "penalty_notify_time"} & set(form.changed_data):
                # Already-queued outbox rows were scheduled under the OLD
                # mode/time — bring them to the new due time. Without this a
                # switch to "Quick (collected)" (or a moved digest time) never
                # touched a row that was already waiting: it kept its old slot
                # (typically tomorrow's digest) and no mail arrived.
                reschedule_pending(request.user)
            log_action(AuditAction.USER_UPDATED, user=request.user, target=request.user)
            messages.success(request, _("Settings saved."))
            response = redirect("accounts:settings")
            # The navbar language switcher is gone for signed-in users, so the
            # Settings page is the ONLY place to change the language. Without
            # refreshing the cookie, a previously set one would keep beating the
            # stored preference (UserLanguageMiddleware precedence).
            response.set_cookie(
                settings.LANGUAGE_COOKIE_NAME,
                form.cleaned_data["preferred_language"],
                max_age=settings.LANGUAGE_COOKIE_AGE,
                samesite="Lax",
            )
            return response
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
