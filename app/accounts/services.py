"""Account services — approval workflow helpers.

No e-mail dispatch here: notifications live exclusively in
``app.notifications.services``.
"""

from allauth.account.models import EmailAddress


def mark_emails_verified(user) -> None:
    """Vouch for a user's address (allauth ``EmailAddress.verified = True``).

    Called when an account becomes trustworthy for login under
    ``ACCOUNT_EMAIL_VERIFICATION = "mandatory"``:

    * admin-created accounts (created as *approved*) — the admin typed the
      address themselves, see the ``post_save`` hook in ``signals.py``,
    * accounts approved through the approval workflow — the approval e-mail
      goes to the very same address.

    Self-registrations stay unverified until the user clicks the link in the
    verification e-mail (or is approved).
    """
    if not user.email:
        return
    EmailAddress.objects.filter(user=user).update(verified=True)
    if not EmailAddress.objects.filter(user=user).exists():
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
