# Seller Master Management System (SellerMS)

A full Flask-based ERP/CRM system covering Sellers, Buyers, Vendors, Employees,
Payroll, Purchase, Sales, Chart of Accounts, Journal, Tax Codes, and Stores —
with full bilingual (English/Arabic) support.

---

## Features

- **Authentication** – Login, logout, remember-me, role-based (Admin/Staff)
- **Dashboard** – Stats cards, recent records, quick actions
- **Sellers** – Full CRUD, banks, documents, warehouses, activity log
- **Buyers / Vendors** – Master records, banks, documents, departments
- **Employees** – Master record, professions, allowances, banks, documents,
  work allocations (buyer/department/location assignment history)
- **Payroll** – Full monthly payroll generation and lifecycle (see
  [Payroll Module](#payroll-module) below)
- **Purchasing** – Requests → Quotations → Orders → Goods Receipt → Invoices
  → Returns → Debit Memos, with line items and attachments at every stage
- **Sales** – Quotations → Orders → Delivery Notes → Invoices → Returns →
  Credit Memos, with line items and attachments at every stage
- **Chart of Accounts** – 5-level hierarchical COA with seeding
- **Journal** – Journal entries and details
- **Tax Codes** – Separate purchase/sales tax codes with rates
- **Stores** – Fixed asset store and consumable store
- **Import/Export** – Generic Excel import/export for master pages
- **Activity Logs** – Audit trail per record
- **Bilingual** – Full English/Arabic with RTL Bootstrap support
- **Security** – CSRF protection, password hashing, file type validation,
  ORM-only queries (no raw SQL)

---

## Tech Stack

- **Backend**: Python 3.10+, Flask 3.x, SQLAlchemy 2.x, Flask-Migrate
- **Auth**: Flask-Login, Werkzeug password hashing, Flask-WTF CSRF
- **Frontend**: Bootstrap 5.3 (LTR & RTL), Font Awesome 6, MUI grid (Payroll),
  Vanilla JS
- **Database**: **MySQL** (via `PyMySQL`)
- **i18n**: Flask-Babel with Arabic/English support (optional — app degrades
  gracefully if not installed)

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate       # Linux/Mac
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. Configure the database

By default the app connects to a local MySQL server:

```
DB_HOST     = localhost
DB_PORT     = 3306
DB_NAME     = sellerms
DB_USER     = Ali
DB_PASSWORD = Naqvi@76
```

Override with environment variables if your MySQL instance differs:

```bash
set DB_HOST=yourhost
set DB_PORT=3306
set DB_NAME=sellerms
set DB_USER=youruser
set DB_PASSWORD=yourpassword
```

Or set `DATABASE_URL` directly to any SQLAlchemy connection string
(e.g. `mysql+pymysql://user:pass@host:3306/dbname?charset=utf8mb4`) to
override the pieces above entirely.

### 3. Run

```bash
python app.py
```

On first run the app will:
- Create all tables (`db.create_all()` + `ensure_schema()` for any columns
  added since your DB was created)
- Create default admin/staff users
- Seed the Chart of Accounts and tax codes
- Start the dev server on `http://localhost:5000`

---

## Default Login Credentials

| Role  | Username | Password   |
|-------|----------|------------|
| Admin | admin    | Admin@123  |
| Staff | staff    | Staff@123  |

> **Change these passwords immediately in production!**

---

## Project Structure

```
seller_ms/
├── app.py                  # Application factory & entry point
├── config.py                # Configuration classes (MySQL connection)
├── models.py                 # SQLAlchemy models (all tables)
├── forms.py                  # WTForms form classes
├── requirements.txt
│
├── database/routes/
│   ├── auth.py                    # Login/logout/language
│   ├── dashboard.py                # Dashboard statistics
│   ├── sellers.py                  # Seller CRUD + documents + contacts
│   ├── employees.py                # Employee master
│   ├── employee_import.py          # Employee Excel import
│   ├── work_allocations.py         # Buyer/department/location assignment
│   ├── payroll.py                  # Payroll module (see below)
│   ├── purchase.py                 # Full purchase cycle
│   ├── sales.py                    # Full sales cycle
│   ├── coa.py                      # Chart of Accounts
│   ├── journal.py                  # Journal entries
│   ├── financial.py                # Financial year setup
│   ├── purchase_tax_code.py / sales_tax_code.py
│   ├── unit_measurement.py
│   ├── vendor_doc.py / buyer_doc.py
│   ├── stores.py                   # Fixed asset / consumable stores
│   ├── io_tools.py / io_registrations.py  # Generic import/export
│   └── lookups.py
│
├── templates/               # Jinja templates (mirrors routes above)
├── static/                  # CSS/JS
└── uploads/                 # Uploaded documents (auto-created)
```

---

## Database Schema (key tables)

| Area | Tables |
|---|---|
| Auth/Audit | `users`, `activity_logs` |
| Sellers | `sellers`, `seller_banks`, `seller_documents`, `seller_warehouses` |
| Buyers/Vendors | `buyers`, `buyer_banks`, `buyer_documents`, `buyer_departments`, `vendors`, `vendor_banks`, `vendor_documents` |
| Employees | `employees`, `employee_professions`, `profession_master`, `employee_allowance_types`, `employee_allowances`, `employee_banks`, `employee_documents`, `employee_work_allocation` |
| Payroll | `salary_consolidation` |
| Purchasing | `purchase_requests`, `purchase_quotations`, `purchase_orders`, `goods_receipt_notes`, `purchase_invoices`, `goods_return_requests`, `purchase_debit_memos`, `purchase_attachments` (+ line-item tables), `purchase_tax_code` |
| Sales | `sales_quotations`, `sales_orders`, `delivery_notes`, `sales_invoices`, `sales_return_requests`, `sales_credit_memos`, `sales_attachments` (+ line-item tables), `sales_tax_code` |
| Accounting | `level_one` … `level_five` (Chart of Accounts), `journal_entries`, `journal_entry_detail`, `financial_year`, `financial_year_detail` |
| Items/Stores | `item_categories`, `item_sub_categories`, `item_master`, `tax_categories`, `fixed_asset_store`, `consumable_store`, `unit_measurement` |
| Other | `grl`, `grl_detail` |

---

## Payroll Module

The payroll module (`database/routes/payroll.py`, `templates/payroll/list.html`)
is a single unified page at `/payroll/`: a Stage‑1 header/generate form on top,
and one editable grid below with 47 fields in a fixed spec order
(`payroll_status`, `employ_payroll_status`, `buyer_department`,
`employee_name`, `day_hour`, `extra_ot`, `paid`, etc.).

### Stage 1 — Generate
- Header form: Month From/To (max 31-day range), Kafeel, Buyer, Department,
  Location, Salary Category, Salary Order (auto-filled per Buyer).
- Employee matching: `kafeel` and `salary_category` are matched against the
  **Employee** record; `buyer_id`, `buyer_department`, and `location` are
  matched against the employee's **`EmployeeWorkAllocation`** rows.
- Duplicate-payroll check: re-generating for an overlapping period prompts to
  open the existing payroll instead of creating a new one.
- All row data is populated from two snapshot functions:
  - `_employee_snapshot(employee_id)` — name, profession, nationality, iqama,
    salary_category, salary_type, day_hour, basic_salary, **allowance**,
    OT rate, iqama_expiry, status, bank_code, iban_no, po_rate — pulled
    straight from the `Employee` record.
  - `_wa_buyer_snapshot(employee_id)` — buyer_id, buyer_name,
    buyer_department, location — pulled from the employee's **latest**
    `EmployeeWorkAllocation` row (kept separate from the Employee-only
    snapshot on purpose).

### Stage 2 — Edit
- Single-click-to-edit grid: Sheet No, Absent, Total Hours, Extra OT, Bonus,
  Deduction, Advance, Credit, Paid, Holidays.
- Every dependent value (e.g. `monthly_salary`) recalculates server-side on
  each cell edit.
- **Refresh** (per-row or Refresh All): re-runs *both* snapshot functions, so
  edits already made in Stage 2 (absent, OT, bonus, etc.) are preserved, but
  Employee-master fields and Buyer/Department/Location are re-pulled to their
  current values — useful after an employee is reassigned to a new buyer, or
  their basic salary/allowance changes.
- 30-day total-days rule: rows over 30 total days are flagged
  (yellow/red) and saving is blocked without admin/payroll-manager override.
- Adding the same employee twice correctly flags both rows `Double`.

### Stage 3 — Approve / Post
- Role-gated. **Approved** locks everything except Sheet No. **Post** locks
  the row completely and turns it gray.

### Known fixes applied
- **OT Rate**: `_employee_snapshot()` originally omitted OT rate entirely,
  and `_build_row()` separately hardcoded `ot_rate=0`, silently overwriting
  any value. Fixed by reading `Employee.overtime_rate` into the snapshot's
  `ot_rate` field and removing the hardcoded `0`.
- **Buyer/Department/Location matching**: reverted to filtering by
  `EmployeeWorkAllocation` (not the Employee record) to match the intended
  design — an employee's buyer/department/location is a function of their
  *current work allocation*, not a static employee field.

### Not yet implemented
- Full arrow-key cell-to-cell navigation between non-editing cells in the
  grid (Enter/Tab already work via MUI's built-in edit-mode navigation).
- The 15 payroll reports still use the simpler `reports.html` table view —
  functional, but not yet restyled to match the main grid.

---

## Role Permissions

| Feature              | Admin | Staff |
|-----------------------|:-----:|:-----:|
| View records          | ✅    | ✅    |
| Search & filter       | ✅    | ✅    |
| Export CSV/Excel      | ✅    | ✅    |
| Add / edit (full)     | ✅    | ❌    |
| Edit contact info     | ✅    | ✅    |
| Delete                | ✅    | ❌    |
| Manage documents      | ✅    | Upload only |
| Payroll Approve/Post  | ✅ (role-gated) | ❌ |

---

## Production Deployment

Deployment target is **cPanel shared hosting via Phusion Passenger** ("Setup
Python App" in cPanel), not a self-managed VPS — there is no gunicorn/nginx
step, Passenger is its own application server and process manager.

1. **Upload the project** to the app's directory on the host (via cPanel's
   Git Version Control, File Manager upload, or FTP).
2. **cPanel → Setup Python App**: create/point the app at that directory,
   matching Python version, and set the *Application startup file* to
   `passenger_wsgi.py` and *Application Entry point* to `application` (the
   WSGI object `passenger_wsgi.py` exposes via `create_app('production')`).
3. **Environment variables**: set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
   `DB_PASSWORD`, `SECRET_KEY` either in cPanel's "Environment variables"
   section for the app, or by placing a real `.env` file (copied from
   `.env.example`) next to `passenger_wsgi.py` — `config.py` loads either
   automatically. **`SECRET_KEY` and `DB_PASSWORD` are required**; there is
   no real-credential fallback baked into the code.
4. **`DB_USER` needs `CREATE DATABASE` privilege, not just access to one
   database.** This app manages more than one MySQL database:
   - `DB_NAME` (e.g. `sellerms`) holds both the ERP schema and the SaaS
     control tables (customers/subscriptions/module catalog) — these were
     merged into one physical database, kept apart only by a separate
     Flask-SQLAlchemy bind key (`'saas'`) in `config.py`, so no second
     connection string is needed for this part.
   - Every time a Super Admin provisions a **paid SaaS customer** (SaaS
     Admin → Customers → Add), the app issues `CREATE DATABASE` on the
     same MySQL server to give that customer their own dedicated database
     (`database/tenant_provisioning.py`), reusing the same `DB_USER` /
     `DB_PASSWORD` — no separate credentials or host needed per tenant.

   A cPanel MySQL user created through cPanel's own "MySQL Databases" tool
   is commonly scoped to specific, individually pre-created databases and
   may **not** have a database-server-wide `CREATE DATABASE` grant by
   default. Confirmed in practice: cPanel also auto-prefixes every
   database *and* MySQL user with your cPanel account name (e.g. a tenant
   meant to be named `Noman` actually gets created as `proledg_Noman`,
   and a `proledg_ali` DB_USER is itself only ever granted access to
   databases matching that same `proledg_` prefix) — a raw, unprefixed
   `CREATE DATABASE` from the app fails there with "Access denied for
   user ... to database ..." (MySQL error 1044), not "unknown database".
   Set **`TENANT_DB_PREFIX`** (e.g. `proledg_`, see `.env.example`) to
   your cPanel account's own prefix so every tenant database this app
   creates is automatically named within what the host actually allows —
   otherwise provisioning fails with a permissions error at the exact
   moment a paying customer is created (trial customers are unaffected:
   they share `DB_NAME` and never trigger `CREATE DATABASE`).
5. **PDF generation requires Chromium + its system libraries.**
   `database/pdf_engine.py` renders invoices/documents to PDF via
   Playwright's headless Chromium (`playwright install chromium`), which
   needs several OS-level shared libraries (libnss3, libatk, etc.) beyond
   what `pip install` provides. Standard shared cPanel hosting has no
   root/sudo access to install those, so this can fail there even though
   `pip install -r requirements.txt` itself succeeds. Verify with your
   host that Playwright/Chromium is actually usable in your specific
   environment before going live — if not, printing/PDF export routes
   that depend on `pdf_engine.py` will error at request time even though
   the rest of the app works.
6. **Install dependencies** into the app's virtualenv (cPanel provides a
   pip command per app, e.g. `pip install -r requirements.txt`), then
   `playwright install chromium` for the PDF engine above (see its own
   system-dependency caveat).
7. **Restart the app** from cPanel once configured — `passenger_wsgi.py`
   calls `init_db()` on startup, which creates any missing tables, seeds
   the Chart of Accounts/RBAC catalog/default users, and backfills the RBAC
   `role_id` on any pre-existing user (all idempotent, safe on every
   restart).
8. **Change the default admin password immediately** (`admin` / `Admin@123`,
   auto-created only if no `admin` user exists yet) before exposing the
   site publicly -- see [Security Features](#security-features).

---

## Security Features

- ✅ CSRF tokens on all forms
- ✅ Hashed passwords (Werkzeug)
- ✅ File type & size validation for uploads (MIME-type allow-list, 64MB
  request cap)
- ✅ SQLAlchemy ORM (no raw SQL → no injection)
- ✅ Role-based access control (4-tier RBAC: Super Admin / Admin / Power
  User / User, per-module/form/action, enforced via `@permission_required`
  in `database/routes/rbac.py`)
- ✅ Login required on all protected routes

**Before going live**, change the auto-created default accounts' passwords
(`admin` / `Admin@123`, `staff` / `Staff@123`, `SuperAdmin` / the password in
`database/routes/rbac.py`'s `SUPER_ADMIN_PASSWORD`) — these exist purely so
there's a way to log in on a brand-new database, not as production
credentials.

---

## Language Switching

Click the globe icon (🌐) in the top navbar to switch between English and
Arabic. The preference is saved in the session. Arabic mode uses Bootstrap
RTL automatically.

---

## Customization

- **Colors**: Edit CSS variables in `static/css/style.css` (`:root` block)
- **Items per page**: `ITEMS_PER_PAGE` in `config.py`
- **Allowed upload MIME types**: `ALLOWED_MIMETYPES` in `config.py`
- **Max upload size**: `MAX_CONTENT_LENGTH` in `config.py`
- **Add new fields**: `models.py` → `forms.py` → the matching template
