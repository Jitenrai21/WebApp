from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


INITIAL_TIPPER_ITEMS = [
    "Baluwa - plaster",
    "Baluwa - crusher",
    "Roda",
    "Dhunga",
]


def seed_tipper_items(apps, schema_editor):
    TipperItem = apps.get_model("core", "TipperItem")
    for item_name in INITIAL_TIPPER_ITEMS:
        TipperItem.objects.get_or_create(name=item_name)


def unseed_tipper_items(apps, schema_editor):
    TipperItem = apps.get_model("core", "TipperItem")
    TipperItem.objects.filter(name__in=INITIAL_TIPPER_ITEMS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_transactioncategory_and_migrate_data"),
    ]

    operations = [
        migrations.CreateModel(
            name="TipperItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="TipperRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("date", models.DateField(default=django.utils.timezone.now)),
                (
                    "record_type",
                    models.CharField(
                        choices=[("expense", "Expense"), ("value_added", "Value Added")],
                        max_length=20,
                    ),
                ),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tipper_records",
                        to="core.tipperitem",
                    ),
                ),
            ],
            options={
                "ordering": ["-date", "-created_at"],
                "indexes": [
                    models.Index(fields=["date"], name="core_tipperr_date_idx"),
                    models.Index(fields=["item"], name="core_tipperr_item_idx"),
                    models.Index(fields=["record_type"], name="core_tipperr_type_idx"),
                ],
            },
        ),
        migrations.RunPython(seed_tipper_items, unseed_tipper_items),
    ]
