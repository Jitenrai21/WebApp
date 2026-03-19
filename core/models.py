from django.db import models
from django.db.models import Sum
from django.utils import timezone
from datetime import timedelta


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

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Transaction(TimeStampedModel):
    date = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    type = models.CharField(max_length=10, choices=TransactionType.choices)
    category = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    sale = models.ForeignKey(
        "Sale",
        on_delete=models.SET_NULL,
        related_name="receipts",
        blank=True,
        null=True,
    )
    due_date = models.DateField(blank=True, null=True)
    attachment = models.FileField(upload_to="transactions/", blank=True, null=True)
    status = models.CharField(
        max_length=10,
        choices=RecordStatus.choices,
        default=RecordStatus.PENDING,
    )

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()} - {self.amount} ({self.date})"

    @property
    def alert_state(self):
        if not self.due_date:
            return "none"
        if self.status == RecordStatus.PAID:
            return "resolved"

        today = timezone.localdate()
        if self.due_date < today:
            return "overdue"
        if self.due_date <= today + timedelta(days=7):
            return "upcoming"
        return "none"


class Sale(TimeStampedModel):
    invoice_number = models.CharField(max_length=40, unique=True)
    date = models.DateField(default=timezone.now)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales")
    items = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2)
    due_date = models.DateField()
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(
        max_length=10,
        choices=RecordStatus.choices,
        default=RecordStatus.PENDING,
    )

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
        if self.payment_status == "paid":
            return "resolved"

        today = timezone.localdate()
        if self.due_date < today:
            return "overdue"
        if self.due_date <= today + timedelta(days=7):
            return "upcoming"
        return "none"


class AlertType(models.TextChoices):
    OVERDUE = "overdue", "Overdue"
    UPCOMING = "upcoming", "Upcoming"


class AlertSource(models.TextChoices):
    SALE = "sale", "Sale"
    TRANSACTION = "transaction", "Transaction"


class AlertNotification(TimeStampedModel):
    alert_type = models.CharField(max_length=20, choices=AlertType.choices)
    source_type = models.CharField(max_length=20, choices=AlertSource.choices)
    source_id = models.PositiveIntegerField()
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name="alert_notifications",
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
        indexes = [
            models.Index(fields=["is_active", "is_read"]),
            models.Index(fields=["source_type", "source_id"]),
            models.Index(fields=["due_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_alert_type_display()} {self.get_source_type_display()} #{self.source_id}"
