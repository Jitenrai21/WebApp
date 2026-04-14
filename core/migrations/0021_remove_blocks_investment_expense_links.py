from django.db import migrations


def remove_investment_expense_links(apps, schema_editor):
    Transaction = apps.get_model("core", "Transaction")
    Transaction.objects.filter(
        blocks_record__record_type="investment",
        type="expense",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_blocksrecord_investment_type"),
    ]

    operations = [
        migrations.RunPython(remove_investment_expense_links, migrations.RunPython.noop),
    ]
