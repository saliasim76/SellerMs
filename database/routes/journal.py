"""
Journal Entry — VIEW ONLY (master/detail).

The Journal page only displays entries; there is no manual add/edit form.
Entries are created programmatically from a source document (to be wired
later) via `create_journal_entry(...)`, which assigns the JEV-<FY>-<n>
number and enforces the active-financial-year rule.

    journal_entries(id, je_no, origion, origin_type, origin_id, refrence,
                    posting_date, due_date, document_date, narration, ...)
    journal_entry_detail(id, journal_entry_id FK, code, account_name,
                         control_account, debit, credit, narration)
"""

import io
from datetime import date, datetime

from flask import Blueprint, render_template, request, jsonify, session, send_file
from flask_login import login_required, current_user
from sqlalchemy import func

from models import (db, JournalEntry, JournalEntryDetail,
                    next_je_no, NoActiveFinancialYearError,
                    SupplierMaster, BuyerMaster, Employee, LevelFive, LevelOne, Owner)

journal_bp = Blueprint('journal', __name__, url_prefix='/journal')


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ── Page (view only) ────────────────────────────────────────────
@journal_bp.route('/')
@login_required
def journal_list():
    return render_template('journal/list.html')


@journal_bp.route('/data')
@login_required
def journal_data():
    """One grid ROW per Journal Entry detail LINE (not one row per JE header)
    -- a Journal Entry can carry more than one detail-line pair (one per
    source document line item) since each posts its own Code/Control
    Account/Debit/Credit, and joining them into one cell hid that. Header
    fields (je_no, origion, posting_date, ...) are repeated on every line
    belonging to that header; `id` stays the JE header id (so the View
    button still opens the full breakdown) while `detail_id` identifies
    this specific line."""
    from database.routes.shared import ORIGIN_MODEL_MAP, ORIGIN_VIEW_URL, origin_document_exists
    rows = JournalEntry.query.order_by(JournalEntry.id.desc()).all()
    out = []
    for j in rows:
        header = j.to_dict()
        details = header.pop('details', [])
        origin_type = (j.origin_type or '').strip()
        mapping = ORIGIN_MODEL_MAP.get(origin_type)
        # origin_id is an Integer column, but Payroll's origin is a string
        # batch key (e.g. "PR-3") stored in origion instead, since a batch
        # has no single row to give it a real Integer PK -- falling back to
        # origion here lets that case resolve through the exact same
        # existence-check/URL-template machinery as every Integer-keyed
        # document type above.
        doc_key = j.origin_id or j.origion
        has_child = bool(mapping and origin_document_exists(mapping[0], mapping[1], doc_key))
        header['has_child'] = has_child
        # Only offer a link to the source document while it still exists --
        # has_child already proved that above, so reuse it rather than
        # re-checking doc_key truthiness separately.
        url_template = ORIGIN_VIEW_URL.get(origin_type)
        header['origin_url'] = url_template.format(id=doc_key) if (has_child and url_template) else ''
        if not details:
            row = dict(header)
            row.update({'detail_id': None, 'code': '', 'reference_code': '', 'account_name': '',
                        'control_account': '', 'debit': 0, 'credit': 0, 'line_narration': ''})
            out.append(row)
            continue
        for det in details:
            row = dict(header)
            row.update({
                'detail_id': det['id'],
                'code': det['code'],
                'reference_code': det.get('reference_code', ''),
                'account_name': det['account_name'],
                'control_account': det['control_account'],
                'debit': det['debit'],
                'credit': det['credit'],
                'line_narration': det['narration'],
            })
            out.append(row)
    return jsonify(out)


@journal_bp.route('/<int:je_id>/json')
@login_required
def journal_json(je_id):
    je = JournalEntry.query.get_or_404(je_id)
    return jsonify(je.to_dict())


# ── Programmatic creation (used by source documents later) ──────
def create_journal_entry(origion='', refrence='', posting_date=None,
                         due_date=None, narration='', lines=None,
                         origin_type='', origin_id=None):
    """Create a Journal Entry (+ detail lines) with an auto JEV number.

    lines: list of dicts with keys code, account_name, control_account,
           debit, credit, narration.
    Raises NoActiveFinancialYearError if no financial year is Open.
    Caller is responsible for committing (or use commit=True path below).
    """
    je = JournalEntry(
        je_no=next_je_no(),                       # enforces active FY
        origion=(origion or '').strip(),
        origin_type=(origin_type or '').strip(),
        origin_id=origin_id,
        refrence=(refrence or '').strip(),
        posting_date=posting_date or date.today(),
        due_date=due_date,
        document_date=date.today(),               # always today
        narration=(narration or '').strip(),
    )
    db.session.add(je)
    db.session.flush()
    for ln in (lines or []):
        db.session.add(JournalEntryDetail(
            journal_entry_id=je.id,
            code=(ln.get('code') or '').strip(),
            account_name=(ln.get('account_name') or '').strip(),
            control_account=(ln.get('control_account') or '').strip(),
            debit=_num(ln.get('debit')),
            credit=_num(ln.get('credit')),
            narration=(ln.get('narration') or '').strip(),
        ))
    return je


@journal_bp.route('/<int:je_id>/delete', methods=['POST'])
@login_required
def journal_delete(je_id):
    """Blocked while this entry's source document (the GRN/Purchase
    Invoice/... it was posted from) still exists -- the correct way to
    remove a Journal Entry is to delete/unpost that document itself, which
    already reverses everything correctly. This route is only for
    genuinely orphaned entries."""
    from database.routes.shared import ORIGIN_MODEL_MAP, origin_document_exists
    je = JournalEntry.query.get_or_404(je_id)
    mapping = ORIGIN_MODEL_MAP.get((je.origin_type or '').strip())
    if mapping and origin_document_exists(mapping[0], mapping[1], je.origin_id):
        return jsonify({'ok': False, 'error': _t(
            'Child record found: the source document for this entry still exists. Delete or unpost it first.',
            'تم العثور على سجل فرعي: لا يزال المستند المصدر لهذا القيد موجودًا. احذفه أو ألغِ ترحيله أولاً.')}), 400
    try:
        db.session.delete(je)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@journal_bp.errorhandler(NoActiveFinancialYearError)
def _no_fy(e):
    return jsonify({'ok': False, 'error': _t(
        'Please activate your financial year first.',
        'الرجاء تفعيل السنة المالية أولاً.')}), 400


# ═════════════════════════════════════════════════════════════════
#  LEDGER SYSTEM — a calculated/reporting view over the existing
#  journal_entries / journal_entry_detail tables (no duplicate storage).
#
#  Every row in these two tables already represents a POSTED transaction:
#  a plain Save on any document never writes here, only an explicit
#  Post & Save does (see journal_bp module docstring / coa_balance_data
#  for the same reasoning) -- so "Posted only" is satisfied by
#  construction and there is no separate status column to filter on.
#
#  Buyer/Supplier/Employee filters match against
#  JournalEntryDetail.reference_code, the same column the Supplier
#  Account report and the Purchase/Sales control-account override already
#  populate with the party's own code (supplier_code/buyer_code). No
#  Cost Center / Department concept exists anywhere in this schema, so
#  those two spec filters have no real data to back them and are
#  intentionally left out rather than shipped as non-functional stubs.
# ═════════════════════════════════════════════════════════════════

def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        return None


def _resolve_reference_code():
    """Buyer/Supplier/Employee filter -> the code stored in reference_code.

    Returns (code_or_None, filter_was_requested). When a filter was
    requested but the chosen party has no code (or no longer exists), the
    caller must treat the ledger as empty rather than silently ignoring
    the filter.
    """
    supplier_id = request.args.get('supplier_id', type=int)
    if supplier_id:
        s = SupplierMaster.query.get(supplier_id)
        return (s.supplier_code if s else None), True
    buyer_id = request.args.get('buyer_id', type=int)
    if buyer_id:
        b = BuyerMaster.query.get(buyer_id)
        return (b.buyer_code if b else None), True
    employee_id = request.args.get('employee_id', type=int)
    if employee_id:
        e = Employee.query.get(employee_id)
        return (e.employee_code if e else None), True
    return None, False


def _ledger_filters(account_code, reference_code, document_type, document_no):
    filters = [JournalEntryDetail.code == account_code]
    if reference_code:
        filters.append(JournalEntryDetail.reference_code == reference_code)
    if document_type:
        filters.append(JournalEntry.origin_type == document_type)
    if document_no:
        filters.append(JournalEntry.origion.ilike(f'%{document_no}%'))
    return filters


def _ledger_opening_balance(account_code, reference_code, document_type, document_no, from_date):
    """Opening Balance = total Debit - total Credit of every matching
    posted line BEFORE From Date. Zero when From Date is not set."""
    if not from_date:
        return 0.0
    filters = _ledger_filters(account_code, reference_code, document_type, document_no)
    filters.append(JournalEntry.posting_date < from_date)
    row = (db.session.query(func.coalesce(func.sum(JournalEntryDetail.debit), 0),
                            func.coalesce(func.sum(JournalEntryDetail.credit), 0))
           .join(JournalEntry, JournalEntryDetail.journal_entry_id == JournalEntry.id)
           .filter(*filters).first())
    total_debit = float(row[0] or 0) if row else 0.0
    total_credit = float(row[1] or 0) if row else 0.0
    return round(total_debit - total_credit, 2)


def _ledger_period_subquery(account_code, reference_code, document_type, document_no,
                            from_date, to_date, posting_date_exact):
    """One row per matching journal_entry_detail line inside [From Date, To
    Date] (inclusive both ends), each carrying its own running cumulative
    Debit-Credit total computed by a MySQL window function ordered exactly
    as required: Posting Date, then Journal Entry id, then Journal Entry
    Line id -- so ties on the same posting date preserve original posting
    sequence instead of being reordered by document number or anything
    else. Every column in this ORDER BY is part of a primary key further
    down the tuple, so no two rows can ever tie across the full order,
    which keeps this correct regardless of MySQL's RANGE-vs-ROWS window
    frame default."""
    filters = _ledger_filters(account_code, reference_code, document_type, document_no)
    if from_date:
        filters.append(JournalEntry.posting_date >= from_date)
    if to_date:
        filters.append(JournalEntry.posting_date <= to_date)
    if posting_date_exact:
        filters.append(JournalEntry.posting_date == posting_date_exact)

    order_cols = [JournalEntry.posting_date.asc(), JournalEntry.id.asc(), JournalEntryDetail.id.asc()]
    cum_expr = func.sum(JournalEntryDetail.debit - JournalEntryDetail.credit).over(
        order_by=order_cols).label('cum')

    return (db.session.query(
                JournalEntryDetail.id.label('detail_id'),
                JournalEntry.id.label('je_id'),
                JournalEntry.je_no.label('je_no'),
                JournalEntry.origion.label('origion'),
                JournalEntry.origin_type.label('origin_type'),
                JournalEntry.posting_date.label('posting_date'),
                JournalEntry.refrence.label('refrence'),
                JournalEntry.narration.label('je_narration'),
                JournalEntryDetail.reference_code.label('reference_code'),
                JournalEntryDetail.narration.label('line_narration'),
                JournalEntryDetail.debit.label('debit'),
                JournalEntryDetail.credit.label('credit'),
                cum_expr)
            .join(JournalEntry, JournalEntryDetail.journal_entry_id == JournalEntry.id)
            .filter(*filters)
            .subquery())


def _ledger_request_params():
    account_code = (request.args.get('account') or '').strip()
    from_date = _parse_date(request.args.get('from_date'))
    to_date = _parse_date(request.args.get('to_date'))
    posting_date_exact = _parse_date(request.args.get('posting_date'))
    document_type = (request.args.get('document_type') or '').strip()
    document_no = (request.args.get('document_no') or '').strip()
    reference_code, party_filter_requested = _resolve_reference_code()
    return {
        'account_code': account_code, 'from_date': from_date, 'to_date': to_date,
        'posting_date_exact': posting_date_exact, 'document_type': document_type,
        'document_no': document_no, 'reference_code': reference_code,
        'party_filter_requested': party_filter_requested,
    }


def _ledger_compute(p):
    """Returns (opening_balance, period_debit, period_credit, closing_balance,
    ordered_rows) where ordered_rows are plain result rows (not yet
    paginated) each carrying a `.cum` running total relative to
    opening_balance. Caller adds opening_balance to `.cum` for the
    displayed Balance."""
    if p['party_filter_requested'] and not p['reference_code']:
        # The selected Buyer/Supplier/Employee has no code to match against
        # reference_code (or no longer exists) -- guaranteed-empty ledger,
        # never a silently-ignored filter.
        return 0.0, 0.0, 0.0, 0.0, []

    opening_balance = _ledger_opening_balance(
        p['account_code'], p['reference_code'], p['document_type'], p['document_no'], p['from_date'])
    subq = _ledger_period_subquery(
        p['account_code'], p['reference_code'], p['document_type'], p['document_no'],
        p['from_date'], p['to_date'], p['posting_date_exact'])
    rows = (db.session.query(subq)
            .order_by(subq.c.posting_date.asc(), subq.c.je_id.asc(), subq.c.detail_id.asc())
            .all())
    tot = db.session.query(func.coalesce(func.sum(subq.c.debit), 0),
                           func.coalesce(func.sum(subq.c.credit), 0)).select_from(subq).first()
    period_debit = round(float(tot[0] or 0), 2)
    period_credit = round(float(tot[1] or 0), 2)
    closing_balance = round(opening_balance + period_debit - period_credit, 2)
    return opening_balance, period_debit, period_credit, closing_balance, rows


def _fmt_amount(v, blank_if_zero=False):
    v = round(v or 0, 2)
    if blank_if_zero and abs(v) < 0.005:
        return ''
    if v < 0:
        return f'({abs(v):,.2f})'
    return f'{v:,.2f}'


@journal_bp.route('/ledger')
@login_required
def ledger_list():
    return render_template('journal/ledger.html')


@journal_bp.route('/ledger/accounts')
@login_required
def ledger_accounts():
    l1_by_code = {r.code: r for r in LevelOne.query.all()}
    rows = LevelFive.query.filter(LevelFive.status == 'active').order_by(LevelFive.code).all()
    out = []
    for r in rows:
        nature_code = (r.code or '')[:1]
        l1 = l1_by_code.get(nature_code)
        out.append({
            'code': r.code, 'name': r.drawers or '', 'name_ar': r.drawers_ar or '',
            'control_account': r.control_account or 'No',
            'account_type_code': nature_code,
            'account_type_name': l1.drawers if l1 else '',
        })
    return jsonify(out)


@journal_bp.route('/ledger/account-types')
@login_required
def ledger_account_types():
    rows = LevelOne.query.order_by(LevelOne.code).all()
    return jsonify([{'code': r.code, 'name': r.drawers} for r in rows])


def _ledger_scope_filters(document_type_filter=False):
    """Account + party scope shared by the Document Type / Document No.
    dropdown sources, so each cascades from whatever the user already
    picked instead of always listing every value in the system. Returns
    None when a Buyer/Supplier/Employee was chosen but no longer resolves
    to a code -- the caller must then return an empty list rather than
    silently showing the unfiltered set."""
    account_code = (request.args.get('account') or '').strip()
    reference_code, party_filter_requested = _resolve_reference_code()
    if party_filter_requested and not reference_code:
        return None
    filters = []
    if account_code:
        filters.append(JournalEntryDetail.code == account_code)
    if reference_code:
        filters.append(JournalEntryDetail.reference_code == reference_code)
    if document_type_filter:
        document_type = (request.args.get('document_type') or '').strip()
        if document_type:
            filters.append(JournalEntry.origin_type == document_type)
    return filters


@journal_bp.route('/ledger/document-types')
@login_required
def ledger_document_types():
    filters = _ledger_scope_filters()
    if filters is None:
        return jsonify([])
    filters += [JournalEntry.origin_type.isnot(None), JournalEntry.origin_type != '']
    rows = (db.session.query(JournalEntry.origin_type)
            .join(JournalEntryDetail, JournalEntryDetail.journal_entry_id == JournalEntry.id)
            .filter(*filters).distinct().order_by(JournalEntry.origin_type).all())
    return jsonify([r[0] for r in rows])


@journal_bp.route('/ledger/document-numbers')
@login_required
def ledger_document_numbers():
    filters = _ledger_scope_filters(document_type_filter=True)
    if filters is None:
        return jsonify([])
    filters += [JournalEntry.origion.isnot(None), JournalEntry.origion != '']
    rows = (db.session.query(JournalEntry.origion)
            .join(JournalEntryDetail, JournalEntryDetail.journal_entry_id == JournalEntry.id)
            .filter(*filters).distinct().order_by(JournalEntry.origion).all())
    return jsonify([r[0] for r in rows])


@journal_bp.route('/ledger/data')
@login_required
def ledger_data():
    p = _ledger_request_params()
    if not p['account_code']:
        return jsonify({'ok': False, 'error': _t(
            'Please select an Account first.', 'الرجاء اختيار الحساب أولاً.')}), 400
    account = LevelFive.query.filter_by(code=p['account_code']).first()
    if not account:
        return jsonify({'ok': False, 'error': _t('Account not found.', 'الحساب غير موجود.')}), 404

    opening_balance, period_debit, period_credit, closing_balance, rows = _ledger_compute(p)

    page = max(1, request.args.get('page', 1, type=int))
    page_size = min(1000, max(1, request.args.get('page_size', 100, type=int)))
    total_count = len(rows)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = min(page, total_pages)
    page_rows = rows[(page - 1) * page_size: page * page_size]

    rows_out = []
    for r in page_rows:
        rows_out.append({
            'detail_id': r.detail_id, 'je_id': r.je_id, 'je_no': r.je_no or '',
            'posting_date': r.posting_date.isoformat() if r.posting_date else '',
            'document_type': r.origin_type or '', 'document_no': r.origion or '',
            'reference_code': r.reference_code or '',
            'description': (r.line_narration or r.je_narration or r.refrence or ''),
            'debit': float(r.debit or 0), 'credit': float(r.credit or 0),
            'balance': round(opening_balance + float(r.cum or 0), 2),
        })

    last_running = round(opening_balance + float(rows[-1].cum or 0), 2) if rows else opening_balance
    integrity_ok = abs(last_running - closing_balance) < 0.01

    return jsonify({
        'ok': True,
        'account': {'code': account.code, 'name': account.drawers, 'name_ar': account.drawers_ar or ''},
        'from_date': p['from_date'].isoformat() if p['from_date'] else '',
        'to_date': p['to_date'].isoformat() if p['to_date'] else '',
        'opening_balance': opening_balance,
        'total_debit': period_debit,
        'total_credit': period_credit,
        'closing_balance': closing_balance,
        'total_count': total_count,
        'page': page, 'page_size': page_size, 'total_pages': total_pages,
        'show_opening_row': page == 1,
        'integrity_ok': integrity_ok,
        'rows': rows_out,
    })


@journal_bp.route('/ledger/export/excel')
@login_required
def ledger_export_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from database.routes.shared import xlsx_safe

    p = _ledger_request_params()
    if not p['account_code']:
        return jsonify({'ok': False, 'error': _t(
            'Please select an Account first.', 'الرجاء اختيار الحساب أولاً.')}), 400
    account = LevelFive.query.filter_by(code=p['account_code']).first()
    if not account:
        return jsonify({'ok': False, 'error': _t('Account not found.', 'الحساب غير موجود.')}), 404

    opening_balance, period_debit, period_credit, closing_balance, rows = _ledger_compute(p)

    wb = Workbook()
    ws = wb.active
    ws.title = 'Ledger'
    ws.append([f'Account: {account.code} - {xlsx_safe(account.drawers) or ""}'])
    ws.append([f'From: {p["from_date"].isoformat() if p["from_date"] else "—"}   '
               f'To: {p["to_date"].isoformat() if p["to_date"] else "—"}'])
    ws.append([])

    headers = ['Date', 'Document Type', 'Document No', 'Description', 'Debit', 'Credit', 'Balance']
    hdr_row = ws.max_row + 1
    hdr_fill = PatternFill('solid', fgColor='1E3A5F')
    hdr_font = Font(color='FFFFFF', bold=True, size=10)
    for i, h in enumerate(headers, 1):
        cell = ws.cell(row=hdr_row, column=i, value=h)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal='center')
    widths = [12, 16, 16, 40, 14, 14, 14]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = f'A{hdr_row + 1}'

    money_fmt = '#,##0.00;(#,##0.00)'
    r = hdr_row + 1
    bold = Font(bold=True)
    ws.cell(row=r, column=4, value='Opening Balance').font = bold
    ws.cell(row=r, column=7, value=opening_balance).font = bold
    ws.cell(row=r, column=7).number_format = money_fmt
    r += 1
    for row in rows:
        debit = float(row.debit or 0)
        credit = float(row.credit or 0)
        ws.cell(row=r, column=1, value=row.posting_date.isoformat() if row.posting_date else '')
        ws.cell(row=r, column=2, value=xlsx_safe(row.origin_type) or '')
        ws.cell(row=r, column=3, value=xlsx_safe(row.origion) or '')
        ws.cell(row=r, column=4, value=xlsx_safe(row.line_narration or row.je_narration or row.refrence or ''))
        if debit:
            ws.cell(row=r, column=5, value=debit).number_format = money_fmt
        if credit:
            ws.cell(row=r, column=6, value=credit).number_format = money_fmt
        bal_cell = ws.cell(row=r, column=7, value=round(opening_balance + float(row.cum or 0), 2))
        bal_cell.number_format = money_fmt
        r += 1

    ws.cell(row=r, column=4, value='Total / Closing Balance').font = bold
    for col, val in ((5, period_debit), (6, period_credit), (7, closing_balance)):
        c = ws.cell(row=r, column=col, value=val)
        c.font = bold
        c.number_format = money_fmt

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f'ledger_{account.code}.xlsx'
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@journal_bp.route('/ledger/print')
@login_required
def ledger_print():
    p = _ledger_request_params()
    if not p['account_code']:
        return _t('Please select an Account first.', 'الرجاء اختيار الحساب أولاً.'), 400
    account = LevelFive.query.filter_by(code=p['account_code']).first()
    if not account:
        return _t('Account not found.', 'الحساب غير موجود.'), 404

    opening_balance, period_debit, period_credit, closing_balance, raw_rows = _ledger_compute(p)

    rows = []
    for row in raw_rows:
        debit = float(row.debit or 0)
        credit = float(row.credit or 0)
        balance = round(opening_balance + float(row.cum or 0), 2)
        rows.append({
            'posting_date': row.posting_date.strftime('%d-%b-%y') if row.posting_date else '',
            'document_type': row.origin_type or '', 'document_no': row.origion or '',
            'description': (row.line_narration or row.je_narration or row.refrence or ''),
            'debit_display': _fmt_amount(debit, blank_if_zero=True),
            'credit_display': _fmt_amount(credit, blank_if_zero=True),
            'balance_display': _fmt_amount(balance),
        })

    last_running = round(opening_balance + float(raw_rows[-1].cum or 0), 2) if raw_rows else opening_balance
    integrity_ok = abs(last_running - closing_balance) < 0.01

    owner = Owner.query.first()
    return render_template('journal/ledger_print.html',
        account=account, from_date=p['from_date'], to_date=p['to_date'],
        opening_balance_display=_fmt_amount(opening_balance),
        total_debit_display=_fmt_amount(period_debit),
        total_credit_display=_fmt_amount(period_credit),
        closing_balance_display=_fmt_amount(closing_balance),
        integrity_ok=integrity_ok, rows=rows, owner=owner,
        printed_at=datetime.now(), current_user_name=getattr(current_user, 'username', ''))