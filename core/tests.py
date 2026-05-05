import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import SaleForm
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
	CustomerType,
	JCBRecord,
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


def bs_today_date():
	return timezone.localdate()


def bs_add_days(base_date, days):
	return base_date + timedelta(days=days)


class SalesWorkflowTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="tester", password="pass1234")
		self.customer = Customer.objects.create(
			name="Acme Pvt",
			type=CustomerType.REGULAR,
		)
		self.sale = Sale.objects.create(
			invoice_number="INV-1001",
			customer=self.customer,
			total_amount=Decimal("1000.00"),
			due_date="2026-03-30",
			items=[{"item": "Item A", "quantity": 2, "price": 500}],
			notes="Monthly recurring order",
		)

	def _set_calendar_mode(self, mode):
		session = self.client.session
		session["calendar_mode"] = mode
		session.save()

	def test_sale_detail_requires_login(self):
		response = self.client.get(reverse("sale_detail", args=[self.sale.pk]))
		self.assertEqual(response.status_code, 302)
		self.assertIn(reverse("login"), response.url)

	def test_sales_filter_by_payment_status(self):
		self.client.login(username="tester", password="pass1234")

		# unpaid before any receipt
		response_unpaid = self.client.get(reverse("sales"), {"status": "unpaid"})
		self.assertContains(response_unpaid, "INV-1001")

		Transaction.objects.create(
			customer=self.customer,
			sale=self.sale,
			date="2026-03-10",
			amount=Decimal("400.00"),
			type=TransactionType.INCOME,
			category="Sales Receipt",
		)
		response_partial = self.client.get(reverse("sales"), {"status": "partial"})
		self.assertContains(response_partial, "INV-1001")

		Transaction.objects.create(
			customer=self.customer,
			sale=self.sale,
			date="2026-03-11",
			amount=Decimal("600.00"),
			type=TransactionType.INCOME,
			category="Sales Receipt",
		)
		response_paid = self.client.get(reverse("sales"), {"status": "paid"})
		self.assertContains(response_paid, "INV-1001")

	def test_sales_table_links_customer_profile(self):
		self.client.login(username="tester", password="pass1234")
		response = self.client.get(reverse("sales"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, reverse("customer_detail", args=[self.customer.pk]))

	def test_sale_form_validates_itemized_json(self):
		invalid_form = SaleForm(
			data={
				"invoice_number": "INV-2001",
				"date": "2026-03-12",
				"customer": self.customer.pk,
				"items": json.dumps([{"item": "Bad Item", "quantity": 0, "price": 10}]),
				"notes": "Test",
				"total_amount": "10",
				"due_date": "2026-03-15",
			}
		)
		self.assertFalse(invalid_form.is_valid())
		self.assertIn("items", invalid_form.errors)

		valid_form = SaleForm(
			data={
				"invoice_number": "INV-2002",
				"date": "2026-03-12",
				"customer": self.customer.pk,
				"items": json.dumps([{"item": "Good Item", "quantity": 1, "price": 100}]),
				"notes": "Test",
				"total_amount": "100",
				"due_date": "2026-03-16",
			}
		)
		self.assertTrue(valid_form.is_valid())

	def test_sale_form_defaults_due_date_to_today_for_new_sale(self):
		form = SaleForm()
		self.assertEqual(form.fields["due_date"].initial, bs_today_date())

	def test_sales_filter_accepts_bs_dates_in_bs_mode(self):
		self.client.login(username="tester", password="pass1234")
		self.sale.refresh_from_db()
		self._set_calendar_mode("bs")

		response = self.client.get(
			reverse("sales"),
			{
				"date_from": self.sale.bs_date,
				"date_to": self.sale.bs_date,
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "INV-1001")

	def test_sale_form_accepts_bs_dates_in_bs_mode(self):
		self.client.login(username="tester", password="pass1234")
		self.sale.refresh_from_db()

		form = SaleForm(
			data={
				"invoice_number": "INV-2003",
				"date": self.sale.bs_date,
				"customer_input": self.customer.name,
				"items": json.dumps([{"item": "Good Item", "quantity": 1, "price": 100}]),
				"notes": "Test",
				"total_amount": "100",
				"due_date": self.sale.bs_due_date,
				"status": RecordStatus.PENDING,
				"alert_enabled": "on",
			},
			calendar_mode="bs",
		)

		self.assertTrue(form.is_valid())
		self.assertEqual(form.cleaned_data["date"], self.sale.date)
		self.assertEqual(form.cleaned_data["due_date"], self.sale.due_date)

	def test_sale_form_rejects_invalid_bs_dates_in_bs_mode(self):
		self.client.login(username="tester", password="pass1234")

		form = SaleForm(
			data={
				"invoice_number": "INV-2004",
				"date": "2082-99-99",
				"customer_input": self.customer.name,
				"items": json.dumps([{"item": "Good Item", "quantity": 1, "price": 100}]),
				"notes": "Test",
				"total_amount": "100",
				"due_date": "2082-13-40",
				"status": RecordStatus.PENDING,
			},
			calendar_mode="bs",
		)

		self.assertFalse(form.is_valid())
		self.assertIn("date", form.errors)
		self.assertIn("valid BS date", str(form.errors.get("due_date", "")))

	def test_inline_receipt_create_links_to_sale(self):
		self.client.login(username="tester", password="pass1234")

		response = self.client.post(
			reverse("sale_receipt_create", args=[self.sale.pk]),
			data={
				"date": "2026-03-12",
				"amount": "700",
				"payment_method": "cash",
				"category": "Sales Receipt",
				"description": "First payment",
			},
			HTTP_HX_REQUEST="true",
		)

		self.assertEqual(response.status_code, 200)
		receipt = Transaction.objects.get(sale=self.sale)
		self.assertEqual(receipt.customer, self.customer)
		self.assertEqual(receipt.type, TransactionType.INCOME)
		self.assertContains(response, "Linked Cash Receipts")


class DashboardWorkflowTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="dash", password="pass1234")
		self.customer = Customer.objects.create(
			name="Dashboard Client",
			type=CustomerType.REGULAR,
		)
		self.sale = Sale.objects.create(
			invoice_number="INV-D-001",
			customer=self.customer,
			total_amount=Decimal("1200.00"),
			due_date="2026-03-10",
			date="2026-03-01",
			items=[{"item": "Plan", "quantity": 1, "price": 1200}],
		)
		Transaction.objects.create(
			customer=self.customer,
			sale=self.sale,
			date="2026-03-05",
			amount=Decimal("300.00"),
			type=TransactionType.INCOME,
			category="Sales Receipt",
		)
		Transaction.objects.create(
			customer=self.customer,
			date="2026-03-06",
			amount=Decimal("200.00"),
			type=TransactionType.EXPENSE,
			category="Operations",
		)

	def test_dashboard_requires_login(self):
		response = self.client.get(reverse("dashboard"))
		self.assertEqual(response.status_code, 302)
		self.assertIn(reverse("login"), response.url)

	def test_dashboard_renders_kpis_and_tables(self):
		self.client.login(username="dash", password="pass1234")
		response = self.client.get(reverse("dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Total Sales")
		self.assertContains(response, "Outstanding Receivables")
		self.assertContains(response, "Recent Sales")
		self.assertContains(response, "INV-D-001")

	def test_dashboard_htmx_partial_response(self):
		self.client.login(username="dash", password="pass1234")
		response = self.client.get(
			reverse("dashboard"),
			{"date_from": "2026-03-01", "date_to": "2026-03-31"},
			HTTP_HX_REQUEST="true",
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "sales-trend-chart")
		self.assertNotContains(response, "<html")

	def test_dashboard_filter_excludes_outside_date_range(self):
		self.client.login(username="dash", password="pass1234")
		response = self.client.get(
			reverse("dashboard"),
			{"date_from": "2026-04-01", "date_to": "2026-04-30"},
		)
		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, "INV-D-001")

	def test_top_customer_chart_uses_sales_totals_per_customer_record(self):
		self.client.login(username="dash", password="pass1234")

		# Extra receipt should not inflate sales-based customer ranking totals.
		Transaction.objects.create(
			customer=self.customer,
			sale=self.sale,
			date="2026-03-07",
			amount=Decimal("150.00"),
			type=TransactionType.INCOME,
			category="Sales Receipt",
		)

		# Same display name but different customer record should remain a separate bar.
		other_customer_same_name = Customer.objects.create(
			name="Dashboard Client",
			type=CustomerType.REGULAR,
		)
		Sale.objects.create(
			invoice_number="INV-D-002",
			customer=other_customer_same_name,
			total_amount=Decimal("800.00"),
			date="2026-03-02",
			items=[{"item": "Extra", "quantity": 1, "price": 800}],
		)

		response = self.client.get(
			reverse("dashboard"),
			{"date_from": "2026-03-01", "date_to": "2026-03-31"},
		)
		self.assertEqual(response.status_code, 200)

		top_labels = response.context["top_customer_labels"]
		top_values = response.context["top_customer_values"]

		self.assertEqual(top_labels.count("Dashboard Client"), 2)
		self.assertIn(1200.0, top_values)
		self.assertIn(800.0, top_values)

	def test_dashboard_material_income_includes_partial_payments(self):
		self.client.login(username="dash", password="pass1234")
		BlocksRecord.objects.create(
			date="2026-03-04",
			record_type=BlocksRecordType.SALE,
			unit_type=BlocksUnitType.SIX_INCH,
			quantity=10,
			price_per_unit=Decimal("100.00"),
			paid_amount=Decimal("400.00"),
			due_date="2026-03-10",
			payment_status=RecordStatus.PENDING,
		)

		response = self.client.get(
			reverse("dashboard"),
			{"date_from": "2026-03-01", "date_to": "2026-03-31"},
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["blocks_summary"]["total_sale_income"], Decimal("400.00"))


class SalePaymentSyncRegressionTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="sync-user", password="pass1234")
		self.customer = Customer.objects.create(name="Sync Customer", type=CustomerType.REGULAR)

	def _create_sale(self, invoice_number, total="1000.00"):
		return Sale.objects.create(
			invoice_number=invoice_number,
			customer=self.customer,
			total_amount=Decimal(total),
			paid_amount=Decimal("0.00"),
			status="pending",
			due_date=bs_today_date(),
			alert_enabled=True,
			items=[{"item": "Work", "quantity": 1, "price": float(total)}],
		)

	def _post_cash_income(self, sale, amount):
		return self.client.post(
			reverse("transaction_create"),
			data={
				"date": bs_today_date().isoformat(),
				"amount": str(amount),
				"type": TransactionType.INCOME,
				"payment_method": "cash",
				"category": "",
				"description": "Linked payment",
				"customer": str(self.customer.id),
				"sale": str(sale.id),
			},
		)

	def test_partial_then_full_linked_payment_updates_sale_fields(self):
		self.client.login(username="sync-user", password="pass1234")
		sale = self._create_sale("INV-SYNC-001")

		partial_response = self._post_cash_income(sale, "400.00")
		self.assertEqual(partial_response.status_code, 302)
		sale.refresh_from_db()
		self.assertEqual(sale.paid_amount, Decimal("400.00"))
		self.assertEqual(sale.status, RecordStatus.PENDING)

		full_response = self._post_cash_income(sale, "600.00")
		self.assertEqual(full_response.status_code, 302)
		sale.refresh_from_db()
		self.assertEqual(sale.paid_amount, Decimal("1000.00"))
		self.assertEqual(sale.status, RecordStatus.PAID)

		sales_response = self.client.get(reverse("sales"))
		row = sales_response.context["sales"].get(pk=sale.pk)
		self.assertEqual(row.received_total, Decimal("1000.00"))
		self.assertEqual(row.remaining_balance, Decimal("0.00"))

	def test_direct_mark_paid_and_linked_completion_reach_same_state(self):
		self.client.login(username="sync-user", password="pass1234")
		sale_linked = self._create_sale("INV-SYNC-002")
		sale_mark_paid = self._create_sale("INV-SYNC-003")

		self._post_cash_income(sale_linked, "400.00")
		self._post_cash_income(sale_linked, "600.00")
		mark_response = self.client.post(reverse("sale_mark_paid", args=[sale_mark_paid.pk]))
		self.assertEqual(mark_response.status_code, 302)

		sale_linked.refresh_from_db()
		sale_mark_paid.refresh_from_db()
		self.assertEqual(sale_linked.status, sale_mark_paid.status)
		self.assertEqual(sale_linked.paid_amount, sale_mark_paid.paid_amount)

	def test_overpayment_keeps_balance_zero_and_status_paid(self):
		self.client.login(username="sync-user", password="pass1234")
		sale = self._create_sale("INV-SYNC-004")

		response = self._post_cash_income(sale, "1200.00")
		self.assertEqual(response.status_code, 302)
		sale.refresh_from_db()
		self.assertEqual(sale.status, RecordStatus.PAID)
		self.assertEqual(sale.paid_amount, Decimal("1200.00"))

		sales_response = self.client.get(reverse("sales"))
		row = sales_response.context["sales"].get(pk=sale.pk)
		self.assertEqual(row.remaining_balance, Decimal("0.00"))

	def test_transaction_edit_after_paid_reverts_sale_to_pending_when_needed(self):
		self.client.login(username="sync-user", password="pass1234")
		sale = self._create_sale("INV-SYNC-005")
		tx = Transaction.objects.create(
			date=bs_today_date(),
			amount=Decimal("1000.00"),
			type=TransactionType.INCOME,
			payment_method="cash",
			customer=self.customer,
			sale=sale,
		)
		self.client.post(
			reverse("transaction_edit", args=[tx.pk]),
			data={
				"date": bs_today_date().isoformat(),
				"amount": "1000.00",
				"type": TransactionType.INCOME,
				"payment_method": "cash",
				"category": "",
				"description": "Initial full payment",
				"customer": str(self.customer.id),
				"sale": str(sale.id),
			},
		)
		sale.refresh_from_db()
		self.assertEqual(sale.status, RecordStatus.PAID)

		edit_response = self.client.post(
			reverse("transaction_edit", args=[tx.pk]),
			data={
				"date": bs_today_date().isoformat(),
				"amount": "700.00",
				"type": TransactionType.INCOME,
				"payment_method": "cash",
				"category": "",
				"description": "Adjusted payment",
				"customer": str(self.customer.id),
				"sale": str(sale.id),
			},
		)
		self.assertEqual(edit_response.status_code, 302)
		sale.refresh_from_db()
		self.assertEqual(sale.paid_amount, Decimal("700.00"))
		self.assertEqual(sale.status, RecordStatus.PENDING)

	def test_transaction_delete_rolls_back_paid_sale_to_pending(self):
		self.client.login(username="sync-user", password="pass1234")
		sale = self._create_sale("INV-SYNC-006")
		tx = Transaction.objects.create(
			date=bs_today_date(),
			amount=Decimal("1000.00"),
			type=TransactionType.INCOME,
			payment_method="cash",
			customer=self.customer,
			sale=sale,
		)
		self.client.post(
			reverse("transaction_edit", args=[tx.pk]),
			data={
				"date": bs_today_date().isoformat(),
				"amount": "1000.00",
				"type": TransactionType.INCOME,
				"payment_method": "cash",
				"category": "",
				"description": "Confirm full payment",
				"customer": str(self.customer.id),
				"sale": str(sale.id),
			},
		)
		sale.refresh_from_db()
		self.assertEqual(sale.status, RecordStatus.PAID)

		delete_response = self.client.post(reverse("transaction_delete", args=[tx.pk]))
		self.assertEqual(delete_response.status_code, 302)
		sale.refresh_from_db()
		self.assertEqual(sale.paid_amount, Decimal("0.00"))
		self.assertEqual(sale.status, RecordStatus.PENDING)

	def test_mark_paid_with_existing_partial_receipt_creates_shortfall_income_entry(self):
		self.client.login(username="sync-user", password="pass1234")
		sale = self._create_sale("INV-SYNC-007")

		self._post_cash_income(sale, "400.00")
		response = self.client.post(reverse("sale_mark_paid", args=[sale.pk]))
		self.assertEqual(response.status_code, 302)

		sale.refresh_from_db()
		self.assertEqual(sale.status, RecordStatus.PAID)
		self.assertEqual(sale.paid_amount, Decimal("1000.00"))

		auto_category = TransactionCategory.objects.get(name="Sale Income (Auto)")
		auto_entries = Transaction.objects.filter(
			sale=sale,
			type=TransactionType.INCOME,
			category=auto_category,
		)
		self.assertEqual(auto_entries.count(), 1)
		self.assertEqual(auto_entries.first().amount, Decimal("600.00"))

	def test_mark_paid_shortfall_income_entry_appears_in_finance_ledger(self):
		self.client.login(username="sync-user", password="pass1234")
		sale = self._create_sale("INV-SYNC-008")
		self._post_cash_income(sale, "250.00")

		self.client.post(reverse("sale_mark_paid", args=[sale.pk]))

		cash_response = self.client.get(reverse("finance_ledger"))
		self.assertEqual(cash_response.status_code, 200)
		self.assertContains(cash_response, "INV-SYNC-008")
		self.assertContains(cash_response, "Sale Income (Auto)")

	def test_sale_create_pending_auto_applies_credit_balance(self):
		self.client.login(username="sync-user", password="pass1234")
		self.customer.credit_balance = Decimal("500.00")
		self.customer.save(update_fields=["credit_balance", "updated_at"])

		response = self.client.post(
			reverse("sale_create"),
			data={
				"invoice_number": "INV-SYNC-009",
				"date": bs_today_date().isoformat(),
				"customer": str(self.customer.pk),
				"customer_input": self.customer.name,
				"status": RecordStatus.PENDING,
				"items": json.dumps([
					{"item": "Service", "quantity": 1, "price": 300, "amount": 300}
				]),
				"notes": "Auto credit apply test",
				"total_amount": "300.00",
				"due_date": bs_today_date().isoformat(),
			},
		)

		self.assertEqual(response.status_code, 302)
		sale = Sale.objects.get(invoice_number="INV-SYNC-009")
		self.customer.refresh_from_db()

		self.assertEqual(sale.status, RecordStatus.PAID)
		self.assertEqual(sale.paid_amount, Decimal("300.00"))
		self.assertEqual(self.customer.credit_balance, Decimal("200.00"))

		credit_category = TransactionCategory.objects.get(name="Credit Balance Applied")
		receipt = Transaction.objects.get(sale=sale, category=credit_category, type=TransactionType.INCOME)
		self.assertEqual(receipt.amount, Decimal("300.00"))

	def test_sale_edit_pending_auto_applies_credit_balance_partially(self):
		self.client.login(username="sync-user", password="pass1234")
		self.customer.credit_balance = Decimal("250.00")
		self.customer.save(update_fields=["credit_balance", "updated_at"])
		sale = self._create_sale("INV-SYNC-010", total="700.00")

		response = self.client.post(
			reverse("sale_edit", args=[sale.pk]),
			data={
				"invoice_number": sale.invoice_number,
				"date": sale.date.isoformat(),
				"customer": str(self.customer.pk),
				"customer_input": self.customer.name,
				"status": RecordStatus.PENDING,
				"items": json.dumps(sale.items),
				"notes": sale.notes,
				"total_amount": "700.00",
				"due_date": bs_today_date().isoformat(),
			},
		)

		self.assertEqual(response.status_code, 302)
		sale.refresh_from_db()
		self.customer.refresh_from_db()

		self.assertEqual(sale.status, RecordStatus.PENDING)
		self.assertEqual(sale.paid_amount, Decimal("250.00"))
		self.assertEqual(self.customer.credit_balance, Decimal("0.00"))

		credit_category = TransactionCategory.objects.get(name="Credit Balance Applied")
		receipt = Transaction.objects.get(sale=sale, category=credit_category, type=TransactionType.INCOME)
		self.assertEqual(receipt.amount, Decimal("250.00"))

	def test_sale_delete_restores_auto_applied_credit_balance(self):
		self.client.login(username="sync-user", password="pass1234")
		self.customer.credit_balance = Decimal("500.00")
		self.customer.save(update_fields=["credit_balance", "updated_at"])

		create_response = self.client.post(
			reverse("sale_create"),
			data={
				"invoice_number": "INV-SYNC-011",
				"date": bs_today_date().isoformat(),
				"customer": str(self.customer.pk),
				"customer_input": self.customer.name,
				"status": RecordStatus.PENDING,
				"items": json.dumps([
					{"item": "Service", "quantity": 1, "price": 300, "amount": 300}
				]),
				"notes": "Delete fallback test",
				"total_amount": "300.00",
				"due_date": bs_today_date().isoformat(),
			},
		)
		self.assertEqual(create_response.status_code, 302)

		sale = Sale.objects.get(invoice_number="INV-SYNC-011")
		self.customer.refresh_from_db()
		self.assertEqual(self.customer.credit_balance, Decimal("200.00"))

		delete_response = self.client.post(reverse("sale_delete", args=[sale.pk]))
		self.assertEqual(delete_response.status_code, 302)

		self.customer.refresh_from_db()
		self.assertEqual(self.customer.credit_balance, Decimal("500.00"))
		self.assertFalse(Sale.objects.filter(invoice_number="INV-SYNC-011").exists())


class MaterialSalesCreditTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="material-user", password="pass1234")
		self.customer = Customer.objects.create(
			name="Material Customer",
			type=CustomerType.REGULAR,
		)

	def test_blocks_sale_create_auto_applies_credit_balance(self):
		self.client.login(username="material-user", password="pass1234")
		self.customer.credit_balance = Decimal("500.00")
		self.customer.save(update_fields=["credit_balance", "updated_at"])

		response = self.client.post(
			reverse("blocks_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BlocksRecordType.SALE,
				"customer": str(self.customer.pk),
				"customer_input": self.customer.name,
				"unit_type": BlocksUnitType.FOUR_INCH,
				"quantity": "3",
				"price_per_unit": "100.00",
				"paid_amount": "0",
				"due_date": bs_today_date().isoformat(),
				"notes": "Credit apply test",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = BlocksRecord.objects.get(record_type=BlocksRecordType.SALE, notes="Credit apply test")
		self.customer.refresh_from_db()

		self.assertEqual(record.payment_status, RecordStatus.PAID)
		self.assertEqual(record.paid_amount, Decimal("300.00"))
		self.assertEqual(self.customer.credit_balance, Decimal("200.00"))

		credit_category = TransactionCategory.objects.get(name="Credit Balance Applied")
		receipt = Transaction.objects.get(
			blocks_record=record,
			category=credit_category,
			type=TransactionType.INCOME,
		)
		self.assertEqual(receipt.amount, Decimal("300.00"))

	def test_cement_sale_create_auto_applies_credit_balance_partially(self):
		self.client.login(username="material-user", password="pass1234")
		self.customer.credit_balance = Decimal("250.00")
		self.customer.save(update_fields=["credit_balance", "updated_at"])

		response = self.client.post(
			reverse("cement_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": CementRecordType.SALE,
				"customer": str(self.customer.pk),
				"customer_input": self.customer.name,
				"unit_type": CementUnitType.PPC,
				"quantity": "7",
				"price_per_unit": "100.00",
				"paid_amount": "0",
				"due_date": bs_today_date().isoformat(),
				"notes": "Cement credit apply",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = CementRecord.objects.get(record_type=CementRecordType.SALE, notes="Cement credit apply")
		self.customer.refresh_from_db()

		self.assertEqual(record.payment_status, RecordStatus.PENDING)
		self.assertEqual(record.paid_amount, Decimal("250.00"))
		self.assertEqual(self.customer.credit_balance, Decimal("0.00"))

		credit_category = TransactionCategory.objects.get(name="Credit Balance Applied")
		receipt = Transaction.objects.get(
			cement_record=record,
			category=credit_category,
			type=TransactionType.INCOME,
		)
		self.assertEqual(receipt.amount, Decimal("250.00"))


class AlertsWorkflowTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="alert-user", password="pass1234")
		self.customer = Customer.objects.create(name="Alert Customer", type=CustomerType.REGULAR)
		today = bs_today_date()

		self.overdue_sale = Sale.objects.create(
			invoice_number="INV-A-OVERDUE",
			customer=self.customer,
			total_amount=Decimal("1000.00"),
			due_date=today - timedelta(days=2),
			date=today - timedelta(days=10),
			notes="Overdue sale note",
			items=[{"item": "A", "quantity": 1, "price": 1000}],
		)
		self.upcoming_sale = Sale.objects.create(
			invoice_number="INV-A-UPCOMING",
			customer=self.customer,
			total_amount=Decimal("800.00"),
			due_date=bs_add_days(today, 3),
			date=today,
			items=[{"item": "B", "quantity": 1, "price": 800}],
		)

	def test_alert_states_for_sale_and_transaction(self):
		self.assertEqual(self.overdue_sale.alert_state, "overdue")
		self.assertEqual(self.upcoming_sale.alert_state, "upcoming")


	def test_alerts_page_requires_login(self):
		response = self.client.get(reverse("alerts"))
		self.assertEqual(response.status_code, 302)
		self.assertIn(reverse("login"), response.url)

	def test_alerts_filter_overdue(self):
		self.client.login(username="alert-user", password="pass1234")
		response = self.client.get(reverse("alerts"), {"type": "overdue"})
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "NPR 1,000")
		self.assertNotContains(response, "NPR 800")

	def test_alerts_table_includes_invoice_link_and_sale_description(self):
		self.client.login(username="alert-user", password="pass1234")
		response = self.client.get(reverse("alerts"), {"type": "overdue"})
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, reverse("sale_detail", args=[self.overdue_sale.pk]))
		self.assertContains(response, self.overdue_sale.invoice_number)
		self.assertContains(response, "Overdue sale note")

	def test_badge_count_and_viewed_mark_read(self):
		self.client.login(username="alert-user", password="pass1234")
		call_command("process_alert_notifications")

		badge_before = self.client.get(reverse("alerts_badge"))
		self.assertContains(badge_before, "badge-error")

		self.client.get(reverse("alerts"))
		badge_after = self.client.get(reverse("alerts_badge"))
		self.assertContains(badge_after, "badge-error")

	def test_badge_uses_live_alert_items_without_scheduler_run(self):
		self.client.login(username="alert-user", password="pass1234")
		badge = self.client.get(reverse("alerts_badge"))
		self.assertContains(badge, "badge-error")

	def test_scheduler_command_is_idempotent(self):
		call_command("process_alert_notifications")
		first_count = AlertNotification.objects.count()
		call_command("process_alert_notifications")
		second_count = AlertNotification.objects.count()
		self.assertEqual(first_count, second_count)
		self.assertGreater(first_count, 0)

	def test_manual_alert_create_and_display(self):
		self.client.login(username="alert-user", password="pass1234")
		response = self.client.post(
			reverse("manual_alert_create"),
			data={
				"due_date": bs_today_date().isoformat(),
				"title": "Follow up call",
				"message": "Call customer for pending documents.",
				"alert_type": "",
			},
		)

		self.assertEqual(response.status_code, 302)
		self.assertRedirects(response, reverse("alerts"))
		manual_alert = AlertNotification.objects.get(title="Follow up call")
		self.assertEqual(manual_alert.source_type, AlertSource.MANUAL)
		self.assertEqual(manual_alert.alert_type, AlertType.MANUAL)
		self.assertIsNone(manual_alert.customer)

		alerts_page = self.client.get(reverse("alerts"))
		self.assertContains(alerts_page, "Follow up call")
		self.assertContains(alerts_page, "Manual")

	def test_manual_alert_duplicate_validation(self):
		self.client.login(username="alert-user", password="pass1234")
		due_date = bs_today_date().isoformat()

		AlertNotification.objects.create(
			alert_type=AlertType.MANUAL,
			source_type=AlertSource.MANUAL,
			source_id=None,
			customer=None,
			due_date=due_date,
			amount=Decimal("0.00"),
			title="Unique Task",
			message="Original",
		)

		response = self.client.post(
			reverse("manual_alert_create"),
			data={
				"due_date": due_date,
				"title": "Unique Task",
				"message": "Duplicate",
				"alert_type": "manual",
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(
			response,
			"A manual alert with this title already exists for this due date.",
		)

	def test_manual_alert_edit_delete_and_resolve(self):
		self.client.login(username="alert-user", password="pass1234")
		manual_alert = AlertNotification.objects.create(
			alert_type=AlertType.MANUAL,
			source_type=AlertSource.MANUAL,
			source_id=None,
			customer=None,
			due_date=bs_today_date(),
			amount=Decimal("0.00"),
			title="Initial Alert",
			message="Initial message",
		)

		edit_response = self.client.post(
			reverse("manual_alert_edit", args=[manual_alert.pk]),
			data={
				"due_date": bs_today_date().isoformat(),
				"title": "Updated Alert",
				"message": "Updated message",
				"alert_type": "upcoming",
			},
		)
		self.assertEqual(edit_response.status_code, 302)
		manual_alert.refresh_from_db()
		self.assertEqual(manual_alert.title, "Updated Alert")
		self.assertEqual(manual_alert.alert_type, AlertType.UPCOMING)

		resolve_response = self.client.post(reverse("alert_notification_resolve", args=[manual_alert.pk]))
		self.assertEqual(resolve_response.status_code, 302)
		manual_alert.refresh_from_db()
		self.assertFalse(manual_alert.is_active)

		delete_response = self.client.post(reverse("manual_alert_delete", args=[manual_alert.pk]))
		self.assertEqual(delete_response.status_code, 302)
		self.assertFalse(AlertNotification.objects.filter(pk=manual_alert.pk).exists())


class CustomerDueCreditBehaviorTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="customer-due", password="pass1234")
		self.customer = Customer.objects.create(
			name="Credit Test Customer",
			type=CustomerType.REGULAR,
			credit_balance=Decimal("500.00"),
		)
		Sale.objects.create(
			invoice_number="INV-CREDIT-001",
			customer=self.customer,
			total_amount=Decimal("1000.00"),
			paid_amount=Decimal("0.00"),
			due_date=bs_today_date(),
			items=[{"item": "Item A", "quantity": 1, "price": 1000}],
		)

	def test_due_amount_is_not_auto_reduced_by_credit_balance(self):
		self.client.login(username="customer-due", password="pass1234")
		response = self.client.get(reverse("customer_detail", args=[self.customer.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "NPR 1,000")
		self.assertContains(response, "NPR 500")

	def test_apply_credit_balance_allocates_to_sale(self):
		self.client.login(username="customer-due", password="pass1234")
		sale = Sale.objects.get(invoice_number="INV-CREDIT-001")

		response = self.client.post(
			reverse("customer_allocate_payment", args=[self.customer.pk]),
			data={
				"allocation_mode": "credit",
				"payment_date": bs_today_date().isoformat(),
				"sale_ids": [str(sale.id)],
			},
		)

		self.assertEqual(response.status_code, 302)
		sale.refresh_from_db()
		self.customer.refresh_from_db()
		self.assertEqual(sale.paid_amount, Decimal("500.00"))
		self.assertEqual(self.customer.credit_balance, Decimal("0.00"))

		credit_applied_tx = Transaction.objects.filter(
			customer=self.customer,
			sale=sale,
			type=TransactionType.INCOME,
			category__name="Credit Balance Applied",
		).first()
		self.assertIsNotNone(credit_applied_tx)
		self.assertEqual(credit_applied_tx.amount, Decimal("500.00"))

	def test_credit_applied_entries_not_double_counted_in_total_payment(self):
		self.client.login(username="customer-due", password="pass1234")
		sale = Sale.objects.get(invoice_number="INV-CREDIT-001")

		self.client.post(
			reverse("customer_allocate_payment", args=[self.customer.pk]),
			data={
				"allocation_mode": "credit",
				"payment_date": bs_today_date().isoformat(),
				"sale_ids": [str(sale.id)],
			},
		)

		response = self.client.get(reverse("customer_detail", args=[self.customer.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["total_payment"], Decimal("0.00"))

	def test_credit_applied_entries_hidden_from_finance_ledger(self):
		self.client.login(username="customer-due", password="pass1234")
		sale = Sale.objects.get(invoice_number="INV-CREDIT-001")

		self.client.post(
			reverse("customer_allocate_payment", args=[self.customer.pk]),
			data={
				"allocation_mode": "credit",
				"payment_date": bs_today_date().isoformat(),
				"sale_ids": [str(sale.id)],
			},
		)

		response = self.client.get(reverse("finance_ledger"))
		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, "Credit Balance Applied")

	def test_credit_applied_entries_excluded_from_dashboard_income(self):
		self.client.login(username="customer-due", password="pass1234")
		sale = Sale.objects.get(invoice_number="INV-CREDIT-001")
		payment_category = TransactionCategory.objects.create(name="Sales Payment Allocation", is_predefined=True)

		# Actual new cash inflow (should count)
		Transaction.objects.create(
			customer=self.customer,
			date=bs_today_date(),
			amount=Decimal("200.00"),
			type=TransactionType.INCOME,
			category=payment_category,
		)

		# Internal credit application (should not count in dashboard income)
		self.client.post(
			reverse("customer_allocate_payment", args=[self.customer.pk]),
			data={
				"allocation_mode": "credit",
				"payment_date": bs_today_date().isoformat(),
				"sale_ids": [str(sale.id)],
			},
		)

		response = self.client.get(reverse("dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["kpis"]["total_income"], Decimal("200.00"))

	def test_unassigned_sale_appears_in_alerts_and_timeline(self):
		self.client.login(username="alert-user", password="pass1234")
		today = bs_today_date()
		unassigned_sale = Sale.objects.create(
			invoice_number="INV-A-UNASSIGNED",
			customer=None,
			total_amount=Decimal("450.00"),
			due_date=today - timedelta(days=1),
			date=today - timedelta(days=5),
			status="pending",
			alert_enabled=True,
			items=[{"item": "U", "quantity": 1, "price": 450}],
		)

		response = self.client.get(reverse("alerts"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Invoice INV-A-UNASSIGNED")
		self.assertContains(response, "NPR 450")

		filtered_response = self.client.get(reverse("alerts"), {"customer": "__unassigned__"})
		self.assertEqual(filtered_response.status_code, 200)
		self.assertContains(filtered_response, "Invoice INV-A-UNASSIGNED")

		call_command("process_alert_notifications")
		notification = AlertNotification.objects.filter(
			source_type=AlertSource.SALE,
			source_id=unassigned_sale.id,
		).first()
		self.assertIsNotNone(notification)
		self.assertIsNone(notification.customer)


class TipperRecordsDescriptionTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="tipper-user", password="pass1234")
		self.item = TipperItem.objects.create(name="Gravel")

	def test_create_tipper_record_with_description(self):
		self.client.login(username="tipper-user", password="pass1234")

		response = self.client.post(
			reverse("tipper_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"item": str(self.item.id),
				"record_type": TipperRecordType.VALUE_ADDED,
				"description": "Loaded soil from site A to site B.",
				"amount": "1500.00",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = TipperRecord.objects.get(item=self.item)
		self.assertEqual(record.description, "Loaded soil from site A to site B.")

	def test_description_optional_for_tipper_record(self):
		self.client.login(username="tipper-user", password="pass1234")

		response = self.client.post(
			reverse("tipper_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"item": str(self.item.id),
				"record_type": TipperRecordType.EXPENSE,
				"description": "",
				"amount": "250.00",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = TipperRecord.objects.get(item=self.item)
		self.assertEqual(record.description, "")

	def test_edit_tipper_record_description(self):
		self.client.login(username="tipper-user", password="pass1234")
		record = TipperRecord.objects.create(
			date=bs_today_date(),
			item=self.item,
			record_type=TipperRecordType.EXPENSE,
			amount=Decimal("320.00"),
		)

		response = self.client.post(
			reverse("tipper_record_edit", args=[record.pk]),
			data={
				"date": bs_today_date().isoformat(),
				"item": str(self.item.id),
				"record_type": TipperRecordType.EXPENSE,
				"description": "Fuel refill for route 3.",
				"amount": "320.00",
			},
		)

		self.assertEqual(response.status_code, 302)
		record.refresh_from_db()
		self.assertEqual(record.description, "Fuel refill for route 3.")

	def test_tipper_list_search_matches_description(self):
		self.client.login(username="tipper-user", password="pass1234")
		TipperRecord.objects.create(
			date=bs_today_date(),
			item=self.item,
			record_type=TipperRecordType.VALUE_ADDED,
			description="Night shift haul",
			amount=Decimal("900.00"),
		)

		response = self.client.get(reverse("tipper_records"), {"q": "night shift"})
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Night shift haul")

	def test_tipper_detail_and_list_show_placeholder_when_description_empty(self):
		self.client.login(username="tipper-user", password="pass1234")
		record = TipperRecord.objects.create(
			date=bs_today_date(),
			item=self.item,
			record_type=TipperRecordType.EXPENSE,
			amount=Decimal("100.00"),
		)

		list_response = self.client.get(reverse("tipper_records"))
		self.assertEqual(list_response.status_code, 200)
		self.assertContains(list_response, "&mdash;", html=False)

		detail_response = self.client.get(reverse("tipper_record_detail", args=[record.pk]))
		self.assertEqual(detail_response.status_code, 200)
		self.assertContains(detail_response, "Description:")
		self.assertContains(detail_response, "&mdash;", html=False)


class TipperExpenseLedgerSyncTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="tipper-sync", password="pass1234")
		self.item = TipperItem.objects.create(name="Diesel")

	def test_create_expense_creates_global_expense_transaction(self):
		self.client.login(username="tipper-sync", password="pass1234")

		response = self.client.post(
			reverse("tipper_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"item": str(self.item.id),
				"record_type": TipperRecordType.EXPENSE,
				"description": "Fuel refill",
				"amount": "1200.00",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = TipperRecord.objects.get(item=self.item, record_type=TipperRecordType.EXPENSE)
		tx = Transaction.objects.get(tipper_record=record)
		self.assertEqual(tx.type, TransactionType.EXPENSE)
		self.assertEqual(tx.amount, Decimal("1200.00"))
		self.assertEqual(tx.category.name, "Tipper Expense")

	def test_create_value_added_does_not_create_global_expense_transaction(self):
		self.client.login(username="tipper-sync", password="pass1234")

		response = self.client.post(
			reverse("tipper_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"item": str(self.item.id),
				"record_type": TipperRecordType.VALUE_ADDED,
				"description": "Backhaul income",
				"amount": "1800.00",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = TipperRecord.objects.get(item=self.item, record_type=TipperRecordType.VALUE_ADDED)
		self.assertFalse(Transaction.objects.filter(tipper_record=record, type=TransactionType.EXPENSE).exists())

	def test_edit_expense_to_value_added_removes_global_expense_transaction(self):
		self.client.login(username="tipper-sync", password="pass1234")
		record = TipperRecord.objects.create(
			date=bs_today_date(),
			item=self.item,
			record_type=TipperRecordType.EXPENSE,
			amount=Decimal("700.00"),
		)

		self.client.post(
			reverse("tipper_record_edit", args=[record.pk]),
			data={
				"date": bs_today_date().isoformat(),
				"item": str(self.item.id),
				"record_type": TipperRecordType.EXPENSE,
				"description": "Initial expense",
				"amount": "700.00",
			},
		)
		self.assertTrue(Transaction.objects.filter(tipper_record=record, type=TransactionType.EXPENSE).exists())

		response = self.client.post(
			reverse("tipper_record_edit", args=[record.pk]),
			data={
				"date": bs_today_date().isoformat(),
				"item": str(self.item.id),
				"record_type": TipperRecordType.VALUE_ADDED,
				"description": "Converted to value add",
				"amount": "900.00",
			},
		)

		self.assertEqual(response.status_code, 302)
		record.refresh_from_db()
		self.assertEqual(record.record_type, TipperRecordType.VALUE_ADDED)
		self.assertFalse(Transaction.objects.filter(tipper_record=record, type=TransactionType.EXPENSE).exists())

	def test_dashboard_total_expense_includes_tipper_expense_only(self):
		self.client.login(username="tipper-sync", password="pass1234")

		self.client.post(
			reverse("tipper_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"item": str(self.item.id),
				"record_type": TipperRecordType.EXPENSE,
				"description": "Fuel expense",
				"amount": "500.00",
			},
		)
		self.client.post(
			reverse("tipper_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"item": str(self.item.id),
				"record_type": TipperRecordType.VALUE_ADDED,
				"description": "Value add haul",
				"amount": "2500.00",
			},
		)

		response = self.client.get(reverse("dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["kpis"]["total_expenses"], Decimal("500.00"))


class JCBStatusFilterTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="jcb-filter", password="pass1234")

	def test_pending_filter_excludes_expense_only_rows(self):
		self.client.login(username="jcb-filter", password="pass1234")

		JCBRecord.objects.create(
			date=bs_today_date(),
			site_name="Work Site",
			start_time=Decimal("600.00"),
			end_time=Decimal("602.00"),
			status=RecordStatus.PENDING,
			rate=Decimal("2000.00"),
			total_amount=Decimal("4000.00"),
		)
		JCBRecord.objects.create(
			date=bs_today_date(),
			site_name="Fuel",
			start_time=Decimal("0.00"),
			end_time=Decimal("0.00"),
			status=RecordStatus.PENDING,
			expense_item="Diesel",
			expense_amount=Decimal("1200.00"),
		)

		response = self.client.get(reverse("jcb_records"), {"status": RecordStatus.PENDING})
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Work Site")
		self.assertNotContains(response, "Diesel")


class BlocksInvestmentLedgerSyncTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="blocks-sync", password="pass1234")
		self.customer = Customer.objects.create(name="Blocks Buyer")

	def test_create_investment_does_not_create_global_expense_transaction(self):
		self.client.login(username="blocks-sync", password="pass1234")

		response = self.client.post(
			reverse("blocks_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BlocksRecordType.INVESTMENT,
				"investment": "1000.00",
				"notes": "Cement and labor",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = BlocksRecord.objects.get(record_type=BlocksRecordType.INVESTMENT)
		self.assertEqual(record.transactions.count(), 0)
		self.assertFalse(
			Transaction.objects.filter(
				blocks_record=record,
				type=TransactionType.EXPENSE,
			).exists()
		)

	def test_sale_record_starts_pending_without_payment_transaction(self):
		self.client.login(username="blocks-sync", password="pass1234")

		response = self.client.post(
			reverse("blocks_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BlocksRecordType.SALE,
				"customer_input": self.customer.name,
				"unit_type": "4_inch",
				"quantity": "10",
				"price_per_unit": "100.00",
				"paid_amount": "0.00",
				"notes": "Retail sale",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = BlocksRecord.objects.get(record_type=BlocksRecordType.SALE)
		self.assertEqual(record.pending_amount, Decimal("1000.00"))
		self.assertEqual(record.paid_amount, Decimal("0.00"))
		self.assertFalse(Transaction.objects.filter(blocks_record=record, type=TransactionType.INCOME).exists())

	def test_sale_customer_assignment_is_optional(self):
		self.client.login(username="blocks-sync", password="pass1234")
		response = self.client.post(
			reverse("blocks_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BlocksRecordType.SALE,
				"unit_type": "4_inch",
				"quantity": "10",
				"price_per_unit": "100.00",
				"paid_amount": "0.00",
			},
		)
		self.assertEqual(response.status_code, 302)
		record = BlocksRecord.objects.get(record_type=BlocksRecordType.SALE)
		self.assertIsNone(record.customer)

	def test_blocks_sale_marked_paid_with_empty_amount_auto_populates(self):
		"""When form status=PAID but paid_amount empty, auto-fill with sale_income."""
		self.client.login(username="blocks-sync", password="pass1234")
		response = self.client.post(
			reverse("blocks_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BlocksRecordType.SALE,
				"customer_input": self.customer.name,
				"unit_type": "4_inch",
				"quantity": "10",
				"price_per_unit": "100.00",
				"payment_status": RecordStatus.PAID,
				"paid_amount": "",
			},
		)
		self.assertEqual(response.status_code, 302)
		record = BlocksRecord.objects.get(record_type=BlocksRecordType.SALE)
		self.assertEqual(record.sale_income, Decimal("1000.00"))
		self.assertEqual(record.paid_amount, Decimal("1000.00"))
		self.assertEqual(record.payment_status, RecordStatus.PAID)
		self.assertEqual(record.pending_amount, Decimal("0.00"))

	def test_blocks_sale_with_partial_payment_stays_pending(self):
		"""Partial payment should not auto-populate and keep status as pending."""
		self.client.login(username="blocks-sync", password="pass1234")
		response = self.client.post(
			reverse("blocks_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BlocksRecordType.SALE,
				"customer_input": self.customer.name,
				"unit_type": "4_inch",
				"quantity": "10",
				"price_per_unit": "100.00",
				"payment_status": RecordStatus.PAID,
				"paid_amount": "600.00",
			},
		)
		self.assertEqual(response.status_code, 302)
		record = BlocksRecord.objects.get(record_type=BlocksRecordType.SALE)
		self.assertEqual(record.sale_income, Decimal("1000.00"))
		self.assertEqual(record.paid_amount, Decimal("600.00"))
		self.assertEqual(record.pending_amount, Decimal("400.00"))
		self.assertEqual(record.payment_status, RecordStatus.PENDING)

	def test_delete_investment_record_does_not_delete_linked_expense_transaction(self):
		self.client.login(username="blocks-sync", password="pass1234")
		record = BlocksRecord.objects.create(
			date=bs_today_date(),
			record_type=BlocksRecordType.INVESTMENT,
			investment=Decimal("500.00"),
		)
		expense_tx = Transaction.objects.create(
			date=bs_today_date(),
			amount=Decimal("500.00"),
			type=TransactionType.EXPENSE,
			blocks_record=record,
		)

		response = self.client.post(reverse("blocks_record_delete", args=[record.pk]))

		self.assertEqual(response.status_code, 302)
		expense_tx.refresh_from_db()
		self.assertIsNone(expense_tx.blocks_record)


class CementInvestmentLedgerSyncTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="cement-sync", password="pass1234")
		self.customer = Customer.objects.create(name="Cement Buyer")

	def test_invalid_unit_type_is_rejected(self):
		self.client.login(username="cement-sync", password="pass1234")

		response = self.client.post(
			reverse("cement_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": CementRecordType.STOCK,
				"unit_type": "4_inch",
				"quantity": "10",
				"notes": "Should fail",
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertFalse(CementRecord.objects.exists())
		self.assertContains(response, "Select a valid choice")

	def test_create_investment_does_not_create_global_expense_transaction(self):
		self.client.login(username="cement-sync", password="pass1234")

		response = self.client.post(
			reverse("cement_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": CementRecordType.INVESTMENT,
				"investment": "1000.00",
				"notes": "Cement and labor",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = CementRecord.objects.get(record_type=CementRecordType.INVESTMENT)
		self.assertEqual(record.transactions.count(), 0)
		self.assertFalse(
			Transaction.objects.filter(
				cement_record=record,
				type=TransactionType.EXPENSE,
			).exists()
		)

	def test_sale_record_starts_pending_without_payment_transaction(self):
		self.client.login(username="cement-sync", password="pass1234")

		response = self.client.post(
			reverse("cement_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": CementRecordType.SALE,
				"customer_input": self.customer.name,
				"unit_type": CementUnitType.PPC,
				"quantity": "10",
				"price_per_unit": "100.00",
				"paid_amount": "0.00",
				"notes": "Retail sale",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = CementRecord.objects.get(record_type=CementRecordType.SALE)
		self.assertEqual(record.pending_amount, Decimal("1000.00"))
		self.assertEqual(record.paid_amount, Decimal("0.00"))
		self.assertFalse(Transaction.objects.filter(cement_record=record, type=TransactionType.INCOME).exists())

	def test_sale_customer_assignment_is_optional(self):
		self.client.login(username="cement-sync", password="pass1234")
		response = self.client.post(
			reverse("cement_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": CementRecordType.SALE,
				"unit_type": CementUnitType.PPC,
				"quantity": "10",
				"price_per_unit": "100.00",
				"paid_amount": "0.00",
			},
		)
		self.assertEqual(response.status_code, 302)
		record = CementRecord.objects.get(record_type=CementRecordType.SALE)
		self.assertIsNone(record.customer)

	def test_cement_sale_marked_paid_with_empty_amount_auto_populates(self):
		"""When form status=PAID but paid_amount empty, auto-fill with sale_income."""
		self.client.login(username="cement-sync", password="pass1234")
		response = self.client.post(
			reverse("cement_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": CementRecordType.SALE,
				"customer_input": self.customer.name,
				"unit_type": CementUnitType.PPC,
				"quantity": "5",
				"price_per_unit": "200.00",
				"payment_status": RecordStatus.PAID,
				"paid_amount": "",
			},
		)
		self.assertEqual(response.status_code, 302)
		record = CementRecord.objects.get(record_type=CementRecordType.SALE)
		self.assertEqual(record.sale_income, Decimal("1000.00"))
		self.assertEqual(record.paid_amount, Decimal("1000.00"))
		self.assertEqual(record.payment_status, RecordStatus.PAID)
		self.assertEqual(record.pending_amount, Decimal("0.00"))

	def test_cement_sale_with_partial_payment_stays_pending(self):
		"""Partial payment should not auto-populate and keep status as pending."""
		self.client.login(username="cement-sync", password="pass1234")
		response = self.client.post(
			reverse("cement_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": CementRecordType.SALE,
				"customer_input": self.customer.name,
				"unit_type": CementUnitType.PPC,
				"quantity": "5",
				"price_per_unit": "200.00",
				"payment_status": RecordStatus.PAID,
				"paid_amount": "750.00",
			},
		)
		self.assertEqual(response.status_code, 302)
		record = CementRecord.objects.get(record_type=CementRecordType.SALE)
		self.assertEqual(record.sale_income, Decimal("1000.00"))
		self.assertEqual(record.paid_amount, Decimal("750.00"))
		self.assertEqual(record.pending_amount, Decimal("250.00"))
		self.assertEqual(record.payment_status, RecordStatus.PENDING)

	def test_delete_investment_record_does_not_delete_linked_expense_transaction(self):
		self.client.login(username="cement-sync", password="pass1234")
		record = CementRecord.objects.create(
			date=bs_today_date(),
			record_type=CementRecordType.INVESTMENT,
			investment=Decimal("500.00"),
		)
		expense_tx = Transaction.objects.create(
			date=bs_today_date(),
			amount=Decimal("500.00"),
			type=TransactionType.EXPENSE,
			cement_record=record,
		)

		response = self.client.post(reverse("cement_record_delete", args=[record.pk]))

		self.assertEqual(response.status_code, 302)
		expense_tx.refresh_from_db()
		self.assertIsNone(expense_tx.cement_record)


class BambooInvestmentLedgerSyncTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="bamboo-sync", password="pass1234")
		self.customer = Customer.objects.create(name="Bamboo Buyer")

	def test_create_investment_does_not_create_global_expense_transaction(self):
		self.client.login(username="bamboo-sync", password="pass1234")

		response = self.client.post(
			reverse("bamboo_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BambooRecordType.INVESTMENT,
				"investment": "1000.00",
				"notes": "Bamboo purchase",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = BambooRecord.objects.get(record_type=BambooRecordType.INVESTMENT)
		self.assertEqual(record.transactions.count(), 0)
		self.assertFalse(
			Transaction.objects.filter(
				bamboo_record=record,
				type=TransactionType.EXPENSE,
			).exists()
		)

	def test_sale_record_starts_pending_without_payment_transaction(self):
		self.client.login(username="bamboo-sync", password="pass1234")

		response = self.client.post(
			reverse("bamboo_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BambooRecordType.SALE,
				"customer_input": self.customer.name,
				"quantity": "10",
				"price_per_unit": "100.00",
				"paid_amount": "0.00",
				"notes": "Retail sale",
			},
		)

		self.assertEqual(response.status_code, 302)
		record = BambooRecord.objects.get(record_type=BambooRecordType.SALE)
		self.assertEqual(record.pending_amount, Decimal("1000.00"))
		self.assertEqual(record.paid_amount, Decimal("0.00"))
		self.assertFalse(Transaction.objects.filter(bamboo_record=record, type=TransactionType.INCOME).exists())

	def test_sale_customer_assignment_is_optional(self):
		self.client.login(username="bamboo-sync", password="pass1234")
		response = self.client.post(
			reverse("bamboo_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BambooRecordType.SALE,
				"quantity": "10",
				"price_per_unit": "100.00",
				"paid_amount": "0.00",
			},
		)
		self.assertEqual(response.status_code, 302)
		record = BambooRecord.objects.get(record_type=BambooRecordType.SALE)
		self.assertIsNone(record.customer)

	def test_bamboo_sale_marked_paid_with_empty_amount_auto_populates(self):
		"""When form status=PAID but paid_amount empty, auto-fill with sale_income."""
		self.client.login(username="bamboo-sync", password="pass1234")
		response = self.client.post(
			reverse("bamboo_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BambooRecordType.SALE,
				"customer_input": self.customer.name,
				"quantity": "8",
				"price_per_unit": "125.00",
				"payment_status": RecordStatus.PAID,
				"paid_amount": "",
			},
		)
		self.assertEqual(response.status_code, 302)
		record = BambooRecord.objects.get(record_type=BambooRecordType.SALE)
		self.assertEqual(record.sale_income, Decimal("1000.00"))
		self.assertEqual(record.paid_amount, Decimal("1000.00"))
		self.assertEqual(record.payment_status, RecordStatus.PAID)
		self.assertEqual(record.pending_amount, Decimal("0.00"))

	def test_bamboo_sale_with_partial_payment_stays_pending(self):
		"""Partial payment should not auto-populate and keep status as pending."""
		self.client.login(username="bamboo-sync", password="pass1234")
		response = self.client.post(
			reverse("bamboo_record_create"),
			data={
				"date": bs_today_date().isoformat(),
				"record_type": BambooRecordType.SALE,
				"customer_input": self.customer.name,
				"quantity": "8",
				"price_per_unit": "125.00",
				"payment_status": RecordStatus.PAID,
				"paid_amount": "500.00",
			},
		)
		self.assertEqual(response.status_code, 302)
		record = BambooRecord.objects.get(record_type=BambooRecordType.SALE)
		self.assertEqual(record.sale_income, Decimal("1000.00"))
		self.assertEqual(record.paid_amount, Decimal("500.00"))
		self.assertEqual(record.pending_amount, Decimal("500.00"))
		self.assertEqual(record.payment_status, RecordStatus.PENDING)


class ModuleCustomerAllocationTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="module-alloc", password="pass1234")
		self.customer = Customer.objects.create(name="Module Allocation Customer")
		self.blocks_sale = BlocksRecord.objects.create(
			date=bs_today_date(),
			record_type=BlocksRecordType.SALE,
			customer=self.customer,
			unit_type=BlocksUnitType.FOUR_INCH,
			quantity=10,
			price_per_unit=Decimal("100.00"),
			paid_amount=Decimal("0.00"),
		)
		self.unassigned_bamboo_sale = BambooRecord.objects.create(
			date=bs_today_date(),
			record_type=BambooRecordType.SALE,
			customer=None,
			quantity=5,
			price_per_unit=Decimal("80.00"),
			paid_amount=Decimal("0.00"),
		)

	def test_customer_payment_allocation_reduces_module_pending(self):
		self.client.login(username="module-alloc", password="pass1234")
		response = self.client.post(
			reverse("customer_allocate_payment", args=[self.customer.pk]),
			data={
				"allocation_mode": "cash",
				"payment_amount": "400.00",
				"payment_date": bs_today_date().isoformat(),
				"payment_method": PaymentMethod.CASH,
				"blocks_sale_ids": [str(self.blocks_sale.pk)],
			},
		)
		self.assertEqual(response.status_code, 302)
		self.blocks_sale.refresh_from_db()
		self.assertEqual(self.blocks_sale.paid_amount, Decimal("400.00"))
		self.assertEqual(self.blocks_sale.pending_amount, Decimal("600.00"))
		self.assertEqual(self.blocks_sale.payment_status, RecordStatus.PENDING)
		detail = self.client.get(reverse("customer_detail", args=[self.customer.pk]))
		self.assertEqual(detail.status_code, 200)
		self.assertEqual(detail.context["due_amount"], Decimal("600.00"))

	def test_customer_profile_uses_merged_pending_table_and_ignores_unassigned_modules(self):
		self.client.login(username="module-alloc", password="pass1234")
		detail = self.client.get(reverse("customer_detail", args=[self.customer.pk]))
		self.assertEqual(detail.status_code, 200)
		self.assertContains(detail, "Total Pending Payments")
		self.assertContains(detail, "Source/Type")
		self.assertContains(detail, "Supplementary Sales")
		self.assertContains(detail, "Blocks")
		self.assertNotContains(detail, f"#{self.unassigned_bamboo_sale.pk}")
		self.assertEqual(len(detail.context["pending_payment_rows"]), 1)

	def test_delete_investment_record_does_not_delete_linked_expense_transaction(self):
		self.client.login(username="module-alloc", password="pass1234")
		record = BambooRecord.objects.create(
			date=bs_today_date(),
			record_type=BambooRecordType.INVESTMENT,
			investment=Decimal("500.00"),
		)
		expense_tx = Transaction.objects.create(
			date=bs_today_date(),
			amount=Decimal("500.00"),
			type=TransactionType.EXPENSE,
			bamboo_record=record,
		)

		response = self.client.post(reverse("bamboo_record_delete", args=[record.pk]))

		self.assertEqual(response.status_code, 302)
		expense_tx.refresh_from_db()
		self.assertIsNone(expense_tx.bamboo_record)


class SupplementaryRecordDetailTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="profile-user", password="pass1234")
		self.customer = Customer.objects.create(name="Profile Customer")
		self.bamboo_record = BambooRecord.objects.create(
			date=bs_today_date(),
			record_type=BambooRecordType.SALE,
			customer=self.customer,
			quantity=5,
			price_per_unit=Decimal("80.00"),
			paid_amount=Decimal("100.00"),
		)
		self.cement_record = CementRecord.objects.create(
			date=bs_today_date(),
			record_type=CementRecordType.SALE,
			customer=self.customer,
			unit_type=CementUnitType.PPC,
			quantity=10,
			price_per_unit=Decimal("60.00"),
			paid_amount=Decimal("0.00"),
		)
		self.blocks_record = BlocksRecord.objects.create(
			date=bs_today_date(),
			record_type=BlocksRecordType.SALE,
			customer=self.customer,
			unit_type=BlocksUnitType.SIX_INCH,
			quantity=12,
			price_per_unit=Decimal("95.00"),
			paid_amount=Decimal("50.00"),
		)
		self.bamboo_tx = Transaction.objects.create(
			date=bs_today_date(),
			amount=Decimal("100.00"),
			type=TransactionType.INCOME,
			customer=self.customer,
			bamboo_record=self.bamboo_record,
		)

	def test_supplementary_detail_pages_require_login(self):
		for route_name, record in (
			("bamboo_record_detail", self.bamboo_record),
			("cement_record_detail", self.cement_record),
			("blocks_record_detail", self.blocks_record),
		):
			response = self.client.get(reverse(route_name, args=[record.pk]))
			self.assertEqual(response.status_code, 302)

	def test_bamboo_detail_page_links_customer_and_transactions(self):
		self.client.login(username="profile-user", password="pass1234")
		response = self.client.get(reverse("bamboo_record_detail", args=[self.bamboo_record.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, reverse("customer_detail", args=[self.customer.pk]))
		self.assertContains(response, reverse("transaction_detail", args=[self.bamboo_tx.pk]))
		self.assertContains(response, "Mark as Paid")


