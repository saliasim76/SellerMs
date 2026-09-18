"""Shared helpers for the lookups sub-blueprints.

Kept in one place so every sub-module (professions, buyers, items, ...) uses the
exact same admin check and translation helper that the original lookups.py had.
"""
import re
from functools import wraps

from flask import request, redirect, url_for, flash, session, jsonify
from flask_login import current_user

# XML 1.0 (and therefore .xlsx) forbids most ASCII control characters --
# openpyxl raises IllegalCharacterError and aborts the WHOLE export the
# moment any single cell anywhere contains one of these, e.g. text pasted
# from Word/PDF/an old system. Every openpyxl-based export in this app
# (Payroll, Journal/Ledger, Chart of Accounts, Employee import templates,
# the generic io_tools.py exporter) should run every cell value through
# xlsx_safe() before writing it, rather than writing raw DB values.
_ILLEGAL_XLSX_CHARS_RE = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def xlsx_safe(v):
    """Strip characters openpyxl/XML can't represent, leaving everything
    else (including tabs/newlines and non-string values) untouched."""
    if isinstance(v, str):
        return _ILLEGAL_XLSX_CHARS_RE.sub('', v)
    return v


def admin_required(f):
    @wraps(f)
    def d(*a, **k):
        if not current_user.is_admin():
            flash('Access denied', 'danger')
            return redirect(request.referrer or url_for('dashboard.index'))
        return f(*a, **k)
    return d


def super_admin_required(f):
    """Gate for platform-level configuration (e.g. the SaaS module/pricing
    catalog) that must stay restricted to the super admin flag, not the
    older 'admin' role used by admin_required."""
    @wraps(f)
    def d(*a, **k):
        if not getattr(current_user, 'is_super_admin', False):
            flash('Access denied', 'danger')
            return redirect(request.referrer or url_for('dashboard.index'))
        return f(*a, **k)
    return d


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


# ── SaaS Basic/Expert plan gate ─────────────────────────────────────
def current_tenant_customer():
    """The saas Customer record for whichever tenant database this
    request's session is currently bound to (see
    database/tenant_routing.py). None outside a tenant session (e.g. the
    platform's own dev/admin database, which has no saas Customer row of
    its own)."""
    tenant_db = session.get('tenant_db')
    if not tenant_db:
        return None
    from models import Customer
    return Customer.query.filter_by(database_name=tenant_db).first()


def current_saas_customer():
    """The saas Customer record for whoever is currently signed in,
    regardless of whether they're a paid tenant (own dedicated database,
    resolved via session['tenant_db'] same as current_tenant_customer())
    or a trial customer (shares the main database, so there's no
    tenant_db session value to key off -- resolved by matching
    current_user's email against Customer.email instead, same lookup
    is_trial_user() uses). None for the platform's own Super Admin, and
    for a standalone (non-SaaS) install with no Customer row at all --
    both cases mean "not subscription-gated", not "gated with nothing
    unlocked"."""
    customer = current_tenant_customer()
    if customer:
        return customer
    try:
        if not current_user or not current_user.is_authenticated:
            return None
        if getattr(current_user, 'is_super_admin', False):
            return None
    except RuntimeError:
        return None
    if session.get('tenant_db'):
        return None  # already handled above; avoids a second lookup
    from models import Customer
    return Customer.query.filter_by(email=current_user.email).first()


def is_basic_mode():
    """True while the current tenant's SaaS plan is 'basic' -- Chart of
    Accounts is fully locked and every Post & Save action is blocked for
    everyone in that tenant, except a Super Admin (current_user.
    effectively_super_admin -- the real platform account, or a tenant's
    own local SuperAdmin), who always has full control regardless of
    plan. Always False outside a tenant session."""
    try:
        if current_user and getattr(current_user, 'effectively_super_admin', False):
            return False
    except RuntimeError:
        pass
    customer = current_tenant_customer()
    return bool(customer and customer.access_mode == 'basic')


def block_in_basic_mode_json(f):
    """Blocks a POST & Save (or other JSON) route entirely while the
    current tenant's plan is 'basic' -- apply to every *_post_and_save
    route (GRN/PINV/PRN/PDM and their Sales equivalents), the only actions
    that mutate the GL/stock."""
    @wraps(f)
    def d(*a, **k):
        if is_basic_mode():
            return jsonify({'ok': False, 'error': _t(
                'Posting is locked on the Basic plan. Upgrade to Expert to unlock it.',
                'الترحيل مقفل في الخطة الأساسية. قم بالترقية إلى خطة الخبير لتفعيله.')}), 403
        return f(*a, **k)
    return d


# ── SaaS trial-user shared-data write gate ──────────────────────────
def is_trial_user():
    """True when the signed-in user is logged into a SaaS trial
    customer's own account. Trial customers share SellerMs's single
    database (see tenant_provisioning.py's docstring on why every paid
    customer instead gets their own dedicated one), so unlike a paid
    tenant, letting a trial user add or delete platform-wide reference
    data -- Chart of Accounts, Tax Codes, Auto Code Selection -- would
    affect every other trial customer and the platform's own shared
    defaults, not just their own account. Always False for the
    platform's own Super Admin, and for a paid tenant's own users
    (session['tenant_db'] already means their own isolated database, so
    there is no shared-data risk to guard against there)."""
    try:
        if not current_user or not current_user.is_authenticated:
            return False
        if getattr(current_user, 'is_super_admin', False):
            return False
    except RuntimeError:
        return False
    if session.get('tenant_db'):
        return False
    from models import Customer
    customer = Customer.query.filter_by(email=current_user.email).first()
    return bool(customer and not customer.database_name)


def block_trial_write(f):
    """Blocks a SaaS trial customer's own login from adding/deleting
    shared reference data through a flash+redirect style route (Chart of
    Accounts) -- see is_trial_user() above. Editing an existing row is
    deliberately left alone; only add/delete are gated."""
    @wraps(f)
    def d(*a, **k):
        if is_trial_user():
            flash(_t(
                'Trial accounts cannot add or delete this shared reference data -- '
                'it is shared with every other trial customer. Upgrade to a paid '
                'plan for your own dedicated database.',
                'لا يمكن للحسابات التجريبية إضافة أو حذف هذه البيانات المرجعية المشتركة '
                '-- فهي مشتركة مع كل عميل تجريبي آخر. قم بالترقية إلى خطة مدفوعة '
                'للحصول على قاعدة بياناتك الخاصة.'), 'warning')
            return redirect(request.referrer or url_for('dashboard.index'))
        return f(*a, **k)
    return d


def block_trial_write_json(f):
    """JSON-response counterpart of block_trial_write(), for routes that
    reply with jsonify() instead of flash+redirect (Purchase/Sales Tax
    Codes, Auto Code Selection add/delete)."""
    @wraps(f)
    def d(*a, **k):
        if is_trial_user():
            return jsonify({'ok': False, 'error': _t(
                'Trial accounts cannot add or delete this shared reference data -- '
                'it is shared with every other trial customer. Upgrade to a paid '
                'plan for your own dedicated database.',
                'لا يمكن للحسابات التجريبية إضافة أو حذف هذه البيانات المرجعية المشتركة '
                '-- فهي مشتركة مع كل عميل تجريبي آخر. قم بالترقية إلى خطة مدفوعة '
                'للحصول على قاعدة بياناتك الخاصة.')}), 403
        return f(*a, **k)
    return d


def resolve_purchase_type(kind, requested_type):
    """Purchase Type is a free choice (Assets or Expense) regardless of
    Kind -- Services is no longer forced to Expense. Kept as a shared
    helper (rather than inlining `f.get('purchase_type') or None` at each
    call site) so this business rule stays a single edit point if it
    changes again."""
    requested_type = (requested_type or '').strip()
    return requested_type or None


# ── GRL (general ledger row per postable document) helpers ─────────
# Moved here from purchase.py (originally Purchase-only) so both
# purchase.py and sales.py can share ONE correctly-incrementing GRL
# numbering sequence (GRL-<FY>-<n>) -- GRL is process-wide, not
# module-scoped, so this numbering logic must not be duplicated per
# module (that would risk two modules generating colliding numbers).
def _next_grl_no():
    """Auto GRL number: GRL-<FY year>-<n>."""
    from datetime import date
    from models import db, GRL, active_fy_year
    year = active_fy_year() or date.today().year
    like = f'GRL-{year}-%'
    max_num = 0
    for g in db.session.query(GRL).filter(GRL.grl_no.like(like)).all():
        if g.grl_no:
            try:
                num = int(g.grl_no.rsplit('-', 1)[1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue
    n = max_num + 1
    grl_no = f'GRL-{year}-{n}'
    retries = 0
    while GRL.query.filter_by(grl_no=grl_no).first() and retries < 100:
        n += 1
        grl_no = f'GRL-{year}-{n}'
        retries += 1
    return grl_no


def _get_auto_code(module_code, form_code):
    """The AutoCodeSelection row configured for one (module, form) pair --
    shared across every module that posts to GL via a fixed, admin-
    configurable Chart-of-Accounts code (Cash & Bank's Outgoing/Incoming
    Payment, Payroll's Salary Expense/Payable, ...), so this lookup and its
    join shape are never duplicated per module."""
    from models import AutoCodeSelection, Module, SystemForm
    return (AutoCodeSelection.query
            .join(Module, AutoCodeSelection.module_id == Module.id)
            .join(SystemForm, AutoCodeSelection.form_id == SystemForm.id)
            .filter(Module.code == module_code, SystemForm.code == form_code)
            .first())


def _grl_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _grl_lines_from(lines, form_code='goods_receipt_note', module_code='purchase', kind='Goods'):
    """Compute the two-record GRL preview for a list of line items (each
    with .item_code and .total -- works for GoodsReceiptLineItem,
    PurchaseOrderLineItem, PurchaseInvoiceLineItem, PurchaseReturnNoteLineItem,
    and their Sales-side equivalents, since they all share that same shape).

    Per source line item, this returns ONE dict describing the pair of GRL
    records that line posts (the caller renders both):
      code             = ItemMaster.levelfive_code       (its account code)
      account_name     = ItemMaster.levelfive_drawer_en  (its drawer)
      control_account  = LevelFive.control_account        (Yes/No, via that code)
      debit            = line total
      credit           = 0
    The second (offsetting) record's code/name/nature/control_account1 come
    from the Auto Code Selection mapping configured for the relevant module +
    form (Chart of Accounts -> Auto Code Selection, keyed by `module_code` +
    `form_code`), not from the item.

    `kind` mirrors the parent document's own Kind field (Goods/Services --
    every form this is called for carries one). Kind = Goods is the case
    described above, completely unchanged. Kind = Services means this
    document's own line-item dropdowns (see e.g. pr_list.html's dual
    Item Code/Item Name mechanism) let the user pick a Level Five account
    directly instead of an Item Master item, so `ln.item_code` IS ALREADY
    that account's own code -- record 1 uses it as-is (no ItemMaster
    indirection), with `ln.description` (the line's own stored item/account
    name) as a fallback account name if the code doesn't resolve to a real
    Level Five row for some reason.
    """
    from models import ItemMaster, LevelFive, AutoCodeSelection, Module, SystemForm

    item_codes = [ln.item_code for ln in lines if ln.item_code]
    items = {}
    l5_by_code = {}
    if item_codes:
        if kind == 'Services':
            for row in LevelFive.query.filter(LevelFive.code.in_(item_codes)).all():
                l5_by_code[row.code] = row
        else:
            for it in ItemMaster.query.filter(ItemMaster.item_code.in_(item_codes)).all():
                items[it.item_code] = it
    l5_codes = {getattr(it, 'levelfive_code', None) for it in items.values()}
    l5_codes.discard(None)
    l5_ctrl = {}
    if l5_codes:
        for row in LevelFive.query.filter(LevelFive.code.in_(list(l5_codes))).all():
            l5_ctrl[row.code] = row.control_account or 'No'

    acs = (AutoCodeSelection.query
           .join(Module, AutoCodeSelection.module_id == Module.id)
           .join(SystemForm, AutoCodeSelection.form_id == SystemForm.id)
           .filter(Module.code == module_code, SystemForm.code == form_code)
           .first())
    acs_code      = acs.levelfive_code if acs else ''
    acs_name      = acs.levelfive_drawer_en if acs else ''
    acs_name_ar   = acs.levelfive_drawer_ar if acs else ''
    acs_nature    = acs.nature if acs else 'Credit'
    acs_ctrl_row  = LevelFive.query.filter_by(code=acs_code).first() if acs_code else None
    acs_ctrl      = (acs_ctrl_row.control_account or 'No') if acs_ctrl_row else 'No'

    # Record 1 (the item's own account) posts as a Debit by default -- correct
    # for every other Purchase-module form. Delivery Note, Sales Return
    # Note, Sales Credit Memo, and Purchase Invoice are the exceptions:
    # their record 1 posts as a Credit instead (record 2 -- the Auto Code
    # Selection account -- must then be configured with the opposite
    # nature so the pair still balances). Purchase Invoice's own Auto Code
    # Selection nature is configured as Debit in this installation, so its
    # record 1 must be the opposite (Credit) to ever balance -- matches
    # _apply_supplier_control_account()'s own side='credit' default, which
    # already assumed this same direction for the supplier-override case.
    r1_credit = form_code in ('delivery_note', 'sales_return_note', 'sales_credit_memo', 'purchase_invoice')

    out_lines = []
    for ln in lines:
        if kind == 'Services':
            l5row = l5_by_code.get(ln.item_code)
            l5code = ln.item_code or ''
            drawer = l5row.drawers if l5row else (getattr(ln, 'description', '') or '')
            drawer_ar = l5row.drawers_ar if l5row else ''
            ctrl = (l5row.control_account or 'No') if l5row else ''
        else:
            it = items.get(ln.item_code)
            l5code = getattr(it, 'levelfive_code', '') if it else ''
            drawer = getattr(it, 'levelfive_drawer_en', '') if it else ''
            drawer_ar = getattr(it, 'levelfive_drawer_ar', '') if it else ''
            ctrl   = l5_ctrl.get(l5code, '') if l5code else ''
        total = float(ln.total or 0)
        out_lines.append({
            'code': l5code or '',
            # A vendor/customer reference number (e.g. the supplier's own
            # invoice/subsidiary code) is never a Chart-of-Accounts code and
            # must never be written into `code`/`control_account` -- callers
            # that have one (see purchase.py's _apply_supplier_control_account)
            # set this instead. control_account stays Yes/No, always.
            'reference_code': '',
            'account_name': drawer or '',
            'account_name_ar': drawer_ar or '',
            'code1': acs_code,
            'account_name1': acs_name,
            'account_name_ar1': acs_name_ar,
            'nature1': acs_nature,
            'control_account': ctrl or '',
            'control_account1': acs_ctrl,
            'debit': 0 if r1_credit else total,
            'credit': total if r1_credit else 0,
        })
    return out_lines


# ── Origin-document resolution, shared by GRL/Journal Entry delete guards ──
# origin_type (JournalEntry.origin_type, and by extension which GRL FK
# column is set) -> (model class name, that model's primary-key attribute).
ORIGIN_MODEL_MAP = {
    'GRN':  ('GoodsReceiptNote', 'goods_receipt_note_id'),
    'PINV': ('PurchaseInvoice', 'purchase_invoice_id'),
    'PRN':  ('PurchaseReturnNote', 'purchase_good_return_note_id'),
    'PDM':  ('PurchaseDebitMemo', 'purchase_debit_memo_id'),
    'DN':   ('DeliveryNote', 'delivery_note_id'),
    'SINV': ('SalesInvoice', 'sales_invoice_id'),
    'SRN':  ('SalesReturnNote', 'sales_return_note_id'),
    'SCM':  ('SalesCreditMemo', 'sales_credit_memo_id'),
    'OP':   ('OutgoingPayment', 'id'),
    'IP':   ('IncomingPayment', 'id'),
    'OB':   ('OpeningBalance', 'id'),
    # Payroll's "id" here is really its string payroll_id batch key (e.g.
    # "PR-3"), stored in JournalEntry.origion rather than the Integer
    # origin_id column since a batch has no single row with its own PK --
    # see journal_data()'s doc_key fallback (origin_id or origion).
    'PR':   ('SalaryConsolidation', 'payroll_id'),
}

# origin_type -> that document's own dedicated view page (a '{id}' placeholder
# for its primary key, resolved from JournalEntry.origin_id, or origion for
# the string-keyed types above). Outgoing Payment has no separate view page,
# only Print, so it links there instead.
ORIGIN_VIEW_URL = {
    'GRN':  '/purchase/grn/{id}/view',
    'PINV': '/purchase/invoices/{id}/view',
    'PRN':  '/purchase/return-notes/{id}/view',
    'PDM':  '/purchase/debit-memos/{id}/view',
    'DN':   '/sales/delivery/{id}/view',
    'SINV': '/sales/invoices/{id}/view',
    'SRN':  '/sales/return-notes/{id}/view',
    'SCM':  '/sales/credit-memos/{id}/view',
    'OP':   '/cash-bank/outgoing-payments/{id}/print',
    'IP':   '/cash-bank/incoming-payments/{id}/print',
    'OB':   '/coa/opening-balance/{id}/view',
    'PR':   '/payroll/?payroll_id={id}',
}

# GRL has one direct FK column per document type (exactly one is ever set on
# a given row) -- maps each column name to the same (model, pk) pairs above,
# so a GRL row's source document can be resolved without going through its
# linked Journal Entry.
GRL_FK_ORIGIN_MAP = {
    'goods_receipt_note_id':  ORIGIN_MODEL_MAP['GRN'],
    'purchase_invoice_id':    ORIGIN_MODEL_MAP['PINV'],
    'purchase_return_note_id': ORIGIN_MODEL_MAP['PRN'],
    'purchase_debit_memo_id': ORIGIN_MODEL_MAP['PDM'],
    'delivery_note_id':       ORIGIN_MODEL_MAP['DN'],
    'sales_invoice_id':       ORIGIN_MODEL_MAP['SINV'],
    'sales_return_note_id':   ORIGIN_MODEL_MAP['SRN'],
    'sales_credit_memo_id':   ORIGIN_MODEL_MAP['SCM'],
    'outgoing_payment_id':    ORIGIN_MODEL_MAP['OP'],
    'incoming_payment_id':    ORIGIN_MODEL_MAP['IP'],
    'opening_balance_id':     ORIGIN_MODEL_MAP['OB'],
    'payroll_id':             ORIGIN_MODEL_MAP['PR'],
}


def origin_document_exists(model_name, pk_attr, doc_id):
    """True if the source document (e.g. the GRN a GRL/Journal Entry was
    posted from) still exists. Used to block deleting a ledger entry while
    its Posted source document is still around -- the correct way to remove
    one is to delete/unpost the source document itself, which already
    reverses everything correctly; this list is only for genuinely orphaned
    entries."""
    if not doc_id:
        return False
    import models as _models
    from models import db
    model = getattr(_models, model_name, None)
    if not model:
        return False
    return db.session.query(
        model.query.filter(getattr(model, pk_attr) == doc_id).exists()
    ).scalar()