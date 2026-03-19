from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="sale",
            name="notes",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="transaction",
            name="sale",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="receipts",
                to="core.sale",
            ),
        ),
    ]
