from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from decimal import Decimal, InvalidOperation
from datetime import datetime

from .models import AlertNotification, AlertSource, AlertType, Customer, JCBRecord, Sale, TipperRecord, Transaction
from .models import TransactionCategory
from .models import (
    RecordStatus,
    BambooRecord,
    BambooRecordType,
    BlocksRecord,
    BlocksRecordType,
    BlocksUnitType,
    CementRecord,
    CementRecordType,
    CementUnitType,
)


def _decorate_widget(field_name, field):
    existing_class = field.widget.attrs.get("class", "")
    if field_name in {"alert_enabled"}:
        field.widget.attrs["class"] = f"checkbox checkbox-primary {existing_class}".strip()
        return
    if field_name in {"description", "profile_notes", "address", "items"}:
        field.widget.attrs["class"] = f"textarea textarea-bordered w-full {existing_class}".strip()
    elif field_name in {"type", "payment_method", "customer", "status", "sale", "category", "alert_type", "payment_status"}:
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
    customer_input = forms.CharField(required=False)

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
        customer_name = (cleaned_data.get("customer_input") or "").strip()

        if customer_name:
            customer = Customer.objects.filter(name__iexact=customer_name).order_by("name").first()
            if customer is None:
                customer = Customer.objects.create(name=customer_name)
            cleaned_data["customer"] = customer
        else:
            cleaned_data["customer"] = None

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
        self.fields["customer_input"].widget.attrs["placeholder"] = "Type or choose a customer name"
        self.fields["customer_input"].widget.attrs["autocomplete"] = "off"
        self.fields["customer_input"].widget.attrs["list"] = "sale-customer-options"
        if not self.is_bound and not (self.instance and self.instance.pk):
            self.fields["due_date"].initial = timezone.localdate()
        if self.instance and self.instance.pk and self.instance.customer:
            self.initial["customer_input"] = self.instance.customer.name
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)


class TransactionForm(forms.ModelForm):
    customer_input = forms.CharField(required=False)
    category_input = forms.CharField(required=False)
    sale_input = forms.CharField(required=False)

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

    def clean(self):
        cleaned_data = super().clean()

        customer_name = (cleaned_data.get("customer_input") or "").strip()
        if customer_name:
            customer = Customer.objects.filter(name__iexact=customer_name).order_by("name").first()
            if customer is None:
                customer = Customer.objects.create(name=customer_name)
            cleaned_data["customer"] = customer
        else:
            cleaned_data["customer"] = None

        category_name = (cleaned_data.get("category_input") or "").strip()
        if category_name:
            category = TransactionCategory.objects.filter(name__iexact=category_name).order_by("name").first()
            if category is None:
                category = TransactionCategory.objects.create(name=category_name, is_predefined=False)
            cleaned_data["category"] = category
        else:
            cleaned_data["category"] = None

        sale_value = (cleaned_data.get("sale_input") or "").strip()
        if sale_value:
            cleaned_data["sale"] = Sale.objects.filter(invoice_number__iexact=sale_value).order_by("-date").first()
        else:
            cleaned_data["sale"] = None

        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].required = False
        self.fields["category"].required = False
        self.fields["sale"].required = False
        self.fields["customer_input"].widget.attrs["placeholder"] = "Type or choose a customer name"
        self.fields["customer_input"].widget.attrs["autocomplete"] = "off"
        self.fields["customer_input"].widget.attrs["list"] = "txn-customer-options"
        self.fields["category_input"].widget.attrs["placeholder"] = "Type or choose a category"
        self.fields["category_input"].widget.attrs["autocomplete"] = "off"
        self.fields["category_input"].widget.attrs["list"] = "txn-category-options"
        self.fields["sale_input"].widget.attrs["placeholder"] = "Type or choose invoice number"
        self.fields["sale_input"].widget.attrs["autocomplete"] = "off"
        self.fields["sale_input"].widget.attrs["list"] = "txn-sale-options"
        if self.instance and self.instance.pk:
            if self.instance.customer:
                self.initial["customer_input"] = self.instance.customer.name
            if self.instance.category:
                self.initial["category_input"] = self.instance.category.name
            if self.instance.sale:
                self.initial["sale_input"] = self.instance.sale.invoice_number
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)


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
            "description",
            "amount",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3, "placeholder": "Optional notes for this record"}),
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


class ManualAlertForm(forms.ModelForm):
    class Meta:
        model = AlertNotification
        fields = ["due_date", "title", "message", "alert_type"]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "message": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise forms.ValidationError("Title is required.")
        return title

    def clean(self):
        cleaned_data = super().clean()
        due_date = cleaned_data.get("due_date")
        title = cleaned_data.get("title")

        if due_date and title:
            duplicate_qs = AlertNotification.objects.filter(
                source_type=AlertSource.MANUAL,
                due_date=due_date,
                title__iexact=title,
            )
            if self.instance and self.instance.pk:
                duplicate_qs = duplicate_qs.exclude(pk=self.instance.pk)
            if duplicate_qs.exists():
                self.add_error(
                    "title",
                    "A manual alert with this title already exists for this due date.",
                )

        if not cleaned_data.get("alert_type"):
            cleaned_data["alert_type"] = AlertType.MANUAL

        return cleaned_data

    def save(self, commit=True):
        alert = super().save(commit=False)
        alert.source_type = AlertSource.MANUAL
        alert.source_id = None
        alert.customer = None
        alert.amount = Decimal("0.00")
        if alert.pk is None:
            alert.is_active = True
            alert.is_read = False
            alert.resolved_at = None
        if not alert.alert_type:
            alert.alert_type = AlertType.MANUAL
        if commit:
            alert.save()
        return alert

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["alert_type"].required = False
        self.fields["alert_type"].choices = [
            ("", "Manual (default)"),
            *AlertType.choices,
        ]
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)


class BlocksRecordForm(forms.ModelForm):
    """Form for creating and editing Blocks Records."""
    customer_input = forms.CharField(required=False)
    
    class Meta:
        model = BlocksRecord
        fields = [
            "date",
            "record_type",
            "customer",
            "payment_status",
            "investment",
            "unit_type",
            "quantity",
            "price_per_unit",
            "sale_income",
            "notes",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3, "placeholder": "Additional details or remarks"}),
            "investment": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "quantity": forms.NumberInput(attrs={"min": "0"}),
            "price_per_unit": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "sale_income": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        record_type = cleaned_data.get("record_type")
        payment_status = cleaned_data.get("payment_status")
        customer_name = (cleaned_data.get("customer_input") or "").strip()
        investment = cleaned_data.get("investment")
        quantity = cleaned_data.get("quantity")
        unit_type = cleaned_data.get("unit_type")
        price_per_unit = cleaned_data.get("price_per_unit")

        if record_type == BlocksRecordType.SALE:
            cleaned_data["payment_status"] = payment_status or RecordStatus.PENDING
            if customer_name:
                customer = Customer.objects.filter(name__iexact=customer_name).order_by("name").first()
                if customer is None:
                    customer = Customer.objects.create(name=customer_name)
                cleaned_data["customer"] = customer
            else:
                cleaned_data["customer"] = None
        else:
            cleaned_data["payment_status"] = None
            cleaned_data["customer"] = None
        
        if record_type == BlocksRecordType.INVESTMENT:
            # For INVESTMENT records, investment must be provided
            if investment is None or investment <= 0:
                self.add_error("investment", "Investment amount is required for investment records.")
        
        elif record_type == BlocksRecordType.STOCK:
            # For STOCK records, quantity and unit_type must be provided
            if not unit_type:
                self.add_error("unit_type", "Unit type is required for stock records.")
            if quantity is None or quantity <= 0:
                self.add_error("quantity", "Quantity must be greater than 0 for stock records.")
        
        elif record_type == BlocksRecordType.SALE:
            # For SALE records, quantity, unit_type, and price_per_unit must be provided
            if not unit_type:
                self.add_error("unit_type", "Unit type is required for sale records.")
            if quantity is None or quantity <= 0:
                self.add_error("quantity", "Quantity must be greater than 0 for sale records.")
            if price_per_unit is None or price_per_unit < 0:
                self.add_error("price_per_unit", "Price per unit is required and must be greater than or equal to 0.")
        
        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sale_income"].required = False
        self.fields["sale_income"].disabled = True
        self.fields["sale_income"].label = "Sale Income"
        self.fields["sale_income"].help_text = "Auto-calculated from quantity × price"
        self.fields["payment_status"].required = False
        self.fields["payment_status"].label = "Payment Status"
        self.fields["payment_status"].help_text = "Sale records only"
        self.fields["customer"].required = False
        self.fields["customer"].queryset = Customer.objects.order_by("name")
        self.fields["customer"].help_text = "Optional. You can assign a customer for sale records."
        self.fields["customer_input"].widget.attrs["placeholder"] = "Type or choose a customer name"
        self.fields["customer_input"].widget.attrs["autocomplete"] = "off"
        self.fields["customer_input"].widget.attrs["list"] = "blocks-customer-options"
        if not self.is_bound and not self.instance.pk:
            self.fields["payment_status"].initial = RecordStatus.PENDING
        if self.instance and self.instance.pk and self.instance.customer:
            self.initial["customer_input"] = self.instance.customer.name
        self.fields["investment"].required = False
        self.fields["quantity"].required = False
        self.fields["unit_type"].required = False
        self.fields["price_per_unit"].required = False
        self.fields["record_type"].label = "Record Type"
        
        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)


class CementRecordForm(forms.ModelForm):
    """Form for creating and editing Cement Records."""
    customer_input = forms.CharField(required=False)

    class Meta:
        model = CementRecord
        fields = [
            "date",
            "record_type",
            "customer",
            "payment_status",
            "investment",
            "unit_type",
            "quantity",
            "price_per_unit",
            "sale_income",
            "notes",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3, "placeholder": "Additional details or remarks"}),
            "investment": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "quantity": forms.NumberInput(attrs={"min": "0"}),
            "price_per_unit": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "sale_income": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        record_type = cleaned_data.get("record_type")
        payment_status = cleaned_data.get("payment_status")
        customer_name = (cleaned_data.get("customer_input") or "").strip()
        investment = cleaned_data.get("investment")
        quantity = cleaned_data.get("quantity")
        unit_type = cleaned_data.get("unit_type")
        price_per_unit = cleaned_data.get("price_per_unit")

        if record_type == CementRecordType.SALE:
            cleaned_data["payment_status"] = payment_status or RecordStatus.PENDING
            if customer_name:
                customer = Customer.objects.filter(name__iexact=customer_name).order_by("name").first()
                if customer is None:
                    customer = Customer.objects.create(name=customer_name)
                cleaned_data["customer"] = customer
            else:
                cleaned_data["customer"] = None
        else:
            cleaned_data["payment_status"] = None
            cleaned_data["customer"] = None

        if record_type == CementRecordType.INVESTMENT:
            if investment is None or investment <= 0:
                self.add_error("investment", "Investment amount is required for investment records.")
        elif record_type == CementRecordType.STOCK:
            if not unit_type:
                self.add_error("unit_type", "Unit type is required for stock records.")
            if quantity is None or quantity <= 0:
                self.add_error("quantity", "Quantity must be greater than 0 for stock records.")
        elif record_type == CementRecordType.SALE:
            if not unit_type:
                self.add_error("unit_type", "Unit type is required for sale records.")
            if quantity is None or quantity <= 0:
                self.add_error("quantity", "Quantity must be greater than 0 for sale records.")
            if price_per_unit is None or price_per_unit < 0:
                self.add_error("price_per_unit", "Price per unit is required and must be greater than or equal to 0.")

        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sale_income"].required = False
        self.fields["sale_income"].disabled = True
        self.fields["sale_income"].label = "Sale Income"
        self.fields["sale_income"].help_text = "Auto-calculated from quantity × price"
        self.fields["payment_status"].required = False
        self.fields["payment_status"].label = "Payment Status"
        self.fields["payment_status"].help_text = "Sale records only"
        self.fields["customer"].required = False
        self.fields["customer"].queryset = Customer.objects.order_by("name")
        self.fields["customer"].help_text = "Optional. You can assign a customer for sale records."
        self.fields["customer_input"].widget.attrs["placeholder"] = "Type or choose a customer name"
        self.fields["customer_input"].widget.attrs["autocomplete"] = "off"
        self.fields["customer_input"].widget.attrs["list"] = "cement-customer-options"
        if not self.is_bound and not self.instance.pk:
            self.fields["payment_status"].initial = RecordStatus.PENDING
        if self.instance and self.instance.pk and self.instance.customer:
            self.initial["customer_input"] = self.instance.customer.name
        self.fields["investment"].required = False
        self.fields["quantity"].required = False
        self.fields["unit_type"].required = False
        self.fields["price_per_unit"].required = False
        self.fields["record_type"].label = "Record Type"

        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)


class BambooRecordForm(forms.ModelForm):
    """Form for creating and editing Bamboo Records."""
    customer_input = forms.CharField(required=False)

    class Meta:
        model = BambooRecord
        fields = [
            "date",
            "record_type",
            "customer",
            "payment_status",
            "investment",
            "quantity",
            "price_per_unit",
            "sale_income",
            "notes",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3, "placeholder": "Additional details or remarks"}),
            "investment": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "quantity": forms.NumberInput(attrs={"min": "0"}),
            "price_per_unit": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "sale_income": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        record_type = cleaned_data.get("record_type")
        payment_status = cleaned_data.get("payment_status")
        customer_name = (cleaned_data.get("customer_input") or "").strip()
        investment = cleaned_data.get("investment")
        quantity = cleaned_data.get("quantity")
        price_per_unit = cleaned_data.get("price_per_unit")

        if record_type == BambooRecordType.SALE:
            cleaned_data["payment_status"] = payment_status or RecordStatus.PENDING
            if customer_name:
                customer = Customer.objects.filter(name__iexact=customer_name).order_by("name").first()
                if customer is None:
                    customer = Customer.objects.create(name=customer_name)
                cleaned_data["customer"] = customer
            else:
                cleaned_data["customer"] = None
        else:
            cleaned_data["payment_status"] = None
            cleaned_data["customer"] = None

        if record_type == BambooRecordType.INVESTMENT:
            if investment is None or investment <= 0:
                self.add_error("investment", "Investment amount is required for investment records.")
        elif record_type == BambooRecordType.STOCK:
            if quantity is None or quantity <= 0:
                self.add_error("quantity", "Quantity must be greater than 0 for stock records.")
        elif record_type == BambooRecordType.SALE:
            if quantity is None or quantity <= 0:
                self.add_error("quantity", "Quantity must be greater than 0 for sale records.")
            if price_per_unit is None or price_per_unit < 0:
                self.add_error("price_per_unit", "Price per unit is required and must be greater than or equal to 0.")

        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sale_income"].required = False
        self.fields["sale_income"].disabled = True
        self.fields["sale_income"].label = "Sale Income"
        self.fields["sale_income"].help_text = "Auto-calculated from quantity × price"
        self.fields["payment_status"].required = False
        self.fields["payment_status"].label = "Payment Status"
        self.fields["payment_status"].help_text = "Sale records only"
        self.fields["customer"].required = False
        self.fields["customer"].queryset = Customer.objects.order_by("name")
        self.fields["customer"].help_text = "Optional. You can assign a customer for sale records."
        self.fields["customer_input"].widget.attrs["placeholder"] = "Type or choose a customer name"
        self.fields["customer_input"].widget.attrs["autocomplete"] = "off"
        self.fields["customer_input"].widget.attrs["list"] = "bamboo-customer-options"
        if not self.is_bound and not self.instance.pk:
            self.fields["payment_status"].initial = RecordStatus.PENDING
        if self.instance and self.instance.pk and self.instance.customer:
            self.initial["customer_input"] = self.instance.customer.name
        self.fields["investment"].required = False
        self.fields["quantity"].required = False
        self.fields["price_per_unit"].required = False
        self.fields["record_type"].label = "Record Type"

        for field_name, field in self.fields.items():
            _decorate_widget(field_name, field)
