from django.contrib import admin

from .models import (
    AlertNotification,
    Customer,
    CustomerPayment,
    JCBRecord,
    PaymentAllocation,
    Sale,
    Transaction,
)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "phone",
        "type",
        "opening_balance",
        "credit_balance",
        "created_at",
    )
    list_filter = ("type", "created_at")
    search_fields = ("name", "phone", "address", "credit_terms")
    ordering = ("name",)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "customer",
        "sale",
        "jcb_record",
        "type",
        "payment_method",
        "category",
        "amount",
    )
    list_filter = ("type", "payment_method", "date", "category", "sale", "jcb_record")
    search_fields = (
        "customer__name",
        "category",
        "description",
    )
    autocomplete_fields = ("customer", "sale", "jcb_record")
    date_hierarchy = "date"
    ordering = ("-date",)


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "date",
        "customer",
        "total_amount",
        "paid_amount",
        "due_date",
        "status",
    )
    list_filter = ("status", "date", "due_date")
    search_fields = ("invoice_number", "customer__name", "notes")
    autocomplete_fields = ("customer",)
    date_hierarchy = "date"
    ordering = ("-date",)


@admin.register(AlertNotification)
class AlertNotificationAdmin(admin.ModelAdmin):
    list_display = (
        "alert_type",
        "source_type",
        "source_id",
        "customer",
        "due_date",
        "amount",
        "is_active",
        "is_read",
    )
    list_filter = ("alert_type", "source_type", "is_active", "is_read", "due_date")
    search_fields = ("title", "message", "customer__name")
    autocomplete_fields = ("customer",)


@admin.register(CustomerPayment)
class CustomerPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "payment_date",
        "customer",
        "amount",
        "payment_method",
        "allocated_amount",
        "unallocated_amount",
    )
    list_filter = ("payment_method", "payment_date")
    search_fields = ("customer__name", "notes")
    autocomplete_fields = ("customer",)
    date_hierarchy = "payment_date"
    ordering = ("-payment_date", "-created_at")


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(admin.ModelAdmin):
    list_display = ("customer_payment", "sale", "amount", "transaction", "created_at")
    search_fields = ("sale__invoice_number", "customer_payment__customer__name")
    autocomplete_fields = ("customer_payment", "sale", "transaction")


@admin.register(JCBRecord)
class JCBRecordAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "site_name",
        "start_time",
        "end_time",
        "total_work_hours",
        "rate",
        "total_amount",
        "status",
        "expense_item",
        "expense_amount",
    )
    list_filter = ("status", "date")
    search_fields = ("site_name", "expense_item")
    date_hierarchy = "date"
    ordering = ("-date", "-created_at")
