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
	Customer,
	CustomerType,
	Sale,
	TipperItem,
	TipperRecord,
	TipperRecordType,
	Transaction,
	TransactionCategory,
	TransactionType,
)


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
		self.assertEqual(form.fields["due_date"].initial, timezone.localdate())

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


class AlertsWorkflowTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="alert-user", password="pass1234")
		self.customer = Customer.objects.create(name="Alert Customer", type=CustomerType.REGULAR)
		today = timezone.localdate()

		self.overdue_sale = Sale.objects.create(
			invoice_number="INV-A-OVERDUE",
			customer=self.customer,
			total_amount=Decimal("1000.00"),
			due_date=today - timedelta(days=2),
			date=today - timedelta(days=10),
			items=[{"item": "A", "quantity": 1, "price": 1000}],
		)
		self.upcoming_sale = Sale.objects.create(
			invoice_number="INV-A-UPCOMING",
			customer=self.customer,
			total_amount=Decimal("800.00"),
			due_date=today + timedelta(days=3),
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
		self.assertContains(response, "NPR 1000.00")
		self.assertNotContains(response, "NPR 800.00")

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
				"due_date": timezone.localdate().isoformat(),
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
		due_date = timezone.localdate().isoformat()

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
			due_date=timezone.localdate(),
			amount=Decimal("0.00"),
			title="Initial Alert",
			message="Initial message",
		)

		edit_response = self.client.post(
			reverse("manual_alert_edit", args=[manual_alert.pk]),
			data={
				"due_date": timezone.localdate().isoformat(),
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
			due_date=timezone.localdate(),
			items=[{"item": "Item A", "quantity": 1, "price": 1000}],
		)

	def test_due_amount_is_not_auto_reduced_by_credit_balance(self):
		self.client.login(username="customer-due", password="pass1234")
		response = self.client.get(reverse("customer_detail", args=[self.customer.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "NPR 1000.00")
		self.assertContains(response, "NPR 500.00")

	def test_apply_credit_balance_allocates_to_sale(self):
		self.client.login(username="customer-due", password="pass1234")
		sale = Sale.objects.get(invoice_number="INV-CREDIT-001")

		response = self.client.post(
			reverse("customer_allocate_payment", args=[self.customer.pk]),
			data={
				"allocation_mode": "credit",
				"payment_date": timezone.localdate().isoformat(),
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
				"payment_date": timezone.localdate().isoformat(),
				"sale_ids": [str(sale.id)],
			},
		)

		response = self.client.get(reverse("customer_detail", args=[self.customer.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["total_payment"], Decimal("0.00"))

	def test_credit_applied_entries_hidden_from_cash_entries(self):
		self.client.login(username="customer-due", password="pass1234")
		sale = Sale.objects.get(invoice_number="INV-CREDIT-001")

		self.client.post(
			reverse("customer_allocate_payment", args=[self.customer.pk]),
			data={
				"allocation_mode": "credit",
				"payment_date": timezone.localdate().isoformat(),
				"sale_ids": [str(sale.id)],
			},
		)

		response = self.client.get(reverse("cash_entries"))
		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, "Credit Balance Applied")

	def test_credit_applied_entries_excluded_from_dashboard_income(self):
		self.client.login(username="customer-due", password="pass1234")
		sale = Sale.objects.get(invoice_number="INV-CREDIT-001")
		payment_category = TransactionCategory.objects.create(name="Sales Payment Allocation", is_predefined=True)

		# Actual new cash inflow (should count)
		Transaction.objects.create(
			customer=self.customer,
			date=timezone.localdate(),
			amount=Decimal("200.00"),
			type=TransactionType.INCOME,
			category=payment_category,
		)

		# Internal credit application (should not count in dashboard income)
		self.client.post(
			reverse("customer_allocate_payment", args=[self.customer.pk]),
			data={
				"allocation_mode": "credit",
				"payment_date": timezone.localdate().isoformat(),
				"sale_ids": [str(sale.id)],
			},
		)

		response = self.client.get(reverse("dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["kpis"]["total_income"], Decimal("200.00"))

	def test_unassigned_sale_appears_in_alerts_and_timeline(self):
		self.client.login(username="alert-user", password="pass1234")
		today = timezone.localdate()
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
		self.assertContains(response, "NPR 450.00")

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
				"date": timezone.localdate().isoformat(),
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
				"date": timezone.localdate().isoformat(),
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
			date=timezone.localdate(),
			item=self.item,
			record_type=TipperRecordType.EXPENSE,
			amount=Decimal("320.00"),
		)

		response = self.client.post(
			reverse("tipper_record_edit", args=[record.pk]),
			data={
				"date": timezone.localdate().isoformat(),
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
			date=timezone.localdate(),
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
			date=timezone.localdate(),
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
