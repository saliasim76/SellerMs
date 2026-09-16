"""
Employee Payroll Management Module — 3-stage workflow.

Blueprint name and the list endpoint are kept as `payroll` / `payroll_list`
so the existing sidebar link (url_for('payroll.payroll_list')) keeps working.

Stages:  Initial  ->  Ready  ->  Post
    Initial   : fully editable, rows can be added/removed
    Ready     : values read-only, no add/remove (role-gated) -- payment_status
                is the one exception, still individually editable per row
    Post      : locked entirely, including payment_status

Data sources (spec: "all field come from employee table"):
    filter (which employees qualify) -> EmployeeWorkAllocation
        (kafeel/buyer/department/location/status -- used ONLY to decide
        which employees match the selected payroll criteria; never used
        to populate any displayed/stored field)
    all displayed/stored employee info -> Employee (live), pulled at
        generation time and re-synced only via the explicit Refresh button
        (name/profession/nationality/iqama/basic_salary/allowance/
        salary_category/salary_type/day_hour/po_rate/iqama_expiry/
        status/bank_code/iban_no)
    bank_code/iban -> EmployeeBank (primary)
    buyer_name display -> BuyerMaster.buyer_name_en
"""

import io
import hashlib
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, render_template, request, jsonify, session, current_app,
    send_file,
)
from flask_login import login_required, current_user
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from sqlalchemy import func, case, literal, text

from models import (
    db, SalaryConsolidation, Employee, EmployeeWorkAllocation,
    EmployeeBank, BuyerMaster, BuyerDepartment, ActivityLog, next_payroll_id,
    LevelFive, NoActiveFinancialYearError,
)
from database.routes.employees import _emp_profession_str
from database.routes.shared import _next_grl_no, _get_auto_code, xlsx_safe

payroll_bp = Blueprint('payroll', __name__)


# ══════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════
def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def _pd(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _count_days_and_fridays(d1, d2):
    """Inclusive calendar-day count and Friday count between two dates."""
    if not d1 or not d2 or d2 < d1:
        return 0, 0
    days = (d2 - d1).days + 1
    fridays = sum(
        1 for i in range(days) if (d1 + timedelta(days=i)).weekday() == 4
    )
    return days, fridays


def _employee_bank(emp_id):
    """(bank_code, iban) from the employee's primary bank, else ('','')."""
    b = (EmployeeBank.query
         .filter_by(employee_id=emp_id)
         .order_by(EmployeeBank.is_primary.desc(), EmployeeBank.id.asc())
         .first())
    if not b:
        return '', ''
    return (b.bank_name or ''), (b.iban or '')


def _buyer_name(buyer_id):
    if not buyer_id:
        return ''
    b = BuyerMaster.query.get(buyer_id)
    return (b.buyer_name_en if b else '') or ''


def _audit(action, row_id, detail=''):
    """Best-effort audit entry. Never breaks the main transaction."""
    try:
        db.session.add(ActivityLog(
            user_id=getattr(current_user, 'id', None),
            action=action, target='payroll', target_id=row_id,
            detail=detail,
            ip_address=request.remote_addr,
        ))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
#  Calculation engine  (spec formulas)
# ══════════════════════════════════════════════════════════════════
def _recalc(row):
    """
    Recompute every derived field from the stored inputs, following the spec.

    Inputs (stored / editable): basic_salary, allowance, day_hour, days,
        fridays, holidays, absent, total_hours, extra_ot, ot_rate, bonus,
        deduction, advance, credit, paid, po_rate, po_ot_rate,
        services_charges, salary_type.
    """
    basic   = _num(row.basic_salary)
    allow   = _num(row.allowance)
    days    = _int(row.days)
    fridays = _int(row.fridays)
    holidays = _int(row.holidays)
    absent  = _int(row.absent)
    day_hour = _num(row.day_hour)                  # working hours per day
    total_hours = _num(row.total_hours)
    extra_ot = _num(row.extra_ot)
    ot_rate = _num(row.ot_rate)
    bonus   = _num(row.bonus)
    deduction = _num(row.deduction)
    advance = _num(row.advance)
    credit  = _num(row.credit)
    paid    = _num(row.paid)
    po_rate = _num(row.po_rate)
    po_ot_rate = _num(row.po_ot_rate)
    services = _num(row.services_charges)

    # Working Hour = (Days - Fridays - Holidays - Absent) x Day Hour
    working_hour = max(days - fridays - holidays - absent, 0) * day_hour
    row.working_hour = round(working_hour, 2)

    # OT Hour = Total Hours - Working Hour
    ot_hour = max(total_hours - working_hour, 0)
    row.ot_hour = round(ot_hour, 2)

    # Monthly Salary
    stype = (row.salary_type or '').strip().lower()
    if stype in ('per hour', 'perhour', 'hour', 'hourly'):
        # Per Hour: Basic Salary x Total Hour
        monthly = basic * total_hours
    else:
        # Per Month / Per Day: Daily = (Basic + Allowance)/Days; x (Days - Absent)
        daily = (basic + allow) / days if days else 0.0
        monthly = daily * (days - absent)
    row.monthly_salary = round(monthly, 2)

    # OT Amount = (OT Hour x OT Rate) + (Extra OT x OT Rate)
    ot_amount = (ot_hour * ot_rate) + (extra_ot * ot_rate)
    row.ot_amount = round(ot_amount, 2)

    # Total Salary = Monthly + OT Amount + Bonus - Deduction
    total_salary = monthly + ot_amount + bonus - deduction
    row.total_salary = round(total_salary, 2)

    # Salary Payable = Total Salary - Advance - Credit
    salary_payable = total_salary - advance - credit
    row.salary_payable = round(salary_payable, 2)

    # Balance = Salary Payable - Paid
    row.balance = round(salary_payable - paid, 2)

    # Invoice Amount = (PO Rate x Working Hour) + (PO OT Rate x OT Hour)
    #                  + ((Services Charges / Days) x (Days - Absent))
    services_per_day = (services / days) if days else 0.0
    row.invoice_amount = round(
        po_rate * working_hour + po_ot_rate * ot_hour + (services_per_day * (days - absent)), 2)


# ══════════════════════════════════════════════════════════════════
#  Duplicate-employee (30-day) evaluation
# ══════════════════════════════════════════════════════════════════
def _duplicate_map(month):
    """
    For a given month, return {employee_id: total_days} across ALL payrolls
    (spec rule 11: regardless of payroll_id). Used for row colouring.
    """
    rows = (SalaryConsolidation.query
            .filter(SalaryConsolidation.month == month)
            .all())
    totals = {}
    counts = {}
    for r in rows:
        if r.employee_id is None:
            continue
        totals[r.employee_id] = totals.get(r.employee_id, 0) + _int(r.days)
        counts[r.employee_id] = counts.get(r.employee_id, 0) + 1
    return totals, counts


def _dupe_status(emp_id, month, totals=None, counts=None):
    """Return (color, is_duplicate, total_days). color: white/yellow/red."""
    if totals is None or counts is None:
        totals, counts = _duplicate_map(month)
    total_days = totals.get(emp_id, 0)
    is_dupe = counts.get(emp_id, 0) > 1
    if not is_dupe:
        return 'white', False, total_days
    return ('red' if total_days > 30 else 'yellow'), True, total_days


def _can_override(user):
    """Payroll Manager / Administrator may save >30-day duplicates."""
    role = (getattr(user, 'role', '') or '').lower()
    return role in ('admin', 'administrator', 'payroll manager', 'payroll_manager')


def _is_superadmin(user):
    return bool(getattr(user, 'effectively_super_admin', False))


def _is_admin_or_superadmin(user):
    """Plain Admin (role == 'admin', the same check base.html/admin_required
    use elsewhere) or Super Admin -- deliberately narrower than
    _can_override above, which also lets a Payroll Manager through. Used
    only for the payroll-status permission rules in payroll_set_flow:
    Payroll Manager may move a payroll forward (Initial -> Ready -> Post)
    but not backward out of Ready, and never touch a Posted one at all."""
    return _is_superadmin(user) or bool(getattr(user, 'is_admin', None) and user.is_admin())


# ══════════════════════════════════════════════════════════════════
#  Pages
# ══════════════════════════════════════════════════════════════════
@payroll_bp.route('/')
@login_required
def payroll_list():
    """Single unified page: Stage 1 header/generate form + the payroll grid."""
    return render_template('payroll/list.html',
                           payroll_id=(request.args.get('payroll_id') or ''))


# ── Filter dropdown sources ───────────────────────────────────────
@payroll_bp.route('/filters')
@login_required
def payroll_filters():
    """Distinct kafeel / buyer / buyer_department / location / salary_category
    -- for the Stage 1 header dropdowns. Department and Location are matched
    (and populated here) from EmployeeWorkAllocation; Salary Order cascades
    separately from the selected Buyer (see /payroll/buyer/<id>/context) --
    it's a stored/display field only, never part of the duplicate-check/
    selection criteria (see payroll_generate's comment)."""
    def _distinct(col):
        vals = (db.session.query(col)
                .filter(col.isnot(None), col != '')
                .distinct().all())
        return sorted({v[0] for v in vals if v[0]})

    buyers = [{'id': b.id, 'name': b.buyer_name_en}
              for b in BuyerMaster.query
              .order_by(BuyerMaster.buyer_name_en).all()]

    salary_categories = _distinct(Employee.salary_category) or \
        ['Office Employee', 'Salary & Azad']

    return jsonify({
        # Kafeel comes from Employee (kafeel_name), not from work allocation --
        # matching is done against the employee's own record.
        'kafeel':           _distinct(Employee.kafeel_name),
        'buyer_department': _distinct(EmployeeWorkAllocation.buyer_department),
        'location':         _distinct(EmployeeWorkAllocation.location),
        'buyers':           buyers,
        'salary_category':  salary_categories,
    })


@payroll_bp.route('/buyer/<int:buyer_id>/context')
@login_required
def payroll_buyer_context(buyer_id):
    """Salary Order cascades from the selected Buyer (spec: 'salary order
    refresh from buyer'), for display/storage on the generated rows only --
    it plays no part in employee matching or the duplicate-payroll check
    (see payroll_generate's comment). Departments/locations are returned
    too but the frontend doesn't use them from here -- those two are
    independent dropdowns sourced from EmployeeWorkAllocation instead."""
    buyer = BuyerMaster.query.get_or_404(buyer_id)
    dept_rows = BuyerDepartment.query.filter_by(buyer_id=buyer_id).all()
    departments = sorted({d.department_name for d in dept_rows if d.department_name})
    locations = sorted({d.location_name for d in dept_rows if d.location_name})
    return jsonify({
        'departments': departments,
        'locations': locations,
        'salary_order': buyer.salary_order,
    })


# ── Grid data ─────────────────────────────────────────────────────
@payroll_bp.route('/data')
@login_required
def payroll_data():
    q = SalaryConsolidation.query
    pid = (request.args.get('payroll_id') or '').strip()
    month = (request.args.get('month') or '').strip()
    if pid:
        q = q.filter(SalaryConsolidation.payroll_id == pid)
    if month:
        q = q.filter(SalaryConsolidation.month == month)
    rows = q.order_by(SalaryConsolidation.payroll_id,
                      SalaryConsolidation.employee_name).all()

    # Duplicate colouring is month-scoped and global across payrolls.
    cache = {}
    out = []
    for r in rows:
        if r.month not in cache:
            cache[r.month] = _duplicate_map(r.month)
        totals, counts = cache[r.month]
        color, is_dupe, total_days = _dupe_status(
            r.employee_id, r.month, totals, counts)
        d = r.to_dict()
        posted = (r.payroll_status or 'Initial') == 'Post'
        d['row_color'] = 'gray' if posted else color
        d['is_duplicate'] = is_dupe
        d['dupe_total_days'] = total_days
        out.append(d)
    return jsonify(out)


@payroll_bp.route('/next-id')
@login_required
def payroll_next_id():
    return jsonify({'payroll_id': next_payroll_id()})


# ══════════════════════════════════════════════════════════════════
#  Payroll Summary — one row per Payroll ID, aggregated at the database
#  level (SUM/COUNT DISTINCT via GROUP BY) rather than pulling every
#  employee-level row into Python/JS. Dimension columns that can
#  legitimately vary within one batch (kafeel/buyer/department/location/
#  salary_category -- the Stage 1 generate filters are inclusive, not a
#  single fixed value) collapse to that one value only when every row in
#  the batch agrees, otherwise show "Multiple", mirroring the same
#  handling the spec calls for on Salary Category.
# ══════════════════════════════════════════════════════════════════
@payroll_bp.route('/summary')
@login_required
def payroll_summary():
    return render_template('payroll/summary.html')


@payroll_bp.route('/summary/data')
@login_required
def payroll_summary_data():
    def single_or_multi(col):
        return case(
            (func.count(func.distinct(col)) <= 1, func.max(col)),
            else_=literal(_t('Multiple', 'متعدد')),
        )

    def total(col):
        return func.coalesce(func.sum(col), 0)

    sc = SalaryConsolidation
    rows = (db.session.query(
        sc.payroll_id.label('payroll_id'),
        func.max(sc.payroll_status).label('payroll_status'),
        func.max(sc.month).label('month'),
        func.min(sc.month_from).label('month_from'),
        func.max(sc.month_to).label('month_to'),
        func.max(sc.salary_order).label('salary_order'),
        single_or_multi(sc.kafeel).label('kafeel'),
        single_or_multi(sc.buyer_name).label('buyer_name'),
        single_or_multi(sc.buyer_department).label('buyer_department'),
        single_or_multi(sc.location).label('location'),
        single_or_multi(sc.salary_category).label('salary_category'),
        func.count(func.distinct(sc.employee_id)).label('employee_count'),
        total(sc.basic_salary).label('basic_salary'),
        total(sc.allowance).label('allowance'),
        total(sc.absent).label('absent'),
        total(sc.total_hours).label('total_hours'),
        total(sc.ot_hour).label('ot_hour'),
        total(sc.ot_amount).label('ot_amount'),
        total(sc.bonus).label('bonus'),
        total(sc.deduction).label('deduction'),
        total(sc.advance).label('advance'),
        total(sc.credit).label('credit'),
        total(sc.salary_payable).label('salary_payable'),
        total(sc.paid).label('paid'),
        total(sc.services_charges).label('services_charges'),
        total(sc.invoice_amount).label('invoice_amount'),
    ).group_by(sc.payroll_id).all())

    out = []
    for r in rows:
        salary_payable = round(float(r.salary_payable or 0), 2)
        paid = round(float(r.paid or 0), 2)
        out.append({
            'payroll_id': r.payroll_id or '',
            'payroll_status': r.payroll_status or 'Initial',
            'month': r.month or '',
            'month_from': r.month_from.isoformat() if r.month_from else '',
            'month_to': r.month_to.isoformat() if r.month_to else '',
            'salary_order': r.salary_order or '',
            'kafeel': r.kafeel or '',
            'buyer_name': r.buyer_name or '',
            'buyer_department': r.buyer_department or '',
            'location': r.location or '',
            'salary_category': r.salary_category or '',
            'employee_count': int(r.employee_count or 0),
            'basic_salary': round(float(r.basic_salary or 0), 2),
            'allowance': round(float(r.allowance or 0), 2),
            'absent': int(r.absent or 0),
            'total_hours': round(float(r.total_hours or 0), 2),
            'ot_hour': round(float(r.ot_hour or 0), 2),
            'ot_amount': round(float(r.ot_amount or 0), 2),
            'bonus': round(float(r.bonus or 0), 2),
            'deduction': round(float(r.deduction or 0), 2),
            'advance': round(float(r.advance or 0), 2),
            'credit': round(float(r.credit or 0), 2),
            'salary_payable': salary_payable,
            'paid': paid,
            'balance': round(salary_payable - paid, 2),
            'services_charges': round(float(r.services_charges or 0), 2),
            'invoice_amount': round(float(r.invoice_amount or 0), 2),
        })
    return jsonify(out)


# ══════════════════════════════════════════════════════════════════
#  Stage 1 — Generation
# ══════════════════════════════════════════════════════════════════
def _employee_snapshot(e):
    """Every displayed employee field, read live from Employee. Used both
    at row-build time and by the Refresh button -- single source of truth
    so the two can never drift apart."""
    bank_code, iban = _employee_bank(e.id)
    return dict(
        employee_code=(e.employee_code or ''),
        employee_name=(e.name or ''),
        kafeel=(e.kafeel_name or ''),
        profession=_emp_profession_str(e),
        nationality=(e.nationality or ''),
        iqama=(e.iqama_number or ''),
        salary_category=(e.salary_category or ''),
        salary_type=(e.salary_type or ''),
        day_hour=_num(getattr(e, 'working_hours', 0), 8.0),
        basic_salary=_num(e.basic_salary),
        allowance=_num(e.total_allowances),
        ot_rate=_num(getattr(e, 'overtime_rate', 0)),
        iqama_expiry=e.iqama_expiry,
        status='Active' if getattr(e, 'is_active', True) else 'Inactive',
        bank_code=bank_code, iban_no=iban,
        po_rate=_num(getattr(e, 'po_rate', 0)),
        po_ot_rate=_num(getattr(e, 'po_ot_rate', 0)),
        services_charges=_num(getattr(e, 'services_charges', 0)),
    )


def _latest_joining_date(emp_id):
    """Employee's joining date, read from the SAME 'latest' EmployeeWorkAllocation
    row (highest id) that _wa_buyer_snapshot uses -- an employee with multiple
    joining/allocation records is judged on the most recent one only, never an
    older one, and never returns more than one date per employee."""
    wa = (EmployeeWorkAllocation.query
          .filter_by(employee_id=emp_id)
          .order_by(EmployeeWorkAllocation.id.desc())
          .first())
    return wa.joining_date if wa else None


def _employee_overlap_conflict(employee_id, from_date, to_date, exclude_payroll_id):
    """First existing SalaryConsolidation row for this employee, belonging to
    a DIFFERENT payroll batch, whose own individual payable period overlaps
    [from_date, to_date] -- or None if there's no conflict. This is a
    cross-payroll double-payment guard: the same employee must never be
    paid twice for the same calendar day across two separate payroll runs.

    Rows in the SAME payroll_id (exclude_payroll_id) are never compared --
    re-adding the same employee into the same batch is the existing,
    intentional 'Double' feature (see payroll_add_employee), a different
    concept from paying someone twice via two unrelated payroll batches.
    This also naturally excludes a row from conflicting with itself when
    checking an edit on that same row.

    Falls back to month_from/month_to for rows created before
    emp_from_date/emp_to_date existed."""
    rows = (SalaryConsolidation.query
            .filter(SalaryConsolidation.employee_id == employee_id)
            .filter(SalaryConsolidation.payroll_id != exclude_payroll_id)
            .all())
    for row in rows:
        r_from = row.emp_from_date or row.month_from
        r_to = row.emp_to_date or row.month_to
        if not r_from or not r_to:
            continue
        if r_from <= to_date and r_to >= from_date:
            return row
    return None


def _wa_buyer_snapshot(emp_id):
    """Buyer / Department / Location, read from the employee's LATEST
    EmployeeWorkAllocation row (not the header selection, not BuyerMaster).
    Used both at row-build time and by the Refresh button, so buyer info
    stays in sync with the employee's current work allocation even if it
    was reassigned to a different buyer/department/location after this
    payroll row was first created."""
    wa = (EmployeeWorkAllocation.query
          .filter_by(employee_id=emp_id)
          .order_by(EmployeeWorkAllocation.id.desc())
          .first())
    if not wa:
        return dict(buyer_id=None, buyer_name='', buyer_department='', location='')
    return dict(
        buyer_id=wa.buyer_id,
        buyer_name=(wa.buyer_name or ''),
        buyer_department=(wa.buyer_department or ''),
        location=(wa.location or ''),
    )


def _buyer_salary_order(buyer_id):
    """The buyer's own fixed Salary Order value, or '' if there's no buyer
    or the buyer has none set."""
    if not buyer_id:
        return ''
    buyer = BuyerMaster.query.get(buyer_id)
    return (buyer.salary_order or '') if buyer else ''


def _build_row(e, payroll_id, d1, d2, month_name, salary_category):
    """Create one SalaryConsolidation row for an employee (unsaved).
    Buyer/Department/Location come from the employee's own (latest) work
    allocation record -- not the header selection -- so they're refreshable
    per-row exactly like the Employee-sourced fields.

    Salary Order is NOT stored on EmployeeWorkAllocation at all -- it comes
    from BuyerMaster, keyed by THIS employee's own resolved buyer_id (from
    the same work-allocation snapshot below), never from the header form's
    Buyer filter directly. This matters when the header's Buyer filter is
    left blank ("Any"): a batch can then contain employees belonging to
    several different buyers, each of whom must get their OWN buyer's
    Salary Order, not all be left blank just because no single buyer was
    selected as the filter criterion.

    month_from/month_to always stay the payroll BATCH's master period (d1/d2),
    identical on every row. emp_from_date/emp_to_date are this employee's own
    payable period: emp_from_date is MAX(d1, latest joining date) so someone
    who joined mid-period is only paid from their actual joining date, not
    the whole batch period -- days/fridays (which drive every downstream
    salary/OT/invoice formula in _recalc) are counted over THIS narrower
    range, not the batch's. emp_from_date is user-editable afterwards (see
    payroll_edit); emp_to_date normally just mirrors d2."""
    latest_joining = _latest_joining_date(e.id)
    emp_from = d1
    if latest_joining and latest_joining > d1:
        emp_from = latest_joining
    emp_to = d2
    days, fridays = _count_days_and_fridays(emp_from, emp_to)

    buyer_snapshot = _wa_buyer_snapshot(e.id)
    salary_order = _buyer_salary_order(buyer_snapshot.get('buyer_id'))

    row = SalaryConsolidation(
        payroll_id=payroll_id, employ_payroll_status='Single',
        salary_order=salary_order,
        month_from=d1, month_to=d2, month=month_name,
        emp_from_date=emp_from, emp_to_date=emp_to,
        sheet_no='',
        employee_id=e.id,
        days=days, fridays=fridays, holidays=0, absent=0,
        total_hours=0, extra_ot=0,
        bonus=0, deduction=0, advance=0, credit=0, paid=0,
        payroll_status='Initial',
        **_employee_snapshot(e),
        **buyer_snapshot,
    )
    _recalc(row)
    return row


@payroll_bp.route('/check', methods=['POST'])
@login_required
def payroll_check():
    """Return an existing payroll_id if one already matches the criteria."""
    f = request.form
    d2 = _pd(f.get('month_to'))
    # Month is derived from Month To (e.g. Month To 25-Aug-2026 -> "Aug-26").
    month_name = d2.strftime('%b-%y') if d2 else ''
    buyer_id = _int(f.get('buyer_id')) or None
    buyer_department = (f.get('buyer_department') or '').strip()
    location = (f.get('location') or '').strip()
    kafeel = (f.get('kafeel') or '').strip()
    salary_category = (f.get('salary_category') or '').strip()
    salary_type = (f.get('salary_type') or '').strip()

    # Salary Order is NOT part of the payroll-identity criteria -- it's a
    # stored/display field only. Two generate attempts for the same buyer/
    # department/location/kafeel/month must be caught as duplicates even if
    # their Salary Order text happens to differ (a typo or a different
    # auto-filled default was exactly how an unwanted duplicate payroll used
    # to slip past this check).
    q = (SalaryConsolidation.query
         .filter(SalaryConsolidation.month == month_name)
         .filter(SalaryConsolidation.buyer_id == buyer_id)
         .filter(SalaryConsolidation.buyer_department == buyer_department)
         .filter(SalaryConsolidation.location == location)
         .filter(SalaryConsolidation.kafeel == kafeel))
    if salary_category:
        q = q.filter(SalaryConsolidation.salary_category == salary_category)
    if salary_type:
        q = q.filter(SalaryConsolidation.salary_type == salary_type)
    existing = q.first()
    if existing and existing.payroll_id:
        return jsonify({'exists': True, 'payroll_id': existing.payroll_id})
    return jsonify({'exists': False})


def _matching_employee_ids(kafeel, buyer_id, buyer_department, location,
                            salary_category, to_date, salary_type=''):
    """Employees who qualify for this payroll:
    - Buyer / Department / Location: matched via EmployeeWorkAllocation. When
      Kafeel and Buyer are both given but Department/Location are left blank
      ("All"), every department and location for that Kafeel+Buyer is
      included automatically -- only an *explicit* Department or Location
      choice narrows it further.
    - Kafeel: matched against the employee's own record (Employee.kafeel_name),
      not the work-allocation snapshot. Blank ("Any") matches every kafeel.
    - Salary Category: matched against the employee's own record (already
      a live Employee field).
    - Salary Type: matched against the employee's own record (Employee.
      salary_type). A closed enum -- 'month' (Per Month) or 'hour' (Per
      Hour) only; blank means no filter.
    - Status: Employee.is_active, read live from the Employee Master --
      there is no separate payroll-only status.
    - Latest Joining Date: the employee's latest EmployeeWorkAllocation
      joining_date must be on or before `to_date` (the payroll's Month To).
      An employee who joins AFTER the payroll period ends is not eligible
      for that payroll, regardless of Month From -- only Month To decides
      this (see _latest_joining_date / _build_row for how their payable
      days still start from their own joining date once included).

    NOTE: 'active' work allocation is treated as status == 'active' OR
    status is blank/NULL. Rows created before a `status` value existed (or
    via an older code path that never set it) should not be silently
    excluded just because the column happens to be empty -- only an
    *explicit* non-active status (e.g. an ended allocation) should exclude
    a row."""
    wq = EmployeeWorkAllocation.query.filter(
        db.or_(
            EmployeeWorkAllocation.status == 'active',
            EmployeeWorkAllocation.status.is_(None),
            EmployeeWorkAllocation.status == '',
        )
    )
    if buyer_id:
        wq = wq.filter(EmployeeWorkAllocation.buyer_id == buyer_id)
    if buyer_department:
        wq = wq.filter(EmployeeWorkAllocation.buyer_department == buyer_department)
    if location:
        wq = wq.filter(EmployeeWorkAllocation.location == location)

    seen = []
    seen_set = set()
    for wa in wq.all():
        if wa.employee_id in seen_set:
            continue
        seen_set.add(wa.employee_id)
        seen.append(wa.employee_id)

    out = []
    for eid in seen:
        e = Employee.query.get(eid)
        if not e or not getattr(e, 'is_active', True):
            continue
        if kafeel and (e.kafeel_name or '') != kafeel:
            continue
        if salary_category and (e.salary_category or '') != salary_category:
            continue
        if salary_type and (e.salary_type or '') != salary_type:
            continue
        latest_joining = _latest_joining_date(eid)
        if not latest_joining or latest_joining > to_date:
            continue
        out.append(e)
    return out


@payroll_bp.route('/generate', methods=['POST'])
@login_required
def payroll_generate():
    f = request.form
    d1 = _pd(f.get('month_from'))
    d2 = _pd(f.get('month_to'))
    if not d1 or not d2:
        return jsonify({'ok': False, 'error': _t('Select a valid month range.',
                                                 'اختر نطاق شهر صالح.')}), 400
    if d2 < d1:
        return jsonify({'ok': False, 'error': _t(
            'To Date cannot be earlier than From Date.',
            'لا يمكن أن يكون تاريخ النهاية قبل تاريخ البداية.')}), 400
    if (d2 - d1).days + 1 > 31:
        return jsonify({'ok': False, 'error': _t(
            'Payroll period cannot exceed 31 days. Please select a valid From Date and To Date.',
            'لا يمكن أن تتجاوز فترة كشف الرواتب 31 يومًا. الرجاء اختيار تاريخ بداية ونهاية صالحين.')}), 400

    buyer_id = _int(f.get('buyer_id')) or None
    buyer_department = (f.get('buyer_department') or '').strip()
    location = (f.get('location') or '').strip()
    kafeel = (f.get('kafeel') or '').strip()
    # Salary Order is NOT derived from this header filter's buyer_id -- when
    # Buyer is left blank ("Any"), there's no single buyer to derive it
    # from, but the batch can still contain employees from several
    # different buyers. Each row instead derives its own Salary Order from
    # its own employee's resolved buyer (see _build_row).
    salary_category = (f.get('salary_category') or '').strip()
    salary_type = (f.get('salary_type') or '').strip()
    if salary_type not in ('', 'month', 'hour'):
        return jsonify({'ok': False, 'error': _t(
            'Salary Type must be Per Month or Per Hour.',
            'يجب أن يكون نوع الراتب شهري أو بالساعة.')}), 400
    # Month is derived from Month To (e.g. Month To 25-Aug-2026 -> "Aug-26").
    month_name = d2.strftime('%b-%y')

    # A MySQL named lock, keyed by the exact same criteria as the duplicate
    # check below, closes the race that otherwise lets two near-simultaneous
    # clicks of "Generate" (a double-click, a slow response re-clicked, two
    # browser tabs) both pass the "no existing payroll" check before either
    # has committed -- each would then create its own payroll_id with the
    # identical employees for the identical period. GET_LOCK blocks (up to
    # lock_timeout seconds) rather than failing outright, so a genuinely
    # sequential second click just waits for the first to finish and then
    # correctly sees it as a duplicate. Salary Order is deliberately NOT part
    # of this key -- it must match the duplicate-check query below exactly,
    # and that query no longer treats Salary Order as payroll-identity
    # criteria (see its own comment).
    lock_key = 'payroll_gen:' + hashlib.md5('|'.join(str(v) for v in (
        month_name, buyer_id, buyer_department, location, kafeel,
        salary_category, salary_type)).encode()).hexdigest()
    lock_timeout = 10
    got_lock = db.session.execute(
        text('SELECT GET_LOCK(:k, :t)'), {'k': lock_key, 't': lock_timeout}
    ).scalar()
    if not got_lock:
        return jsonify({'ok': False, 'error': _t(
            'Another request is already generating this exact payroll. Please try again.',
            'يوجد طلب آخر قيد إنشاء نفس كشف الرواتب هذا. الرجاء المحاولة مرة أخرى.')}), 409

    try:
        # MySQL's default REPEATABLE READ fixes this request's snapshot at
        # its FIRST query (e.g. flask-login loading current_user, long
        # before GET_LOCK above) -- so without this, a request that just
        # waited for another one's lock would still read a snapshot from
        # BEFORE that other request committed, and the duplicate check right
        # below would wrongly see no existing payroll. Committing here (a
        # no-op on data, nothing has been written yet) discards that stale
        # snapshot so the very next query starts a fresh one that correctly
        # sees whatever the previous lock-holder just committed.
        db.session.commit()

        # Duplicate-payroll guard (spec Stage 1 validation). Salary Order is
        # deliberately excluded from this criteria set -- it's a stored/
        # display field on each row, not part of what makes two payrolls
        # "the same" one. Without this exclusion, two generate attempts for
        # the same buyer/department/location/kafeel/month could slip past
        # as non-duplicates just because their Salary Order text differed.
        dq = (SalaryConsolidation.query
              .filter(SalaryConsolidation.month == month_name)
              .filter(SalaryConsolidation.buyer_id == buyer_id)
              .filter(SalaryConsolidation.buyer_department == buyer_department)
              .filter(SalaryConsolidation.location == location)
              .filter(SalaryConsolidation.kafeel == kafeel))
        if salary_category:
            dq = dq.filter(SalaryConsolidation.salary_category == salary_category)
        if salary_type:
            dq = dq.filter(SalaryConsolidation.salary_type == salary_type)
        existing = dq.first()
        if existing and existing.payroll_id:
            return jsonify({
                'ok': False, 'duplicate': True,
                'payroll_id': existing.payroll_id,
                'error': _t(f'Payroll already exists. Payroll ID: {existing.payroll_id}',
                            f'كشف الرواتب موجود بالفعل. رقم الكشف: {existing.payroll_id}')
            }), 409

        employees = _matching_employee_ids(kafeel, buyer_id, buyer_department,
                                            location, salary_category, d2,
                                            salary_type)
        if not employees:
            return jsonify({'ok': False, 'error': _t(
                'No active employees match the selected criteria.',
                'لا يوجد موظفون نشطون مطابقون للمعايير.')}), 400

        payroll_id = next_payroll_id()
        created = 0
        skipped_overlap = []
        for e in employees:
            row = _build_row(e, payroll_id, d1, d2, month_name, salary_category)
            # Cross-payroll double-payment guard: skip (don't include) an
            # employee whose individual payable period overlaps a DIFFERENT
            # existing payroll's period for them -- everyone else who
            # matches the criteria still gets generated normally.
            conflict = _employee_overlap_conflict(e.id, row.emp_from_date,
                                                   row.emp_to_date, payroll_id)
            if conflict:
                skipped_overlap.append(
                    f'{e.employee_code} - {e.name} '
                    f'(overlaps payroll {conflict.payroll_id})')
                continue
            db.session.add(row)
            created += 1

        if not created:
            db.session.rollback()
            if skipped_overlap:
                return jsonify({'ok': False, 'error': _t(
                    'Every matching employee already has an overlapping payroll: ',
                    'كل الموظفين المطابقين لديهم بالفعل كشف رواتب متداخل: ')
                    + '; '.join(skipped_overlap)}), 400
            return jsonify({'ok': False, 'error': _t(
                'No active employees match the selected criteria.',
                'لا يوجد موظفون نشطون مطابقون للمعايير.')}), 400

        _audit('create', None,
               f'Generated payroll {payroll_id}: {created} employees, {month_name}'
               + (f'; skipped (date overlap): {"; ".join(skipped_overlap)}' if skipped_overlap else ''))
        try:
            db.session.commit()
            resp = {'ok': True, 'created': created, 'payroll_id': payroll_id}
            if skipped_overlap:
                resp['skipped_overlap'] = skipped_overlap
            return jsonify(resp)
        except Exception:
            db.session.rollback()
            return jsonify({'ok': False, 'error': _t('Could not generate payroll.',
                                                     'تعذّر إنشاء كشف الرواتب.')}), 500
    finally:
        db.session.execute(text('SELECT RELEASE_LOCK(:k)'), {'k': lock_key})


# ══════════════════════════════════════════════════════════════════
#  Stage 2 — Editing
# ══════════════════════════════════════════════════════════════════
_EDITABLE_NUM = ['absent', 'total_hours', 'extra_ot', 'bonus',
                 'deduction', 'advance', 'credit', 'paid', 'holidays']
_EDITABLE_INT = ('absent', 'holidays')


def _flow(row):
    return (row.payroll_status or 'Initial')


@payroll_bp.route('/<int:row_id>/json')
@login_required
def payroll_json(row_id):
    return jsonify(SalaryConsolidation.query.get_or_404(row_id).to_dict())


@payroll_bp.route('/<int:row_id>/edit', methods=['POST'])
@login_required
def payroll_edit(row_id):
    row = SalaryConsolidation.query.get_or_404(row_id)
    if _flow(row) == 'Post':
        return jsonify({'ok': False, 'error': _t(
            'Posted payroll is locked.', 'كشف الرواتب المرحّل مقفل.')}), 403
    if _flow(row) == 'Ready':
        # Ready: everything else is locked -- payment_status is the one
        # exception, still individually changeable per record (holding/
        # releasing one person's payment before the batch posts).
        f = request.form
        if 'payment_status' in f and f.get('payment_status') in ('Ready', 'Hold'):
            row.payment_status = f.get('payment_status')
        try:
            db.session.commit()
            return jsonify({'ok': True, 'row': row.to_dict()})
        except Exception:
            db.session.rollback()
            return jsonify({'ok': False, 'error': _t('Could not update.',
                                                     'تعذّر التحديث.')}), 500

    # Initial: fully editable.
    f = request.form
    if 'sheet_no' in f:
        row.sheet_no = (f.get('sheet_no') or '').strip()
    if 'status' in f and f.get('status') in ('Active', 'Inactive'):
        row.status = f.get('status')
    if 'payment_status' in f and f.get('payment_status') in ('Ready', 'Hold'):
        row.payment_status = f.get('payment_status')
    if 'emp_from_date' in f:
        new_from = _pd(f.get('emp_from_date'))
        who = f'{row.employee_code} - {row.employee_name}'
        if not new_from:
            return jsonify({'ok': False, 'error': _t(
                f'{who}: Invalid From Date.',
                f'{who}: تاريخ بداية غير صالح.')}), 400
        lo, hi = row.month_from, (row.emp_to_date or row.month_to)
        if lo and hi and (new_from < lo or new_from > hi):
            return jsonify({'ok': False, 'error': _t(
                f'{who}: Individual From Date must be between '
                f'{lo.strftime("%d-%m-%Y")} and {hi.strftime("%d-%m-%Y")}.',
                f'{who}: يجب أن يكون تاريخ البداية الفردي بين '
                f'{lo.strftime("%d-%m-%Y")} و {hi.strftime("%d-%m-%Y")}.')}), 400
        # Cross-payroll double-payment guard (see _employee_overlap_conflict)
        # -- an edit narrowing/widening this row's own period must not newly
        # overlap a DIFFERENT payroll's period for the same employee.
        conflict = _employee_overlap_conflict(row.employee_id, new_from, hi, row.payroll_id)
        if conflict:
            c_from = conflict.emp_from_date or conflict.month_from
            c_to = conflict.emp_to_date or conflict.month_to
            return jsonify({'ok': False, 'error': _t(
                f'{who}: new From Date ({new_from} to {hi}) would overlap existing '
                f'payroll {conflict.payroll_id} ({c_from} to {c_to}).',
                f'{who}: تاريخ البداية الجديد يتداخل مع كشف رواتب موجود '
                f'{conflict.payroll_id}.')}), 400
        row.emp_from_date = new_from
        row.days, row.fridays = _count_days_and_fridays(new_from, hi)
    for fld in _EDITABLE_NUM:
        if fld in f:
            val = _num(f.get(fld))
            setattr(row, fld, int(val) if fld in _EDITABLE_INT else val)
    _recalc(row)

    # Duplicate 30-day rule: block save >30 without override.
    color, is_dupe, total_days = _dupe_status(row.employee_id, row.month)
    if is_dupe and total_days > 30 and not _can_override(current_user):
        db.session.rollback()
        return jsonify({'ok': False, 'blocked': True, 'total_days': total_days,
                        'error': _t(
            f'Employee payroll exceeds 30 days ({total_days}). Override permission required.',
            f'رواتب الموظف تتجاوز 30 يومًا ({total_days}). يلزم إذن التجاوز.')}), 403

    _audit('edit', row.id, f'Edited payroll row {row.id}')
    try:
        db.session.commit()
        return jsonify({'ok': True, 'row': row.to_dict(),
                        'row_color': color, 'total_days': total_days})
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t('Could not update.',
                                                 'تعذّر التحديث.')}), 500


@payroll_bp.route('/<int:row_id>/refresh', methods=['POST'])
@login_required
def payroll_refresh_row(row_id):
    """Re-sync every auto-populate field for one row from Employee Master
    (spec: 'we have a refresh button can auto synchronize with employee
    table'). Editable Stage-2 fields (absent/total_hours/etc.) are left
    untouched; only the auto-populate fields are refreshed."""
    row = SalaryConsolidation.query.get_or_404(row_id)
    if _flow(row) != 'Initial':
        return jsonify({'ok': False, 'error': _t(
            'Only Initial payroll rows can be refreshed.',
            'يمكن تحديث صفوف الكشف في حالة "أولي" فقط.')}), 403
    e = Employee.query.get(row.employee_id)
    if not e:
        return jsonify({'ok': False, 'error': _t('Employee not found.',
                                                 'الموظف غير موجود.')}), 404
    for k, v in _employee_snapshot(e).items():
        setattr(row, k, v)
    for k, v in _wa_buyer_snapshot(e.id).items():
        setattr(row, k, v)
    row.salary_order = _buyer_salary_order(row.buyer_id)
    _recalc(row)
    _audit('refresh', row.id, f'Refreshed payroll row {row.id} from Employee Master')
    try:
        db.session.commit()
        return jsonify({'ok': True, 'row': row.to_dict()})
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t('Could not refresh.',
                                                 'تعذّر التحديث.')}), 500


@payroll_bp.route('/<payroll_id>/refresh-all', methods=['POST'])
@login_required
def payroll_refresh_all(payroll_id):
    """Refresh every Initial-stage row in a payroll batch from Employee Master."""
    rows = (SalaryConsolidation.query
            .filter_by(payroll_id=payroll_id).all())
    if not rows:
        return jsonify({'ok': False, 'error': _t('Payroll not found.',
                                                 'الكشف غير موجود.')}), 404
    refreshed = 0
    for row in rows:
        if _flow(row) != 'Initial':
            continue
        e = Employee.query.get(row.employee_id)
        if not e:
            continue
        for k, v in _employee_snapshot(e).items():
            setattr(row, k, v)
        for k, v in _wa_buyer_snapshot(e.id).items():
            setattr(row, k, v)
        row.salary_order = _buyer_salary_order(row.buyer_id)
        _recalc(row)
        refreshed += 1
    _audit('refresh_all', None, f'Refreshed {refreshed} rows in payroll {payroll_id}')
    try:
        db.session.commit()
        return jsonify({'ok': True, 'refreshed': refreshed})
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t('Could not refresh.',
                                                 'تعذّر التحديث.')}), 500


@payroll_bp.route('/<int:row_id>/delete', methods=['POST'])
@login_required
def payroll_delete(row_id):
    row = SalaryConsolidation.query.get_or_404(row_id)
    if _flow(row) != 'Initial':
        return jsonify({'ok': False, 'error': _t(
            'Only Initial payroll rows can be deleted.',
            'يمكن حذف صفوف الكشف في حالة "أولي" فقط.')}), 403
    _audit('delete', row.id, f'Deleted payroll row {row.id} ({row.employee_name})')
    try:
        db.session.delete(row)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t('Could not delete.',
                                                 'تعذّر الحذف.')}), 500


@payroll_bp.route('/<payroll_id>/delete-all', methods=['POST'])
@login_required
def payroll_delete_batch(payroll_id):
    """Delete an ENTIRE payroll batch -- every row sharing this payroll_id --
    in one action. Only while the whole batch is still Initial (every row in
    a batch always shares the same payroll_status -- see payroll_set_flow),
    the same restriction the single-row delete above already enforces. A
    Ready/Post payroll must be moved back to Initial first via 'Edit Payroll
    Status', which already reverses any GL posting (_unpost_payroll_gl) --
    so this route never needs to touch GRL/JournalEntry itself."""
    rows = SalaryConsolidation.query.filter_by(payroll_id=payroll_id).all()
    if not rows:
        return jsonify({'ok': False, 'error': _t('Payroll not found.',
                                                 'الكشف غير موجود.')}), 404
    if _flow(rows[0]) != 'Initial':
        return jsonify({'ok': False, 'error': _t(
            'Only an Initial-stage payroll can be deleted. Move it back to '
            'Initial first (Edit Payroll Status).',
            'يمكن حذف كشف الرواتب في حالة "أولي" فقط. أعد الحالة إلى "أولي" '
            'أولاً (تعديل حالة الكشف).')}), 403

    count = len(rows)
    _audit('delete_payroll', None,
           f'Deleted entire payroll {payroll_id} ({count} employees)')
    try:
        for r in rows:
            db.session.delete(r)
        db.session.commit()
        return jsonify({'ok': True, 'deleted': count})
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t('Could not delete payroll.',
                                                 'تعذّر حذف كشف الرواتب.')}), 500


# ── Add missing employee (constrained to payroll criteria) ────────
@payroll_bp.route('/<payroll_id>/available-employees')
@login_required
def payroll_available_employees(payroll_id):
    """Employees matching the payroll criteria not already in this payroll."""
    ref = (SalaryConsolidation.query
           .filter_by(payroll_id=payroll_id).first())
    if not ref:
        return jsonify([])
    already = {r.employee_id for r in SalaryConsolidation.query
               .filter_by(payroll_id=payroll_id).all()}

    employees = _matching_employee_ids(ref.kafeel, ref.buyer_id,
                                        ref.buyer_department, ref.location,
                                        ref.salary_category, ref.month_to,
                                        ref.salary_type)
    out = [{
        'employee_id': e.id, 'employee_name': e.name or '',
        'profession': _emp_profession_str(e),
        'already_in_payroll': e.id in already,
    } for e in employees]
    return jsonify(out)


@payroll_bp.route('/<payroll_id>/add-employee', methods=['POST'])
@login_required
def payroll_add_employee(payroll_id):
    ref = (SalaryConsolidation.query
           .filter_by(payroll_id=payroll_id).first())
    if not ref:
        return jsonify({'ok': False, 'error': _t('Payroll not found.',
                                                 'الكشف غير موجود.')}), 404
    if _flow(ref) != 'Initial':
        return jsonify({'ok': False, 'error': _t(
            'Employees can only be added while payroll is Initial.',
            'يمكن إضافة الموظفين في حالة "أولي" فقط.')}), 403

    emp_id = _int(request.form.get('employee_id'))
    e = Employee.query.get(emp_id)
    if not e:
        return jsonify({'ok': False, 'error': _t('Employee not found.',
                                                 'الموظف غير موجود.')}), 404

    # Confirm the employee matches the payroll criteria (filter-only check).
    matching_ids = {m.id for m in _matching_employee_ids(
        ref.kafeel, ref.buyer_id, ref.buyer_department, ref.location,
        ref.salary_category, ref.month_to, ref.salary_type)}
    if emp_id not in matching_ids:
        return jsonify({'ok': False, 'error': _t(
            'Employee does not match this payroll\'s criteria.',
            'الموظف لا يطابق معايير هذا الكشف.')}), 400

    row = _build_row(e, ref.payroll_id, ref.month_from, ref.month_to,
                     ref.month, ref.salary_category)

    # Cross-payroll double-payment guard (see _employee_overlap_conflict).
    conflict = _employee_overlap_conflict(e.id, row.emp_from_date,
                                           row.emp_to_date, ref.payroll_id)
    if conflict:
        c_from = conflict.emp_from_date or conflict.month_from
        c_to = conflict.emp_to_date or conflict.month_to
        who = f'{e.employee_code} - {e.name}'
        return jsonify({'ok': False, 'error': _t(
            f'{who}: payable period ({row.emp_from_date} to {row.emp_to_date}) '
            f'overlaps existing payroll {conflict.payroll_id} ({c_from} to {c_to}).',
            f'{who}: الفترة المستحقة تتداخل مع كشف رواتب موجود '
            f'{conflict.payroll_id}.')}), 400

    # If this employee is already present, mark both as Double.
    dupes = (SalaryConsolidation.query
             .filter_by(payroll_id=payroll_id, employee_id=emp_id).all())
    if dupes:
        row.employ_payroll_status = 'Double'
        for d in dupes:
            d.employ_payroll_status = 'Double'

    db.session.add(row)
    _audit('add_employee', None,
           f'Added employee {emp_id} to payroll {payroll_id}')
    try:
        db.session.commit()
        return jsonify({'ok': True, 'row': row.to_dict()})
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t('Could not add employee.',
                                                 'تعذّر إضافة الموظف.')}), 500


# ══════════════════════════════════════════════════════════════════
#  Stage 3 — Approval & Posting
#  GRL/Journal Entry attachment, mirroring the Purchase/Sales modules'
#  simplest 2-record GRL shape (e.g. Purchase Invoice): Debit Salary
#  Expense / Credit Salaries Payable, both the whole batch's total
#  total_salary as a single balanced lump sum (no per-employee lines).
#  A payroll batch has no single row representing "the batch" the way a
#  Purchase Invoice has one row per document, so GRL is found-or-created
#  by the string payroll_id (e.g. "PR-3") via GRL.payroll_id rather than
#  an Integer FK to one row's primary key.
# ══════════════════════════════════════════════════════════════════

def _salary_expense_account():
    """(code, name_en, name_ar, control_account) for the configured Salary
    Expense account -- the Debit side of a Payroll batch's GL entry."""
    row = _get_auto_code('employee', 'payroll_salary_expense')
    if not row or not row.levelfive_code:
        raise ValueError(_t(
            'No GL account configured for Salary Expense. Configure it via '
            'Chart of Accounts > Auto Code Selection.',
            'لم يتم تكوين حساب دفتر الأستاذ لمصروف الرواتب. قم بتكوينه عبر '
            'دليل الحسابات > اختيار الكود التلقائي.'))
    acc = LevelFive.query.filter_by(code=row.levelfive_code).first()
    ctrl = (acc.control_account or 'No') if acc else 'No'
    return row.levelfive_code, row.levelfive_drawer_en or '', row.levelfive_drawer_ar or '', ctrl


def _salary_payable_account():
    """(code, name_en, name_ar, control_account) for the configured
    Salaries Payable account -- the Credit side."""
    row = _get_auto_code('employee', 'payroll_salary_payable')
    if not row or not row.levelfive_code:
        raise ValueError(_t(
            'No GL account configured for Salaries Payable. Configure it via '
            'Chart of Accounts > Auto Code Selection.',
            'لم يتم تكوين حساب دفتر الأستاذ لرواتب مستحقة الدفع. قم بتكوينه عبر '
            'دليل الحسابات > اختيار الكود التلقائي.'))
    acc = LevelFive.query.filter_by(code=row.levelfive_code).first()
    ctrl = (acc.control_account or 'No') if acc else 'No'
    return row.levelfive_code, row.levelfive_drawer_en or '', row.levelfive_drawer_ar or '', ctrl


def _post_payroll_gl(payroll_id, rows):
    """Create/refresh the GRL + JournalEntry for one payroll batch. Raises
    ValueError / NoActiveFinancialYearError on failure -- caller rolls back."""
    from models import GRL, GRLDetail, JournalEntry, JournalEntryDetail, next_je_no
    total = round(sum(_num(r.total_salary) for r in rows), 2)
    if total <= 0:
        raise ValueError(_t('Nothing to post -- total salary is zero.',
                             'لا يوجد شيء لترحيله - إجمالي الراتب صفر.'))

    debit_code, debit_name, debit_name_ar, debit_ctrl = _salary_expense_account()
    credit_code, credit_name, credit_name_ar, credit_ctrl = _salary_payable_account()

    posting_date = date.today()
    narration = f'Payroll {payroll_id}'

    grl = GRL.query.filter_by(payroll_id=payroll_id).first()
    if not grl:
        grl = GRL(payroll_id=payroll_id)
        db.session.add(grl)
    if not grl.grl_no:
        grl.grl_no = _next_grl_no()
    grl.origion = payroll_id
    grl.posting_date = posting_date
    grl.document_date = posting_date
    grl.narration = narration
    db.session.flush()

    je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
    if not je:
        je = JournalEntry(je_no=next_je_no(), origin_type='PR', origin_id=None)
        db.session.add(je)
        db.session.flush()
        grl.journal_entry_id = je.id
    je.origion = payroll_id
    je.posting_date = posting_date
    je.document_date = posting_date
    je.narration = narration

    GRLDetail.query.filter_by(grl_id=grl.id).delete()
    JournalEntryDetail.query.filter_by(journal_entry_id=je.id).delete()
    lines = [
        {'code': debit_code, 'name': debit_name, 'name_ar': debit_name_ar, 'ctrl': debit_ctrl,
         'debit': total, 'credit': 0},
        {'code': credit_code, 'name': credit_name, 'name_ar': credit_name_ar, 'ctrl': credit_ctrl,
         'debit': 0, 'credit': total},
    ]
    for gl in lines:
        db.session.add(GRLDetail(
            grl_id=grl.id, code=gl['code'], account_name=gl['name'], account_name_ar=gl['name_ar'],
            control_account=gl['ctrl'], debit=gl['debit'], credit=gl['credit'], narration=narration))
        db.session.add(JournalEntryDetail(
            journal_entry_id=je.id, code=gl['code'], account_name=gl['name'],
            control_account=gl['ctrl'], debit=gl['debit'], credit=gl['credit'], narration=narration))


def _unpost_payroll_gl(payroll_id):
    """Delete the GRL + JournalEntry for one payroll batch -- called when a
    Posted batch is moved back to Ready/Initial via payroll_set_flow, so
    leaving Post always undoes its GL effect rather than leaving a stale
    entry behind (matches this app's post-only rule in reverse)."""
    from models import GRL, GRLDetail, JournalEntry, JournalEntryDetail
    grl = GRL.query.filter_by(payroll_id=payroll_id).first()
    if not grl:
        return
    je_id = grl.journal_entry_id
    GRLDetail.query.filter_by(grl_id=grl.id).delete()
    db.session.delete(grl)
    db.session.flush()
    if je_id:
        JournalEntryDetail.query.filter_by(journal_entry_id=je_id).delete()
        je = JournalEntry.query.get(je_id)
        if je:
            db.session.delete(je)


@payroll_bp.route('/<payroll_id>/grl')
@login_required
def payroll_grl(payroll_id):
    """The GRL + Journal Entry attached to one payroll batch, if it has
    been Posted -- backs the GRL section on the payroll page, the same
    live/posted display already used on Purchase/Sales/Cash & Bank forms."""
    from models import GRL
    grl = GRL.query.filter_by(payroll_id=payroll_id).first()
    if not grl:
        return jsonify({'ok': True, 'grl': None})
    return jsonify({'ok': True, 'grl': grl.to_dict()})


@payroll_bp.route('/<payroll_id>/grl-preview')
@login_required
def payroll_grl_preview(payroll_id):
    """Live preview of the two GRL records this payroll batch would post
    (Debit Salary Expense / Credit Salaries Payable, both the batch's
    current total_salary) -- computed on the fly from its current rows,
    nothing saved. Shown on the payroll page while the batch is still
    Initial/Ready, the same way grl_preview_from_po() previews a GRN/PINV
    before Post; once the batch is actually moved to Post via
    payroll_set_flow() -> _post_payroll_gl(), the page shows the real
    persisted GRL (via /grl) instead of this preview."""
    rows = SalaryConsolidation.query.filter_by(payroll_id=payroll_id).all()
    if not rows:
        return jsonify({'ok': True, 'lines': []})
    total = round(sum(_num(r.total_salary) for r in rows), 2)
    try:
        debit_code, debit_name, debit_name_ar, debit_ctrl = _salary_expense_account()
        credit_code, credit_name, credit_name_ar, credit_ctrl = _salary_payable_account()
    except ValueError as e:
        return jsonify({'ok': True, 'lines': [], 'error': str(e)})
    narration = f'Payroll {payroll_id}'
    lines = [
        {'code': debit_code, 'account_name': debit_name, 'account_name_ar': debit_name_ar,
         'control_account': debit_ctrl, 'reference_code': '', 'debit': total, 'credit': 0,
         'narration': narration},
        {'code': credit_code, 'account_name': credit_name, 'account_name_ar': credit_name_ar,
         'control_account': credit_ctrl, 'reference_code': '', 'debit': 0, 'credit': total,
         'narration': narration},
    ]
    today = date.today().isoformat()
    return jsonify({'ok': True, 'lines': lines, 'origion': payroll_id,
                     'posting_date': today, 'document_date': today, 'narration': narration})


@payroll_bp.route('/<payroll_id>/set-flow', methods=['POST'])
@login_required
def payroll_set_flow(payroll_id):
    """Direct set to any of the 3 fixed payroll-status values, via the
    'Edit Payroll Status' dropdown (Initial / Ready / Post).

    Permission rules:
    - Currently Post (leaving Post, in any direction): Super Admin only --
      even a plain Admin cannot touch it, since un-posting reverses real
      GL entries (_unpost_payroll_gl).
    - Ready -> Initial (a backward move): Admin or Super Admin only.
    - Everything else (Initial -> Ready, Ready -> Post): the existing,
      broader Payroll Manager / Administrator permission (_can_override).
    """
    target = (request.form.get('flow') or '').strip()
    if target not in ('Initial', 'Ready', 'Post'):
        return jsonify({'ok': False, 'error': _t('Invalid state.',
                                                 'حالة غير صالحة.')}), 400

    rows = (SalaryConsolidation.query
            .filter_by(payroll_id=payroll_id).all())
    if not rows:
        return jsonify({'ok': False, 'error': _t('Payroll not found.',
                                                 'الكشف غير موجود.')}), 404

    current = _flow(rows[0])

    if current == 'Post':
        if not _is_superadmin(current_user):
            return jsonify({'ok': False, 'error': _t(
                'This payroll is Posted. Only a Super Admin can change its status.',
                'تم ترحيل هذا الكشف. يمكن لمسؤول عام فقط تغيير حالته.')}), 403
    elif current == 'Ready' and target == 'Initial':
        if not _is_admin_or_superadmin(current_user):
            return jsonify({'ok': False, 'error': _t(
                'Only an Admin or Super Admin can move a payroll back from Ready to Initial.',
                'يمكن لمسؤول أو مسؤول عام فقط إعادة الكشف من "جاهز" إلى "أولي".')}), 403
    elif not _can_override(current_user):
        return jsonify({'ok': False, 'error': _t(
            'You are not authorized to change the payroll workflow state.',
            'ليس لديك صلاحية لتغيير حالة سير العمل.')}), 403

    try:
        if target == 'Post' and current != 'Post':
            _post_payroll_gl(payroll_id, rows)
        elif current == 'Post' and target != 'Post':
            _unpost_payroll_gl(payroll_id)
        for r in rows:
            r.payroll_status = target
        _audit(target.lower(), None,
               f'Payroll {payroll_id}: {current} -> {target}')
        db.session.commit()
        return jsonify({'ok': True, 'flow': target})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.',
            'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t('Could not update state.',
                                                 'تعذّر تحديث الحالة.')}), 500


# ══════════════════════════════════════════════════════════════════
#  Import / Export  (Excel) -- one payroll batch (?payroll_id=PR-3) or the
#  whole table (no payroll_id). Import is a bulk version of the single-row
#  Edit endpoint above: it applies exactly the same fields under exactly
#  the same stage lock rules (Post = locked, Ready = payment_status only,
#  Initial = full edit set), matched back to their row by the 'ID' column
#  the Export route writes -- never creating a new row, only updating ones
#  that already exist.
# ══════════════════════════════════════════════════════════════════
EXPORT_COLUMNS = [
    ('ID', 'id'),
    ('Payroll ID', 'payroll_id'),
    ('Payroll Status', 'payroll_status'),
    ('Employee Code', 'employee_code'),
    ('Employee Name', 'employee_name'),
    ('Profession', 'profession'),
    ('Nationality', 'nationality'),
    ('Iqama', 'iqama'),
    ('Buyer', 'buyer_name'),
    ('Department', 'buyer_department'),
    ('Location', 'location'),
    ('Kafeel', 'kafeel'),
    ('Salary Type', 'salary_type'),
    ('Days', 'days'),
    ('Fridays', 'fridays'),
    ('Holidays', 'holidays'),
    ('Absent', 'absent'),
    ('Total Hours', 'total_hours'),
    ('Extra OT', 'extra_ot'),
    ('Bonus', 'bonus'),
    ('Deduction', 'deduction'),
    ('Advance', 'advance'),
    ('Credit', 'credit'),
    ('Paid', 'paid'),
    ('Sheet No', 'sheet_no'),
    ('Employee Status', 'status'),
    ('Payment Status', 'payment_status'),
    ('Bank', 'bank_code'),
    ('IBAN', 'iban_no'),
    ('Working Hour', 'working_hour'),
    ('OT Hour', 'ot_hour'),
    ('OT Rate', 'ot_rate'),
    ('OT Amount', 'ot_amount'),
    ('Monthly Salary', 'monthly_salary'),
    ('Total Salary', 'total_salary'),
    ('Salary Payable', 'salary_payable'),
    ('Balance', 'balance'),
    ('Invoice Amount', 'invoice_amount'),
]
HEADER_TO_FIELD = {h: f for h, f in EXPORT_COLUMNS}
# Headers highlighted in the export so it's obvious which columns import
# actually reads back -- must match _apply_import_row()'s field set exactly.
EDITABLE_HEADERS = {'Holidays', 'Absent', 'Total Hours', 'Extra OT', 'Bonus',
                    'Deduction', 'Advance', 'Credit', 'Paid', 'Sheet No',
                    'Employee Status', 'Payment Status'}
IMPORT_NUM_FIELDS = ['absent', 'total_hours', 'extra_ot', 'bonus',
                     'deduction', 'advance', 'credit', 'paid', 'holidays']
IMPORT_INT_FIELDS = ('absent', 'holidays')
ALLOWED_IMPORT_EXT = {'.xlsx', '.xls'}


class RowError(Exception):
    pass


@payroll_bp.route('/export')
@login_required
def payroll_export():
    """Export one payroll batch (?payroll_id=PR-3) or, with no payroll_id,
    every payroll row in the system."""
    pid = (request.args.get('payroll_id') or '').strip()
    q = SalaryConsolidation.query
    if pid:
        q = q.filter(SalaryConsolidation.payroll_id == pid)
    rows = q.order_by(SalaryConsolidation.payroll_id,
                      SalaryConsolidation.employee_name).all()

    wb = Workbook()
    ws = wb.active
    ws.title = 'Payroll'
    hdr_fill = PatternFill('solid', fgColor='1E3A5F')
    hdr_font = Font(color='FFFFFF', bold=True, size=10)
    edit_fill = PatternFill('solid', fgColor='DBEAFE')
    edit_font = Font(color='1E3A5F', bold=True, size=10)
    for i, (h, f) in enumerate(EXPORT_COLUMNS, 1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.fill = edit_fill if h in EDITABLE_HEADERS else hdr_fill
        cell.font = edit_font if h in EDITABLE_HEADERS else hdr_font
        cell.alignment = Alignment(horizontal='center')
        ws.column_dimensions[get_column_letter(i)].width = max(12, len(h) + 3)
    ws.freeze_panes = 'A2'

    for r, row in enumerate(rows, 2):
        d = row.to_dict()
        for c, (h, f) in enumerate(EXPORT_COLUMNS, 1):
            ws.cell(row=r, column=c, value=xlsx_safe(d.get(f)))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f'payroll_{pid}.xlsx' if pid else 'payroll_all.xlsx'
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def _apply_import_row(row, vals):
    """Validate first, mutate second -- so a row rejected by the 30-day
    dupe guard never leaves a half-applied change sitting in the session
    for the batch commit at the end of payroll_import() to pick up."""
    stage = _flow(row)
    if stage == 'Post':
        raise RowError(_t('Posted payroll is locked.', 'كشف الرواتب المرحّل مقفل.'))

    payment_status = vals.get('payment_status')
    payment_status = payment_status if payment_status in ('Ready', 'Hold') else None

    if stage == 'Ready':
        if payment_status:
            row.payment_status = payment_status
        return

    # Initial: full editable set. Check the dupe guard against the row's
    # CURRENT stored `days`/month -- import never changes `days`, so this
    # check is valid whether it runs before or after applying the edits.
    color, is_dupe, total_days = _dupe_status(row.employee_id, row.month)
    if is_dupe and total_days > 30 and not _can_override(current_user):
        raise RowError(_t(
            f'Employee payroll exceeds 30 days ({total_days}). Override permission required.',
            f'رواتب الموظف تتجاوز 30 يومًا ({total_days}). يلزم إذن التجاوز.'))

    if payment_status:
        row.payment_status = payment_status
    if vals.get('sheet_no') is not None:
        row.sheet_no = str(vals['sheet_no']).strip()
    if vals.get('status') in ('Active', 'Inactive'):
        row.status = vals['status']
    for fld in IMPORT_NUM_FIELDS:
        if fld in vals and vals[fld] is not None:
            v = _num(vals[fld])
            setattr(row, fld, int(v) if fld in IMPORT_INT_FIELDS else v)
    _recalc(row)


@payroll_bp.route('/import', methods=['POST'])
@login_required
def payroll_import():
    """Bulk-apply the editable payroll fields from an uploaded Excel file
    (normally the same file payroll_export() produced, edited in Excel).
    Rows are matched by their 'ID' column -- this can never create a row,
    only update one that already exists, and each row still obeys its own
    current stage lock (Initial/Ready/Post), exactly like the single-row
    Edit endpoint."""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': _t('No file selected.', 'لم يتم اختيار ملف')}), 400
    ext = ('.' + f.filename.rsplit('.', 1)[-1].lower()) if '.' in f.filename else ''
    if ext not in ALLOWED_IMPORT_EXT:
        return jsonify({'ok': False, 'error': _t(
            'Only .xlsx / .xls files are allowed.', 'يسمح فقط بملفات .xlsx / .xls')}), 400

    try:
        wb = load_workbook(f, data_only=True, read_only=True)
    except Exception:
        return jsonify({'ok': False, 'error': _t(
            'Could not read the Excel file.', 'تعذر قراءة ملف الإكسل')}), 400

    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return jsonify({'ok': False, 'error': _t('The file is empty.', 'الملف فارغ')}), 400

    col_field = {}
    for idx, h in enumerate(header_row):
        if h is None:
            continue
        fld = HEADER_TO_FIELD.get(str(h).strip())
        if fld:
            col_field[idx] = fld
    if 'id' not in col_field.values():
        return jsonify({'ok': False, 'error': _t(
            'The file is missing the "ID" column -- export payroll data first, '
            'edit that file, then import the same file back.',
            'الملف يفتقد عمود "ID" -- قم بتصدير بيانات الرواتب أولاً، عدّلها، ثم أعد استيراد نفس الملف.')}), 400

    updated = failed = skipped_empty = 0
    failed_rows = []
    excel_row_no = 1
    for raw in rows_iter:
        excel_row_no += 1
        if raw is None or all(v is None or str(v).strip() == '' for v in raw):
            skipped_empty += 1
            continue
        vals = {fld: (raw[idx] if idx < len(raw) else None)
                for idx, fld in col_field.items()}

        try:
            row_id = int(vals.get('id'))
        except (TypeError, ValueError):
            failed += 1
            failed_rows.append({'row': excel_row_no, 'id': vals.get('id'),
                               'employee_name': vals.get('employee_name') or '',
                               'error': _t('Missing/invalid ID.', 'ID مفقود أو غير صالح.')})
            continue

        row = SalaryConsolidation.query.get(row_id)
        if not row:
            failed += 1
            failed_rows.append({'row': excel_row_no, 'id': row_id,
                               'employee_name': vals.get('employee_name') or '',
                               'error': _t('Row ID not found.', 'رقم الصف غير موجود.')})
            continue

        try:
            _apply_import_row(row, vals)
            updated += 1
        except RowError as e:
            failed += 1
            failed_rows.append({'row': excel_row_no, 'id': row_id,
                               'employee_name': row.employee_name or '',
                               'error': str(e)})

    _audit('import', None, f'Payroll import: {updated} updated, {failed} failed')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Import failed at commit stage.', 'فشل الاستيراد في مرحلة الحفظ')}), 500

    return jsonify({
        'ok': True,
        'summary': {'updated': updated, 'failed': failed, 'skipped_empty': skipped_empty},
        'failed_rows': failed_rows,
    })


# ══════════════════════════════════════════════════════════════════
#  Reports  (15 reports, spec section) -- Phase 2, left functional but
#  not the focus of this rebuild; field names updated to match the new
#  schema so nothing crashes.
# ══════════════════════════════════════════════════════════════════
REPORT_DEFS = [
    ('summary',      'Payroll Summary',            'ملخص الرواتب'),
    ('detail',       'Payroll Detail Report',      'تقرير تفصيلي'),
    ('salary_sheet', 'Salary Sheet',               'كشف الرواتب'),
    ('bank_transfer','Bank Transfer Report',       'تقرير التحويل البنكي'),
    ('overtime',     'Overtime Report',            'تقرير العمل الإضافي'),
    ('advance',      'Advance Report',             'تقرير السلف'),
    ('deduction',    'Deduction Report',           'تقرير الخصومات'),
    ('buyer_wise',   'Buyer-wise Payroll',         'الرواتب حسب المشتري'),
    ('department_wise','Department-wise Payroll',   'الرواتب حسب القسم'),
    ('location_wise','Location-wise Payroll',      'الرواتب حسب الموقع'),
    ('kafeel_wise',  'Kafeel-wise Payroll',        'الرواتب حسب الكفيل'),
    ('invoice',      'Invoice Summary',            'ملخص الفواتير'),
    ('audit',        'Payroll Audit Report',       'تقرير التدقيق'),
    ('duplicate',    'Duplicate Employee Report',  'تقرير الموظفين المكررين'),
    ('exceeding',    'Employees Exceeding 30 Days','تجاوز 30 يومًا'),
]


@payroll_bp.route('/reports')
@login_required
def payroll_reports():
    return render_template('payroll/reports.html',
                           reports=REPORT_DEFS,
                           payroll_id=(request.args.get('payroll_id') or ''))


@payroll_bp.route('/reports/<report>/data')
@login_required
def payroll_report_data(report):
    """Return {columns, rows, title} for the requested report."""
    pid = (request.args.get('payroll_id') or '').strip()
    month = (request.args.get('month') or '').strip()

    q = SalaryConsolidation.query
    if pid:
        q = q.filter(SalaryConsolidation.payroll_id == pid)
    if month:
        q = q.filter(SalaryConsolidation.month == month)
    rows = q.order_by(SalaryConsolidation.payroll_id,
                      SalaryConsolidation.employee_name).all()
    data = [r.to_dict() for r in rows]

    def money(xs, k):
        return round(sum(_num(x.get(k)) for x in xs), 2)

    title = next((en for key, en, ar in REPORT_DEFS if key == report), report)

    # ── Grouping helper for the *-wise reports ────────────────────
    def grouped(key_field, key_label):
        buckets = {}
        for x in data:
            k = x.get(key_field) or '—'
            b = buckets.setdefault(k, {'k': k, 'count': 0, 'total_salary': 0,
                                       'salary_payable': 0, 'invoice_amount': 0})
            b['count'] += 1
            b['total_salary'] += _num(x.get('total_salary'))
            b['salary_payable'] += _num(x.get('salary_payable'))
            b['invoice_amount'] += _num(x.get('invoice_amount'))
        out = [{'group': v['k'], 'employees': v['count'],
                'total_salary': round(v['total_salary'], 2),
                'salary_payable': round(v['salary_payable'], 2),
                'invoice_amount': round(v['invoice_amount'], 2)}
               for v in buckets.values()]
        cols = [(key_label, 'group'), ('Employees', 'employees'),
                ('Total Salary', 'total_salary'),
                ('Salary Payable', 'salary_payable'),
                ('Invoice', 'invoice_amount')]
        return cols, out

    if report == 'summary':
        summary = [{
            'metric': 'Employees', 'value': len(data),
        }, {'metric': 'Total Monthly Salary', 'value': money(data, 'monthly_salary')},
           {'metric': 'Total OT Amount', 'value': money(data, 'ot_amount')},
           {'metric': 'Total Salary', 'value': money(data, 'total_salary')},
           {'metric': 'Total Advance', 'value': money(data, 'advance')},
           {'metric': 'Total Deduction', 'value': money(data, 'deduction')},
           {'metric': 'Total Salary Payable', 'value': money(data, 'salary_payable')},
           {'metric': 'Total Paid', 'value': money(data, 'paid')},
           {'metric': 'Total Balance', 'value': money(data, 'balance')},
           {'metric': 'Total Invoice Amount', 'value': money(data, 'invoice_amount')}]
        cols = [('Metric', 'metric'), ('Value', 'value')]
        return jsonify({'title': title, 'columns': cols, 'rows': summary})

    if report == 'detail':
        cols = [('Payroll', 'payroll_id'), ('Employee', 'employee_name'),
                ('Profession', 'profession'), ('Iqama', 'iqama'),
                ('Type', 'salary_type'), ('Days', 'days'),
                ('Monthly', 'monthly_salary'), ('OT Amt', 'ot_amount'),
                ('Bonus', 'bonus'), ('Deduction', 'deduction'),
                ('Total', 'total_salary'), ('Advance', 'advance'),
                ('Credit', 'credit'), ('Payable', 'salary_payable'),
                ('Paid', 'paid'), ('Balance', 'balance')]
        return jsonify({'title': title, 'columns': cols, 'rows': data})

    if report == 'salary_sheet':
        cols = [('Employee', 'employee_name'), ('Iqama', 'iqama'),
                ('Bank', 'bank_code'), ('IBAN', 'iban_no'),
                ('Total Salary', 'total_salary'),
                ('Advance', 'advance'), ('Deduction', 'deduction'),
                ('Payable', 'salary_payable'), ('Paid', 'paid'),
                ('Balance', 'balance')]
        return jsonify({'title': title, 'columns': cols, 'rows': data})

    if report == 'bank_transfer':
        rows2 = [x for x in data if _num(x.get('salary_payable')) > 0]
        cols = [('Employee', 'employee_name'), ('Bank', 'bank_code'),
                ('IBAN', 'iban_no'), ('Amount', 'salary_payable')]
        return jsonify({'title': title, 'columns': cols, 'rows': rows2})

    if report == 'overtime':
        rows2 = [x for x in data if _num(x.get('ot_amount')) > 0]
        cols = [('Employee', 'employee_name'), ('OT Hrs', 'ot_hour'),
                ('Extra OT', 'extra_ot'), ('OT Rate', 'ot_rate'),
                ('OT Amount', 'ot_amount')]
        return jsonify({'title': title, 'columns': cols, 'rows': rows2})

    if report == 'advance':
        rows2 = [x for x in data if _num(x.get('advance')) > 0]
        cols = [('Employee', 'employee_name'), ('Advance', 'advance'),
                ('Total Salary', 'total_salary'), ('Payable', 'salary_payable')]
        return jsonify({'title': title, 'columns': cols, 'rows': rows2})

    if report == 'deduction':
        rows2 = [x for x in data if _num(x.get('deduction')) > 0]
        cols = [('Employee', 'employee_name'), ('Deduction', 'deduction'),
                ('Total Salary', 'total_salary'), ('Payable', 'salary_payable')]
        return jsonify({'title': title, 'columns': cols, 'rows': rows2})

    if report == 'buyer_wise':
        cols, out = grouped('buyer_name', 'Buyer')
        return jsonify({'title': title, 'columns': cols, 'rows': out})
    if report == 'department_wise':
        cols, out = grouped('buyer_department', 'Department')
        return jsonify({'title': title, 'columns': cols, 'rows': out})
    if report == 'location_wise':
        cols, out = grouped('location', 'Location')
        return jsonify({'title': title, 'columns': cols, 'rows': out})
    if report == 'kafeel_wise':
        cols, out = grouped('kafeel', 'Kafeel')
        return jsonify({'title': title, 'columns': cols, 'rows': out})

    if report == 'invoice':
        cols = [('Payroll', 'payroll_id'), ('Employee', 'employee_name'),
                ('PO Rate', 'po_rate'), ('Working Hr', 'working_hour'),
                ('PO OT Rate', 'po_ot_rate'), ('OT Hr', 'ot_hour'),
                ('Invoice Amount', 'invoice_amount')]
        return jsonify({'title': title, 'columns': cols, 'rows': data})

    if report == 'audit':
        logs = (ActivityLog.query
                .filter(ActivityLog.target == 'payroll')
                .order_by(ActivityLog.created_at.desc())
                .limit(500).all())
        rows2 = [{
            'action': l.action or '', 'detail': l.detail or '',
            'user_id': l.user_id, 'ip': l.ip_address or '',
            'when': l.created_at.strftime('%Y-%m-%d %H:%M') if l.created_at else '',
        } for l in logs]
        cols = [('Action', 'action'), ('Detail', 'detail'),
                ('User', 'user_id'), ('IP', 'ip'), ('When', 'when')]
        return jsonify({'title': title, 'columns': cols, 'rows': rows2})

    if report in ('duplicate', 'exceeding'):
        # Month-scoped duplicate totals across all payrolls.
        months = {x.get('month') for x in data if x.get('month')}
        rows2 = []
        for m in months:
            totals, counts = _duplicate_map(m)
            for eid, cnt in counts.items():
                if cnt <= 1:
                    continue
                td = totals.get(eid, 0)
                if report == 'exceeding' and td <= 30:
                    continue
                sample = next((x for x in data
                               if x.get('employee_id') == eid), {})
                rows2.append({
                    'employee_name': sample.get('employee_name', ''), 'month': m,
                    'entries': cnt, 'total_days': td,
                    'flag': 'RED >30' if td > 30 else 'YELLOW ≤30',
                })
        cols = [('Employee', 'employee_name'), ('Month', 'month'),
                ('Entries', 'entries'), ('Total Days', 'total_days'),
                ('Flag', 'flag')]
        return jsonify({'title': title, 'columns': cols, 'rows': rows2})

    return jsonify({'title': title, 'columns': [], 'rows': []})
