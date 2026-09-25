"""Convert legacy per-row payments (Penalty.paid_at) into Payment rows.

Balances become ``Σ penalties − Σ payments``; without this step every
already-paid penalty would count as debt again. Soft-deleted rows are
skipped: in the old model they contributed nothing to any balance.
"""

from typing import ClassVar

from django.db import migrations


def paid_penalties_to_payments(apps, schema_editor):
    Penalty = apps.get_model("penalties", "Penalty")
    Payment = apps.get_model("penalties", "Payment")
    paid = Penalty.objects.filter(paid_at__isnull=False, deleted_at__isnull=True).select_related(
        "matchday"
    )
    for penalty in paid:
        payment = Payment.objects.create(
            player_id=penalty.player_id,
            team_id=penalty.matchday.team_id,
            amount_eur=penalty.amount_eur,
            created_by_id=penalty.paid_by_id,
        )
        # Preserve the original date (created_at is auto_now_add on create).
        Payment.objects.filter(pk=payment.pk).update(created_at=penalty.paid_at)


def payments_stay_payments(apps, schema_editor):
    """No-op: Payment rows remain the single source of payment truth."""


class Migration(migrations.Migration):
    dependencies: ClassVar[list] = [
        ("penalties", "0003_alter_penaltycatalogitem_type_payment"),
    ]

    operations: ClassVar[list] = [
        migrations.RunPython(paid_penalties_to_payments, payments_stay_payments),
    ]
