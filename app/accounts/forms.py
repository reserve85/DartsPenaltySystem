"""Account forms — admin user management + per-user settings.

``is_staff`` is derived server-side from the chosen role (H1): Admin -> True,
Captain/Player -> False. ``team`` is an OPTIONAL captaincy assignment: a
Captain may have none yet, an Admin may additionally captain a team, a Player
never gets one (decoupled from the role — see ``clean`` below).

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
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from app.accounts.models import ApprovalStatus
from app.core.choices import PenaltyNotifyChoice, RoleChoice, ThemeChoice
from app.core.permissions import GROUP_ADMIN, GROUP_CAPTAIN, GROUP_PLAYER
from app.players.models import Player

User = get_user_model()

# Single source of truth: rebuilt from RoleChoice (labels unchanged) so the
# model field choices and every form choice can never drift apart.
ROLE_CHOICES = list(RoleChoice.choices)

# Roles that may hold the optional captaincy (``User.team``) — the edit form
# renders the team dropdown disabled for every other role (see UserUpdateForm).
CAPTAINCY_ROLES = (GROUP_ADMIN, GROUP_CAPTAIN)


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
                if user.approval_status == ApprovalStatus.REQUESTED:
                    # Invitation not completed yet — the account holds no
                    # usable password, but e.g. a password reset by an admin
                    # could get this far; point at the e-mail link instead of
                    # the generic "not approved" text.
                    raise forms.ValidationError(
                        _(
                            "You have been invited but have not completed your registration"
                            " yet. Please use the link in your invitation e-mail."
                        )
                    )
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


def inviteable_players_queryset():
    """Players that are neither linked to a user nor reserved by an OPEN invitation.

    The single-row ``exclude`` keeps both conditions tied to one invitation
    row (reservation = ``accepted_at IS NULL AND expires_at > now``), so a
    player stays selectable while their own invitation is merely expired.
    Refreshed per request in ``InviteCreateForm.__init__`` — like
    ``unlinked_players_queryset``.
    """
    return (
        Player.objects.filter(user_account__isnull=True)
        .exclude(
            invitations__accepted_at__isnull=True,
            invitations__expires_at__gt=timezone.now(),
        )
        .order_by("name")
    )


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
        # The team field is a captaincy assignment, NOT a role requirement:
        # optional for Captain, allowed for Admin, never valid for Player.
        if cleaned.get("role") == GROUP_PLAYER and cleaned.get("team"):
            self.add_error("team", _("A team can only be assigned to Admin or Captain accounts."))
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


class InviteCreateForm(forms.ModelForm):
    """Send an invitation: e-mail + pre-assignment payload, no passwords.

    Mirrors ``UserCreateForm`` minus the password fields — the invitee sets
    their own password on the acceptance page. The ``player`` field is an
    ``Invitation`` field (the model spans two models, hence no ``CreateView``)
    and only offers players that are neither linked nor reserved by another
    OPEN invitation.
    """

    role = forms.ChoiceField(choices=ROLE_CHOICES, label=_("Role"))
    player = forms.ModelChoiceField(
        queryset=inviteable_players_queryset(),
        required=False,
        label=_("Linked player"),
        help_text=_(
            "Optional: pre-assigned player — linked to the account when the invitation is accepted."
        ),
    )

    class Meta:
        model = User
        fields: ClassVar[list] = ["email", "role", "team", "preferred_language"]
        widgets: ClassVar[dict] = {
            "email": forms.EmailInput(attrs={"autocomplete": "email"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Refresh per request — players may have been linked since import time.
        self.fields["player"].queryset = inviteable_players_queryset()
        # Same captaincy toggle contract as UserUpdateForm: the marker lets
        # user_form.html enable the dropdown for Admin/Captain only; the
        # state lives on the WIDGET (field stays editable for crafted POSTs,
        # clean() enforces the rule server-side).
        effective_role = self.data.get("role") if self.is_bound else self.fields["role"].initial
        attrs = self.fields["team"].widget.attrs
        attrs["data-team-roles"] = ",".join(CAPTAINCY_ROLES)
        if effective_role not in CAPTAINCY_ROLES:
            attrs["disabled"] = True

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        existing = User.objects.filter(email__iexact=email).first()
        if existing is not None:
            if existing.approval_status == ApprovalStatus.REQUESTED:
                raise forms.ValidationError(
                    _("Already invited — cancel or resend the existing invitation.")
                )
            raise forms.ValidationError(_("A user with this email address already exists."))
        return email

    def clean(self):
        cleaned = super().clean()
        # The team is a captaincy: optional for Captain, allowed for Admin,
        # never valid for Player (same rule as UserCreateForm).
        if cleaned.get("role") == GROUP_PLAYER and cleaned.get("team"):
            self.add_error("team", _("A team can only be assigned to Admin or Captain accounts."))
        # Server-side reservation guard (the field queryset already enforces
        # it; this keeps the intent explicit if the queryset is ever relaxed).
        player = cleaned.get("player")
        if player is not None and not inviteable_players_queryset().filter(pk=player.pk).exists():
            self.add_error(
                "player",
                _("This player is already reserved by another invitation."),
            )
        return cleaned


class InvitationAcceptForm(forms.Form):
    """Public set-password form of the acceptance link (no username, no e-mail —
    the token fixes the account). Password handling mirrors ``UserCreateForm``:
    match check in ``clean()``, Django's ``validate_password`` in ``_post_clean``.
    """

    password1 = forms.CharField(label=_("Password"), widget=forms.PasswordInput)
    password2 = forms.CharField(label=_("Password confirmation"), widget=forms.PasswordInput)

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", _("The two password fields didn't match."))
        return cleaned

    def _post_clean(self):
        super()._post_clean()
        password = self.cleaned_data.get("password1")
        if password:
            from django.contrib.auth.password_validation import validate_password

            try:
                validate_password(password)
            except forms.ValidationError as error:
                self.add_error("password1", error)


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
        # The team field IS a captaincy — label it accordingly on the edit form.
        self.fields["team"].label = _("Captain in this team")
        if self.instance.pk:
            role = None
            if self.instance.is_admin:
                role = GROUP_ADMIN
            elif self.instance.is_captain:
                role = GROUP_CAPTAIN
            elif self.instance.is_player:
                role = GROUP_PLAYER
            self.fields["role"].initial = role
        # The dropdown is only usable for Captain/Admin accounts (a Player never
        # holds a team). The state is set on the WIDGET only — ``field.disabled``
        # stays False so a crafted POST is still validated against the team/role
        # rule in ``clean()``. ``data-team-roles`` lets user_form.html toggle the
        # dropdown live when the role select changes.
        effective_role = self.data.get("role") if self.is_bound else self.fields["role"].initial
        attrs = self.fields["team"].widget.attrs
        attrs["data-team-roles"] = ",".join(CAPTAINCY_ROLES)
        if effective_role not in CAPTAINCY_ROLES:
            attrs["disabled"] = True

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        duplicate = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists()
        if duplicate:
            raise forms.ValidationError(_("A user with this email address already exists."))
        return email

    def clean(self):
        cleaned = super().clean()
        # Same rule as UserCreateForm: the team is an optional captaincy for
        # Captain/Admin accounts and invalid for the Player role.
        if cleaned.get("role") == GROUP_PLAYER and cleaned.get("team"):
            self.add_error("team", _("A team can only be assigned to Admin or Captain accounts."))
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
    """Language, theme AND the per-user notification preferences.

    The notification block mirrors the mail types a user may switch off:

    * ``penalty_notify_mode``/``penalty_notify_time`` — Off / Quick (collected
      after the coalescing window) / Daily digest at the chosen time,
    * ``repayment_notify``    — a receipt per recorded payment,
    * ``club_news_optin``     — round mails / club information.

    The two booleans are rendered as OFF/subscribe radio groups, so an
    unchecked box is never a hidden "No" — the choice is always visible.
    Transactional mails (invitation, approval, password reset, e-mail
    verification) are deliberately NOT part of this form: they always go out.
    """

    penalty_notify_time = forms.TimeField(
        label=_("Digest time"),
        help_text=_("Only used by the daily digest."),
        input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(attrs={"type": "time", "step": "300"}, format="%H:%M"),
    )
    repayment_notify = forms.BooleanField(
        required=False,
        label=_("Payment confirmations"),
        widget=forms.RadioSelect(
            choices=((True, _("Abonnieren")), (False, _("Off"))),
        ),
    )
    club_news_optin = forms.BooleanField(
        required=False,
        label=_("Club news / round mails"),
        widget=forms.RadioSelect(
            choices=((True, _("Abonnieren")), (False, _("Off"))),
        ),
    )

    class Meta:
        model = User
        fields: ClassVar[list] = [
            "preferred_language",
            "preferred_theme",
            "penalty_notify_mode",
            "penalty_notify_time",
            "repayment_notify",
            "club_news_optin",
        ]
        widgets: ClassVar[dict] = {
            "preferred_language": forms.Select(choices=settings.LANGUAGES),
            "preferred_theme": forms.Select(choices=ThemeChoice.choices),
            "penalty_notify_mode": forms.RadioSelect,
        }

    def clean(self):
        """A digest without a time is a contradiction — say so, don't guess."""
        cleaned = super().clean()
        mode = cleaned.get("penalty_notify_mode")
        if mode == PenaltyNotifyChoice.DAILY and not cleaned.get("penalty_notify_time"):
            self.add_error(
                "penalty_notify_time",
                _("Choose the time of day for the daily digest."),
            )
        return cleaned
