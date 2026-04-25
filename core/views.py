import json
import logging
from decimal import Decimal
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Case, CharField, DecimalField, ExpressionWrapper, F, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone

from .forms import (
	BambooRecordForm,
	BlocksRecordForm,
	CustomerForm,
	CementRecordForm,
	JCBRecordForm,
	ManualAlertForm,
	SaleForm,
	SaleReceiptForm,
	TipperRecordForm,
	TransactionForm,
)
from .calendar_mode import (
	CALENDAR_MODE_SESSION_KEY,
	get_calendar_mode,
	normalize_calendar_mode,
)
from .bs_date_utils import ad_string_to_date, date_to_calendar_input, parse_calendar_date_input, resolve_ad_date_filters
from .models import (
	AlertNotification,
	AlertSource,
	AlertType,
	BambooRecord,
	BambooRecordType,
	BlocksRecord,
	BlocksRecordType,
	BlocksUnitType,
	Customer,
	CustomerPayment,
	CementRecord,
	CementRecordType,
	CementUnitType,
	JCBRecord,
	PaymentAllocation,
	PaymentMethod,
	RecordStatus,
	Sale,
	TipperItem,
	TipperRecord,
	TipperRecordType,
	Transaction,
	TransactionCategory,
	TransactionType,
)
from .report_exports import build_export_response


logger = logging.getLogger(__name__)


AUTO_SALE_INCOME_CATEGORY = "Sale Income (Auto)"
AUTO_SALE_INCOME_DESCRIPTION = "Auto-linked from paid sale"
SALE_INITIAL_PAYMENT_CATEGORY = "Sale Initial Payment"
PAYMENT_ALLOCATION_CATEGORY = "Sales Payment Allocation"
MANUAL_DUE_SETTLEMENT_CATEGORY = "Manual Due Settlement"
CREDIT_TOPUP_CATEGORY = "Customer Credit Top-up"
CREDIT_BALANCE_APPLIED_CATEGORY = "Credit Balance Applied"
JCB_INCOME_CATEGORY = "JCB Income"
JCB_EXPENSE_CATEGORY = "JCB Expense"
TIPPER_EXPENSE_CATEGORY = "Tipper Expense"
UNASSIGNED_CUSTOMER_FILTER = "__unassigned__"


def _calculate_customer_due_amount(total_sales, total_payments, manual_due_amount, credit_balance=Decimal("0.00")):
	due_amount = total_sales - total_payments + manual_due_amount
	if due_amount < 0:
		available_credit = max(credit_balance, Decimal("0.00"))
		if available_credit > 0:
			due_amount = -min(abs(due_amount), available_credit)
		else:
			due_amount = Decimal("0.00")
	return due_amount


def _customer_due_amount_from_sales(customer):
	payment_totals = customer.sales.aggregate(
		total_sales=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))),
		total_paid=Coalesce(Sum("paid_amount"), Value(Decimal("0.00"))),
	)
	material_pending = _customer_material_pending_total(customer)
	return _calculate_customer_due_amount(
		payment_totals["total_sales"] + material_pending,
		payment_totals["total_paid"],
		customer.manual_due_amount,
		customer.credit_balance,
	)


def _customer_material_pending_total(customer):
	blocks_pending = customer.blocks_records.filter(record_type=BlocksRecordType.SALE).aggregate(
		total=Coalesce(Sum("pending_amount"), Value(Decimal("0.00")))
	)["total"]
	cement_pending = customer.cement_records.filter(record_type=CementRecordType.SALE).aggregate(
		total=Coalesce(Sum("pending_amount"), Value(Decimal("0.00")))
	)["total"]
	bamboo_pending = customer.bamboo_records.filter(record_type=BambooRecordType.SALE).aggregate(
		total=Coalesce(Sum("pending_amount"), Value(Decimal("0.00")))
	)["total"]
	return blocks_pending + cement_pending + bamboo_pending


def _material_pending_rows_for_customer(customer):
	rows = []

	for record in customer.blocks_records.filter(record_type=BlocksRecordType.SALE).order_by("date", "created_at", "id"):
		if record.pending_amount <= 0:
			continue
		rows.append({
			"module": "blocks",
			"label": "Blocks",
			"record": record,
			"pending_amount": record.pending_amount,
			"descriptor": f"{record.get_unit_type_display() or 'Sale'} | {record.quantity or 0} units",
		})

	for record in customer.cement_records.filter(record_type=CementRecordType.SALE).order_by("date", "created_at", "id"):
		if record.pending_amount <= 0:
			continue
		rows.append({
			"module": "cement",
			"label": "Cement",
			"record": record,
			"pending_amount": record.pending_amount,
			"descriptor": f"{record.get_unit_type_display() or 'Sale'} | {record.quantity or 0} units",
		})

	for record in customer.bamboo_records.filter(record_type=BambooRecordType.SALE).order_by("date", "created_at", "id"):
		if record.pending_amount <= 0:
			continue
		rows.append({
			"module": "bamboo",
			"label": "Bamboo",
			"record": record,
			"pending_amount": record.pending_amount,
			"descriptor": f"Sale | {record.quantity or 0} units",
		})

	return rows


def _get_or_create_predefined_category(name):
	category, _ = TransactionCategory.objects.get_or_create(
		name=name,
		defaults={"is_predefined": True},
	)
	return category


def _htmx_feedback_response(message, level="success", status=200, redirect_url=""):
	payload = {
		"cf-toast": {
			"message": message,
			"level": level,
		}
	}
	if redirect_url:
		payload["cf-redirect"] = {"url": redirect_url}
	return HttpResponse("", status=status, headers={"HX-Trigger": json.dumps(payload)})


def _get_default_date_range():
	"""Get default date range for last 30 days.
	
	Returns:
		tuple: (default_from, default_to) as ISO format date strings
	"""
	import datetime
	today = datetime.date.today()
	default_from = (today - datetime.timedelta(days=29)).isoformat()
	default_to = today.isoformat()
	return default_from, default_to


def _resolve_request_date_filters(request, *, default_from="", default_to=""):
	calendar_mode = get_calendar_mode(request)
	parse_errors = []
	date_from, date_to = resolve_ad_date_filters(
		request.GET,
		default_from=default_from,
		default_to=default_to,
		calendar_mode=calendar_mode,
		errors=parse_errors,
	)
	for parse_error in parse_errors:
		messages.error(request, parse_error)

	return {
		"date_from": date_from,
		"date_to": date_to,
		"date_from_display": date_to_calendar_input(ad_string_to_date(date_from), calendar_mode) if date_from else "",
		"date_to_display": date_to_calendar_input(ad_string_to_date(date_to), calendar_mode) if date_to else "",
	}


def _form_calendar_mode_kwargs(request):
	return {"calendar_mode": get_calendar_mode(request)}


def _resolve_posted_date(request, raw_value, *, fallback=None):
	fallback_date = fallback or timezone.localdate()
	date_value, parse_error = parse_calendar_date_input(raw_value, get_calendar_mode(request))
	if parse_error:
		messages.error(request, parse_error)
		return fallback_date
	return date_value or fallback_date


@login_required
def set_calendar_mode(request, mode):
	next_url = request.GET.get("next", "").strip()
	request.session[CALENDAR_MODE_SESSION_KEY] = normalize_calendar_mode(mode)

	if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
		return redirect(next_url)
	return redirect("dashboard")


def _dashboard_base_sales_queryset(date_from="", date_to=""):
	sales_queryset = Sale.objects.select_related("customer").annotate(
		received_total=Coalesce(
			Sum("receipts__amount", filter=Q(receipts__type=TransactionType.INCOME)),
			Value(Decimal("0.00")),
		)
	)

	if date_from:
		sales_queryset = sales_queryset.filter(date__gte=date_from)
	if date_to:
		sales_queryset = sales_queryset.filter(date__lte=date_to)

	return sales_queryset


def _dashboard_sales_amount_queryset(date_from="", date_to=""):
	sales_queryset = Sale.objects.select_related("customer")

	if date_from:
		sales_queryset = sales_queryset.filter(date__gte=date_from)
	if date_to:
		sales_queryset = sales_queryset.filter(date__lte=date_to)

	return sales_queryset


def _sales_alert_queryset():
	return (
		Sale.objects.select_related("customer")
		.filter(
			status=RecordStatus.PENDING,
			alert_enabled=True,
			due_date__isnull=False,
		)
		.annotate(
		received_total=Coalesce(
			Sum("receipts__amount", filter=Q(receipts__type=TransactionType.INCOME)),
			Value(Decimal("0.00")),
		)
	)
	)


def _build_alert_items(alert_type="", customer_id="", date_from="", date_to=""):
	today = timezone.localdate()
	upcoming_end = today + timedelta(days=7)

	sales_queryset = _sales_alert_queryset()

	if customer_id == UNASSIGNED_CUSTOMER_FILTER:
		sales_queryset = sales_queryset.filter(customer__isnull=True)
	elif customer_id:
		sales_queryset = sales_queryset.filter(customer_id=customer_id)
	if date_from:
		sales_queryset = sales_queryset.filter(due_date__gte=date_from)
	if date_to:
		sales_queryset = sales_queryset.filter(due_date__lte=date_to)

	alert_items = []

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

		alert_items.append(
			{
				"state": state,
				"source": AlertSource.SALE,
				"due_date": sale.due_date,
				"customer": sale.customer,
				"title": f"Invoice {sale.invoice_number}",
				"invoice_number": sale.invoice_number,
				"sale_description": sale.notes,
				"amount": sale.total_amount - sale.received_total,
				"status_label": sale.payment_status.title(),
				"object_id": sale.id,
			}
		)

	manual_alerts = AlertNotification.objects.select_related("customer").filter(
		source_type=AlertSource.MANUAL,
		is_active=True,
	)
	if customer_id == UNASSIGNED_CUSTOMER_FILTER:
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
		manual_state = AlertType.OVERDUE if manual_alert.due_date < today else AlertType.UPCOMING
		if alert_type and alert_type != manual_state:
			continue
		alert_items.append(
			{
				"state": manual_state,
				"source": AlertSource.MANUAL,
				"due_date": manual_alert.due_date,
				"customer": manual_alert.customer,
				"title": manual_alert.title,
				"invoice_number": "",
				"sale_description": "",
				"amount": manual_alert.amount,
				"status_label": "Manual",
				"object_id": manual_alert.id,
			}
		)

	alert_items.sort(key=lambda item: (item["due_date"], item["state"] == AlertType.UPCOMING))
	return alert_items


def _alerts_badge_count():
	# Badge should only show currently overdue active alerts.
	return sum(1 for item in _build_alert_items() if item["state"] == AlertType.OVERDUE)


def _alerts_context(alert_type="", customer_id="", date_from="", date_to=""):
	today = timezone.localdate()
	alert_items = _build_alert_items(
		alert_type=alert_type,
		customer_id=customer_id,
		date_from=date_from,
		date_to=date_to,
	)

	notification_timeline = AlertNotification.objects.select_related("customer")
	if customer_id == UNASSIGNED_CUSTOMER_FILTER:
		notification_timeline = notification_timeline.filter(customer__isnull=True)
	elif customer_id:
		notification_timeline = notification_timeline.filter(customer_id=customer_id)
	if date_from:
		notification_timeline = notification_timeline.filter(due_date__gte=date_from)
	if date_to:
		notification_timeline = notification_timeline.filter(due_date__lte=date_to)
	if alert_type:
		if alert_type == AlertType.OVERDUE:
			notification_timeline = notification_timeline.filter(
				Q(alert_type=AlertType.OVERDUE)
				| Q(source_type=AlertSource.MANUAL, due_date__lt=today)
			)
		elif alert_type == AlertType.UPCOMING:
			notification_timeline = notification_timeline.filter(
				Q(alert_type=AlertType.UPCOMING)
				| Q(source_type=AlertSource.MANUAL, due_date__gte=today)
			)
		else:
			notification_timeline = notification_timeline.filter(alert_type=alert_type)

	return {
		"alert_items": alert_items,
		"timeline": notification_timeline.order_by("-created_at")[:20],
		"customers": Customer.objects.order_by("name"),
		"filters": {
			"type": alert_type,
			"customer": customer_id,
			"date_from": date_from,
			"date_to": date_to,
		},
		"today": today,
		"alerts_badge_count": _alerts_badge_count(),
	}


def _dashboard_context(request=None, date_from="", date_to=""):
	sales_queryset = _dashboard_base_sales_queryset(date_from, date_to)
	sales_amount_queryset = _dashboard_sales_amount_queryset(date_from, date_to)
	transactions_queryset = Transaction.objects.select_related("customer").exclude(
		category__name=CREDIT_BALANCE_APPLIED_CATEGORY,
	)
	jcb_queryset = JCBRecord.objects.all()
	tipper_queryset = TipperRecord.objects.select_related("item")
	blocks_queryset = BlocksRecord.objects.all()
	cement_queryset = CementRecord.objects.all()
	bamboo_queryset = BambooRecord.objects.all()
	if date_from:
		transactions_queryset = transactions_queryset.filter(date__gte=date_from)
		jcb_queryset = jcb_queryset.filter(date__gte=date_from)
		tipper_queryset = tipper_queryset.filter(date__gte=date_from)
		blocks_queryset = blocks_queryset.filter(date__gte=date_from)
		cement_queryset = cement_queryset.filter(date__gte=date_from)
		bamboo_queryset = bamboo_queryset.filter(date__gte=date_from)
	if date_to:
		transactions_queryset = transactions_queryset.filter(date__lte=date_to)
		jcb_queryset = jcb_queryset.filter(date__lte=date_to)
		tipper_queryset = tipper_queryset.filter(date__lte=date_to)
		blocks_queryset = blocks_queryset.filter(date__lte=date_to)
		cement_queryset = cement_queryset.filter(date__lte=date_to)
		bamboo_queryset = bamboo_queryset.filter(date__lte=date_to)

	kpi_sales = sales_amount_queryset.aggregate(
		total_sales=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))),
	)
	sales_rows = list(sales_queryset)
	received_total = sum((sale.received_total for sale in sales_rows), Decimal("0.00"))
	kpi_income_expense = transactions_queryset.aggregate(
		total_income=Coalesce(
			Sum("amount", filter=Q(type=TransactionType.INCOME)),
			Value(Decimal("0.00")),
		),
		total_expenses=Coalesce(
			Sum("amount", filter=Q(type=TransactionType.EXPENSE)),
			Value(Decimal("0.00")),
		),
	)

	all_time_sales_queryset = _dashboard_base_sales_queryset()
	all_time_sales_rows = list(all_time_sales_queryset)
	outstanding_receivables = sum(
		((sale.total_amount - sale.received_total) for sale in all_time_sales_rows),
		Decimal("0.00"),
	)
	material_outstanding = (
		BlocksRecord.objects.filter(record_type=BlocksRecordType.SALE).aggregate(
			total=Coalesce(Sum("pending_amount"), Value(Decimal("0.00")))
		)["total"]
		+ CementRecord.objects.filter(record_type=CementRecordType.SALE).aggregate(
			total=Coalesce(Sum("pending_amount"), Value(Decimal("0.00")))
		)["total"]
		+ BambooRecord.objects.filter(record_type=BambooRecordType.SALE).aggregate(
			total=Coalesce(Sum("pending_amount"), Value(Decimal("0.00")))
		)["total"]
	)
	outstanding_receivables += material_outstanding
	manual_due_total = sum(
		(customer.manual_due_amount for customer in Customer.objects.filter(manual_due_amount__gt=0)),
		Decimal("0.00"),
	)
	outstanding_receivables += manual_due_total

	jcb_summary_raw = jcb_queryset.aggregate(
		total_work_hours_sum=Coalesce(Sum("total_work_hours"), Value(Decimal("0.00"))),
		total_jcb_income=Coalesce(
			Sum(
				Coalesce(
					F("total_amount"),
					ExpressionWrapper(
						F("total_work_hours") * F("rate"),
						output_field=DecimalField(max_digits=14, decimal_places=2),
					),
				),
				filter=Q(status=RecordStatus.PAID),
			),
			Value(Decimal("0.00")),
		),
		total_jcb_expense=Coalesce(Sum("expense_amount"), Value(Decimal("0.00"))),
	)
	jcb_summary = {
		"total_work_hours": jcb_summary_raw["total_work_hours_sum"],
		"total_jcb_income": jcb_summary_raw["total_jcb_income"],
		"total_jcb_expense": jcb_summary_raw["total_jcb_expense"],
	}
	jcb_summary["net_value"] = jcb_summary["total_jcb_income"] - jcb_summary["total_jcb_expense"]
	jcb_summary["outstanding_receivables"] = jcb_queryset.filter(
		status=RecordStatus.PENDING,
		total_work_hours__gt=0,
	).aggregate(
		total=Coalesce(
			Sum(
				Coalesce(
					F("total_amount"),
					ExpressionWrapper(
						F("total_work_hours") * F("rate"),
						output_field=DecimalField(max_digits=14, decimal_places=2),
					),
				),
			),
			Value(Decimal("0.00")),
		),
	)["total"]

	today = timezone.localdate()
	overdue_sales = [
		sale
		for sale in all_time_sales_rows
		if (
			sale.status == RecordStatus.PENDING
			and sale.alert_enabled
			and sale.due_date
			and sale.due_date < today
			and sale.total_amount > sale.received_total
		)
	]
	overdue_count = len(overdue_sales)
	overdue_amount = sum(
		((sale.total_amount - sale.received_total) for sale in overdue_sales),
		Decimal("0.00"),
	)

	recent_sales = sales_amount_queryset.order_by("-date", "-created_at")[:6]
	recent_transactions = transactions_queryset.order_by("-date", "-created_at")[:6]
	recent_customers = Customer.objects.order_by("-created_at")[:6]

	trend_rows = (
		sales_amount_queryset.values("date")
		.annotate(total=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))))
		.order_by("date")
	)
	calendar_mode = get_calendar_mode(request)
	sales_trend_labels = [date_to_calendar_input(row["date"], calendar_mode) for row in trend_rows]
	sales_trend_values = [float(row["total"]) for row in trend_rows]

	income_vs_expense_values = [
		float(kpi_income_expense["total_income"]),
		float(kpi_income_expense["total_expenses"]),
	]

	top_customer_rows = (
		sales_amount_queryset.filter(customer__isnull=False)
		.values("customer_id", "customer__name")
		.annotate(total=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))))
		.order_by("-total", "customer__name", "customer_id")[:10]
	)
	top_customer_labels = [row["customer__name"] for row in top_customer_rows]
	top_customer_values = [float(row["total"]) for row in top_customer_rows]

	# Pending receivables ranking should always reflect overall outstanding dues,
	# independent from dashboard date filters.
	all_time_sales_queryset = _dashboard_base_sales_queryset()
	all_time_sales_rows = list(all_time_sales_queryset)

	pending_customer_totals = {}
	for sale in all_time_sales_rows:
		if sale.status != RecordStatus.PENDING:
			continue
		if not sale.customer_id:
			continue
		pending_amount = sale.total_amount - sale.received_total
		if pending_amount <= 0:
			continue
		customer_key = sale.customer_id
		if customer_key not in pending_customer_totals:
			pending_customer_totals[customer_key] = {
				"name": sale.customer.name,
				"total": Decimal("0.00"),
			}
		pending_customer_totals[customer_key]["total"] += pending_amount

	# Include legacy manually added due amounts in customer pending totals.
	for customer in Customer.objects.filter(manual_due_amount__gt=0).only("id", "name", "manual_due_amount"):
		customer_key = customer.id
		if customer_key not in pending_customer_totals:
			pending_customer_totals[customer_key] = {
				"name": customer.name,
				"total": Decimal("0.00"),
			}
		pending_customer_totals[customer_key]["total"] += customer.manual_due_amount

	for record in BlocksRecord.objects.select_related("customer").filter(
		record_type=BlocksRecordType.SALE,
		customer__isnull=False,
		pending_amount__gt=0,
	):
		customer_key = record.customer_id
		if customer_key not in pending_customer_totals:
			pending_customer_totals[customer_key] = {
				"name": record.customer.name,
				"total": Decimal("0.00"),
			}
		pending_customer_totals[customer_key]["total"] += record.pending_amount

	for record in CementRecord.objects.select_related("customer").filter(
		record_type=CementRecordType.SALE,
		customer__isnull=False,
		pending_amount__gt=0,
	):
		customer_key = record.customer_id
		if customer_key not in pending_customer_totals:
			pending_customer_totals[customer_key] = {
				"name": record.customer.name,
				"total": Decimal("0.00"),
			}
		pending_customer_totals[customer_key]["total"] += record.pending_amount

	for record in BambooRecord.objects.select_related("customer").filter(
		record_type=BambooRecordType.SALE,
		customer__isnull=False,
		pending_amount__gt=0,
	):
		customer_key = record.customer_id
		if customer_key not in pending_customer_totals:
			pending_customer_totals[customer_key] = {
				"name": record.customer.name,
				"total": Decimal("0.00"),
			}
		pending_customer_totals[customer_key]["total"] += record.pending_amount

	top_pending_customer_rows = sorted(
		pending_customer_totals.values(),
		key=lambda row: row["total"],
		reverse=True,
	)[:10]
	top_pending_customer_labels = [row["name"] for row in top_pending_customer_rows]
	top_pending_customer_values = [float(row["total"]) for row in top_pending_customer_rows]

	jcb_summary_labels = ["JCB Income", "JCB Expense"]
	jcb_summary_values = [
		float(jcb_summary["total_jcb_income"]),
		float(jcb_summary["total_jcb_expense"]),
	]

	# Category breakdown for expenses (for pie chart)
	expense_by_category = (
		transactions_queryset.filter(type=TransactionType.EXPENSE)
		.values("category__name")
		.annotate(total=Coalesce(Sum("amount"), Value(Decimal("0.00"))))
		.order_by("-total")
	)
	category_expense_labels = [row["category__name"] or "Uncategorized" for row in expense_by_category]
	category_expense_values = [float(row["total"]) for row in expense_by_category]

	# Category breakdown for income (for pie chart)
	income_by_category = (
		transactions_queryset.filter(type=TransactionType.INCOME)
		.values("category__name")
		.annotate(total=Coalesce(Sum("amount"), Value(Decimal("0.00"))))
		.order_by("-total")
	)
	category_income_labels = [row["category__name"] or "Uncategorized" for row in income_by_category]
	category_income_values = [float(row["total"]) for row in income_by_category]

	tipper_summary = tipper_queryset.aggregate(
		total_expense=Coalesce(
			Sum("amount", filter=Q(record_type=TipperRecordType.EXPENSE)),
			Value(Decimal("0.00")),
		),
		total_value_added=Coalesce(
			Sum("amount", filter=Q(record_type=TipperRecordType.VALUE_ADDED)),
			Value(Decimal("0.00")),
		),
	)
	tipper_summary_labels = ["Expense", "Value Added"]
	tipper_summary_values = [
		float(tipper_summary["total_expense"]),
		float(tipper_summary["total_value_added"]),
	]
	tipper_summary["net_value"] = tipper_summary["total_value_added"] - tipper_summary["total_expense"]

	net_income = kpi_income_expense["total_income"] - kpi_income_expense["total_expenses"]

	# Blocks Records Summary
	blocks_summary_raw = blocks_queryset.aggregate(
		total_investment=Coalesce(Sum("investment"), Value(Decimal("0.00"))),
		total_sale_income=Coalesce(
			Sum(
				"sale_income",
				filter=Q(
					record_type=BlocksRecordType.SALE,
					payment_status=RecordStatus.PAID,
				),
			),
			Value(Decimal("0.00")),
		),
	)
	blocks_summary = {
		"total_investment": blocks_summary_raw["total_investment"],
		"total_sale_income": blocks_summary_raw["total_sale_income"],
	}
	blocks_summary["net_value"] = blocks_summary["total_sale_income"] - blocks_summary["total_investment"]
	
	# Calculate available stock by unit type
	from django.db import models as django_models
	stock_by_unit = blocks_queryset.filter(
		record_type=BlocksRecordType.STOCK
	).values("unit_type").annotate(
		total_quantity=Coalesce(Sum("quantity"), Value(0))
	).order_by("unit_type")
	
	blocks_summary["four_inch_stock"] = next(
		(row["total_quantity"] for row in stock_by_unit if row["unit_type"] == BlocksUnitType.FOUR_INCH), 
		0
	)
	blocks_summary["six_inch_stock"] = next(
		(row["total_quantity"] for row in stock_by_unit if row["unit_type"] == BlocksUnitType.SIX_INCH), 
		0
	)
	
	# Deduct sold quantities
	sold_by_unit = blocks_queryset.filter(
		record_type=BlocksRecordType.SALE
	).values("unit_type").annotate(
		total_quantity=Coalesce(Sum("quantity"), Value(0))
	).order_by("unit_type")
	
	four_inch_sold = next(
		(row["total_quantity"] for row in sold_by_unit if row["unit_type"] == BlocksUnitType.FOUR_INCH), 
		0
	)
	six_inch_sold = next(
		(row["total_quantity"] for row in sold_by_unit if row["unit_type"] == BlocksUnitType.SIX_INCH), 
		0
	)
	
	blocks_summary["four_inch_stock"] -= four_inch_sold
	blocks_summary["six_inch_stock"] -= six_inch_sold
	
	blocks_summary_labels = ["Investment", "Sale Income"]
	blocks_summary_values = [
		float(blocks_summary["total_investment"]),
		float(blocks_summary["total_sale_income"]),
	]

	# Cement Records Summary
	cement_summary_raw = cement_queryset.aggregate(
		total_investment=Coalesce(Sum("investment"), Value(Decimal("0.00"))),
		total_sale_income=Coalesce(
			Sum(
				"sale_income",
				filter=Q(
					record_type=CementRecordType.SALE,
					payment_status=RecordStatus.PAID,
				),
			),
			Value(Decimal("0.00")),
		),
	)
	cement_summary = {
		"total_investment": cement_summary_raw["total_investment"],
		"total_sale_income": cement_summary_raw["total_sale_income"],
	}
	cement_summary["net_value"] = cement_summary["total_sale_income"] - cement_summary["total_investment"]
	cement_stock_by_unit = cement_queryset.filter(
		record_type=CementRecordType.STOCK
	).values("unit_type").annotate(
		total_quantity=Coalesce(Sum("quantity"), Value(0))
	).order_by("unit_type")
	cement_summary["ppc_stock"] = next(
		(row["total_quantity"] for row in cement_stock_by_unit if row["unit_type"] == CementUnitType.PPC),
		0,
	)
	cement_summary["opc_stock"] = next(
		(row["total_quantity"] for row in cement_stock_by_unit if row["unit_type"] == CementUnitType.OPC),
		0,
	)
	cement_sold_by_unit = cement_queryset.filter(
		record_type=CementRecordType.SALE
	).values("unit_type").annotate(
		total_quantity=Coalesce(Sum("quantity"), Value(0))
	).order_by("unit_type")
	ppc_sold = next(
		(row["total_quantity"] for row in cement_sold_by_unit if row["unit_type"] == CementUnitType.PPC),
		0,
	)
	opc_sold = next(
		(row["total_quantity"] for row in cement_sold_by_unit if row["unit_type"] == CementUnitType.OPC),
		0,
	)
	cement_summary["ppc_stock"] -= ppc_sold
	cement_summary["opc_stock"] -= opc_sold
	cement_summary_labels = ["Investment", "Sale Income"]
	cement_summary_values = [
		float(cement_summary["total_investment"]),
		float(cement_summary["total_sale_income"]),
	]

	# Bamboo Records Summary
	bamboo_summary_raw = bamboo_queryset.aggregate(
		total_investment=Coalesce(Sum("investment"), Value(Decimal("0.00"))),
		total_sale_income=Coalesce(
			Sum(
				"sale_income",
				filter=Q(
					record_type=BambooRecordType.SALE,
					payment_status=RecordStatus.PAID,
				),
			),
			Value(Decimal("0.00")),
		),
	)
	bamboo_summary = {
		"total_investment": bamboo_summary_raw["total_investment"],
		"total_sale_income": bamboo_summary_raw["total_sale_income"],
	}
	bamboo_summary["net_value"] = bamboo_summary["total_sale_income"] - bamboo_summary["total_investment"]
	bamboo_stock_total = bamboo_queryset.filter(record_type=BambooRecordType.STOCK).aggregate(
		total_quantity=Coalesce(Sum("quantity"), Value(0))
	)["total_quantity"]
	bamboo_sold_total = bamboo_queryset.filter(record_type=BambooRecordType.SALE).aggregate(
		total_quantity=Coalesce(Sum("quantity"), Value(0))
	)["total_quantity"]
	bamboo_summary["available_stock"] = bamboo_stock_total - bamboo_sold_total
	bamboo_summary_values = [
		float(bamboo_summary["total_investment"]),
		float(bamboo_summary["total_sale_income"]),
	]

	return {
		"kpis": {
			"total_sales": kpi_sales["total_sales"],
			"received_total": received_total,
			"total_income": kpi_income_expense["total_income"],
			"total_expenses": kpi_income_expense["total_expenses"],
			"net_income": net_income,
			"outstanding_receivables": outstanding_receivables,
			"overdue_count": overdue_count,
			"overdue_amount": overdue_amount,
		},
		"recent_sales": recent_sales,
		"recent_transactions": recent_transactions,
		"recent_customers": recent_customers,
		"sales_trend_labels": sales_trend_labels,
		"sales_trend_values": sales_trend_values,
		"income_expense_labels": ["Income", "Expense"],
		"income_expense_values": income_vs_expense_values,
		"jcb_summary": jcb_summary,
		"jcb_summary_labels": jcb_summary_labels,
		"jcb_summary_values": jcb_summary_values,
		"top_customer_labels": top_customer_labels,
		"top_customer_values": top_customer_values,
		"top_pending_customer_labels": top_pending_customer_labels,
		"top_pending_customer_values": top_pending_customer_values,
		"category_expense_labels": category_expense_labels,
		"category_expense_values": category_expense_values,
		"category_income_labels": category_income_labels,
		"category_income_values": category_income_values,
		"tipper_summary": tipper_summary,
		"tipper_summary_labels": tipper_summary_labels,
		"tipper_summary_values": tipper_summary_values,
		"blocks_summary": blocks_summary,
		"blocks_summary_labels": blocks_summary_labels,
		"blocks_summary_values": blocks_summary_values,
		"cement_summary": cement_summary,
		"cement_summary_labels": cement_summary_labels,
		"cement_summary_values": cement_summary_values,
		"bamboo_summary": bamboo_summary,
		"bamboo_summary_labels": ["Investment", "Sale Income"],
		"bamboo_summary_values": bamboo_summary_values,
		"filters": {
			"date_from": date_from,
			"date_to": date_to,
		},
	}


def _sync_sale_payment_fields(sale, total_received=None):
	if total_received is None:
		summary = sale.receipts.filter(type=TransactionType.INCOME).aggregate(
			total=Coalesce(Sum("amount"), Value(Decimal("0.00")))
		)
		total_received = summary["total"]

	sale.paid_amount = total_received
	sale.status = RecordStatus.PAID if total_received >= sale.total_amount else RecordStatus.PENDING
	if sale.status == RecordStatus.PAID:
		sale.alert_enabled = False
	sale.save(update_fields=["paid_amount", "status", "alert_enabled", "updated_at"])


def _redirect_to_next_or_default(request, default_name, **kwargs):
	next_url = request.POST.get("next", "").strip()
	if next_url and url_has_allowed_host_and_scheme(
		next_url,
		allowed_hosts={request.get_host()},
		require_https=request.is_secure(),
	):
		return redirect(next_url)
	return redirect(default_name, **kwargs)


def _sync_paid_sale_income_entry(sale, income_date=None, force_paid=False):
	auto_sale_category = _get_or_create_predefined_category(AUTO_SALE_INCOME_CATEGORY)
	auto_income_qs = Transaction.objects.filter(
		sale=sale,
		type=TransactionType.INCOME,
		category=auto_sale_category,
	)
	manual_income_total = sale.receipts.filter(type=TransactionType.INCOME).exclude(
		category=auto_sale_category
	).aggregate(
		total=Coalesce(Sum("amount"), Value(Decimal("0.00")))
	)["total"]

	should_reconcile_paid = force_paid or sale.status == RecordStatus.PAID
	shortfall = sale.total_amount - manual_income_total
	if shortfall < 0:
		shortfall = Decimal("0.00")

	if should_reconcile_paid and shortfall > 0:
		auto_income = auto_income_qs.order_by("created_at").first()
		description = f"{AUTO_SALE_INCOME_DESCRIPTION}: {sale.invoice_number}"
		entry_date = income_date or sale.date

		if auto_income:
			auto_income.date = entry_date
			auto_income.amount = shortfall
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
			auto_income_qs.exclude(pk=auto_income.pk).delete()
			return

		Transaction.objects.create(
			date=entry_date,
			amount=shortfall,
			type=TransactionType.INCOME,
			category=auto_sale_category,
			description=description,
			customer=sale.customer,
			sale=sale,
		)
		return

	auto_income_qs.delete()


def _sync_sale_initial_payment_receipt(sale, paid_amount, receipt_date=None):
	initial_payment_category = _get_or_create_predefined_category(SALE_INITIAL_PAYMENT_CATEGORY)
	target_paid_amount = (paid_amount or Decimal("0.00")).quantize(Decimal("0.01"))
	if target_paid_amount < 0:
		target_paid_amount = Decimal("0.00")
	if sale.total_amount > 0 and target_paid_amount > sale.total_amount:
		target_paid_amount = sale.total_amount

	initial_receipts = Transaction.objects.filter(
		sale=sale,
		type=TransactionType.INCOME,
		category=initial_payment_category,
	)

	if target_paid_amount <= 0:
		initial_receipts.delete()
		return Decimal("0.00")

	description = f"Initial payment for sale {sale.invoice_number}"
	entry_date = receipt_date or sale.date
	initial_receipt = initial_receipts.order_by("created_at").first()

	if initial_receipt:
		initial_receipt.date = entry_date
		initial_receipt.amount = target_paid_amount
		initial_receipt.customer = sale.customer
		initial_receipt.description = description
		initial_receipt.save(update_fields=["date", "amount", "customer", "description", "updated_at"])
		initial_receipts.exclude(pk=initial_receipt.pk).delete()
		return target_paid_amount

	Transaction.objects.create(
		date=entry_date,
		amount=target_paid_amount,
		type=TransactionType.INCOME,
		category=initial_payment_category,
		description=description,
		customer=sale.customer,
		sale=sale,
	)
	return target_paid_amount


def _auto_allocate_customer_cash_entry(*, customer, payment_date, payment_amount, payment_method, notes=""):
	"""Apply a customer cash entry to oldest pending sales, then move excess to credit."""
	with db_transaction.atomic():
		customer = Customer.objects.select_for_update().get(pk=customer.pk)
		pending_sales = list(
			Sale.objects.select_for_update()
			.filter(customer=customer, status=RecordStatus.PENDING)
			.order_by("due_date", "date", "created_at", "id")
		)
		pending_blocks = list(
			BlocksRecord.objects.select_for_update()
			.filter(
				customer=customer,
				record_type=BlocksRecordType.SALE,
				pending_amount__gt=0,
			)
			.order_by("date", "created_at", "id")
		)
		pending_cement = list(
			CementRecord.objects.select_for_update()
			.filter(
				customer=customer,
				record_type=CementRecordType.SALE,
				pending_amount__gt=0,
			)
			.order_by("date", "created_at", "id")
		)
		pending_bamboo = list(
			BambooRecord.objects.select_for_update()
			.filter(
				customer=customer,
				record_type=BambooRecordType.SALE,
				pending_amount__gt=0,
			)
			.order_by("date", "created_at", "id")
		)

		customer_payment = CustomerPayment.objects.create(
			customer=customer,
			payment_date=payment_date,
			amount=payment_amount,
			payment_method=payment_method,
			notes=notes,
		)

		remaining_payment = payment_amount
		allocated_total = Decimal("0.00")
		fully_paid_count = 0
		partial_count = 0

		for sale in pending_sales:
			if remaining_payment <= 0:
				break

			sale_due = sale.total_amount - sale.paid_amount
			if sale_due <= 0:
				continue

			allocation_amount = min(remaining_payment, sale_due)
			if allocation_amount <= 0:
				continue

			description = f"Auto-allocated cash entry to invoice {sale.invoice_number}"
			if notes:
				description = f"{description} | {notes}"

			receipt = Transaction.objects.create(
				date=payment_date,
				amount=allocation_amount,
				type=TransactionType.INCOME,
				payment_method=payment_method,
				category=_get_or_create_predefined_category(PAYMENT_ALLOCATION_CATEGORY),
				description=description,
				customer=customer,
				sale=sale,
			)

			PaymentAllocation.objects.create(
				customer_payment=customer_payment,
				sale=sale,
				transaction=receipt,
				amount=allocation_amount,
			)

			remaining_payment -= allocation_amount
			allocated_total += allocation_amount

			_sync_sale_after_receipt_change(sale)

			sale.refresh_from_db(fields=["status", "paid_amount"])
			if sale.status == RecordStatus.PAID:
				fully_paid_count += 1
			else:
				partial_count += 1

		for record in [*pending_blocks, *pending_cement, *pending_bamboo]:
			if remaining_payment <= 0:
				break

			record_due = record.pending_amount
			if record_due <= 0:
				continue

			allocation_amount = min(remaining_payment, record_due)
			if allocation_amount <= 0:
				continue

			description = f"Auto-allocated cash entry to {record.__class__.__name__} sale #{record.id}"
			if notes:
				description = f"{description} | {notes}"

			_create_material_allocation_transaction(
				record,
				customer,
				payment_date,
				payment_method,
				description,
				allocation_amount,
				category_name=PAYMENT_ALLOCATION_CATEGORY,
				customer_payment=customer_payment,
			)

			remaining_payment -= allocation_amount
			allocated_total += allocation_amount

			_sync_material_sale_payment_fields(record)
			record.refresh_from_db(fields=["payment_status", "pending_amount"])
			if record.payment_status == RecordStatus.PAID:
				fully_paid_count += 1
			else:
				partial_count += 1

		if remaining_payment > 0:
			topup_description = "Unallocated customer payment added to customer credit balance"
			if notes:
				topup_description = f"{topup_description} | {notes}"

			Transaction.objects.create(
				date=payment_date,
				amount=remaining_payment,
				type=TransactionType.INCOME,
				payment_method=payment_method,
				category=_get_or_create_predefined_category(CREDIT_TOPUP_CATEGORY),
				description=topup_description,
				customer=customer,
			)
			customer.credit_balance = customer.credit_balance + remaining_payment
			customer.save(update_fields=["credit_balance", "updated_at"])

		customer_payment.allocated_amount = allocated_total
		customer_payment.unallocated_amount = remaining_payment
		customer_payment.save(update_fields=["allocated_amount", "unallocated_amount", "updated_at"])

		return {
			"allocated_total": allocated_total,
			"remaining_payment": remaining_payment,
			"fully_paid_count": fully_paid_count,
			"partial_count": partial_count,
		}


def _auto_apply_customer_credit_to_sale(sale, payment_date=None):
	"""Use available customer credit to settle a pending sale immediately."""
	if not sale.customer_id or sale.status != RecordStatus.PENDING:
		return Decimal("0.00")

	with db_transaction.atomic():
		customer = Customer.objects.select_for_update().get(pk=sale.customer_id)
		sale = Sale.objects.select_for_update().get(pk=sale.pk)

		if sale.status != RecordStatus.PENDING:
			return Decimal("0.00")

		sale_due = sale.total_amount - sale.paid_amount
		if customer.credit_balance <= 0 or sale_due <= 0:
			return Decimal("0.00")

		applied_amount = min(customer.credit_balance, sale_due)
		if applied_amount <= 0:
			return Decimal("0.00")

		Transaction.objects.create(
			date=payment_date or sale.date,
			amount=applied_amount,
			type=TransactionType.INCOME,
			category=_get_or_create_predefined_category(CREDIT_BALANCE_APPLIED_CATEGORY),
			description=f"Auto-applied from customer credit balance to invoice {sale.invoice_number}",
			customer=customer,
			sale=sale,
		)

		customer.credit_balance = customer.credit_balance - applied_amount
		customer.save(update_fields=["credit_balance", "updated_at"])

		_sync_sale_after_receipt_change(sale)
		return applied_amount


def _sync_sale_after_receipt_change(sale):
	# Keep sale payment fields and auto-income mirror aligned after receipt mutations.
	_sync_sale_payment_fields(sale)
	_sync_paid_sale_income_entry(sale)
	_sync_sale_payment_fields(sale)


def _material_transaction_binding(record):
	if isinstance(record, BlocksRecord):
		return {
			"txn_field": "blocks_record",
			"category": "Blocks Sale Income",
			"description": f"Income from {record.get_unit_type_display() or 'blocks'} sale ({record.quantity or 0} units @ {record.price_per_unit or Decimal('0.00')})",
		}
	if isinstance(record, CementRecord):
		return {
			"txn_field": "cement_record",
			"category": "Cement Sale Income",
			"description": f"Income from {record.get_unit_type_display() or 'cement'} sale ({record.quantity or 0} units @ {record.price_per_unit or Decimal('0.00')})",
		}
	if isinstance(record, BambooRecord):
		return {
			"txn_field": "bamboo_record",
			"category": "Bamboo Sale Income",
			"description": f"Income from bamboo sale ({record.quantity or 0} units @ {record.price_per_unit or Decimal('0.00')})",
		}
	raise ValueError("Unsupported material record type")


def _sync_material_sale_payment_fields(record):
	if not record.is_sale:
		return
	binding = _material_transaction_binding(record)
	txn_filter = {
		binding["txn_field"]: record,
		"type": TransactionType.INCOME,
	}
	total_received = Transaction.objects.filter(**txn_filter).aggregate(
		total=Coalesce(Sum("amount"), Value(Decimal("0.00")))
	)["total"]

	record.paid_amount = total_received
	record.save(update_fields=["paid_amount", "pending_amount", "payment_status", "sale_income", "bs_date", "updated_at"])


def _reconcile_material_sale_income_transaction(record):
	"""Align the module auto-income entry with form-level paid amount while preserving manual allocations."""
	if not record.is_sale:
		return
	binding = _material_transaction_binding(record)
	category = _get_or_create_predefined_category(binding["category"])
	base_filter = {
		binding["txn_field"]: record,
		"type": TransactionType.INCOME,
	}
	auto_qs = Transaction.objects.filter(**base_filter, category=category)
	non_auto_total = Transaction.objects.filter(**base_filter).exclude(category=category).aggregate(
		total=Coalesce(Sum("amount"), Value(Decimal("0.00")))
	)["total"]

	target_paid = min(record.paid_amount or Decimal("0.00"), record.sale_income or Decimal("0.00"))
	remaining_for_auto = target_paid - non_auto_total
	if remaining_for_auto < 0:
		remaining_for_auto = Decimal("0.00")

	if remaining_for_auto > 0:
		auto_entry = auto_qs.order_by("created_at").first()
		if auto_entry:
			auto_entry.date = record.date
			auto_entry.amount = remaining_for_auto
			auto_entry.customer = record.customer
			auto_entry.description = binding["description"]
			auto_entry.save(update_fields=["date", "amount", "customer", "description", "updated_at"])
			auto_qs.exclude(pk=auto_entry.pk).delete()
		else:
			create_kwargs = {
				"date": record.date,
				"amount": remaining_for_auto,
				"type": TransactionType.INCOME,
				"category": category,
				"description": binding["description"],
				"customer": record.customer,
				binding["txn_field"]: record,
			}
			Transaction.objects.create(**create_kwargs)
	else:
		auto_qs.delete()

	_sync_material_sale_payment_fields(record)


def _create_material_allocation_transaction(
	record,
	customer,
	payment_date,
	payment_method,
	description,
	amount,
	*,
	category_name=PAYMENT_ALLOCATION_CATEGORY,
	customer_payment=None,
):
	binding = _material_transaction_binding(record)
	txn_kwargs = {
		"date": payment_date,
		"amount": amount,
		"type": TransactionType.INCOME,
		"payment_method": payment_method,
		"category": _get_or_create_predefined_category(category_name),
		"description": description,
		"customer": customer,
		binding["txn_field"]: record,
	}
	transaction_obj = Transaction.objects.create(**txn_kwargs)
	if customer_payment is not None:
		# Keep parity with invoice allocations by preserving a transaction link to the payment event.
		transaction_obj.description = f"{description} [Customer Payment #{customer_payment.id}]"
		transaction_obj.save(update_fields=["description", "updated_at"])
	return transaction_obj


def _sync_jcb_transactions(jcb_record):
	jcb_income_category = _get_or_create_predefined_category(JCB_INCOME_CATEGORY)
	jcb_expense_category = _get_or_create_predefined_category(JCB_EXPENSE_CATEGORY)
	income_description = f"JCB work on {jcb_record.date} ({jcb_record.total_work_hours} hrs)"
	if jcb_record.site_name:
		income_description = f"{income_description} - {jcb_record.site_name}"
	expense_description = f"JCB expense on {jcb_record.date}: {jcb_record.expense_item}"

	income_qs = Transaction.objects.filter(
		jcb_record=jcb_record,
		type=TransactionType.INCOME,
		category=jcb_income_category,
	)

	if jcb_record.status == RecordStatus.PAID:
		income_txn = income_qs.order_by("created_at").first()
		income_amount = jcb_record.income_amount
		if income_txn:
			income_txn.date = jcb_record.date
			income_txn.amount = income_amount
			income_txn.payment_method = PaymentMethod.CASH
			income_txn.description = income_description
			income_txn.save(
				update_fields=[
					"date",
					"amount",
					"payment_method",
					"description",
					"updated_at",
				]
			)
			income_qs.exclude(pk=income_txn.pk).delete()
		else:
			Transaction.objects.create(
				date=jcb_record.date,
				amount=income_amount,
				type=TransactionType.INCOME,
				payment_method=PaymentMethod.CASH,
				category=jcb_income_category,
				description=income_description,
				jcb_record=jcb_record,
			)
	else:
		income_qs.delete()

	expense_qs = Transaction.objects.filter(
		jcb_record=jcb_record,
		type=TransactionType.EXPENSE,
		category=jcb_expense_category,
	)

	if jcb_record.expense_item and jcb_record.expense_amount and jcb_record.expense_amount > 0:
		expense_txn = expense_qs.order_by("created_at").first()
		if expense_txn:
			expense_txn.date = jcb_record.date
			expense_txn.amount = jcb_record.expense_amount
			expense_txn.payment_method = PaymentMethod.CASH
			expense_txn.description = expense_description
			expense_txn.save(
				update_fields=[
					"date",
					"amount",
					"payment_method",
					"description",
					"updated_at",
				]
			)
			expense_qs.exclude(pk=expense_txn.pk).delete()
		else:
			Transaction.objects.create(
				date=jcb_record.date,
				amount=jcb_record.expense_amount,
				type=TransactionType.EXPENSE,
				payment_method=PaymentMethod.CASH,
				category=jcb_expense_category,
				description=expense_description,
				jcb_record=jcb_record,
			)
	else:
		expense_qs.delete()


def _sync_tipper_expense_transaction(tipper_record):
	tipper_expense_category = _get_or_create_predefined_category(TIPPER_EXPENSE_CATEGORY)
	expense_qs = Transaction.objects.filter(
		tipper_record=tipper_record,
		type=TransactionType.EXPENSE,
		category=tipper_expense_category,
	)

	if tipper_record.record_type == TipperRecordType.EXPENSE and tipper_record.amount and tipper_record.amount > 0:
		expense_description = f"Tipper expense on {tipper_record.date}: {tipper_record.item.name}"
		if tipper_record.description:
			expense_description = f"{expense_description} - {tipper_record.description}"

		expense_txn = expense_qs.order_by("created_at").first()
		if expense_txn:
			expense_txn.date = tipper_record.date
			expense_txn.amount = tipper_record.amount
			expense_txn.payment_method = PaymentMethod.CASH
			expense_txn.description = expense_description
			expense_txn.save(
				update_fields=[
					"date",
					"amount",
					"payment_method",
					"description",
					"updated_at",
				]
			)
			expense_qs.exclude(pk=expense_txn.pk).delete()
		else:
			Transaction.objects.create(
				date=tipper_record.date,
				amount=tipper_record.amount,
				type=TransactionType.EXPENSE,
				payment_method=PaymentMethod.CASH,
				category=tipper_expense_category,
				description=expense_description,
				tipper_record=tipper_record,
			)
	else:
		expense_qs.delete()


def _sale_receipt_context(sale, request, form=None):
	receipts = sale.receipts.filter(type=TransactionType.INCOME).order_by("date", "created_at")
	receipt_rows = []
	running_received = Decimal("0.00")

	for receipt in receipts:
		running_received += receipt.amount
		receipt_rows.append(
			{
				"receipt": receipt,
				"running_balance": sale.total_amount - running_received,
			}
		)

	effective_received = sale.total_amount if sale.status == RecordStatus.PAID else running_received
	remaining_balance = sale.total_amount - effective_received
	if remaining_balance < 0:
		remaining_balance = Decimal("0.00")

	return {
		"sale": sale,
		"can_add_receipts": bool(sale.customer_id),
		"receipt_rows": receipt_rows,
		"receipt_form": form or SaleReceiptForm(**_form_calendar_mode_kwargs(request)),
		"total_received": effective_received,
		"remaining_balance": remaining_balance,
		"sale_status": sale.status,
	}


def _customer_payment_context(customer, request):
	sales = customer.sales.all().order_by("-date", "-created_at")
	pending_sales = (
		customer.sales.filter(status=RecordStatus.PENDING)
		.order_by("date", "created_at", "id")
	)
	sales_rows = []
	for sale in sales:
		due_amount = sale.total_amount - sale.paid_amount
		if due_amount < 0:
			due_amount = Decimal("0.00")
		sales_rows.append(
			{
				"sale": sale,
				"due_amount": due_amount,
			}
		)

	pending_sales_rows = []
	for sale in pending_sales:
		due_amount = sale.total_amount - sale.paid_amount
		if due_amount <= 0:
			continue
		pending_sales_rows.append(
			{
				"sale": sale,
				"due_amount": due_amount,
			}
		)

	pending_material_rows = _material_pending_rows_for_customer(customer)
	pending_material_total = sum((row["pending_amount"] for row in pending_material_rows), Decimal("0.00"))
	pending_item_count = len(pending_sales_rows) + len(pending_material_rows)

	pending_payment_rows = []
	for row in pending_sales_rows:
		sale = row["sale"]
		pending_payment_rows.append(
			{
				"source": "Invoice",
				"kind": "invoice",
				"reference": sale.invoice_number,
				"date": sale.date,
				"details": sale.notes or "-",
				"total": sale.total_amount,
				"paid": sale.paid_amount,
				"due": row["due_amount"],
				"status": sale.get_status_display(),
				"sale_id": sale.id,
			}
		)

	for row in pending_material_rows:
		record = row["record"]
		pending_payment_rows.append(
			{
				"source": row["label"],
				"kind": "module",
				"reference": f"#{record.id}",
				"date": record.date,
				"details": row["descriptor"],
				"total": record.sale_income or Decimal("0.00"),
				"paid": record.paid_amount or Decimal("0.00"),
				"due": row["pending_amount"],
				"status": record.get_payment_status_display(),
				"sale_id": None,
			}
		)

	pending_payment_rows.sort(key=lambda row: (row["date"], row["source"], row["reference"]))

	transaction_totals = customer.transactions.filter(type=TransactionType.INCOME).exclude(
		category__name=CREDIT_BALANCE_APPLIED_CATEGORY,
	).aggregate(
		total_income=Coalesce(Sum("amount"), Value(Decimal("0.00"))),
	)
	due_amount = _customer_due_amount_from_sales(customer)

	return {
		"sales": sales,
		"sales_rows": sales_rows,
		"pending_sales": pending_sales,
		"pending_sales_rows": pending_sales_rows,
		"pending_material_rows": pending_material_rows,
		"pending_material_total": pending_material_total,
		"pending_item_count": pending_item_count,
		"pending_payment_rows": pending_payment_rows,
		"total_payment": transaction_totals["total_income"],
		"due_amount": due_amount,
		"manual_due_amount": customer.manual_due_amount,
		"payment_method_choices": PaymentMethod.choices,
		"today": timezone.localdate(),
		"payment_date_value": date_to_calendar_input(timezone.localdate(), get_calendar_mode(request)),
	}


@login_required
def dashboard(request):
	default_from, default_to = _get_default_date_range()
	date_filters = _resolve_request_date_filters(
		request,
		default_from=default_from,
		default_to=default_to,
	)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]
	context = _dashboard_context(request=request, date_from=date_from, date_to=date_to)
	# Pass filters for template default values
	context["filters"] = {
		"date_from": date_filters["date_from_display"],
		"date_to": date_filters["date_to_display"],
	}

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/dashboard_content.html", context)
	return render(request, "core/dashboard.html", context)


@login_required
def export_report(request):
	if not (request.user.is_staff or request.user.is_superuser):
		return HttpResponse(status=403)

	report_name = request.GET.get("report", "").strip()
	export_format = request.GET.get("format", "csv").strip().lower()

	try:
		response = build_export_response(report_name, export_format, request.GET)
	except ValueError as error:
		logger.warning("Export request rejected: %s (%s)", report_name, error)
		return HttpResponse("Unsupported export request.", status=400)

	logger.info(
		"Exported report=%s format=%s user=%s",
		report_name,
		export_format,
		request.user.get_username(),
	)
	return response


@login_required
def cash_entries(request):
	default_from, default_to = _get_default_date_range()
	transactions = Transaction.objects.select_related("customer", "sale", "bamboo_record", "cement_record", "jcb_record", "tipper_record", "category").exclude(
		category__name=CREDIT_BALANCE_APPLIED_CATEGORY,
	)

	query = request.GET.get("q", "").strip()
	date_filters = _resolve_request_date_filters(
		request,
		default_from=default_from,
		default_to=default_to,
	)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]
	transaction_type = request.GET.get("type", "").strip()
	payment_method = request.GET.get("payment_method", "").strip()
	category_id = request.GET.get("category", "").strip()
	sort = request.GET.get("sort", "-date")

	if query:
		transactions = transactions.filter(
			Q(category__name__icontains=query)
			| Q(description__icontains=query)
			| Q(customer__name__icontains=query)
		)
	if date_from:
		transactions = transactions.filter(date__gte=date_from)
	if date_to:
		transactions = transactions.filter(date__lte=date_to)
	if transaction_type:
		transactions = transactions.filter(type=transaction_type)
	if payment_method:
		transactions = transactions.filter(payment_method=payment_method)
	if category_id:
		transactions = transactions.filter(category_id=category_id)

	allowed_sorts = {
		"-date": "-date",
		"date": "date",
		"-amount": "-amount",
		"amount": "amount",
		"customer": "customer__name",
		"-customer": "-customer__name",
	}
	transactions = transactions.order_by(allowed_sorts.get(sort, "-date"), "-created_at")

	paginator = Paginator(transactions, 20)
	page_obj = paginator.get_page(request.GET.get("page"))

	context = {
		"transactions": page_obj.object_list,
		"page_obj": page_obj,
		"payment_method_choices": PaymentMethod.choices,
		"categories": TransactionCategory.objects.all().order_by("name"),
		"filters": {
			"q": query,
			"date_from": date_filters["date_from_display"],
			"date_to": date_filters["date_to_display"],
			"type": transaction_type,
			"payment_method": payment_method,
			"category": category_id,
			"sort": sort,
		},
	}

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/transaction_table.html", context)
	return render(request, "core/cash_entries.html", context)


@login_required
def transaction_detail(request, pk):
	transaction_obj = get_object_or_404(
		Transaction.objects.select_related("customer", "sale", "bamboo_record", "cement_record", "jcb_record", "tipper_record"),
		pk=pk,
	)

	context = {
		"transaction": transaction_obj,
	}
	return render(request, "core/transaction_detail.html", context)


@login_required
def transaction_create(request):
	if request.method == "POST":
		form = TransactionForm(request.POST, request.FILES, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			customer = form.cleaned_data.get("customer")
			transaction_type = form.cleaned_data.get("type")
			linked_sale = form.cleaned_data.get("sale")

			should_auto_allocate = (
				transaction_type == TransactionType.INCOME
				and customer is not None
				and linked_sale is None
			)

			if should_auto_allocate:
				allocation_result = _auto_allocate_customer_cash_entry(
					customer=customer,
					payment_date=form.cleaned_data["date"],
					payment_amount=form.cleaned_data["amount"],
					payment_method=form.cleaned_data.get("payment_method") or PaymentMethod.CASH,
					notes=(form.cleaned_data.get("description") or "").strip(),
				)

				summary_text = (
					f"Auto-allocated payment: NPR {allocation_result['allocated_total']}. "
					f"{allocation_result['fully_paid_count']} fully paid, "
					f"{allocation_result['partial_count']} partially paid."
				)
				if allocation_result["remaining_payment"] > 0:
					summary_text += (
						f" NPR {allocation_result['remaining_payment']} added to customer credit balance."
					)
				messages.success(request, summary_text)
				return redirect("cash_entries")

			transaction_obj = form.save()
			if transaction_obj.sale_id:
				linked_sale = Sale.objects.filter(pk=transaction_obj.sale_id).first()
				if linked_sale:
					_sync_sale_after_receipt_change(linked_sale)
			messages.success(request, "Transaction created successfully.")
			return redirect("cash_entries")
		messages.error(request, "Please fix the errors below.")
	else:
		form = TransactionForm(**_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/transaction_form.html",
		{
			"form": form,
			"form_title": "Add Cash Entry",
			"submit_label": "Create Entry",
			"has_customers": Customer.objects.exists(),
		},
	)


@login_required
def transaction_edit(request, pk):
	transaction = get_object_or_404(Transaction, pk=pk)
	original_sale_id = transaction.sale_id

	if request.method == "POST":
		form = TransactionForm(request.POST, request.FILES, instance=transaction, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			updated_transaction = form.save()
			sale_ids_to_sync = {sale_id for sale_id in [original_sale_id, updated_transaction.sale_id] if sale_id}
			for sale_id in sale_ids_to_sync:
				linked_sale = Sale.objects.filter(pk=sale_id).first()
				if linked_sale:
					_sync_sale_after_receipt_change(linked_sale)
			messages.success(request, "Transaction updated successfully.")
			return redirect("cash_entries")
		messages.error(request, "Please fix the errors below.")
	else:
		form = TransactionForm(instance=transaction, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/transaction_form.html",
		{
			"form": form,
			"form_title": "Edit Cash Entry",
			"submit_label": "Update Entry",
			"transaction": transaction,
			"has_customers": Customer.objects.exists(),
		},
	)


@login_required
def transaction_delete(request, pk):
	transaction_obj = get_object_or_404(Transaction, pk=pk)

	if request.method != "POST":
		return redirect("cash_entries")

	linked_sale_id = transaction_obj.sale_id
	entry_label = f"{transaction_obj.get_type_display()} entry on {transaction_obj.date}"

	try:
		transaction_obj.delete()
		if linked_sale_id:
			linked_sale = Sale.objects.filter(pk=linked_sale_id).first()
			if linked_sale:
				_sync_sale_after_receipt_change(linked_sale)
	except Exception:
		if request.headers.get("HX-Request"):
			return _htmx_feedback_response(
				"Unable to delete cash entry right now.",
				level="error",
				status=400,
			)
		messages.error(request, "Unable to delete cash entry right now.")
		return redirect("cash_entries")

	if request.headers.get("HX-Request"):
		return _htmx_feedback_response(f"Deleted {entry_label}.")

	messages.success(request, f"Deleted {entry_label}.")
	return redirect("cash_entries")


@login_required
def jcb_records(request):
	default_from, default_to = _get_default_date_range()
	queryset = JCBRecord.objects.all().annotate(
		income_amount_calc=Coalesce(
			F("total_amount"),
			ExpressionWrapper(
				F("total_work_hours") * F("rate"),
				output_field=DecimalField(max_digits=14, decimal_places=2),
			),
		)
	)

	query = request.GET.get("q", "").strip()
	status = request.GET.get("status", "").strip()
	date_filters = _resolve_request_date_filters(
		request,
		default_from=default_from,
		default_to=default_to,
	)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]
	sort = request.GET.get("sort", "-date")

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
	queryset = queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at")

	paginator = Paginator(queryset, 20)
	page_obj = paginator.get_page(request.GET.get("page"))

	context = {
		"jcb_records": page_obj.object_list,
		"page_obj": page_obj,
		"filters": {
			"q": query,
			"status": status,
			"date_from": date_filters["date_from_display"],
			"date_to": date_filters["date_to_display"],
			"sort": sort,
		},
	}

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/jcb_records_table.html", context)
	return render(request, "core/jcb_records.html", context)


@login_required
def jcb_record_create(request):
	if request.method == "POST":
		form = JCBRecordForm(request.POST, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			jcb_record = form.save()
			_sync_jcb_transactions(jcb_record)
			messages.success(request, "JCB record created successfully.")
			return redirect("jcb_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = JCBRecordForm(**_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/jcb_record_form.html",
		{
			"form": form,
			"form_title": "Add JCB Record",
			"submit_label": "Create Record",
		},
	)


@login_required
def jcb_record_edit(request, pk):
	jcb_record = get_object_or_404(JCBRecord, pk=pk)

	if request.method == "POST":
		form = JCBRecordForm(request.POST, instance=jcb_record, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			jcb_record = form.save()
			_sync_jcb_transactions(jcb_record)
			messages.success(request, "JCB record updated successfully.")
			return redirect("jcb_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = JCBRecordForm(instance=jcb_record, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/jcb_record_form.html",
		{
			"form": form,
			"form_title": "Edit JCB Record",
			"submit_label": "Update Record",
			"jcb_record": jcb_record,
		},
	)


@login_required
def jcb_record_delete(request, pk):
	jcb_record = get_object_or_404(JCBRecord, pk=pk)

	if request.method != "POST":
		return redirect("jcb_records")

	record_date = jcb_record.date
	redirect_url = request.POST.get("redirect_to", "").strip()

	try:
		with db_transaction.atomic():
			Transaction.objects.filter(jcb_record=jcb_record).delete()
			jcb_record.delete()
	except Exception:
		if request.headers.get("HX-Request"):
			return _htmx_feedback_response(
				"Unable to delete JCB record right now.",
				level="error",
				status=400,
			)
		messages.error(request, "Unable to delete JCB record right now.")
		return redirect("jcb_records")

	if request.headers.get("HX-Request"):
		return _htmx_feedback_response(
			f"Deleted JCB record from {record_date}.",
			redirect_url=redirect_url,
		)

	messages.success(request, f"Deleted JCB record from {record_date}.")
	return redirect("jcb_records")


@login_required
def jcb_record_mark_paid(request, pk):
	jcb_record = get_object_or_404(JCBRecord, pk=pk)

	if request.method != "POST":
		return _redirect_to_next_or_default(request, "jcb_records")

	if jcb_record.status == RecordStatus.PAID:
		messages.info(request, "JCB record is already marked as paid.")
		return _redirect_to_next_or_default(request, "jcb_records")

	jcb_record.status = RecordStatus.PAID
	jcb_record.save(update_fields=["status", "updated_at"])
	_sync_jcb_transactions(jcb_record)

	messages.success(request, f"JCB record on {jcb_record.date} marked as paid.")
	return _redirect_to_next_or_default(request, "jcb_records")


@login_required
def tipper_records(request):
	default_from, default_to = _get_default_date_range()
	queryset = TipperRecord.objects.select_related("item").all()

	query = request.GET.get("q", "").strip()
	date_filters = _resolve_request_date_filters(
		request,
		default_from=default_from,
		default_to=default_to,
	)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]
	record_type = request.GET.get("record_type", "").strip()
	item_id = request.GET.get("item", "").strip()
	sort = request.GET.get("sort", "-date")

	if query:
		queryset = queryset.filter(Q(item__name__icontains=query) | Q(description__icontains=query))
	if date_from:
		queryset = queryset.filter(date__gte=date_from)
	if date_to:
		queryset = queryset.filter(date__lte=date_to)
	if record_type:
		queryset = queryset.filter(record_type=record_type)
	if item_id:
		queryset = queryset.filter(item_id=item_id)

	allowed_sorts = {
		"-date": "-date",
		"date": "date",
		"-amount": "-amount",
		"amount": "amount",
		"item": "item__name",
		"-item": "-item__name",
	}
	queryset = queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at")

	paginator = Paginator(queryset, 20)
	page_obj = paginator.get_page(request.GET.get("page"))

	context = {
		"tipper_records": page_obj.object_list,
		"page_obj": page_obj,
		"items": TipperItem.objects.order_by("name"),
		"record_type_choices": TipperRecordType.choices,
		"filters": {
			"q": query,
			"date_from": date_filters["date_from_display"],
			"date_to": date_filters["date_to_display"],
			"record_type": record_type,
			"item": item_id,
			"sort": sort,
		},
	}

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/tipper_records_table.html", context)
	return render(request, "core/tipper_records.html", context)


@login_required
def tipper_record_detail(request, pk):
	tipper_record = get_object_or_404(TipperRecord.objects.select_related("item"), pk=pk)
	return render(request, "core/tipper_record_detail.html", {"tipper_record": tipper_record})


@login_required
def tipper_record_create(request):
	if request.method == "POST":
		form = TipperRecordForm(request.POST, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			with db_transaction.atomic():
				tipper_record = form.save()
				_sync_tipper_expense_transaction(tipper_record)
			messages.success(request, "Tipper record created successfully.")
			return redirect("tipper_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = TipperRecordForm(**_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/tipper_record_form.html",
		{
			"form": form,
			"form_title": "Add Tipper Record",
			"submit_label": "Create Record",
		},
	)


@login_required
def tipper_record_edit(request, pk):
	tipper_record = get_object_or_404(TipperRecord, pk=pk)

	if request.method == "POST":
		form = TipperRecordForm(request.POST, instance=tipper_record, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			with db_transaction.atomic():
				tipper_record = form.save()
				_sync_tipper_expense_transaction(tipper_record)
			messages.success(request, "Tipper record updated successfully.")
			return redirect("tipper_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = TipperRecordForm(instance=tipper_record, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/tipper_record_form.html",
		{
			"form": form,
			"form_title": "Edit Tipper Record",
			"submit_label": "Update Record",
			"tipper_record": tipper_record,
		},
	)


@login_required
def tipper_record_delete(request, pk):
	tipper_record = get_object_or_404(TipperRecord, pk=pk)

	if request.method != "POST":
		return redirect("tipper_records")

	record_label = f"{tipper_record.get_record_type_display()} on {tipper_record.date}"

	try:
		with db_transaction.atomic():
			Transaction.objects.filter(tipper_record=tipper_record, type=TransactionType.EXPENSE).delete()
			tipper_record.delete()
	except Exception:
		if request.headers.get("HX-Request"):
			return _htmx_feedback_response(
				"Unable to delete tipper record right now.",
				level="error",
				status=400,
			)
		messages.error(request, "Unable to delete tipper record right now.")
		return redirect("tipper_records")

	if request.headers.get("HX-Request"):
		return _htmx_feedback_response(f"Deleted tipper record: {record_label}.")

	messages.success(request, f"Deleted tipper record: {record_label}.")
	return redirect("tipper_records")


@login_required
def sales(request):
	default_from, default_to = _get_default_date_range()
	queryset = Sale.objects.select_related("customer").annotate(
		received_total=F("paid_amount"),
	)
	queryset = queryset.annotate(
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

	query = request.GET.get("q", "").strip()
	status = request.GET.get("status", "").strip()
	customer_id = request.GET.get("customer", "").strip()
	date_filters = _resolve_request_date_filters(
		request,
		default_from=default_from,
		default_to=default_to,
	)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]
	sort = request.GET.get("sort", "-date")

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
	queryset = queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at")

	paginator = Paginator(queryset, 20)
	page_obj = paginator.get_page(request.GET.get("page"))

	context = {
		"sales": page_obj.object_list,
		"page_obj": page_obj,
		"customers": Customer.objects.all(),
		"filters": {
			"q": query,
			"status": status,
			"customer": customer_id,
			"date_from": date_filters["date_from_display"],
			"date_to": date_filters["date_to_display"],
			"sort": sort,
		},
	}
	if request.headers.get("HX-Request"):
		return render(request, "core/partials/sales_table.html", context)
	return render(request, "core/sales.html", context)


@login_required
def sale_create(request):
	if request.method == "POST":
		form = SaleForm(request.POST, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			sale = form.save(commit=False)
			initial_paid_amount = form.cleaned_data.get("paid_amount") or Decimal("0.00")
			if sale.status == RecordStatus.PAID:
				sale.alert_enabled = False
			sale.save()
			if initial_paid_amount > 0:
				_sync_sale_initial_payment_receipt(sale, initial_paid_amount, receipt_date=sale.date)
				_sync_sale_after_receipt_change(sale)
			elif sale.status == RecordStatus.PAID:
				_sync_paid_sale_income_entry(sale, income_date=sale.date, force_paid=True)
				_sync_sale_payment_fields(sale)
			else:
				_sync_sale_payment_fields(sale)
			if sale.status == RecordStatus.PENDING and sale.customer_id:
				_auto_apply_customer_credit_to_sale(sale)
				sale.refresh_from_db(fields=["status", "paid_amount"])
			messages.success(request, "Sale created successfully.")
			return redirect("sale_detail", pk=sale.pk)
		messages.error(request, "Please fix the errors below.")
	else:
		form = SaleForm(**_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/sale_form.html",
		{
			"form": form,
			"form_title": "Add Sale",
			"submit_label": "Create Sale",
			"has_customers": Customer.objects.exists(),
		},
	)


@login_required
def sale_edit(request, pk):
	sale = get_object_or_404(Sale, pk=pk)

	if request.method == "POST":
		form = SaleForm(request.POST, instance=sale, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			sale = form.save(commit=False)
			initial_paid_amount = form.cleaned_data.get("paid_amount") or Decimal("0.00")
			if sale.status == RecordStatus.PAID:
				sale.alert_enabled = False
			sale.save()
			if initial_paid_amount > 0:
				_sync_sale_initial_payment_receipt(sale, initial_paid_amount, receipt_date=sale.date)
				_sync_sale_after_receipt_change(sale)
			elif sale.status == RecordStatus.PAID:
				_sync_paid_sale_income_entry(sale, income_date=sale.date, force_paid=True)
				_sync_sale_payment_fields(sale)
			else:
				_sync_sale_payment_fields(sale)
			if sale.status == RecordStatus.PENDING and sale.customer_id:
				_auto_apply_customer_credit_to_sale(sale)
				sale.refresh_from_db(fields=["status", "paid_amount"])
			messages.success(request, "Sale updated successfully.")
			return redirect("sale_detail", pk=sale.pk)
		messages.error(request, "Please fix the errors below.")
	else:
		form = SaleForm(instance=sale, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/sale_form.html",
		{
			"form": form,
			"form_title": "Edit Sale",
			"submit_label": "Update Sale",
			"has_customers": Customer.objects.exists(),
			"sale": sale,
		},
	)


@login_required
def sale_detail(request, pk):
	sale = get_object_or_404(Sale.objects.select_related("customer"), pk=pk)
	context = _sale_receipt_context(sale, request)
	return render(request, "core/sale_detail.html", context)


@login_required
def sale_delete(request, pk):
	sale = get_object_or_404(Sale, pk=pk)

	if request.method != "POST":
		return redirect("sale_detail", pk=sale.pk)

	invoice_number = sale.invoice_number
	redirect_url = request.POST.get("redirect_to", "").strip()

	try:
		with db_transaction.atomic():
			credit_applied_total = Decimal("0.00")
			if sale.customer_id:
				credit_applied_total = (
					Transaction.objects.filter(
						sale=sale,
						type=TransactionType.INCOME,
						category__name=CREDIT_BALANCE_APPLIED_CATEGORY,
					)
					.aggregate(total=Coalesce(Sum("amount"), Value(Decimal("0.00"))))["total"]
				)

				if credit_applied_total > 0:
					customer = Customer.objects.select_for_update().get(pk=sale.customer_id)
					customer.credit_balance = customer.credit_balance + credit_applied_total
					customer.save(update_fields=["credit_balance", "updated_at"])

			Transaction.objects.filter(sale=sale).delete()
			sale.delete()
	except Exception:
		if request.headers.get("HX-Request"):
			return _htmx_feedback_response(
				"Unable to delete sale right now.",
				level="error",
				status=400,
			)
		messages.error(request, "Unable to delete sale right now.")
		return redirect("sales")

	if request.headers.get("HX-Request"):
		return _htmx_feedback_response(
			f"Deleted sale {invoice_number}.",
			redirect_url=redirect_url,
		)

	messages.success(request, f"Deleted sale {invoice_number}.")
	return redirect("sales")


@login_required
def sale_toggle_alert(request, pk):
	sale = get_object_or_404(Sale.objects.select_related("customer"), pk=pk)

	if request.method != "POST":
		return _redirect_to_next_or_default(request, "sale_detail", pk=sale.pk)

	if sale.status != RecordStatus.PENDING:
		messages.error(request, "Alert status can only be changed for pending sales.")
		return _redirect_to_next_or_default(request, "sale_detail", pk=sale.pk)

	sale.alert_enabled = not sale.alert_enabled
	sale.save(update_fields=["alert_enabled", "updated_at"])

	state_label = "enabled" if sale.alert_enabled else "disabled"
	messages.success(request, f"Alerts {state_label} for invoice {sale.invoice_number}.")
	return _redirect_to_next_or_default(request, "sale_detail", pk=sale.pk)


@login_required
def sale_mark_paid(request, pk):
	sale = get_object_or_404(Sale.objects.select_related("customer"), pk=pk)

	if request.method != "POST":
		return _redirect_to_next_or_default(request, "sale_detail", pk=sale.pk)

	if sale.status == RecordStatus.PAID:
		messages.info(request, f"Invoice {sale.invoice_number} is already paid.")
		return _redirect_to_next_or_default(request, "sale_detail", pk=sale.pk)

	sale.status = RecordStatus.PAID
	sale.alert_enabled = False
	sale.due_date = None
	sale.save(update_fields=["status", "alert_enabled", "due_date", "updated_at"])
	_sync_paid_sale_income_entry(sale, income_date=timezone.localdate(), force_paid=True)
	_sync_sale_payment_fields(sale)

	messages.success(request, f"Invoice {sale.invoice_number} marked as paid.")
	return _redirect_to_next_or_default(request, "sale_detail", pk=sale.pk)


@login_required
def sale_receipt_create(request, pk):
	sale = get_object_or_404(Sale.objects.select_related("customer"), pk=pk)

	if request.method != "POST":
		return redirect("sale_detail", pk=sale.pk)

	if not sale.customer_id:
		messages.error(request, "Assign a customer to this sale before adding receipts.")
		context = _sale_receipt_context(sale, request)
		if request.headers.get("HX-Request"):
			return render(request, "core/partials/sale_receipts_panel.html", context, status=400)
		return render(request, "core/sale_detail.html", context, status=400)

	form = SaleReceiptForm(request.POST, **_form_calendar_mode_kwargs(request))
	if form.is_valid():
		receipt = form.save(commit=False)
		receipt.type = TransactionType.INCOME
		receipt.customer = sale.customer
		receipt.sale = sale
		if not receipt.category:
			receipt.category = _get_or_create_predefined_category("Sales Receipt")
		receipt.save()
		_sync_sale_after_receipt_change(sale)
		messages.success(request, "Cash receipt added to sale.")

		if request.headers.get("HX-Request"):
			context = _sale_receipt_context(sale, request)
			context["inline_success"] = "Cash receipt added to sale."
			return render(request, "core/partials/sale_receipts_panel.html", context)
		return redirect("sale_detail", pk=sale.pk)

	messages.error(request, "Please fix the receipt form errors.")
	context = _sale_receipt_context(sale, request, form=form)
	if request.headers.get("HX-Request"):
		return render(request, "core/partials/sale_receipts_panel.html", context, status=400)
	return render(request, "core/sale_detail.html", context, status=400)


@login_required
def customers(request):
	customer_sales_total = (
		Sale.objects.filter(customer=OuterRef("pk"))
		.values("customer")
		.annotate(total=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))))
		.values("total")[:1]
	)
	customer_payment_total = (
		Transaction.objects.filter(customer=OuterRef("pk"), type=TransactionType.INCOME)
		.exclude(category__name=CREDIT_BALANCE_APPLIED_CATEGORY)
		.values("customer")
		.annotate(total=Coalesce(Sum("amount"), Value(Decimal("0.00"))))
		.values("total")[:1]
	)
	queryset = Customer.objects.annotate(
		total_sales=Coalesce(Subquery(customer_sales_total, output_field=DecimalField(max_digits=14, decimal_places=2)), Value(Decimal("0.00"))),
		total_payments=Coalesce(Subquery(customer_payment_total, output_field=DecimalField(max_digits=14, decimal_places=2)), Value(Decimal("0.00"))),
	)
	query = request.GET.get("q", "").strip()
	customer_type = request.GET.get("type", "").strip()
	credit_status = request.GET.get("credit_status", "").strip()

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

	customers = list(queryset.order_by("name"))
	for customer in customers:
		customer.due_amount = _customer_due_amount_from_sales(customer)

	context = {
		"customers": customers,
		"filters": {
			"q": query,
			"type": customer_type,
			"credit_status": credit_status,
		},
	}
	return render(request, "core/customers.html", context)


@login_required
def customer_detail(request, pk):
	customer = get_object_or_404(Customer, pk=pk)
	transactions = customer.transactions.all().order_by("-date", "-created_at")

	context = {
		"customer": customer,
		"transactions": transactions,
	}
	context.update(_customer_payment_context(customer, request))
	return render(request, "core/customer_detail.html", context)


@login_required
def customer_allocate_payment(request, pk):
	customer = get_object_or_404(Customer, pk=pk)

	if request.method != "POST":
		return redirect("customer_detail", pk=customer.pk)

	raw_amount = request.POST.get("payment_amount", "").strip()
	raw_date = request.POST.get("payment_date", "").strip()
	payment_method = request.POST.get("payment_method", PaymentMethod.CASH).strip()
	allocation_mode = request.POST.get("allocation_mode", "cash").strip()
	use_credit_balance = allocation_mode == "credit"
	sale_ids = request.POST.getlist("sale_ids")
	blocks_sale_ids = request.POST.getlist("blocks_sale_ids")
	cement_sale_ids = request.POST.getlist("cement_sale_ids")
	bamboo_sale_ids = request.POST.getlist("bamboo_sale_ids")
	notes = request.POST.get("notes", "").strip()
	allocate_manual_due = request.POST.get("allocate_manual_due") == "on"

	try:
		payment_amount = Decimal(raw_amount)
	except Exception:
		payment_amount = Decimal("0.00")

	if not use_credit_balance and payment_amount <= 0:
		message = "Enter a valid payment amount greater than zero."
		if request.headers.get("HX-Request"):
			context = {"customer": customer, "allocation_error": message}
			context.update(_customer_payment_context(customer, request))
			return render(request, "core/partials/customer_payment_section.html", context)
		messages.error(request, message)
		return redirect("customer_detail", pk=customer.pk)

	if use_credit_balance and customer.credit_balance <= 0:
		message = "No available credit balance to allocate."
		if request.headers.get("HX-Request"):
			context = {"customer": customer, "allocation_error": message}
			context.update(_customer_payment_context(customer, request))
			return render(request, "core/partials/customer_payment_section.html", context)
		messages.error(request, message)
		return redirect("customer_detail", pk=customer.pk)

	if not sale_ids and not blocks_sale_ids and not cement_sale_ids and not bamboo_sale_ids and not allocate_manual_due:
		message = "Select at least one pending sale or manual due to allocate payment."
		if request.headers.get("HX-Request"):
			context = {"customer": customer, "allocation_error": message}
			context.update(_customer_payment_context(customer, request))
			return render(request, "core/partials/customer_payment_section.html", context)
		messages.error(request, message)
		return redirect("customer_detail", pk=customer.pk)

	if allocate_manual_due and customer.manual_due_amount <= 0:
		allocate_manual_due = False

	if payment_method not in dict(PaymentMethod.choices):
		payment_method = PaymentMethod.CASH

	payment_date = _resolve_posted_date(request, raw_date, fallback=timezone.localdate())

	with db_transaction.atomic():
		customer = Customer.objects.select_for_update().get(pk=customer.pk)
		selected_sales = list(
			Sale.objects.select_for_update()
			.filter(customer=customer, status=RecordStatus.PENDING, id__in=sale_ids)
			.order_by("date", "created_at", "id")
		)
		selected_blocks_sales = list(
			BlocksRecord.objects.select_for_update()
			.filter(
				customer=customer,
				record_type=BlocksRecordType.SALE,
				id__in=blocks_sale_ids,
				pending_amount__gt=0,
			)
			.order_by("date", "created_at", "id")
		)
		selected_cement_sales = list(
			CementRecord.objects.select_for_update()
			.filter(
				customer=customer,
				record_type=CementRecordType.SALE,
				id__in=cement_sale_ids,
				pending_amount__gt=0,
			)
			.order_by("date", "created_at", "id")
		)
		selected_bamboo_sales = list(
			BambooRecord.objects.select_for_update()
			.filter(
				customer=customer,
				record_type=BambooRecordType.SALE,
				id__in=bamboo_sale_ids,
				pending_amount__gt=0,
			)
			.order_by("date", "created_at", "id")
		)

		if not selected_sales and not selected_blocks_sales and not selected_cement_sales and not selected_bamboo_sales and not allocate_manual_due:
			message = "No eligible pending sales or manual due found for allocation."
			if request.headers.get("HX-Request"):
				context = {"customer": customer, "allocation_error": message}
				context.update(_customer_payment_context(customer, request))
				return render(request, "core/partials/customer_payment_section.html", context)
			messages.error(request, message)
			return redirect("customer_detail", pk=customer.pk)

		if use_credit_balance:
			customer_payment = None
			remaining_payment = customer.credit_balance
		else:
			customer_payment = CustomerPayment.objects.create(
				customer=customer,
				payment_date=payment_date,
				amount=payment_amount,
				payment_method=payment_method,
				notes=notes,
			)
			remaining_payment = payment_amount

		allocated_total = Decimal("0.00")
		fully_paid_count = 0
		partial_count = 0

		for sale in selected_sales:
			if remaining_payment <= 0:
				break

			sale_due = sale.total_amount - sale.paid_amount
			if sale_due <= 0:
				continue

			allocation_amount = min(remaining_payment, sale_due)
			if allocation_amount <= 0:
				continue

			if use_credit_balance:
				receipt = Transaction.objects.create(
					date=payment_date,
					amount=allocation_amount,
					type=TransactionType.INCOME,
					category=_get_or_create_predefined_category(CREDIT_BALANCE_APPLIED_CATEGORY),
					description=f"Allocated from customer credit balance to invoice {sale.invoice_number}",
					customer=customer,
					sale=sale,
				)
			else:
				receipt = Transaction.objects.create(
					date=payment_date,
					amount=allocation_amount,
					type=TransactionType.INCOME,
					category=_get_or_create_predefined_category(PAYMENT_ALLOCATION_CATEGORY),
					description=f"Allocated from customer payment to invoice {sale.invoice_number}",
					customer=customer,
					sale=sale,
				)

				PaymentAllocation.objects.create(
					customer_payment=customer_payment,
					sale=sale,
					transaction=receipt,
					amount=allocation_amount,
				)

			remaining_payment -= allocation_amount
			allocated_total += allocation_amount

			_sync_sale_after_receipt_change(sale)

			sale.refresh_from_db(fields=["status", "paid_amount"])
			if sale.status == RecordStatus.PAID:
				fully_paid_count += 1
			else:
				partial_count += 1

		for module_name, module_records in [
			("blocks", selected_blocks_sales),
			("cement", selected_cement_sales),
			("bamboo", selected_bamboo_sales),
		]:
			for record in module_records:
				if remaining_payment <= 0:
					break

				record_due = record.pending_amount
				if record_due <= 0:
					continue

				allocation_amount = min(remaining_payment, record_due)
				if allocation_amount <= 0:
					continue

				description = f"Allocated from customer payment to {module_name} sale #{record.id}"
				if use_credit_balance:
					description = f"Allocated from customer credit balance to {module_name} sale #{record.id}"

				transaction_category = CREDIT_BALANCE_APPLIED_CATEGORY if use_credit_balance else PAYMENT_ALLOCATION_CATEGORY
				payment_method_to_use = PaymentMethod.CASH if use_credit_balance else payment_method
				_create_material_allocation_transaction(
					record,
					customer,
					payment_date,
					payment_method_to_use,
					description,
					allocation_amount,
					category_name=transaction_category,
					customer_payment=customer_payment,
				)

				remaining_payment -= allocation_amount
				allocated_total += allocation_amount
				_sync_material_sale_payment_fields(record)
				record.refresh_from_db(fields=["payment_status", "pending_amount"])
				if record.payment_status == RecordStatus.PAID:
					fully_paid_count += 1
				else:
					partial_count += 1

		# Handle manually added due amounts
		manual_due_to_allocate = allocate_manual_due
		manual_due_amount_before = customer.manual_due_amount

		if manual_due_to_allocate and remaining_payment > 0 and customer.manual_due_amount > 0:
			# Allocate remaining payment to manual due amount
			allocate_to_manual_due = min(remaining_payment, customer.manual_due_amount)
			if allocate_to_manual_due > 0:
				manual_due_category = (
					CREDIT_BALANCE_APPLIED_CATEGORY if use_credit_balance else MANUAL_DUE_SETTLEMENT_CATEGORY
				)
				manual_due_description = (
					"Allocated from customer credit balance to manual due settlement"
					if use_credit_balance
					else "Allocated from customer payment to manual due settlement"
				)
				Transaction.objects.create(
					date=payment_date,
					amount=allocate_to_manual_due,
					type=TransactionType.INCOME,
					category=_get_or_create_predefined_category(manual_due_category),
					description=manual_due_description,
					customer=customer,
				)
			customer.manual_due_amount -= allocate_to_manual_due
			remaining_payment -= allocate_to_manual_due
			allocated_total += allocate_to_manual_due

		if customer_payment is not None:
			customer_payment.allocated_amount = allocated_total
			customer_payment.unallocated_amount = remaining_payment
			customer_payment.save(update_fields=["allocated_amount", "unallocated_amount", "updated_at"])
		elif allocated_total > 0:
			customer.credit_balance = max(Decimal("0.00"), customer.credit_balance - allocated_total)
			customer.save(update_fields=["credit_balance", "manual_due_amount", "updated_at"])
			remaining_payment = customer.credit_balance

		if (not use_credit_balance) and manual_due_to_allocate and customer.manual_due_amount < manual_due_amount_before:
			customer.save(update_fields=["manual_due_amount", "updated_at"])

		if (not use_credit_balance) and remaining_payment > 0:
			Transaction.objects.create(
				date=payment_date,
				amount=remaining_payment,
				type=TransactionType.INCOME,
				category=_get_or_create_predefined_category(CREDIT_TOPUP_CATEGORY),
				description="Unallocated customer payment added to customer credit balance",
				customer=customer,
			)
			customer.credit_balance = customer.credit_balance + remaining_payment
			customer.save(update_fields=["credit_balance", "updated_at"])
		elif not use_credit_balance:
			customer.save()

	if use_credit_balance:
		summary_text = (
			f"Credit allocated: NPR {allocated_total}. "
			f"{fully_paid_count} fully paid, {partial_count} partially paid."
		)
		if customer.credit_balance > 0:
			summary_text += f" Remaining credit balance: NPR {customer.credit_balance}."
	else:
		summary_text = (
			f"Payment allocated: NPR {allocated_total}. "
			f"{fully_paid_count} fully paid, {partial_count} partially paid."
		)
		if remaining_payment > 0:
			summary_text += f" NPR {remaining_payment} added to customer credit."

	if request.headers.get("HX-Request"):
		context = {
			"customer": customer,
			"allocation_success": summary_text,
		}
		context.update(_customer_payment_context(customer, request))
		return render(request, "core/partials/customer_payment_section.html", context)

	messages.success(request, summary_text)
	return redirect("customer_detail", pk=customer.pk)


@login_required
def customer_create(request):
	if request.method == "POST":
		form = CustomerForm(request.POST)
		if form.is_valid():
			customer = form.save()
			messages.success(request, "Customer created successfully.")
			return redirect("customer_detail", pk=customer.pk)
		messages.error(request, "Please fix the errors below.")
	else:
		form = CustomerForm()

	return render(
		request,
		"core/customer_form.html",
		{
			"form": form,
			"form_title": "Add Customer",
			"submit_label": "Create Customer",
		},
	)


@login_required
def customer_edit(request, pk):
	customer = get_object_or_404(Customer, pk=pk)

	if request.method == "POST":
		form = CustomerForm(request.POST, instance=customer)
		if form.is_valid():
			customer = form.save()
			messages.success(request, "Customer updated successfully.")
			return redirect("customer_detail", pk=customer.pk)
		messages.error(request, "Please fix the errors below.")
	else:
		form = CustomerForm(instance=customer)

	return render(
		request,
		"core/customer_form.html",
		{
			"form": form,
			"form_title": "Edit Customer",
			"submit_label": "Update Customer",
			"customer": customer,
		},
	)


@login_required
def customer_delete(request, pk):
	customer = get_object_or_404(Customer, pk=pk)

	if request.method != "POST":
		return redirect("customer_detail", pk=customer.pk)

	customer_name = customer.name
	redirect_url = request.POST.get("redirect_to", "").strip()

	try:
		customer.delete()
	except Exception:
		if request.headers.get("HX-Request"):
			return _htmx_feedback_response(
				"Unable to delete customer right now.",
				level="error",
				status=400,
			)
		messages.error(request, "Unable to delete customer right now.")
		return redirect("customers")

	if request.headers.get("HX-Request"):
		return _htmx_feedback_response(
			f"Deleted customer {customer_name}.",
			redirect_url=redirect_url,
		)

	messages.success(request, f"Deleted customer {customer_name}.")
	return redirect("customers")


@login_required
def alerts(request):
	alert_type = request.GET.get("type", "").strip()
	customer_id = request.GET.get("customer", "").strip()
	date_filters = _resolve_request_date_filters(request)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]

	AlertNotification.objects.filter(is_active=True, is_read=False).update(is_read=True)
	context = _alerts_context(alert_type, customer_id, date_from, date_to)
	context["filters"]["date_from"] = date_filters["date_from_display"]
	context["filters"]["date_to"] = date_filters["date_to_display"]

	if request.headers.get("HX-Request"):
		context["include_badge_oob"] = True
		return render(request, "core/partials/alerts_content.html", context)
	context["include_badge_oob"] = False
	return render(request, "core/alerts.html", context)


@login_required
def alerts_badge(request):
	context = {
		"alerts_badge_count": _alerts_badge_count(),
	}
	return render(request, "core/partials/alerts_badge.html", context)


@login_required
def alert_notification_resolve(request, pk):
	notification = get_object_or_404(AlertNotification, pk=pk)

	if request.method != "POST":
		return redirect("alerts")

	notification.is_active = False
	notification.is_read = True
	notification.resolved_at = timezone.now()
	notification.save(update_fields=["is_active", "is_read", "resolved_at", "updated_at"])
	messages.success(request, "Notification resolved.")

	alert_type = request.GET.get("type", "").strip()
	customer_id = request.GET.get("customer", "").strip()
	date_filters = _resolve_request_date_filters(request)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]
	context = _alerts_context(alert_type, customer_id, date_from, date_to)
	context["filters"]["date_from"] = date_filters["date_from_display"]
	context["filters"]["date_to"] = date_filters["date_to_display"]

	if request.headers.get("HX-Request"):
		context["include_badge_oob"] = True
		return render(request, "core/partials/alerts_content.html", context)
	return redirect("alerts")


@login_required
def manual_alert_create(request):
	if request.method == "POST":
		form = ManualAlertForm(request.POST, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			form.save()
			messages.success(request, "Manual alert created successfully.")
			return redirect("alerts")
		messages.error(request, "Please fix the errors below.")
	else:
		form = ManualAlertForm(initial={"due_date": timezone.localdate()}, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/manual_alert_form.html",
		{
			"form": form,
			"form_title": "Create Manual Alert",
			"submit_label": "Create Alert",
		},
	)


@login_required
def manual_alert_edit(request, pk):
	manual_alert = get_object_or_404(AlertNotification, pk=pk)
	if manual_alert.source_type != AlertSource.MANUAL:
		messages.error(request, "Only manual alerts can be edited.")
		return redirect("alerts")

	if request.method == "POST":
		form = ManualAlertForm(request.POST, instance=manual_alert, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			form.save()
			messages.success(request, "Manual alert updated successfully.")
			return redirect("alerts")
		messages.error(request, "Please fix the errors below.")
	else:
		form = ManualAlertForm(instance=manual_alert, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/manual_alert_form.html",
		{
			"form": form,
			"form_title": "Edit Manual Alert",
			"submit_label": "Update Alert",
		},
	)


@login_required
def manual_alert_delete(request, pk):
	manual_alert = get_object_or_404(AlertNotification, pk=pk)
	if manual_alert.source_type != AlertSource.MANUAL:
		messages.error(request, "Only manual alerts can be deleted.")
		return redirect("alerts")

	if request.method != "POST":
		return redirect("alerts")

	manual_alert.delete()
	messages.success(request, "Manual alert deleted successfully.")
	return redirect("alerts")


# BLOCKS RECORDS VIEWS

@login_required
def blocks_records(request):
	"""Display list of blocks records with filtering and pagination."""
	default_from, default_to = _get_default_date_range()
	queryset = BlocksRecord.objects.select_related("customer")

	query = request.GET.get("q", "").strip()
	record_type = request.GET.get("record_type", "").strip()
	payment_status = request.GET.get("payment_status", "").strip()
	unit_type = request.GET.get("unit_type", "").strip()
	date_filters = _resolve_request_date_filters(
		request,
		default_from=default_from,
		default_to=default_to,
	)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]
	sort = request.GET.get("sort", "-date")

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
	if unit_type:
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
	queryset = queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at")

	paginator = Paginator(queryset, 20)
	page_obj = paginator.get_page(request.GET.get("page"))

	context = {
		"blocks_records": page_obj.object_list,
		"page_obj": page_obj,
		"filters": {
			"q": query,
			"record_type": record_type,
			"payment_status": payment_status,
			"unit_type": unit_type,
			"date_from": date_filters["date_from_display"],
			"date_to": date_filters["date_to_display"],
			"sort": sort,
		},
		"record_type_choices": BlocksRecordType.choices,
		"payment_status_choices": RecordStatus.choices,
		"unit_type_choices": BlocksUnitType.choices,
	}

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/blocks_records_table.html", context)

	return render(request, "core/blocks_records.html", context)


@login_required
def blocks_record_create(request):
	"""Create a new blocks record."""
	if request.method == "POST":
		form = BlocksRecordForm(request.POST, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			blocks_record = form.save()
			
			# Only sale records create linked global income transactions.
			if blocks_record.is_sale:
				_reconcile_material_sale_income_transaction(blocks_record)
			
			messages.success(request, "Blocks record created successfully.")
			return redirect("blocks_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = BlocksRecordForm(initial={"date": timezone.localdate()}, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/blocks_record_form.html",
		{
			"form": form,
			"form_title": "Add Blocks Record",
			"submit_label": "Create Record",
		},
	)


@login_required
def blocks_record_edit(request, pk):
	"""Edit an existing blocks record."""
	blocks_record = get_object_or_404(BlocksRecord, pk=pk)

	if request.method == "POST":
		form = BlocksRecordForm(request.POST, instance=blocks_record, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			blocks_record = form.save()
			
			# Keep only sale-income sync behavior; investment never links to global expense.
			blocks_record.transactions.filter(type=TransactionType.INCOME).exclude(
				category=_get_or_create_predefined_category(PAYMENT_ALLOCATION_CATEGORY)
			).exclude(
				category=_get_or_create_predefined_category(CREDIT_BALANCE_APPLIED_CATEGORY)
			).delete()
			if blocks_record.is_sale:
				_reconcile_material_sale_income_transaction(blocks_record)
			
			messages.success(request, "Blocks record updated successfully.")
			return redirect("blocks_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = BlocksRecordForm(instance=blocks_record, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/blocks_record_form.html",
		{
			"form": form,
			"form_title": "Edit Blocks Record",
			"submit_label": "Update Record",
		},
	)


@login_required
def blocks_record_delete(request, pk):
	"""Delete a blocks record."""
	blocks_record = get_object_or_404(BlocksRecord, pk=pk)

	if request.method != "POST":
		return redirect("blocks_records")

	# Only sale-income transactions are managed by the blocks module.
	blocks_record.transactions.filter(type=TransactionType.INCOME).delete()
	blocks_record.delete()
	messages.success(request, "Blocks record deleted successfully.")
	return redirect("blocks_records")


@login_required
def blocks_record_mark_paid(request, pk):
	"""Mark a pending blocks sale record as paid."""
	blocks_record = get_object_or_404(BlocksRecord, pk=pk)

	if request.method != "POST":
		return _redirect_to_next_or_default(request, "blocks_records")

	if blocks_record.record_type != BlocksRecordType.SALE:
		messages.error(request, "Only sale records can be marked as paid.")
		return _redirect_to_next_or_default(request, "blocks_records")

	if blocks_record.payment_status == RecordStatus.PAID:
		messages.info(request, "Blocks record is already marked as paid.")
		return _redirect_to_next_or_default(request, "blocks_records")

	blocks_record.paid_amount = blocks_record.sale_income or Decimal("0.00")
	blocks_record.save()
	_reconcile_material_sale_income_transaction(blocks_record)
	messages.success(request, f"Blocks sale record on {blocks_record.date} marked as paid.")
	return _redirect_to_next_or_default(request, "blocks_records")


def _create_blocks_sale_transaction(blocks_record):
	"""Create transaction entries for blocks sale records."""
	_reconcile_material_sale_income_transaction(blocks_record)


# CEMENT RECORDS VIEWS


@login_required
def cement_records(request):
	"""Display list of cement records with filtering and pagination."""
	default_from, default_to = _get_default_date_range()
	queryset = CementRecord.objects.select_related("customer")

	query = request.GET.get("q", "").strip()
	record_type = request.GET.get("record_type", "").strip()
	payment_status = request.GET.get("payment_status", "").strip()
	unit_type = request.GET.get("unit_type", "").strip()
	date_filters = _resolve_request_date_filters(
		request,
		default_from=default_from,
		default_to=default_to,
	)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]
	sort = request.GET.get("sort", "-date")

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
	if unit_type:
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
	queryset = queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at")

	paginator = Paginator(queryset, 20)
	page_obj = paginator.get_page(request.GET.get("page"))

	context = {
		"cement_records": page_obj.object_list,
		"page_obj": page_obj,
		"filters": {
			"q": query,
			"record_type": record_type,
			"payment_status": payment_status,
			"unit_type": unit_type,
			"date_from": date_filters["date_from_display"],
			"date_to": date_filters["date_to_display"],
			"sort": sort,
		},
		"record_type_choices": CementRecordType.choices,
		"payment_status_choices": RecordStatus.choices,
		"unit_type_choices": CementUnitType.choices,
	}

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/cement_records_table.html", context)

	return render(request, "core/cement_records.html", context)


@login_required
def cement_record_create(request):
	"""Create a new cement record."""
	if request.method == "POST":
		form = CementRecordForm(request.POST, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			cement_record = form.save()
			if cement_record.is_sale:
				_reconcile_material_sale_income_transaction(cement_record)
			messages.success(request, "Cement record created successfully.")
			return redirect("cement_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = CementRecordForm(initial={"date": timezone.localdate()}, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/cement_record_form.html",
		{
			"form": form,
			"form_title": "Add Cement Record",
			"submit_label": "Create Record",
		},
	)


@login_required
def cement_record_edit(request, pk):
	"""Edit an existing cement record."""
	cement_record = get_object_or_404(CementRecord, pk=pk)

	if request.method == "POST":
		form = CementRecordForm(request.POST, instance=cement_record, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			cement_record = form.save()
			cement_record.transactions.filter(type=TransactionType.INCOME).exclude(
				category=_get_or_create_predefined_category(PAYMENT_ALLOCATION_CATEGORY)
			).exclude(
				category=_get_or_create_predefined_category(CREDIT_BALANCE_APPLIED_CATEGORY)
			).delete()
			if cement_record.is_sale:
				_reconcile_material_sale_income_transaction(cement_record)
			messages.success(request, "Cement record updated successfully.")
			return redirect("cement_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = CementRecordForm(instance=cement_record, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/cement_record_form.html",
		{
			"form": form,
			"form_title": "Edit Cement Record",
			"submit_label": "Update Record",
		},
	)


@login_required
def cement_record_delete(request, pk):
	"""Delete a cement record."""
	cement_record = get_object_or_404(CementRecord, pk=pk)

	if request.method != "POST":
		return redirect("cement_records")

	cement_record.transactions.filter(type=TransactionType.INCOME).delete()
	cement_record.delete()
	messages.success(request, "Cement record deleted successfully.")
	return redirect("cement_records")


@login_required
def cement_record_mark_paid(request, pk):
	"""Mark a pending cement sale record as paid."""
	cement_record = get_object_or_404(CementRecord, pk=pk)

	if request.method != "POST":
		return _redirect_to_next_or_default(request, "cement_records")

	if cement_record.record_type != CementRecordType.SALE:
		messages.error(request, "Only sale records can be marked as paid.")
		return _redirect_to_next_or_default(request, "cement_records")

	if cement_record.payment_status == RecordStatus.PAID:
		messages.info(request, "Cement record is already marked as paid.")
		return _redirect_to_next_or_default(request, "cement_records")

	cement_record.paid_amount = cement_record.sale_income or Decimal("0.00")
	cement_record.save()
	_reconcile_material_sale_income_transaction(cement_record)
	messages.success(request, f"Cement sale record on {cement_record.date} marked as paid.")
	return _redirect_to_next_or_default(request, "cement_records")


def _create_cement_sale_transaction(cement_record):
	"""Create transaction entries for cement sale records."""
	_reconcile_material_sale_income_transaction(cement_record)


# BAMBOO RECORDS VIEWS


@login_required
def bamboo_records(request):
	"""Display list of bamboo records with filtering and pagination."""
	default_from, default_to = _get_default_date_range()
	queryset = BambooRecord.objects.select_related("customer")

	query = request.GET.get("q", "").strip()
	record_type = request.GET.get("record_type", "").strip()
	payment_status = request.GET.get("payment_status", "").strip()
	date_filters = _resolve_request_date_filters(
		request,
		default_from=default_from,
		default_to=default_to,
	)
	date_from = date_filters["date_from"]
	date_to = date_filters["date_to"]
	sort = request.GET.get("sort", "-date")

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
	queryset = queryset.order_by(allowed_sorts.get(sort, "-date"), "-created_at")

	paginator = Paginator(queryset, 20)
	page_obj = paginator.get_page(request.GET.get("page"))

	context = {
		"bamboo_records": page_obj.object_list,
		"page_obj": page_obj,
		"filters": {
			"q": query,
			"record_type": record_type,
			"payment_status": payment_status,
			"date_from": date_filters["date_from_display"],
			"date_to": date_filters["date_to_display"],
			"sort": sort,
		},
		"record_type_choices": BambooRecordType.choices,
		"payment_status_choices": RecordStatus.choices,
	}

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/bamboo_records_table.html", context)

	return render(request, "core/bamboo_records.html", context)


@login_required
def bamboo_record_create(request):
	"""Create a new bamboo record."""
	if request.method == "POST":
		form = BambooRecordForm(request.POST, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			bamboo_record = form.save()
			if bamboo_record.is_sale:
				_reconcile_material_sale_income_transaction(bamboo_record)
			messages.success(request, "Bamboo record created successfully.")
			return redirect("bamboo_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = BambooRecordForm(initial={"date": timezone.localdate()}, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/bamboo_record_form.html",
		{
			"form": form,
			"form_title": "Add Bamboo Record",
			"submit_label": "Create Record",
		},
	)


@login_required
def bamboo_record_edit(request, pk):
	"""Edit an existing bamboo record."""
	bamboo_record = get_object_or_404(BambooRecord, pk=pk)

	if request.method == "POST":
		form = BambooRecordForm(request.POST, instance=bamboo_record, **_form_calendar_mode_kwargs(request))
		if form.is_valid():
			bamboo_record = form.save()
			bamboo_record.transactions.filter(type=TransactionType.INCOME).exclude(
				category=_get_or_create_predefined_category(PAYMENT_ALLOCATION_CATEGORY)
			).exclude(
				category=_get_or_create_predefined_category(CREDIT_BALANCE_APPLIED_CATEGORY)
			).delete()
			if bamboo_record.is_sale:
				_reconcile_material_sale_income_transaction(bamboo_record)
			messages.success(request, "Bamboo record updated successfully.")
			return redirect("bamboo_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = BambooRecordForm(instance=bamboo_record, **_form_calendar_mode_kwargs(request))

	return render(
		request,
		"core/bamboo_record_form.html",
		{
			"form": form,
			"form_title": "Edit Bamboo Record",
			"submit_label": "Update Record",
		},
	)


@login_required
def bamboo_record_delete(request, pk):
	"""Delete a bamboo record."""
	bamboo_record = get_object_or_404(BambooRecord, pk=pk)

	if request.method != "POST":
		return redirect("bamboo_records")

	bamboo_record.transactions.filter(type=TransactionType.INCOME).delete()
	bamboo_record.delete()
	messages.success(request, "Bamboo record deleted successfully.")
	return redirect("bamboo_records")


@login_required
def bamboo_record_mark_paid(request, pk):
	"""Mark a pending bamboo sale record as paid."""
	bamboo_record = get_object_or_404(BambooRecord, pk=pk)

	if request.method != "POST":
		return _redirect_to_next_or_default(request, "bamboo_records")

	if bamboo_record.record_type != BambooRecordType.SALE:
		messages.error(request, "Only sale records can be marked as paid.")
		return _redirect_to_next_or_default(request, "bamboo_records")

	if bamboo_record.payment_status == RecordStatus.PAID:
		messages.info(request, "Bamboo record is already marked as paid.")
		return _redirect_to_next_or_default(request, "bamboo_records")

	bamboo_record.paid_amount = bamboo_record.sale_income or Decimal("0.00")
	bamboo_record.save()
	_reconcile_material_sale_income_transaction(bamboo_record)
	messages.success(request, f"Bamboo sale record on {bamboo_record.date} marked as paid.")
	return _redirect_to_next_or_default(request, "bamboo_records")


def _create_bamboo_sale_transaction(bamboo_record):
	"""Create transaction entries for bamboo sale records."""
	_reconcile_material_sale_income_transaction(bamboo_record)
