"""Notification center — mailbox tabs/actions + the money event families.

Covers the rebuilt list (Unread/All tabs, mark read/unread with ``next``,
delete one / delete read) and the in-app event families: new penalties,
recorded payments (both channels), approval decisions, reverted payments and
changed/deleted penalties — always language-tolerant (the local ``.mo`` is
stale for every new msgid).
"""
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.urls import reverse
from notifications.models import Notification
from notifications.signals import notify

from app.penalties.services import (
    assign_group_penalty,
    assign_penalty,
    delete_payment,
    edit_penalty,
    record_payment,
    soft_delete_penalty,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


def _make(user, *, verb="Ping", level="info"):
    """One in-app row via the production signal (actor = the recipient)."""
    notify.send(sender=user, recipient=user, verb=verb, level=level)
    return Notification.objects.get(recipient=user, verb=verb)


def _target_rows(obj):
    """Rows whose GenericForeignKey ``target`` points at ``obj``.

    ``Notification.objects.filter(target=…)`` is impossible (GFKs have no
    reverse relation) — filter on the concrete content-type columns instead.
    """
    from django.contrib.contenttypes.models import ContentType

    return Notification.objects.filter(
        target_content_type=ContentType.objects.get_for_model(obj),
        target_object_id=obj.pk,
    )


def _link_payer(player, **kwargs):
    """Active account for ``player`` (in-app rule — no e-mail requirement)."""
    kwargs.setdefault("approval_status", "approved")
    kwargs.setdefault("email", f"payer{player.pk}@example.com")
    user = User.objects.create_user(password="pw", **kwargs)
    user.player_link = player
    user.save(update_fields=["player_link"])
    return user


# ---------------------------------------------------------------------------
# 1. Bell for every signed-in role (badge from unread_notification_count)
# ---------------------------------------------------------------------------
def test_bell_badge_for_captain(captain_client, captain_user):
    _make(captain_user)
    _make(captain_user, verb="Second")
    content = captain_client.get(reverse("dashboard:index")).content.decode()
    assert reverse("notifications:unread") in content
    assert '<span class="badge text-bg-warning">2</span>' in content


def test_bell_badge_for_player(player_client, player_user):
    _make(player_user)
    content = player_client.get(reverse("dashboard:index")).content.decode()
    assert reverse("notifications:unread") in content
    assert '<span class="badge text-bg-warning">1</span>' in content


def test_bell_badge_for_roleless_account(db, role_groups, team):
    user = User.objects.create_user(
        email="roleless@example.com", password="pw", approval_status="approved"
    )
    _make(user)
    client = Client()
    client.force_login(user)
    content = client.get(reverse("dashboard:index")).content.decode()
    assert reverse("notifications:unread") in content
    assert '<span class="badge text-bg-warning">1</span>' in content


def test_zero_badge_renders_without_span(admin_client, admin_user):
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    assert '<span class="badge text-bg-warning">0</span>' not in content


# ---------------------------------------------------------------------------
# 2./3. Tabs + mark read/unread links (with ?next= back to the same tab)
# ---------------------------------------------------------------------------
def test_tabs_render_and_split_read_rows(admin_client, admin_user):
    _make(admin_user, verb="Still unread bell")
    read_row = _make(admin_user, verb="Already read bell")
    Notification.objects.filter(pk=read_row.pk).update(unread=False)

    unread_html = admin_client.get(reverse("notifications:unread")).content.decode()
    all_html = admin_client.get(reverse("notifications:all")).content.decode()

    assert "Still unread bell" in unread_html
    assert "Already read bell" not in unread_html
    assert "Still unread bell" in all_html
    assert "Already read bell" in all_html
    # active tab highlighting
    assert 'nav-link active' in unread_html and 'nav-link active' in all_html


def test_mark_as_read_link_carries_next_and_returns_to_unread_tab(admin_client, admin_user):
    row = _make(admin_user, verb="Clickable")
    list_url = reverse("notifications:unread")
    content = admin_client.get(list_url).content.decode()
    mark_url = reverse("notifications:mark_as_read", args=[row.slug])
    assert f"{mark_url}?next=" in content

    response = admin_client.get(f"{mark_url}?next={list_url}")
    assert response.status_code == 302
    assert response.url == list_url
    row.refresh_from_db()
    assert row.unread is False




# ---------------------------------------------------------------------------
# 4. delete one (own rows only; GET 405; anonymous -> login)
# ---------------------------------------------------------------------------
def test_delete_one_removes_only_the_callers_row(admin_client, admin_user, captain_user):
    mine = _make(admin_user, verb="My row")
    theirs = _make(captain_user, verb="Foreign row")
    next_url = reverse("notifications:unread")

    response = admin_client.post(
        reverse("notifications_manage:delete_one", args=[mine.pk]), {"next": next_url}
    )
    assert response.status_code == 302
    assert response.url == next_url
    assert not Notification.objects.filter(pk=mine.pk).exists()
    assert Notification.objects.filter(pk=theirs.pk).exists()


def test_delete_one_rejects_a_foreign_row(admin_client, captain_user):
    theirs = _make(captain_user, verb="Foreign row")
    response = admin_client.post(
        reverse("notifications_manage:delete_one", args=[theirs.pk])
    )
    assert response.status_code == 404
    assert Notification.objects.filter(pk=theirs.pk).exists()


def test_delete_one_get_is_405(admin_client, admin_user):
    row = _make(admin_user)
    response = admin_client.get(reverse("notifications_manage:delete_one", args=[row.pk]))
    assert response.status_code == 405
    assert Notification.objects.filter(pk=row.pk).exists()


def test_delete_one_anonymous_redirects_to_login(db):
    user = User.objects.create_user(email="anon-target@example.com", password="pw")
    row = _make(user)
    response = Client().post(reverse("notifications_manage:delete_one", args=[row.pk]))
    assert response.status_code == 302
    assert "login" in response.url
    assert Notification.objects.filter(pk=row.pk).exists()


def test_delete_one_rejects_unsafe_next(admin_client, admin_user):
    row = _make(admin_user)
    response = admin_client.post(
        reverse("notifications_manage:delete_one", args=[row.pk]),
        {"next": "https://evil.example.com/phish"},
    )
    assert response.status_code == 302
    # falls back to the mailbox instead of redirecting off-site
    assert response.url == reverse("notifications:all")
    assert not Notification.objects.filter(pk=row.pk).exists()


# ---------------------------------------------------------------------------
# 5. delete read (caller's read rows only)
# ---------------------------------------------------------------------------
def test_delete_read_keeps_unread_and_foreign_rows(admin_client, admin_user, captain_user):
    read_row = _make(admin_user, verb="Read of mine")
    Notification.objects.filter(pk=read_row.pk).update(unread=False)
    unread_row = _make(admin_user, verb="Unread of mine")
    foreign_read = _make(captain_user, verb="Read of the other")
    Notification.objects.filter(pk=foreign_read.pk).update(unread=False)
    next_url = reverse("notifications:unread")

    response = admin_client.post(
        reverse("notifications_manage:delete_read"), {"next": next_url}
    )
    assert response.status_code == 302
    assert response.url == next_url
    assert not Notification.objects.filter(pk=read_row.pk).exists()
    assert Notification.objects.filter(pk=unread_row.pk).exists()
    assert Notification.objects.filter(pk=foreign_read.pk).exists()


def test_delete_read_get_is_405(admin_client):
    assert admin_client.get(reverse("notifications_manage:delete_read")).status_code == 405


def test_mark_as_unread_link_carries_next(admin_client, admin_user):
    row = _make(admin_user, verb="Read it")
    Notification.objects.filter(pk=row.pk).update(unread=False)
    list_url = reverse("notifications:all")
    content = admin_client.get(list_url).content.decode()
    mark_url = reverse("notifications:mark_as_unread", args=[row.slug])
    assert f"{mark_url}?next=" in content

    response = admin_client.get(f"{mark_url}?next={list_url}")
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.unread is True


# ---------------------------------------------------------------------------
# 6. In-app event families — new penalty / recorded payment (both channels)
# ---------------------------------------------------------------------------
def test_new_penalty_creates_warning_row_for_the_player(
    matchday_with_players, catalog_normal, admin_user, team
):
    md, players = matchday_with_players(2)
    payer = _link_payer(players[0])

    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )

    row = Notification.objects.get(recipient=payer)
    assert row.level == "warning"
    assert row.target == penalty
    assert "5.00" in row.verb  # language-tolerant: amount is the stable part
    assert "New penalty" in row.verb or "Neue Strafe" in row.verb


def test_new_penalty_needs_no_e_mail_address(
    matchday_with_players, catalog_normal, admin_user
):
    """In-app rule: an active account WITHOUT an e-mail still gets a row."""
    md, players = matchday_with_players(2)
    payer = User.objects.create_user(
        email=f"temporary{players[0].pk}@example.com",
        password="pw",
        approval_status="approved",
    )
    # The manager enforces a non-empty address; the MODEL does not — clear it
    # the way a legacy account without a mail server would look.
    User.objects.filter(pk=payer.pk).update(email="")
    payer.refresh_from_db()
    payer.player_link = players[0]
    payer.save(update_fields=["player_link"])

    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    assert Notification.objects.filter(recipient=payer).exists()


def test_one_payment_yields_two_mails_and_two_in_app_rows(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    payer = _link_payer(players[0])
    mail.outbox.clear()

    payment = record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)

    assert len(mail.outbox) == 2  # payer + cashier
    rows = _target_rows(payment)
    assert rows.count() == 2
    assert {row.recipient_id for row in rows} == {payer.pk, admin_user.pk}
    assert all(row.level == "success" for row in rows)


def test_in_app_rows_survive_mail_opt_out(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    """Decision 5: in-app is additive and NOT switchable off."""
    md, players = matchday_with_players(2)
    payer = _link_payer(players[0])
    payer.repayment_notify = False
    payer.penalty_notify_mode = "off"
    payer.save(update_fields=["repayment_notify", "penalty_notify_mode"])
    mail.outbox.clear()

    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    payment = record_payment(player=players[0], team=md.team, amount_eur=5, actor=admin_user)

    # Penalty: no mail at all (mode "off"), but the in-app row exists.
    assert _target_rows(penalty).filter(recipient=payer).exists()
    # Payment: the payer opted out of their confirmation mail…
    assert all(m.to != [payer.email] for m in mail.outbox)
    # …but the in-app rows for payer AND cashier are still created.
    assert _target_rows(payment).filter(recipient=payer).exists()
    assert _target_rows(payment).filter(recipient=admin_user).exists()


# ---------------------------------------------------------------------------
# 9. Revert / edit / delete events (decision 10) — in-app only, no e-mail
# ---------------------------------------------------------------------------
def test_revert_leaves_two_rows_and_no_new_mail(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    payer = _link_payer(players[0])
    payment = record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)
    mail.outbox.clear()
    before = Notification.objects.count()

    delete_payment(payment=payment, actor=admin_user)

    assert len(mail.outbox) == 0  # "no new e-mail" decision — locked
    fresh = list(Notification.objects.order_by("-pk")[:2])
    assert len(fresh) == 2
    assert {row.recipient_id for row in fresh} == {payer.pk, admin_user.pk}
    assert all(row.level == "warning" for row in fresh)
    assert all(row.target is None for row in fresh)  # row is gone — no GFK
    assert Notification.objects.count() == before + 2


def test_revert_deduplicates_when_payer_is_the_cashier(
    matchday_with_players, catalog_normal, admin_user, cashier
):
    md, players = matchday_with_players(2)
    # Same account as the cashier: the payer IS the receiving cashier.
    admin_user.player_link = players[0]
    admin_user.save(update_fields=["player_link"])
    payment = record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)
    before = Notification.objects.count()

    delete_payment(payment=payment, actor=admin_user)

    assert Notification.objects.count() == before + 1  # ONE row, not two


def test_edit_penalty_notifies_with_the_new_amount(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    payer = _link_payer(players[0])
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    before = Notification.objects.count()

    edit_penalty(penalty=penalty, actor=admin_user, amount_eur=Decimal("7.50"))

    fresh = Notification.objects.order_by("-pk").first()
    assert fresh.recipient == payer
    assert fresh.target == penalty
    assert "7.50" in fresh.verb  # NEW amount, language-tolerant
    assert Notification.objects.count() == before + 1



def test_group_edit_notifies_one_row_per_affected_player(
    matchday_with_players, catalog_group, admin_user
):
    md, players = matchday_with_players(4)
    linked = [_link_payer(p) for p in players[:3]]  # every affected player has an account
    rows = assign_group_penalty(
        matchday=md,
        trigger_player=players[3],
        catalog_item=catalog_group,
        actor=admin_user,
    )
    assert len(rows) >= 2
    before = Notification.objects.count()

    edit_penalty(penalty=rows[0], actor=admin_user, amount_eur=Decimal("2.00"))

    assert Notification.objects.count() == before + len(rows)
    fresh = list(Notification.objects.order_by("-pk")[: len(rows)])
    # every affected player got THEIR OWN row (each targets their own penalty)
    assert {row.recipient_id for row in fresh} == {user.pk for user in linked[: len(rows)]}


def test_soft_delete_single_notifies_exactly_one_row(
    matchday_with_players, catalog_normal, admin_user
):
    md, players = matchday_with_players(2)
    _link_payer(players[0])
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    before = Notification.objects.count()

    soft_delete_penalty(penalty=penalty, actor=admin_user, scope="single")

    assert Notification.objects.count() == before + 1


def test_soft_delete_group_notifies_one_row_per_player(
    matchday_with_players, catalog_group, admin_user
):
    md, players = matchday_with_players(4)
    for player in players[:3]:
        _link_payer(player)
    rows = assign_group_penalty(
        matchday=md,
        trigger_player=players[3],
        catalog_item=catalog_group,
        actor=admin_user,
    )
    before = Notification.objects.count()

    soft_delete_penalty(penalty=rows[0], actor=admin_user, scope="group")

    assert Notification.objects.count() == before + len(rows)
    fresh = list(Notification.objects.order_by("-pk")[: len(rows)])
    assert all(row.target is not None for row in fresh)  # soft-deleted row resolves


def test_changed_penalty_without_account_is_skipped_silently(
    matchday_with_players, catalog_normal, admin_user
):
    """No account -> no row AND no exception (skip path)."""
    md, players = matchday_with_players(2)  # players[0] has NO linked account
    penalty = assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    before = Notification.objects.count()

    edit_penalty(penalty=penalty, actor=admin_user, amount_eur=Decimal("9.00"))
    soft_delete_penalty(penalty=penalty, actor=admin_user, scope="single")

    assert Notification.objects.count() == before  # nothing created, nothing raised


# ---------------------------------------------------------------------------
# 10. Snapshot semantics (review L5a)
# ---------------------------------------------------------------------------
def test_received_by_survives_a_cashier_change(
    matchday_with_players, catalog_normal, admin_user, captain_user, team
):
    from app.core.models import AuditAction, AuditLog
    from app.teams.services import set_cashier

    set_cashier(team=team, user=admin_user, actor=admin_user)
    md, players = matchday_with_players(2)
    payment = record_payment(player=players[0], team=md.team, amount_eur=3, actor=admin_user)

    set_cashier(team=team, user=captain_user, actor=admin_user)  # cashier changes…

    payment.refresh_from_db()
    assert payment.received_by == admin_user  # …the payment keeps its receiver
    entry = AuditLog.objects.get(action=AuditAction.PAYMENT_RECORDED, target_id=payment.pk)
    assert entry.metadata["received_by_id"] == admin_user.pk
    assert entry.metadata["received_by_email"] == admin_user.email



# ---------------------------------------------------------------------------
# 8. Live pending-approvals badge (admin-only, count from User rows)
# ---------------------------------------------------------------------------
def test_live_pending_approval_badge(admin_client, captain_client, admin_user):
    User.objects.create_user(email="pending1@example.com", password="pw")
    User.objects.create_user(email="pending2@example.com", password="pw")
    danger = "badge text-bg-danger"

    admin_html = admin_client.get(reverse("dashboard:index")).content.decode()
    assert f'<span class="{danger}">2</span>' in admin_html
    assert reverse("accounts:approval_list") in admin_html

    # Never for non-admins…
    captain_html = captain_client.get(reverse("dashboard:index")).content.decode()
    assert danger not in captain_html

    # …and independent of the mailbox state (rows can be read/deleted freely).
    Notification.objects.all().delete()
    admin_html = admin_client.get(reverse("dashboard:index")).content.decode()
    assert f'<span class="{danger}">2</span>' in admin_html

    # An approval drops the badge without relying on notification rows.
    first = User.objects.get(email="pending1@example.com")
    response = admin_client.post(
        reverse("accounts:approval", args=[first.pk]), {"action": "approve"}
    )
    assert response.status_code == 302
    admin_html = admin_client.get(reverse("dashboard:index")).content.decode()
    assert f'<span class="{danger}">1</span>' in admin_html
    assert f'<span class="{danger}">2</span>' not in admin_html

