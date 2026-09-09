"""Cash & Bank.

Outgoing Payment: a full accounting transaction (Draft -> Approved ->
Posted -> Cancelled). Pay To Type is Supplier/Buyer/Employee/GL Account;
Payment Type (Advance/Outstanding) controls whether Supplier/Buyer/Employee
settle against outstanding Purchase Invoices/Sales Invoices/Payroll Payable
rows (Detail Table 1 -- OutgoingPaymentAdjustment) or post a single lump sum
straight to that party's own Advance control account with no detail lines.
GL Account always uses a direct multi-line GL/expense payment instead
(Detail Table 2 -- OutgoingPaymentGLDetail), with VAT split onto its own
Input VAT line when applicable. GL posting (GRL + JournalEntry) only ever
happens on Post; Cancel creates a reversing GRL/JournalEntry pair rather
than deleting anything, matching this project's rule that only an explicit
Post/Cancel action may move accounting balances and that posted accounting
history is never destroyed, only reversed.

Incoming Payment is the exact AR-side mirror of Outgoing Payment above, with
every debit/credit side reversed: Receive From Type is Buyer/Supplier/
Employee/GL Account; Payment Type (Advance/Outstanding) controls whether
Buyer/Supplier/Employee settle against outstanding Sales Invoices/Purchase
Invoices/Payroll Payable rows (Detail Table 1 -- IncomingPaymentAdjustment)
or post a single lump sum straight to that party's own Advance control
account. GL Account always uses a direct multi-line GL/income receipt
instead (Detail Table 2 -- IncomingPaymentGLDetail), with VAT split onto
its own Output VAT line when applicable.
"""
from datetime import date, datetime
import json

from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required, current_user

from models import (
    db, OutgoingPayment, OutgoingPaymentAdjustment, OutgoingPaymentGLDetail, PaymentMode,
    IncomingPayment, IncomingPaymentAdjustment, IncomingPaymentGLDetail, SalesInvoice,
    PurchaseInvoice, SupplierMaster, BuyerMaster, Employee, LevelFive, LevelFour, GRL,
    Owner, SalaryConsolidation,
)
from database.routes.shared import _t, _next_grl_no, _get_auto_code, admin_required

cash_bank_bp = Blueprint('cash_bank', __name__)

# The two pre-posting Outgoing Payment states a user can freely move between
# (via the status dropdown or the dedicated Approve button) without any GL
# effect, per this app's post-only rule. Posted and Cancelled are each only
# ever reached through their own dedicated route, which is why both the
# current and the requested status in set-status are restricted to this set.
OP_PRE_POST_STATUSES = {'Draft', 'Approved'}

# Same set, for Incoming Payment -- the AR-side mirror of Outgoing Payment.
IP_PRE_POST_STATUSES = {'Draft', 'Approved'}

# Level Four code that constrains the "Pay To/Receive From = Account" and
# "GL Account" dropdowns (and, server-side, the values actually saved for
# them) for a given Mode of Payment/Receipt: Bank Transfer/Check/Credit
# Card/POS all settle through the bank account (A2-07-01 "Cash at Bank");
# Cash settles through the cash-in-hand account (A2-07-02 "Cash In hand").
# Matched by PaymentMode.name (case-insensitive); a custom-named mode not
# in this map isn't constrained at all -- the full Level Five list is
# offered/accepted.
PAYMENT_MODE_NAME_LEVEL_FOUR = {
    'bank transfer': 'A2-07-01', 'check': 'A2-07-01', 'credit card': 'A2-07-01', 'pos': 'A2-07-01',
    'cash': 'A2-07-02',
}


def _level_four_for_payment_mode(payment_mode):
    """Level Four code PAYMENT_MODE_NAME_LEVEL_FOUR maps `payment_mode`
    (a PaymentMode row, or None) to, or None when it isn't mapped."""
    if not payment_mode or not payment_mode.name:
        return None
    return PAYMENT_MODE_NAME_LEVEL_FOUR.get(payment_mode.name.strip().lower())


def _payment_account_l4(payment_method):
    """Payment Account scoping shared by both Outgoing and Incoming
    Payment: 'A2-07-02' (Cash In Hand) when the selected Payment Method's
    name is 'Cash' (case-insensitive); 'A2-07-01' (Cash at Bank) for every
    other Payment Method, known or custom -- unlike
    PAYMENT_MODE_NAME_LEVEL_FOUR above, there is no unscoped fallback
    here: it's always one heading or the other."""
    if payment_method and (payment_method.name or '').strip().lower() == 'cash':
        return 'A2-07-02'
    return 'A2-07-01'


def _pd(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _next_op_no():
    """OP-<active FY year>-<n>, matching the numbering convention of every
    other GL-posting document in this app. Falls back to the calendar year
    only so a Draft can still be created before a financial year is
    activated; Post itself hard-requires an active FY via next_je_no()."""
    from models import active_fy_year
    year = active_fy_year() or datetime.utcnow().year
    like = f'OP-{year}-%'
    max_num = 0
    for doc in OutgoingPayment.query.filter(OutgoingPayment.payment_no.like(like)).all():
        if doc.payment_no:
            try:
                num = int(doc.payment_no.rsplit('-', 1)[1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue
    n = max_num + 1
    payment_no = f'OP-{year}-{n}'
    retries = 0
    while OutgoingPayment.query.filter_by(payment_no=payment_no).first() and retries < 100:
        n += 1
        payment_no = f'OP-{year}-{n}'
        retries += 1
    return payment_no


_PAY_TO_PARTY_LOOKUP = {}   # populated lazily below, once the party models are importable


def _pay_to_party_lookup():
    if not _PAY_TO_PARTY_LOOKUP:
        _PAY_TO_PARTY_LOOKUP.update({
            'Supplier': (SupplierMaster, 'supplier_code'),
            'Buyer': (BuyerMaster, 'buyer_code'),
            'Employee': (Employee, 'employee_code'),
        })
    return _PAY_TO_PARTY_LOOKUP


def _debit_account_for(doc):
    """(code, name_en, name_ar, control_account, reference_code) for the
    Debit side of an Outgoing Payment's GL entry -- Supplier/Buyer/Employee
    only (GL Account posts each Detail Table 2 line's own code directly, see
    outgoing_payment_post()).

    Mirrors the per-party control-account override already used by Purchase
    Invoice/Sales Invoice: when the specific selected party has their own
    control account configured (via that party's own Chart of Account
    picker), the debit posts to THAT party's account instead of the shared
    Auto Code Selection account, with reference_code set to the party's own
    code (e.g. a supplier's "Ven-2026-1") for per-party traceability. Falls
    back to the shared Auto Code Selection account configured for that Pay
    To type + Payment Type pair when the party has no control account of
    their own -- Outstanding settlement and Advance are deliberately
    separate accounts (e.g. "Supplier Payable" vs. "Advance to Supplier"),
    since they represent different balances even for the same supplier."""
    model_lookup = _pay_to_party_lookup()
    model, code_attr = model_lookup[doc.pay_to_type]
    party = None
    if doc.pay_to_id:
        try:
            party = model.query.get(int(doc.pay_to_id))
        except (TypeError, ValueError):
            party = None
    if party and getattr(party, 'levelfive_code', None):
        party_account = LevelFive.query.filter_by(code=party.levelfive_code).first()
        if party_account and party_account.control_account == 'Yes':
            return (party_account.code, party_account.drawers or '', party_account.drawers_ar or '',
                    party_account.control_account, getattr(party, code_attr, '') or '')

    form_code = {
        ('Supplier', 'Outstanding'): 'outgoing_payment_supplier',
        ('Supplier', 'Advance'): 'outgoing_payment_supplier_advance',
        ('Buyer', 'Outstanding'): 'outgoing_payment_buyer',
        ('Buyer', 'Advance'): 'outgoing_payment_buyer_advance',
        ('Employee', 'Outstanding'): 'outgoing_payment_employee',
        ('Employee', 'Advance'): 'outgoing_payment_employee_advance',
    }[(doc.pay_to_type, doc.payment_type)]
    row = _get_auto_code('cash_bank', form_code)
    if not row or not row.levelfive_code:
        raise ValueError(_t(
            f'No GL account configured for "{form_code}". Configure it via '
            'Chart of Accounts > Auto Code Selection.',
            f'لم يتم تكوين حساب دفتر الأستاذ لـ "{form_code}". قم بتكوينه عبر '
            'دليل الحسابات > اختيار الكود التلقائي.'))
    acc = LevelFive.query.filter_by(code=row.levelfive_code).first()
    ctrl = (acc.control_account or 'No') if acc else 'No'
    return row.levelfive_code, row.levelfive_drawer_en or '', row.levelfive_drawer_ar or '', ctrl, ''


def _credit_account_for(payment_account_id):
    """(code, name_en, name_ar, control_account) for the GL account this
    payment actually settles through -- the user-picked Level Five account
    (payment_account_id, from the form's own "Payment Account" dropdown).
    Required in every case."""
    if not payment_account_id:
        raise ValueError(_t('Select the Payment Account.', 'اختر حساب الدفع.'))
    acc = LevelFive.query.filter_by(code=payment_account_id).first()
    if not acc:
        raise ValueError(_t(
            'The selected Payment Account was not found.',
            'حساب الدفع المحدد غير موجود.'))
    return acc.code, acc.drawers or '', acc.drawers_ar or '', acc.control_account or 'No'


def _input_vat_account():
    """(code, name_en, name_ar, control_account) for the configured Input
    VAT account -- the extra Debit line Detail Table 2 (GL Account) posts
    when any of its lines carries a VAT amount."""
    row = _get_auto_code('cash_bank', 'outgoing_payment_input_vat')
    if not row or not row.levelfive_code:
        raise ValueError(_t(
            'No GL account configured for Input VAT. Configure it via '
            'Chart of Accounts > Auto Code Selection.',
            'لم يتم تكوين حساب دفتر الأستاذ لضريبة القيمة المضافة المدخلات. قم بتكوينه عبر '
            'دليل الحسابات > اختيار الكود التلقائي.'))
    acc = LevelFive.query.filter_by(code=row.levelfive_code).first()
    ctrl = (acc.control_account or 'No') if acc else 'No'
    return row.levelfive_code, row.levelfive_drawer_en or '', row.levelfive_drawer_ar or '', ctrl


def _outstanding_source(document_type, document_id, lock=False):
    """(source_row, outstanding_amount, document_no, document_date, due_date)
    for one outstanding Purchase Invoice / Sales Invoice / Payroll Payable
    row -- the three document types Detail Table 1 can settle. Returns
    (None, 0.0, '', None, None) when the type/id doesn't resolve to a real
    row. Pass lock=True (only from within an active Post/Cancel transaction)
    to SELECT ... FOR UPDATE the row, so two concurrent Outgoing Payments
    can never both settle the same remaining balance."""
    if document_type == 'Purchase Invoice':
        q = PurchaseInvoice.query.filter_by(purchase_invoice_id=document_id)
        inv = q.with_for_update().first() if lock else q.first()
        if not inv:
            return None, 0.0, '', None, None
        outstanding = float(inv.total_incl_vat or 0) - float(inv.paid_amount or 0)
        return inv, outstanding, inv.doc_no or '', inv.document_date, None
    if document_type == 'Sales Invoice':
        q = SalesInvoice.query.filter_by(sales_invoice_id=document_id)
        inv = q.with_for_update().first() if lock else q.first()
        if not inv:
            return None, 0.0, '', None, None
        outstanding = float(inv.total_incl_vat or 0) - float(inv.paid_amount or 0)
        return inv, outstanding, inv.doc_no or '', inv.document_date, None
    if document_type == 'Payroll Payable':
        q = SalaryConsolidation.query.filter_by(id=document_id)
        row = q.with_for_update().first() if lock else q.first()
        if not row:
            return None, 0.0, '', None, None
        outstanding = float(row.salary_payable or 0) - float(row.paid or 0)
        return row, outstanding, row.payroll_id or '', row.month_to, None
    return None, 0.0, '', None, None


@cash_bank_bp.route('/cash-bank/outgoing-payments/grl-preview')
@login_required
def outgoing_payment_grl_preview():
    """Live, read-only preview of the GRL/JournalEntry lines Post would
    create -- same lookups as outgoing_payment_post(), against a throwaway
    stand-in object so no OutgoingPayment needs to exist yet. GL Account
    lines are passed as gl_lines_json (a JSON array of {code, amount, vat})
    since Detail Table 2 can carry several independent lines at once."""
    from types import SimpleNamespace
    pay_to_type = (request.args.get('pay_to_type') or '').strip()
    pay_to_id = (request.args.get('pay_to_id') or '').strip()
    payment_type = (request.args.get('payment_type') or '').strip()
    payment_account_id = (request.args.get('payment_account_id') or '').strip()

    if pay_to_type == 'GL Account':
        try:
            gl_lines_raw = json.loads(request.args.get('gl_lines_json') or '[]')
        except (TypeError, ValueError):
            gl_lines_raw = []
        lines = []
        total_amount = total_vat = 0.0
        for gl in gl_lines_raw:
            code = (gl.get('code') or '').strip()
            amount = _num(gl.get('amount'))
            vat = _num(gl.get('vat'))
            if not code or amount <= 0:
                continue
            acc = LevelFive.query.filter_by(code=code).first()
            if not acc:
                continue
            lines.append({'code': acc.code, 'account_name': acc.drawers or '',
                          'control_account': acc.control_account or 'No', 'reference_code': '',
                          'debit': round(amount, 2), 'credit': 0})
            total_amount += amount
            total_vat += vat
        if not lines:
            return jsonify({'ok': True, 'lines': []})
        try:
            if total_vat > 0.005:
                vat_code, vat_name, _va, vat_ctrl = _input_vat_account()
                lines.append({'code': vat_code, 'account_name': vat_name, 'control_account': vat_ctrl,
                              'reference_code': '', 'debit': round(total_vat, 2), 'credit': 0})
            credit_code, credit_name, _cn_ar, credit_ctrl = _credit_account_for(payment_account_id)
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        lines.append({'code': credit_code, 'account_name': credit_name, 'control_account': credit_ctrl,
                      'reference_code': '', 'debit': 0, 'credit': round(total_amount + total_vat, 2)})
        return jsonify({'ok': True, 'lines': lines})

    if not pay_to_type or not pay_to_id or payment_type not in ('Advance', 'Outstanding'):
        return jsonify({'ok': True, 'lines': []})
    total_amount_including_vat = _num(request.args.get('total_amount_including_vat'))
    if total_amount_including_vat <= 0:
        return jsonify({'ok': True, 'lines': []})

    try:
        fake_doc = SimpleNamespace(pay_to_type=pay_to_type, pay_to_id=pay_to_id, payment_type=payment_type)
        debit_code, debit_name, _dn_ar, debit_ctrl, debit_ref = _debit_account_for(fake_doc)
        credit_code, credit_name, _cn_ar, credit_ctrl = _credit_account_for(payment_account_id)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    lines = [
        {'code': debit_code, 'account_name': debit_name, 'control_account': debit_ctrl,
         'reference_code': debit_ref, 'debit': round(total_amount_including_vat, 2), 'credit': 0},
        {'code': credit_code, 'account_name': credit_name, 'control_account': credit_ctrl,
         'reference_code': '', 'debit': 0, 'credit': round(total_amount_including_vat, 2)},
    ]
    return jsonify({'ok': True, 'lines': lines})


@cash_bank_bp.route('/cash-bank/outgoing-payments/next-doc-numbers')
@login_required
def outgoing_payment_next_doc_numbers():
    """Live preview of the payment/GRL/JE numbers a new Draft (or a Draft/
    Approved payment not yet Posted) would be assigned -- pure reads, no
    row created, so this is safe to call before anything is saved. Like
    every other "next number" preview in this app, it can go stale under
    concurrent creation; the real number is only actually assigned at
    Save (payment_no) / Post (grl_no, je_no)."""
    from models import next_je_no, NoActiveFinancialYearError
    je_no = ''
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'payment_no': _next_op_no(), 'grl_no': _next_grl_no(), 'je_no': je_no})


# ══════════════════════════════════════════════════════════════════
# OUTGOING PAYMENT -- Pages
# ══════════════════════════════════════════════════════════════════

@cash_bank_bp.route('/cash-bank/outgoing-payments')
@login_required
def outgoing_payment_list():
    return render_template('cash_bank/outgoing_payment.html')


@cash_bank_bp.route('/cash-bank/outgoing-payments/<int:id>/print')
@login_required
def outgoing_payment_print(id):
    doc = OutgoingPayment.query.get_or_404(id)
    owner = Owner.query.first()
    return render_template('cash_bank/outgoing_payment_print.html', doc=doc, owner=owner)


# ══════════════════════════════════════════════════════════════════
# OUTGOING PAYMENT -- Data / lookups
# ══════════════════════════════════════════════════════════════════

@cash_bank_bp.route('/cash-bank/outgoing-payments/data')
@login_required
def outgoing_payment_data():
    q = OutgoingPayment.query
    payment_no = (request.args.get('payment_no') or '').strip()
    pay_to_type = (request.args.get('pay_to_type') or '').strip()
    pay_to_id = (request.args.get('pay_to_id') or '').strip()
    date_from = _pd(request.args.get('date_from'))
    date_to = _pd(request.args.get('date_to'))
    status = (request.args.get('status') or '').strip()
    if payment_no:
        q = q.filter(OutgoingPayment.payment_no.ilike(f'%{payment_no}%'))
    if pay_to_type:
        q = q.filter(OutgoingPayment.pay_to_type == pay_to_type)
    if pay_to_id:
        q = q.filter(OutgoingPayment.pay_to_id == pay_to_id)
    if date_from:
        q = q.filter(OutgoingPayment.payment_date >= date_from)
    if date_to:
        q = q.filter(OutgoingPayment.payment_date <= date_to)
    if status:
        q = q.filter(OutgoingPayment.status == status)
    rows = q.order_by(OutgoingPayment.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@cash_bank_bp.route('/cash-bank/outgoing-payments/<int:id>/json')
@login_required
def outgoing_payment_json(id):
    op = OutgoingPayment.query.get_or_404(id)
    d = op.to_dict()
    # Same read-only item breakdown the invoice picker attaches when a line
    # is first added -- re-attached here too so it's still there on reload
    # (e.g. reopening a Draft for editing), not just right after picking.
    # No such breakdown exists for Payroll Payable (no line-item concept).
    for line, ln_dict in zip(op.adjustment_lines, d['adjustment_lines']):
        items = []
        if line.document_type == 'Purchase Invoice':
            inv = PurchaseInvoice.query.get(line.document_id)
            if inv:
                items = [li.to_dict() for li in sorted(inv.line_items, key=lambda li: li.line_number)]
        elif line.document_type == 'Sales Invoice':
            inv = SalesInvoice.query.get(line.document_id)
            if inv:
                items = [li.to_dict() for li in sorted(inv.line_items, key=lambda li: li.line_number)]
        ln_dict['items'] = items
    return jsonify(d)


@cash_bank_bp.route('/cash-bank/outgoing-payments/next-doc-no')
@login_required
def outgoing_payment_next_doc_no():
    return jsonify({'payment_no': _next_op_no()})


@cash_bank_bp.route('/cash-bank/outgoing-payments/suppliers')
@login_required
def outgoing_payment_suppliers():
    rows = SupplierMaster.query.order_by(SupplierMaster.supplier_name_en).all()
    return jsonify([{'id': s.id, 'name': s.supplier_name_en or ''} for s in rows])


@cash_bank_bp.route('/cash-bank/outgoing-payments/buyers')
@login_required
def outgoing_payment_buyers():
    rows = BuyerMaster.query.order_by(BuyerMaster.buyer_name_en).all()
    return jsonify([{'id': b.id, 'name': b.buyer_name_en or ''} for b in rows])


@cash_bank_bp.route('/cash-bank/outgoing-payments/employees')
@login_required
def outgoing_payment_employees():
    rows = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()
    return jsonify([{'id': e.id, 'name': e.name or ''} for e in rows])


@cash_bank_bp.route('/cash-bank/outgoing-payments/payment-modes')
@login_required
def outgoing_payment_modes_lookup():
    """Active Payment Methods for the "Payment Method" dropdown -- mirrors
    incoming_payment_modes_lookup()."""
    return jsonify([p.to_dict() for p in PaymentMode.query.filter_by(active=True).order_by(PaymentMode.name).all()])


@cash_bank_bp.route('/cash-bank/outgoing-payments/accounts')
@login_required
def outgoing_payment_accounts():
    """Chart-of-Accounts options for the "Payment Account" dropdown, all
    under Level Three A2-07 (Cash and cash equivalents): Payment Method =
    Cash offers only its Cash In Hand accounts (A2-07-02); every other
    Payment Method offers only its Cash at Bank accounts (A2-07-01) -- see
    _payment_account_l4(). With no Payment Method selected yet, every
    A2-07 account is offered (both headings)."""
    from models import LevelFour
    q = (LevelFive.query
         .join(LevelFour, LevelFive.level_four_code == LevelFour.code)
         .filter(LevelFour.level_three_code == 'A2-07'))
    payment_method_id = request.args.get('payment_method_id', type=int)
    if payment_method_id:
        l4_code = _payment_account_l4(PaymentMode.query.get(payment_method_id))
        q = q.filter(LevelFour.code == l4_code)
    rows = q.order_by(LevelFive.code).all()
    return jsonify([{'code': a.code, 'name': a.drawers or ''} for a in rows])


def _outstanding_docs_response(rows, doc_no_attr, doc_date_attr, ref_attr, id_attr):
    out = []
    for r in rows:
        balance = float(r.total_incl_vat or 0) - float(r.paid_amount or 0)
        if balance <= 0.005:
            continue   # fully paid -- never offered
        out.append({
            'document_id': getattr(r, id_attr),
            'document_no': getattr(r, doc_no_attr) or '',
            'document_date': str(getattr(r, doc_date_attr)) if getattr(r, doc_date_attr) else '',
            'due_date': '',
            'reference_no': getattr(r, ref_attr) or '',
            'outstanding_amount': balance,
        })
    return out


@cash_bank_bp.route('/cash-bank/outgoing-payments/supplier-invoices')
@login_required
def outgoing_payment_supplier_invoices():
    """Outstanding (Posted, balance > 0) Purchase Invoices for a supplier --
    the picker grid behind "Add New Record" when Pay To = Supplier."""
    supplier_id = request.args.get('supplier_id', type=int)
    if not supplier_id:
        return jsonify([])
    invs = (PurchaseInvoice.query
            .filter(PurchaseInvoice.supplier_id == supplier_id)
            .filter(PurchaseInvoice.posting_status == 'Posted')
            .order_by(PurchaseInvoice.document_date).all())
    return jsonify(_outstanding_docs_response(
        invs, 'doc_no', 'document_date', 'supplier_ref_no', 'purchase_invoice_id'))


@cash_bank_bp.route('/cash-bank/outgoing-payments/buyer-invoices')
@login_required
def outgoing_payment_buyer_invoices():
    """Outstanding (Posted, balance > 0) Sales Invoices for a buyer -- the
    picker grid behind "Add New Record" when Pay To = Buyer."""
    buyer_id = request.args.get('buyer_id', type=int)
    if not buyer_id:
        return jsonify([])
    invs = (SalesInvoice.query
            .filter(SalesInvoice.buyer_id == buyer_id)
            .filter(SalesInvoice.posting_status == 'Posted')
            .order_by(SalesInvoice.document_date).all())
    return jsonify(_outstanding_docs_response(
        invs, 'doc_no', 'document_date', 'buyer_ref_no', 'sales_invoice_id'))


@cash_bank_bp.route('/cash-bank/outgoing-payments/employee-payables')
@login_required
def outgoing_payment_employee_payables():
    """Outstanding (Posted, balance > 0) Payroll Payable rows for an
    employee -- the picker grid behind "Add New Record" when Pay To =
    Employee. Each SalaryConsolidation row is one payroll run's payable
    for that employee; salary_payable - paid is its outstanding amount."""
    employee_id = request.args.get('employee_id', type=int)
    if not employee_id:
        return jsonify([])
    rows = (SalaryConsolidation.query
            .filter(SalaryConsolidation.employee_id == employee_id)
            .filter(SalaryConsolidation.payroll_status == 'Post')
            .order_by(SalaryConsolidation.month_to).all())
    out = []
    for row in rows:
        balance = float(row.salary_payable or 0) - float(row.paid or 0)
        if balance <= 0.005:
            continue
        out.append({
            'document_id': row.id,
            'document_no': row.payroll_id or '',
            'document_date': str(row.month_to) if row.month_to else '',
            'due_date': '',
            'reference_no': '',
            'outstanding_amount': balance,
        })
    return jsonify(out)


# ══════════════════════════════════════════════════════════════════
# OUTGOING PAYMENT -- Draft save (no GL, no balance mutation)
# ══════════════════════════════════════════════════════════════════

def _apply_op_master_fields(doc, f):
    """Set every master (header) field. No DB dependency on doc.id, so this
    always runs BEFORE the first flush -- pay_to_type etc. are NOT NULL, and
    flushing an OutgoingPayment before they're set would fail immediately.
    Returns (pay_to_type, payment_type)."""
    pay_to_type = (f.get('pay_to_type') or '').strip()
    if pay_to_type not in ('Supplier', 'Buyer', 'Employee', 'GL Account'):
        raise ValueError(_t('Select a valid Pay To type.', 'اختر نوع مستلم صالح.'))
    pay_to_id = (f.get('pay_to_id') or '').strip()
    # Pay To = GL Account has no single party of its own -- Detail Table 2
    # names each line's own account directly (see _apply_op_gl_lines()).
    if pay_to_type != 'GL Account' and not pay_to_id:
        raise ValueError(_t('Select the Supplier/Buyer/Employee.',
                             'اختر المورد/المشتري/الموظف.'))
    doc.pay_to_type = pay_to_type
    doc.pay_to_id = pay_to_id or None

    payment_type = (f.get('payment_type') or '').strip()
    if pay_to_type == 'GL Account':
        payment_type = None   # not applicable -- Detail Table 2 always drives this payment
    elif payment_type not in ('Advance', 'Outstanding'):
        raise ValueError(_t('Select a valid Payment Type.', 'اختر نوع دفعة صالح.'))
    doc.payment_type = payment_type

    doc.payment_date = _pd(f.get('payment_date'))
    # Posting Date is intentionally blank while Draft (mirrors the Purchase
    # module's own "empty + disabled until Approved" convention) and
    # outgoing_payment_post() always posts as of today() regardless of
    # whatever is stored here, so this is never actually required for Save
    # or Post to succeed.
    doc.posting_date = _pd(f.get('posting_date'))
    doc.reference_no = (f.get('reference_no') or '').strip()
    doc.narration = (f.get('narration') or '').strip()
    if not doc.payment_date:
        raise ValueError(_t('Payment Date is required.', 'تاريخ الدفع مطلوب.'))

    payment_method_id = f.get('payment_method_id', type=int)
    if not payment_method_id:
        raise ValueError(_t('Payment Method is required.', 'طريقة الدفع مطلوبة.'))
    payment_method = PaymentMode.query.get(payment_method_id)
    if not payment_method:
        raise ValueError(_t('Invalid Payment Method.', 'طريقة دفع غير صالحة.'))
    doc.payment_method_id = payment_method_id

    # Payment Account -- the Cash/Bank account this payment is actually paid
    # FROM (see _credit_account_for()), scoped exactly like
    # outgoing_payment_accounts()'s own dropdown: Cash In Hand (A2-07-02)
    # when Payment Method is Cash, Cash at Bank (A2-07-01) for every other
    # Payment Method (see _payment_account_l4()).
    payment_account_id = (f.get('payment_account_id') or '').strip()
    if not payment_account_id:
        raise ValueError(_t('Select the Payment Account.', 'اختر حساب الدفع.'))
    l4_code = _payment_account_l4(payment_method)
    acc = LevelFive.query.filter_by(code=payment_account_id).first()
    if not acc or acc.level_four_code != l4_code:
        raise ValueError(_t(
            f'This Payment Method only accepts an account under {l4_code}.',
            f'تقبل طريقة الدفع هذه فقط حسابًا ضمن {l4_code}.'))
    doc.payment_account_id = payment_account_id

    return pay_to_type, payment_type


def _apply_op_adjustment_lines(doc, f):
    """Detail Table 1 (Outstanding payments only). Replaces every
    adjustment line, re-deriving each one's outstanding_amount live from
    its source document (never trusting whatever the client last saw),
    and rejecting overpayment or the same document twice in one payment.
    Requires doc.id (the caller must flush the master row first)."""
    dtypes   = request.form.getlist('adj_document_type[]')
    dids     = request.form.getlist('adj_document_id[]')
    docnos   = request.form.getlist('adj_document_no[]')
    duedates = request.form.getlist('adj_due_date[]')
    pays     = request.form.getlist('adj_payment_amount[]')
    refnos   = request.form.getlist('adj_reference_no[]')
    narrs    = request.form.getlist('adj_narration[]')

    n = len(pays)
    if n == 0:
        raise ValueError(_t('At least one outstanding document is required.',
                             'مطلوب مستند مستحق واحد على الأقل.'))

    OutgoingPaymentAdjustment.query.filter_by(outgoing_payment_id=doc.id).delete()

    seen = set()
    total_payment = 0.0
    added = 0
    for i in range(n):
        pay_amt = _num(pays[i] if i < len(pays) else 0)
        if pay_amt <= 0:
            continue
        document_type = (dtypes[i] if i < len(dtypes) else '').strip()
        if document_type not in ('Purchase Invoice', 'Sales Invoice', 'Payroll Payable'):
            raise ValueError(_t('Invalid outstanding document type.', 'نوع مستند مستحق غير صالح.'))
        try:
            document_id = int(dids[i]) if i < len(dids) and dids[i] else None
        except ValueError:
            document_id = None
        if not document_id:
            raise ValueError(_t('Invalid outstanding document.', 'مستند مستحق غير صالح.'))
        key = (document_type, document_id)
        if key in seen:
            raise ValueError(_t('The same document cannot be added twice to one payment.',
                                 'لا يمكن إضافة نفس المستند مرتين في نفس الدفعة.'))
        seen.add(key)

        source, outstanding, doc_no_live, doc_date_live, _due = _outstanding_source(document_type, document_id)
        if not source:
            raise ValueError(_t('A referenced outstanding document no longer exists.',
                                 'أحد المستندات المستحقة المرجعية لم يعد موجوداً.'))
        if pay_amt > outstanding + 0.01:
            raise ValueError(_t(
                f'Payment for {doc_no_live} ({pay_amt:.2f}) cannot exceed its outstanding amount ({outstanding:.2f}).',
                f'الدفعة لـ {doc_no_live} ({pay_amt:.2f}) لا يمكن أن تتجاوز المبلغ المستحق ({outstanding:.2f}).'))

        db.session.add(OutgoingPaymentAdjustment(
            outgoing_payment_id=doc.id, detail_id=i + 1,
            document_type=document_type, document_id=document_id,
            document_no=doc_no_live, document_date=doc_date_live,
            due_date=_pd(duedates[i]) if i < len(duedates) else None,
            outstanding_amount=round(outstanding, 2),
            payment_amount=round(pay_amt, 2),
            remaining_amount=round(outstanding - pay_amt, 2),
            reference_no=(refnos[i] if i < len(refnos) else '').strip(),
            narration=(narrs[i] if i < len(narrs) else '').strip(),
        ))
        added += 1
        total_payment += pay_amt

    if not added:
        raise ValueError(_t('Payment Amount must be greater than zero.',
                             'يجب أن يكون مبلغ الدفع أكبر من صفر.'))

    doc.total_amount = round(total_payment, 2)
    doc.total_vat = 0
    doc.total_amount_including_vat = round(total_payment, 2)


def _apply_op_advance(doc, f):
    """Advance payment_type: no detail lines at all -- a single lump sum
    posted directly to the party's own Advance control account (see
    _debit_account_for())."""
    amount = _num(f.get('advance_amount'))
    if amount <= 0:
        raise ValueError(_t('Advance amount must be greater than zero.',
                             'يجب أن يكون مبلغ السلفة أكبر من صفر.'))
    OutgoingPaymentAdjustment.query.filter_by(outgoing_payment_id=doc.id).delete()
    doc.total_amount = round(amount, 2)
    doc.total_vat = 0
    doc.total_amount_including_vat = round(amount, 2)


def _apply_op_gl_lines(doc, f):
    """Detail Table 2 (Pay To = GL Account only). Each line names its own
    Level Five code directly; VAT is optional per line and validated as
    amount + vat == total."""
    codes  = request.form.getlist('gl_code[]')
    docnos = request.form.getlist('gl_document_no[]')
    purps  = request.form.getlist('gl_purpose[]')
    amts   = request.form.getlist('gl_amount[]')
    vats   = request.form.getlist('gl_vat[]')
    narrs  = request.form.getlist('gl_narration[]')

    n = len(amts)
    if n == 0:
        raise ValueError(_t('At least one GL detail line is required.',
                             'مطلوب سطر تفصيل واحد على الأقل.'))

    OutgoingPaymentGLDetail.query.filter_by(outgoing_payment_id=doc.id).delete()

    total_amount = total_vat = 0.0
    added = 0
    for i in range(n):
        amount = _num(amts[i] if i < len(amts) else 0)
        if amount <= 0:
            continue
        code = (codes[i] if i < len(codes) else '').strip()
        acc = LevelFive.query.filter_by(code=code).first()
        if not acc:
            raise ValueError(_t(f'GL code "{code}" not found.', f'كود دفتر الأستاذ "{code}" غير موجود.'))
        vat = _num(vats[i] if i < len(vats) else 0)
        if vat < 0:
            raise ValueError(_t('VAT cannot be negative.', 'لا يمكن أن تكون ضريبة القيمة المضافة سالبة.'))
        db.session.add(OutgoingPaymentGLDetail(
            outgoing_payment_id=doc.id, detail_id=i + 1,
            code=acc.code, name=acc.drawers or '',
            document_no=(docnos[i] if i < len(docnos) else '').strip(),
            purpose=(purps[i] if i < len(purps) else '').strip(),
            amount=round(amount, 2), vat=round(vat, 2), total=round(amount + vat, 2),
            narration=(narrs[i] if i < len(narrs) else '').strip(),
        ))
        added += 1
        total_amount += amount
        total_vat += vat

    if not added:
        raise ValueError(_t('Amount must be greater than zero.', 'يجب أن يكون المبلغ أكبر من صفر.'))

    doc.total_amount = round(total_amount, 2)
    doc.total_vat = round(total_vat, 2)
    doc.total_amount_including_vat = round(total_amount + total_vat, 2)


def _apply_op_detail(doc, f, pay_to_type, payment_type):
    """Dispatches to whichever ONE of Detail Table 1 / Detail Table 2 /
    Advance applies, always clearing the other detail table first so a
    payment can never carry rows in both at once."""
    if pay_to_type == 'GL Account':
        OutgoingPaymentAdjustment.query.filter_by(outgoing_payment_id=doc.id).delete()
        _apply_op_gl_lines(doc, f)
    elif payment_type == 'Advance':
        OutgoingPaymentGLDetail.query.filter_by(outgoing_payment_id=doc.id).delete()
        _apply_op_advance(doc, f)
    else:
        OutgoingPaymentGLDetail.query.filter_by(outgoing_payment_id=doc.id).delete()
        _apply_op_adjustment_lines(doc, f)


@cash_bank_bp.route('/cash-bank/outgoing-payments/add', methods=['POST'])
@login_required
def outgoing_payment_add():
    try:
        status = (request.form.get('status') or 'Draft').strip()
        if status not in OP_PRE_POST_STATUSES:
            status = 'Draft'
        doc = OutgoingPayment(payment_no=_next_op_no(), status=status, created_by=current_user.id)
        pay_to_type, payment_type = _apply_op_master_fields(doc, request.form)
        db.session.add(doc)
        db.session.flush()
        _apply_op_detail(doc, request.form, pay_to_type, payment_type)
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.id, 'payment_no': doc.payment_no})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@cash_bank_bp.route('/cash-bank/outgoing-payments/<int:id>/edit', methods=['POST'])
@login_required
def outgoing_payment_edit(id):
    """Editable in Draft/Approved -- the whole form only locks once Posted
    or Cancelled, matching the Purchase module's own footer mechanism
    (Cancel / Save & Post / Save)."""
    doc = OutgoingPayment.query.get_or_404(id)
    if doc.status not in OP_PRE_POST_STATUSES:
        return jsonify({'ok': False, 'error': _t(
            'This payment can no longer be edited.', 'لا يمكن تعديل هذه الدفعة بعد الآن.')}), 403
    try:
        pay_to_type, payment_type = _apply_op_master_fields(doc, request.form)
        _apply_op_detail(doc, request.form, pay_to_type, payment_type)
        doc.updated_by = current_user.id
        db.session.commit()
        return jsonify({'ok': True})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@cash_bank_bp.route('/cash-bank/outgoing-payments/<int:id>/delete', methods=['POST'])
@login_required
def outgoing_payment_delete(id):
    doc = OutgoingPayment.query.get_or_404(id)
    if doc.status != 'Draft':
        return jsonify({'ok': False, 'error': _t(
            'Only a Draft payment can be deleted.', 'يمكن حذف المسودة فقط.')}), 403
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


@cash_bank_bp.route('/cash-bank/outgoing-payments/<int:id>/approve', methods=['POST'])
@login_required
def outgoing_payment_approve(id):
    doc = OutgoingPayment.query.get_or_404(id)
    if doc.status != 'Draft':
        return jsonify({'ok': False, 'error': _t(
            'Only a Draft payment can be approved.', 'يمكن اعتماد المسودة فقط.')}), 403
    doc.status = 'Approved'
    doc.updated_by = current_user.id
    db.session.commit()
    return jsonify({'ok': True, 'status': doc.status})


@cash_bank_bp.route('/cash-bank/outgoing-payments/<int:id>/set-status', methods=['POST'])
@login_required
def outgoing_payment_set_status(id):
    """Plain status flip between Draft/Approved -- the status dropdown's own
    save action. Never touches GL (matches this app's post-only rule);
    Posted and Cancelled stay reachable only through their own dedicated
    Post/Cancel routes, so both the payment's current status and the
    requested one must already be in the pre-posting set."""
    doc = OutgoingPayment.query.get_or_404(id)
    if doc.status not in OP_PRE_POST_STATUSES:
        return jsonify({'ok': False, 'error': _t(
            'This payment has already moved past Draft/Approved.',
            'تجاوزت هذه الدفعة مرحلة مسودة/معتمد.')}), 403
    new_status = (request.form.get('status') or '').strip()
    if new_status not in OP_PRE_POST_STATUSES:
        return jsonify({'ok': False, 'error': _t('Invalid status.', 'حالة غير صالحة.')}), 400
    doc.status = new_status
    doc.updated_by = current_user.id
    db.session.commit()
    return jsonify({'ok': True, 'status': doc.status})


# ══════════════════════════════════════════════════════════════════
# OUTGOING PAYMENT -- Post (creates the GL entry, updates invoice balances)
# ══════════════════════════════════════════════════════════════════

@cash_bank_bp.route('/cash-bank/outgoing-payments/<int:id>/post', methods=['POST'])
@login_required
def outgoing_payment_post(id):
    from models import JournalEntry, JournalEntryDetail, GRLDetail, next_je_no, NoActiveFinancialYearError
    try:
        doc = OutgoingPayment.query.get_or_404(id)
        if doc.status == 'Posted':
            return jsonify({'ok': False, 'error': _t(
                'This payment has already been posted.', 'تم ترحيل هذه الدفعة مسبقاً.')}), 400
        if doc.status == 'Cancelled':
            return jsonify({'ok': False, 'error': _t(
                'This payment is cancelled.', 'تم إلغاء هذه الدفعة.')}), 400

        gl_lines = []   # [{'code','name','name_ar','ctrl','ref','debit','credit'}]

        if doc.pay_to_type == 'GL Account':
            if not doc.gl_lines:
                return jsonify({'ok': False, 'error': _t(
                    'At least one GL detail line is required.', 'مطلوب سطر تفصيل واحد على الأقل.')}), 400
            total_amount = total_vat = 0.0
            for ln in doc.gl_lines:
                acc = LevelFive.query.filter_by(code=ln.code).first()
                if not acc:
                    return jsonify({'ok': False, 'error': _t(
                        f'GL code "{ln.code}" not found.', f'كود دفتر الأستاذ "{ln.code}" غير موجود.')}), 400
                gl_lines.append({'code': acc.code, 'name': acc.drawers or '', 'name_ar': acc.drawers_ar or '',
                                 'ctrl': acc.control_account or 'No', 'ref': '',
                                 'debit': round(float(ln.amount or 0), 2), 'credit': 0})
                total_amount += float(ln.amount or 0)
                total_vat += float(ln.vat or 0)
            if total_vat > 0.005:
                vat_code, vat_name, vat_name_ar, vat_ctrl = _input_vat_account()
                gl_lines.append({'code': vat_code, 'name': vat_name, 'name_ar': vat_name_ar,
                                 'ctrl': vat_ctrl, 'ref': '', 'debit': round(total_vat, 2), 'credit': 0})
            credit_code, credit_name, credit_name_ar, credit_ctrl = _credit_account_for(doc.payment_account_id)
            gl_lines.append({'code': credit_code, 'name': credit_name, 'name_ar': credit_name_ar,
                             'ctrl': credit_ctrl, 'ref': '', 'debit': 0,
                             'credit': round(total_amount + total_vat, 2)})
            doc.total_amount = round(total_amount, 2)
            doc.total_vat = round(total_vat, 2)
            doc.total_amount_including_vat = round(total_amount + total_vat, 2)

        elif doc.payment_type == 'Advance':
            amt = float(doc.total_amount_including_vat or 0)
            if amt <= 0:
                return jsonify({'ok': False, 'error': _t(
                    'Total Payment must be greater than zero.', 'يجب أن يكون إجمالي الدفع أكبر من صفر.')}), 400
            debit_code, debit_name, debit_name_ar, debit_ctrl, debit_ref = _debit_account_for(doc)
            credit_code, credit_name, credit_name_ar, credit_ctrl = _credit_account_for(doc.payment_account_id)
            gl_lines = [
                {'code': debit_code, 'name': debit_name, 'name_ar': debit_name_ar, 'ctrl': debit_ctrl,
                 'ref': debit_ref, 'debit': round(amt, 2), 'credit': 0},
                {'code': credit_code, 'name': credit_name, 'name_ar': credit_name_ar, 'ctrl': credit_ctrl,
                 'ref': '', 'debit': 0, 'credit': round(amt, 2)},
            ]

        else:   # Outstanding -- Supplier/Buyer/Employee
            if not doc.adjustment_lines:
                return jsonify({'ok': False, 'error': _t(
                    'At least one outstanding document is required.', 'مطلوب مستند مستحق واحد على الأقل.')}), 400
            total_payment = 0.0
            # Re-validate against the DATABASE's current balance, not
            # whatever was true when this Draft was opened -- row-locked
            # (lock=True) so two concurrent Outgoing Payments can never
            # both settle the same remaining balance, and mutated right
            # here (not in a separate pass) so the lock is held from
            # validation through to commit.
            for ln in doc.adjustment_lines:
                pay_amt = float(ln.payment_amount or 0)
                if pay_amt <= 0:
                    return jsonify({'ok': False, 'error': _t(
                        'Payment Amount must be greater than zero.', 'يجب أن يكون مبلغ الدفع أكبر من صفر.')}), 400
                source, outstanding, doc_no_live, _dd, _due = _outstanding_source(
                    ln.document_type, ln.document_id, lock=True)
                if source is None:
                    return jsonify({'ok': False, 'error': _t(
                        f'{ln.document_no} no longer exists.', 'المستند المرجعي لم يعد موجوداً.')}), 400
                if pay_amt > outstanding + 0.01:
                    return jsonify({'ok': False, 'error': _t(
                        f'Payment for {doc_no_live} ({pay_amt:.2f}) exceeds its current outstanding amount '
                        f'({outstanding:.2f}).',
                        f'الدفعة لـ {doc_no_live} ({pay_amt:.2f}) تتجاوز المبلغ المستحق الحالي '
                        f'({outstanding:.2f}).')}), 400
                ln.outstanding_amount = round(outstanding, 2)
                ln.remaining_amount = round(outstanding - pay_amt, 2)
                if ln.document_type in ('Purchase Invoice', 'Sales Invoice'):
                    source.paid_amount = float(source.paid_amount or 0) + pay_amt
                elif ln.document_type == 'Payroll Payable':
                    source.paid = float(source.paid or 0) + pay_amt
                    source.balance = float(source.salary_payable or 0) - float(source.paid or 0)
                total_payment += pay_amt

            debit_code, debit_name, debit_name_ar, debit_ctrl, debit_ref = _debit_account_for(doc)
            credit_code, credit_name, credit_name_ar, credit_ctrl = _credit_account_for(doc.payment_account_id)
            gl_lines = [
                {'code': debit_code, 'name': debit_name, 'name_ar': debit_name_ar, 'ctrl': debit_ctrl,
                 'ref': debit_ref, 'debit': round(total_payment, 2), 'credit': 0},
                {'code': credit_code, 'name': credit_name, 'name_ar': credit_name_ar, 'ctrl': credit_ctrl,
                 'ref': '', 'debit': 0, 'credit': round(total_payment, 2)},
            ]
            doc.total_amount = round(total_payment, 2)
            doc.total_vat = 0
            doc.total_amount_including_vat = round(total_payment, 2)

        tot_d = round(sum(g['debit'] for g in gl_lines), 2)
        tot_c = round(sum(g['credit'] for g in gl_lines), 2)
        if abs(tot_d - tot_c) > 0.01:
            return jsonify({'ok': False, 'error': _t(
                'Total Debit must equal Total Credit.', 'يجب أن يتساوى إجمالي المدين مع إجمالي الدائن.')}), 400

        # The GL entry's posting date is always the actual moment of
        # posting, not whatever was entered/left over on the document
        # while it was Draft/Approved -- doc.posting_date is updated to
        # match so the header display stays consistent with what was
        # really posted.
        posting_date = date.today()
        doc.posting_date = posting_date
        narration = (doc.narration or f'Outgoing Payment {doc.payment_no}')[:500]

        grl = GRL.query.filter_by(outgoing_payment_id=doc.id).first()
        if not grl:
            grl = GRL(outgoing_payment_id=doc.id)
            db.session.add(grl)
        if not grl.grl_no:
            grl.grl_no = _next_grl_no()
        grl.origion = doc.payment_no or ''
        grl.posting_date = posting_date
        grl.due_date = None
        grl.document_date = doc.payment_date
        grl.narration = narration
        db.session.flush()

        je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
        if not je:
            je = JournalEntry(je_no=next_je_no(), origin_type='OP', origin_id=doc.id)
            db.session.add(je)
            db.session.flush()
            grl.journal_entry_id = je.id
        je.origion = grl.origion
        je.posting_date = grl.posting_date
        je.due_date = grl.due_date
        je.document_date = grl.document_date
        je.narration = narration

        GRLDetail.query.filter_by(grl_id=grl.id).delete()
        JournalEntryDetail.query.filter_by(journal_entry_id=je.id).delete()
        for gl in gl_lines:
            db.session.add(GRLDetail(
                grl_id=grl.id, code=gl['code'], reference_code=gl['ref'], account_name=gl['name'],
                account_name_ar=gl['name_ar'], control_account=gl['ctrl'], debit=gl['debit'],
                credit=gl['credit'], narration=narration))
            db.session.add(JournalEntryDetail(
                journal_entry_id=je.id, code=gl['code'], reference_code=gl['ref'], account_name=gl['name'],
                control_account=gl['ctrl'], debit=gl['debit'], credit=gl['credit'], narration=narration))

        doc.journal_entry_id = je.id
        doc.grl_id = grl.id
        doc.status = 'Posted'
        doc.updated_by = current_user.id

        db.session.commit()
        return jsonify({'ok': True, 'id': doc.id, 'payment_no': doc.payment_no, 'status': doc.status})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.', 'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# OUTGOING PAYMENT -- Cancel (reversing GL entry, never a deletion)
# ══════════════════════════════════════════════════════════════════

@cash_bank_bp.route('/cash-bank/outgoing-payments/<int:id>/cancel', methods=['POST'])
@login_required
def outgoing_payment_cancel(id):
    from models import JournalEntry, JournalEntryDetail, GRLDetail, next_je_no, NoActiveFinancialYearError
    try:
        doc = OutgoingPayment.query.get_or_404(id)
        if doc.status != 'Posted':
            return jsonify({'ok': False, 'error': _t(
                'Only a Posted payment can be cancelled.', 'يمكن إلغاء الدفعات المرحّلة فقط.')}), 400
        orig_grl = GRL.query.get(doc.grl_id) if doc.grl_id else None
        if not orig_grl:
            return jsonify({'ok': False, 'error': _t(
                'Original ledger entry not found.', 'القيد الأصلي غير موجود.')}), 400
        orig_details = GRLDetail.query.filter_by(grl_id=orig_grl.id).all()
        if not orig_details:
            return jsonify({'ok': False, 'error': _t(
                'Original ledger entry has no detail lines.', 'القيد الأصلي لا يحتوي على تفاصيل.')}), 400

        # Restore each settled document's balance -- row-locked the same
        # way outgoing_payment_post() locks it, for the same reason.
        for ln in doc.adjustment_lines:
            source, _outstanding, _dn, _dd, _due = _outstanding_source(ln.document_type, ln.document_id, lock=True)
            if not source:
                continue
            pay_amt = float(ln.payment_amount or 0)
            if ln.document_type in ('Purchase Invoice', 'Sales Invoice'):
                source.paid_amount = max(float(source.paid_amount or 0) - pay_amt, 0)
            elif ln.document_type == 'Payroll Payable':
                source.paid = max(float(source.paid or 0) - pay_amt, 0)
                source.balance = float(source.salary_payable or 0) - float(source.paid or 0)

        narration = f'Reversal of {doc.payment_no}'
        rev_grl = GRL(outgoing_payment_id=doc.id, grl_no=_next_grl_no(), origion=narration,
                      posting_date=date.today(), due_date=orig_grl.due_date,
                      document_date=date.today(), narration=narration)
        db.session.add(rev_grl)
        db.session.flush()

        rev_je = JournalEntry(je_no=next_je_no(), origin_type='OP', origin_id=doc.id,
                              origion=narration, posting_date=rev_grl.posting_date,
                              due_date=rev_grl.due_date, document_date=rev_grl.document_date,
                              narration=narration)
        db.session.add(rev_je)
        db.session.flush()
        rev_grl.journal_entry_id = rev_je.id

        for d in orig_details:
            db.session.add(GRLDetail(
                grl_id=rev_grl.id, code=d.code, account_name=d.account_name,
                account_name_ar=d.account_name_ar, control_account=d.control_account,
                debit=d.credit, credit=d.debit, narration=narration))
            db.session.add(JournalEntryDetail(
                journal_entry_id=rev_je.id, code=d.code, account_name=d.account_name,
                control_account=d.control_account, debit=d.credit, credit=d.debit, narration=narration))

        doc.status = 'Cancelled'
        doc.updated_by = current_user.id
        db.session.commit()
        return jsonify({'ok': True, 'status': doc.status})
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.', 'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# PAYMENT MODE -- admin-configurable Payment Mode -> GL Account map
# ══════════════════════════════════════════════════════════════════

@cash_bank_bp.route('/cash-bank/payment-modes')
@login_required
@admin_required
def payment_mode_list():
    return render_template('cash_bank/payment_mode_settings.html')


@cash_bank_bp.route('/cash-bank/payment-modes/data')
@login_required
def payment_mode_data():
    return jsonify([p.to_dict() for p in PaymentMode.query.order_by(PaymentMode.id).all()])


@cash_bank_bp.route('/cash-bank/payment-modes/<int:id>/update', methods=['POST'])
@login_required
@admin_required
def payment_mode_update(id):
    pm = PaymentMode.query.get_or_404(id)
    gl = (request.form.get('gl_account_id') or '').strip()
    if gl and not LevelFive.query.filter_by(code=gl).first():
        return jsonify({'ok': False, 'error': _t(
            'GL account not found.', 'حساب دفتر الأستاذ غير موجود.')}), 400
    pm.gl_account_id = gl or None
    pm.active = (request.form.get('active') or '').strip().lower() in ('1', 'true', 'on', 'yes')
    db.session.commit()
    return jsonify({'ok': True, 'payment_mode': pm.to_dict()})


####################################################################
# INCOMING PAYMENT -- the AR-side mirror of Outgoing Payment (every
# debit/credit side reversed)
####################################################################

def _next_ip_no():
    """IP-<active FY year>-<n>, the exact mirror of _next_op_no()."""
    from models import active_fy_year
    year = active_fy_year() or datetime.utcnow().year
    like = f'IP-{year}-%'
    max_num = 0
    for doc in IncomingPayment.query.filter(IncomingPayment.payment_no.like(like)).all():
        if doc.payment_no:
            try:
                num = int(doc.payment_no.rsplit('-', 1)[1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue
    n = max_num + 1
    payment_no = f'IP-{year}-{n}'
    retries = 0
    while IncomingPayment.query.filter_by(payment_no=payment_no).first() and retries < 100:
        n += 1
        payment_no = f'IP-{year}-{n}'
        retries += 1
    return payment_no


_RECEIVE_FROM_PARTY_LOOKUP = {}


def _receive_from_party_lookup():
    if not _RECEIVE_FROM_PARTY_LOOKUP:
        _RECEIVE_FROM_PARTY_LOOKUP.update({
            'Buyer': (BuyerMaster, 'buyer_code'),
            'Supplier': (SupplierMaster, 'supplier_code'),
            'Employee': (Employee, 'employee_code'),
        })
    return _RECEIVE_FROM_PARTY_LOOKUP


def _credit_account_for_ip(doc):
    """(code, name_en, name_ar, control_account, reference_code) for the
    Credit side of an Incoming Payment's GL entry -- Buyer/Supplier/
    Employee only (GL Account posts each Detail Table 2 line's own code
    directly, see incoming_payment_post()). Exact mirror of
    _debit_account_for(): the per-party control-account override first
    (reference_code = the party's own code), else the shared Auto Code
    Selection account for that Receive From type + Payment Type pair --
    Outstanding settlement and Advance are deliberately separate accounts
    (e.g. "Buyer (AR)" vs. "Advance from Buyer"), since they represent
    different balances even for the same party."""
    model_lookup = _receive_from_party_lookup()
    model, code_attr = model_lookup[doc.receive_from_type]
    party = None
    if doc.receive_from_id:
        try:
            party = model.query.get(int(doc.receive_from_id))
        except (TypeError, ValueError):
            party = None
    if party and getattr(party, 'levelfive_code', None):
        party_account = LevelFive.query.filter_by(code=party.levelfive_code).first()
        if party_account and party_account.control_account == 'Yes':
            return (party_account.code, party_account.drawers or '', party_account.drawers_ar or '',
                    party_account.control_account, getattr(party, code_attr, '') or '')

    form_code = {
        ('Buyer', 'Outstanding'): 'incoming_payment_buyer',
        ('Buyer', 'Advance'): 'incoming_payment_buyer_advance',
        ('Supplier', 'Outstanding'): 'incoming_payment_supplier',
        ('Supplier', 'Advance'): 'incoming_payment_supplier_advance',
        ('Employee', 'Outstanding'): 'incoming_payment_employee',
        ('Employee', 'Advance'): 'incoming_payment_employee_advance',
    }[(doc.receive_from_type, doc.payment_type)]
    row = _get_auto_code('cash_bank', form_code)
    if not row or not row.levelfive_code:
        raise ValueError(_t(
            f'No GL account configured for "{form_code}". Configure it via '
            'Chart of Accounts > Auto Code Selection.',
            f'لم يتم تكوين حساب دفتر الأستاذ لـ "{form_code}". قم بتكوينه عبر '
            'دليل الحسابات > اختيار الكود التلقائي.'))
    acc = LevelFive.query.filter_by(code=row.levelfive_code).first()
    ctrl = (acc.control_account or 'No') if acc else 'No'
    return row.levelfive_code, row.levelfive_drawer_en or '', row.levelfive_drawer_ar or '', ctrl, ''


def _debit_account_for_ip(payment_account_id):
    """(code, name_en, name_ar, control_account) for the GL account this
    receipt is actually received INTO -- exact mirror of
    _credit_account_for(). Required in every case."""
    if not payment_account_id:
        raise ValueError(_t('Select the Payment Account.', 'اختر حساب الدفع.'))
    acc = LevelFive.query.filter_by(code=payment_account_id).first()
    if not acc:
        raise ValueError(_t(
            'The selected Payment Account was not found.',
            'حساب الدفع المحدد غير موجود.'))
    return acc.code, acc.drawers or '', acc.drawers_ar or '', acc.control_account or 'No'


def _output_vat_account():
    """(code, name_en, name_ar, control_account) for the configured Output
    VAT account -- the extra Credit line Detail Table 2 (GL Account) posts
    when any of its lines carries a VAT amount. Mirror of
    _input_vat_account()."""
    row = _get_auto_code('cash_bank', 'incoming_payment_output_vat')
    if not row or not row.levelfive_code:
        raise ValueError(_t(
            'No GL account configured for Output VAT. Configure it via '
            'Chart of Accounts > Auto Code Selection.',
            'لم يتم تكوين حساب دفتر الأستاذ لضريبة القيمة المضافة المخرجات. قم بتكوينه عبر '
            'دليل الحسابات > اختيار الكود التلقائي.'))
    acc = LevelFive.query.filter_by(code=row.levelfive_code).first()
    ctrl = (acc.control_account or 'No') if acc else 'No'
    return row.levelfive_code, row.levelfive_drawer_en or '', row.levelfive_drawer_ar or '', ctrl


@cash_bank_bp.route('/cash-bank/incoming-payments/grl-preview')
@login_required
def incoming_payment_grl_preview():
    """Live, read-only preview of the GRL/JournalEntry lines Post would
    create -- the exact mirror of outgoing_payment_grl_preview()."""
    from types import SimpleNamespace
    receive_from_type = (request.args.get('receive_from_type') or '').strip()
    receive_from_id = (request.args.get('receive_from_id') or '').strip()
    payment_type = (request.args.get('payment_type') or '').strip()
    payment_account_id = (request.args.get('payment_account_id') or '').strip()

    if receive_from_type == 'GL Account':
        try:
            gl_lines_raw = json.loads(request.args.get('gl_lines_json') or '[]')
        except (TypeError, ValueError):
            gl_lines_raw = []
        lines = []
        total_amount = total_vat = 0.0
        for gl in gl_lines_raw:
            code = (gl.get('code') or '').strip()
            amount = _num(gl.get('amount'))
            vat = _num(gl.get('vat'))
            if not code or amount <= 0:
                continue
            acc = LevelFive.query.filter_by(code=code).first()
            if not acc:
                continue
            lines.append({'code': acc.code, 'account_name': acc.drawers or '',
                          'control_account': acc.control_account or 'No', 'reference_code': '',
                          'debit': 0, 'credit': round(amount, 2)})
            total_amount += amount
            total_vat += vat
        if not lines:
            return jsonify({'ok': True, 'lines': []})
        try:
            debit_code, debit_name, _dn_ar, debit_ctrl = _debit_account_for_ip(payment_account_id)
            if total_vat > 0.005:
                vat_code, vat_name, _va, vat_ctrl = _output_vat_account()
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        result_lines = [{'code': debit_code, 'account_name': debit_name, 'control_account': debit_ctrl,
                         'reference_code': '', 'debit': round(total_amount + total_vat, 2), 'credit': 0}]
        result_lines.extend(lines)
        if total_vat > 0.005:
            result_lines.append({'code': vat_code, 'account_name': vat_name, 'control_account': vat_ctrl,
                                 'reference_code': '', 'debit': 0, 'credit': round(total_vat, 2)})
        return jsonify({'ok': True, 'lines': result_lines})

    if not receive_from_type or not receive_from_id or payment_type not in ('Advance', 'Outstanding'):
        return jsonify({'ok': True, 'lines': []})
    total_amount_including_vat = _num(request.args.get('total_amount_including_vat'))
    if total_amount_including_vat <= 0:
        return jsonify({'ok': True, 'lines': []})

    try:
        fake_doc = SimpleNamespace(receive_from_type=receive_from_type, receive_from_id=receive_from_id,
                                    payment_type=payment_type)
        debit_code, debit_name, _dn_ar, debit_ctrl = _debit_account_for_ip(payment_account_id)
        credit_code, credit_name, _cn_ar, credit_ctrl, credit_ref = _credit_account_for_ip(fake_doc)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    lines = [
        {'code': debit_code, 'account_name': debit_name, 'control_account': debit_ctrl,
         'reference_code': '', 'debit': round(total_amount_including_vat, 2), 'credit': 0},
        {'code': credit_code, 'account_name': credit_name, 'control_account': credit_ctrl,
         'reference_code': credit_ref, 'debit': 0, 'credit': round(total_amount_including_vat, 2)},
    ]
    return jsonify({'ok': True, 'lines': lines})


@cash_bank_bp.route('/cash-bank/incoming-payments/next-doc-numbers')
@login_required
def incoming_payment_next_doc_numbers():
    """Mirror of outgoing_payment_next_doc_numbers()."""
    from models import next_je_no, NoActiveFinancialYearError
    je_no = ''
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'payment_no': _next_ip_no(), 'grl_no': _next_grl_no(), 'je_no': je_no})


# ══════════════════════════════════════════════════════════════════
# INCOMING PAYMENT -- Pages
# ══════════════════════════════════════════════════════════════════

@cash_bank_bp.route('/cash-bank/incoming-payments')
@login_required
def incoming_payment_list():
    return render_template('cash_bank/incoming_payment.html')


@cash_bank_bp.route('/cash-bank/incoming-payments/<int:id>/print')
@login_required
def incoming_payment_print(id):
    doc = IncomingPayment.query.get_or_404(id)
    owner = Owner.query.first()
    return render_template('cash_bank/incoming_payment_print.html', doc=doc, owner=owner)


# ══════════════════════════════════════════════════════════════════
# INCOMING PAYMENT -- Data / lookups
# ══════════════════════════════════════════════════════════════════

@cash_bank_bp.route('/cash-bank/incoming-payments/data')
@login_required
def incoming_payment_data():
    q = IncomingPayment.query
    payment_no = (request.args.get('payment_no') or '').strip()
    receive_from_type = (request.args.get('receive_from_type') or '').strip()
    receive_from_id = (request.args.get('receive_from_id') or '').strip()
    date_from = _pd(request.args.get('date_from'))
    date_to = _pd(request.args.get('date_to'))
    status = (request.args.get('status') or '').strip()
    if payment_no:
        q = q.filter(IncomingPayment.payment_no.ilike(f'%{payment_no}%'))
    if receive_from_type:
        q = q.filter(IncomingPayment.receive_from_type == receive_from_type)
    if receive_from_id:
        q = q.filter(IncomingPayment.receive_from_id == receive_from_id)
    if date_from:
        q = q.filter(IncomingPayment.payment_date >= date_from)
    if date_to:
        q = q.filter(IncomingPayment.payment_date <= date_to)
    if status:
        q = q.filter(IncomingPayment.status == status)
    rows = q.order_by(IncomingPayment.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@cash_bank_bp.route('/cash-bank/incoming-payments/<int:id>/json')
@login_required
def incoming_payment_json(id):
    ip = IncomingPayment.query.get_or_404(id)
    d = ip.to_dict()
    # Same read-only item breakdown Outgoing Payment attaches -- see
    # outgoing_payment_json()'s identical rationale.
    for line, ln_dict in zip(ip.adjustment_lines, d['adjustment_lines']):
        items = []
        if line.document_type == 'Sales Invoice':
            inv = SalesInvoice.query.get(line.document_id)
            if inv:
                items = [li.to_dict() for li in sorted(inv.line_items, key=lambda li: li.line_number)]
        elif line.document_type == 'Purchase Invoice':
            inv = PurchaseInvoice.query.get(line.document_id)
            if inv:
                items = [li.to_dict() for li in sorted(inv.line_items, key=lambda li: li.line_number)]
        ln_dict['items'] = items
    return jsonify(d)


@cash_bank_bp.route('/cash-bank/incoming-payments/next-doc-no')
@login_required
def incoming_payment_next_doc_no():
    return jsonify({'payment_no': _next_ip_no()})


@cash_bank_bp.route('/cash-bank/incoming-payments/payment-modes')
@login_required
def incoming_payment_modes_lookup():
    return jsonify([p.to_dict() for p in PaymentMode.query.filter_by(active=True).order_by(PaymentMode.name).all()])


@cash_bank_bp.route('/cash-bank/incoming-payments/buyers')
@login_required
def incoming_payment_buyers():
    rows = BuyerMaster.query.order_by(BuyerMaster.buyer_name_en).all()
    return jsonify([{'id': b.id, 'name': b.buyer_name_en or ''} for b in rows])


@cash_bank_bp.route('/cash-bank/incoming-payments/suppliers')
@login_required
def incoming_payment_suppliers():
    rows = SupplierMaster.query.order_by(SupplierMaster.supplier_name_en).all()
    return jsonify([{'id': s.id, 'name': s.supplier_name_en or ''} for s in rows])


@cash_bank_bp.route('/cash-bank/incoming-payments/employees')
@login_required
def incoming_payment_employees():
    rows = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()
    return jsonify([{'id': e.id, 'name': e.name or ''} for e in rows])


@cash_bank_bp.route('/cash-bank/incoming-payments/accounts')
@login_required
def incoming_payment_accounts():
    """Mirror of outgoing_payment_accounts() -- same A2-07 Cash/Bank
    scoping by Payment Method."""
    q = (LevelFive.query
         .join(LevelFour, LevelFive.level_four_code == LevelFour.code)
         .filter(LevelFour.level_three_code == 'A2-07'))
    payment_method_id = request.args.get('payment_method_id', type=int)
    if payment_method_id:
        l4_code = _payment_account_l4(PaymentMode.query.get(payment_method_id))
        q = q.filter(LevelFour.code == l4_code)
    rows = q.order_by(LevelFive.code).all()
    return jsonify([{'code': a.code, 'name': a.drawers or ''} for a in rows])


@cash_bank_bp.route('/cash-bank/incoming-payments/buyer-invoices')
@login_required
def incoming_payment_buyer_invoices():
    """Outstanding (Posted, balance > 0) Sales Invoices for a buyer --
    mirror of outgoing_payment_supplier_invoices()."""
    buyer_id = request.args.get('buyer_id', type=int)
    if not buyer_id:
        return jsonify([])
    invs = (SalesInvoice.query
            .filter(SalesInvoice.buyer_id == buyer_id)
            .filter(SalesInvoice.posting_status == 'Posted')
            .order_by(SalesInvoice.document_date).all())
    return jsonify(_outstanding_docs_response(
        invs, 'doc_no', 'document_date', 'buyer_ref_no', 'sales_invoice_id'))


@cash_bank_bp.route('/cash-bank/incoming-payments/supplier-invoices')
@login_required
def incoming_payment_supplier_invoices():
    """Outstanding (Posted, balance > 0) Purchase Invoices for a supplier
    -- for the (less common, but symmetric) case of collecting a refund
    FROM a supplier. Mirror of outgoing_payment_supplier_invoices()."""
    supplier_id = request.args.get('supplier_id', type=int)
    if not supplier_id:
        return jsonify([])
    invs = (PurchaseInvoice.query
            .filter(PurchaseInvoice.supplier_id == supplier_id)
            .filter(PurchaseInvoice.posting_status == 'Posted')
            .order_by(PurchaseInvoice.document_date).all())
    return jsonify(_outstanding_docs_response(
        invs, 'doc_no', 'document_date', 'supplier_ref_no', 'purchase_invoice_id'))


@cash_bank_bp.route('/cash-bank/incoming-payments/employee-payables')
@login_required
def incoming_payment_employee_payables():
    """Outstanding (Posted, balance > 0) Payroll Payable rows for an
    employee -- for collecting a recovery FROM an employee (e.g. an
    over-payment). Mirror of outgoing_payment_employee_payables()."""
    employee_id = request.args.get('employee_id', type=int)
    if not employee_id:
        return jsonify([])
    rows = (SalaryConsolidation.query
            .filter(SalaryConsolidation.employee_id == employee_id)
            .filter(SalaryConsolidation.payroll_status == 'Post')
            .order_by(SalaryConsolidation.month_to).all())
    out = []
    for row in rows:
        balance = float(row.salary_payable or 0) - float(row.paid or 0)
        if balance <= 0.005:
            continue
        out.append({
            'document_id': row.id,
            'document_no': row.payroll_id or '',
            'document_date': str(row.month_to) if row.month_to else '',
            'due_date': '',
            'reference_no': '',
            'outstanding_amount': balance,
        })
    return jsonify(out)


# ══════════════════════════════════════════════════════════════════
# INCOMING PAYMENT -- Draft save (no GL, no balance mutation)
# ══════════════════════════════════════════════════════════════════

def _apply_ip_master_fields(doc, f):
    """Mirror of _apply_op_master_fields(). Returns (receive_from_type,
    payment_type)."""
    receive_from_type = (f.get('receive_from_type') or '').strip()
    if receive_from_type not in ('Buyer', 'Supplier', 'Employee', 'GL Account'):
        raise ValueError(_t('Select a valid Receive From type.', 'اختر نوع جهة استلام صالح.'))
    receive_from_id = (f.get('receive_from_id') or '').strip()
    # Receive From = GL Account has no single party of its own -- Detail
    # Table 2 names each line's own account directly.
    if receive_from_type != 'GL Account' and not receive_from_id:
        raise ValueError(_t('Select the Buyer/Supplier/Employee.',
                             'اختر المشتري/المورد/الموظف.'))
    doc.receive_from_type = receive_from_type
    doc.receive_from_id = receive_from_id or None

    payment_type = (f.get('payment_type') or '').strip()
    if receive_from_type == 'GL Account':
        payment_type = None
    elif payment_type not in ('Advance', 'Outstanding'):
        raise ValueError(_t('Select a valid Payment Type.', 'اختر نوع دفعة صالح.'))
    doc.payment_type = payment_type

    doc.payment_date = _pd(f.get('payment_date'))
    # Posting Date follows the same mechanism as Outgoing Payment: blank
    # while Draft, and incoming_payment_post() always posts as of today()
    # regardless of whatever is stored here.
    doc.posting_date = _pd(f.get('posting_date'))
    doc.reference_no = (f.get('reference_no') or '').strip()
    doc.narration = (f.get('narration') or '').strip()
    if not doc.payment_date:
        raise ValueError(_t('Payment Date is required.', 'تاريخ الدفع مطلوب.'))

    payment_method_id = f.get('payment_method_id', type=int)
    if not payment_method_id:
        raise ValueError(_t('Payment Method is required.', 'طريقة الدفع مطلوبة.'))
    payment_method = PaymentMode.query.get(payment_method_id)
    if not payment_method:
        raise ValueError(_t('Invalid Payment Method.', 'طريقة دفع غير صالحة.'))
    doc.payment_method_id = payment_method_id

    # Payment Account -- the Cash/Bank account this receipt is actually
    # received INTO, scoped exactly like Outgoing Payment's own
    # payment_account_id (see _payment_account_l4()).
    payment_account_id = (f.get('payment_account_id') or '').strip()
    if not payment_account_id:
        raise ValueError(_t('Select the Payment Account.', 'اختر حساب الدفع.'))
    l4_code = _payment_account_l4(payment_method)
    acc = LevelFive.query.filter_by(code=payment_account_id).first()
    if not acc or acc.level_four_code != l4_code:
        raise ValueError(_t(
            f'This Payment Method only accepts an account under {l4_code}.',
            f'تقبل طريقة الدفع هذه فقط حسابًا ضمن {l4_code}.'))
    doc.payment_account_id = payment_account_id

    return receive_from_type, payment_type


def _apply_ip_adjustment_lines(doc, f):
    """Detail Table 1 (Outstanding receipts only). Mirror of
    _apply_op_adjustment_lines()."""
    dtypes   = request.form.getlist('adj_document_type[]')
    dids     = request.form.getlist('adj_document_id[]')
    docnos   = request.form.getlist('adj_document_no[]')
    duedates = request.form.getlist('adj_due_date[]')
    pays     = request.form.getlist('adj_payment_amount[]')
    refnos   = request.form.getlist('adj_reference_no[]')
    narrs    = request.form.getlist('adj_narration[]')

    n = len(pays)
    if n == 0:
        raise ValueError(_t('At least one outstanding document is required.',
                             'مطلوب مستند مستحق واحد على الأقل.'))

    IncomingPaymentAdjustment.query.filter_by(incoming_payment_id=doc.id).delete()

    seen = set()
    total_payment = 0.0
    added = 0
    for i in range(n):
        pay_amt = _num(pays[i] if i < len(pays) else 0)
        if pay_amt <= 0:
            continue
        document_type = (dtypes[i] if i < len(dtypes) else '').strip()
        if document_type not in ('Sales Invoice', 'Purchase Invoice', 'Payroll Payable'):
            raise ValueError(_t('Invalid outstanding document type.', 'نوع مستند مستحق غير صالح.'))
        try:
            document_id = int(dids[i]) if i < len(dids) and dids[i] else None
        except ValueError:
            document_id = None
        if not document_id:
            raise ValueError(_t('Invalid outstanding document.', 'مستند مستحق غير صالح.'))
        key = (document_type, document_id)
        if key in seen:
            raise ValueError(_t('The same document cannot be added twice to one payment.',
                                 'لا يمكن إضافة نفس المستند مرتين في نفس الدفعة.'))
        seen.add(key)

        source, outstanding, doc_no_live, doc_date_live, _due = _outstanding_source(document_type, document_id)
        if not source:
            raise ValueError(_t('A referenced outstanding document no longer exists.',
                                 'أحد المستندات المستحقة المرجعية لم يعد موجوداً.'))
        if pay_amt > outstanding + 0.01:
            raise ValueError(_t(
                f'Payment for {doc_no_live} ({pay_amt:.2f}) cannot exceed its outstanding amount ({outstanding:.2f}).',
                f'الدفعة لـ {doc_no_live} ({pay_amt:.2f}) لا يمكن أن تتجاوز المبلغ المستحق ({outstanding:.2f}).'))

        db.session.add(IncomingPaymentAdjustment(
            incoming_payment_id=doc.id, detail_id=i + 1,
            document_type=document_type, document_id=document_id,
            document_no=doc_no_live, document_date=doc_date_live,
            due_date=_pd(duedates[i]) if i < len(duedates) else None,
            outstanding_amount=round(outstanding, 2),
            payment_amount=round(pay_amt, 2),
            remaining_amount=round(outstanding - pay_amt, 2),
            reference_no=(refnos[i] if i < len(refnos) else '').strip(),
            narration=(narrs[i] if i < len(narrs) else '').strip(),
        ))
        added += 1
        total_payment += pay_amt

    if not added:
        raise ValueError(_t('Payment Amount must be greater than zero.',
                             'يجب أن يكون مبلغ الدفع أكبر من صفر.'))

    doc.total_amount = round(total_payment, 2)
    doc.total_vat = 0
    doc.total_amount_including_vat = round(total_payment, 2)


def _apply_ip_advance(doc, f):
    """Advance payment_type: no detail lines at all. Mirror of
    _apply_op_advance()."""
    amount = _num(f.get('advance_amount'))
    if amount <= 0:
        raise ValueError(_t('Advance amount must be greater than zero.',
                             'يجب أن يكون مبلغ السلفة أكبر من صفر.'))
    IncomingPaymentAdjustment.query.filter_by(incoming_payment_id=doc.id).delete()
    doc.total_amount = round(amount, 2)
    doc.total_vat = 0
    doc.total_amount_including_vat = round(amount, 2)


def _apply_ip_gl_lines(doc, f):
    """Detail Table 2 (Receive From = GL Account only). Mirror of
    _apply_op_gl_lines()."""
    codes  = request.form.getlist('gl_code[]')
    docnos = request.form.getlist('gl_document_no[]')
    purps  = request.form.getlist('gl_purpose[]')
    amts   = request.form.getlist('gl_amount[]')
    vats   = request.form.getlist('gl_vat[]')
    narrs  = request.form.getlist('gl_narration[]')

    n = len(amts)
    if n == 0:
        raise ValueError(_t('At least one GL detail line is required.',
                             'مطلوب سطر تفصيل واحد على الأقل.'))

    IncomingPaymentGLDetail.query.filter_by(incoming_payment_id=doc.id).delete()

    total_amount = total_vat = 0.0
    added = 0
    for i in range(n):
        amount = _num(amts[i] if i < len(amts) else 0)
        if amount <= 0:
            continue
        code = (codes[i] if i < len(codes) else '').strip()
        acc = LevelFive.query.filter_by(code=code).first()
        if not acc:
            raise ValueError(_t(f'GL code "{code}" not found.', f'كود دفتر الأستاذ "{code}" غير موجود.'))
        vat = _num(vats[i] if i < len(vats) else 0)
        if vat < 0:
            raise ValueError(_t('VAT cannot be negative.', 'لا يمكن أن تكون ضريبة القيمة المضافة سالبة.'))
        db.session.add(IncomingPaymentGLDetail(
            incoming_payment_id=doc.id, detail_id=i + 1,
            code=acc.code, name=acc.drawers or '',
            document_no=(docnos[i] if i < len(docnos) else '').strip(),
            purpose=(purps[i] if i < len(purps) else '').strip(),
            amount=round(amount, 2), vat=round(vat, 2), total=round(amount + vat, 2),
            narration=(narrs[i] if i < len(narrs) else '').strip(),
        ))
        added += 1
        total_amount += amount
        total_vat += vat

    if not added:
        raise ValueError(_t('Amount must be greater than zero.', 'يجب أن يكون المبلغ أكبر من صفر.'))

    doc.total_amount = round(total_amount, 2)
    doc.total_vat = round(total_vat, 2)
    doc.total_amount_including_vat = round(total_amount + total_vat, 2)


def _apply_ip_detail(doc, f, receive_from_type, payment_type):
    """Dispatches to whichever ONE of Detail Table 1 / Detail Table 2 /
    Advance applies. Mirror of _apply_op_detail()."""
    if receive_from_type == 'GL Account':
        IncomingPaymentAdjustment.query.filter_by(incoming_payment_id=doc.id).delete()
        _apply_ip_gl_lines(doc, f)
    elif payment_type == 'Advance':
        IncomingPaymentGLDetail.query.filter_by(incoming_payment_id=doc.id).delete()
        _apply_ip_advance(doc, f)
    else:
        IncomingPaymentGLDetail.query.filter_by(incoming_payment_id=doc.id).delete()
        _apply_ip_adjustment_lines(doc, f)


@cash_bank_bp.route('/cash-bank/incoming-payments/add', methods=['POST'])
@login_required
def incoming_payment_add():
    try:
        status = (request.form.get('status') or 'Draft').strip()
        if status not in IP_PRE_POST_STATUSES:
            status = 'Draft'
        doc = IncomingPayment(payment_no=_next_ip_no(), status=status, created_by=current_user.id)
        receive_from_type, payment_type = _apply_ip_master_fields(doc, request.form)
        db.session.add(doc)
        db.session.flush()
        _apply_ip_detail(doc, request.form, receive_from_type, payment_type)
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.id, 'payment_no': doc.payment_no})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@cash_bank_bp.route('/cash-bank/incoming-payments/<int:id>/edit', methods=['POST'])
@login_required
def incoming_payment_edit(id):
    """Editable in Draft/Approved -- mirrors outgoing_payment_edit()."""
    doc = IncomingPayment.query.get_or_404(id)
    if doc.status not in IP_PRE_POST_STATUSES:
        return jsonify({'ok': False, 'error': _t(
            'This payment can no longer be edited.', 'لا يمكن تعديل هذه الدفعة بعد الآن.')}), 403
    try:
        receive_from_type, payment_type = _apply_ip_master_fields(doc, request.form)
        _apply_ip_detail(doc, request.form, receive_from_type, payment_type)
        doc.updated_by = current_user.id
        db.session.commit()
        return jsonify({'ok': True})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@cash_bank_bp.route('/cash-bank/incoming-payments/<int:id>/delete', methods=['POST'])
@login_required
def incoming_payment_delete(id):
    doc = IncomingPayment.query.get_or_404(id)
    if doc.status != 'Draft':
        return jsonify({'ok': False, 'error': _t(
            'Only a Draft payment can be deleted.', 'يمكن حذف المسودة فقط.')}), 403
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


@cash_bank_bp.route('/cash-bank/incoming-payments/<int:id>/set-status', methods=['POST'])
@login_required
def incoming_payment_set_status(id):
    """Mirror of outgoing_payment_set_status()."""
    doc = IncomingPayment.query.get_or_404(id)
    if doc.status not in IP_PRE_POST_STATUSES:
        return jsonify({'ok': False, 'error': _t(
            'This payment has already moved past Draft/Approved.',
            'تجاوزت هذه الدفعة مرحلة مسودة/معتمد.')}), 403
    new_status = (request.form.get('status') or '').strip()
    if new_status not in IP_PRE_POST_STATUSES:
        return jsonify({'ok': False, 'error': _t('Invalid status.', 'حالة غير صالحة.')}), 400
    doc.status = new_status
    doc.updated_by = current_user.id
    db.session.commit()
    return jsonify({'ok': True, 'status': doc.status})


# ══════════════════════════════════════════════════════════════════
# INCOMING PAYMENT -- Post (creates the GL entry, updates invoice balances)
# ══════════════════════════════════════════════════════════════════

@cash_bank_bp.route('/cash-bank/incoming-payments/<int:id>/post', methods=['POST'])
@login_required
def incoming_payment_post(id):
    """Mirror of outgoing_payment_post(): every debit/credit side
    reversed -- Debit = Payment Account (money received INTO it) or each
    GL line's own account, Credit = the party's AR/Advance control
    account or the configured Output VAT account."""
    from models import JournalEntry, JournalEntryDetail, GRLDetail, next_je_no, NoActiveFinancialYearError
    try:
        doc = IncomingPayment.query.get_or_404(id)
        if doc.status == 'Posted':
            return jsonify({'ok': False, 'error': _t(
                'This payment has already been posted.', 'تم ترحيل هذه الدفعة مسبقاً.')}), 400
        if doc.status == 'Cancelled':
            return jsonify({'ok': False, 'error': _t(
                'This payment is cancelled.', 'تم إلغاء هذه الدفعة.')}), 400

        gl_lines = []

        if doc.receive_from_type == 'GL Account':
            if not doc.gl_lines:
                return jsonify({'ok': False, 'error': _t(
                    'At least one GL detail line is required.', 'مطلوب سطر تفصيل واحد على الأقل.')}), 400
            total_amount = total_vat = 0.0
            for ln in doc.gl_lines:
                acc = LevelFive.query.filter_by(code=ln.code).first()
                if not acc:
                    return jsonify({'ok': False, 'error': _t(
                        f'GL code "{ln.code}" not found.', f'كود دفتر الأستاذ "{ln.code}" غير موجود.')}), 400
                gl_lines.append({'code': acc.code, 'name': acc.drawers or '', 'name_ar': acc.drawers_ar or '',
                                 'ctrl': acc.control_account or 'No', 'ref': '',
                                 'debit': 0, 'credit': round(float(ln.amount or 0), 2)})
                total_amount += float(ln.amount or 0)
                total_vat += float(ln.vat or 0)
            debit_code, debit_name, debit_name_ar, debit_ctrl = _debit_account_for_ip(doc.payment_account_id)
            gl_lines.insert(0, {'code': debit_code, 'name': debit_name, 'name_ar': debit_name_ar,
                                'ctrl': debit_ctrl, 'ref': '',
                                'debit': round(total_amount + total_vat, 2), 'credit': 0})
            if total_vat > 0.005:
                vat_code, vat_name, vat_name_ar, vat_ctrl = _output_vat_account()
                gl_lines.append({'code': vat_code, 'name': vat_name, 'name_ar': vat_name_ar,
                                 'ctrl': vat_ctrl, 'ref': '', 'debit': 0, 'credit': round(total_vat, 2)})
            doc.total_amount = round(total_amount, 2)
            doc.total_vat = round(total_vat, 2)
            doc.total_amount_including_vat = round(total_amount + total_vat, 2)

        elif doc.payment_type == 'Advance':
            amt = float(doc.total_amount_including_vat or 0)
            if amt <= 0:
                return jsonify({'ok': False, 'error': _t(
                    'Total Payment must be greater than zero.', 'يجب أن يكون إجمالي الدفع أكبر من صفر.')}), 400
            debit_code, debit_name, debit_name_ar, debit_ctrl = _debit_account_for_ip(doc.payment_account_id)
            credit_code, credit_name, credit_name_ar, credit_ctrl, credit_ref = _credit_account_for_ip(doc)
            gl_lines = [
                {'code': debit_code, 'name': debit_name, 'name_ar': debit_name_ar, 'ctrl': debit_ctrl,
                 'ref': '', 'debit': round(amt, 2), 'credit': 0},
                {'code': credit_code, 'name': credit_name, 'name_ar': credit_name_ar, 'ctrl': credit_ctrl,
                 'ref': credit_ref, 'debit': 0, 'credit': round(amt, 2)},
            ]

        else:   # Outstanding -- Buyer/Supplier/Employee
            if not doc.adjustment_lines:
                return jsonify({'ok': False, 'error': _t(
                    'At least one outstanding document is required.', 'مطلوب مستند مستحق واحد على الأقل.')}), 400
            total_payment = 0.0
            # Re-validate against the DATABASE's current balance, row-
            # locked -- see outgoing_payment_post()'s identical rationale.
            for ln in doc.adjustment_lines:
                pay_amt = float(ln.payment_amount or 0)
                if pay_amt <= 0:
                    return jsonify({'ok': False, 'error': _t(
                        'Payment Amount must be greater than zero.', 'يجب أن يكون مبلغ الدفع أكبر من صفر.')}), 400
                source, outstanding, doc_no_live, _dd, _due = _outstanding_source(
                    ln.document_type, ln.document_id, lock=True)
                if source is None:
                    return jsonify({'ok': False, 'error': _t(
                        f'{ln.document_no} no longer exists.', 'المستند المرجعي لم يعد موجوداً.')}), 400
                if pay_amt > outstanding + 0.01:
                    return jsonify({'ok': False, 'error': _t(
                        f'Payment for {doc_no_live} ({pay_amt:.2f}) exceeds its current outstanding amount '
                        f'({outstanding:.2f}).',
                        f'الدفعة لـ {doc_no_live} ({pay_amt:.2f}) تتجاوز المبلغ المستحق الحالي '
                        f'({outstanding:.2f}).')}), 400
                ln.outstanding_amount = round(outstanding, 2)
                ln.remaining_amount = round(outstanding - pay_amt, 2)
                if ln.document_type in ('Purchase Invoice', 'Sales Invoice'):
                    source.paid_amount = float(source.paid_amount or 0) + pay_amt
                elif ln.document_type == 'Payroll Payable':
                    source.paid = float(source.paid or 0) + pay_amt
                    source.balance = float(source.salary_payable or 0) - float(source.paid or 0)
                total_payment += pay_amt

            debit_code, debit_name, debit_name_ar, debit_ctrl = _debit_account_for_ip(doc.payment_account_id)
            credit_code, credit_name, credit_name_ar, credit_ctrl, credit_ref = _credit_account_for_ip(doc)
            gl_lines = [
                {'code': debit_code, 'name': debit_name, 'name_ar': debit_name_ar, 'ctrl': debit_ctrl,
                 'ref': '', 'debit': round(total_payment, 2), 'credit': 0},
                {'code': credit_code, 'name': credit_name, 'name_ar': credit_name_ar, 'ctrl': credit_ctrl,
                 'ref': credit_ref, 'debit': 0, 'credit': round(total_payment, 2)},
            ]
            doc.total_amount = round(total_payment, 2)
            doc.total_vat = 0
            doc.total_amount_including_vat = round(total_payment, 2)

        tot_d = round(sum(g['debit'] for g in gl_lines), 2)
        tot_c = round(sum(g['credit'] for g in gl_lines), 2)
        if abs(tot_d - tot_c) > 0.01:
            return jsonify({'ok': False, 'error': _t(
                'Total Debit must equal Total Credit.', 'يجب أن يتساوى إجمالي المدين مع إجمالي الدائن.')}), 400

        # The GL entry's posting date is always the actual moment of
        # posting -- see outgoing_payment_post()'s identical rationale.
        posting_date = date.today()
        doc.posting_date = posting_date
        narration = (doc.narration or f'Incoming Payment {doc.payment_no}')[:500]

        grl = GRL.query.filter_by(incoming_payment_id=doc.id).first()
        if not grl:
            grl = GRL(incoming_payment_id=doc.id)
            db.session.add(grl)
        if not grl.grl_no:
            grl.grl_no = _next_grl_no()
        grl.origion = doc.payment_no or ''
        grl.posting_date = posting_date
        grl.due_date = None
        grl.document_date = doc.payment_date
        grl.narration = narration
        db.session.flush()

        je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
        if not je:
            je = JournalEntry(je_no=next_je_no(), origin_type='IP', origin_id=doc.id)
            db.session.add(je)
            db.session.flush()
            grl.journal_entry_id = je.id
        je.origion = grl.origion
        je.posting_date = grl.posting_date
        je.due_date = grl.due_date
        je.document_date = grl.document_date
        je.narration = narration

        GRLDetail.query.filter_by(grl_id=grl.id).delete()
        JournalEntryDetail.query.filter_by(journal_entry_id=je.id).delete()
        for gl in gl_lines:
            db.session.add(GRLDetail(
                grl_id=grl.id, code=gl['code'], reference_code=gl['ref'], account_name=gl['name'],
                account_name_ar=gl['name_ar'], control_account=gl['ctrl'], debit=gl['debit'],
                credit=gl['credit'], narration=narration))
            db.session.add(JournalEntryDetail(
                journal_entry_id=je.id, code=gl['code'], reference_code=gl['ref'], account_name=gl['name'],
                control_account=gl['ctrl'], debit=gl['debit'], credit=gl['credit'], narration=narration))

        doc.journal_entry_id = je.id
        doc.grl_id = grl.id
        doc.status = 'Posted'
        doc.updated_by = current_user.id

        db.session.commit()
        return jsonify({'ok': True, 'id': doc.id, 'payment_no': doc.payment_no, 'status': doc.status})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.', 'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# INCOMING PAYMENT -- Cancel (reversing GL entry, never a deletion)
# ══════════════════════════════════════════════════════════════════

@cash_bank_bp.route('/cash-bank/incoming-payments/<int:id>/cancel', methods=['POST'])
@login_required
def incoming_payment_cancel(id):
    """Mirror of outgoing_payment_cancel()."""
    from models import JournalEntry, JournalEntryDetail, GRLDetail, next_je_no, NoActiveFinancialYearError
    try:
        doc = IncomingPayment.query.get_or_404(id)
        if doc.status != 'Posted':
            return jsonify({'ok': False, 'error': _t(
                'Only a Posted payment can be cancelled.', 'يمكن إلغاء الدفعات المرحّلة فقط.')}), 400
        orig_grl = GRL.query.get(doc.grl_id) if doc.grl_id else None
        if not orig_grl:
            return jsonify({'ok': False, 'error': _t(
                'Original ledger entry not found.', 'القيد الأصلي غير موجود.')}), 400
        orig_details = GRLDetail.query.filter_by(grl_id=orig_grl.id).all()
        if not orig_details:
            return jsonify({'ok': False, 'error': _t(
                'Original ledger entry has no detail lines.', 'القيد الأصلي لا يحتوي على تفاصيل.')}), 400

        # Restore each settled document's balance -- row-locked the same
        # way incoming_payment_post() locks it, for the same reason.
        for ln in doc.adjustment_lines:
            source, _outstanding, _dn, _dd, _due = _outstanding_source(ln.document_type, ln.document_id, lock=True)
            if not source:
                continue
            pay_amt = float(ln.payment_amount or 0)
            if ln.document_type in ('Purchase Invoice', 'Sales Invoice'):
                source.paid_amount = max(float(source.paid_amount or 0) - pay_amt, 0)
            elif ln.document_type == 'Payroll Payable':
                source.paid = max(float(source.paid or 0) - pay_amt, 0)
                source.balance = float(source.salary_payable or 0) - float(source.paid or 0)

        narration = f'Reversal of {doc.payment_no}'
        rev_grl = GRL(incoming_payment_id=doc.id, grl_no=_next_grl_no(), origion=narration,
                      posting_date=date.today(), due_date=orig_grl.due_date,
                      document_date=date.today(), narration=narration)
        db.session.add(rev_grl)
        db.session.flush()

        rev_je = JournalEntry(je_no=next_je_no(), origin_type='IP', origin_id=doc.id,
                              origion=narration, posting_date=rev_grl.posting_date,
                              due_date=rev_grl.due_date, document_date=rev_grl.document_date,
                              narration=narration)
        db.session.add(rev_je)
        db.session.flush()
        rev_grl.journal_entry_id = rev_je.id

        for d in orig_details:
            db.session.add(GRLDetail(
                grl_id=rev_grl.id, code=d.code, account_name=d.account_name,
                account_name_ar=d.account_name_ar, control_account=d.control_account,
                debit=d.credit, credit=d.debit, narration=narration))
            db.session.add(JournalEntryDetail(
                journal_entry_id=rev_je.id, code=d.code, account_name=d.account_name,
                control_account=d.control_account, debit=d.credit, credit=d.debit, narration=narration))

        doc.status = 'Cancelled'
        doc.updated_by = current_user.id
        db.session.commit()
        return jsonify({'ok': True, 'status': doc.status})
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.', 'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500
