from decimal import Decimal

from django.db import migrations, models
from django.db.models import Sum


def backfill_material_pending(apps, schema_editor):
    Transaction = apps.get_model("core", "Transaction")
    BlocksRecord = apps.get_model("core", "BlocksRecord")
    CementRecord = apps.get_model("core", "CementRecord")
    BambooRecord = apps.get_model("core", "BambooRecord")

    for record in BlocksRecord.objects.filter(record_type="sale"):
        total = record.sale_income or Decimal("0.00")
        paid = Transaction.objects.filter(blocks_record_id=record.id, type="income").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        if paid > total and total > 0:
            paid = total
        record.paid_amount = paid
        record.pending_amount = max(total - paid, Decimal("0.00"))
        record.payment_status = "paid" if total > 0 and record.pending_amount == 0 else "pending"
        record.save(update_fields=["paid_amount", "pending_amount", "payment_status", "updated_at"])

    for record in CementRecord.objects.filter(record_type="sale"):
        total = record.sale_income or Decimal("0.00")
        paid = Transaction.objects.filter(cement_record_id=record.id, type="income").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        if paid > total and total > 0:
            paid = total
        record.paid_amount = paid
        record.pending_amount = max(total - paid, Decimal("0.00"))
        record.payment_status = "paid" if total > 0 and record.pending_amount == 0 else "pending"
        record.save(update_fields=["paid_amount", "pending_amount", "payment_status", "updated_at"])

    for record in BambooRecord.objects.filter(record_type="sale"):
        total = record.sale_income or Decimal("0.00")
        paid = Transaction.objects.filter(bamboo_record_id=record.id, type="income").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        if paid > total and total > 0:
            paid = total
        record.paid_amount = paid
        record.pending_amount = max(total - paid, Decimal("0.00"))
        record.payment_status = "paid" if total > 0 and record.pending_amount == 0 else "pending"
        record.save(update_fields=["paid_amount", "pending_amount", "payment_status", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_alertnotification_bs_due_date_bamboorecord_bs_date_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="bamboorecord",
            name="paid_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="bamboorecord",
            name="pending_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="blocksrecord",
            name="paid_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="blocksrecord",
            name="pending_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="cementrecord",
            name="paid_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="cementrecord",
            name="pending_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.RunPython(backfill_material_pending, migrations.RunPython.noop),
    ]
