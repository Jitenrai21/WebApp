from decimal import Decimal
from types import SimpleNamespace

from .models import PaymentMethod


def _money(value):
	numeric = value if isinstance(value, Decimal) else Decimal(str(value or 0))
	return numeric.quantize(Decimal("0.01"))


def summarize_customer_payment(customer_payment):
	allocated_total = Decimal("0.00")
	allocation_lines = []

	for allocation in customer_payment.allocations.select_related("sale").all():
		allocated_total += allocation.amount or Decimal("0.00")
		sale_label = allocation.sale.invoice_number if allocation.sale_id and allocation.sale else f"Sale #{allocation.sale_id or '-'}"
		allocation_lines.append(f"{sale_label} (NPR {_money(allocation.amount)})")

	unallocated_total = customer_payment.amount - allocated_total
	if unallocated_total < 0:
		unallocated_total = Decimal("0.00")

	parts = []
	if allocated_total > 0:
		parts.append(f"Allocated: NPR {_money(allocated_total)} to sales")
	if unallocated_total > 0:
		parts.append(f"Unallocated: NPR {_money(unallocated_total)} to credit balance")

	summary = ", ".join(parts) if parts else "Customer payment"
	return allocated_total, unallocated_total, allocation_lines, summary


def build_customer_payment_display(customer_payment):
	allocated_total, unallocated_total, allocation_lines, summary = summarize_customer_payment(customer_payment)
	display = SimpleNamespace(
		id=f"cp-{customer_payment.id}",
		date=customer_payment.payment_date,
		customer=customer_payment.customer,
		type="income",
		payment_method=customer_payment.payment_method,
		category="Sales Payment Allocation",
		amount=customer_payment.amount,
		summary_text=summary,
		description=f"{summary}. Open customer profile for allocation breakdown.",
		customer_payment_id=customer_payment.id,
		is_grouped_payment=True,
		allocated_total=allocated_total,
		unallocated_total=unallocated_total,
		allocation_lines=allocation_lines,
		allocation_count=len(allocation_lines),
	)
	display.get_type_display = lambda: "Income"
	display.get_payment_method_display = lambda: dict(PaymentMethod.choices).get(customer_payment.payment_method, customer_payment.payment_method)
	return display
