from decimal import Decimal
from types import SimpleNamespace

from .models import PaymentMethod, Transaction


def _money(value):
	numeric = value if isinstance(value, Decimal) else Decimal(str(value or 0))
	return numeric.quantize(Decimal("0.01"))


def summarize_customer_payment(customer_payment):
	# Use stored allocated/unallocated amounts from the payment record for accuracy.
	allocated_total = customer_payment.allocated_amount or Decimal("0.00")
	unallocated_total = customer_payment.unallocated_amount or Decimal("0.00")
	allocation_lines = []

	# First include explicit PaymentAllocation lines (invoice allocations)
	txn_ids = set()
	for allocation in customer_payment.allocations.select_related("sale", "transaction").all():
		amt = allocation.amount or Decimal("0.00")
		sale_label = allocation.sale.invoice_number if allocation.sale_id and allocation.sale else f"Sale #{allocation.sale_id or '-'}"
		allocation_lines.append(f"{sale_label} (NPR {_money(amt)})")
		if allocation.transaction_id:
			txn_ids.add(allocation.transaction_id)

	# Also include material allocations which are created as Transactions linked to the CustomerPayment
	# (they carry a description tag like "[Customer Payment #<id>]"), but do not have PaymentAllocation rows.
	extra_txns = Transaction.objects.filter(description__contains=f"[Customer Payment #{customer_payment.id}]")
	for txn in extra_txns.exclude(pk__in=txn_ids):
		amt = txn.amount or Decimal("0.00")
		# Prefer a human-friendly label depending on which record field is set
		if txn.sale_id and txn.sale:
			label = txn.sale.invoice_number
		elif txn.blocks_record_id:
			label = f"Blocks sale #{txn.blocks_record_id}"
		elif txn.cement_record_id:
			label = f"Cement sale #{txn.cement_record_id}"
		elif txn.bamboo_record_id:
			label = f"Bamboo sale #{txn.bamboo_record_id}"
		else:
			label = f"Txn #{txn.id}"

		allocation_lines.append(f"{label} (NPR {_money(amt)})")
		txn_ids.add(txn.pk)

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
