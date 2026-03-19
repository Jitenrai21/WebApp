from datetime import timedelta
from decimal import Decimal
import logging

from django.core.management.base import BaseCommand
from django.core.mail import mail_admins
from django.db.models import F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.models import AlertNotification, AlertSource, AlertType, RecordStatus, Sale, Transaction, TransactionType

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process overdue and upcoming alerts and persist notification timeline records."

    def handle(self, *args, **options):
        today = timezone.localdate()
        upcoming_end = today + timedelta(days=7)

        active_signatures = set()
        created_count = 0
        updated_count = 0

        sales_queryset = Sale.objects.select_related("customer").annotate(
            received_total=Coalesce(
                Sum("receipts__amount", filter=Q(receipts__type=TransactionType.INCOME)),
                Value(Decimal("0.00")),
            )
        )

        for sale in sales_queryset:
            if sale.received_total >= sale.total_amount:
                continue

            alert_type = None
            if sale.due_date < today:
                alert_type = AlertType.OVERDUE
            elif today <= sale.due_date <= upcoming_end:
                alert_type = AlertType.UPCOMING

            if not alert_type:
                continue

            signature = (alert_type, AlertSource.SALE, sale.id, sale.due_date)
            active_signatures.add(signature)

            title = f"Invoice {sale.invoice_number} is {alert_type}"
            message = (
                f"Customer {sale.customer.name} has outstanding invoice {sale.invoice_number} "
                f"due on {sale.due_date}."
            )
            amount = sale.total_amount - sale.received_total

            _, created = AlertNotification.objects.update_or_create(
                alert_type=alert_type,
                source_type=AlertSource.SALE,
                source_id=sale.id,
                due_date=sale.due_date,
                defaults={
                    "customer": sale.customer,
                    "amount": amount,
                    "title": title,
                    "message": message,
                    "is_active": True,
                    "resolved_at": None,
                },
            )
            if created:
                created_count += 1
                logger.info("Created sale alert notification for sale=%s type=%s", sale.id, alert_type)
            else:
                updated_count += 1

        tx_queryset = Transaction.objects.select_related("customer").filter(
            due_date__isnull=False,
            status=RecordStatus.PENDING,
        )

        for transaction in tx_queryset:
            alert_type = None
            if transaction.due_date < today:
                alert_type = AlertType.OVERDUE
            elif today <= transaction.due_date <= upcoming_end:
                alert_type = AlertType.UPCOMING

            if not alert_type:
                continue

            signature = (alert_type, AlertSource.TRANSACTION, transaction.id, transaction.due_date)
            active_signatures.add(signature)

            title = f"Transaction {transaction.category} is {alert_type}"
            message = (
                f"Customer {transaction.customer.name} has a pending transaction in category "
                f"{transaction.category} due on {transaction.due_date}."
            )

            _, created = AlertNotification.objects.update_or_create(
                alert_type=alert_type,
                source_type=AlertSource.TRANSACTION,
                source_id=transaction.id,
                due_date=transaction.due_date,
                defaults={
                    "customer": transaction.customer,
                    "amount": transaction.amount,
                    "title": title,
                    "message": message,
                    "is_active": True,
                    "resolved_at": None,
                },
            )
            if created:
                created_count += 1
                logger.info(
                    "Created transaction alert notification for transaction=%s type=%s",
                    transaction.id,
                    alert_type,
                )
            else:
                updated_count += 1

        # Resolve any active notification whose source signature is no longer active.
        for notification in AlertNotification.objects.filter(is_active=True):
            signature = (
                notification.alert_type,
                notification.source_type,
                notification.source_id,
                notification.due_date,
            )
            if signature not in active_signatures:
                notification.is_active = False
                notification.resolved_at = timezone.now()
                notification.save(update_fields=["is_active", "resolved_at", "updated_at"])

        summary = (
            f"Alert processing completed: created={created_count}, "
            f"updated={updated_count}, active={len(active_signatures)}"
        )
        logger.info(summary)
        self.stdout.write(self.style.SUCCESS(summary))

        if created_count > 0:
            try:
                mail_admins(
                    subject="Company Flow: New timeline alerts generated",
                    message=summary,
                    fail_silently=True,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("Unable to send alert summary email: %s", exc)
