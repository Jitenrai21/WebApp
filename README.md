# Company Flow Management App

Company Flow Management App is a Django-based operational finance system for tracking daily business cash flow, sales invoices, customer balances, JCB records, and due-date alerts.

It is designed for businesses that need practical day-to-day control over:

- sales and payment progress
- cash income and expenses
- customer-level balances and allocations
- machinery work records (JCB)
- overdue and upcoming notifications

## Core Capabilities

- Dashboard with KPI cards, trend charts, and date-range filtering (defaulting to recent period)
- Sales ledger with invoice lifecycle, due-date tracking, receipt capture, and pagination
- NPR-style amount rendering across profile cards and tables (comma-grouped, no trailing .00 for whole amounts)
- Customer profile management with credit balance and manual due support
- Customer payment allocation across multiple pending invoices with allocation history
- Automatic credit application to pending sales during sale create/edit when customer credit is available
- Safe rollback on sale deletion that restores auto-applied credit to customer balance
- Cash entry management (income and expense) with categories, links, optional attachments, and paginated list view
- JCB operational records with hour calculation, income/expense summaries, paid-state flow, and paginated list view
- Tipper records module with expense vs value-added tracking, optional descriptions, detail view, analytics cards, and paginated list view
- Standardized paginated list views at 20 rows per page across sales, cash entries, JCB, and tipper tables
- Alert center with overdue/upcoming pipeline, timeline history, and status resolution
- Manual alert creation, editing, and deletion for custom reminders
- Alert badge that emphasizes unresolved overdue items for quick action
- HTMX partial responses for responsive filtering, pagination, and table refresh without full page reloads
- Authentication through Django auth views (login/logout)

## Technology Stack

- Python
- Django 6.0.3
- PostgreSQL
- HTMX-enabled partial updates in templates
- ECharts chart rendering for dashboard visualizations

## Project Structure

```text
CompanyFlowManagementApp/
├── .env
├── .gitignore
├── CompanyLogo.png
├── manage.py
├── README.md
├── requirements.txt
├── config/
│   ├── __init__.py
│   ├── settings.py
│   ├── settings_test.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
├── core/
│   ├── __init__.py
│   ├── models.py
│   ├── views.py
│   ├── urls.py
│   ├── forms.py
│   ├── admin.py
│   ├── apps.py
│   ├── tests.py
│   ├── migrations/
│   │   ├── 0001_initial.py ... 0018_*.py
│   │   └── __init__.py
│   ├── management/
│   │   └── commands/
│   │       ├── process_alert_notifications.py
│   │       └── sync_paid_sales_income.py
│   └── static/
│       └── core/
│           ├── CompanyLogo.png
│           └── theme.css
├── documentations/
│   ├── breakdown.txt
│   ├── db_log.txt
│   └── roadmap.txt
├── templates/
│   ├── base.html
│   ├── registration/
│   │   ├── login.html
│   │   └── logged_out.html
│   └── core/
│       ├── alerts.html
│       ├── cash_entries.html
│       ├── customers.html
│       ├── customer_detail.html
│       ├── customer_form.html
│       ├── dashboard.html
│       ├── jcb_records.html
│       ├── jcb_record_form.html
│       ├── manual_alert_form.html
│       ├── sales.html
│       ├── sale_form.html
│       ├── sale_detail.html
│       ├── tipper_records.html
│       ├── tipper_record_detail.html
│       ├── tipper_record_form.html
│       ├── transaction_detail.html
│       ├── transaction_form.html
│       └── partials/
│           ├── alerts_badge.html
│           ├── alerts_content.html
│           ├── dashboard_content.html
│           ├── customer_payment_section.html
│           ├── jcb_records_table.html
│           ├── sales_table.html
│           ├── sale_receipts_panel.html
│           ├── tipper_records_table.html
│           ├── transaction_table.html
└── assets/
```

## Data Model Overview

Key entities in the application:

- Customer: profile, terms, opening/credit balance, and manual due amount
- Sale: invoice metadata, JSON item lines, due-date alerts, payment state, and status
- TransactionCategory: predefined/custom category taxonomy for cash entries
- Transaction: categorized income/expense, optional customer/sale/JCB links, and attachments
- CustomerPayment: customer-level payment events with allocated and unallocated portions
- PaymentAllocation: split allocation records from customer payment to one or many sales
- JCBRecord: machine work logs with hour calculation, rates, totals, and operational expense
- TipperItem: normalized item/entity for tipper tracking
- TipperRecord: tipper expense and value-added transactional rows with optional description notes
- AlertNotification: overdue/upcoming/manual timeline alerts with active/resolved state

## URL Surface

Main routes include:

- / (Dashboard)
- /sales, /sales/new, /sales/<id>, /sales/<id>/edit, /sales/<id>/delete
- /sales/<id>/toggle-alert, /sales/<id>/mark-paid, /sales/<id>/receipts/add
- /cash-entries, /cash-entries/new, /cash-entries/<id>, /cash-entries/<id>/edit, /cash-entries/<id>/delete
- /customers, /customers/new, /customers/<id>, /customers/<id>/edit, /customers/<id>/delete
- /customers/<id>/allocate-payment
- /jcb-records, /jcb-records/new, /jcb-records/<id>/edit, /jcb-records/<id>/mark-paid, /jcb-records/<id>/delete
- /tipper-records, /tipper-records/new, /tipper-records/<id>, /tipper-records/<id>/edit, /tipper-records/<id>/delete
- /alerts, /alerts/badge
- /alerts/manual/new, /alerts/manual/<id>/edit, /alerts/manual/<id>/delete
- /alerts/notifications/<id>/resolve
- /accounts/login, /accounts/logout

## Local Development Setup

### 1) Prerequisites

- Python 3.11+
- PostgreSQL
- pip

### 2) Clone and enter project

```bash
git clone <your-repository-url>
cd CompanyFlowManagementApp
```

### 3) Create virtual environment

Windows PowerShell:

```powershell
python -m venv env
.\env\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python -m venv env
source env/bin/activate
```

### 4) Install dependencies

```bash
pip install -r requirements.txt
```

### 5) Configure environment variables

Create a .env file in project root:

```env
DJANGO_SECRET_KEY=replace-with-a-secure-key
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost
DJANGO_CSRF_TRUSTED_ORIGINS=

POSTGRES_DB=company_flow_db
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
```

### 6) Apply migrations and create admin user

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 7) Run development server

```bash
python manage.py runserver
```

Visit:

- http://127.0.0.1:8000/

## Management Commands

### Process alert notifications

Creates or updates overdue and upcoming alert timeline entries:

```bash
python manage.py process_alert_notifications
```

### Sync paid sales auto-income entries

Backfills and keeps automatic paid-sale income transactions aligned:

```bash
python manage.py sync_paid_sales_income
```

## Visual Assets and Screenshots

### Dashboard

![Dashboard](assets/screencapture-127-0-0-1-8000-2026-03-28-15_54_34.png)

### Sales

![Sales List](assets/screencapture-127-0-0-1-8000-sales-2026-03-28-15_56_29.png)
![Add Sale](assets/screencapture-127-0-0-1-8000-sales-new-2026-03-28-15_56_44.png)

### JCB Records

![JCB Records](assets/screencapture-127-0-0-1-8000-jcb-records-2026-03-28-15_57_22.png)

### Tipper Records

![Tipper Records](assets/screencapture-127-0-0-1-8000-tipper-records-2026-03-28-15_57_54.png)
![Add Tipper Record](assets/screencapture-127-0-0-1-8000-tipper-records-new-2026-03-28-15_58_07.png)

### Customers

![Customers List](assets/screencapture-127-0-0-1-8000-customers-2026-03-28-15_59_43.png)
![Add Customer](assets/screencapture-127-0-0-1-8000-customers-new-2026-03-28-16_00_05.png)

### Cash Entries

![Cash Entries](assets/screencapture-127-0-0-1-8000-cash-entries-2026-03-28-15_55_43.png)
![Add Cash Entry](assets/screencapture-127-0-0-1-8000-cash-entries-new-2026-03-28-15_55_59.png)

### Alerts

![Alerts](assets/screencapture-127-0-0-1-8000-alerts-2026-03-28-16_00_31.png)
![Create Manual Alert](assets/screencapture-127-0-0-1-8000-alerts-manual-new-2026-03-28-16_00_45.png)

## Testing and Validation

Quick sanity check:

```bash
python manage.py check
```

Run test suite:

```bash
python manage.py test
```

Run tests with SQLite test settings (useful when PostgreSQL test DB create privileges are restricted):

```bash
python manage.py test --settings=config.settings_test
```

## Deployment Notes

Before production deployment:

- Set DJANGO_DEBUG=False
- Set secure DJANGO_SECRET_KEY
- Configure DJANGO_ALLOWED_HOSTS and DJANGO_CSRF_TRUSTED_ORIGINS properly
- Use production PostgreSQL credentials
- Serve static files via STATIC_ROOT and your web server/CDN strategy
- Schedule process_alert_notifications command via cron/task scheduler

## License

Add your project license here (for example, MIT, Apache-2.0, or Proprietary).
