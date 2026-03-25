from django.db import models
from django.db.models import Sum
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal, InvalidOperation


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class CustomerType(models.TextChoices):
    REGULAR = "regular", "Regular"
    SUPPLIER = "supplier", "Supplier"
    WHOLESALE = "wholesale", "Wholesale"


class RecordStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"


class TransactionType(models.TextChoices):
    INCOME = "income", "Income"
    EXPENSE = "expense", "Expense"


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Cash"
    BANK_TRANSFER = "bank_transfer", "Bank Transfer"
    CHEQUE = "cheque", "Cheque"
    MOBANKING = "mobanking", "Mobanking"
    OTHER = "other", "Other"


class TransactionCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_predefined = models.BooleanField(default=True, help_text="If True, this is a system-defined category")
    
    class Meta:
        ordering = ["name"]
    
    def __str__(self) -> str:
        return self.name


class Customer(TimeStampedModel):
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    credit_terms = models.CharField(max_length=120, blank=True)
    profile_notes = models.TextField(blank=True)
    type = models.CharField(
        max_length=20,
        choices=CustomerType.choices,
        default=CustomerType.REGULAR,
    )
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    manual_due_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="Manually added due amount for legacy/existing dues")

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Transaction(TimeStampedModel):
    date = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    type = models.CharField(max_length=10, choices=TransactionType.choices)
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )
    category = models.ForeignKey(
        TransactionCategory,
        on_delete=models.SET_NULL,
        related_name="transactions",
        blank=True,
        null=True,
    )
    description = models.TextField(blank=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        related_name="transactions",
        blank=True,
        null=True,
    )
    sale = models.ForeignKey(
        "Sale",
        on_delete=models.SET_NULL,
        related_name="receipts",
        blank=True,
        null=True,
    )
    jcb_record = models.ForeignKey(
        "JCBRecord",
        on_delete=models.SET_NULL,
        related_name="transactions",
        blank=True,
        null=True,
    )
    attachment = models.FileField(upload_to="transactions/", blank=True, null=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["customer"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()} - {self.amount} ({self.date})"

class Sale(TimeStampedModel):
    invoice_number = models.CharField(max_length=40, unique=True)
    date = models.DateField(default=timezone.now)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        related_name="sales",
        blank=True,
        null=True,
    )
    items = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2)
    due_date = models.DateField(blank=True, null=True)
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(
        max_length=10,
        choices=RecordStatus.choices,
        default=RecordStatus.PENDING,
    )
    alert_enabled = models.BooleanField(default=False)

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return self.invoice_number

    @property
    def total_received(self):
        aggregated = self.receipts.filter(type=TransactionType.INCOME).aggregate(total=Sum("amount"))
        return aggregated["total"] or 0

    @property
    def payment_status(self):
        received = self.total_received
        if received <= 0:
            return "unpaid"
        if received >= self.total_amount:
            return "paid"
        return "partial"

    @property
    def alert_state(self):
        if not self.alert_enabled:
            return "none"
        if not self.due_date:
            return "none"
        if self.payment_status == "paid":
            return "resolved"

        today = timezone.localdate()
        if self.due_date < today:
            return "overdue"
        if self.due_date <= today + timedelta(days=7):
            return "upcoming"
        return "none"


class JCBRecord(TimeStampedModel):
    date = models.DateField(default=timezone.now)
    site_name = models.CharField(max_length=120, blank=True, null=True)
    start_time = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0.00"))
    end_time = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0.00"))
    total_work_hours = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    status = models.CharField(
        max_length=10,
        choices=RecordStatus.choices,
        default=RecordStatus.PENDING,
    )
    rate = models.DecimalField(max_digits=12, decimal_places=2, default=2000)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    expense_item = models.CharField(max_length=120, blank=True)
    expense_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        site = self.site_name or "N/A"
        return f"JCB {self.date} [{site}] ({self.start_time}-{self.end_time})"

    @property
    def income_amount(self):
        if self.total_amount is not None:
            return self.total_amount.quantize(Decimal("0.01"))
        return (self.total_work_hours * self.rate).quantize(Decimal("0.01"))

    def save(self, *args, **kwargs):
        try:
            worked = Decimal(str(self.end_time)) - Decimal(str(self.start_time))
        except (InvalidOperation, TypeError, ValueError):
            worked = Decimal("0")

        if worked < 0:
            worked = Decimal("0")

        self.total_work_hours = worked.quantize(Decimal("0.01"))
        if self.total_amount is None:
            self.total_amount = (self.total_work_hours * self.rate).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)


class TipperRecordType(models.TextChoices):
    EXPENSE = "expense", "Expense"
    VALUE_ADDED = "value_added", "Value Added"


class TipperItem(models.Model):
    name = models.CharField(max_length=120, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class TipperRecord(TimeStampedModel):
    date = models.DateField(default=timezone.now)
    item = models.ForeignKey(
        TipperItem,
        on_delete=models.CASCADE,
        related_name="tipper_records",
    )
    record_type = models.CharField(max_length=20, choices=TipperRecordType.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["date"], name="core_tipperr_date_idx"),
            models.Index(fields=["item"], name="core_tipperr_item_idx"),
            models.Index(fields=["record_type"], name="core_tipperr_type_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_record_type_display()} - {self.item.name} ({self.amount})"


class AlertType(models.TextChoices):
    OVERDUE = "overdue", "Overdue"
    UPCOMING = "upcoming", "Upcoming"
    MANUAL = "manual", "Manual"


class AlertSource(models.TextChoices):
    SALE = "sale", "Sale"
    TRANSACTION = "transaction", "Transaction"
    MANUAL = "manual", "Manual"


class AlertNotification(TimeStampedModel):
    alert_type = models.CharField(max_length=20, choices=AlertType.choices)
    source_type = models.CharField(max_length=20, choices=AlertSource.choices)
    source_id = models.PositiveIntegerField(blank=True, null=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name="alert_notifications",
        blank=True,
        null=True,
    )
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    title = models.CharField(max_length=180)
    message = models.TextField(blank=True)
    is_read = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    resolved_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-due_date", "-created_at"]
        unique_together = (
            "alert_type",
            "source_type",
            "source_id",
            "due_date",
        )
        constraints = [
            models.UniqueConstraint(
                fields=["due_date", "title", "source_type"],
                condition=models.Q(source_type=AlertSource.MANUAL),
                name="core_alertn_manual_due_title_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["is_active", "is_read"]),
            models.Index(fields=["source_type", "source_id"]),
            models.Index(fields=["due_date"]),
        ]

    def __str__(self) -> str:
        source_id = self.source_id if self.source_id is not None else "-"
        return f"{self.get_alert_type_display()} {self.get_source_type_display()} #{source_id}"


class CustomerPayment(TimeStampedModel):
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name="customer_payments",
    )
    payment_date = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )
    allocated_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    unallocated_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-payment_date", "-created_at"]
        indexes = [
            models.Index(fields=["customer", "payment_date"]),
        ]

    def __str__(self) -> str:
        return f"Payment {self.customer.name} {self.amount} on {self.payment_date}"


class PaymentAllocation(TimeStampedModel):
    customer_payment = models.ForeignKey(
        CustomerPayment,
        on_delete=models.CASCADE,
        related_name="allocations",
    )
    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name="payment_allocations",
    )
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.SET_NULL,
        related_name="payment_allocations",
        null=True,
        blank=True,
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["sale"]),
            models.Index(fields=["customer_payment"]),
        ]

    def __str__(self) -> str:
        return f"{self.sale.invoice_number} <- {self.amount}"
