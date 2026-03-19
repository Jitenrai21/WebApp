from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("cash-entries/", views.cash_entries, name="cash_entries"),
    path("cash-entries/new/", views.transaction_create, name="transaction_create"),
    path("cash-entries/<int:pk>/edit/", views.transaction_edit, name="transaction_edit"),
    path("sales/", views.sales, name="sales"),
    path("sales/new/", views.sale_create, name="sale_create"),
    path("sales/<int:pk>/", views.sale_detail, name="sale_detail"),
    path("sales/<int:pk>/edit/", views.sale_edit, name="sale_edit"),
    path("sales/<int:pk>/receipts/add/", views.sale_receipt_create, name="sale_receipt_create"),
    path("customers/", views.customers, name="customers"),
    path("customers/new/", views.customer_create, name="customer_create"),
    path("customers/<int:pk>/", views.customer_detail, name="customer_detail"),
    path("customers/<int:pk>/edit/", views.customer_edit, name="customer_edit"),
    path("alerts/", views.alerts, name="alerts"),
    path("alerts/badge/", views.alerts_badge, name="alerts_badge"),
    path(
        "alerts/notifications/<int:pk>/resolve/",
        views.alert_notification_resolve,
        name="alert_notification_resolve",
    ),
]
