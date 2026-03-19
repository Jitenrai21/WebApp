from django.contrib import admin

from .models import AlertNotification, Customer, Sale, Transaction


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "type", "opening_balance", "created_at")
    list_filter = ("type", "created_at")
    search_fields = ("name", "phone", "address", "credit_terms")
    ordering = ("name",)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("date", "due_date", "customer", "sale", "type", "category", "amount", "status")
    list_filter = ("type", "status", "date", "due_date", "category", "sale")
    search_fields = (
        "customer__name",
        "category",
        "description",
    )
    autocomplete_fields = ("customer", "sale")
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
