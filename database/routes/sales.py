from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, session, current_app, abort
from flask_login import login_required, current_user
from decimal import Decimal
from datetime import datetime, date
from sqlalchemy import text
import os
import random
import re
import base64
import mimetypes
from werkzeug.utils import secure_filename

from database.routes.rbac import permission_required, permission_required_json

from models import (
    db, BuyerMaster,
    Owner, OwnerBank,
    SalesRequest, SalesQuotation, SalesOrder, DeliveryNote,
    SalesInvoice, SalesReturnRequest, SalesCreditMemo,
    SalesReturnNote, SalesReturnNoteLineItem,
    SalesAttachment,
    SalesRequestLineItem, SalesQuotationLineItem, SalesOrderLineItem,
    DeliveryLineItem, SalesInvoiceLineItem, SalesReturnLineItem, SalesCreditMemoLineItem,
    PurchaseTaxCode, SalesTaxCode,
    GRL, GRLDetail,
    ItemMaster, StoreTransaction, LevelFive,
    ZatcaSettings,
)
from database.routes.shared import _next_grl_no, _grl_num, _grl_lines_from, block_in_basic_mode_json

sale_bp = Blueprint('sales', __name__)


@sale_bp.errorhandler(Exception)
def _handle_sale_errors(e):
    from flask import jsonify, session
    if e.__class__.__name__ == 'NoActiveFinancialYear':
        msg = ('الرجاء تفعيل سنة مالية (بحالة مفتوحة) قبل إضافة أي سجل.'
               if session.get('lang') == 'ar'
               else 'Please activate a financial year (status Open) before adding any record.')
        return jsonify({'ok': False, 'error': msg}), 400
    raise e


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def pd(val):
    """Parse date string, return None if empty/invalid."""
    if not val:
        return None
    try:
        return datetime.strptime(str(val).strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError, AttributeError):
        return None


def _buyer_list():
    """Return list of buyers for dropdowns (sales counterpart of suppliers)."""
    return [{'id': b.id, 'name': b.buyer_name_en, 'name_ar': b.buyer_name_ar or ''}
            for b in BuyerMaster.query.filter_by(is_active=True).order_by(BuyerMaster.buyer_name_en).all()]


@sale_bp.route('/sales/owners/<int:owner_id>/banks')
@login_required
def owner_banks(owner_id):
    """Active bank accounts for a owner, for the invoice/credit-memo bank dropdown."""
    banks = OwnerBank.query.filter_by(owner_id=owner_id).order_by(OwnerBank.is_primary.desc()).all()
    return jsonify([b.to_dict() for b in banks])


@sale_bp.route('/sales/owners/<int:owner_id>/details')
@login_required
def sinv_owner_details(owner_id):
    """Read-only Name/Street/City/VAT/CRN (English + Arabic) for the Sales
    Invoice Seller section's "Selected Seller Details" card -- never
    re-typed, always derived live from Owner."""
    o = Owner.query.get_or_404(owner_id)
    return jsonify({
        'name': o.name or '', 'name_ar': o.name_ar or '',
        'street': o.street_name or '', 'street_ar': o.street_name_ar or '',
        'city': o.city or '', 'city_ar': o.city_ar or '',
        'vat_number': o.vat_number or '', 'crn': o.crn or '',
    })


@sale_bp.route('/sales/buyers/<int:buyer_id>/details')
@login_required
def sinv_buyer_details(buyer_id):
    """Read-only Name/Street/City/VAT/CRN (English + Arabic) for the Sales
    Invoice Buyer section's "Selected Buyer Details" card -- never
    re-typed, always derived live from BuyerMaster."""
    b = BuyerMaster.query.get_or_404(buyer_id)
    return jsonify({
        'name': b.buyer_name_en or '', 'name_ar': b.buyer_name_ar or '',
        'street': b.street_name or '', 'street_ar': b.street_name_ar or '',
        'city': b.city or '', 'city_ar': b.city_ar or '',
        'vat_number': b.vat_number or '', 'crn': b.crn or '',
    })


def _validate_sr_sq_dates(valid_until, required_date):
    """valid_until must be >= today (document_date); required_date must be <= valid_until."""
    today = date.today()
    if valid_until and valid_until < today:
        return 'Valid Until must be on/after the document date'
    if required_date and valid_until and required_date > valid_until:
        return 'Required Date must be on/before Valid Until'
    return None


def _validate_posting_date(posting_date, required_date):
    """Posting date can be the current date or on/before the required date."""
    if posting_date and required_date and posting_date > required_date:
        return 'Posting Date must be on or before the Required Date'
    return None


def _ensure_doc_counters_table():
    """Ensure the doc_counters table exists."""
    try:
        table_exists = db.session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='doc_counters'")
        ).fetchone()
        
        if not table_exists:
            db.session.execute(text("""
                CREATE TABLE doc_counters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_type TEXT NOT NULL UNIQUE,
                    counter_value INTEGER DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            db.session.commit()
            
            doc_types = ['SR', 'SQ', 'SO', 'DN', 'SINV', 'SRR', 'SCM']
            for dt in doc_types:
                db.session.execute(
                    text("INSERT OR IGNORE INTO doc_counters (doc_type, counter_value) VALUES (:dt, 0)"),
                    {'dt': dt}
                )
            db.session.commit()
        return True
    except Exception as e:
        print(f"Error creating doc_counters table: {e}")
        return False


class NoActiveFinancialYear(Exception):
    """Raised when there is no Open financial year to number a document."""
    pass


# Logical doc type -> the new prefix embedded in the document number.
SALES_PREFIX = {
    'SR':  'SLR',   # Sales Request
    'SQ':  'SLQ',   # Sales Quotation
    'SO':  'SLO',   # Sales Order
    'DN':  'SDN',   # Delivery Note
    'SINV':'SLI',   # Sales Invoice
    'SRR': 'SRR',   # Sales Return Request
    'SRN': 'SRN',   # Sale Return Note
    'SCM': 'SCM',   # Sales Credit Memo
}


def _next_doc_no(doc_type, model):
    """Unique document number: <PREFIX>-<FY year>-<n>  e.g. SLR-2026-1.

    The year is the active (Open) financial year, not the calendar year.
    Raises NoActiveFinancialYear if no financial year is open.
    """
    from models import active_fy_year
    year = active_fy_year()
    if not year:
        raise NoActiveFinancialYear()

    prefix = SALES_PREFIX.get(doc_type, doc_type)
    like = f'{prefix}-{year}-%'

    max_num = 0
    for doc in db.session.query(model).filter(model.doc_no.like(like)).all():
        if doc.doc_no:
            try:
                num = int(doc.doc_no.rsplit('-', 1)[1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue

    n = max_num + 1
    doc_no = f'{prefix}-{year}-{n}'

    retries = 0
    while model.query.filter_by(doc_no=doc_no).first() and retries < 100:
        n += 1
        doc_no = f'{prefix}-{year}-{n}'
        retries += 1
    if retries >= 100:
        doc_no = f'{prefix}-{year}-{datetime.now().strftime("%Y%m%d%H%M%S")}'
    return doc_no


def _next_sinv_doc_no(transaction_type):
    """Sales Invoice document numbering, one independent sequence per
    Transaction Type: <transaction_type>-<FY year>-<n>, e.g. STD-INV-2026-1.
    Each of the 6 dropdown values (STD-INV, STD-DR, STD-CR, SIM-INV,
    SIM-DR, SIM-CR -- see database/zatca/engine.py's SINV_TRANSACTION_TYPES)
    uses itself as the prefix and keeps its own count, so e.g. the first
    Standard Debit Note is always STD-DR-<year>-1 no matter how many
    Standard Invoices already exist for that year. This replaces the
    single shared SLI-<year>-<n> counter _next_doc_no('SINV', ...) used to
    assign every Sales Invoice regardless of type -- existing SLI-prefixed
    invoices are untouched and keep their historical numbers; only NEW
    invoices use this. Raises NoActiveFinancialYear if no financial year
    is open, exactly like _next_doc_no."""
    from models import active_fy_year
    year = active_fy_year()
    if not year:
        raise NoActiveFinancialYear()

    like = f'{transaction_type}-{year}-%'
    max_num = 0
    for doc in db.session.query(SalesInvoice).filter(SalesInvoice.doc_no.like(like)).all():
        if doc.doc_no:
            try:
                num = int(doc.doc_no.rsplit('-', 1)[1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue

    n = max_num + 1
    doc_no = f'{transaction_type}-{year}-{n}'

    retries = 0
    while SalesInvoice.query.filter_by(doc_no=doc_no).first() and retries < 100:
        n += 1
        doc_no = f'{transaction_type}-{year}-{n}'
        retries += 1
    if retries >= 100:
        doc_no = f'{transaction_type}-{year}-{datetime.now().strftime("%Y%m%d%H%M%S")}'
    return doc_no


def _sinv_txn_category(transaction_type):
    """Standard/Simplified, derived from a transaction_type value's prefix
    -- covers both the current STD-/SIM- dropdown values and the legacy
    STD_/SMP_ values already stored on invoices created before the
    dropdown was consolidated to a single field. The route layer always
    derives invoice_category this way rather than trusting a separately
    posted form value, so the two fields can never desync."""
    t = (transaction_type or '').upper()
    if t.startswith('STD'):
        return 'standard'
    if t.startswith('SIM') or t.startswith('SMP'):
        return 'simplified'
    return None




def _save_attachments(doc_type, doc_id, files):
    """Save uploaded attachments under static/uploads/sales/<doc_type>/<doc_id>/."""
    upload_dir = os.path.join('static', 'uploads', 'sales', doc_type, str(doc_id))
    os.makedirs(upload_dir, exist_ok=True)
    for f in files:
        if not f or not f.filename:
            continue
        fname = secure_filename(f.filename)
        fpath = os.path.join(upload_dir, fname)
        f.save(fpath)
        att = SalesAttachment(
            doc_type=doc_type, doc_id=doc_id,
            filename=fname, filepath=fpath,
            file_size=os.path.getsize(fpath),
            uploaded_by=current_user.id,
        )
        db.session.add(att)


# ─────────────────────────────────────────────────────────────
# SALES ATTACHMENTS — view / download / delete
# Mirrors purchase.py's _attachment_dir_and_name()/purchase_attachment_*
# routes exactly, but resolves the RBAC permission dynamically from the
# attachment's own doc_type rather than hardcoding one -- Purchase's
# routes hardcode ('purchase','supplier',...) regardless of which document
# type the attachment actually belongs to, a pre-existing inconsistency
# not worth carrying over here.
# ─────────────────────────────────────────────────────────────
_SALES_ATTACHMENT_PERMISSION_MAP = {
    'SR': ('sale', 'sales_request'), 'SQ': ('sale', 'sales_quotation'),
    'SO': ('sale', 'sales_order'), 'DN': ('sale', 'delivery_note'),
    'SINV': ('sale', 'sales_invoice'), 'SINVZ': ('sale', 'sales_invoice'),
    'SRR': ('sale', 'sales_return_request'), 'SRN': ('sale', 'sales_return_note'),
    'SCM': ('sale', 'sales_credit_memo'),
}


def _sales_attachment_dir_and_name(att):
    rel = (att.filepath or '').replace('\\', '/')
    directory = os.path.abspath(os.path.dirname(rel))
    fname = os.path.basename(rel)
    return directory, fname


def _check_sales_attachment_permission(att, action):
    from database.routes.rbac import can
    module, form = _SALES_ATTACHMENT_PERMISSION_MAP.get(att.doc_type, ('sale', 'sales_invoice'))
    if not can(module, form, action):
        abort(403)


@sale_bp.route('/sales/attachments/<int:att_id>/view')
@login_required
def sales_attachment_view(att_id):
    """Open the attachment inline when the browser can show it."""
    from flask import send_from_directory
    att = SalesAttachment.query.get_or_404(att_id)
    _check_sales_attachment_permission(att, 'view')
    directory, fname = _sales_attachment_dir_and_name(att)
    full = os.path.join(directory, fname)
    if not os.path.exists(full):
        abort(404)
    mime = mimetypes.guess_type(fname)[0] or 'application/octet-stream'
    inline_ok = mime in ('application/pdf', 'text/plain', 'text/csv', 'text/xml', 'application/xml') or mime.startswith('image/')
    return send_from_directory(directory, fname, as_attachment=not inline_ok,
                               download_name=att.filename or fname, mimetype=mime)


@sale_bp.route('/sales/attachments/<int:att_id>/download')
@login_required
def sales_attachment_download(att_id):
    """Always download the attachment."""
    from flask import send_from_directory
    att = SalesAttachment.query.get_or_404(att_id)
    _check_sales_attachment_permission(att, 'view')
    directory, fname = _sales_attachment_dir_and_name(att)
    full = os.path.join(directory, fname)
    if not os.path.exists(full):
        abort(404)
    mime = mimetypes.guess_type(fname)[0] or 'application/octet-stream'
    return send_from_directory(directory, fname, as_attachment=True,
                               download_name=att.filename or fname, mimetype=mime)


@sale_bp.route('/sales/attachments/<int:att_id>/delete', methods=['POST'])
@login_required
def sales_attachment_delete(att_id):
    """Delete a single attachment: remove the file from disk, then the row."""
    att = SalesAttachment.query.get_or_404(att_id)
    _check_sales_attachment_permission(att, 'delete')
    try:
        directory, fname = _sales_attachment_dir_and_name(att)
        full = os.path.join(directory, fname)
        if os.path.exists(full):
            os.remove(full)
        db.session.delete(att)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


def _save_generated_attachment(doc_type, doc_id, filename, content_bytes):
    """Same on-disk convention as _save_attachments(), for server-generated
    content (e.g. a ZATCA XML) rather than an uploaded FileStorage."""
    upload_dir = os.path.join('static', 'uploads', 'sales', doc_type, str(doc_id))
    os.makedirs(upload_dir, exist_ok=True)
    fname = secure_filename(filename)
    fpath = os.path.join(upload_dir, fname)
    with open(fpath, 'wb') as fh:
        fh.write(content_bytes)
    att = SalesAttachment(
        doc_type=doc_type, doc_id=doc_id,
        filename=fname, filepath=fpath,
        file_size=os.path.getsize(fpath),
        uploaded_by=current_user.id,
    )
    db.session.add(att)
    db.session.flush()
    return att


def _resolve_tax_code(account_code, doc_type='purchase'):
    """Resolve a tax master's account code (e.g. 'P1') to (rate, display_text).

    PurchaseTaxCode/SalesTaxCode don't have a numeric rate column — the rate
    lives inside the `tax_code` text field (e.g. "15%", "Exempt", "0%").
    `display_text` is that text, which is what gets stored on the line item
    so saved documents show the rate ("15%"), not the internal account code.
    """
    try:
        if doc_type == 'purchase':
            tax = PurchaseTaxCode.query.filter_by(account_code=account_code, status='Active').first()
        else:
            tax = SalesTaxCode.query.filter_by(account_code=account_code, status='Active').first()

        if tax and tax.tax_code:
            m = re.search(r'(\d+(?:\.\d+)?)\s*%', tax.tax_code)
            rate = Decimal(m.group(1)) if m else Decimal('0')
            return rate, tax.tax_code
    except Exception:
        pass

    # Legacy placeholder codes used by the shared line-item widget.
    LEGACY_TAX_CODES = {
        'VAT15': (Decimal('15'), '15%'),
        'VAT0':  (Decimal('0'),  '0%'),
        'EXEMPT': (Decimal('0'), 'Exempt'),
    }
    if account_code in LEGACY_TAX_CODES:
        return LEGACY_TAX_CODES[account_code]

    # Already-resolved display value (e.g. "15%") carried forward from
    # another document's line -- extract the rate directly instead of
    # failing Decimal() on the '%' sign and silently defaulting to 0.
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', account_code or '')
    if m:
        return Decimal(m.group(1)), account_code

    # Fallback: unknown/legacy code — try parsing it directly as a rate.
    try:
        return Decimal(account_code), account_code
    except Exception:
        return Decimal('0'), account_code


def _save_doc_line_items(LIModel, fk_field, fk_value, f, doc_type='purchase', source_link_field=None):
    """Generic save for any dedicated line item model with improved decimal precision.

    source_link_field: optional model attribute name (e.g.
    'sales_order_line_item_id') populated from the parallel
    'li_source_line_id[]' form field, mirroring purchase.py's own
    _save_doc_line_items() -- needed so Delivery Note lines can record
    which SO line they were sourced from, for remaining-quantity checks.
    """
    LIModel.query.filter_by(**{fk_field: fk_value}).delete()

    codes    = f.getlist('li_item_code[]')
    descs    = f.getlist('li_item_desc[]')
    whs      = f.getlist('li_warehouse[]')
    uoms     = f.getlist('li_uom[]')
    qtys     = f.getlist('li_qty[]')
    rates    = f.getlist('li_rate[]')
    discs    = f.getlist('li_discount[]')
    freights = f.getlist('li_freight[]')
    tcodes   = f.getlist('li_tax_code[]')
    src_ids  = f.getlist('li_source_line_id[]') if source_link_field else []

    total_bd = Decimal('0.00')
    total_disc = Decimal('0.00')
    total_fr = Decimal('0.00')
    total_vat = Decimal('0.00')
    total_taxable = Decimal('0.00')

    for i in range(len(qtys)):
        try:
            # Handle empty/None values safely
            qty_str = qtys[i] if i < len(qtys) and qtys[i] else '0'
            rate_str = rates[i] if i < len(rates) and rates[i] else '0'
            disc_str = discs[i] if i < len(discs) and discs[i] else '0'
            fr_str = freights[i] if i < len(freights) and freights[i] else '0'

            # Remove commas and clean
            qty_str = qty_str.replace(',', '').strip()
            rate_str = rate_str.replace(',', '').strip()
            disc_str = disc_str.replace(',', '').strip()
            fr_str = fr_str.replace(',', '').strip()

            # Parse with 4 decimal precision
            qty = Decimal(qty_str or '0').quantize(Decimal('0.0001'))
            rate = Decimal(rate_str or '0').quantize(Decimal('0.0001'))
            disc = Decimal(disc_str or '0').quantize(Decimal('0.0001'))
            fr = Decimal(fr_str or '0').quantize(Decimal('0.0001'))

            # Resolve tax code -> (numeric rate, display text like "15%")
            tax_code = tcodes[i] if i < len(tcodes) and tcodes[i] else '0'
            tax_code = tax_code.strip()
            tax_rate, tax_code_text = _resolve_tax_code(tax_code, doc_type)

            # Round the taxable base to 2dp FIRST, then derive VAT and the
            # line total from THAT rounded figure -- not the raw 4-decimal
            # intermediate. The header totals below are built by summing
            # these exact same already-rounded per-line values, so they
            # can never drift by a cent from what each line displays --
            # summing unrounded amounts and rounding only the header total
            # once at the end (the previous approach) is exactly the kind
            # of thing that makes line items not add up to the printed/
            # ZATCA-submitted summary total.
            taxable_rounded = max(Decimal('0.00'), ((qty * rate - disc) + fr).quantize(Decimal('0.01')))
            tax_amt = (taxable_rounded * tax_rate / 100).quantize(Decimal('0.01'))
            total = (taxable_rounded + tax_amt).quantize(Decimal('0.01'))
            bd_rounded = (qty * rate).quantize(Decimal('0.01'))
            disc_rounded = disc.quantize(Decimal('0.01'))
            fr_rounded = fr.quantize(Decimal('0.01'))

            li = LIModel(**{
                fk_field:        fk_value,
                'line_number':   i + 1,
                'item_code':     codes[i]  if i < len(codes)  else '',
                'description':   descs[i]  if i < len(descs)  else '',
                'warehouse':     whs[i]    if i < len(whs)    else '',
                'uom':           uoms[i]   if i < len(uoms)   else 'unit',
                'quantity':      qty,  # 4 decimals
                'rate':          rate,  # 4 decimals
                'discount':      disc,  # 4 decimals
                'freight':       fr,    # 4 decimals
                'taxable':       taxable_rounded,  # 2 decimals
                'tax_code':      tax_code_text,
                'tax_amount':    tax_amt,  # 2 decimals
                'total':         total,  # 2 decimals
            })
            if source_link_field:
                src_id = src_ids[i] if i < len(src_ids) and src_ids[i] else None
                setattr(li, source_link_field, int(src_id) if src_id else None)
            db.session.add(li)
            total_bd      += bd_rounded
            total_disc    += disc_rounded
            total_fr      += fr_rounded
            total_taxable += taxable_rounded
            total_vat     += tax_amt
        except Exception as e:
            print(f"Error processing line item {i}: {str(e)}")
            import traceback
            traceback.print_exc()

    return {
        'total_before_discount': total_bd,
        'total_discount':        total_disc,
        'total_freight':         total_fr,
        'total_excl_vat':        total_taxable,
        'vat_amount':            total_vat,
        'total_incl_vat':        (total_taxable + total_vat),
    }


@sale_bp.route('/sales/next-doc-no')
@login_required
def next_doc_no():
    """Preview the next document number for a given sales doc type."""
    t = (request.args.get('type') or 'SR').upper()
    force_new = request.args.get('force_new', 'false').lower() == 'true'

    if t == 'SINV':
        # Sales Invoice numbering is keyed by Transaction Type, not a
        # single shared SINV counter -- see _next_sinv_doc_no(). Until a
        # (valid) transaction type is chosen there's nothing to preview.
        from database.zatca import engine as zengine
        txn_type = (request.args.get('transaction_type') or '').strip().upper()
        if txn_type not in zengine.SINV_TRANSACTION_TYPES:
            return jsonify({'doc_no': '', 'force_new': force_new})
        try:
            doc_no = _next_sinv_doc_no(txn_type)
            return jsonify({'doc_no': doc_no, 'force_new': force_new})
        except Exception as e:
            print(f"Error generating doc number: {e}")
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            return jsonify({'doc_no': f'{txn_type}-{timestamp}', 'force_new': force_new})

    model_map = {
        'SR': SalesRequest, 'SQ': SalesQuotation, 'SO': SalesOrder,
        'DN': DeliveryNote, 'SINV': SalesInvoice,
        'SRR': SalesReturnRequest, 'SRN': SalesReturnNote, 'SCM': SalesCreditMemo,
    }
    model = model_map.get(t, SalesRequest)
    try:
        doc_no = _next_doc_no(t, model)
        return jsonify({'doc_no': doc_no, 'force_new': force_new})
    except Exception as e:
        print(f"Error generating doc number: {e}")
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return jsonify({'doc_no': f'{t}-{timestamp}', 'force_new': force_new})


# ══════════════════════════════════════════════════════════════════
# SALES REQUESTS (SR)
# ══════════════════════════════════════════════════════════════════

@sale_bp.route('/sales/requests')
@login_required
@permission_required('sale', 'sales_request', 'view')
def sr_list():
    return render_template('sales/sr_list.html', owners=Owner.query.order_by(Owner.name).all())


@sale_bp.route('/sales/requests/data')
@login_required
@permission_required_json('sale', 'sales_request', 'view')
def sr_data():
    rows = SalesRequest.query.order_by(SalesRequest.sales_request_id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@sale_bp.route('/sales/requests/<int:id>/json')
@login_required
@permission_required_json('sale', 'sales_request', 'view')
def sr_json(id):
    sr = SalesRequest.query.get_or_404(id)
    d = sr.to_dict()
    d['items'] = [i.to_dict() for i in SalesRequestLineItem.query.filter_by(sales_request_id=id).order_by(SalesRequestLineItem.line_number).all()]
    d['attachments'] = [{'filename':a.filename,'filepath':a.filepath} for a in
                        SalesAttachment.query.filter_by(doc_type='SR', doc_id=id).all()]
    return jsonify(d)


@sale_bp.route('/sales/requests/<int:id>/view')
@login_required
@permission_required('sale', 'sales_request', 'view')
def sr_view(id):
    sr = SalesRequest.query.get_or_404(id)
    items = SalesRequestLineItem.query.filter_by(sales_request_id=id).order_by(SalesRequestLineItem.line_number).all()
    attachments = SalesAttachment.query.filter_by(doc_type='SR', doc_id=id).all()
    return render_template('sales/sr_view.html', pr=sr, items=items, attachments=attachments)


@sale_bp.route('/sales/requests/<int:id>/summary')
@login_required
@permission_required_json('sale', 'sales_request', 'view')
def sr_summary(id):
    """Lightweight PR data for auto-filling Sales Quotation form."""
    sr = SalesRequest.query.get_or_404(id)
    d = sr.to_dict()
    d['items'] = [i.to_dict() for i in SalesRequestLineItem.query.filter_by(sales_request_id=id).order_by(SalesRequestLineItem.line_number).all()]
    return jsonify(d)


@sale_bp.route('/sales/requests/add', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_request', 'add')
def sr_add():
    f = request.form
    valid_until   = pd(f.get('valid_until'))
    required_date = pd(f.get('required_date'))
    posting_date  = pd(f.get('posting_date')) or date.today()
    err = _validate_sr_sq_dates(valid_until, required_date)
    if not err:
        err = _validate_posting_date(posting_date, required_date)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    sr = SalesRequest(
        doc_no=_next_doc_no('SR', SalesRequest),
        requester=current_user.username,
        requester_name=current_user.username,
        owner_id=int(f.get('owner_id')) if f.get('owner_id') else None,
        status=f.get('status','Open'),
        kind=f.get('kind','Goods'),
        posting_date=pd(f.get('posting_date')) or date.today(),
        valid_until=valid_until,
        document_date=date.today(),
        required_date=required_date,
        remarks=f.get('remarks','').strip(),
        account_code=f.get('account_code','').strip() or None,
        approved_by=f.get('approved_by','').strip(),
        created_by=current_user.id,
    )
    db.session.add(sr)
    db.session.flush()
    tots = _save_doc_line_items(SalesRequestLineItem, 'sales_request_id', sr.sales_request_id, f, 'sales')
    for k,v in tots.items(): 
        setattr(sr, k, float(v) if isinstance(v, Decimal) else v)
    _save_attachments('SR', sr.sales_request_id, request.files.getlist('attachments'))
    db.session.commit()
    return jsonify({'ok': True, 'id': sr.sales_request_id, 'doc_no': sr.doc_no})


@sale_bp.route('/sales/requests/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_request', 'edit')
def sr_edit(id):
    sr = SalesRequest.query.get_or_404(id)
    f = request.form
    valid_until   = pd(f.get('valid_until'))
    required_date = pd(f.get('required_date'))
    posting_date  = pd(f.get('posting_date')) or date.today()
    err = _validate_sr_sq_dates(valid_until, required_date)
    if not err:
        err = _validate_posting_date(posting_date, required_date)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    for fld in ['status','remarks','approved_by']:
        setattr(sr, fld, f.get(fld,'').strip())
    sr.kind = f.get('kind','Goods')
    if not sr.requester:
        sr.requester = current_user.username
    if not sr.requester_name:
        sr.requester_name = current_user.username
    sr.owner_id = int(f.get('owner_id')) if f.get('owner_id') else None
    sr.account_code = f.get('account_code','').strip() or None
    sr.posting_date  = pd(f.get('posting_date')) or date.today()
    sr.valid_until    = valid_until
    sr.document_date  = date.today()
    sr.required_date  = required_date
    tots = _save_doc_line_items(SalesRequestLineItem, 'sales_request_id', id, f, 'sales')
    for k,v in tots.items(): 
        setattr(sr, k, float(v) if isinstance(v, Decimal) else v)
    _save_attachments('SR', id, request.files.getlist('attachments'))
    db.session.commit()
    return jsonify({'ok': True})


@sale_bp.route('/sales/requests/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_request', 'delete')
def sr_delete(id):
    sr = SalesRequest.query.get_or_404(id)
    SalesRequestLineItem.query.filter_by(sales_request_id=id).delete()
    SalesAttachment.query.filter_by(doc_type='SR', doc_id=id).delete()
    db.session.delete(sr)
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
# SALES QUOTATIONS (SQ)
# ══════════════════════════════════════════════════════════════════

@sale_bp.route('/sales/quotations')
@login_required
@permission_required('sale', 'sales_quotation', 'view')
def sq_list():
    srs = [{'id':p.sales_request_id,'doc_no':p.doc_no} for p in SalesRequest.query.filter_by(status='Approved').order_by(SalesRequest.sales_request_id.desc()).all()]
    return render_template('sales/sq_list.html', buyers=_buyer_list(), srs=srs, owners=Owner.query.order_by(Owner.name).all())


@sale_bp.route('/sales/quotations/data')
@login_required
@permission_required_json('sale', 'sales_quotation', 'view')
def sq_data():
    rows = SalesQuotation.query.order_by(SalesQuotation.sales_quotation_id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@sale_bp.route('/sales/quotations/<int:id>/json')
@login_required
@permission_required_json('sale', 'sales_quotation', 'view')
def sq_json(id):
    sq = SalesQuotation.query.get_or_404(id)
    d = sq.to_dict()
    d['items'] = [i.to_dict() for i in SalesQuotationLineItem.query.filter_by(sales_quotation_id=id).order_by(SalesQuotationLineItem.line_number).all()]
    d['attachments'] = [{'filename':a.filename} for a in SalesAttachment.query.filter_by(doc_type='SQ', doc_id=id).all()]
    return jsonify(d)


@sale_bp.route('/sales/quotations/<int:id>/view')
@login_required
@permission_required('sale', 'sales_quotation', 'view')
def sq_view(id):
    sq = SalesQuotation.query.get_or_404(id)
    items = SalesQuotationLineItem.query.filter_by(sales_quotation_id=id).order_by(SalesQuotationLineItem.line_number).all()
    attachments = SalesAttachment.query.filter_by(doc_type='SQ', doc_id=id).all()
    return render_template('sales/sq_view.html', doc=sq, items=items, attachments=attachments, doc_type='SQ')


@sale_bp.route('/sales/quotations/<int:id>/summary')
@login_required
@permission_required_json('sale', 'sales_quotation', 'view')
def sq_summary(id):
    """Lightweight PQ data for auto-filling Sales Order form."""
    sq = SalesQuotation.query.get_or_404(id)
    d = sq.to_dict()
    d['items'] = [i.to_dict() for i in SalesQuotationLineItem.query.filter_by(sales_quotation_id=id).order_by(SalesQuotationLineItem.line_number).all()]
    return jsonify(d)


@sale_bp.route('/sales/quotations/defaults/<int:owner_id>')
@login_required
@permission_required_json('sale', 'sales_quotation', 'view')
def sq_defaults(owner_id):
    """The Owner's saved default Terms & Conditions / Sign & Stamp content,
    used to pre-fill those editors when starting a brand-new Sales Quotation.
    Either can be empty if the "Default" checkbox has never been checked
    (or was last saved unchecked, which clears it)."""
    owner = Owner.query.get_or_404(owner_id)
    return jsonify({
        'terms_conditions': owner.sq_default_terms_conditions or '',
        'sign_stamp': owner.sq_default_sign_stamp or '',
    })


def _apply_sq_report_defaults(f, owner_id, terms_html, sign_stamp_html):
    """The "Default" checkbox next to the Terms & Conditions / Sign & Stamp
    editors: checked saves the just-submitted content as that Owner's
    default for future new Sales Quotations; unchecked clears it so future
    new quotations start blank instead."""
    if not owner_id:
        return
    owner = Owner.query.get(owner_id)
    if not owner:
        return
    if f.get('save_terms_default') == 'on':
        owner.sq_default_terms_conditions = terms_html or None
    elif f.get('save_terms_default') == 'off':
        owner.sq_default_terms_conditions = None
    if f.get('save_sign_stamp_default') == 'on':
        owner.sq_default_sign_stamp = sign_stamp_html or None
    elif f.get('save_sign_stamp_default') == 'off':
        owner.sq_default_sign_stamp = None


@sale_bp.route('/sales/quotations/add', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_quotation', 'add')
def sq_add():
    f = request.form
    sr_id = int(f.get('sr_id')) if f.get('sr_id') else None
    sr = SalesRequest.query.get(sr_id) if sr_id else None
    if sr_id and (not sr or sr.status != 'Approved'):
        return jsonify({'ok': False, 'error': 'Selected Sales Request is not Approved'}), 400

    buyer_id = int(f.get('buyer_id')) if f.get('buyer_id') else None
    if not buyer_id:
        return jsonify({'ok': False, 'error': 'Buyer must be selected'}), 400

    subject = f.get('subject','').strip()

    valid_until   = pd(f.get('valid_until'))
    required_date = pd(f.get('required_date'))
    posting_date  = pd(f.get('posting_date')) or date.today()
    err = _validate_sr_sq_dates(valid_until, required_date)
    if not err:
        err = _validate_posting_date(posting_date, required_date)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    sq = SalesQuotation(
        doc_no=_next_doc_no('SQ', SalesQuotation),
        sales_request_id=sr_id,
        requester=current_user.username,
        requester_name=current_user.username,
        buyer_id=buyer_id,
        buyer_ref_no=f.get('buyer_ref_no','').strip(),
        owner_id=int(f.get('owner_id')) if f.get('owner_id') else None,
        status=f.get('status','Open'),
        kind=sr.kind if sr else f.get('kind','Goods'),
        posting_date=pd(f.get('posting_date')) or date.today(),
        valid_until=valid_until,
        document_date=date.today(),
        required_date=required_date,
        subject=subject,
        remarks=f.get('remarks','').strip(),
        body=f.get('body','').strip() or None,
        account_code=(sr.account_code if sr else None) or f.get('account_code','').strip() or None,
        report_style=f.get('report_style','header_footer') if f.get('report_style') in ('header_footer','default') else 'header_footer',
        item_summary_display=f.get('item_summary_display','on') if f.get('item_summary_display') in ('on','off') else 'on',
        terms_conditions=f.get('terms_conditions','').strip() or None,
        sign_stamp=f.get('sign_stamp','').strip() or None,
        approved_by=f.get('approved_by','').strip(),
        created_by=current_user.id,
    )
    db.session.add(sq)
    db.session.flush()
    _apply_sq_report_defaults(f, sq.owner_id, sq.terms_conditions, sq.sign_stamp)

    tots = _save_doc_line_items(SalesQuotationLineItem, 'sales_quotation_id', sq.sales_quotation_id, f, 'sales')
    for k, v in tots.items():
        setattr(sq, k, float(v) if isinstance(v, Decimal) else v)

    _save_attachments('SQ', sq.sales_quotation_id, request.files.getlist('attachments'))
    db.session.commit()
    return jsonify({'ok': True, 'id': sq.sales_quotation_id, 'doc_no': sq.doc_no})


@sale_bp.route('/sales/quotations/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_quotation', 'edit')
def sq_edit(id):
    sq = SalesQuotation.query.get_or_404(id)
    f = request.form
    subject = f.get('subject','').strip()

    valid_until   = pd(f.get('valid_until'))
    required_date = pd(f.get('required_date'))
    posting_date  = pd(f.get('posting_date')) or date.today()
    err = _validate_sr_sq_dates(valid_until, required_date)
    if not err:
        err = _validate_posting_date(posting_date, required_date)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    sr_id = int(f.get('sr_id')) if f.get('sr_id') else None
    sr = SalesRequest.query.get(sr_id) if sr_id else None

    buyer_id = int(f.get('buyer_id')) if f.get('buyer_id') else None
    if not buyer_id:
        return jsonify({'ok': False, 'error': 'Buyer must be selected'}), 400

    for fld in ['status','remarks','approved_by']:
        setattr(sq, fld, f.get(fld,'').strip())
    sq.kind = sr.kind if sr else f.get('kind','Goods')
    if not sq.requester:
        sq.requester = current_user.username
    if not sq.requester_name:
        sq.requester_name = current_user.username

    sq.sales_request_id = sr_id
    sq.buyer_id = buyer_id
    sq.buyer_ref_no = f.get('buyer_ref_no','').strip()
    sq.owner_id = int(f.get('owner_id')) if f.get('owner_id') else None
    sq.account_code = (sr.account_code if sr else None) or f.get('account_code','').strip() or None
    sq.subject = subject
    sq.body = f.get('body','').strip() or None
    sq.report_style = f.get('report_style','header_footer') if f.get('report_style') in ('header_footer','default') else 'header_footer'
    sq.item_summary_display = f.get('item_summary_display','on') if f.get('item_summary_display') in ('on','off') else 'on'
    sq.terms_conditions = f.get('terms_conditions','').strip() or None
    sq.sign_stamp = f.get('sign_stamp','').strip() or None
    sq.posting_date  = pd(f.get('posting_date')) or date.today()
    sq.valid_until    = valid_until
    sq.document_date  = date.today()
    sq.required_date  = required_date
    _apply_sq_report_defaults(f, sq.owner_id, sq.terms_conditions, sq.sign_stamp)

    tots = _save_doc_line_items(SalesQuotationLineItem, 'sales_quotation_id', id, f, 'sales')
    for k, v in tots.items():
        setattr(sq, k, float(v) if isinstance(v, Decimal) else v)

    _save_attachments('SQ', id, request.files.getlist('attachments'))
    db.session.commit()
    return jsonify({'ok': True})


@sale_bp.route('/sales/quotations/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_quotation', 'delete')
def sq_delete(id):
    SalesQuotationLineItem.query.filter_by(sales_quotation_id=id).delete()
    SalesAttachment.query.filter_by(doc_type='SQ', doc_id=id).delete()
    sq = SalesQuotation.query.get_or_404(id)
    db.session.delete(sq)
    db.session.commit()
    return jsonify({'ok': True})


def _uploaded_file_data_uri(owner, path_attr):
    """Embed an Owner-uploaded image (logo, stamp, ...) as a base64 data URI
    so the print page is self-contained (no separate file-serving route)."""
    rel_path = getattr(owner, path_attr, None) if owner else None
    if not rel_path:
        return None
    abs_path = os.path.join(current_app.config['UPLOAD_FOLDER'], rel_path)
    if not os.path.exists(abs_path):
        return None
    mime = mimetypes.guess_type(abs_path)[0] or 'image/png'
    with open(abs_path, 'rb') as f:
        return f'data:{mime};base64,' + base64.b64encode(f.read()).decode('ascii')


def _owner_logo_data_uri(owner):
    return _uploaded_file_data_uri(owner, 'logo_path')


@sale_bp.route('/sales/quotations/<int:id>/print')
@login_required
@permission_required('sale', 'sales_quotation', 'print')
def sq_print(id):
    """Professional, letterhead-styled A4 print view of a Sales Quotation --
    read-only, does not touch the saved document in any way."""
    doc = SalesQuotation.query.get_or_404(id)
    owner = doc.owner or Owner.query.first()
    item_rows = SalesQuotationLineItem.query.filter_by(sales_quotation_id=id).order_by(SalesQuotationLineItem.line_number).all()

    header_data_uri = _uploaded_file_data_uri(owner, 'header_path')
    footer_data_uri = _uploaded_file_data_uri(owner, 'footer_path')
    # "Use Header/Footer" is the default report style; only actually switches
    # the letterhead out if the Owner has both images uploaded -- otherwise
    # fall back to the built-in letterhead rather than printing a blank strip.
    use_owner_hf = (doc.report_style or 'header_footer') == 'header_footer' and header_data_uri and footer_data_uri

    return render_template('sales/sales_quotation_print.html', doc=doc, owner=owner, item_rows=item_rows,
        logo_data_uri=_owner_logo_data_uri(owner),
        signature_data_uri=_uploaded_file_data_uri(doc.creator, 'signature_path'),
        header_data_uri=header_data_uri, footer_data_uri=footer_data_uri,
        use_owner_hf=use_owner_hf)


def _html_to_lines(html):
    """Very small HTML->plain-text line splitter for the rich-text Terms &
    Conditions field -- good enough to carry the entered text/paragraph
    breaks into a Word document without pulling in a full HTML renderer."""
    if not html:
        return []
    import re, html as html_mod
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    text = re.sub(r'</(div|p|li)>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = html_mod.unescape(text)
    return [line.strip() for line in text.split('\n') if line.strip()]


def _build_sq_docx(doc, owner, item_rows):
    """Build a Word (.docx) rendition of the Sales Quotation print view --
    same content/order as the HTML print page, in a plain professional
    layout. Read-only: never touches the saved document."""
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from io import BytesIO

    d = Document()

    header_abs = os.path.join(current_app.config['UPLOAD_FOLDER'], owner.header_path) if owner and owner.header_path else None
    footer_abs = os.path.join(current_app.config['UPLOAD_FOLDER'], owner.footer_path) if owner and owner.footer_path else None
    use_owner_hf = ((doc.report_style or 'header_footer') == 'header_footer'
                     and header_abs and os.path.exists(header_abs)
                     and footer_abs and os.path.exists(footer_abs))

    if use_owner_hf:
        try:
            header = d.sections[0].header
            header_p = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
            header_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            header_p.add_run().add_picture(header_abs, width=Inches(6.3))
        except Exception:
            use_owner_hf = False

    if not use_owner_hf:
        if owner and owner.logo_path:
            abs_logo = os.path.join(current_app.config['UPLOAD_FOLDER'], owner.logo_path)
            if os.path.exists(abs_logo):
                try:
                    d.add_picture(abs_logo, width=Inches(1.2))
                except Exception:
                    pass

        name_p = d.add_paragraph()
        name_run = name_p.add_run(owner.name if owner else '—')
        name_run.bold = True
        name_run.font.size = Pt(16)
        if owner and owner.report_color:
            try:
                name_run.font.color.rgb = RGBColor.from_string(owner.report_color.lstrip('#'))
            except Exception:
                pass
        if owner and owner.name_ar:
            d.add_paragraph(owner.name_ar)

        meta_bits = []
        if owner and owner.phone: meta_bits.append(f'Tel: {owner.phone}')
        if owner and owner.email: meta_bits.append(f'Email: {owner.email}')
        if meta_bits:
            d.add_paragraph('    '.join(meta_bits))
        vc_bits = []
        if owner and owner.vat_number: vc_bits.append(f'VAT: {owner.vat_number}')
        if owner and owner.crn: vc_bits.append(f'C.R: {owner.crn}')
        if vc_bits:
            d.add_paragraph('    '.join(vc_bits))

    d.add_paragraph('_' * 70)

    title_p = d.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_p.add_run('SALES QUOTATION')
    title_run.bold = True
    title_run.font.size = Pt(14)
    d.add_paragraph(f'No: {doc.doc_no}')

    d.add_paragraph(f'Date: {doc.posting_date.strftime("%d-%b-%Y") if doc.posting_date else "—"}')
    d.add_paragraph(f'To: {doc.buyer.buyer_name_en if doc.buyer else "—"}')
    d.add_paragraph(f'Subject: {doc.subject or "—"}')

    if doc.body:
        d.add_paragraph('Dear Sir,')
        d.add_paragraph(doc.body)

    # Line items table: always shown, independent of item_summary_display
    scope_p = d.add_paragraph()
    scope_p.add_run('Scope & Rate').bold = True

    table = d.add_table(rows=1, cols=6)
    table.style = 'Light Grid Accent 1'
    for i, text in enumerate(['S/N', 'Description', 'Quantity', 'Unit', 'Rate', 'Amount']):
        cell = table.rows[0].cells[i]
        cell.text = text
        cell.paragraphs[0].runs[0].bold = True
    for li in item_rows:
        row = table.add_row().cells
        row[0].text = str(li.line_number)
        row[1].text = li.description or '—'
        row[2].text = f'{float(li.quantity or 0):,.2f}'
        row[3].text = (li.uom or '').upper() or '—'
        row[4].text = f'{float(li.rate or 0):,.2f}'
        row[5].text = f'{float(li.total or 0):,.2f}'
    d.add_paragraph('')

    # Totals summary: hidden when item_summary_display is 'off'
    if (doc.item_summary_display or 'on') == 'on':
        for label, val in [
            ('Total before discount', doc.total_before_discount),
            ('Total Discount', doc.total_discount),
            ('Freight', doc.total_freight),
            ('Total excl. VAT', doc.total_excl_vat),
            ('VAT Amount', doc.vat_amount),
        ]:
            d.add_paragraph(f'{label}: {float(val or 0):,.2f}')
        total_p = d.add_paragraph()
        total_p.add_run(f'Total incl. VAT: SAR {float(doc.total_incl_vat or 0):,.2f}').bold = True

    if doc.terms_conditions:
        for line in _html_to_lines(doc.terms_conditions):
            d.add_paragraph(line)

    if doc.creator and doc.creator.signature_path:
        abs_sig = os.path.join(current_app.config['UPLOAD_FOLDER'], doc.creator.signature_path)
        if os.path.exists(abs_sig):
            try:
                d.add_picture(abs_sig, width=Inches(1.5))
            except Exception:
                pass

    footer = d.sections[0].footer
    footer_p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    if use_owner_hf:
        footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        try:
            footer_p.add_run().add_picture(footer_abs, width=Inches(6.3))
        except Exception:
            pass
    else:
        footer_bits = []
        if owner:
            addr = ', '.join(x for x in [owner.street_name, owner.building_number, owner.district,
                                          owner.city, owner.postal_code, owner.country] if x)
            if addr: footer_bits.append(addr)
            if owner.phone: footer_bits.append(f'Tel: {owner.phone}')
            if owner.email: footer_bits.append(f'Email: {owner.email}')
            if owner.website: footer_bits.append(f'Web: {owner.website}')
            if owner.vat_number: footer_bits.append(f'VAT: {owner.vat_number}')
            if owner.crn: footer_bits.append(f'C.R: {owner.crn}')
        footer_p.text = '   |   '.join(footer_bits)
        footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    buf = BytesIO()
    d.save(buf)
    buf.seek(0)
    return buf


@sale_bp.route('/sales/quotations/<int:id>/print/word')
@login_required
@permission_required('sale', 'sales_quotation', 'print')
def sq_print_word(id):
    """Download the same Sales Quotation print content as a .docx file."""
    from flask import send_file
    doc = SalesQuotation.query.get_or_404(id)
    owner = doc.owner or Owner.query.first()
    item_rows = SalesQuotationLineItem.query.filter_by(sales_quotation_id=id).order_by(SalesQuotationLineItem.line_number).all()
    buf = _build_sq_docx(doc, owner, item_rows)
    return send_file(buf, as_attachment=True, download_name=f'{doc.doc_no}.docx',
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


# ══════════════════════════════════════════════════════════════════
# SALES ORDERS (SO)
# ══════════════════════════════════════════════════════════════════

@sale_bp.route('/sales/orders')
@login_required
@permission_required('sale', 'sales_order', 'view')
def so_list():
    sqs = [{'id': p.sales_quotation_id, 'doc_no': p.doc_no}
           for p in SalesQuotation.query.filter_by(status='Approved').order_by(SalesQuotation.sales_quotation_id.desc()).all()]
    return render_template('sales/so_list.html', buyers=_buyer_list(), sqs=sqs, owners=Owner.query.order_by(Owner.name).all())


@sale_bp.route('/sales/orders/data')
@login_required
@permission_required_json('sale', 'sales_order', 'view')
def so_data():
    rows = SalesOrder.query.order_by(SalesOrder.sales_order_id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@sale_bp.route('/sales/orders/<int:id>/json')
@login_required
@permission_required_json('sale', 'sales_order', 'view')
def so_json(id):
    so = SalesOrder.query.get_or_404(id)
    d = so.to_dict()
    d['items'] = [i.to_dict() for i in SalesOrderLineItem.query
                  .filter_by(sales_order_id=id)
                  .order_by(SalesOrderLineItem.line_number).all()]
    d['attachments'] = [{'filename': a.filename} 
                        for a in SalesAttachment.query.filter_by(doc_type='SO', doc_id=id).all()]
    return jsonify(d)


@sale_bp.route('/sales/orders/<int:id>/summary')
@login_required
@permission_required_json('sale', 'sales_order', 'view')
def so_summary(id):
    """Lightweight SO data for auto-filling Delivery Note/Sales Invoice
    forms. with_progress=True on each line so a Delivery Note can cap its
    editable delivery quantity at what's still remaining, mirroring
    po_summary() in purchase.py."""
    so = SalesOrder.query.get_or_404(id)
    d = so.to_dict()
    d['items'] = [i.to_dict(with_progress=True) for i in SalesOrderLineItem.query
                  .filter_by(sales_order_id=id)
                  .order_by(SalesOrderLineItem.line_number).all()]
    return jsonify(d)


@sale_bp.route('/sales/orders/<int:id>/view')
@login_required
@permission_required('sale', 'sales_order', 'view')
def so_view(id):
    so = SalesOrder.query.get_or_404(id)
    items = SalesOrderLineItem.query.filter_by(sales_order_id=id).order_by(SalesOrderLineItem.line_number).all()
    attachments = SalesAttachment.query.filter_by(doc_type='SO', doc_id=id).all()
    return render_template('sales/so_view.html', doc=so, items=items, attachments=attachments, doc_type='SO')


@sale_bp.route('/sales/orders/add', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_order', 'add')
def so_add():
    try:
        f = request.form
        sq_id = int(f.get('sq_id')) if f.get('sq_id') else None
        sq = SalesQuotation.query.get(sq_id) if sq_id else None
        if sq_id and (not sq or sq.status != 'Approved'):
            return jsonify({'ok': False, 'error': 'Selected Sales Quotation is not Approved'}), 400

        buyer_id = int(f.get('buyer_id')) if f.get('buyer_id') else (sq.buyer_id if sq else None)
        if not buyer_id:
            return jsonify({'ok': False, 'error': 'Buyer must be selected'}), 400

        doc_no = _next_doc_no('SO', SalesOrder)
        print(f"Generated doc_no: {doc_no}")

        so = SalesOrder(
            doc_no=doc_no,
            sales_quotation_id=sq_id,
            buyer_id=buyer_id,
            buyer_ref_no=f.get('buyer_ref_no', '').strip(),
            owner_id=int(f.get('owner_id')) if f.get('owner_id') else None,
            remarks=f.get('remarks', '').strip(),
            account_code=f.get('account_code','').strip() or None,
            status=f.get('status', 'Open'),
            kind=f.get('kind','Goods'),
            posting_date=pd(f.get('posting_date')) or date.today(),
            delivery_date=pd(f.get('delivery_date')),
            document_date=date.today(),
            created_by=current_user.id,
        )
        db.session.add(so)
        db.session.flush()

        tots = _save_doc_line_items(SalesOrderLineItem, 'sales_order_id', so.sales_order_id, f, 'sales')
        for k, v in tots.items():
            setattr(so, k, float(v) if isinstance(v, Decimal) else v)

        _save_attachments('SO', so.sales_order_id, request.files.getlist('attachments'))
        
        db.session.commit()
        return jsonify({'ok': True, 'id': so.sales_order_id, 'doc_no': so.doc_no})
    
    except Exception as e:
        db.session.rollback()
        error_msg = str(e)
        print(f"ERROR in so_add: {error_msg}")
        import traceback
        traceback.print_exc()
        
        if 'UNIQUE constraint failed' in error_msg or 'IntegrityError' in error_msg:
            try:
                timestamp = datetime.now().strftime("%H%M%S")
                fallback_doc_no = f'SO-{date.today().year}-{timestamp}'
                
                existing = SalesOrder.query.filter_by(doc_no=fallback_doc_no).first()
                if existing:
                    fallback_doc_no = f'SO-{date.today().year}-{timestamp}-{random.randint(100,999)}'
                
                so.doc_no = fallback_doc_no
                db.session.commit()
                return jsonify({'ok': True, 'id': so.sales_order_id, 'doc_no': so.doc_no, 'retry': True})
                
            except Exception as retry_error:
                db.session.rollback()
                print(f"Retry failed: {retry_error}")
                return jsonify({'ok': False, 'error': 'Duplicate document number. Please try again.'}), 500
        
        return jsonify({'ok': False, 'error': error_msg}), 500


@sale_bp.route('/sales/orders/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_order', 'edit')
def so_edit(id):
    try:
        so = SalesOrder.query.get_or_404(id)
        f = request.form
        sq_id = int(f.get('sq_id')) if f.get('sq_id') else None
        sq = SalesQuotation.query.get(sq_id) if sq_id else None

        buyer_id = int(f.get('buyer_id')) if f.get('buyer_id') else (sq.buyer_id if sq else None)
        if not buyer_id:
            return jsonify({'ok': False, 'error': 'Buyer must be selected'}), 400

        so.sales_quotation_id = sq_id
        so.buyer_id = buyer_id
        so.buyer_ref_no = f.get('buyer_ref_no', '').strip()
        so.owner_id = int(f.get('owner_id')) if f.get('owner_id') else None
        so.remarks = f.get('remarks', '').strip()
        so.account_code = f.get('account_code','').strip() or None
        so.status = f.get('status', 'Open')
        so.kind = f.get('kind','Goods')
        so.posting_date  = pd(f.get('posting_date')) or date.today()
        so.delivery_date = pd(f.get('delivery_date'))
        so.document_date = date.today()

        tots = _save_doc_line_items(SalesOrderLineItem, 'sales_order_id', id, f, 'sales')
        for k, v in tots.items():
            setattr(so, k, float(v) if isinstance(v, Decimal) else v)

        _save_attachments('SO', id, request.files.getlist('attachments'))
        
        db.session.commit()
        return jsonify({'ok': True})
    
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in so_edit: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/orders/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_order', 'delete')
def so_delete(id):
    try:
        SalesOrderLineItem.query.filter_by(sales_order_id=id).delete()
        SalesAttachment.query.filter_by(doc_type='SO', doc_id=id).delete()
        so = SalesOrder.query.get_or_404(id)
        db.session.delete(so)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# DELIVERY NOTES (DN)
# ══════════════════════════════════════════════════════════════════

@sale_bp.route('/sales/delivery')
@login_required
@permission_required('sale', 'delivery_note', 'view')
def dn_list():
    sos = [{'id':p.sales_order_id,'doc_no':p.doc_no} for p in SalesOrder.query.filter_by(status='Approved').order_by(SalesOrder.sales_order_id.desc()).all()]
    return render_template('sales/dn_list.html', buyers=_buyer_list(), sos=sos, owners=Owner.query.order_by(Owner.name).all())


@sale_bp.route('/sales/delivery/data')
@login_required
@permission_required_json('sale', 'delivery_note', 'view')
def dn_data():
    return jsonify([r.to_dict() for r in DeliveryNote.query.order_by(DeliveryNote.delivery_note_id.desc()).all()])


@sale_bp.route('/sales/delivery/<int:id>/json')
@login_required
@permission_required_json('sale', 'delivery_note', 'view')
def dn_json(id):
    doc = DeliveryNote.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in DeliveryLineItem.query.filter_by(delivery_note_id=id).order_by(DeliveryLineItem.line_number).all()]
    d['attachments'] = [{'filename':a.filename} for a in SalesAttachment.query.filter_by(doc_type='DN', doc_id=id).all()]
    return jsonify(d)


@sale_bp.route('/sales/delivery/<int:id>/view')
@login_required
@permission_required('sale', 'delivery_note', 'view')
def dn_view(id):
    doc = DeliveryNote.query.get_or_404(id)
    return render_template('sales/dn_view.html', doc=doc,
        items=DeliveryLineItem.query.filter_by(delivery_note_id=id).order_by(DeliveryLineItem.line_number).all(),
        attachments=SalesAttachment.query.filter_by(doc_type='DN', doc_id=id).all())


@sale_bp.route('/sales/delivery/<int:id>/summary')
@login_required
@permission_required_json('sale', 'delivery_note', 'view')
def dn_summary(id):
    """Lightweight GRN data for auto-filling Sales Invoice form."""
    doc = DeliveryNote.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in DeliveryLineItem.query
                  .filter_by(delivery_note_id=id)
                  .order_by(DeliveryLineItem.line_number).all()]
    return jsonify(d)


def _validate_dn_delivery_quantities(f):
    """Every DN line sourced from a SO line (li_source_line_id[], parallel
    to li_qty[]) must not exceed that SO line's remaining_quantity() --
    i.e. after subtracting whatever other Active deliveries have already
    claimed against it. Mirrors _validate_grn_receipt_quantities() in
    purchase.py. Row-locks each SO line so concurrent posts can't both
    slip past the cap."""
    src_ids = f.getlist('li_source_line_id[]')
    qtys    = f.getlist('li_qty[]')
    for i in range(len(qtys)):
        src_id = src_ids[i] if i < len(src_ids) and src_ids[i] else None
        if not src_id:
            continue
        try:
            qty = Decimal((qtys[i] or '0').replace(',', '').strip() or '0')
        except Exception:
            qty = Decimal(0)
        so_line = (SalesOrderLineItem.query
                   .filter_by(sales_order_line_item_id=int(src_id))
                   .with_for_update().first())
        if not so_line:
            continue
        remaining = Decimal(str(so_line.remaining_quantity()))
        if qty > remaining:
            raise ValueError(f'Delivery quantity cannot exceed the remaining SO quantity of {remaining}.')


def _check_dn_stock_availability(f, kind='Goods'):
    """New validation with no Purchase-side equivalent (receiving only
    ever adds stock, so GRN never needs this): a delivery cannot exceed
    the item's current Active store balance. Only meaningful right before
    stock transactions are actually (re)created -- callers must reverse
    this document's own prior stock transactions first (see dn_edit),
    so this always compares against the true post-reversal balance.
    Skipped entirely for a Service-kind delivery -- no physical stock to
    check against."""
    if (kind or '').strip() == 'Services':
        return
    codes = f.getlist('li_item_code[]')
    qtys  = f.getlist('li_qty[]')
    for i in range(len(qtys)):
        code = codes[i] if i < len(codes) else ''
        if not code:
            continue
        try:
            qty = Decimal((qtys[i] or '0').replace(',', '').strip() or '0')
        except Exception:
            qty = Decimal(0)
        if qty <= 0:
            continue
        balance = (db.session.query(db.func.coalesce(db.func.sum(StoreTransaction.quantity), 0))
                   .filter(StoreTransaction.item_code == code, StoreTransaction.status == 'Active')
                   .scalar())
        balance = Decimal(str(balance or 0))
        if qty > balance:
            raise ValueError(f'Insufficient stock for item "{code}": requested {qty}, available {balance}.')


def _reverse_dn_store_transactions(dn_id):
    """Mark every Active Store Transaction created by this Delivery Note
    as Reversed (never deleted -- full audit history kept). Used before
    re-saving or deleting an already-Posted DN. Mirrors
    _reverse_grn_store_transactions()."""
    (StoreTransaction.query
     .filter_by(delivery_note_id=dn_id, status='Active')
     .update({'status': 'Reversed'}, synchronize_session=False))


def _create_dn_store_transactions(doc):
    """Move quantity OUT of each line's Store -- the Sales-side mirror of
    _create_grn_store_transactions(), storing quantity NEGATIVE
    (transaction_type='Sales Delivery') so the existing Purchase-side
    SUM(quantity) balance queries net correctly with zero changes there.
    Called solely when a Delivery Note is Posted, never on a plain Save.

    A Service-kind Delivery Note bypasses the store entirely -- a service
    has no physical stock to deliver, so no Store Transaction is created
    for it at all (its GL posting still happens normally)."""
    if (doc.kind or '').strip() == 'Services':
        return
    from models import Store
    lines = (DeliveryLineItem.query
             .filter_by(delivery_note_id=doc.delivery_note_id).all())
    for li in lines:
        item = ItemMaster.query.filter_by(item_code=li.item_code).first() if li.item_code else None
        store_type = (item.store or '').strip() if item else ''
        store = Store.query.filter_by(name=store_type).first() if store_type else None
        so_line = (SalesOrderLineItem.query.get(li.sales_order_line_item_id)
                   if li.sales_order_line_item_id else None)
        so = so_line.sales_order if so_line else doc.sales_order
        db.session.add(StoreTransaction(
            store_id=store.id if store else None,
            store_type=store_type,
            item_id=item.id if item else None,
            item_code=li.item_code or '',
            item_name=(item.name_en if item else '') or li.description or '',
            uom=li.uom,
            quantity=-li.quantity,  # NEGATIVE -- stock going OUT
            sales_order_id=so.sales_order_id if so else None,
            sales_order_doc_no=so.doc_no if so else '',
            sales_order_line_item_id=li.sales_order_line_item_id,
            delivery_note_id=doc.delivery_note_id,
            delivery_note_doc_no=doc.doc_no,
            delivery_line_item_id=li.delivery_line_item_id,
            buyer_id=doc.buyer_id,
            buyer_name=doc.buyer.buyer_name_en if doc.buyer else '',
            unit_price=li.rate,
            total_amount=li.total,
            transaction_type='Sales Delivery',
            transaction_date=doc.posting_date or date.today(),
            posting_date=doc.posting_date or date.today(),
            created_by=current_user.id,
            status='Active',
        ))


def _apply_dn_fields(doc, f, is_new):
    """Header + line items + attachments, shared by dn_add/dn_edit and
    dn_post_and_save(). Does NOT touch GRL/Journal Entry or stock --
    posting is a separate, explicit step. Mirrors _apply_grn_fields()."""
    so_id = int(f.get('so_id')) if f.get('so_id') else None
    so = SalesOrder.query.get(so_id) if so_id else None
    if is_new:
        if not so_id:
            raise ValueError('An Approved Sales Order must be selected')
        if not so or so.status != 'Approved':
            raise ValueError('Selected Sales Order is not Approved')
    doc.sales_order_id  = so_id
    doc.buyer_id         = int(f.get('buyer_id')) if f.get('buyer_id') else None
    doc.contact_person   = f.get('contact_person', '').strip()
    doc.buyer_ref_no     = f.get('buyer_ref_no', '').strip()
    doc.owner_id         = so.owner_id if so else (int(f.get('owner_id')) if f.get('owner_id') else None)
    doc.account_code     = so.account_code if so else (f.get('account_code', '').strip() or None)
    doc.status           = f.get('status', 'Open')
    doc.kind             = so.kind if so else f.get('kind', 'Goods')
    doc.posting_date     = pd(f.get('posting_date')) or date.today()
    doc.delivery_date    = pd(f.get('delivery_date'))
    doc.document_date    = date.today()
    _validate_dn_delivery_quantities(f)
    db.session.flush()   # ensure doc.delivery_note_id exists
    tots = _save_doc_line_items(DeliveryLineItem, 'delivery_note_id', doc.delivery_note_id, f,
                                 'sales', source_link_field='sales_order_line_item_id')
    for k, v in tots.items(): setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
    _save_attachments('DN', doc.delivery_note_id, request.files.getlist('attachments'))


@sale_bp.route('/sales/delivery/add', methods=['POST'])
@login_required
@permission_required_json('sale', 'delivery_note', 'add')
def dn_add():
    """Save Delivery Note: the document only. No stock, no GRL, no
    Journal Entry -- posting only happens via Post & Save
    (dn_post_and_save)."""
    try:
        doc = DeliveryNote(doc_no=_next_doc_no('DN', DeliveryNote), created_by=current_user.id)
        db.session.add(doc)
        _apply_dn_fields(doc, request.form, is_new=True)
        db.session.commit()
        return jsonify({'ok':True,'id':doc.delivery_note_id,'doc_no':doc.doc_no})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in dn_add: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/delivery/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('sale', 'delivery_note', 'edit')
def dn_edit(id):
    """Save Delivery Note: the document only -- see dn_add().

    If this DN was already Posted, its old Store Transactions are reversed
    FIRST (so the remaining-quantity/stock-availability checks below see
    the true, un-doubled balances) and fresh ones are created for the new
    line quantities immediately after -- i.e. an edit of a Posted DN stays
    Posted and its stock impact is always in sync with its current lines.
    """
    try:
        doc = DeliveryNote.query.get_or_404(id)
        was_posted = doc.posting_status == 'Posted'
        if was_posted:
            _reverse_dn_store_transactions(id)
        _apply_dn_fields(doc, request.form, is_new=False)
        if was_posted:
            _check_dn_stock_availability(request.form, doc.kind)
            _create_dn_store_transactions(doc)
        db.session.commit(); return jsonify({'ok':True})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in dn_edit: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/delivery/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('sale', 'delivery_note', 'delete')
def dn_delete(id):
    doc = DeliveryNote.query.get_or_404(id)
    if doc.posting_status == 'Posted':
        _reverse_dn_store_transactions(id)
    DeliveryLineItem.query.filter_by(delivery_note_id=id).delete()
    SalesAttachment.query.filter_by(doc_type='DN', doc_id=id).delete()
    GRL.query.filter_by(delivery_note_id=id).delete()
    db.session.delete(doc); db.session.commit()
    return jsonify({'ok':True})


@sale_bp.route('/sales/delivery/next-grl-je-no')
@login_required
@permission_required_json('sale', 'delivery_note', 'view')
def dn_next_grl_je_no():
    """Preview the GRL No / Je No a new Delivery Note's GRL would get,
    shown in Add mode before anything is saved -- mirrors
    grn_next_grl_je_no() in purchase.py."""
    from models import next_je_no, NoActiveFinancialYearError
    grl_no = _next_grl_no()
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'ok': True, 'grl_no': grl_no, 'je_no': je_no})


@sale_bp.route('/sales/delivery/<int:dn_id>/grl-build')
@login_required
@permission_required_json('sale', 'delivery_note', 'view')
def grl_build_dn(dn_id):
    """Assemble the GRL view for a Delivery Note entirely from its own
    data + Item Master -- mirrors grl_build() for GRN. Frontend-only
    display; nothing is saved."""
    dn = DeliveryNote.query.get_or_404(dn_id)
    lines = (DeliveryLineItem.query
             .filter_by(delivery_note_id=dn_id)
             .order_by(DeliveryLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code='delivery_note', module_code='sale', kind=dn.kind or 'Goods')
    buyer = BuyerMaster.query.get(dn.buyer_id) if dn.buyer_id else None
    _apply_buyer_control_account(grl_lines, buyer, side='credit', kind=dn.kind or 'Goods')
    return jsonify({
        'ok': True,
        'origion': dn.doc_no or '',
        'posting_date': dn.posting_date.isoformat() if dn.posting_date else '',
        'due_date': dn.delivery_date.isoformat() if getattr(dn, 'delivery_date', None) else '',
        'document_date': dn.document_date.isoformat() if getattr(dn, 'document_date', None) else '',
        'lines': grl_lines,
    })


@sale_bp.route('/sales/orders/<int:so_id>/grl-preview')
@login_required
@permission_required_json('sale', 'delivery_note', 'view')
def grl_preview_from_so(so_id):
    """Preview the GRL records for a not-yet-saved Delivery Note (or, via
    ?form=sales_invoice, a not-yet-saved Sales Invoice created directly
    from this Sales Order), shown in Add mode as soon as its source Sales
    Order is selected -- mirrors grl_preview_from_grr() in purchase.py."""
    so = SalesOrder.query.get_or_404(so_id)
    form_code = request.args.get('form', 'delivery_note')
    lines = (SalesOrderLineItem.query
             .filter_by(sales_order_id=so_id)
             .order_by(SalesOrderLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code=form_code, module_code='sale', kind=so.kind or 'Goods')
    buyer = BuyerMaster.query.get(so.buyer_id) if so.buyer_id else None
    if form_code == 'sales_invoice':
        # Sales Invoice's record 1 is always a Debit (Accounts Receivable
        # increases when a customer is invoiced) whether it's the item's
        # own account or -- once overridden -- the buyer's own control
        # account, unlike Purchase Invoice's AP override, which flips to
        # Credit because a payable is the opposite natural side.
        _apply_buyer_control_account(grl_lines, buyer, side='debit', kind=so.kind or 'Goods')
    elif form_code == 'delivery_note':
        # Delivery Note's Auto Code Selection nature is Debit, so its
        # overridden record 1 must post as a Credit (opposite side).
        _apply_buyer_control_account(grl_lines, buyer, side='credit', kind=so.kind or 'Goods')
    return jsonify({'ok': True, 'lines': grl_lines})


@sale_bp.route('/sales/delivery/<int:dn_id>/grl')
@login_required
@permission_required_json('sale', 'delivery_note', 'view')
def grl_get_dn(dn_id):
    """Return the GRL (with details) for a Delivery Note, or an empty
    shell -- mirrors grl_get() for GRN."""
    dn = DeliveryNote.query.get_or_404(dn_id)
    grl = GRL.query.filter_by(delivery_note_id=dn_id).first()
    if grl:
        return jsonify({'ok': True, 'grl': grl.to_dict()})
    return jsonify({'ok': True, 'grl': {
        'id': None, 'delivery_note_id': dn_id,
        'origion': dn.doc_no or '', 'grl_no': '', 'posting_date': '',
        'due_date': '', 'document_date': '', 'narration': '', 'details': [],
    }})


@sale_bp.route('/sales/delivery/post-and-save', methods=['POST'])
@login_required
@permission_required_json('sale', 'delivery_note', 'post')
@block_in_basic_mode_json
def dn_post_and_save():
    """Post & Save: validate+save the Delivery Note (Step 1), then
    create/refresh its GRL master+detail (Step 3) and linked Journal Entry
    (Step 2), THEN move stock -- ALL in this one transaction, so a failure
    anywhere rolls back everything (no partially-posted document, no
    orphaned stock movement). Rejects re-posting a DN whose posting_status
    is already 'Posted'. Mirrors grn_post_and_save() exactly, keyed off
    GRL.delivery_note_id / origin_type='DN' / form_code='delivery_note',
    plus the stock-availability check GRN never needed (receiving only
    ever adds stock; delivering can't exceed what's actually on hand).
    """
    from models import JournalEntry, JournalEntryDetail, next_je_no, NoActiveFinancialYearError
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = DeliveryNote.query.get_or_404(int(id_str))
            if doc.posting_status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Delivery Note has already been posted.',
                    'تم ترحيل إذن التسليم هذا مسبقاً.')}), 400
            _apply_dn_fields(doc, f, is_new=False)
        else:
            doc = DeliveryNote(doc_no=_next_doc_no('DN', DeliveryNote), created_by=current_user.id)
            db.session.add(doc)
            _apply_dn_fields(doc, f, is_new=True)

        _check_dn_stock_availability(f, doc.kind)

        posting_date  = pd(f.get('grl_posting_date')) or doc.posting_date or date.today()
        due_date      = pd(f.get('grl_due_date')) or doc.delivery_date
        if due_date and posting_date and posting_date > due_date:
            return jsonify({'ok': False, 'error': _t(
                'Posting Date must be on or before the Due Date',
                'يجب أن يكون تاريخ الترحيل قبل أو يساوي تاريخ الاستحقاق')}), 400

        grl = GRL.query.filter_by(delivery_note_id=doc.delivery_note_id).first()
        if not grl:
            grl = GRL(delivery_note_id=doc.delivery_note_id)
            db.session.add(grl)
        if not grl.grl_no:
            grl.grl_no = _next_grl_no()
        grl.origion       = (f.get('grl_origion','') or doc.doc_no or '').strip()
        grl.posting_date  = posting_date
        grl.due_date      = due_date
        grl.document_date = date.today()
        grl.narration     = (f.get('grl_narration','') or '').strip()
        db.session.flush()

        codes    = request.form.getlist('d_code[]')
        refcodes = request.form.getlist('d_reference_code[]')
        names    = request.form.getlist('d_account_name[]')
        names_ar = request.form.getlist('d_account_name_ar[]')
        controls = request.form.getlist('d_control_account[]')
        debits   = request.form.getlist('d_debit[]')
        credits  = request.form.getlist('d_credit[]')
        narrs    = request.form.getlist('d_narration[]')

        non_blank = [i for i in range(len(codes))
                     if (codes[i] or '').strip() or (i < len(names) and (names[i] or '').strip())]
        if not non_blank:
            return jsonify({'ok': False, 'error': _t(
                'No GRL detail records to post.', 'لا توجد سجلات لترحيلها.')}), 400
        if len(non_blank) > 2:
            return jsonify({'ok': False, 'error': 'Only two GRL detail records are allowed.'}), 400

        tot_d = sum(float(_grl_num(debits[i] if i < len(debits) else 0)) for i in range(len(codes)))
        tot_c = sum(float(_grl_num(credits[i] if i < len(credits) else 0)) for i in range(len(codes)))
        if abs(tot_d - tot_c) > 0.005:
            return jsonify({'ok': False, 'error': 'Total Debit must equal Total Credit.'}), 400

        je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
        if not je:
            je = JournalEntry(je_no=next_je_no(), origin_type='DN', origin_id=doc.delivery_note_id)
            db.session.add(je)
            db.session.flush()
            grl.journal_entry_id = je.id
        je.origion       = grl.origion
        je.posting_date  = grl.posting_date
        je.due_date      = grl.due_date
        je.document_date = grl.document_date
        je.narration     = grl.narration

        GRLDetail.query.filter_by(grl_id=grl.id).delete()
        JournalEntryDetail.query.filter_by(journal_entry_id=je.id).delete()
        for i in range(len(codes)):
            code = (codes[i] or '').strip()
            if not code and not (names[i] if i < len(names) else '').strip():
                continue
            reference_code  = (refcodes[i] if i < len(refcodes) else '').strip()
            account_name    = (names[i] if i < len(names) else '').strip()
            control_account = (controls[i] if i < len(controls) else '').strip()
            debit           = _grl_num(debits[i] if i < len(debits) else 0)
            credit          = _grl_num(credits[i] if i < len(credits) else 0)
            narration       = (narrs[i] if i < len(narrs) else '').strip()
            db.session.add(GRLDetail(
                grl_id=grl.id, code=code, reference_code=reference_code, account_name=account_name,
                account_name_ar=(names_ar[i] if i < len(names_ar) else '').strip(),
                control_account=control_account, debit=debit, credit=credit, narration=narration,
            ))
            db.session.add(JournalEntryDetail(
                journal_entry_id=je.id, code=code, reference_code=reference_code, account_name=account_name,
                control_account=control_account, debit=debit, credit=credit, narration=narration,
            ))

        doc.posting_status = 'Posted'
        _create_dn_store_transactions(doc)
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.delivery_note_id, 'doc_no': doc.doc_no, 'grl': grl.to_dict()})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.',
            'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in dn_post_and_save: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


def _apply_buyer_control_account(lines, buyer, side='debit', kind='Goods'):
    """Sales Invoice and Sales Credit Memo only: mirrors
    _apply_supplier_control_account() in purchase.py structurally, but
    keyed off the document's own Buyer (BuyerMaster) instead of a
    Supplier, and -- unlike that function -- never flips which side
    record 1 posts to. Record 1 normally posts the item's own Level Five
    account (Debit for Sales Invoice, Credit for Sales Credit Memo -- see
    _grl_lines_from() in shared.py). When the document's buyer has their
    own control account assigned (via the Buyer form's Chart of Account
    picker, which only offers control accounts), record 1 is replaced
    entirely by that SAME buyer account instead -- code/account name come
    from the buyer's own mapping, and Reference Code carries the buyer's
    own subsidiary code (e.g. 'Buy-2026-1') for per-buyer traceability.
    No-op -- record 1 stays exactly as _grl_lines_from() computed it -- if
    the buyer has no control account configured.

    `side` must always match whichever side record 1 already defaults to
    for that particular form (Debit for Sales Invoice, Credit for Sales
    Credit Memo) -- the override only ever swaps WHICH account record 1
    posts to (the item's vs. the buyer's own), never the accounting
    direction, because both represent the same Accounts Receivable
    relationship with this buyer, just increasing (Sales Invoice) or
    decreasing (Sales Credit Memo) it. This is unlike Purchase
    Invoice/Purchase Debit Memo's supplier override, which represents a
    liability (Accounts Payable) on the opposite natural side from the
    item account it replaces, and so genuinely does flip sides.

    `kind` is the parent document's own Kind (Goods/Services) -- mirrors
    _apply_supplier_control_account()'s own `kind` guard: Services is
    always a no-op, since record 1 there is already the line's own chosen
    Level Five account, not an Item Master item standing in for one."""
    if kind == 'Services':
        return lines
    if not buyer or not getattr(buyer, 'levelfive_code', None):
        return lines
    buyer_account = LevelFive.query.filter_by(code=buyer.levelfive_code).first()
    if not buyer_account or buyer_account.control_account != 'Yes':
        return lines
    for ln in lines:
        total = float(ln.get('debit') or 0) + float(ln.get('credit') or 0)
        ln['code'] = buyer.levelfive_code
        ln['account_name'] = buyer_account.drawers or ''
        ln['account_name_ar'] = buyer_account.drawers_ar or ''
        ln['control_account'] = buyer_account.control_account or 'Yes'
        ln['reference_code'] = buyer.buyer_code or ''
        if side == 'debit':
            ln['debit'] = total
            ln['credit'] = 0
        else:
            ln['debit'] = 0
            ln['credit'] = total
    return lines


# ══════════════════════════════════════════════════════════════════
# SALES INVOICES (SINV)
# ══════════════════════════════════════════════════════════════════

@sale_bp.route('/sales/invoices')
@login_required
@permission_required('sale', 'sales_invoice', 'view')
def sinv_list():
    sos = [{'id':p.sales_order_id,'doc_no':p.doc_no} for p in SalesOrder.query.filter_by(status='Approved').order_by(SalesOrder.sales_order_id.desc()).all()]
    dns = [{'id':g.delivery_note_id,'doc_no':g.doc_no,'sales_order_id':g.sales_order_id} for g in DeliveryNote.query.order_by(DeliveryNote.delivery_note_id.desc()).all()]
    return render_template('sales/sinv_list.html', buyers=_buyer_list(), sos=sos, dns=dns, owners=Owner.query.order_by(Owner.name).all())


@sale_bp.route('/sales/invoices/data')
@login_required
@permission_required_json('sale', 'sales_invoice', 'view')
def sinv_data():
    return jsonify([r.to_dict() for r in SalesInvoice.query.order_by(SalesInvoice.sales_invoice_id.desc()).all()])


@sale_bp.route('/sales/invoices/reference-list')
@login_required
@permission_required_json('sale', 'sales_invoice', 'view')
def sinv_reference_list():
    """Previously-saved invoices for the reference-invoice picker."""
    category = (request.args.get('category') or '').strip().lower()
    exclude  = request.args.get('exclude', type=int)
    q = SalesInvoice.query
    if category in ('standard', 'simplified'):
        q = q.filter(SalesInvoice.invoice_category == category)
    if exclude:
        q = q.filter(SalesInvoice.sales_invoice_id != exclude)
    rows = q.order_by(SalesInvoice.sales_invoice_id.desc()).all()
    return jsonify([{
        'id': r.sales_invoice_id,
        'doc_no': r.doc_no or '',
        'buyer_name': r.buyer.buyer_name_en if r.buyer else '',
        'invoice_category': r.invoice_category or '',
        'transaction_type': r.transaction_type or '',
        'document_date': str(r.document_date) if r.document_date else '',
        'total_incl_vat': float(r.total_incl_vat or 0),
    } for r in rows])


@sale_bp.route('/sales/invoices/<int:id>/json')
@login_required
@permission_required_json('sale', 'sales_invoice', 'view')
def sinv_json(id):
    doc = SalesInvoice.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in SalesInvoiceLineItem.query.filter_by(sales_invoice_id=id).order_by(SalesInvoiceLineItem.line_number).all()]
    d['attachments'] = [{'filename':a.filename} for a in SalesAttachment.query.filter_by(doc_type='SINV', doc_id=id).all()]
    return jsonify(d)


@sale_bp.route('/sales/invoices/<int:id>/view')
@login_required
@permission_required('sale', 'sales_invoice', 'view')
def sinv_view(id):
    doc = SalesInvoice.query.get_or_404(id)
    zatca_attachment = None
    if doc.zatca_attachment_id:
        zatca_attachment = SalesAttachment.query.get(doc.zatca_attachment_id)
    return render_template('sales/sinv_view.html', doc=doc,
        items=SalesInvoiceLineItem.query.filter_by(sales_invoice_id=id).order_by(SalesInvoiceLineItem.line_number).all(),
        attachments=SalesAttachment.query.filter_by(doc_type='SINV', doc_id=id).all(),
        zatca_attachment=zatca_attachment,
        qr_b64=_sinv_qr_for(doc) if doc.zatca_invoice_hash else None)


def _sinv_qr_b64(doc):
    """ZATCA Phase-1 QR for a Sales Invoice -- here WE are the seller, so
    it's built from the Owner (our company), not the Buyer. Kept unchanged
    as the fallback for any invoice that hasn't gone through "Create ZATCA
    Invoice" yet -- see _sinv_zatca_qr_b64() for the Phase-2 (9-tag) QR."""
    from database.routes.purchase import _zatca_qr_payload, _qr_image_b64
    ts_date = doc.document_date or doc.posting_date or date.today()
    timestamp_iso = datetime.combine(ts_date, datetime.min.time()).strftime('%Y-%m-%dT%H:%M:%SZ')
    qr_payload = _zatca_qr_payload(
        seller_name=doc.owner.name if doc.owner else '',
        vat_number=(doc.owner.vat_number if doc.owner else '') or '',
        timestamp_iso=timestamp_iso,
        total=float(doc.total_incl_vat or 0),
        vat_amount=float(doc.vat_amount or 0),
    )
    return _qr_image_b64(qr_payload)


def _sinv_zatca_qr_b64(doc):
    """ZATCA Phase-2 QR (9 tags) for a Sales Invoice that has already been
    through "Create ZATCA Invoice" -- extends the Phase-1 payload with the
    invoice hash/signature/public key/certificate-authority signature. Only
    call this once doc.zatca_status != 'not_generated'; callers should fall
    back to _sinv_qr_b64() otherwise."""
    from database.routes.purchase import _zatca_qr_payload, _qr_image_b64
    from database.routes.zatca import zatca_settings_query
    from database.zatca import engine as zengine
    settings = zatca_settings_query().first()
    ts_date = doc.document_date or doc.posting_date or date.today()
    timestamp_iso = datetime.combine(ts_date, datetime.min.time()).strftime('%Y-%m-%dT%H:%M:%SZ')
    extra_tags = zengine.phase2_qr_extra_tags(
        invoice_hash_b64=doc.zatca_invoice_hash or '',
        signature_b64=doc.zatca_xml_signature or '',
        public_key_b64=(settings.cert_public_key_b64 if settings else '') or '',
        cert_ca_signature_b64=(settings.cert_ca_signature_b64 if settings else '') or '',
    )
    qr_payload = _zatca_qr_payload(
        seller_name=doc.owner.name if doc.owner else '',
        vat_number=(doc.owner.vat_number if doc.owner else '') or '',
        timestamp_iso=timestamp_iso,
        total=float(doc.total_incl_vat or 0),
        vat_amount=float(doc.vat_amount or 0),
        extra_tags=extra_tags,
    )
    return _qr_image_b64(qr_payload)


def _sinv_qr_for(doc):
    """Picks the right QR for wherever a Sales Invoice's QR is displayed --
    Phase 2 once generated and accepted (or awaiting live submission),
    Phase 1 otherwise: old/never-submitted invoices keep printing exactly
    as they always have, and an invoice ZATCA explicitly rejected
    (zatca_status == 'failed') falls back to the plain Phase-1 QR rather
    than displaying signature/hash tags implying a clearance that never
    happened -- the signed XML stays attached regardless, as the record of
    what was attempted."""
    status = doc.zatca_status or 'not_generated'
    if status not in ('not_generated', 'failed') and doc.zatca_invoice_hash:
        try:
            return _sinv_zatca_qr_b64(doc)
        except Exception:
            pass
    return _sinv_qr_b64(doc)


_SINV_PRINT_TEMPLATE_FILES = {
    'classic':    'sales/sales_invoice_print.html',
    'letterhead': 'sales/sales_invoice_print_letterhead.html',
    'formal':     'sales/sales_invoice_print_formal.html',
    'default1':   'sales/sales_invoice_print_default1.html',
}


def _amount_in_words(amount):
    """English + Arabic "amount in words" for the formal template's
    legally-conventional SAR amount-in-words line, e.g. 92552.00 ->
    ('Ninety two thousand, five hundred and fifty two Saudi Riyals only.',
     'اثنان و تسعون ألفاً و خمسمائة و اثنان و خمسون ريال سعودي فقط لا غير.')
    Uses num2words for the actual digit-to-word conversion in both
    languages -- a well-established library, not a hand-rolled one, since
    correct Arabic number grammar (gender agreement, dual forms) is
    genuinely intricate; its Arabic output is a standard, widely-used
    approximation, not independently verified against a native speaker
    for every possible amount."""
    from num2words import num2words
    amount = Decimal(str(amount or 0))
    riyals = int(amount)
    halalas = int((amount - riyals) * 100)

    en = num2words(riyals, lang='en').replace('-', ' ').capitalize()
    en += ' Saudi Riyal' + ('s' if riyals != 1 else '')
    if halalas:
        en += ' and ' + num2words(halalas, lang='en').replace('-', ' ') + ' Halala' + ('s' if halalas != 1 else '')
    en += ' only.'

    ar = num2words(riyals, lang='ar') + ' ريال سعودي'
    if halalas:
        ar += ' و' + num2words(halalas, lang='ar') + ' هللة'
    ar += ' فقط لا غير.'

    return en, ar


def _sinv_print_html(doc, template_override=None, is_preview=False):
    """Renders the Tax Invoice print layout to an HTML string -- shared by
    sinv_print() (served directly for on-screen/browser-print use) and
    sinv_pdf() (fed to a headless browser to produce a real downloadable
    PDF, identically rendered). Which of the 4 templates is used is picked
    from Owner.sinv_print_template (set in Owner Settings) -- reading it
    here, in the one function both routes call, is what guarantees Print
    and the PDF download can never show a different template from each
    other. `template_override`, when given, is used instead of the saved
    Owner setting -- sinv_preview_template() uses this so Owner Settings'
    "Preview" button can show any of the 4 templates against a real
    invoice without first saving the dropdown's choice. `is_preview`
    suppresses the templates' on-load auto-print() -- sinv_preview_template()
    sets it so comparing templates from Owner Settings never pops the
    browser's native print dialog, which (on Windows Chrome at least) is
    process-modal and blocks input to every other tab/window, including
    the Owner form the Preview button was opened from, until dismissed."""
    owner = doc.owner or Owner.query.first()
    items = SalesInvoiceLineItem.query.filter_by(sales_invoice_id=doc.sales_invoice_id).order_by(SalesInvoiceLineItem.line_number).all()

    bank = OwnerBank.query.get(doc.bank_account_id) if doc.bank_account_id else None

    # The Description column shows the Item Master's own name for the line's
    # item_code (the name an item is actually catalogued under) rather than
    # whatever free-text note was typed into the line at entry time -- falls
    # back to that free text only for lines with no item_code / no matching
    # Item Master row (e.g. ad-hoc service lines).
    item_codes = {li.item_code for li in items if li.item_code}
    item_master_by_code = {}
    if item_codes:
        for im in ItemMaster.query.filter(ItemMaster.item_code.in_(item_codes)).all():
            item_master_by_code[im.item_code] = im

    item_rows = []
    for li in items:
        m = re.search(r'(\d+(?:\.\d+)?)\s*%', li.tax_code or '')
        tax_pct = f'{m.group(1)}%' if m else (li.tax_code or '—')
        item_master = item_master_by_code.get(li.item_code) if li.item_code else None
        item_name = item_master.name_en if item_master and item_master.name_en else None
        item_rows.append({
            'item_code': li.item_code or '', 'description': item_name or li.description or '', 'uom': li.uom or '',
            'quantity': float(li.quantity or 0), 'rate': float(li.rate or 0),
            'discount': float(li.discount or 0), 'taxable': float(li.taxable or 0),
            'tax_pct': tax_pct, 'tax_amount': float(li.tax_amount or 0), 'total': float(li.total or 0),
        })

    if template_override in _SINV_PRINT_TEMPLATE_FILES:
        template_key = template_override
    else:
        template_key = (getattr(owner, 'sinv_print_template', None) or 'formal') if owner else 'formal'
    template_name = _SINV_PRINT_TEMPLATE_FILES.get(template_key, _SINV_PRINT_TEMPLATE_FILES['formal'])

    header_data_uri = _uploaded_file_data_uri(owner, 'header_path')
    footer_data_uri = _uploaded_file_data_uri(owner, 'footer_path')
    # The letterhead template falls back to the classic built-in header
    # rather than printing a blank strip if the Owner picked "letterhead"
    # but hasn't actually uploaded both images yet.
    use_owner_hf = template_key == 'letterhead' and header_data_uri and footer_data_uri

    amount_in_words_en, amount_in_words_ar = _amount_in_words(doc.total_incl_vat)

    # The formal template's Bank Details row deliberately shows the
    # Owner's own active bank account, not doc.bank_account_id -- an
    # invoice may not have a specific bank chosen at all, and this row is
    # meant to tell the buyer where to pay the Owner, which doesn't
    # depend on which bank (if any) the invoice itself references.
    owner_active_bank = None
    if owner:
        owner_active_bank = (OwnerBank.query.filter_by(owner_id=owner.id, is_primary=True).first()
                              or OwnerBank.query.filter_by(owner_id=owner.id).first())

    return render_template(template_name, doc=doc, owner=owner, item_rows=item_rows,
        bank=bank, owner_active_bank=owner_active_bank, qr_b64=_sinv_qr_for(doc),
        logo_data_uri=_owner_logo_data_uri(owner),
        bg_logo_data_uri=_uploaded_file_data_uri(owner, 'bg_logo_path'),
        header_data_uri=header_data_uri, footer_data_uri=footer_data_uri, use_owner_hf=use_owner_hf,
        amount_in_words_en=amount_in_words_en, amount_in_words_ar=amount_in_words_ar,
        is_preview=is_preview)


@sale_bp.route('/sales/invoices/preview-template')
@login_required
@permission_required('sale', 'sales_invoice', 'print')
def sinv_preview_template():
    """Owner Settings' "Preview" button: renders the most recent Sales
    Invoice using whichever template is passed in ?template=, regardless
    of what's actually saved on Owner -- lets someone compare all 3
    templates against a real invoice before committing to a choice by
    saving the form."""
    template_key = (request.args.get('template') or '').strip()
    doc = SalesInvoice.query.order_by(SalesInvoice.sales_invoice_id.desc()).first()
    if not doc:
        return _t(
            '<div style="font-family:sans-serif;padding:60px 20px;text-align:center;color:#6b7280;">'
            'No Sales Invoice exists yet to preview a template against.<br>Create one first.</div>',
            '<div dir="rtl" style="font-family:sans-serif;padding:60px 20px;text-align:center;color:#6b7280;">'
            'لا توجد فاتورة مبيعات بعد لمعاينة القالب عليها.<br>يرجى إنشاء واحدة أولاً.</div>')
    return _sinv_print_html(doc, template_override=template_key, is_preview=True)


@sale_bp.route('/sales/invoices/<int:id>/print')
@login_required
@permission_required('sale', 'sales_invoice', 'print')
def sinv_print(id):
    """Formal Tax Invoice print layout for a Sales Invoice -- mirrors
    purchase.pinv_print, with Seller/Buyer swapped: Seller is our own Owner
    (we're the one selling), Buyer is the invoice's own BuyerMaster."""
    doc = SalesInvoice.query.get_or_404(id)
    return _sinv_print_html(doc)


@sale_bp.route('/sales/invoices/<int:id>/pdf')
@login_required
@permission_required('sale', 'sales_invoice', 'print')
def sinv_pdf(id):
    """Download a real PDF file of the Tax Invoice -- same template, same
    headless-Chromium-rendered visual output as /print, but as an actual
    file rather than a browser print dialog (which JS can't hook into to
    attach anything). If this invoice has already been through ZATCA, its
    signed XML is embedded inside the PDF as a genuine file attachment (a
    Factur-X/ZUGFeRD-style hybrid document), so the one downloaded file
    carries both the human-readable invoice and its machine-readable XML;
    otherwise a plain PDF is returned."""
    from flask import Response
    from database import pdf_engine

    doc = SalesInvoice.query.get_or_404(id)
    html = _sinv_print_html(doc)
    try:
        pdf_bytes = pdf_engine.html_to_pdf(html)
    except Exception as e:
        current_app.logger.exception('PDF rendering failed for SalesInvoice %s', id)
        return jsonify({'ok': False, 'error': _t(
            f'Failed to render PDF: {e}', f'فشل إنشاء ملف PDF: {e}')}), 500

    if doc.zatca_attachment_id:
        xml_att = SalesAttachment.query.get(doc.zatca_attachment_id)
        if xml_att:
            try:
                directory, fname = _sales_attachment_dir_and_name(xml_att)
                with open(os.path.join(directory, fname), 'rb') as f:
                    xml_bytes = f.read()
                pdf_bytes = pdf_engine.embed_file_in_pdf(pdf_bytes, xml_att.filename, xml_bytes)
            except Exception:
                current_app.logger.exception(
                    'Failed to embed ZATCA XML into PDF for SalesInvoice %s -- serving the plain PDF instead', id)

    filename = f'{doc.doc_no or ("SINV-" + str(id))}.pdf'
    return Response(pdf_bytes, mimetype='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="{filename}"',
    })


@sale_bp.route('/sales/invoices/<int:id>/summary')
@login_required
@permission_required_json('sale', 'sales_invoice', 'view')
def sinv_summary(id):
    doc = SalesInvoice.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in SalesInvoiceLineItem.query.filter_by(sales_invoice_id=id).order_by(SalesInvoiceLineItem.line_number).all()]
    return jsonify(d)


@sale_bp.route('/sales/invoices/add', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_invoice', 'add')
def sinv_add():
    try:
        from database.zatca import engine as zengine
        f = request.form
        so_id = int(f.get('so_id')) if f.get('so_id') else None
        so = SalesOrder.query.get(so_id) if so_id else None
        if so_id and (not so or so.status != 'Approved'):
            return jsonify({'ok': False, 'error': 'Selected Sales Order is not Approved'}), 400

        txn_type = (f.get('transaction_type') or '').strip().upper()
        if txn_type not in zengine.SINV_TRANSACTION_TYPES:
            return jsonify({'ok': False, 'error': _t(
                'Select a valid Transaction Type.', 'يرجى اختيار نوع معاملة صالح.')}), 400

        status = f.get('status','Open')
        posting_date = pd(f.get('posting_date')) or date.today()
        if status != 'Open' and not posting_date:
            posting_date = date.today()

        from_date = pd(f.get('from_date'))
        to_date = pd(f.get('to_date'))
        if from_date and to_date and from_date > to_date:
            return jsonify({'ok': False, 'error': 'From Date must be on or before To Date'}), 400

        doc = SalesInvoice(
            doc_no=_next_sinv_doc_no(txn_type),
            sales_order_id=so_id,
            delivery_note_id=int(f.get('dn_id')) if f.get('dn_id') else None,
            buyer_id=int(f.get('buyer_id')) if f.get('buyer_id') else (so.buyer_id if so else None),
            buyer_ref_no=f.get('buyer_ref_no','').strip(),
            transaction_type=txn_type,
            invoice_category=_sinv_txn_category(txn_type),
            reference_invoices=f.get('reference_invoices','').strip() or None,
            project_ref=f.get('project_ref','').strip() or None,
            status=status,
            kind=f.get('kind','Goods'),
            payment_method=f.get('payment_method','Credit'),
            owner_id=int(f.get('owner_id')) if f.get('owner_id') else None,
            account_code=f.get('account_code','').strip() or None,
            bank_account_id=int(f.get('bank_account_id')) if f.get('bank_account_id') else None,
            posting_date=posting_date,
            delivery_date=pd(f.get('delivery_date')),
            document_date=date.today(),
            from_date=from_date,
            to_date=to_date,
            created_by=current_user.id,
        )
        db.session.add(doc)
        db.session.flush()

        tots = _save_doc_line_items(SalesInvoiceLineItem, 'sales_invoice_id', doc.sales_invoice_id, f, 'sales')
        for k,v in tots.items():
            setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
        _save_attachments('SINV', doc.sales_invoice_id, request.files.getlist('attachments'))
        
        db.session.commit()
        return jsonify({'ok':True,'id':doc.sales_invoice_id,'doc_no':doc.doc_no})
    
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in sinv_add: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/invoices/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_invoice', 'edit')
def sinv_edit(id):
    try:
        from database.zatca import engine as zengine
        doc = SalesInvoice.query.get_or_404(id)
        f = request.form

        # Edit accepts the current 6 dropdown values AND the legacy
        # underscore-separated ones (STD_INV, SMP_CR, ...) still on file
        # for invoices created before the dropdown was consolidated --
        # unlike sinv_add(), which only accepts the current 6, since an
        # old invoice being edited for an unrelated field must not be
        # forced into re-classification just because its dropdown option
        # was renamed.
        txn_type = (f.get('transaction_type') or '').strip().upper()
        if txn_type not in zengine.TXN_TYPE_MAP:
            return jsonify({'ok': False, 'error': _t(
                'Select a valid Transaction Type.', 'يرجى اختيار نوع معاملة صالح.')}), 400

        status = f.get('status','Open')
        posting_date = pd(f.get('posting_date')) or date.today()
        if status != 'Open' and not posting_date:
            posting_date = date.today()

        from_date = pd(f.get('from_date'))
        to_date = pd(f.get('to_date'))
        if from_date and to_date and from_date > to_date:
            return jsonify({'ok': False, 'error': 'From Date must be on or before To Date'}), 400

        doc.sales_order_id = int(f.get('so_id')) if f.get('so_id') else None
        doc.delivery_note_id = int(f.get('dn_id')) if f.get('dn_id') else None
        doc.buyer_id = int(f.get('buyer_id')) if f.get('buyer_id') else None
        doc.buyer_ref_no = f.get('buyer_ref_no','').strip()
        doc.transaction_type = txn_type
        doc.invoice_category = _sinv_txn_category(txn_type)
        doc.reference_invoices = f.get('reference_invoices','').strip() or None
        doc.project_ref = f.get('project_ref','').strip() or None
        doc.status = status
        doc.kind = f.get('kind','Goods')
        doc.payment_method = f.get('payment_method','Credit')
        doc.owner_id = int(f.get('owner_id')) if f.get('owner_id') else None
        doc.account_code = f.get('account_code','').strip() or None
        doc.bank_account_id = int(f.get('bank_account_id')) if f.get('bank_account_id') else None
        doc.posting_date  = posting_date
        doc.delivery_date = pd(f.get('delivery_date'))
        doc.document_date = date.today()
        doc.from_date = from_date
        doc.to_date = to_date

        tots = _save_doc_line_items(SalesInvoiceLineItem, 'sales_invoice_id', id, f, 'sales')
        for k,v in tots.items(): 
            setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
        _save_attachments('SINV', id, request.files.getlist('attachments'))
        
        db.session.commit()
        return jsonify({'ok':True})
    
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in sinv_edit: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/invoices/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_invoice', 'delete')
def sinv_delete(id):
    try:
        SalesInvoiceLineItem.query.filter_by(sales_invoice_id=id).delete()
        SalesAttachment.query.filter_by(doc_type='SINV', doc_id=id).delete()
        GRL.query.filter_by(sales_invoice_id=id).delete()
        db.session.delete(SalesInvoice.query.get_or_404(id))
        db.session.commit()
        return jsonify({'ok':True})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in sinv_delete: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


def _apply_sinv_fields(doc, f, is_new):
    """Header + line items, shared by sinv_post_and_save() -- sinv_add()/
    sinv_edit() keep their own separate inline logic unchanged (matching
    this codebase's existing SINV convention); this exists only so Post &
    Save doesn't duplicate that same logic a third time."""
    from database.zatca import engine as zengine
    so_id = int(f.get('so_id')) if f.get('so_id') else None
    so = SalesOrder.query.get(so_id) if so_id else None
    if is_new and so_id and (not so or so.status != 'Approved'):
        raise ValueError('Selected Sales Order is not Approved')

    # New documents must use one of the current 6 dropdown values (also
    # the numbering prefix -- see _next_sinv_doc_no()); editing an existing
    # one also accepts a legacy underscore-separated value already on file,
    # so an old invoice isn't forced into re-classification by an edit to
    # some unrelated field. Mirrors sinv_add()/sinv_edit()'s own checks.
    txn_type = (f.get('transaction_type') or '').strip().upper()
    valid_types = zengine.SINV_TRANSACTION_TYPES if is_new else zengine.TXN_TYPE_MAP
    if txn_type not in valid_types:
        raise ValueError('Select a valid Transaction Type.')

    status = f.get('status', 'Open')
    posting_date = pd(f.get('posting_date')) or date.today()

    from_date = pd(f.get('from_date'))
    to_date = pd(f.get('to_date'))
    if from_date and to_date and from_date > to_date:
        raise ValueError('From Date must be on or before To Date')

    doc.sales_order_id = so_id
    doc.delivery_note_id = int(f.get('dn_id')) if f.get('dn_id') else None
    doc.buyer_id = int(f.get('buyer_id')) if f.get('buyer_id') else (so.buyer_id if so else None)
    doc.buyer_ref_no = f.get('buyer_ref_no', '').strip()
    doc.transaction_type = txn_type
    doc.invoice_category = _sinv_txn_category(txn_type)
    doc.reference_invoices = f.get('reference_invoices', '').strip() or None
    doc.project_ref = f.get('project_ref', '').strip() or None
    doc.status = status
    doc.kind = f.get('kind', 'Goods')
    doc.payment_method = f.get('payment_method', 'Credit')
    doc.owner_id = int(f.get('owner_id')) if f.get('owner_id') else None
    doc.account_code = f.get('account_code', '').strip() or None
    doc.bank_account_id = int(f.get('bank_account_id')) if f.get('bank_account_id') else None
    doc.posting_date = posting_date
    doc.delivery_date = pd(f.get('delivery_date'))
    doc.document_date = date.today()
    doc.from_date = from_date
    doc.to_date = to_date
    db.session.flush()  # ensure doc.sales_invoice_id exists
    tots = _save_doc_line_items(SalesInvoiceLineItem, 'sales_invoice_id', doc.sales_invoice_id, f, 'sales')
    for k, v in tots.items(): setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
    _save_attachments('SINV', doc.sales_invoice_id, request.files.getlist('attachments'))


@sale_bp.route('/sales/invoices/next-grl-je-no')
@login_required
@permission_required_json('sale', 'sales_invoice', 'view')
def sinv_next_grl_je_no():
    """Preview the GRL No / Je No a new Sales Invoice's GRL would get,
    shown in Add mode before anything is saved -- mirrors
    pinv's own next-grl-je-no in purchase.py."""
    from models import next_je_no, NoActiveFinancialYearError
    grl_no = _next_grl_no()
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'ok': True, 'grl_no': grl_no, 'je_no': je_no})


@sale_bp.route('/sales/invoices/<int:sinv_id>/grl-build')
@login_required
@permission_required_json('sale', 'sales_invoice', 'view')
def grl_build_sinv(sinv_id):
    """Assemble the GRL view for a Sales Invoice entirely from its own
    data + Item Master -- mirrors grl_build_prn(). Frontend-only display;
    nothing is saved."""
    sinv = SalesInvoice.query.get_or_404(sinv_id)
    lines = (SalesInvoiceLineItem.query
             .filter_by(sales_invoice_id=sinv_id)
             .order_by(SalesInvoiceLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code='sales_invoice', module_code='sale', kind=sinv.kind or 'Goods')
    buyer = BuyerMaster.query.get(sinv.buyer_id) if sinv.buyer_id else None
    # Sales Invoice's record 1 is always a Debit -- see grl_preview_from_so().
    _apply_buyer_control_account(grl_lines, buyer, side='debit', kind=sinv.kind or 'Goods')
    return jsonify({
        'ok': True,
        'origion': sinv.doc_no or '',
        'posting_date': sinv.posting_date.isoformat() if sinv.posting_date else '',
        'due_date': sinv.delivery_date.isoformat() if getattr(sinv, 'delivery_date', None) else '',
        'document_date': sinv.document_date.isoformat() if getattr(sinv, 'document_date', None) else '',
        'lines': grl_lines,
    })


@sale_bp.route('/sales/invoices/<int:sinv_id>/grl')
@login_required
@permission_required_json('sale', 'sales_invoice', 'view')
def grl_get_sinv(sinv_id):
    """Return the GRL (with details) for a Sales Invoice, or an empty
    shell -- mirrors grl_get_prn()."""
    sinv = SalesInvoice.query.get_or_404(sinv_id)
    grl = GRL.query.filter_by(sales_invoice_id=sinv_id).first()
    if grl:
        return jsonify({'ok': True, 'grl': grl.to_dict()})
    return jsonify({'ok': True, 'grl': {
        'id': None, 'sales_invoice_id': sinv_id,
        'origion': sinv.doc_no or '', 'grl_no': '', 'posting_date': '',
        'due_date': '', 'document_date': '', 'narration': '', 'details': [],
    }})


@sale_bp.route('/sales/invoices/post-and-save', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_invoice', 'post')
@block_in_basic_mode_json
def sinv_post_and_save():
    """Post & Save: validate+save the Sales Invoice (Step 1), then
    create/refresh its GRL master+detail (Step 3) and linked Journal Entry
    (Step 2) -- ALL in this one transaction. Rejects re-posting a SINV
    whose posting_status is already 'Posted'. Mirrors pinv_post_and_save()
    in purchase.py exactly, keyed off GRL.sales_invoice_id /
    origin_type='SINV' / form_code='sales_invoice'.
    """
    from models import JournalEntry, JournalEntryDetail, next_je_no, NoActiveFinancialYearError
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = SalesInvoice.query.get_or_404(int(id_str))
            if doc.posting_status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Sales Invoice has already been posted.',
                    'تم ترحيل فاتورة البيع هذه مسبقاً.')}), 400
            _apply_sinv_fields(doc, f, is_new=False)
        else:
            from database.zatca import engine as zengine
            txn_type = (f.get('transaction_type') or '').strip().upper()
            if txn_type not in zengine.SINV_TRANSACTION_TYPES:
                return jsonify({'ok': False, 'error': _t(
                    'Select a valid Transaction Type.', 'يرجى اختيار نوع معاملة صالح.')}), 400
            doc = SalesInvoice(doc_no=_next_sinv_doc_no(txn_type), created_by=current_user.id)
            db.session.add(doc)
            _apply_sinv_fields(doc, f, is_new=True)

        posting_date  = pd(f.get('grl_posting_date')) or doc.posting_date or date.today()
        due_date      = pd(f.get('grl_due_date')) or doc.delivery_date
        if due_date and posting_date and posting_date > due_date:
            return jsonify({'ok': False, 'error': _t(
                'Posting Date must be on or before the Due Date',
                'يجب أن يكون تاريخ الترحيل قبل أو يساوي تاريخ الاستحقاق')}), 400

        grl = GRL.query.filter_by(sales_invoice_id=doc.sales_invoice_id).first()
        if not grl:
            grl = GRL(sales_invoice_id=doc.sales_invoice_id)
            db.session.add(grl)
        if not grl.grl_no:
            grl.grl_no = _next_grl_no()
        grl.origion       = (f.get('grl_origion','') or doc.doc_no or '').strip()
        grl.posting_date  = posting_date
        grl.due_date      = due_date
        grl.document_date = date.today()
        grl.narration     = (f.get('grl_narration','') or '').strip()
        db.session.flush()

        codes    = request.form.getlist('d_code[]')
        refcodes = request.form.getlist('d_reference_code[]')
        names    = request.form.getlist('d_account_name[]')
        names_ar = request.form.getlist('d_account_name_ar[]')
        controls = request.form.getlist('d_control_account[]')
        debits   = request.form.getlist('d_debit[]')
        credits  = request.form.getlist('d_credit[]')
        narrs    = request.form.getlist('d_narration[]')

        non_blank = [i for i in range(len(codes))
                     if (codes[i] or '').strip() or (i < len(names) and (names[i] or '').strip())]
        if not non_blank:
            return jsonify({'ok': False, 'error': _t(
                'No GRL detail records to post.', 'لا توجد سجلات لترحيلها.')}), 400
        if len(non_blank) > 2:
            return jsonify({'ok': False, 'error': 'Only two GRL detail records are allowed.'}), 400

        tot_d = sum(float(_grl_num(debits[i] if i < len(debits) else 0)) for i in range(len(codes)))
        tot_c = sum(float(_grl_num(credits[i] if i < len(credits) else 0)) for i in range(len(codes)))
        if abs(tot_d - tot_c) > 0.005:
            return jsonify({'ok': False, 'error': 'Total Debit must equal Total Credit.'}), 400

        je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
        if not je:
            je = JournalEntry(je_no=next_je_no(), origin_type='SINV', origin_id=doc.sales_invoice_id)
            db.session.add(je)
            db.session.flush()
            grl.journal_entry_id = je.id
        je.origion       = grl.origion
        je.posting_date  = grl.posting_date
        je.due_date      = grl.due_date
        je.document_date = grl.document_date
        je.narration     = grl.narration

        GRLDetail.query.filter_by(grl_id=grl.id).delete()
        JournalEntryDetail.query.filter_by(journal_entry_id=je.id).delete()
        for i in range(len(codes)):
            code = (codes[i] or '').strip()
            if not code and not (names[i] if i < len(names) else '').strip():
                continue
            reference_code  = (refcodes[i] if i < len(refcodes) else '').strip()
            account_name    = (names[i] if i < len(names) else '').strip()
            control_account = (controls[i] if i < len(controls) else '').strip()
            debit           = _grl_num(debits[i] if i < len(debits) else 0)
            credit          = _grl_num(credits[i] if i < len(credits) else 0)
            narration       = (narrs[i] if i < len(narrs) else '').strip()
            # code/control_account/reference_code are written identically
            # into both GRLDetail and JournalEntryDetail -- the two ledgers
            # must never diverge on these three fields.
            db.session.add(GRLDetail(
                grl_id=grl.id, code=code, reference_code=reference_code, account_name=account_name,
                account_name_ar=(names_ar[i] if i < len(names_ar) else '').strip(),
                control_account=control_account, debit=debit, credit=credit, narration=narration,
            ))
            db.session.add(JournalEntryDetail(
                journal_entry_id=je.id, code=code, reference_code=reference_code, account_name=account_name,
                control_account=control_account, debit=debit, credit=credit, narration=narration,
            ))

        doc.posting_status = 'Posted'
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.sales_invoice_id, 'doc_no': doc.doc_no, 'grl': grl.to_dict()})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.',
            'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in sinv_post_and_save: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/invoices/<int:id>/zatca/generate', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_invoice', 'approve')
def sinv_zatca_generate(id):
    """"Create ZATCA Invoice": builds, hashes, signs, QR-encodes, and
    attaches the UBL XML for a Posted Sales Invoice -- only ever available
    once, since re-issuing would burn a new ICV for an already-issued
    document (which ZATCA forbids). Live submission to ZATCA's Clearance/
    Reporting endpoints only happens when ZATCA_LIVE_CALLS_ENABLED is set;
    otherwise the invoice is generated and signed locally only, clearly
    flagged as such in the response."""
    from database.zatca import engine as zengine
    from database.routes.zatca import zatca_settings_query
    doc = SalesInvoice.query.get_or_404(id)

    if doc.posting_status != 'Posted':
        return jsonify({'ok': False, 'error': _t(
            'The invoice must be Posted before it can be submitted to ZATCA.',
            'يجب ترحيل الفاتورة أولاً قبل إرسالها إلى زاتكا.')}), 400
    if (doc.zatca_status or 'not_generated') not in ('not_generated', 'failed'):
        return jsonify({'ok': False, 'error': _t(
            'This invoice has already been submitted to ZATCA.',
            'تم إرسال هذه الفاتورة إلى زاتكا مسبقاً.')}), 400

    settings = zatca_settings_query().with_for_update().first()
    # active_csid() prefers a Production CSID over a Compliance one -- must
    # be used here rather than checking compliance_csid_binary directly, or
    # an account that has moved on to a real Production certificate (with
    # nothing left in the compliance_* slots) would be wrongly told to
    # complete onboarding despite already being fully onboarded.
    active_cert_type, active_binary_token, active_secret_enc = settings.active_csid() if settings else (None, None, None)
    if not active_binary_token:
        return jsonify({'ok': False, 'onboarding_required': True, 'error': _t(
            'Complete ZATCA onboarding (Compliance or Production CSID) before creating ZATCA invoices.',
            'أكمل عملية التسجيل في زاتكا (شهادة الامتثال أو الإنتاج) قبل إنشاء فواتير زاتكا.')}), 400

    referenced_uuid = None
    if doc.reference_invoices:
        ref_ids = [int(x) for x in doc.reference_invoices.split(',') if x.strip().isdigit()]
        for ref_id in ref_ids:
            ref_doc = SalesInvoice.query.get(ref_id)
            if not ref_doc or not ref_doc.zatca_uuid:
                return jsonify({'ok': False, 'error': _t(
                    'Every referenced invoice must already have its own ZATCA UUID before this Credit/Debit Note can be generated.',
                    'يجب أن تحتوي كل فاتورة مرجعية على معرف زاتكا الخاص بها قبل إنشاء إشعار الدائن/المدين هذا.')}), 400
            referenced_uuid = ref_doc.zatca_uuid

    items = SalesInvoiceLineItem.query.filter_by(sales_invoice_id=id).order_by(SalesInvoiceLineItem.line_number).all()
    if not items:
        return jsonify({'ok': False, 'error': _t('This invoice has no line items.', 'لا تحتوي هذه الفاتورة على بنود.')}), 400

    owner = doc.owner or Owner.query.first()
    buyer = doc.buyer

    icv = int(settings.last_icv or 0) + 1
    pih = settings.last_invoice_hash or zengine.genesis_hash()
    uuid_str = zengine.new_uuid()

    try:
        private_key_pem = zengine.decrypt_secret(settings.private_key_pem_enc, current_app.config['SECRET_KEY'])
        if not private_key_pem:
            raise ValueError('No signing key on file -- generate a keypair/CSR in ZATCA Settings first')
        private_key = zengine.load_private_key_from_pem(private_key_pem)
        root, submission_type = zengine.build_invoice_xml(doc, items, owner, buyer, settings, uuid_str, icv, pih,
                                                            referenced_uuid=referenced_uuid)
        finalized, invoice_hash, signature_b64 = zengine.sign_and_finalize(root, private_key, active_binary_token)
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('ZATCA XML build/sign failed for SalesInvoice %s', id)
        return jsonify({'ok': False, 'error': f'Failed to build/sign the ZATCA invoice: {e}'}), 500

    try:
        from database.routes.purchase import _zatca_qr_payload, _qr_image_b64
        qr_extra = zengine.phase2_qr_extra_tags(
            invoice_hash, signature_b64,
            settings.cert_public_key_b64 or '', settings.cert_ca_signature_b64 or '')
        ts_date = doc.document_date or doc.posting_date or date.today()
        timestamp_iso = datetime.combine(ts_date, datetime.min.time()).strftime('%Y-%m-%dT%H:%M:%SZ')
        seller_name = owner.name if owner else ''
        seller_vat = (owner.vat_number if owner else '') or ''
        total = float(doc.total_incl_vat or 0)
        vat_amount = float(doc.vat_amount or 0)
        phase2_qr_payload = _zatca_qr_payload(
            seller_name=seller_name, vat_number=seller_vat,
            timestamp_iso=timestamp_iso, total=total,
            vat_amount=vat_amount, extra_tags=qr_extra)
        # Embedded in the XML itself (not just rendered for print) --
        # must happen before serialize(), and uses this exact same
        # base64 payload so the embedded and printed QR always match.
        zengine.set_qr_reference(finalized, phase2_qr_payload)
        xml_bytes = zengine.serialize(finalized)

        attachment = _save_generated_attachment('SINVZ', doc.sales_invoice_id,
                                                 f'{doc.doc_no or ("SINV-" + str(id))}.xml', xml_bytes)

        doc.zatca_uuid = uuid_str
        doc.zatca_icv = icv
        doc.zatca_pih = pih
        doc.zatca_invoice_hash = invoice_hash
        doc.zatca_xml_signature = signature_b64
        doc.zatca_submission_type = submission_type
        doc.zatca_attachment_id = attachment.id
        doc.zatca_generated_at = datetime.utcnow()

        if current_app.config.get('ZATCA_LIVE_CALLS_ENABLED'):
            secret = zengine.decrypt_secret(active_secret_enc, current_app.config['SECRET_KEY'])
            try:
                if doc.zatca_submission_type == 'clearance':
                    status_code, resp_json = zengine.submit_clearance(settings.environment, xml_bytes, uuid_str, active_binary_token, secret)
                else:
                    status_code, resp_json = zengine.submit_reporting(settings.environment, xml_bytes, uuid_str, active_binary_token, secret)
                doc.zatca_status = 'cleared' if status_code < 300 else 'failed'
                doc.zatca_response_message = str(resp_json)[:4000]
                doc.zatca_submitted_at = datetime.utcnow()
            except Exception as e:
                doc.zatca_status = 'failed'
                doc.zatca_response_message = f'Live submission error: {e}'
        else:
            doc.zatca_status = 'generated'
            doc.zatca_response_message = _t(
                'Generated and signed locally only; live submission to ZATCA is disabled (Phase 2).',
                'تم الإنشاء والتوقيع محلياً فقط؛ الإرسال المباشر إلى زاتكا معطل (المرحلة الثانية).')

        if doc.zatca_status == 'failed':
            # ZATCA did not approve this invoice -- the QR must not display
            # signature/hash tags implying a clearance that never happened.
            # Fall back to the plain Phase-1 QR; the signed XML stays
            # attached either way as the record of what was attempted.
            qr_payload = _zatca_qr_payload(
                seller_name=seller_name, vat_number=seller_vat,
                timestamp_iso=timestamp_iso, total=total, vat_amount=vat_amount)
        else:
            qr_payload = phase2_qr_payload
        doc.zatca_qr_code = qr_payload
        qr_b64 = _qr_image_b64(qr_payload)

        settings.last_icv = icv
        settings.last_invoice_hash = invoice_hash
        settings.updated_at = datetime.utcnow()
        settings.updated_by = current_user.id

        db.session.commit()
        return jsonify({
            'ok': True, 'uuid': uuid_str, 'icv': icv, 'status': doc.zatca_status,
            'attachment_id': attachment.id, 'qr_b64': qr_b64, 'message': doc.zatca_response_message,
        })
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('ZATCA submission failed for SalesInvoice %s', id)
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# SALES RETURN REQUESTS (SRR)
# ══════════════════════════════════════════════════════════════════

# A return request only makes sense against the original invoiced goods --
# Debit/Credit Note invoices (STD-DR/STD-CR/SIM-DR/SIM-CR, plus their legacy
# underscore-separated aliases, never migrated) are themselves adjustments
# and must never be selectable as the source of an SRR.
SRR_INVOICE_ONLY_TYPES = ('STD-INV', 'SIM-INV', 'STD_INV', 'SMP_INV')

@sale_bp.route('/sales/returns')
@login_required
@permission_required('sale', 'sales_return_request', 'view')
def srr_list():
    sins = [{'id':p.sales_invoice_id,'doc_no':p.doc_no} for p in SalesInvoice.query
            .filter_by(status='Approved')
            .filter(SalesInvoice.transaction_type.in_(SRR_INVOICE_ONLY_TYPES))
            .order_by(SalesInvoice.sales_invoice_id.desc()).all()]
    return render_template('sales/srr_list.html', buyers=_buyer_list(), sinvs=sins, owners=Owner.query.order_by(Owner.name).all())


@sale_bp.route('/sales/returns/data')
@login_required
@permission_required_json('sale', 'sales_return_request', 'view')
def srr_data():
    return jsonify([r.to_dict() for r in SalesReturnRequest.query.order_by(SalesReturnRequest.sales_return_request_id.desc()).all()])


@sale_bp.route('/sales/returns/<int:id>/json')
@login_required
@permission_required_json('sale', 'sales_return_request', 'view')
def srr_json(id):
    doc = SalesReturnRequest.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in SalesReturnLineItem.query.filter_by(sales_return_request_id=id).order_by(SalesReturnLineItem.line_number).all()]
    d['attachments'] = [{'filename':a.filename} for a in SalesAttachment.query.filter_by(doc_type='SRR', doc_id=id).all()]
    return jsonify(d)


@sale_bp.route('/sales/returns/<int:id>/view')
@login_required
@permission_required('sale', 'sales_return_request', 'view')
def srr_view(id):
    doc = SalesReturnRequest.query.get_or_404(id)
    return render_template('sales/srr_view.html', doc=doc,
        items=SalesReturnLineItem.query.filter_by(sales_return_request_id=id).order_by(SalesReturnLineItem.line_number).all(),
        attachments=SalesAttachment.query.filter_by(doc_type='SRR', doc_id=id).all())


@sale_bp.route('/sales/returns/<int:id>/summary')
@login_required
@permission_required_json('sale', 'sales_return_request', 'view')
def srr_summary(id):
    doc = SalesReturnRequest.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in SalesReturnLineItem.query.filter_by(sales_return_request_id=id).order_by(SalesReturnLineItem.line_number).all()]
    return jsonify(d)


def _validate_srr_qty(f, sales_invoice_id):
    """SRR line quantities must not exceed the original Sales Invoice line quantity for that item."""
    if not sales_invoice_id:
        return None
    src_lines = SalesInvoiceLineItem.query.filter_by(sales_invoice_id=sales_invoice_id).all()
    max_by_code = {}
    for li in src_lines:
        code = (li.item_code or '').strip()
        max_by_code[code] = max_by_code.get(code, Decimal(0)) + Decimal(li.quantity or 0)

    codes = f.getlist('li_item_code[]')
    qtys  = f.getlist('li_qty[]')
    for i in range(len(qtys)):
        code = (codes[i] if i < len(codes) else '').strip()
        try:
            qty = Decimal(qtys[i] or '0')
        except Exception:
            qty = Decimal(0)
        if qty <= 0:
            continue
        if code not in max_by_code:
            return f'Item "{code}" is not part of the selected Sales Invoice'
        if qty > max_by_code[code]:
            return f'Return quantity for "{code}" ({qty}) exceeds invoiced quantity ({max_by_code[code]})'
    return None


@sale_bp.route('/sales/returns/add', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_return_request', 'add')
def srr_add():
    try:
        f = request.form
        si_id = int(f.get('si_id')) if f.get('si_id') else None
        sinv = SalesInvoice.query.get(si_id) if si_id else None
        if si_id and (not sinv or sinv.status != 'Approved'):
            return jsonify({'ok': False, 'error': 'Selected Sales Invoice is not Approved'}), 400
        if sinv and sinv.transaction_type not in SRR_INVOICE_ONLY_TYPES:
            return jsonify({'ok': False, 'error': 'A Return Request cannot be filed against a Debit/Credit Note invoice'}), 400

        err = _validate_srr_qty(f, si_id)
        if err:
            return jsonify({'ok': False, 'error': err}), 400

        delivery_date = pd(f.get('delivery_date'))
        if delivery_date and delivery_date < date.today():
            return jsonify({'ok': False, 'error': 'Delivery date must be on/after the document date'}), 400

        doc = SalesReturnRequest(
            doc_no=_next_doc_no('SRR', SalesReturnRequest),
            sales_invoice_id=si_id,
            buyer_id=int(f.get('buyer_id')) if f.get('buyer_id') else None,
            contact_person=f.get('contact_person','').strip(),
            buyer_ref_no=f.get('buyer_ref_no','').strip(),
            owner_id=int(f.get('owner_id')) if f.get('owner_id') else None,
            account_code=f.get('account_code','').strip() or None,
            status=f.get('status','Open'),
            kind=f.get('kind','Goods'),
            posting_date=pd(f.get('posting_date')) or date.today(),
            delivery_date=delivery_date,
            document_date=date.today(),
            created_by=current_user.id,
        )
        db.session.add(doc)
        db.session.flush()
        tots = _save_doc_line_items(SalesReturnLineItem, 'sales_return_request_id', doc.sales_return_request_id, f, 'sales')
        for k,v in tots.items(): 
            setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
        _save_attachments('SRR', doc.sales_return_request_id, request.files.getlist('attachments'))
        db.session.commit()
        return jsonify({'ok':True,'id':doc.sales_return_request_id,'doc_no':doc.doc_no})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in srr_add: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/returns/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_return_request', 'edit')
def srr_edit(id):
    try:
        doc = SalesReturnRequest.query.get_or_404(id)
        f = request.form
        si_id = int(f.get('si_id')) if f.get('si_id') else None
        sinv = SalesInvoice.query.get(si_id) if si_id else None
        if sinv and sinv.transaction_type not in SRR_INVOICE_ONLY_TYPES:
            return jsonify({'ok': False, 'error': 'A Return Request cannot be filed against a Debit/Credit Note invoice'}), 400

        err = _validate_srr_qty(f, si_id)
        if err:
            return jsonify({'ok': False, 'error': err}), 400

        delivery_date = pd(f.get('delivery_date'))
        if delivery_date and delivery_date < date.today():
            return jsonify({'ok': False, 'error': 'Delivery date must be on/after the document date'}), 400

        doc.sales_invoice_id=si_id
        doc.buyer_id=int(f.get('buyer_id')) if f.get('buyer_id') else None
        doc.contact_person=f.get('contact_person','').strip()
        doc.buyer_ref_no=f.get('buyer_ref_no','').strip()
        doc.owner_id = int(f.get('owner_id')) if f.get('owner_id') else None
        doc.account_code = f.get('account_code','').strip() or None
        doc.status=f.get('status','Open')
        doc.kind=f.get('kind','Goods')
        doc.posting_date  = pd(f.get('posting_date')) or date.today()
        doc.delivery_date = delivery_date
        doc.document_date = date.today()
        tots = _save_doc_line_items(SalesReturnLineItem, 'sales_return_request_id', id, f, 'sales')
        for k,v in tots.items(): 
            setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
        _save_attachments('SRR', id, request.files.getlist('attachments'))
        db.session.commit()
        return jsonify({'ok':True})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in srr_edit: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/returns/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_return_request', 'delete')
def srr_delete(id):
    SalesReturnLineItem.query.filter_by(sales_return_request_id=id).delete()
    SalesAttachment.query.filter_by(doc_type='SRR', doc_id=id).delete()
    db.session.delete(SalesReturnRequest.query.get_or_404(id))
    db.session.commit()
    return jsonify({'ok':True})


# ══════════════════════════════════════════════════════════════════
# SALE RETURN NOTE (SRN)
#   Sits between Sales Return Request and Sales Credit Memo:
#   SRR -> Sale Return Note -> Sales Credit Memo
#   Mirrors PURCHASE RETURN NOTE (purchase.py), including GL posting.
# ══════════════════════════════════════════════════════════════════

@sale_bp.route('/sales/return-notes')
@login_required
@permission_required('sale', 'sales_return_note', 'view')
def srn_list():
    srrs = [{'id':p.sales_return_request_id,'doc_no':p.doc_no} for p in SalesReturnRequest.query.filter_by(status='Approved').order_by(SalesReturnRequest.sales_return_request_id.desc()).all()]
    return render_template('sales/srn_list.html', buyers=_buyer_list(), srrs=srrs, owners=Owner.query.order_by(Owner.name).all())

@sale_bp.route('/sales/return-notes/data')
@login_required
@permission_required_json('sale', 'sales_return_note', 'view')
def srn_data():
    return jsonify([r.to_dict() for r in SalesReturnNote.query.order_by(SalesReturnNote.sales_return_note_id.desc()).all()])

@sale_bp.route('/sales/return-notes/<int:id>/json')
@login_required
@permission_required_json('sale', 'sales_return_note', 'view')
def srn_json(id):
    doc = SalesReturnNote.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in SalesReturnNoteLineItem.query.filter_by(sales_return_note_id=id).order_by(SalesReturnNoteLineItem.line_number).all()]
    d['attachments'] = [{'id':a.id,'filename':a.filename} for a in SalesAttachment.query.filter_by(doc_type='SRN', doc_id=id).all()]
    return jsonify(d)

@sale_bp.route('/sales/return-notes/<int:id>/view')
@login_required
@permission_required('sale', 'sales_return_note', 'view')
def srn_view(id):
    doc = SalesReturnNote.query.get_or_404(id)
    return render_template('sales/srn_view.html', doc=doc,
        items=SalesReturnNoteLineItem.query.filter_by(sales_return_note_id=id).order_by(SalesReturnNoteLineItem.line_number).all(),
        attachments=SalesAttachment.query.filter_by(doc_type='SRN', doc_id=id).all())

@sale_bp.route('/sales/return-notes/<int:id>/summary')
@login_required
@permission_required_json('sale', 'sales_return_note', 'view')
def srn_summary(id):
    doc = SalesReturnNote.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in SalesReturnNoteLineItem.query.filter_by(sales_return_note_id=id).order_by(SalesReturnNoteLineItem.line_number).all()]
    return jsonify(d)

def _apply_srn_fields(doc, f, is_new):
    """Header + line items, shared by srn_add/srn_edit. Mirrors
    _apply_prn_fields() in purchase.py."""
    srr_id = int(f.get('srr_id')) if f.get('srr_id') else None
    srr = SalesReturnRequest.query.get(srr_id) if srr_id else None
    if is_new and srr_id and (not srr or srr.status != 'Approved'):
        raise ValueError('Selected Sales Return Request is not Approved')

    doc.sales_return_request_id = srr_id
    doc.buyer_id          = int(f.get('buyer_id')) if f.get('buyer_id') else None
    doc.contact_person    = f.get('contact_person', '').strip()
    doc.buyer_ref         = f.get('buyer_ref', '').strip()
    doc.owner_id          = int(f.get('owner_id')) if f.get('owner_id') else None
    doc.account_code      = srr.account_code if srr else (f.get('account_code', '').strip() or None)
    doc.status            = f.get('status', 'Open')
    doc.kind              = srr.kind if srr else f.get('kind', 'Goods')
    doc.posting_date      = pd(f.get('posting_date')) or date.today()
    doc.delivery_date     = pd(f.get('delivery_date'))
    doc.document_date     = date.today()
    db.session.flush()   # ensure doc.sales_return_note_id exists
    tots = _save_doc_line_items(SalesReturnNoteLineItem, 'sales_return_note_id', doc.sales_return_note_id, f, 'sales')
    for k, v in tots.items(): setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
    _save_attachments('SRN', doc.sales_return_note_id, request.files.getlist('attachments'))


@sale_bp.route('/sales/return-notes/add', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_return_note', 'add')
def srn_add():
    try:
        doc = SalesReturnNote(doc_no=_next_doc_no('SRN', SalesReturnNote), created_by=current_user.id)
        db.session.add(doc)
        _apply_srn_fields(doc, request.form, is_new=True)
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.sales_return_note_id, 'doc_no': doc.doc_no})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in srn_add: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@sale_bp.route('/sales/return-notes/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_return_note', 'edit')
def srn_edit(id):
    try:
        doc = SalesReturnNote.query.get_or_404(id)
        _apply_srn_fields(doc, request.form, is_new=False)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in srn_edit: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@sale_bp.route('/sales/return-notes/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_return_note', 'delete')
def srn_delete(id):
    SalesReturnNoteLineItem.query.filter_by(sales_return_note_id=id).delete()
    SalesAttachment.query.filter_by(doc_type='SRN', doc_id=id).delete()
    GRL.query.filter_by(sales_return_note_id=id).delete()
    db.session.delete(SalesReturnNote.query.get_or_404(id)); db.session.commit()
    return jsonify({'ok':True})


@sale_bp.route('/sales/return-notes/next-grl-je-no')
@login_required
@permission_required_json('sale', 'sales_return_note', 'view')
def srn_next_grl_je_no():
    """Preview the GRL No / Je No a new Sale Return Note's GRL would get,
    shown in Add mode before anything is saved -- mirrors
    prn_next_grl_je_no()."""
    from models import next_je_no, NoActiveFinancialYearError
    grl_no = _next_grl_no()
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'ok': True, 'grl_no': grl_no, 'je_no': je_no})


@sale_bp.route('/sales/return-notes/<int:srn_id>/grl-build')
@login_required
@permission_required_json('sale', 'sales_return_note', 'view')
def grl_build_srn(srn_id):
    """Assemble the GRL view for a Sale Return Note entirely from SRN data
    + Item Master -- mirrors grl_build_prn(). Frontend-only display;
    nothing is saved."""
    srn = SalesReturnNote.query.get_or_404(srn_id)
    lines = (SalesReturnNoteLineItem.query
             .filter_by(sales_return_note_id=srn_id)
             .order_by(SalesReturnNoteLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code='sales_return_note', module_code='sale', kind=srn.kind or 'Goods')
    buyer = BuyerMaster.query.get(srn.buyer_id) if srn.buyer_id else None
    _apply_buyer_control_account(grl_lines, buyer, side='credit', kind=srn.kind or 'Goods')
    return jsonify({
        'ok': True,
        'origion': srn.doc_no or '',
        'posting_date': srn.posting_date.isoformat() if srn.posting_date else '',
        'due_date': srn.delivery_date.isoformat() if getattr(srn, 'delivery_date', None) else '',
        'document_date': srn.document_date.isoformat() if getattr(srn, 'document_date', None) else '',
        'lines': grl_lines,
    })


@sale_bp.route('/sales/return-notes/<int:srn_id>/grl')
@login_required
@permission_required_json('sale', 'sales_return_note', 'view')
def grl_get_srn(srn_id):
    """Return the GRL (with details) for a Sale Return Note, or an empty
    shell -- mirrors grl_get_prn()."""
    srn = SalesReturnNote.query.get_or_404(srn_id)
    grl = GRL.query.filter_by(sales_return_note_id=srn_id).first()
    if grl:
        return jsonify({'ok': True, 'grl': grl.to_dict()})
    return jsonify({'ok': True, 'grl': {
        'id': None, 'sales_return_note_id': srn_id,
        'origion': srn.doc_no or '', 'grl_no': '', 'posting_date': '',
        'due_date': '', 'document_date': '', 'narration': '', 'details': [],
    }})


@sale_bp.route('/sales/returns/<int:srr_id>/grl-preview')
@login_required
@permission_required_json('sale', 'sales_return_note', 'view')
def grl_preview_from_srr(srr_id):
    """Preview the GRL records for a not-yet-saved Sale Return Note (or,
    via ?form=sales_credit_memo, a not-yet-saved Sales Credit Memo created
    directly from this Sales Return Request), shown in Add mode as soon as
    its source is selected -- mirrors grl_preview_from_grr() in
    purchase.py."""
    srr = SalesReturnRequest.query.get_or_404(srr_id)
    form_code = request.args.get('form', 'sales_return_note')
    lines = (SalesReturnLineItem.query
             .filter_by(sales_return_request_id=srr_id)
             .order_by(SalesReturnLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code=form_code, module_code='sale', kind=srr.kind or 'Goods')
    buyer = BuyerMaster.query.get(srr.buyer_id) if getattr(srr, 'buyer_id', None) else None
    # Both Sale Return Note's and Sales Credit Memo's Auto Code Selection
    # nature is Debit, so their overridden record 1 posts as a Credit
    # (opposite side) either way -- see _apply_buyer_control_account()'s
    # docstring.
    _apply_buyer_control_account(grl_lines, buyer, side='credit', kind=srr.kind or 'Goods')
    return jsonify({'ok': True, 'lines': grl_lines})


@sale_bp.route('/sales/return-notes/<int:srn_id>/grl-preview')
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'view')
def grl_preview_from_srn(srn_id):
    """Preview the GRL records for a not-yet-saved Sales Credit Memo, shown
    in Add mode as soon as its source Sales Return Note is selected --
    mirrors grl_preview_from_prn() in purchase.py."""
    srn = SalesReturnNote.query.get_or_404(srn_id)
    form_code = request.args.get('form', 'sales_credit_memo')
    lines = (SalesReturnNoteLineItem.query
             .filter_by(sales_return_note_id=srn_id)
             .order_by(SalesReturnNoteLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code=form_code, module_code='sale', kind=srn.kind or 'Goods')
    buyer = BuyerMaster.query.get(srn.buyer_id) if getattr(srn, 'buyer_id', None) else None
    _apply_buyer_control_account(grl_lines, buyer, side='credit', kind=srn.kind or 'Goods')
    return jsonify({'ok': True, 'lines': grl_lines})


@sale_bp.route('/sales/return-notes/post-and-save', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_return_note', 'post')
@block_in_basic_mode_json
def srn_post_and_save():
    """Post & Save: validate+save the Sale Return Note (Step 1), then
    create/refresh its GRL master+detail (Step 3) and linked Journal Entry
    (Step 2) -- ALL in this one transaction, so a failure anywhere rolls
    back everything (no partially-posted document). Rejects re-posting a
    SRN whose posting_status is already 'Posted'. Mirrors
    prn_post_and_save() exactly, just keyed off GRL.sales_return_note_id /
    origin_type='SRN' / form_code='sales_return_note'.
    """
    from models import JournalEntry, JournalEntryDetail, next_je_no, NoActiveFinancialYearError
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = SalesReturnNote.query.get_or_404(int(id_str))
            if doc.posting_status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Sale Return Note has already been posted.',
                    'تم ترحيل مذكرة إرجاع البيع هذه مسبقاً.')}), 400
            _apply_srn_fields(doc, f, is_new=False)
        else:
            doc = SalesReturnNote(doc_no=_next_doc_no('SRN', SalesReturnNote), created_by=current_user.id)
            db.session.add(doc)
            _apply_srn_fields(doc, f, is_new=True)

        posting_date  = pd(f.get('grl_posting_date')) or doc.posting_date or date.today()
        due_date      = pd(f.get('grl_due_date')) or doc.delivery_date
        if due_date and posting_date and posting_date > due_date:
            return jsonify({'ok': False, 'error': _t(
                'Posting Date must be on or before the Due Date',
                'يجب أن يكون تاريخ الترحيل قبل أو يساوي تاريخ الاستحقاق')}), 400

        grl = GRL.query.filter_by(sales_return_note_id=doc.sales_return_note_id).first()
        if not grl:
            grl = GRL(sales_return_note_id=doc.sales_return_note_id)
            db.session.add(grl)
        if not grl.grl_no:
            grl.grl_no = _next_grl_no()
        grl.origion       = (f.get('grl_origion','') or doc.doc_no or '').strip()
        grl.posting_date  = posting_date
        grl.due_date      = due_date
        grl.document_date = date.today()
        grl.narration     = (f.get('grl_narration','') or '').strip()
        db.session.flush()

        codes     = request.form.getlist('d_code[]')
        names     = request.form.getlist('d_account_name[]')
        names_ar  = request.form.getlist('d_account_name_ar[]')
        controls  = request.form.getlist('d_control_account[]')
        refcodes  = request.form.getlist('d_reference_code[]')
        debits    = request.form.getlist('d_debit[]')
        credits   = request.form.getlist('d_credit[]')
        narrs     = request.form.getlist('d_narration[]')

        non_blank = [i for i in range(len(codes))
                     if (codes[i] or '').strip() or (i < len(names) and (names[i] or '').strip())]
        if not non_blank:
            return jsonify({'ok': False, 'error': _t(
                'No GRL detail records to post.', 'لا توجد سجلات لترحيلها.')}), 400
        if len(non_blank) > 2:
            return jsonify({'ok': False, 'error': 'Only two GRL detail records are allowed.'}), 400

        tot_d = sum(float(_grl_num(debits[i] if i < len(debits) else 0)) for i in range(len(codes)))
        tot_c = sum(float(_grl_num(credits[i] if i < len(credits) else 0)) for i in range(len(codes)))
        if abs(tot_d - tot_c) > 0.005:
            return jsonify({'ok': False, 'error': 'Total Debit must equal Total Credit.'}), 400

        je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
        if not je:
            je = JournalEntry(je_no=next_je_no(), origin_type='SRN', origin_id=doc.sales_return_note_id)
            db.session.add(je)
            db.session.flush()
            grl.journal_entry_id = je.id
        je.origion       = grl.origion
        je.posting_date  = grl.posting_date
        je.due_date      = grl.due_date
        je.document_date = grl.document_date
        je.narration     = grl.narration

        GRLDetail.query.filter_by(grl_id=grl.id).delete()
        JournalEntryDetail.query.filter_by(journal_entry_id=je.id).delete()
        for i in range(len(codes)):
            code = (codes[i] or '').strip()
            if not code and not (names[i] if i < len(names) else '').strip():
                continue
            account_name    = (names[i] if i < len(names) else '').strip()
            control_account = (controls[i] if i < len(controls) else '').strip()
            reference_code  = (refcodes[i] if i < len(refcodes) else '').strip()
            debit           = _grl_num(debits[i] if i < len(debits) else 0)
            credit          = _grl_num(credits[i] if i < len(credits) else 0)
            narration       = (narrs[i] if i < len(narrs) else '').strip()
            db.session.add(GRLDetail(
                grl_id=grl.id, code=code, account_name=account_name,
                account_name_ar=(names_ar[i] if i < len(names_ar) else '').strip(),
                control_account=control_account, reference_code=reference_code,
                debit=debit, credit=credit, narration=narration,
            ))
            db.session.add(JournalEntryDetail(
                journal_entry_id=je.id, code=code, account_name=account_name,
                control_account=control_account, reference_code=reference_code,
                debit=debit, credit=credit, narration=narration,
            ))

        doc.posting_status = 'Posted'
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.sales_return_note_id, 'doc_no': doc.doc_no, 'grl': grl.to_dict()})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.',
            'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in srn_post_and_save: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# SALES CREDIT MEMOS (SCM)
# ══════════════════════════════════════════════════════════════════

@sale_bp.route('/sales/credit-memos')
@login_required
@permission_required('sale', 'sales_credit_memo', 'view')
def scm_list():
    srns = [{'id':p.sales_return_note_id,'doc_no':p.doc_no,'sales_return_request_id':p.sales_return_request_id}
            for p in SalesReturnNote.query.filter_by(status='Approved').order_by(SalesReturnNote.sales_return_note_id.desc()).all()]
    return render_template('sales/scm_list.html', buyers=_buyer_list(), srns=srns, owners=Owner.query.order_by(Owner.name).all())


@sale_bp.route('/sales/credit-memos/data')
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'view')
def scm_data():
    return jsonify([r.to_dict() for r in SalesCreditMemo.query.order_by(SalesCreditMemo.sales_credit_memo_id.desc()).all()])


@sale_bp.route('/sales/credit-memos/<int:id>/json')
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'view')
def scm_json(id):
    doc = SalesCreditMemo.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in SalesCreditMemoLineItem.query.filter_by(sales_credit_memo_id=id).order_by(SalesCreditMemoLineItem.line_number).all()]
    d['attachments'] = [{'filename':a.filename} for a in SalesAttachment.query.filter_by(doc_type='SCM', doc_id=id).all()]
    return jsonify(d)


@sale_bp.route('/sales/credit-memos/<int:id>/view')
@login_required
@permission_required('sale', 'sales_credit_memo', 'view')
def scm_view(id):
    doc = SalesCreditMemo.query.get_or_404(id)
    return render_template('sales/scm_view.html', doc=doc,
        items=SalesCreditMemoLineItem.query.filter_by(sales_credit_memo_id=id).order_by(SalesCreditMemoLineItem.line_number).all(),
        attachments=SalesAttachment.query.filter_by(doc_type='SCM', doc_id=id).all())


@sale_bp.route('/sales/credit-memos/add', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'add')
def scm_add():
    try:
        f = request.form
        srn_id = int(f.get('srn_id')) if f.get('srn_id') else None
        srn = SalesReturnNote.query.get(srn_id) if srn_id else None
        if srn_id and (not srn or srn.status != 'Approved'):
            return jsonify({'ok': False, 'error': 'Selected Sales Return Note is not Approved'}), 400

        doc = SalesCreditMemo(
            doc_no=_next_doc_no('SCM', SalesCreditMemo),
            sales_return_note_id=srn_id,
            sales_return_request_id=(srn.sales_return_request_id if srn else None),
            sales_invoice_id=(srn.sales_return_request.sales_invoice_id
                               if srn and srn.sales_return_request else None),
            buyer_id=int(f.get('buyer_id')) if f.get('buyer_id') else None,
            contact_person=f.get('contact_person','').strip(),
            buyer_ref_no=f.get('buyer_ref_no','').strip(),
            status=f.get('status','Open'),
            kind=f.get('kind','Goods'),
            payment_method=f.get('payment_method','Credit'),
            owner_id=int(f.get('owner_id')) if f.get('owner_id') else None,
            account_code=f.get('account_code','').strip() or None,
            bank_account_id=int(f.get('bank_account_id')) if f.get('bank_account_id') else None,
            posting_date=pd(f.get('posting_date')) or date.today(),
            delivery_date=pd(f.get('delivery_date')),
            document_date=date.today(),
            created_by=current_user.id,
        )
        db.session.add(doc)
        db.session.flush()
        tots = _save_doc_line_items(SalesCreditMemoLineItem, 'sales_credit_memo_id', doc.sales_credit_memo_id, f, 'sales')
        for k,v in tots.items(): 
            setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
        _save_attachments('SCM', doc.sales_credit_memo_id, request.files.getlist('attachments'))
        db.session.commit()
        return jsonify({'ok':True,'id':doc.sales_credit_memo_id,'doc_no':doc.doc_no})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in scm_add: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/credit-memos/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'edit')
def scm_edit(id):
    try:
        doc = SalesCreditMemo.query.get_or_404(id)
        f = request.form
        srn_id = int(f.get('srn_id')) if f.get('srn_id') else None
        srn = SalesReturnNote.query.get(srn_id) if srn_id else None

        doc.sales_return_note_id = srn_id
        doc.sales_return_request_id = (srn.sales_return_request_id if srn else None)
        doc.sales_invoice_id = (srn.sales_return_request.sales_invoice_id
                                 if srn and srn.sales_return_request else None)
        doc.buyer_id=int(f.get('buyer_id')) if f.get('buyer_id') else None
        doc.contact_person=f.get('contact_person','').strip()
        doc.buyer_ref_no=f.get('buyer_ref_no','').strip()
        doc.status=f.get('status','Open')
        doc.kind=f.get('kind','Goods')
        doc.payment_method = f.get('payment_method','Credit')
        doc.owner_id = int(f.get('owner_id')) if f.get('owner_id') else None
        doc.account_code = f.get('account_code','').strip() or None
        doc.bank_account_id = int(f.get('bank_account_id')) if f.get('bank_account_id') else None
        doc.posting_date  = pd(f.get('posting_date')) or date.today()
        doc.delivery_date = pd(f.get('delivery_date'))
        doc.document_date = date.today()
        tots = _save_doc_line_items(SalesCreditMemoLineItem, 'sales_credit_memo_id', id, f, 'sales')
        for k,v in tots.items(): 
            setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
        _save_attachments('SCM', id, request.files.getlist('attachments'))
        db.session.commit()
        return jsonify({'ok':True})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in scm_edit: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@sale_bp.route('/sales/credit-memos/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'delete')
def scm_delete(id):
    SalesCreditMemoLineItem.query.filter_by(sales_credit_memo_id=id).delete()
    SalesAttachment.query.filter_by(doc_type='SCM', doc_id=id).delete()
    GRL.query.filter_by(sales_credit_memo_id=id).delete()
    db.session.delete(SalesCreditMemo.query.get_or_404(id))
    db.session.commit()
    return jsonify({'ok':True})


def _apply_scm_fields(doc, f, is_new):
    """Header + line items, shared by scm_post_and_save() -- scm_add()/
    scm_edit() keep their own separate inline logic unchanged, matching
    the existing SCM convention; this exists only so Post & Save doesn't
    duplicate that logic a third time. Mirrors _apply_pdm_fields() in
    purchase.py."""
    srn_id = int(f.get('srn_id')) if f.get('srn_id') else None
    srn = SalesReturnNote.query.get(srn_id) if srn_id else None

    doc.sales_return_note_id = srn_id
    doc.sales_return_request_id = (srn.sales_return_request_id if srn else None)
    doc.sales_invoice_id = (srn.sales_return_request.sales_invoice_id
                             if srn and srn.sales_return_request else None)
    doc.buyer_id = int(f.get('buyer_id')) if f.get('buyer_id') else None
    doc.contact_person = f.get('contact_person', '').strip()
    doc.buyer_ref_no = f.get('buyer_ref_no', '').strip()
    doc.status = f.get('status', 'Open')
    doc.kind = f.get('kind', 'Goods')
    doc.payment_method = f.get('payment_method', 'Credit')
    doc.owner_id = int(f.get('owner_id')) if f.get('owner_id') else None
    doc.account_code = f.get('account_code', '').strip() or None
    doc.bank_account_id = int(f.get('bank_account_id')) if f.get('bank_account_id') else None
    doc.posting_date = pd(f.get('posting_date')) or date.today()
    doc.delivery_date = pd(f.get('delivery_date'))
    doc.document_date = date.today()
    db.session.flush()  # ensure doc.sales_credit_memo_id exists
    tots = _save_doc_line_items(SalesCreditMemoLineItem, 'sales_credit_memo_id', doc.sales_credit_memo_id, f, 'sales')
    for k, v in tots.items(): setattr(doc, k, float(v) if isinstance(v, Decimal) else v)
    _save_attachments('SCM', doc.sales_credit_memo_id, request.files.getlist('attachments'))


@sale_bp.route('/sales/credit-memos/next-grl-je-no')
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'view')
def scm_next_grl_je_no():
    """Preview the GRL No / Je No a new Sales Credit Memo's GRL would get,
    shown in Add mode before anything is saved -- mirrors PDM's own in
    purchase.py."""
    from models import next_je_no, NoActiveFinancialYearError
    grl_no = _next_grl_no()
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'ok': True, 'grl_no': grl_no, 'je_no': je_no})


@sale_bp.route('/sales/credit-memos/<int:scm_id>/grl-build')
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'view')
def grl_build_scm(scm_id):
    """Assemble the GRL view for a Sales Credit Memo entirely from its own
    data + Item Master -- mirrors grl_build_prn(). Frontend-only display;
    nothing is saved."""
    scm = SalesCreditMemo.query.get_or_404(scm_id)
    lines = (SalesCreditMemoLineItem.query
             .filter_by(sales_credit_memo_id=scm_id)
             .order_by(SalesCreditMemoLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code='sales_credit_memo', module_code='sale', kind=scm.kind or 'Goods')
    buyer = BuyerMaster.query.get(scm.buyer_id) if scm.buyer_id else None
    # Sales Credit Memo's record 1 is always a Credit -- see
    # _apply_buyer_control_account()'s docstring.
    _apply_buyer_control_account(grl_lines, buyer, side='credit', kind=scm.kind or 'Goods')
    return jsonify({
        'ok': True,
        'origion': scm.doc_no or '',
        'posting_date': scm.posting_date.isoformat() if scm.posting_date else '',
        'due_date': scm.delivery_date.isoformat() if getattr(scm, 'delivery_date', None) else '',
        'document_date': scm.document_date.isoformat() if getattr(scm, 'document_date', None) else '',
        'lines': grl_lines,
    })


@sale_bp.route('/sales/credit-memos/<int:scm_id>/grl')
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'view')
def grl_get_scm(scm_id):
    """Return the GRL (with details) for a Sales Credit Memo, or an empty
    shell -- mirrors grl_get_prn()."""
    scm = SalesCreditMemo.query.get_or_404(scm_id)
    grl = GRL.query.filter_by(sales_credit_memo_id=scm_id).first()
    if grl:
        return jsonify({'ok': True, 'grl': grl.to_dict()})
    return jsonify({'ok': True, 'grl': {
        'id': None, 'sales_credit_memo_id': scm_id,
        'origion': scm.doc_no or '', 'grl_no': '', 'posting_date': '',
        'due_date': '', 'document_date': '', 'narration': '', 'details': [],
    }})


@sale_bp.route('/sales/credit-memos/post-and-save', methods=['POST'])
@login_required
@permission_required_json('sale', 'sales_credit_memo', 'post')
@block_in_basic_mode_json
def scm_post_and_save():
    """Post & Save: validate+save the Sales Credit Memo (Step 1), then
    create/refresh its GRL master+detail (Step 3) and linked Journal Entry
    (Step 2) -- ALL in this one transaction. Rejects re-posting a SCM
    whose posting_status is already 'Posted'. Mirrors pdm_post_and_save()
    in purchase.py exactly, keyed off GRL.sales_credit_memo_id /
    origin_type='SCM' / form_code='sales_credit_memo'.
    """
    from models import JournalEntry, JournalEntryDetail, next_je_no, NoActiveFinancialYearError
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = SalesCreditMemo.query.get_or_404(int(id_str))
            if doc.posting_status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Sales Credit Memo has already been posted.',
                    'تم ترحيل إشعار دائن البيع هذا مسبقاً.')}), 400
            _apply_scm_fields(doc, f, is_new=False)
        else:
            doc = SalesCreditMemo(doc_no=_next_doc_no('SCM', SalesCreditMemo), created_by=current_user.id)
            db.session.add(doc)
            _apply_scm_fields(doc, f, is_new=True)

        posting_date  = pd(f.get('grl_posting_date')) or doc.posting_date or date.today()
        due_date      = pd(f.get('grl_due_date')) or doc.delivery_date
        if due_date and posting_date and posting_date > due_date:
            return jsonify({'ok': False, 'error': _t(
                'Posting Date must be on or before the Due Date',
                'يجب أن يكون تاريخ الترحيل قبل أو يساوي تاريخ الاستحقاق')}), 400

        grl = GRL.query.filter_by(sales_credit_memo_id=doc.sales_credit_memo_id).first()
        if not grl:
            grl = GRL(sales_credit_memo_id=doc.sales_credit_memo_id)
            db.session.add(grl)
        if not grl.grl_no:
            grl.grl_no = _next_grl_no()
        grl.origion       = (f.get('grl_origion','') or doc.doc_no or '').strip()
        grl.posting_date  = posting_date
        grl.due_date      = due_date
        grl.document_date = date.today()
        grl.narration     = (f.get('grl_narration','') or '').strip()
        db.session.flush()

        codes    = request.form.getlist('d_code[]')
        refcodes = request.form.getlist('d_reference_code[]')
        names    = request.form.getlist('d_account_name[]')
        names_ar = request.form.getlist('d_account_name_ar[]')
        controls = request.form.getlist('d_control_account[]')
        debits   = request.form.getlist('d_debit[]')
        credits  = request.form.getlist('d_credit[]')
        narrs    = request.form.getlist('d_narration[]')

        non_blank = [i for i in range(len(codes))
                     if (codes[i] or '').strip() or (i < len(names) and (names[i] or '').strip())]
        if not non_blank:
            return jsonify({'ok': False, 'error': _t(
                'No GRL detail records to post.', 'لا توجد سجلات لترحيلها.')}), 400
        if len(non_blank) > 2:
            return jsonify({'ok': False, 'error': 'Only two GRL detail records are allowed.'}), 400

        tot_d = sum(float(_grl_num(debits[i] if i < len(debits) else 0)) for i in range(len(codes)))
        tot_c = sum(float(_grl_num(credits[i] if i < len(credits) else 0)) for i in range(len(codes)))
        if abs(tot_d - tot_c) > 0.005:
            return jsonify({'ok': False, 'error': 'Total Debit must equal Total Credit.'}), 400

        je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
        if not je:
            je = JournalEntry(je_no=next_je_no(), origin_type='SCM', origin_id=doc.sales_credit_memo_id)
            db.session.add(je)
            db.session.flush()
            grl.journal_entry_id = je.id
        je.origion       = grl.origion
        je.posting_date  = grl.posting_date
        je.due_date      = grl.due_date
        je.document_date = grl.document_date
        je.narration     = grl.narration

        GRLDetail.query.filter_by(grl_id=grl.id).delete()
        JournalEntryDetail.query.filter_by(journal_entry_id=je.id).delete()
        for i in range(len(codes)):
            code = (codes[i] or '').strip()
            if not code and not (names[i] if i < len(names) else '').strip():
                continue
            reference_code  = (refcodes[i] if i < len(refcodes) else '').strip()
            account_name    = (names[i] if i < len(names) else '').strip()
            control_account = (controls[i] if i < len(controls) else '').strip()
            debit           = _grl_num(debits[i] if i < len(debits) else 0)
            credit          = _grl_num(credits[i] if i < len(credits) else 0)
            narration       = (narrs[i] if i < len(narrs) else '').strip()
            # code/control_account/reference_code are written identically
            # into both GRLDetail and JournalEntryDetail -- the two ledgers
            # must never diverge on these three fields.
            db.session.add(GRLDetail(
                grl_id=grl.id, code=code, reference_code=reference_code, account_name=account_name,
                account_name_ar=(names_ar[i] if i < len(names_ar) else '').strip(),
                control_account=control_account, debit=debit, credit=credit, narration=narration,
            ))
            db.session.add(JournalEntryDetail(
                journal_entry_id=je.id, code=code, reference_code=reference_code, account_name=account_name,
                control_account=control_account, debit=debit, credit=credit, narration=narration,
            ))

        doc.posting_status = 'Posted'
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.sales_credit_memo_id, 'doc_no': doc.doc_no, 'grl': grl.to_dict()})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.',
            'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in scm_post_and_save: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# SALES ORDER QUANTITY TRACKING -- read-only report grid, one row per
# Approved Sales Order line, showing Original/Delivered/Remaining
# quantity and a drill-down into every Delivery Note that has posted
# against it. Mirrors po_tracking_list()/po_tracking_data()/
# po_tracking_receipts() in purchase.py exactly, keyed off
# SalesOrderLineItem instead of PurchaseOrderLineItem.
# ══════════════════════════════════════════════════════════════════

@sale_bp.route('/sales/so-tracking')
@login_required
@permission_required('sale', 'so_quantity_tracking', 'view')
def so_tracking_list():
    return render_template('sales/so_tracking_list.html')


@sale_bp.route('/sales/so-tracking/data')
@login_required
@permission_required_json('sale', 'so_quantity_tracking', 'view')
def so_tracking_data():
    rows = (SalesOrderLineItem.query
            .join(SalesOrder, SalesOrder.sales_order_id == SalesOrderLineItem.sales_order_id)
            .filter(SalesOrder.status == 'Approved')
            .order_by(SalesOrderLineItem.sales_order_id.desc(), SalesOrderLineItem.line_number).all())
    out = []
    for li in rows:
        d = li.to_dict(with_progress=True)
        so = li.sales_order
        d['so_doc_no'] = so.doc_no if so else ''
        d['so_status'] = so.status if so else ''
        last_txn = (StoreTransaction.query
                    .filter_by(sales_order_line_item_id=li.sales_order_line_item_id, status='Active')
                    .order_by(StoreTransaction.id.desc()).first())
        d['last_dn_doc_no'] = last_txn.delivery_note_doc_no if last_txn else ''
        out.append(d)
    return jsonify(out)


@sale_bp.route('/sales/so-tracking/<int:so_line_id>/deliveries')
@login_required
@permission_required_json('sale', 'so_quantity_tracking', 'view')
def so_tracking_deliveries(so_line_id):
    """The 'View Deliveries' drill-down: every Active delivery (Store
    Transaction) ever posted against this one SO line, newest first."""
    rows = (StoreTransaction.query
            .filter_by(sales_order_line_item_id=so_line_id, status='Active')
            .order_by(StoreTransaction.id.desc()).all())
    return jsonify([r.to_dict() for r in rows])