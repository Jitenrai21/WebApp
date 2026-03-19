from django import forms
from django.core.exceptions import ValidationError

from .models import Customer, Sale, Transaction


def _decorate_widget(field_name, field):
    existing_class = field.widget.attrs.get("class", "")
    if field_name in {"description", "profile_notes", "address", "items"}:
        field.widget.attrs["class"] = f"textarea textarea-bordered w-full {existing_class}".strip()
    elif field_name in {"type", "customer", "status", "sale"}:
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


class SaleForm(forms.ModelForm):
    class Meta:
        model = Sale
        fields = [
            "invoice_number",
            "date",
            "customer",
            "items",
            "notes",
            "total_amount",
            "due_date",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "items": forms.Textarea(
                attrs={
                    "rows": 5,
                    "placeholder": '[{"item": "Product A", "quantity": 2, "price": 500}]',
                }
            ),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

        help_texts = {
            "items": "Enter a JSON list of items with item, quantity, and price.",
        }

    def clean_total_amount(self):
        total_amount = self.cleaned_data["total_amount"]
        if total_amount <= 0:
            raise forms.ValidationError("Total amount must be greater than 0.")
        return total_amount

    def clean_items(self):
        items = self.cleaned_data.get("items")
        if not items:
            raise ValidationError("At least one item is required.")
        if not isinstance(items, list):
            raise ValidationError("Items must be a JSON list.")

        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValidationError(f"Item #{index} must be an object.")
            if not item.get("item"):
                raise ValidationError(f"Item #{index} must include 'item'.")

            quantity = item.get("quantity")
            price = item.get("price")
            if quantity is None or price is None:
                raise ValidationError(f"Item #{index} must include quantity and price.")
            try:
                quantity_number = float(quantity)
                price_number = float(price)
            except (TypeError, ValueError):
                raise ValidationError(f"Item #{index} quantity and price must be numbers.")

            if quantity_number <= 0 or price_number < 0:
                raise ValidationError(
                    f"Item #{index} quantity must be > 0 and price cannot be negative."
                )
        return items

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].required = True
        self.fields["customer"].error_messages["required"] = "Please select a customer."
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)
        self.fields["customer"].widget.attrs["data-customer-autocomplete"] = "true"


class TransactionForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = [
            "date",
            "due_date",
            "amount",
            "type",
            "category",
            "description",
            "customer",
            "sale",
            "attachment",
            "status",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Amount must be greater than 0.")
        return amount

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].required = True
        self.fields["customer"].error_messages["required"] = "Please select a customer."
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)
        self.fields["customer"].widget.attrs["data-customer-autocomplete"] = "true"
        self.fields["sale"].required = False


class SaleReceiptForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = [
            "date",
            "amount",
            "category",
            "description",
            "status",
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
