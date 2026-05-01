import csv
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_class, timedelta
from decimal import Decimal
from html import escape as html_escape
from io import BytesIO, StringIO
from typing import Callable, Iterable, Sequence

from django.db.models import Case, DecimalField, ExpressionWrapper, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.http import HttpResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.text import slugify

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .bs_date_utils import resolve_ad_date_filters
from .finance_ledger_display import build_customer_payment_display
from .models import (
    AlertNotification,
    AlertSource,
    AlertType,
    BambooRecord,
    BambooRecordType,
    BlocksRecord,
    BlocksRecordType,
    BlocksUnitType,
    CementRecord,
    CementRecordType,
    CementUnitType,
    Customer,
    CustomerPayment,
    JCBRecord,
    RecordStatus,
    Sale,
    TipperRecord,
    TipperRecordType,
    TransactionCategory,
    Transaction,
    TransactionType,
)

logger = logging.getLogger(__name__)

AUTO_SALE_INCOME_CATEGORY = "Sale Income (Auto)"
CREDIT_BALANCE_APPLIED_CATEGORY = "Credit Balance Applied"
PAYMENT_ALLOCATION_CATEGORY = "Sales Payment Allocation"
CREDIT_TOPUP_CATEGORY = "Customer Credit Top-up"


@dataclass
class ExportDefinition:
    title: str
    filename_slug: str
    headers: list[str]
    row_factory: Callable[[], Iterable[Sequence[object]]]
    filter_summary: list[str] = field(default_factory=list)


def _default_date_range():
    today = timezone.localdate()
    default_from = today - timedelta(days=29)
    return default_from, today


def _parse_date(raw_value):
    if not raw_value:
        return None
    try:
        return date_class.fromisoformat(str(raw_value))
    except ValueError:
        return None


def _normalize_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _money(value):
    numeric = value if isinstance(value, Decimal) else Decimal(str(value or 0))
    return f"{numeric.quantize(Decimal('0.01')):.2f}"


def _currency_label(value):
    return f"NPR {_money(value)}"


def _filtered_transactions(params, include_credit_adjustments=False):
    queryset = Transaction.objects.select_related(
        "customer",
        "sale",
        "category",
        "jcb_record",
        "tipper_record",
        "blocks_record",
        "cement_record",
        "bamboo_record",
    )
    if not include_credit_adjustments:
        queryset = queryset.exclude(category__name=CREDIT_BALANCE_APPLIED_CATEGORY)

    query = (params.get("q", "") or "").strip()
    transaction_type = (params.get("type", "") or "").strip()
    customer_id = (params.get("customer", "") or "").strip()
    category_id = (params.get("category", "") or "").strip()
    resolved_date_from, resolved_date_to = resolve_ad_date_filters(
        params,
        calendar_mode=(params.get("calendar_mode", "ad") or "ad"),
    )
    date_from = _parse_date(resolved_date_from)
    date_to = _parse_date(resolved_date_to)
    sort = (params.get("sort", "-date") or "-date").strip()

    if query:
        queryset = queryset.filter(
            Q(category__name__icontains=query)
            | Q(description__icontains=query)
            | Q(customer__name__icontains=query)
        )
    if transaction_type:
        queryset = queryset.filter(type=transaction_type)
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if category_id:
        queryset = queryset.filter(category_id=category_id)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    allowed_sorts = {
        "-date": "-date",
        "date": "date",
        "-amount": "-amount",
        "amount": "amount",
        "customer": "customer__name",
        "-customer": "-customer__name",
    }
    return queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at"), {
        "q": query,
        "type": transaction_type,
        "customer": customer_id,
        "category": category_id,
        "date_from": date_from,
        "date_to": date_to,
        "sort": sort,
    }


def _filtered_sales(params):
    queryset = Sale.objects.select_related("customer").annotate(
        received_total=F("paid_amount"),
        status_rank=Case(
            When(status=RecordStatus.PAID, then=Value(2)),
            default=Value(1),
            output_field=IntegerField(),
        ),
        remaining_balance=Case(
            When(paid_amount__gte=F("total_amount"), then=Value(Decimal("0.00"))),
            default=F("total_amount") - F("paid_amount"),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ),
    )

    query = (params.get("q", "") or "").strip()
    status = (params.get("status", "") or "").strip()
    customer_id = (params.get("customer", "") or "").strip()
    resolved_date_from, resolved_date_to = resolve_ad_date_filters(
        params,
        calendar_mode=(params.get("calendar_mode", "ad") or "ad"),
    )
    date_from = _parse_date(resolved_date_from)
    date_to = _parse_date(resolved_date_to)
    sort = (params.get("sort", "-date") or "-date").strip()

    if query:
        queryset = queryset.filter(
            Q(invoice_number__icontains=query)
            | Q(notes__icontains=query)
            | Q(customer__name__icontains=query)
        )
    if status:
        queryset = queryset.filter(status=status)
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    allowed_sorts = {
        "-date": "-date",
        "date": "date",
        "-amount": "-total_amount",
        "amount": "total_amount",
        "status": "status_rank",
        "-status": "-status_rank",
    }
    return queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at"), {
        "q": query,
        "status": status,
        "customer": customer_id,
        "date_from": date_from,
        "date_to": date_to,
        "sort": sort,
    }


def _filtered_jcb_records(params):
    queryset = JCBRecord.objects.all().annotate(
        income_amount_calc=Coalesce(
            F("total_amount"),
            ExpressionWrapper(
                F("total_work_hours") * F("rate"),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
        )
    )

    query = (params.get("q", "") or "").strip()
    status = (params.get("status", "") or "").strip()
    resolved_date_from, resolved_date_to = resolve_ad_date_filters(
        params,
        calendar_mode=(params.get("calendar_mode", "ad") or "ad"),
    )
    date_from = _parse_date(resolved_date_from)
    date_to = _parse_date(resolved_date_to)
    sort = (params.get("sort", "-date") or "-date").strip()

    if query:
        queryset = queryset.filter(
            Q(site_name__icontains=query)
            | Q(expense_item__icontains=query)
            | Q(status__icontains=query)
        )
    if status:
        queryset = queryset.filter(status=status).exclude(
            Q(start_time=Decimal("0.00"))
            & Q(end_time=Decimal("0.00"))
            & ~Q(expense_item="")
            & Q(expense_amount__isnull=False)
        )
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    allowed_sorts = {
        "-date": "-date",
        "date": "date",
        "-hours": "-total_work_hours",
        "hours": "total_work_hours",
        "-income": "-income_amount_calc",
        "income": "income_amount_calc",
    }
    return queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at"), {
        "q": query,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
        "sort": sort,
    }


def _filtered_tipper_records(params):
    queryset = TipperRecord.objects.select_related("item").all()
    query = (params.get("q", "") or "").strip()
    record_type = (params.get("record_type", "") or "").strip()
    item_id = (params.get("item", "") or "").strip()
    resolved_date_from, resolved_date_to = resolve_ad_date_filters(
        params,
        calendar_mode=(params.get("calendar_mode", "ad") or "ad"),
    )
    date_from = _parse_date(resolved_date_from)
    date_to = _parse_date(resolved_date_to)
    sort = (params.get("sort", "-date") or "-date").strip()

    if query:
        queryset = queryset.filter(Q(item__name__icontains=query) | Q(description__icontains=query))
    if record_type:
        queryset = queryset.filter(record_type=record_type)
    if item_id:
        queryset = queryset.filter(item_id=item_id)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    allowed_sorts = {
        "-date": "-date",
        "date": "date",
        "-amount": "-amount",
        "amount": "amount",
        "item": "item__name",
        "-item": "-item__name",
    }
    return queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at"), {
        "q": query,
        "record_type": record_type,
        "item": item_id,
        "date_from": date_from,
        "date_to": date_to,
        "sort": sort,
    }


def _filtered_material_records(params, model, record_type_choices, unit_type_choices=None):
    queryset = model.objects.all()
    query = (params.get("q", "") or "").strip()
    record_type = (params.get("record_type", "") or "").strip()
    payment_status = (params.get("payment_status", "") or "").strip()
    resolved_date_from, resolved_date_to = resolve_ad_date_filters(
        params,
        calendar_mode=(params.get("calendar_mode", "ad") or "ad"),
    )
    date_from = _parse_date(resolved_date_from)
    date_to = _parse_date(resolved_date_to)
    sort = (params.get("sort", "-date") or "-date").strip()
    unit_type = (params.get("unit_type", "") or "").strip()

    if query:
        queryset = queryset.filter(
            Q(notes__icontains=query)
            | Q(record_type__icontains=query)
            | Q(customer__name__icontains=query)
        )
    if record_type:
        queryset = queryset.filter(record_type=record_type)
    if payment_status:
        queryset = queryset.filter(payment_status=payment_status)
    if unit_type and unit_type_choices is not None:
        queryset = queryset.filter(unit_type=unit_type)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    allowed_sorts = {
        "-date": "-date",
        "date": "date",
        "-investment": "-investment",
        "investment": "investment",
        "-income": "-sale_income",
        "income": "sale_income",
    }
    return queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at"), {
        "q": query,
        "record_type": record_type,
        "payment_status": payment_status,
        "unit_type": unit_type,
        "date_from": date_from,
        "date_to": date_to,
        "sort": sort,
    }


def _alert_items(params):
    today = timezone.localdate()
    upcoming_end = today + timedelta(days=7)
    alert_type = (params.get("type", "") or "").strip()
    customer_id = (params.get("customer", "") or "").strip()
    resolved_date_from, resolved_date_to = resolve_ad_date_filters(
        params,
        calendar_mode=(params.get("calendar_mode", "ad") or "ad"),
    )
    date_from = _parse_date(resolved_date_from)
    date_to = _parse_date(resolved_date_to)

    sales_queryset = Sale.objects.select_related("customer").filter(
        status=RecordStatus.PENDING,
        alert_enabled=True,
        due_date__isnull=False,
    ).annotate(
        received_total=Coalesce(
            Sum("receipts__amount", filter=Q(receipts__type=TransactionType.INCOME)),
            Value(Decimal("0.00")),
        )
    )

    if customer_id == "__unassigned__":
        sales_queryset = sales_queryset.filter(customer__isnull=True)
    elif customer_id:
        sales_queryset = sales_queryset.filter(customer_id=customer_id)
    if date_from:
        sales_queryset = sales_queryset.filter(due_date__gte=date_from)
    if date_to:
        sales_queryset = sales_queryset.filter(due_date__lte=date_to)

    alert_rows = []
    for sale in sales_queryset:
        if sale.received_total >= sale.total_amount:
            continue
        state = ""
        if sale.due_date < today:
            state = AlertType.OVERDUE
        elif today <= sale.due_date <= upcoming_end:
            state = AlertType.UPCOMING
        if not state:
            continue
        if alert_type and state != alert_type:
            continue
        alert_rows.append(
            [
                sale.due_date,
                state.title(),
                "Sale",
                sale.customer.name if sale.customer else "",
                f"Invoice {sale.invoice_number}",
                sale.invoice_number,
                sale.total_amount - sale.received_total,
                sale.payment_status.title(),
            ]
        )

    manual_alerts = AlertNotification.objects.select_related("customer").filter(
        source_type=AlertSource.MANUAL,
        is_active=True,
    )
    if customer_id == "__unassigned__":
        manual_alerts = manual_alerts.filter(customer__isnull=True)
    elif customer_id:
        manual_alerts = manual_alerts.filter(customer_id=customer_id)
    if date_from:
        manual_alerts = manual_alerts.filter(due_date__gte=date_from)
    if date_to:
        manual_alerts = manual_alerts.filter(due_date__lte=date_to)
    if alert_type:
        manual_alerts = manual_alerts.filter(alert_type=alert_type)

    for manual_alert in manual_alerts:
        state = AlertType.OVERDUE if manual_alert.due_date < today else AlertType.UPCOMING
        if alert_type and state != alert_type:
            continue
        alert_rows.append(
            [
                manual_alert.due_date,
                state.title(),
                "Manual",
                manual_alert.customer.name if manual_alert.customer else "",
                manual_alert.title,
                "",
                manual_alert.amount,
                "Manual",
            ]
        )

    alert_rows.sort(key=lambda row: (row[0], row[1] == AlertType.UPCOMING.title()))
    return alert_rows, {
        "type": alert_type,
        "customer": customer_id,
        "date_from": date_from,
        "date_to": date_to,
    }


def _customer_rows(params):
    queryset = Customer.objects.all()
    query = (params.get("q", "") or "").strip()
    customer_type = (params.get("type", "") or "").strip()
    credit_status = (params.get("credit_status", "") or "").strip()
    date_from = _parse_date(params.get("date_from", "") or "")
    date_to = _parse_date(params.get("date_to", "") or "")

    if query:
        queryset = queryset.filter(
            Q(name__icontains=query)
            | Q(phone__icontains=query)
            | Q(address__icontains=query)
            | Q(credit_terms__icontains=query)
        )
    if customer_type:
        queryset = queryset.filter(type=customer_type)
    if credit_status == "with_balance":
        queryset = queryset.filter(opening_balance__gt=0)
    elif credit_status == "zero_balance":
        queryset = queryset.filter(opening_balance=0)

    queryset = queryset.order_by("name")
    customer_ids = list(queryset.values_list("id", flat=True))

    sale_queryset = Sale.objects.filter(customer_id__in=customer_ids)
    income_queryset = Transaction.objects.filter(
        customer_id__in=customer_ids,
        type=TransactionType.INCOME,
    ).exclude(category__name=CREDIT_BALANCE_APPLIED_CATEGORY)

    if date_from:
        sale_queryset = sale_queryset.filter(date__gte=date_from)
        income_queryset = income_queryset.filter(date__gte=date_from)
    if date_to:
        sale_queryset = sale_queryset.filter(date__lte=date_to)
        income_queryset = income_queryset.filter(date__lte=date_to)

    sales_totals = {
        row["customer_id"]: row["total"]
        for row in sale_queryset.values("customer_id").annotate(total=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))))
    }
    receipt_totals = {
        row["customer_id"]: row["total"]
        for row in income_queryset.values("customer_id").annotate(total=Coalesce(Sum("amount"), Value(Decimal("0.00"))))
    }
    lifetime_sales_totals = {
        row["customer_id"]: row["total"]
        for row in Sale.objects.filter(customer_id__in=customer_ids)
        .values("customer_id")
        .annotate(total=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))))
    }
    lifetime_paid_totals = {
        row["customer_id"]: row["total"]
        for row in Transaction.objects.filter(
            customer_id__in=customer_ids,
            type=TransactionType.INCOME,
        )
        .exclude(category__name=CREDIT_BALANCE_APPLIED_CATEGORY)
        .values("customer_id")
        .annotate(total=Coalesce(Sum("amount"), Value(Decimal("0.00"))))
    }

    def row_factory():
        for customer in queryset:
            sales_total = sales_totals.get(customer.id, Decimal("0.00"))
            receipts_total = receipt_totals.get(customer.id, Decimal("0.00"))
            lifetime_sales = lifetime_sales_totals.get(customer.id, Decimal("0.00"))
            lifetime_received = lifetime_paid_totals.get(customer.id, Decimal("0.00"))
            outstanding_due = customer.opening_balance + customer.manual_due_amount + lifetime_sales - lifetime_received
            if outstanding_due < 0:
                outstanding_due = Decimal("0.00")
            yield [
                customer.name,
                customer.get_type_display(),
                customer.phone,
                customer.address,
                customer.credit_terms,
                customer.opening_balance,
                customer.credit_balance,
                customer.manual_due_amount,
                sales_total,
                receipts_total,
                outstanding_due,
            ]

    return ExportDefinition(
        title="Customer Statement",
        filename_slug="customer_statement",
        headers=[
            "Name",
            "Type",
            "Phone",
            "Address",
            "Credit Terms",
            "Opening Balance",
            "Credit Balance",
            "Manual Due Amount",
            "Sales in Range",
            "Receipts in Range",
            "Outstanding Due",
        ],
        row_factory=row_factory,
        filter_summary=[
            f"Customer filters: search={query or 'all'}, type={customer_type or 'all'}, credit_status={credit_status or 'all'}",
            f"Statement range: {date_from.isoformat() if date_from else 'all time'} to {date_to.isoformat() if date_to else 'all time'}",
        ],
    )


def _cash_flow_rows(params):
    date_from = _parse_date(params.get("date_from", "") or "")
    date_to = _parse_date(params.get("date_to", "") or "")
    queryset = Transaction.objects.exclude(category__name=CREDIT_BALANCE_APPLIED_CATEGORY)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    daily_totals = (
        queryset.values("date")
        .annotate(
            income=Coalesce(Sum("amount", filter=Q(type=TransactionType.INCOME)), Value(Decimal("0.00"))),
            expense=Coalesce(Sum("amount", filter=Q(type=TransactionType.EXPENSE)), Value(Decimal("0.00"))),
        )
        .order_by("date")
    )

    def row_factory():
        running_balance = Decimal("0.00")
        for row in daily_totals:
            net = row["income"] - row["expense"]
            running_balance += net
            yield [row["date"], row["income"], row["expense"], net, running_balance]

    return ExportDefinition(
        title="Cash Flow Report",
        filename_slug="cash_flow_report",
        headers=["Date", "Income", "Expense", "Net", "Running Balance"],
        row_factory=row_factory,
        filter_summary=[
            f"Date range: {date_from.isoformat() if date_from else 'all time'} to {date_to.isoformat() if date_to else 'all time'}",
        ],
    )


def _product_performance_rows(params):
    date_from = _parse_date(params.get("date_from", "") or "")
    date_to = _parse_date(params.get("date_to", "") or "")
    queryset = Sale.objects.all().order_by("date", "created_at")
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    product_totals = defaultdict(lambda: {
        "quantity": Decimal("0.00"),
        "revenue": Decimal("0.00"),
        "lines": 0,
        "latest_sale": None,
    })

    for sale in queryset:
        sale_items = sale.items or []
        for item in sale_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("item", "")).strip() or "Unnamed Item"
            try:
                quantity = Decimal(str(item.get("quantity", 1)))
            except Exception:
                quantity = Decimal("0.00")
            try:
                amount = Decimal(str(item.get("amount", 0)))
            except Exception:
                amount = Decimal("0.00")
            product_totals[name]["quantity"] += quantity
            product_totals[name]["revenue"] += amount
            product_totals[name]["lines"] += 1
            product_totals[name]["latest_sale"] = sale.date

    def row_factory():
        for name, totals in sorted(product_totals.items(), key=lambda item: (-item[1]["revenue"], item[0].lower())):
            avg_price = Decimal("0.00")
            if totals["quantity"] > 0:
                avg_price = totals["revenue"] / totals["quantity"]
            yield [
                name,
                totals["quantity"],
                totals["lines"],
                totals["revenue"],
                avg_price,
                totals["latest_sale"],
            ]

    return ExportDefinition(
        title="Product Performance Report",
        filename_slug="product_performance_report",
        headers=["Item", "Units Sold", "Sales Lines", "Revenue", "Average Unit Price", "Latest Sale"],
        row_factory=row_factory,
        filter_summary=[
            f"Date range: {date_from.isoformat() if date_from else 'all time'} to {date_to.isoformat() if date_to else 'all time'}",
        ],
    )


def _material_row_factory(queryset, include_unit_type=False):
    def row_factory():
        for record in queryset:
            row = [
                record.date,
                record.get_record_type_display(),
            ]
            row.append(record.get_payment_status_display() if getattr(record, "payment_status", None) else "")
            if include_unit_type:
                unit_display = record.get_unit_type_display() if getattr(record, "unit_type", None) else ""
                row.append(unit_display)
            row.extend([
                record.quantity,
                record.price_per_unit,
                record.investment,
                record.sale_income,
                getattr(record, "paid_amount", Decimal("0.00")),
                getattr(record, "pending_amount", Decimal("0.00")),
                record.notes,
            ])
            yield row

    return row_factory


def _build_material_definition(params, model, title, filename_slug, has_unit_type=False):
    queryset, filters = _filtered_material_records(params, model, None, unit_type_choices=True if has_unit_type else None)
    headers = ["Date", "Record Type", "Payment Status"]
    if has_unit_type:
        headers.append("Unit Type")
    headers.extend(["Quantity", "Price Per Unit", "Investment", "Sale Income", "Paid Amount", "Pending Amount", "Notes"])
    summary = [
        f"Date range: {filters['date_from'].isoformat() if filters['date_from'] else 'all time'} to {filters['date_to'].isoformat() if filters['date_to'] else 'all time'}",
        f"Record type: {filters['record_type'] or 'all'}",
        f"Payment status: {filters['payment_status'] or 'all'}",
    ]
    if has_unit_type:
        summary.append(f"Unit type: {filters['unit_type'] or 'all'}")
    return ExportDefinition(
        title=title,
        filename_slug=filename_slug,
        headers=headers,
        row_factory=_material_row_factory(queryset, include_unit_type=has_unit_type),
        filter_summary=summary,
    )


def _build_finance_ledger_definition(params):
    queryset, filters = _filtered_transactions(params)
    query = filters["q"]
    transaction_type = filters["type"]
    category_id = filters["category"]
    selected_category_name = None
    if category_id:
        selected_category_name = TransactionCategory.objects.filter(pk=category_id).values_list("name", flat=True).first()
    transactions = queryset.exclude(
        category__name__in=[
            PAYMENT_ALLOCATION_CATEGORY,
            CREDIT_TOPUP_CATEGORY,
        ]
    )
    customer_payments = CustomerPayment.objects.select_related("customer").prefetch_related("allocations__sale")
    if query:
        customer_payments = customer_payments.filter(
            Q(customer__name__icontains=query)
            | Q(notes__icontains=query)
            | Q(allocations__sale__invoice_number__icontains=query)
        )
    if filters["date_from"]:
        customer_payments = customer_payments.filter(payment_date__gte=filters["date_from"])
    if filters["date_to"]:
        customer_payments = customer_payments.filter(payment_date__lte=filters["date_to"])
    if filters["customer"]:
        customer_payments = customer_payments.filter(customer_id=filters["customer"])
    if transaction_type and transaction_type != TransactionType.INCOME:
        customer_payments = customer_payments.none()
    if category_id and selected_category_name != PAYMENT_ALLOCATION_CATEGORY:
        customer_payments = customer_payments.none()
    if filters["sort"] in {"customer", "-customer"}:
        customer_payments = customer_payments.order_by("customer__name", "-created_at")
    else:
        customer_payments = customer_payments.order_by("-payment_date", "-created_at")
    headers = [
        "Date",
        "Type",
        "Amount",
        "Payment Method",
        "Customer",
        "Category",
        "Summary",
        "Allocated To Sales",
        "Unallocated To Credit",
        "Linked Sale Count",
        "Description",
    ]

    def _sort_key(entry):
        entry_date = getattr(entry, "date", None) or getattr(entry, "payment_date", None)
        entry_amount = getattr(entry, "amount", Decimal("0.00"))
        entry_customer = getattr(getattr(entry, "customer", None), "name", "")
        entry_created = getattr(entry, "created_at", None) or timezone.now()
        if filters["sort"] == "amount":
            return (entry_amount, entry_date or timezone.localdate(), entry_created)
        if filters["sort"] == "-amount":
            return (entry_amount, entry_date or timezone.localdate(), entry_created)
        if filters["sort"] == "customer":
            return (entry_customer.lower(), entry_date or timezone.localdate(), entry_created)
        if filters["sort"] == "-customer":
            return (entry_customer.lower(), entry_date or timezone.localdate(), entry_created)
        return (entry_date or timezone.localdate(), entry_created)

    combined_rows = list(transactions) + [build_customer_payment_display(payment) for payment in customer_payments.distinct()]
    if filters["sort"] in {"-date", "-amount", "-customer"}:
        combined_rows = sorted(combined_rows, key=_sort_key, reverse=True)
    else:
        combined_rows = sorted(combined_rows, key=_sort_key)

    def row_factory():
        for transaction in combined_rows:
            if getattr(transaction, "is_grouped_payment", False):
                yield [
                    transaction.date,
                    transaction.get_type_display(),
                    transaction.amount,
                    transaction.get_payment_method_display(),
                    transaction.customer.name if transaction.customer else "",
                    transaction.category,
                    transaction.summary_text,
                    transaction.allocated_total,
                    transaction.unallocated_total,
                    transaction.allocation_count,
                    transaction.description,
                ]
                continue

            linked_modules = []
            if transaction.sale_id:
                linked_modules.append("Sale")
            if transaction.jcb_record_id:
                linked_modules.append("JCB")
            if transaction.blocks_record_id:
                linked_modules.append("Blocks")
            if transaction.cement_record_id:
                linked_modules.append("Cement")
            if transaction.bamboo_record_id:
                linked_modules.append("Bamboo")
            if transaction.tipper_record_id:
                linked_modules.append("Tipper")
            yield [
                transaction.date,
                transaction.get_type_display(),
                transaction.amount,
                transaction.get_payment_method_display(),
                transaction.customer.name if transaction.customer else "",
                transaction.category.name if transaction.category else "",
                "",
                "",
                "",
                "",
                transaction.description,
            ]

    return ExportDefinition(
        title="Finance Ledger Report",
        filename_slug="finance_ledger_report",
        headers=headers,
        row_factory=row_factory,
        filter_summary=[
            f"Search: {filters['q'] or 'all'}",
            f"Type: {filters['type'] or 'all'}, customer: {filters['customer'] or 'all'}, category: {filters['category'] or 'all'}",
            f"Date range: {filters['date_from'].isoformat() if filters['date_from'] else 'all time'} to {filters['date_to'].isoformat() if filters['date_to'] else 'all time'}",
        ],
    )


def _build_sales_definition(params, dues_only=False):
    queryset, filters = _filtered_sales(params)
    if dues_only:
        queryset = queryset.filter(remaining_balance__gt=0)

    def summarize_sale_items(items):
        if not isinstance(items, list) or not items:
            return "", Decimal("0.00"), "", "", "0"

        item_names = []
        units_seen = []
        item_lines = []
        total_quantity = Decimal("0.00")

        for item in items:
            if not isinstance(item, dict):
                continue

            name = str(item.get("item", "")).strip()
            unit = str(item.get("unit", "")).strip()
            raw_qty = item.get("quantity", 0)

            try:
                qty = Decimal(str(raw_qty))
            except Exception:
                qty = Decimal("0.00")

            if name and name not in item_names:
                item_names.append(name)
            if unit and unit not in units_seen:
                units_seen.append(unit)

            if name:
                qty_text = f"{qty.normalize()}" if qty != qty.to_integral_value() else str(int(qty))
                if unit:
                    item_lines.append(f"{name} ({unit}) x{qty_text}")
                else:
                    item_lines.append(f"{name} x{qty_text}")

            total_quantity += qty

        quantity_text = f"{total_quantity.normalize()}" if total_quantity != total_quantity.to_integral_value() else str(int(total_quantity))
        return ", ".join(item_names), total_quantity, ", ".join(units_seen), "; ".join(item_lines), quantity_text

    headers = [
        "Invoice Number",
        "Date",
        "Customer",
        "Items",
        "Quantity Sold",
        "Units Sold",
        "Item Lines",
        "Status",
        "Total Amount",
        "Paid Amount",
        "Remaining Balance",
        "Due Date",
        "Alert Enabled",
        "Notes",
    ]

    def row_factory():
        for sale in queryset:
            remaining_balance = sale.total_amount - sale.paid_amount
            if remaining_balance < 0:
                remaining_balance = Decimal("0.00")
            item_names, _total_qty, units_sold, item_lines, quantity_text = summarize_sale_items(sale.items)
            yield [
                sale.invoice_number,
                sale.date,
                sale.customer.name if sale.customer else "",
                item_names,
                quantity_text,
                units_sold,
                item_lines,
                sale.get_status_display(),
                sale.total_amount,
                sale.paid_amount,
                remaining_balance,
                sale.due_date,
                sale.alert_enabled,
                sale.notes,
            ]

    return ExportDefinition(
        title="Due Payments Report" if dues_only else "Sales Report",
        filename_slug="due_payments_report" if dues_only else "sales_report",
        headers=headers,
        row_factory=row_factory,
        filter_summary=[
            f"Search: {filters['q'] or 'all'}",
            f"Status: {filters['status'] or ('pending due only' if dues_only else 'all')}, customer: {filters['customer'] or 'all'}",
            f"Date range: {filters['date_from'].isoformat() if filters['date_from'] else 'all time'} to {filters['date_to'].isoformat() if filters['date_to'] else 'all time'}",
        ],
    )


def _build_jcb_definition(params):
    queryset, filters = _filtered_jcb_records(params)
    headers = [
        "Date",
        "Site Name",
        "Start Time",
        "End Time",
        "Work Hours",
        "Status",
        "Rate",
        "Total Amount",
        "Expense Item",
        "Expense Amount",
    ]

    def row_factory():
        for record in queryset:
            yield [
                record.date,
                record.site_name or "",
                record.start_time,
                record.end_time,
                record.total_work_hours,
                record.get_status_display(),
                record.rate,
                record.income_amount_calc,
                record.expense_item,
                record.expense_amount,
            ]

    return ExportDefinition(
        title="JCB Records Report",
        filename_slug="jcb_records_report",
        headers=headers,
        row_factory=row_factory,
        filter_summary=[
            f"Search: {filters['q'] or 'all'}",
            f"Status: {filters['status'] or 'all'}",
            f"Date range: {filters['date_from'].isoformat() if filters['date_from'] else 'all time'} to {filters['date_to'].isoformat() if filters['date_to'] else 'all time'}",
        ],
    )


def _build_tipper_definition(params):
    queryset, filters = _filtered_tipper_records(params)
    headers = ["Date", "Item", "Type", "Amount", "Description"]

    def row_factory():
        for record in queryset:
            yield [
                record.date,
                record.item.name,
                record.get_record_type_display(),
                record.amount,
                record.description,
            ]

    return ExportDefinition(
        title="Tipper Records Report",
        filename_slug="tipper_records_report",
        headers=headers,
        row_factory=row_factory,
        filter_summary=[
            f"Search: {filters['q'] or 'all'}",
            f"Record type: {filters['record_type'] or 'all'}, item: {filters['item'] or 'all'}",
            f"Date range: {filters['date_from'].isoformat() if filters['date_from'] else 'all time'} to {filters['date_to'].isoformat() if filters['date_to'] else 'all time'}",
        ],
    )


def _build_alerts_definition(params):
    rows, filters = _alert_items(params)
    headers = ["Due Date", "State", "Source", "Customer", "Title", "Invoice Number", "Amount", "Status"]
    return ExportDefinition(
        title="Alerts Report",
        filename_slug="alerts_report",
        headers=headers,
        row_factory=lambda: iter(rows),
        filter_summary=[
            f"Type: {filters['type'] or 'all'}, customer: {filters['customer'] or 'all'}",
            f"Date range: {filters['date_from'].isoformat() if filters['date_from'] else 'all time'} to {filters['date_to'].isoformat() if filters['date_to'] else 'all time'}",
        ],
    )


def _build_dashboard_cash_flow_definition(params):
    return _cash_flow_rows(params)


def _build_dashboard_product_definition(params):
    return _product_performance_rows(params)


def _build_customer_statement_definition(params):
    return _customer_rows(params)


def _build_blocks_definition(params):
    return _build_material_definition(params, BlocksRecord, "Blocks Records Report", "blocks_records_report", has_unit_type=True)


def _build_cement_definition(params):
    return _build_material_definition(params, CementRecord, "Cement Records Report", "cement_records_report", has_unit_type=True)


def _build_bamboo_definition(params):
    return _build_material_definition(params, BambooRecord, "Bamboo Records Report", "bamboo_records_report", has_unit_type=False)


REPORT_BUILDERS = {
    "sales": _build_sales_definition,
    "due_payments": lambda params: _build_sales_definition(params, dues_only=True),
    "finance_ledger": _build_finance_ledger_definition,
    "cash_entries": _build_finance_ledger_definition,
    "jcb_records": _build_jcb_definition,
    "tipper_records": _build_tipper_definition,
    "alerts": _build_alerts_definition,
    "customer_statement": _build_customer_statement_definition,
    "customers": _build_customer_statement_definition,
    "blocks_records": _build_blocks_definition,
    "cement_records": _build_cement_definition,
    "bamboo_records": _build_bamboo_definition,
    "cash_flow": _build_dashboard_cash_flow_definition,
    "product_performance": _build_dashboard_product_definition,
}


def _rows_to_text(rows):
    for row in rows:
        yield [_normalize_text(cell) for cell in row]


class _Echo:
    def write(self, value):
        return value


def _stream_table_response(definition, delimiter, content_type, extension):
    pseudo_buffer = _Echo()
    writer = csv.writer(pseudo_buffer, delimiter=delimiter)

    def iterator():
        yield writer.writerow(definition.headers)
        for row in definition.row_factory():
            yield writer.writerow([_normalize_text(cell) for cell in row])

    response = StreamingHttpResponse(iterator(), content_type=f"{content_type}; charset=utf-8")
    filename = f"{definition.filename_slug}-{timezone.localdate().isoformat()}.{extension}"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _build_pdf_response(definition):
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ExportTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        textColor=colors.HexColor("#2f4666"),
        spaceAfter=6,
    )
    meta_style = ParagraphStyle(
        "ExportMeta",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#3f5b7f"),
    )
    cell_style = ParagraphStyle(
        "ExportCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
    )

    elements = [Paragraph(html_escape(definition.title), title_style)]
    if definition.filter_summary:
        for line in definition.filter_summary:
            elements.append(Paragraph(html_escape(line), meta_style))
        elements.append(Spacer(1, 4 * mm))

    table_data = [[Paragraph(html_escape(header), cell_style) for header in definition.headers]]
    rows = list(definition.row_factory())
    if rows:
        for row in rows:
            table_data.append([Paragraph(html_escape(_normalize_text(cell)), cell_style) for cell in row])
    else:
        table_data.append([Paragraph("No records found.", cell_style)] + [""] * (len(definition.headers) - 1))

    table = Table(table_data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#b9e5e8")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#2f4666")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LEADING", (0, 0), (-1, -1), 10),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#a8cfdb")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.HexColor("#f6fcfb")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(table)
    document.build(elements)

    pdf_bytes = buffer.getvalue()
    buffer.close()
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    filename = f"{definition.filename_slug}-{timezone.localdate().isoformat()}.pdf"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def build_export_response(report_name, export_format, params):
    builder = REPORT_BUILDERS.get(report_name)
    if builder is None:
        raise ValueError("Unsupported report type.")

    export_format = (export_format or "csv").strip().lower()
    if export_format not in {"csv", "xls", "pdf"}:
        raise ValueError("Unsupported export format.")

    definition = builder(params)
    if export_format == "pdf":
        return _build_pdf_response(definition)
    if export_format == "xls":
        return _stream_table_response(definition, delimiter="\t", content_type="application/vnd.ms-excel", extension="xls")
    return _stream_table_response(definition, delimiter=",", content_type="text/csv", extension="csv")


def available_reports():
    return tuple(sorted(REPORT_BUILDERS.keys()))