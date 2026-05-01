from django.core.management.base import BaseCommand

from core.models import RecordStatus, Sale, Transaction, TransactionCategory, TransactionType


AUTO_SALE_INCOME_CATEGORY = "Sale Income (Auto)"
AUTO_SALE_INCOME_DESCRIPTION = "Auto-linked from paid sale"


def _get_or_create_predefined_category(name):
    category, _ = TransactionCategory.objects.get_or_create(
        name=name,
        defaults={"is_predefined": True},
    )
    return category


class Command(BaseCommand):
    help = "Backfill and sync auto income Finance Ledger entries for paid sales."

    def handle(self, *args, **options):
        created = 0
        updated = 0
        deleted = 0
        skipped = 0
        auto_sale_category = _get_or_create_predefined_category(AUTO_SALE_INCOME_CATEGORY)

        sales = Sale.objects.select_related("customer").all()

        for sale in sales:
            auto_income_qs = Transaction.objects.filter(
                sale=sale,
                type=TransactionType.INCOME,
                category=auto_sale_category,
            )

            has_manual_income = sale.receipts.filter(type=TransactionType.INCOME).exclude(
                category=auto_sale_category
            ).exists()

            if sale.status == RecordStatus.PAID and not has_manual_income:
                auto_income = auto_income_qs.order_by("created_at").first()
                description = f"{AUTO_SALE_INCOME_DESCRIPTION}: {sale.invoice_number}"

                if auto_income:
                    auto_income.date = sale.date
                    auto_income.amount = sale.total_amount
                    auto_income.customer = sale.customer
                    auto_income.description = description
                    auto_income.save(
                        update_fields=[
                            "date",
                            "amount",
                            "customer",
                            "description",
                            "updated_at",
                        ]
                    )
                    updated += 1
                    extras = auto_income_qs.exclude(pk=auto_income.pk)
                    extras_count = extras.count()
                    if extras_count:
                        extras.delete()
                        deleted += extras_count
                else:
                    Transaction.objects.create(
                        date=sale.date,
                        amount=sale.total_amount,
                        type=TransactionType.INCOME,
                        category=auto_sale_category,
                        description=description,
                        customer=sale.customer,
                        sale=sale,
                    )
                    created += 1
                continue

            auto_count = auto_income_qs.count()
            if auto_count:
                auto_income_qs.delete()
                deleted += auto_count
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS("Paid sales income sync complete."))
        self.stdout.write(
            f"Created: {created}, Updated: {updated}, Deleted: {deleted}, Skipped: {skipped}"
        )
