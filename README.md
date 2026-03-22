# Company Flow Management App

![Company Logo](CompanyLogo.png)

Company Flow Management App is a Django-based operational finance system for tracking daily business cash flow, sales invoices, customer balances, JCB records, and due-date alerts.

It is designed for businesses that need practical day-to-day control over:

- sales and payment progress
- cash income and expenses
- customer-level balances and allocations
- machinery work records (JCB)
- overdue and upcoming notifications

## Core Capabilities

- Dashboard with KPI cards and business trend charts
- Sales ledger with invoice lifecycle and receipt tracking
- Customer profile management with credit and manual due support
- Customer payment allocation against one or more pending invoices
- Cash entry management (income and expense) with optional attachments
- JCB operational records with work hour and amount summary
- Notification timeline for overdue and upcoming items
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
├── CompanyLogo.png
├── manage.py
├── requirements.txt
├── config/
│   ├── settings.py
│   ├── settings_test.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
├── core/
│   ├── models.py
│   ├── views.py
│   ├── urls.py
│   ├── forms.py
│   ├── admin.py
│   ├── tests.py
│   ├── migrations/
│   ├── management/
│   │   └── commands/
│   │       ├── process_alert_notifications.py
│   │       └── sync_paid_sales_income.py
│   └── static/
│       └── core/
│           ├── CompanyLogo.png
│           └── theme.css
├── templates/
│   ├── base.html
│   ├── registration/
│   │   ├── login.html
│   │   └── logged_out.html
│   └── core/
│       ├── dashboard.html
│       ├── sales.html
│       ├── sale_form.html
│       ├── sale_detail.html
│       ├── cash_entries.html
│       ├── transaction_form.html
│       ├── customers.html
│       ├── customer_form.html
│       ├── customer_detail.html
│       ├── jcb_records.html
│       ├── jcb_record_form.html
│       ├── alerts.html
│       └── partials/
│           ├── dashboard_content.html
│           ├── customer_payment_section.html
│           ├── sales_table.html
│           ├── transaction_table.html
│           ├── jcb_records_table.html
│           ├── alerts_content.html
│           ├── alerts_badge.html
│           └── sale_receipts_panel.html
└── assets/
    ├── dashboard.png
    ├── sales-2026-03-20-19_25_01.png
    ├── sales-new-2026-03-20-19_25_21.png
    ├── customers-2026-03-20-19_25_37.png
    ├── customers-new-2026-03-20-19_30_21.png
    ├── cash-entries-2026-03-20-19_24_38.png
    ├── cash-entries-new-2026-03-20-19_24_51.png
    ├── alerts-2026-03-20-19_30_43.png
    ├── accounts-login-2026-03-20-19_31_14.png
    └── accounts-logout-2026-03-20-19_30_58.png
```

## Data Model Overview

Key entities in the application:

- Customer: profile, credit terms, opening balance, credit balance, manual due amount
- Sale: invoice, customer link, items, total amount, payment status, due date
- Transaction: income/expense entries and linked receipts
- CustomerPayment: customer-level payment event
- PaymentAllocation: allocation records that map customer payment to invoices
- JCBRecord: machine work details, hours, rates, expense item and amount
- AlertNotification: overdue/upcoming timeline records

## URL Surface

Main routes include:

- / (Dashboard)
- /sales, /sales/new, /sales/<id>, /sales/<id>/edit
- /cash-entries, /cash-entries/new, /cash-entries/<id>/edit
- /customers, /customers/new, /customers/<id>, /customers/<id>/allocate-payment
- /jcb-records, /jcb-records/new, /jcb-records/<id>/edit
- /alerts
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

![Dashboard](assets/dashboard.png)

### Sales

![Sales List](assets/sales-2026-03-20-19_25_01.png)
![Add Sale](assets/sales-new-2026-03-20-19_25_21.png)

### Customers

![Customers List](assets/customers-2026-03-20-19_25_37.png)
![Add Customer](assets/customers-new-2026-03-20-19_30_21.png)

### Cash Entries

![Cash Entries](assets/cash-entries-2026-03-20-19_24_38.png)
![Add Cash Entry](assets/cash-entries-new-2026-03-20-19_24_51.png)

### Alerts

![Alerts](assets/alerts-2026-03-20-19_30_43.png)

### Authentication

![Login](assets/accounts-login-2026-03-20-19_31_14.png)
![Logged Out](assets/accounts-logout-2026-03-20-19_30_58.png)

## Testing and Validation

Quick sanity check:

```bash
python manage.py check
```

Run test suite:

```bash
python manage.py test
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
