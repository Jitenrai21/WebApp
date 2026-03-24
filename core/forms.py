from django import forms
from django.core.exceptions import ValidationError
from decimal import Decimal, InvalidOperation
from datetime import datetime

from .models import Customer, JCBRecord, Sale, TipperRecord, Transaction
from .models import RecordStatus


def _decorate_widget(field_name, field):
    existing_class = field.widget.attrs.get("class", "")
    if field_name in {"alert_enabled"}:
        field.widget.attrs["class"] = f"checkbox checkbox-primary {existing_class}".strip()
        return
    if field_name in {"description", "profile_notes", "address", "items"}:
        field.widget.attrs["class"] = f"textarea textarea-bordered w-full {existing_class}".strip()
    elif field_name in {"type", "payment_method", "customer", "status", "sale", "category"}:
        field.widget.attrs["class"] = f"select select-bordered w-full {existing_class}".strip()
    elif field_name == "attachment":
        field.widget.attrs["class"] = f"file-input file-input-bordered w-full {existing_class}".strip()
    else:
        field.widget.attrs["class"] = f"input input-bordered w-full {existing_class}".strip()


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "name",
            "phone",
            "address",
            "credit_terms",
            "profile_notes",
            "type",
            "opening_balance",
            "manual_due_amount",
        ]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if len(name) < 2:
            raise forms.ValidationError("Customer name must be at least 2 characters.")
        return name

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)
        self.fields["address"].widget.attrs["rows"] = 1


class SaleForm(forms.ModelForm):
    @staticmethod
    def _generate_invoice_number(prefix="INV"):
        counter = 1
        today_stamp = datetime.now().strftime("%Y%m%d")
        while True:
            candidate = f"{prefix}-{today_stamp}-{counter:03d}"
            if not Sale.objects.filter(invoice_number=candidate).exists():
                return candidate
            counter += 1

    class Meta:
        model = Sale
        fields = [
            "invoice_number",
            "date",
            "customer",
            "status",
            "alert_enabled",
            "items",
            "notes",
            "total_amount",
            "due_date",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "items": forms.HiddenInput(),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

        help_texts = {
            "items": "Add items with name and price using the item table.",
        }

    def clean_total_amount(self):
        total_amount = self.cleaned_data["total_amount"]
        if total_amount <= 0:
            raise forms.ValidationError("Total amount must be greater than 0.")
        return total_amount

    def clean_invoice_number(self):
        invoice_number = (self.cleaned_data.get("invoice_number") or "").strip()

        if invoice_number:
            return invoice_number

        # Keep existing invoice number on edit if user leaves this blank.
        if self.instance and self.instance.pk and self.instance.invoice_number:
            return self.instance.invoice_number

        return self._generate_invoice_number()

    def clean_items(self):
        items = self.cleaned_data.get("items")
        if not items:
            raise ValidationError("At least one item is required.")
        if not isinstance(items, list):
            raise ValidationError("Items must be a JSON list.")

        normalized_items = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValidationError(f"Item #{index} must be an object.")
            item_name = str(item.get("item", "")).strip()
            if not item_name:
                raise ValidationError(f"Item #{index} must include 'item'.")

            price = item.get("price")
            if price in (None, ""):
                raise ValidationError(f"Item #{index} must include price.")

            quantity = item.get("quantity", 1)
            try:
                quantity_number = Decimal(str(quantity))
                price_number = Decimal(str(price))
            except (TypeError, ValueError, InvalidOperation):
                raise ValidationError(f"Item #{index} quantity and price must be numbers.")

            if quantity_number <= 0 or price_number < 0:
                raise ValidationError(
                    f"Item #{index} quantity must be > 0 and price cannot be negative."
                )

            amount_number = (quantity_number * price_number).quantize(Decimal("0.01"))
            normalized_items.append(
                {
                    "item": item_name,
                    "quantity": float(quantity_number),
                    "price": float(price_number),
                    "amount": float(amount_number),
                }
            )

        return normalized_items

    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get("status")
        due_date = cleaned_data.get("due_date")
        alert_enabled = cleaned_data.get("alert_enabled")

        if status == RecordStatus.PAID:
            cleaned_data["due_date"] = None
            if alert_enabled:
                cleaned_data["alert_enabled"] = False
        elif not due_date:
            self.add_error("due_date", "Due date is required when sale status is Pending.")

        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["invoice_number"].required = False
        self.fields["invoice_number"].widget.attrs["placeholder"] = "Leave blank to auto-generate"
        self.fields["customer"].required = False
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)
        self.fields["customer"].widget.attrs["data-customer-autocomplete"] = "true"
        self.fields["customer"].widget.attrs["data-customer-placeholder"] = "Search customer by name..."


class TransactionForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = [
            "date",
            "amount",
            "type",
            "payment_method",
            "category",
            "description",
            "customer",
            "sale",
            "attachment",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Amount must be greater than 0.")
        return amount

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].required = False
        self.fields["category"].required = False
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)
        self.fields["customer"].widget.attrs["data-customer-autocomplete"] = "true"
        self.fields["customer"].widget.attrs["data-customer-placeholder"] = "Search customer by name..."
        self.fields["category"].widget.attrs["data-category-autocomplete"] = "true"
        self.fields["category"].widget.attrs["data-category-placeholder"] = "Search or create category..."
        self.fields["sale"].required = False


class SaleReceiptForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = [
            "date",
            "amount",
            "payment_method",
            "category",
            "description",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Receipt amount must be greater than 0.")
        return amount

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)


class JCBRecordForm(forms.ModelForm):
    class Meta:
        model = JCBRecord
        fields = [
            "date",
            "site_name",
            "start_time",
            "end_time",
            "status",
            "rate",
            "total_amount",
            "expense_item",
            "expense_amount",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "end_time": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "rate": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "total_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "expense_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")
        expense_item = (cleaned_data.get("expense_item") or "").strip()
        expense_amount = cleaned_data.get("expense_amount")
        rate = cleaned_data.get("rate")
        total_amount = cleaned_data.get("total_amount")
        status = cleaned_data.get("status")

        has_expense_item = bool(expense_item)
        has_expense_amount = expense_amount not in (None, "")
        has_no_time_input = start_time in (None, "") and end_time in (None, "")
        has_zero_time_input = (
            start_time is not None
            and end_time is not None
            and Decimal(str(start_time)) == Decimal("0")
            and Decimal(str(end_time)) == Decimal("0")
        )
        expense_only_mode = has_expense_item and has_expense_amount and (has_no_time_input or has_zero_time_input)

        if has_expense_item and not has_expense_amount:
            self.add_error("expense_amount", "Enter expense amount when expense item is provided.")
        if has_expense_amount and not has_expense_item:
            self.add_error("expense_item", "Enter expense item when expense amount is provided.")

        if expense_amount is not None and expense_amount != "" and expense_amount < 0:
            self.add_error("expense_amount", "Expense amount cannot be negative.")

        # Expense-only mode: auto-fill non-expense fields so only date + expense pair is needed.
        if expense_only_mode:
            cleaned_data["start_time"] = Decimal("0.00")
            cleaned_data["end_time"] = Decimal("0.00")
            if rate in (None, ""):
                cleaned_data["rate"] = Decimal("2000.00")
            if status in (None, ""):
                cleaned_data["status"] = RecordStatus.PENDING
            if total_amount in (None, ""):
                cleaned_data["total_amount"] = Decimal("0.00")
            cleaned_data["expense_item"] = expense_item
            return cleaned_data

        if (start_time in (None, "")) != (end_time in (None, "")):
            self.add_error("start_time", "Provide both start and end time together.")
            self.add_error("end_time", "Provide both start and end time together.")

        if start_time is None and end_time is None:
            self.add_error("start_time", "Start time is required unless this is an expense-only record.")
            self.add_error("end_time", "End time is required unless this is an expense-only record.")

        if start_time is not None and end_time is not None:
            if start_time < 0:
                self.add_error("start_time", "Start time must be 0 or greater.")
            if end_time < 0:
                self.add_error("end_time", "End time must be 0 or greater.")

            if end_time <= start_time:
                self.add_error("end_time", "End time must be greater than start time.")

        if total_amount is not None and total_amount < 0:
            self.add_error("total_amount", "Total amount cannot be negative.")

        if rate in (None, ""):
            cleaned_data["rate"] = Decimal("2000.00")
            rate = cleaned_data["rate"]

        if status in (None, ""):
            cleaned_data["status"] = RecordStatus.PENDING

        if total_amount in (None, "") and start_time is not None and end_time is not None and rate is not None:
            worked = end_time - start_time
            if worked > 0:
                cleaned_data["total_amount"] = (worked * rate).quantize(Decimal("0.01"))

        cleaned_data["expense_item"] = expense_item
        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["start_time"].required = False
        self.fields["end_time"].required = False
        self.fields["rate"].required = False
        self.fields["status"].required = False
        self.fields["total_amount"].required = False
        if self.instance and self.instance.pk and self.instance.total_amount is None:
            worked = self.instance.end_time - self.instance.start_time
            if worked > 0:
                self.initial["total_amount"] = (worked * self.instance.rate).quantize(Decimal("0.01"))
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)


class TipperRecordForm(forms.ModelForm):
    class Meta:
        model = TipperRecord
        fields = [
            "date",
            "item",
            "record_type",
            "amount",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Amount must be greater than 0.")
        return amount

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)
