from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_sale_notes_transaction_sale"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="due_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="AlertNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "alert_type",
                    models.CharField(
                        choices=[("overdue", "Overdue"), ("upcoming", "Upcoming")],
                        max_length=20,
                    ),
                ),
                (
                    "source_type",
                    models.CharField(
                        choices=[("sale", "Sale"), ("transaction", "Transaction")],
                        max_length=20,
                    ),
                ),
                ("source_id", models.PositiveIntegerField()),
                ("due_date", models.DateField()),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("title", models.CharField(max_length=180)),
                ("message", models.TextField(blank=True)),
                ("is_read", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="alert_notifications",
                        to="core.customer",
                    ),
                ),
            ],
            options={
                "ordering": ["-due_date", "-created_at"],
                "unique_together": {("alert_type", "source_type", "source_id", "due_date")},
            },
        ),
        migrations.AddIndex(
            model_name="alertnotification",
            index=models.Index(fields=["is_active", "is_read"], name="core_alertno_is_acti_e80787_idx"),
        ),
        migrations.AddIndex(
            model_name="alertnotification",
            index=models.Index(fields=["source_type", "source_id"], name="core_alertno_source__de4e60_idx"),
        ),
        migrations.AddIndex(
            model_name="alertnotification",
            index=models.Index(fields=["due_date"], name="core_alertno_due_dat_a0242f_idx"),
        ),
    ]
