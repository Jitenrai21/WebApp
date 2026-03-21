import json
from decimal import Decimal
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction as db_transaction
from django.db.models import Case, CharField, DecimalField, ExpressionWrapper, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import CustomerForm, JCBRecordForm, SaleForm, SaleReceiptForm, TransactionForm
from .models import (
	AlertNotification,
	AlertSource,
	AlertType,
	Customer,
	CustomerPayment,
	JCBRecord,
	PaymentAllocation,
	PaymentMethod,
	RecordStatus,
	Sale,
	Transaction,
	TransactionType,
)


AUTO_SALE_INCOME_CATEGORY = "Sale Income (Auto)"
AUTO_SALE_INCOME_DESCRIPTION = "Auto-linked from paid sale"
PAYMENT_ALLOCATION_CATEGORY = "Sales Payment Allocation"
JCB_INCOME_CATEGORY = "JCB Income"
JCB_EXPENSE_CATEGORY = "JCB Expense"


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


def _sales_alert_queryset():
	return Sale.objects.select_related("customer").annotate(
		received_total=Coalesce(
			Sum("receipts__amount", filter=Q(receipts__type=TransactionType.INCOME)),
			Value(Decimal("0.00")),
		)
	)


def _build_alert_items(alert_type="", customer_id="", date_from="", date_to=""):
	today = timezone.localdate()
	upcoming_end = today + timedelta(days=7)

	sales_queryset = _sales_alert_queryset()

	if customer_id:
		sales_queryset = sales_queryset.filter(customer_id=customer_id)
	if date_from:
		sales_queryset = sales_queryset.filter(due_date__gte=date_from)
	if date_to:
		sales_queryset = sales_queryset.filter(due_date__lte=date_to)

	alert_items = []

	for sale in sales_queryset:
		if not sale.customer_id:
			continue
		if not sale.due_date:
			continue

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
				"amount": sale.total_amount - sale.received_total,
				"status_label": sale.payment_status.title(),
				"object_id": sale.id,
			}
		)

	alert_items.sort(key=lambda item: (item["due_date"], item["state"] == AlertType.UPCOMING))
	return alert_items


def _alerts_badge_count():
	return AlertNotification.objects.filter(is_active=True, is_read=False).count()


def _alerts_context(alert_type="", customer_id="", date_from="", date_to=""):
	alert_items = _build_alert_items(
		alert_type=alert_type,
		customer_id=customer_id,
		date_from=date_from,
		date_to=date_to,
	)

	notification_timeline = AlertNotification.objects.select_related("customer")
	if customer_id:
		notification_timeline = notification_timeline.filter(customer_id=customer_id)
	if date_from:
		notification_timeline = notification_timeline.filter(due_date__gte=date_from)
	if date_to:
		notification_timeline = notification_timeline.filter(due_date__lte=date_to)
	if alert_type:
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
		"alerts_badge_count": _alerts_badge_count(),
	}


def _dashboard_context(date_from="", date_to=""):
	sales_queryset = _dashboard_base_sales_queryset(date_from, date_to)
	transactions_queryset = Transaction.objects.select_related("customer")
	jcb_queryset = JCBRecord.objects.all()
	if date_from:
		transactions_queryset = transactions_queryset.filter(date__gte=date_from)
		jcb_queryset = jcb_queryset.filter(date__gte=date_from)
	if date_to:
		transactions_queryset = transactions_queryset.filter(date__lte=date_to)
		jcb_queryset = jcb_queryset.filter(date__lte=date_to)

	kpi_sales = sales_queryset.aggregate(
		total_sales=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))),
	)
	sales_rows = list(sales_queryset)
	received_total = sum((sale.received_total for sale in sales_rows), Decimal("0.00"))
	outstanding_receivables = sum(
		((sale.total_amount - sale.received_total) for sale in sales_rows),
		Decimal("0.00"),
	)
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

	today = timezone.localdate()
	overdue_sales = [
		sale
		for sale in sales_rows
		if sale.due_date and sale.due_date < today and sale.total_amount > sale.received_total
	]
	overdue_count = len(overdue_sales)
	overdue_amount = sum(
		((sale.total_amount - sale.received_total) for sale in overdue_sales),
		Decimal("0.00"),
	)

	recent_sales = sales_queryset.order_by("-date", "-created_at")[:6]
	recent_transactions = transactions_queryset.order_by("-date", "-created_at")[:6]
	recent_customers = Customer.objects.order_by("-created_at")[:6]

	trend_rows = (
		sales_queryset.values("date")
		.annotate(total=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))))
		.order_by("date")
	)
	sales_trend_labels = [row["date"].isoformat() for row in trend_rows]
	sales_trend_values = [float(row["total"]) for row in trend_rows]

	income_vs_expense_values = [
		float(kpi_income_expense["total_income"]),
		float(kpi_income_expense["total_expenses"]),
	]

	top_customer_rows = (
		sales_queryset.values("customer__name")
		.annotate(total=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))))
		.order_by("-total", "customer__name")[:5]
	)
	top_customer_labels = [row["customer__name"] for row in top_customer_rows]
	top_customer_values = [float(row["total"]) for row in top_customer_rows]

	jcb_summary_labels = ["JCB Income", "JCB Expense"]
	jcb_summary_values = [
		float(jcb_summary["total_jcb_income"]),
		float(jcb_summary["total_jcb_expense"]),
	]

	return {
		"kpis": {
			"total_sales": kpi_sales["total_sales"],
			"received_total": received_total,
			"total_income": kpi_income_expense["total_income"],
			"total_expenses": kpi_income_expense["total_expenses"],
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
	sale.save(update_fields=["paid_amount", "status", "updated_at"])


def _sync_paid_sale_income_entry(sale):
	auto_income_qs = Transaction.objects.filter(
		sale=sale,
		type=TransactionType.INCOME,
		category=AUTO_SALE_INCOME_CATEGORY,
	)

	has_manual_income = sale.receipts.filter(type=TransactionType.INCOME).exclude(
		category=AUTO_SALE_INCOME_CATEGORY
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
			auto_income_qs.exclude(pk=auto_income.pk).delete()
			return

		Transaction.objects.create(
			date=sale.date,
			amount=sale.total_amount,
			type=TransactionType.INCOME,
			category=AUTO_SALE_INCOME_CATEGORY,
			description=description,
			customer=sale.customer,
			sale=sale,
		)
		return

	auto_income_qs.delete()


def _sync_jcb_transactions(jcb_record):
	income_description = f"JCB work on {jcb_record.date} ({jcb_record.total_work_hours} hrs)"
	if jcb_record.site_name:
		income_description = f"{income_description} - {jcb_record.site_name}"
	expense_description = f"JCB expense on {jcb_record.date}: {jcb_record.expense_item}"

	income_qs = Transaction.objects.filter(
		jcb_record=jcb_record,
		type=TransactionType.INCOME,
		category=JCB_INCOME_CATEGORY,
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
				category=JCB_INCOME_CATEGORY,
				description=income_description,
				jcb_record=jcb_record,
			)
	else:
		income_qs.delete()

	expense_qs = Transaction.objects.filter(
		jcb_record=jcb_record,
		type=TransactionType.EXPENSE,
		category=JCB_EXPENSE_CATEGORY,
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
				category=JCB_EXPENSE_CATEGORY,
				description=expense_description,
				jcb_record=jcb_record,
			)
	else:
		expense_qs.delete()


def _sale_receipt_context(sale, form=None):
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
		"receipt_form": form or SaleReceiptForm(),
		"total_received": effective_received,
		"remaining_balance": remaining_balance,
		"sale_status": sale.status,
	}


def _customer_payment_context(customer):
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

	payment_totals = sales.aggregate(
		total_sales=Coalesce(Sum("total_amount"), Value(Decimal("0.00"))),
		total_paid=Coalesce(Sum("paid_amount"), Value(Decimal("0.00"))),
	)
	due_amount = payment_totals["total_sales"] - payment_totals["total_paid"]
	if due_amount < 0:
		due_amount = Decimal("0.00")

	return {
		"sales": sales,
		"sales_rows": sales_rows,
		"pending_sales": pending_sales,
		"pending_sales_rows": pending_sales_rows,
		"total_payment": payment_totals["total_paid"],
		"due_amount": due_amount,
		"payment_method_choices": PaymentMethod.choices,
		"today": timezone.localdate(),
	}


@login_required
def dashboard(request):
	date_from = request.GET.get("date_from", "").strip()
	date_to = request.GET.get("date_to", "").strip()
	context = _dashboard_context(date_from=date_from, date_to=date_to)

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/dashboard_content.html", context)
	return render(request, "core/dashboard.html", context)


@login_required
def cash_entries(request):
	transactions = Transaction.objects.select_related("customer").all()

	query = request.GET.get("q", "").strip()
	date_from = request.GET.get("date_from", "").strip()
	date_to = request.GET.get("date_to", "").strip()
	transaction_type = request.GET.get("type", "").strip()
	customer_id = request.GET.get("customer", "").strip()
	sort = request.GET.get("sort", "-date")

	if query:
		transactions = transactions.filter(
			Q(category__icontains=query)
			| Q(description__icontains=query)
			| Q(customer__name__icontains=query)
		)
	if date_from:
		transactions = transactions.filter(date__gte=date_from)
	if date_to:
		transactions = transactions.filter(date__lte=date_to)
	if transaction_type:
		transactions = transactions.filter(type=transaction_type)
	if customer_id:
		transactions = transactions.filter(customer_id=customer_id)

	allowed_sorts = {
		"-date": "-date",
		"date": "date",
		"-amount": "-amount",
		"amount": "amount",
		"customer": "customer__name",
		"-customer": "-customer__name",
	}
	transactions = transactions.order_by(allowed_sorts.get(sort, "-date"), "-created_at")

	context = {
		"transactions": transactions,
		"customers": Customer.objects.all(),
		"filters": {
			"q": query,
			"date_from": date_from,
			"date_to": date_to,
			"type": transaction_type,
			"customer": customer_id,
			"sort": sort,
		},
	}

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/transaction_table.html", context)
	return render(request, "core/cash_entries.html", context)


@login_required
def transaction_create(request):
	if request.method == "POST":
		form = TransactionForm(request.POST, request.FILES)
		if form.is_valid():
			form.save()
			messages.success(request, "Transaction created successfully.")
			return redirect("cash_entries")
		messages.error(request, "Please fix the errors below.")
	else:
		form = TransactionForm()

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

	if request.method == "POST":
		form = TransactionForm(request.POST, request.FILES, instance=transaction)
		if form.is_valid():
			form.save()
			messages.success(request, "Transaction updated successfully.")
			return redirect("cash_entries")
		messages.error(request, "Please fix the errors below.")
	else:
		form = TransactionForm(instance=transaction)

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
				_sync_sale_payment_fields(linked_sale)
				_sync_paid_sale_income_entry(linked_sale)
				_sync_sale_payment_fields(linked_sale)
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
	date_from = request.GET.get("date_from", "").strip()
	date_to = request.GET.get("date_to", "").strip()
	sort = request.GET.get("sort", "-date")

	if query:
		queryset = queryset.filter(
			Q(site_name__icontains=query)
			| Q(expense_item__icontains=query)
			| Q(status__icontains=query)
		)
	if status:
		queryset = queryset.filter(status=status)
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

	context = {
		"jcb_records": queryset,
		"filters": {
			"q": query,
			"status": status,
			"date_from": date_from,
			"date_to": date_to,
			"sort": sort,
		},
	}

	if request.headers.get("HX-Request"):
		return render(request, "core/partials/jcb_records_table.html", context)
	return render(request, "core/jcb_records.html", context)


@login_required
def jcb_record_create(request):
	if request.method == "POST":
		form = JCBRecordForm(request.POST)
		if form.is_valid():
			jcb_record = form.save()
			_sync_jcb_transactions(jcb_record)
			messages.success(request, "JCB record created successfully.")
			return redirect("jcb_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = JCBRecordForm()

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
		form = JCBRecordForm(request.POST, instance=jcb_record)
		if form.is_valid():
			jcb_record = form.save()
			_sync_jcb_transactions(jcb_record)
			messages.success(request, "JCB record updated successfully.")
			return redirect("jcb_records")
		messages.error(request, "Please fix the errors below.")
	else:
		form = JCBRecordForm(instance=jcb_record)

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
def sales(request):
	queryset = Sale.objects.select_related("customer").annotate(
		received_total=Coalesce(
			Sum("receipts__amount", filter=Q(receipts__type=TransactionType.INCOME)),
			Value(Decimal("0.00")),
		),
	)
	queryset = queryset.annotate(
		status_rank=Case(
			When(status=RecordStatus.PAID, then=Value(2)),
			default=Value(1),
			output_field=IntegerField(),
		),
		remaining_balance=F("total_amount") - F("received_total"),
	)

	query = request.GET.get("q", "").strip()
	status = request.GET.get("status", "").strip()
	customer_id = request.GET.get("customer", "").strip()
	date_from = request.GET.get("date_from", "").strip()
	date_to = request.GET.get("date_to", "").strip()
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

	context = {
		"sales": queryset,
		"customers": Customer.objects.all(),
		"filters": {
			"q": query,
			"status": status,
			"customer": customer_id,
			"date_from": date_from,
			"date_to": date_to,
			"sort": sort,
		},
	}
	if request.headers.get("HX-Request"):
		return render(request, "core/partials/sales_table.html", context)
	return render(request, "core/sales.html", context)


@login_required
def sale_create(request):
	if request.method == "POST":
		form = SaleForm(request.POST)
		if form.is_valid():
			sale = form.save(commit=False)
			sale.paid_amount = sale.total_amount if sale.status == RecordStatus.PAID else Decimal("0.00")
			sale.save()
			_sync_paid_sale_income_entry(sale)
			messages.success(request, "Sale created successfully.")
			return redirect("sale_detail", pk=sale.pk)
		messages.error(request, "Please fix the errors below.")
	else:
		form = SaleForm()

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
		form = SaleForm(request.POST, instance=sale)
		if form.is_valid():
			sale = form.save(commit=False)
			sale.save()
			_sync_paid_sale_income_entry(sale)
			if sale.status == RecordStatus.PAID:
				sale.paid_amount = sale.total_amount
			else:
				summary = sale.receipts.filter(type=TransactionType.INCOME).aggregate(
					total=Coalesce(Sum("amount"), Value(Decimal("0.00")))
				)
				sale.paid_amount = summary["total"]
			sale.save(update_fields=["paid_amount", "updated_at"])
			messages.success(request, "Sale updated successfully.")
			return redirect("sale_detail", pk=sale.pk)
		messages.error(request, "Please fix the errors below.")
	else:
		form = SaleForm(instance=sale)

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
	context = _sale_receipt_context(sale)
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
def sale_receipt_create(request, pk):
	sale = get_object_or_404(Sale.objects.select_related("customer"), pk=pk)

	if request.method != "POST":
		return redirect("sale_detail", pk=sale.pk)

	if not sale.customer_id:
		messages.error(request, "Assign a customer to this sale before adding receipts.")
		context = _sale_receipt_context(sale)
		if request.headers.get("HX-Request"):
			return render(request, "core/partials/sale_receipts_panel.html", context, status=400)
		return render(request, "core/sale_detail.html", context, status=400)

	form = SaleReceiptForm(request.POST)
	if form.is_valid():
		receipt = form.save(commit=False)
		receipt.type = TransactionType.INCOME
		receipt.customer = sale.customer
		receipt.sale = sale
		if not receipt.category:
			receipt.category = "Sales Receipt"
		receipt.save()
		_sync_sale_payment_fields(sale)
		_sync_paid_sale_income_entry(sale)
		_sync_sale_payment_fields(sale)
		messages.success(request, "Cash receipt added to sale.")

		if request.headers.get("HX-Request"):
			context = _sale_receipt_context(sale)
			context["inline_success"] = "Cash receipt added to sale."
			return render(request, "core/partials/sale_receipts_panel.html", context)
		return redirect("sale_detail", pk=sale.pk)

	messages.error(request, "Please fix the receipt form errors.")
	context = _sale_receipt_context(sale, form=form)
	if request.headers.get("HX-Request"):
		return render(request, "core/partials/sale_receipts_panel.html", context, status=400)
	return render(request, "core/sale_detail.html", context, status=400)


@login_required
def customers(request):
	queryset = Customer.objects.all()
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

	context = {
		"customers": queryset.order_by("name"),
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
	context.update(_customer_payment_context(customer))
	return render(request, "core/customer_detail.html", context)


@login_required
def customer_allocate_payment(request, pk):
	customer = get_object_or_404(Customer, pk=pk)

	if request.method != "POST":
		return redirect("customer_detail", pk=customer.pk)

	raw_amount = request.POST.get("payment_amount", "").strip()
	raw_date = request.POST.get("payment_date", "").strip()
	payment_method = request.POST.get("payment_method", PaymentMethod.CASH).strip()
	sale_ids = request.POST.getlist("sale_ids")
	notes = request.POST.get("notes", "").strip()

	try:
		payment_amount = Decimal(raw_amount)
	except Exception:
		payment_amount = Decimal("0.00")

	if payment_amount <= 0:
		message = "Enter a valid payment amount greater than zero."
		if request.headers.get("HX-Request"):
			context = {"customer": customer, "allocation_error": message}
			context.update(_customer_payment_context(customer))
			return render(request, "core/partials/customer_payment_section.html", context)
		messages.error(request, message)
		return redirect("customer_detail", pk=customer.pk)

	if not sale_ids:
		message = "Select at least one pending sale to allocate payment."
		if request.headers.get("HX-Request"):
			context = {"customer": customer, "allocation_error": message}
			context.update(_customer_payment_context(customer))
			return render(request, "core/partials/customer_payment_section.html", context)
		messages.error(request, message)
		return redirect("customer_detail", pk=customer.pk)

	if payment_method not in dict(PaymentMethod.choices):
		payment_method = PaymentMethod.CASH

	payment_date = timezone.localdate()
	if raw_date:
		try:
			payment_date = timezone.datetime.strptime(raw_date, "%Y-%m-%d").date()
		except ValueError:
			payment_date = timezone.localdate()

	with db_transaction.atomic():
		selected_sales = list(
			Sale.objects.select_for_update()
			.filter(customer=customer, status=RecordStatus.PENDING, id__in=sale_ids)
			.order_by("date", "created_at", "id")
		)

		if not selected_sales:
			message = "No eligible pending sales were found for allocation."
			if request.headers.get("HX-Request"):
				context = {"customer": customer, "allocation_error": message}
				context.update(_customer_payment_context(customer))
				return render(request, "core/partials/customer_payment_section.html", context)
			messages.error(request, message)
			return redirect("customer_detail", pk=customer.pk)

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

			receipt = Transaction.objects.create(
				date=payment_date,
				amount=allocation_amount,
				type=TransactionType.INCOME,
				category=PAYMENT_ALLOCATION_CATEGORY,
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

			_sync_sale_payment_fields(sale)
			_sync_paid_sale_income_entry(sale)

			sale.refresh_from_db(fields=["status", "paid_amount"])
			if sale.status == RecordStatus.PAID:
				fully_paid_count += 1
			else:
				partial_count += 1

		customer_payment.allocated_amount = allocated_total
		customer_payment.unallocated_amount = remaining_payment
		customer_payment.save(update_fields=["allocated_amount", "unallocated_amount", "updated_at"])

		if remaining_payment > 0:
			customer.credit_balance = customer.credit_balance + remaining_payment
			customer.save(update_fields=["credit_balance", "updated_at"])

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
		context.update(_customer_payment_context(customer))
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
	date_from = request.GET.get("date_from", "").strip()
	date_to = request.GET.get("date_to", "").strip()

	AlertNotification.objects.filter(is_active=True, is_read=False).update(is_read=True)
	context = _alerts_context(alert_type, customer_id, date_from, date_to)

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
	date_from = request.GET.get("date_from", "").strip()
	date_to = request.GET.get("date_to", "").strip()
	context = _alerts_context(alert_type, customer_id, date_from, date_to)

	if request.headers.get("HX-Request"):
		context["include_badge_oob"] = True
		return render(request, "core/partials/alerts_content.html", context)
	return redirect("alerts")
