"""Account forms — admin user management + per-user settings.

``is_staff`` is derived server-side from the chosen role (H1): Admin -> True,
Captain/Player -> False. Role ``Captain`` requires ``team``.

Also home of the allauth login gate (``ACCOUNT_FORMS = {"login": …}``):
``ApprovalLoginForm`` blocks pending/rejected accounts with a clear message.
"""

from typing import ClassVar

from allauth.account.forms import LoginForm as AllauthLoginForm
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from app.accounts.models import ApprovalStatus
from app.core.choices import ThemeChoice
from app.core.permissions import GROUP_ADMIN, GROUP_CAPTAIN, GROUP_PLAYER
from app.players.models import Player

User = get_user_model()

ROLE_CHOICES = [
    (GROUP_ADMIN, _("Admin")),
    (GROUP_CAPTAIN, _("Captain")),
    (GROUP_PLAYER, _("Player")),
]


class ApprovalLoginForm(AllauthLoginForm):
    """Login only for admin-approved accounts (django-allauth login form).

    Wired via ``ACCOUNT_FORMS`` in settings; pending and rejected users get a
    clear error message instead of a generic "wrong password".
    """

    def clean(self):
        cleaned_data = super().clean()
        if not self._errors:
            user = getattr(self, "user", None)
            if user is not None and not user.is_approved:
                if user.approval_status == ApprovalStatus.REJECTED:
                    raise forms.ValidationError(
                        _("Your registration has been declined. Please contact the club.")
                    )
                raise forms.ValidationError(
                    _(
                        "Your account has not been approved by an administrator yet. "
                        "Please contact the club."
                    )
                )
        return cleaned_data


def unlinked_players_queryset(user=None):
    """Players that are NOT linked to a user account (the link is 1:1).

    When editing ``user``, that user's own current link stays selectable —
    every other already-linked player is hidden.
    """
    queryset = Player.objects.filter(user_account__isnull=True)
    if user is not None and user.pk:
        queryset = Player.objects.filter(Q(user_account__isnull=True) | Q(user_account=user))
    return queryset.order_by("name")


class UserCreateForm(forms.ModelForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES, label=_("Role"))
    password1 = forms.CharField(label=_("Password"), widget=forms.PasswordInput)
    password2 = forms.CharField(label=_("Password confirmation"), widget=forms.PasswordInput)

    class Meta:
        model = User
        fields: ClassVar[list] = [
            "email",
            "role",
            "team",
            "player_link",
            "is_active",
            "preferred_language",
        ]
        widgets: ClassVar[dict] = {
            "email": forms.EmailInput(attrs={"autocomplete": "email"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only NOT-yet-linked players may be picked for a NEW user.
        self.fields["player_link"].queryset = unlinked_players_queryset()

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("A user with this email address already exists."))
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("role") == GROUP_CAPTAIN and not cleaned.get("team"):
            self.add_error("team", _("A Captain needs an assigned team."))
        return cleaned

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError(_("The two password fields didn't match."))
        return password2

    def _post_clean(self):
        super()._post_clean()
        password = self.cleaned_data.get("password2")
        if password:
            from django.contrib.auth.password_validation import validate_password

            try:
                validate_password(password, self.instance)
            except forms.ValidationError as error:
                self.add_error("password2", error)

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password2"])
        user.is_staff = self.cleaned_data["role"] == GROUP_ADMIN
        # Accounts created by an admin are usable right away — the approval
        # workflow only applies to self-registration.
        user.approval_status = ApprovalStatus.APPROVED
        if commit:
            user.save()
            role_group = Group.objects.get(name=self.cleaned_data["role"])
            user.groups.set([role_group])
        return user


class UserApprovalForm(forms.Form):
    """Single-save approval: approve the user AND link (or create) a player.

    One POST performs the whole review step — the admin neither switches
    pages nor saves twice.
    """

    player = forms.ModelChoiceField(
        queryset=unlinked_players_queryset(),
        required=False,
        label=_("Existing player"),
        help_text=_("Optional: link this account to an existing player."),
    )
    new_player_name = forms.CharField(
        max_length=120,
        required=False,
        label=_("New player"),
        help_text=_("Optional: create a new player and link it immediately."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Refresh per request — players may have been linked since import time.
        self.fields["player"].queryset = unlinked_players_queryset()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("player") and (cleaned.get("new_player_name") or "").strip():
            self.add_error(
                "new_player_name",
                _("Choose an existing player OR enter a new player name — not both."),
            )
        return cleaned


class UserUpdateForm(forms.ModelForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES, label=_("Role"))

    class Meta:
        model = User
        fields: ClassVar[list] = [
            "email",
            "role",
            "team",
            "player_link",
            "is_active",
            "preferred_language",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only NOT-yet-linked players — plus this user's own current link.
        self.fields["player_link"].queryset = unlinked_players_queryset(self.instance)
        if self.instance.pk:
            role = None
            if self.instance.is_admin:
                role = GROUP_ADMIN
            elif self.instance.is_captain:
                role = GROUP_CAPTAIN
            elif self.instance.is_player:
                role = GROUP_PLAYER
            self.fields["role"].initial = role

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        duplicate = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists()
        if duplicate:
            raise forms.ValidationError(_("A user with this email address already exists."))
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("role") == GROUP_CAPTAIN and not cleaned.get("team"):
            self.add_error("team", _("A Captain needs an assigned team."))
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = self.cleaned_data["role"] == GROUP_ADMIN
        if commit:
            user.save()
            if user.pk:
                role_group = Group.objects.get(name=self.cleaned_data["role"])
                user.groups.set([role_group])
        return user


class SettingsForm(forms.ModelForm):
    """Preferred language + theme (password handled by PasswordChangeForm)."""

    class Meta:
        model = User
        fields: ClassVar[list] = ["preferred_language", "preferred_theme"]
        widgets: ClassVar[dict] = {
            "preferred_language": forms.Select(choices=settings.LANGUAGES),
            "preferred_theme": forms.Select(choices=ThemeChoice.choices),
        }
