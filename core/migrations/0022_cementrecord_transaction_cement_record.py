from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_remove_blocks_investment_expense_links"),
    ]

    operations = [
        migrations.CreateModel(
            name="CementRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("date", models.DateField(default=django.utils.timezone.now)),
                (
                    "record_type",
                    models.CharField(
                        choices=[
                            ("investment", "Investment"),
                            ("stock", "Stock (Addition)"),
                            ("sale", "Sale"),
                        ],
                        help_text="Type of record: investment, stock inventory update, or sale",
                        max_length=20,
                    ),
                ),
                (
                    "investment",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Amount spent on cement procurement or production",
                        max_digits=14,
                        null=True,
                    ),
                ),
                (
                    "sale_income",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Auto-calculated: quantity × price (for sale records)",
                        max_digits=14,
                        null=True,
                    ),
                ),
                (
                    "unit_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("ppc", "PPC"),
                            ("opc", "OPC"),
                        ],
                        help_text="Type of cement unit: PPC or OPC",
                        max_length=20,
                        null=True,
                    ),
                ),
                (
                    "quantity",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="Number of units (for stock addition or sale)",
                        null=True,
                    ),
                ),
                (
                    "price_per_unit",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Price per unit (for sale records)",
                        max_digits=12,
                        null=True,
                    ),
                ),
                (
                    "notes",
                    models.TextField(blank=True, help_text="Additional details or remarks"),
                ),
            ],
            options={
                "ordering": ["-date", "-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="cementrecord",
            index=models.Index(fields=["date"], name="core_cement_date_4c3f77_idx"),
        ),
        migrations.AddIndex(
            model_name="cementrecord",
            index=models.Index(fields=["record_type"], name="core_cement_record_3de1e0_idx"),
        ),
        migrations.AddIndex(
            model_name="cementrecord",
            index=models.Index(fields=["unit_type"], name="core_cement_unit_t_8c7fb6_idx"),
        ),
        migrations.AddField(
            model_name="transaction",
            name="cement_record",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="transactions",
                to="core.cementrecord",
            ),
        ),
    ]