from decimal import Decimal
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Case, CharField, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import CustomerForm, SaleForm, SaleReceiptForm, TransactionForm
from .models import (
	AlertNotification,
	AlertSource,
	AlertType,
	Customer,
	RecordStatus,
	Sale,
	Transaction,
	TransactionType,
)


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
	transactions_queryset = Transaction.objects.select_related("customer").filter(due_date__isnull=False)

	if customer_id:
		sales_queryset = sales_queryset.filter(customer_id=customer_id)
		transactions_queryset = transactions_queryset.filter(customer_id=customer_id)
	if date_from:
		sales_queryset = sales_queryset.filter(due_date__gte=date_from)
		transactions_queryset = transactions_queryset.filter(due_date__gte=date_from)
	if date_to:
		sales_queryset = sales_queryset.filter(due_date__lte=date_to)
		transactions_queryset = transactions_queryset.filter(due_date__lte=date_to)

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
				"amount": sale.total_amount - sale.received_total,
				"status_label": sale.payment_status.title(),
				"object_id": sale.id,
			}
		)

	for transaction in transactions_queryset:
		if transaction.status == RecordStatus.PAID:
			continue

		state = ""
		if transaction.due_date < today:
			state = AlertType.OVERDUE
		elif today <= transaction.due_date <= upcoming_end:
			state = AlertType.UPCOMING

		if not state:
			continue
		if alert_type and state != alert_type:
			continue

		alert_items.append(
			{
				"state": state,
				"source": AlertSource.TRANSACTION,
				"due_date": transaction.due_date,
				"customer": transaction.customer,
				"title": transaction.category,
				"amount": transaction.amount,
				"status_label": transaction.get_status_display(),
				"object_id": transaction.id,
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
	if date_from:
		transactions_queryset = transactions_queryset.filter(date__gte=date_from)
	if date_to:
		transactions_queryset = transactions_queryset.filter(date__lte=date_to)

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

	today = timezone.localdate()
	overdue_sales = [
		sale
		for sale in sales_rows
		if sale.due_date < today and sale.total_amount > sale.received_total
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

	payment_status = "unpaid"
	if running_received >= sale.total_amount:
		payment_status = "paid"
	elif running_received > 0:
		payment_status = "partial"

	return {
		"sale": sale,
		"receipt_rows": receipt_rows,
		"receipt_form": form or SaleReceiptForm(initial={"status": RecordStatus.PAID}),
		"total_received": running_received,
		"remaining_balance": sale.total_amount - running_received,
		"payment_status": payment_status,
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
def sales(request):
	queryset = Sale.objects.select_related("customer").annotate(
		received_total=Coalesce(
			Sum("receipts__amount", filter=Q(receipts__type=TransactionType.INCOME)),
			Value(Decimal("0.00")),
		),
	)
	queryset = queryset.annotate(
		payment_state=Case(
			When(received_total__gte=F("total_amount"), then=Value("paid")),
			When(received_total__lte=Decimal("0.00"), then=Value("unpaid")),
			default=Value("partial"),
			output_field=CharField(),
		),
		status_rank=Case(
			When(received_total__gte=F("total_amount"), then=Value(3)),
			When(received_total__lte=Decimal("0.00"), then=Value(1)),
			default=Value(2),
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
		queryset = queryset.filter(payment_state=status)
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
			sale.paid_amount = Decimal("0.00")
			sale.status = RecordStatus.PENDING
			sale.save()
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
			sale = form.save()
			_sync_sale_payment_fields(sale)
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
def sale_receipt_create(request, pk):
	sale = get_object_or_404(Sale.objects.select_related("customer"), pk=pk)

	if request.method != "POST":
		return redirect("sale_detail", pk=sale.pk)

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
	sales = customer.sales.all().order_by("-date", "-created_at")

	totals = transactions.aggregate(
		total_income=Sum("amount", filter=Q(type="income")),
		total_expense=Sum("amount", filter=Q(type="expense")),
	)

	context = {
		"customer": customer,
		"transactions": transactions,
		"sales": sales,
		"total_income": totals["total_income"] or 0,
		"total_expense": totals["total_expense"] or 0,
	}
	return render(request, "core/customer_detail.html", context)


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
