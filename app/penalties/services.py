"""Penalty service layer — the ONLY place that writes Penalty rows.

All state-changing operations for penalties and their audit entries go through
these functions; views never manipulate ``Penalty`` rows directly.

Money model (B1): every penalty row is a debt row with ``amount_eur > 0``;
a positive balance means the player owes that amount to the club pot.
"""

import logging
import uuid
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from app.core.models import AuditAction
from app.core.permissions import user_can_manage_team
from app.core.services import log_action
from app.matchdays.models import Matchday, MatchdayPlayer
from app.notifications.services import eligible_user_for, flush_outbox, notification_service
from app.penalties.models import Payment, Penalty, PenaltyType
from app.players.models import Player
from app.teams.services import cashier_user_for

logger = logging.getLogger("app.penalties")


def _notify_penalties_created(penalties) -> None:
    """Queue a batch of NEW penalties — at most ONE e-mail per affected player.

    The e-mail is NOT sent here: every row goes into the outbox
    (``NotificationOutbox``) and ``flush_outbox()`` groups everything that is
    due for the same user into one message. That is what turns "5 penalties ->
    5 e-mails" into "5 penalties -> 1 e-mail", both for one save operation and
    for entries made within the coalescing window (quick mode) or before the
    daily digest time.

    Players without a user account (or without a valid/active account) are
    silently skipped — the check itself lives in
    ``app.notifications.services.eligible_user_for``.
    """
    if not penalties:
        return
    notification_service.queue_penalties(penalties)
    flush_outbox()


def _notify_repayment(payment) -> None:
    """E-mail the affected user about a recorded (partial) repayment.

    A repayment is a RECEIPT (one e-mail per recorded payment, never
    collected) and can be switched off individually — Settings → "Payment
    confirmations".
    """
    user = eligible_user_for(payment.player)
    if user is None:
        return
    if not getattr(user, "repayment_notify", True):
        logger.info("Repayment e-mail skipped: %s opted out.", user.email)
        return
    notification_service.send_penalty_repayment(user=user, repayment=payment)


def assert_can_manage_penalty(user, penalty) -> None:
    """Raises PermissionDenied unless the user manages the matchday's team."""
    if (
        user is None
        or not user.is_authenticated
        or not user_can_manage_team(user, penalty.matchday.team)
    ):
        raise PermissionDenied


def record_payment(*, player, team, amount_eur, actor, season=None) -> Payment:
    """Record a (partial) payment from a player towards THIS team's pot.

    Example: 25.00 € owed, 20.00 € paid → the balance drops to 5.00 €.
    Any positive amount is allowed (overpayment → credit / negative balance).
    The payment belongs to ``season`` (the globally active season) — it only
    reduces debt of that same season. One PAYMENT_RECORDED audit entry.

    No payment without a receiver (invariant): the team's current cashier is
    resolved here and stored on ``Payment.received_by`` (PROTECT, snapshot);
    without a usable cashier the recording is refused.
    """
    amount = Decimal(amount_eur)
    if amount <= 0:
        raise ValidationError(_("The amount must be a positive number."))
    received = cashier_user_for(team)
    if received is None:
        raise ValidationError(_("No usable cashier is set for this team yet."))
    with transaction.atomic():
        payment = Payment.objects.create(
            player=player,
            team=team,
            amount_eur=amount,
            created_by=actor,
            received_by=received,
            season=season,
        )
        log_action(
            AuditAction.PAYMENT_RECORDED,
            user=actor,
            target=payment,
            metadata={
                "amount_eur": str(amount),
                "player_id": player.pk,
                "team_id": team.pk,
                "received_by_id": received.pk,
                "received_by_email": received.email,
            },
        )
    _notify_repayment(payment)
    return payment


def delete_payment(*, payment, actor) -> None:
    """Revert a mis-recorded payment — the debt is owed again. Audited."""
    metadata = {
        "amount_eur": str(payment.amount_eur),
        "player_id": payment.player_id,
        "team_id": payment.team_id,
    }
    with transaction.atomic():
        log_action(AuditAction.PAYMENT_DELETED, user=actor, target=payment, metadata=metadata)
        payment.delete()


def _assert_participant(matchday, player) -> None:
    if not MatchdayPlayer.objects.filter(matchday=matchday, player=player).exists():
        raise ValidationError(_("Player is not a participant of this matchday."))


def _assert_double_partner(*, matchday, trigger, partner, catalog_item) -> None:
    """Guard the optional DOUBLES partner (flag + distinct + participant).

    ``partner`` may always be ``None`` — "no doubles partner" is a valid choice.
    """
    if partner is None:
        return
    if not catalog_item.affects_both_players:
        raise ValidationError(_("This catalog item does not apply to both doubles players."))
    if partner.pk == trigger.pk:
        raise ValidationError(_("The doubles partner must be another player."))
    _assert_participant(matchday, partner)


def assign_penalty(
    *,
    matchday,
    player,
    catalog_item,
    amount_eur=None,
    description_snapshot=None,
    double_partner=None,
    actor,
) -> Penalty:
    """Create ONE penalty row (+ a second for a doubles partner) + audit entries.

    NORMAL items always use the effective catalog amount for the matchday's
    team — a passed-in ``amount_eur`` is IGNORED (an individual amount is a
    MANUAL entry). MANUAL items require an individual ``amount_eur`` and a
    non-empty ``description_snapshot`` (the reason/comment).

    With ``double_partner`` (only allowed for catalog items flagged
    ``affects_both_players``) BOTH players are charged the same amount/comment;
    the two rows share one ``group_id`` so edit/soft-delete hit the pair.
    Returns the row of ``player`` (the doubles lead).
    """
    if catalog_item.type not in (PenaltyType.NORMAL, PenaltyType.MANUAL):
        raise ValidationError(_("A normal penalty requires a NORMAL catalog item."))
    _assert_participant(matchday, player)
    _assert_double_partner(
        matchday=matchday, trigger=player, partner=double_partner, catalog_item=catalog_item
    )

    if catalog_item.type == PenaltyType.MANUAL:
        if amount_eur is None:
            raise ValidationError(_("A manual penalty needs an individual amount."))
        amount = Decimal(amount_eur)
        if amount <= 0:
            raise ValidationError(_("The amount must be a positive number."))
        text = (description_snapshot or "").strip()
        if not text:
            raise ValidationError(_("A manual penalty needs a comment."))
    else:
        # Only the catalog (or team-specific) amount may be used.
        amount = catalog_item.amount_for_team(matchday.team)
        text = (description_snapshot or "").strip() or catalog_item.description

    metadata: dict | None = {"manual": True} if catalog_item.type == PenaltyType.MANUAL else None
    if double_partner is not None:
        metadata = dict(metadata or {})
        metadata["double_partner_pk"] = double_partner.pk
    # Both rows of a doubles penalty share ONE group_id: edit and soft delete
    # always act on the pair (same semantics as any other group rows).
    group_id = uuid.uuid4().hex if double_partner is not None else None
    charged = [player] if double_partner is None else [player, double_partner]
    created: list[Penalty] = []
    with transaction.atomic():
        for charged_player in charged:
            penalty = Penalty.objects.create(
                matchday=matchday,
                player=charged_player,
                catalog_item=catalog_item,
                description_snapshot=text,
                amount_eur=amount,
                group_id=group_id,
                created_by=actor,
            )
            created.append(penalty)
            log_action(AuditAction.PENALTY_ASSIGNED, user=actor, target=penalty, metadata=metadata)
    # Notify only after the rows are committed — a rollback must not mail.
    _notify_penalties_created(created)
    return created[0]


def assign_group_penalty(
    *, matchday, trigger_player, catalog_item, double_partner=None, actor
) -> list[Penalty]:
    """Group penalty: one +amount row for EVERY OTHER participant (M4).

    The trigger player (e.g. the 180 thrower) receives no row. With a
    ``double_partner`` (only allowed for catalog items flagged
    ``affects_both_players``) that player is EXCLUDED as well — every remaining
    participant pays. All rows share one ``group_id``; one PENALTY_ASSIGNED
    audit entry per generated row.

    Every generated row names WHO caused the penalty in its
    ``description_snapshot``: the catalog text plus the trigger player and —
    when excluded too — the doubles partner, e.g. ``Highfinish Tag — MP1 & MP2``.
    """
    if catalog_item.type != PenaltyType.PER_ALL_OTHER_MATCHDAY_PLAYERS:
        raise ValidationError(_("A group penalty requires a group catalog item."))
    _assert_participant(matchday, trigger_player)
    _assert_double_partner(
        matchday=matchday,
        trigger=trigger_player,
        partner=double_partner,
        catalog_item=catalog_item,
    )

    total = MatchdayPlayer.objects.filter(matchday=matchday).count()
    excluded = 1 if double_partner is None else 2  # trigger (+ doubles partner)
    if total <= excluded:
        if double_partner is None:
            raise ValidationError(_("A group penalty needs at least two participants."))
        raise ValidationError(
            _("A group penalty with a doubles partner needs at least three participants.")
        )

    other_participants = MatchdayPlayer.objects.filter(matchday=matchday).exclude(
        player=trigger_player
    )
    if double_partner is not None:
        # The doubles partner is excluded TOO — all others receive the penalty.
        other_participants = other_participants.exclude(player=double_partner)
    other_participants = other_participants.select_related("player")
    # Per-team catalog fee: each team may override the default amount.
    amount = catalog_item.amount_for_team(matchday.team)
    group_id = uuid.uuid4().hex
    metadata: dict = {"group_id": group_id, "trigger_player_pk": trigger_player.pk}
    if double_partner is not None:
        metadata["double_partner_pk"] = double_partner.pk
    # Snapshot = catalog text + WHO caused it (thrower, plus the doubles
    # partner when that one is excluded too) — language-neutral, so no extra UI
    # column is needed. The base text is trimmed so the combined value always
    # fits description_snapshot (255 chars) with the names intact.
    causers = " & ".join(
        [trigger_player.name] + ([double_partner.name] if double_partner is not None else [])
    )
    suffix = f" — {causers}"
    description = f"{catalog_item.description[: max(0, 255 - len(suffix))]}{suffix}"
    created: list[Penalty] = []
    with transaction.atomic():
        for participation in other_participants:
            penalty = Penalty.objects.create(
                matchday=matchday,
                player=participation.player,
                catalog_item=catalog_item,
                description_snapshot=description,
                amount_eur=amount,
                group_id=group_id,
                created_by=actor,
            )
            created.append(penalty)
            log_action(
                AuditAction.PENALTY_ASSIGNED,
                user=actor,
                target=penalty,
                metadata=metadata,
            )
    # Notify only after the group is committed — a rollback must not mail.
    _notify_penalties_created(created)
    return created


def _group_rows(penalty) -> list[Penalty]:
    """All non-deleted rows of the group (or just this penalty)."""
    if penalty.group_id:
        return list(Penalty.objects.filter(group_id=penalty.group_id))
    return [penalty]


def edit_penalty(
    *,
    penalty,
    actor,
    catalog_item=None,
    amount_eur=None,
    description_snapshot=None,
) -> Penalty:
    """Update a penalty — and ALL rows sharing its ``group_id`` (M4).

    Writes one PENALTY_EDITED audit entry per affected row.
    """
    group_id = penalty.group_id
    rows = _group_rows(penalty)
    if not rows:
        raise PermissionDenied

    if amount_eur is not None:
        amount = Decimal(amount_eur)
        if amount <= 0:
            raise ValidationError(_("The penalty amount must be positive."))
    else:
        amount = None

    with transaction.atomic():
        for row in rows:
            if amount is not None:
                row.amount_eur = amount
            if description_snapshot is not None:
                row.description_snapshot = description_snapshot
            if catalog_item is not None:
                row.catalog_item = catalog_item
            row.save()
            log_action(
                AuditAction.PENALTY_EDITED,
                user=actor,
                target=row,
                metadata={"group_id": group_id},
            )
    return rows[0]


def soft_delete_penalty(*, penalty, actor, scope="group") -> None:
    """Soft-delete a penalty row — or every row of its group.

    ``scope="group"`` (default) soft-deletes ALL rows sharing the penalty's
    ``group_id``; ``scope="single"`` ONLY the given row. The edit page asks
    captains/admins which of the two applies (see ``PenaltyDeleteView``).
    History and financial aggregates survive (soft delete only); one
    PENALTY_DELETED audit entry (incl. the chosen scope) is written per
    affected row.
    """
    if scope not in ("single", "group"):
        raise ValidationError(_("Invalid delete scope."))
    group_id = penalty.group_id
    rows = [penalty] if scope == "single" else _group_rows(penalty)
    now = timezone.now()
    with transaction.atomic():
        for row in rows:
            row.deleted_at = now
            row.deleted_by = actor
            row.save(update_fields=["deleted_at", "deleted_by"])
            log_action(
                AuditAction.PENALTY_DELETED,
                user=actor,
                target=row,
                metadata={"group_id": group_id, "scope": scope},
            )


def player_balance(player, *, season=None) -> Decimal:
    """Σ penalties − Σ payments across all teams (B1), one season.

    Positive balance = the player still owes this to the club pot;
    negative = they paid more than they owed (credit).
    """
    total = player_total(player, season=season)
    paid = player_total_paid(player, season=season)
    return total - paid


def player_total(player, *, season=None) -> Decimal:
    """Σ penalties (gross, no payments subtracted) across all teams."""
    total = _season_penalty_qs(Penalty.objects.filter(player=player), season).aggregate(
        sum=models.Sum("amount_eur")
    )["sum"]
    return total or Decimal(0)


def player_total_paid(player, *, season=None) -> Decimal:
    """Σ all recorded payments of the player (any team), one season."""
    total = _season_payment_qs(Payment.objects.filter(player=player), season).aggregate(
        sum=models.Sum("amount_eur")
    )["sum"]
    return total or Decimal(0)


def player_team_balance(player, team, *, season=None) -> Decimal:
    """Σ penalties − Σ payments for ONE player scoped to ONE team (M3)."""
    return player_team_total(player, team, season=season) - player_team_paid(
        player, team, season=season
    )


def player_team_total(player, team, *, season=None) -> Decimal:
    """Σ penalties (gross) for ONE player scoped to ONE team and season."""
    total = _season_penalty_qs(
        Penalty.objects.filter(player=player, matchday__team=team), season
    ).aggregate(sum=models.Sum("amount_eur"))["sum"]
    return total or Decimal(0)


def player_team_paid(player, team, *, season=None) -> Decimal:
    """Σ payments of this player for THIS team's pot, one season."""
    total = _season_payment_qs(Payment.objects.filter(player=player, team=team), season).aggregate(
        sum=models.Sum("amount_eur")
    )["sum"]
    return total or Decimal(0)


def team_paid_total(team, *, season=None) -> Decimal:
    """Σ all payments recorded for this team's pot, one season."""
    total = _season_payment_qs(Payment.objects.filter(team=team), season).aggregate(
        sum=models.Sum("amount_eur")
    )["sum"]
    return total or Decimal(0)


def team_penalties_total(team, *, season=None) -> Decimal:
    """Σ penalties (gross) for this team, one season — "Strafe gesamt"."""
    total = _season_penalty_qs(Penalty.objects.filter(matchday__team=team), season).aggregate(
        sum=models.Sum("amount_eur")
    )["sum"]
    return total or Decimal(0)


# ---------------------------------------------------------------------------
# Financial reads (B1/M1/M2/M3)
#
# Positive balance = debt to the club pot. Totals are scoped by
# ``Penalty.matchday.team`` — money belongs to the team whose matchday it was,
# even if the player was later reassigned (M3). All reads exclude soft-deleted
# rows (the default ``PenaltyManager`` already filters them).
# ---------------------------------------------------------------------------
def _season_penalty_qs(qs, season):
    """Restrict penalty rows to one season (None = no season filtering)."""
    if season is None:
        return qs
    return qs.filter(matchday__season=season)


def _season_payment_qs(qs, season):
    """Restrict payment rows to one season (None = no season filtering)."""
    if season is None:
        return qs
    return qs.filter(season=season)


def _scoped_sum_by_player(team, *, season=None) -> dict[int, Decimal]:
    """player_id -> Σ amount_eur (non-deleted) on this team's matchdays."""
    rows = (
        _season_penalty_qs(Penalty.objects.filter(matchday__team=team), season)
        .values("player_id")
        .annotate(total=models.Sum("amount_eur"))
        .values_list("player_id", "total")
    )
    return {player_id: total or Decimal(0) for player_id, total in rows}


def _scoped_payments_by_player(team, *, season=None) -> dict[int, Decimal]:
    """player_id -> Σ recorded payments for THIS team's pot."""
    rows = (
        _season_payment_qs(Payment.objects.filter(team=team), season)
        .values("player_id")
        .annotate(total=models.Sum("amount_eur"))
        .values_list("player_id", "total")
    )
    return {player_id: total or Decimal(0) for player_id, total in rows}


def team_balances(team, *, season=None) -> list:
    """Per-player totals for ACTIVE players, scoped by ``matchday.team`` (M2).

    Includes the team's active players (even with zero balance) plus any
    active player with debts from this team's matchdays (M3). Each returned
    player object carries ``total`` (Σ penalties), ``paid`` (Σ payments) and
    ``balance`` (total − paid = still open) for the given season.
    """
    ids = set(
        # Season-dependent roster: only the players assigned to THIS team in
        # the given season count as active roster members (M2/M3).
        Player.objects.in_team(team, season).filter(active=True).values_list("pk", flat=True)
    )
    ids |= set(
        _season_penalty_qs(Penalty.objects.filter(matchday__team=team), season)
        .filter(player__active=True)
        .values_list("player_id", flat=True)
        .distinct()
    )
    players = list(Player.objects.filter(pk__in=ids).order_by("name"))
    sums = _scoped_sum_by_player(team, season=season)
    payments = _scoped_payments_by_player(team, season=season)
    for player in players:
        player.total = sums.get(player.pk, Decimal(0))
        player.paid = payments.get(player.pk, Decimal(0))
        player.balance = player.total - player.paid
    return players


def inactive_player_balances(team, *, season=None) -> list:
    """Per-player totals for INACTIVE players, scoped by ``matchday.team`` (M2)."""
    ids = set(
        _season_penalty_qs(Penalty.objects.filter(matchday__team=team), season)
        .filter(player__active=False)
        .values_list("player_id", flat=True)
        .distinct()
    )
    players = list(Player.objects.filter(pk__in=ids, active=False).order_by("name"))
    sums = _scoped_sum_by_player(team, season=season)
    payments = _scoped_payments_by_player(team, season=season)
    for player in players:
        player.total = sums.get(player.pk, Decimal(0))
        player.paid = payments.get(player.pk, Decimal(0))
        player.balance = player.total - player.paid
    return players


def season_open_balance(season) -> Decimal:
    """Σ penalties − Σ payments of ONE season; ``0`` = fully settled.

    Used by the season delete guard: a season with open amounts (debt OR
    credit) must never be deleted — the money story has to stay auditable.
    """
    if season is None:
        return Decimal(0)
    penalties = _season_penalty_qs(Penalty.objects.all(), season).aggregate(
        total=models.Sum("amount_eur")
    )["total"] or Decimal(0)
    payments = _season_payment_qs(Payment.objects.all(), season).aggregate(
        total=models.Sum("amount_eur")
    )["total"] or Decimal(0)
    return penalties - payments


def matchday_totals(team, *, season=None) -> list:
    """Per-matchday totals for a team (gross), ordered by date ascending.

    The FIRST matchday (oldest date) is listed on top, the last one at the
    bottom — matching the chronological flow of a season.
    """
    matchday_qs = Matchday.objects.filter(team=team)
    if season is not None:
        matchday_qs = matchday_qs.filter(season=season)
    matchdays = list(matchday_qs.order_by("date", "created_at"))
    rows = (
        _season_penalty_qs(Penalty.objects.filter(matchday__team=team), season)
        .values("matchday_id")
        .annotate(total=models.Sum("amount_eur"))
        .values_list("matchday_id", "total")
    )
    totals = {matchday_id: total or Decimal(0) for matchday_id, total in rows}
    for matchday in matchdays:
        matchday.total = totals.get(matchday.pk, Decimal(0))
    return matchdays


def most_common_penalties(team=None, *, season=None, limit=10) -> list:
    """Group by ``description_snapshot`` (M1); count + Σ amount, count desc."""
    queryset = _season_penalty_qs(Penalty.objects.all(), season)
    if team is not None:
        queryset = queryset.filter(matchday__team=team)
    return list(
        queryset.values("description_snapshot")
        .annotate(count=models.Count("id"), total=models.Sum("amount_eur"))
        .order_by("-count", "description_snapshot")[:limit]
    )


def assigned_penalties_qs(team=None, *, season=None):
    """Annotated listing for the financial screen (season-scoped)."""
    queryset = _season_penalty_qs(
        Penalty.objects.select_related("player", "matchday", "matchday__team", "catalog_item"),
        season,
    ).order_by("-created_at", "-id")
    if team is not None:
        queryset = queryset.filter(matchday__team=team)
    return queryset
