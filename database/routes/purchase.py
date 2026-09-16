from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, session, abort, current_app
from flask_login import login_required, current_user
from decimal import Decimal
from datetime import datetime, date
from sqlalchemy import text
import os, re, uuid
from werkzeug.utils import secure_filename

from database.routes.shared import _next_grl_no, _grl_num, _grl_lines_from, resolve_purchase_type, block_in_basic_mode_json
from database.routes.rbac import permission_required, permission_required_json

# ✅ FIX: Complete imports - ItemMaster is in models.py
from models import (
    db, 
    SupplierMaster, 
    SupplierBank,
    SupplierDocument,
    PurchaseRequest, 
    PurchaseQuotation, 
    PurchaseOrder, 
    GoodsReceiptNote,
    PurchaseInvoice, 
    GoodsReturnRequest,
    PurchaseReturnNote,
    PurchaseReturnNoteLineItem,
    PurchaseDebitMemo,
    PurchaseAttachment,
    PurchaseRequestLineItem,
    PurchaseQuotationLineItem,
    PurchaseOrderLineItem,
    GoodsReceiptLineItem,
    PurchaseInvoiceLineItem,
    GoodsReturnLineItem,
    PurchaseDebitMemoLineItem,
    GRL,
    GRLDetail,
    LevelFive,
    PurchaseTaxCode,
    SalesTaxCode,
    ItemMaster,           # ✅ Now properly imported
    ItemCategory,         # ✅ Now properly imported
    ItemSubCategory,      # ✅ Now properly imported
    ItemUnit,
    ItemUnitMeasurement,              # ✅ Now properly imported
)

pur_bp = Blueprint('purchase', __name__)


@pur_bp.errorhandler(Exception)
def _handle_pur_errors(e):
    # Surface a clean message when no financial year is open, instead of a 500.
    from flask import jsonify, session
    if e.__class__.__name__ == 'NoActiveFinancialYear':
        msg = ('الرجاء تفعيل سنة مالية (بحالة مفتوحة) قبل إضافة أي سجل.'
               if session.get('lang') == 'ar'
               else 'Please activate a financial year (status Open) before adding any record.')
        return jsonify({'ok': False, 'error': msg}), 400
    raise e


# ══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════

def _t(en, ar): return ar if session.get('lang') == 'ar' else en

def pd(val):
    """Parse date string, return None if empty/invalid."""
    if not val:
        return None
    try:
        return datetime.strptime(str(val).strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError, AttributeError):
        return None

def _supplier_list():
    """Return list of suppliers for dropdowns."""
    return [{'id':v.id,'name':v.supplier_name_en,'name_ar':v.supplier_name_ar or ''}
            for v in SupplierMaster.query.filter_by(is_active=True).order_by(SupplierMaster.supplier_name_en).all()]

def _owner_list():
    """Return list of owners for dropdowns (the company's own warehouses hang off these)."""
    from models import Owner
    return [{'id': s.id, 'name': s.name} for s in Owner.query.order_by(Owner.name).all()]

def _owner_warehouses(owner_id):
    """Warehouses of the (single) Owner, for docs that no longer let the user pick an Owner."""
    from models import OwnerWarehouse
    return [w.to_dict() for w in OwnerWarehouse.query.filter_by(owner_id=owner_id).order_by(OwnerWarehouse.id).all()]

class NoActiveFinancialYear(Exception):
    """Raised when there is no Open financial year to number a document."""
    pass


# Logical doc type -> the new prefix embedded in the document number.
PURCHASE_PREFIX = {
    'PR':  'PER',   # Purchase Request
    'PQ':  'PEQ',   # Purchase Quotation
    'PO':  'PRO',   # Purchase Order
    'GRN': 'GRN',   # Goods Receipt Note
    'PINV':'PRI',   # Purchase Invoice
    'GRR': 'GRR',   # Goods Return Request
    'PRN': 'PRN',   # Purchase Return Note
    'PDM': 'PDM',   # Purchase Debit Memo
}


def _next_doc_no(doc_type, model):
    """Unique document number: <PREFIX>-<FY year>-<n>  e.g. PER-2026-1.

    The year is the active (Open) financial year, not the calendar year.
    Raises NoActiveFinancialYear if no financial year is open.
    """
    from models import active_fy_year
    year = active_fy_year()
    if not year:
        raise NoActiveFinancialYear()

    prefix = PURCHASE_PREFIX.get(doc_type, doc_type)
    like = f'{prefix}-{year}-%'

    # Highest existing sequence for this prefix + year.
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

    # Ensure uniqueness.
    retries = 0
    while model.query.filter_by(doc_no=doc_no).first() and retries < 100:
        n += 1
        doc_no = f'{prefix}-{year}-{n}'
        retries += 1
    if retries >= 100:
        doc_no = f'{prefix}-{year}-{datetime.now().strftime("%Y%m%d%H%M%S")}'
    return doc_no


def _save_attachments(doc_type, doc_id, files):
    """Save uploaded attachments."""
    upload_dir = os.path.join('static','uploads','purchase',doc_type,str(doc_id))
    os.makedirs(upload_dir, exist_ok=True)
    for f in files:
        if not f or not f.filename: continue
        fname = secure_filename(f.filename)
        fpath = os.path.join(upload_dir, fname)
        f.save(fpath)
        att = PurchaseAttachment(
            doc_type=doc_type, doc_id=doc_id,
            filename=fname, filepath=fpath,
            file_size=os.path.getsize(fpath),
            uploaded_by=current_user.id,
        )
        db.session.add(att)


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

    # Legacy literal codes: several line-item widgets across the Purchase
    # module (GRN/GRR/PINV/PRN/PDM/PO -- see the shared line-item partial)
    # submit a fixed display code like "VAT15" instead of a real
    # PurchaseTaxCode/SalesTaxCode account_code such as "P1". Recognize those
    # directly so the server's tax_amount matches what the client already
    # previewed, instead of silently falling through to 0% below.
    LEGACY_TAX_CODES = {
        'VAT15': (Decimal('15'), '15%'),
        'VAT0':  (Decimal('0'),  '0%'),
        'EXEMPT': (Decimal('0'), 'Exempt'),
    }
    if account_code in LEGACY_TAX_CODES:
        return LEGACY_TAX_CODES[account_code]

    # A downstream document (e.g. Purchase Quotation populated from a
    # Purchase Request, or any later document copying an earlier one's
    # lines) can resubmit the PREVIOUS document's already-resolved display
    # text -- e.g. "15%" -- as if it were a fresh account_code to look up.
    # That's not a real account_code, but it already tells us the rate, so
    # parse it the same way a matched PurchaseTaxCode row's text would be,
    # instead of falling through to 0% just because no account_code matched.
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', account_code or '')
    if m:
        return Decimal(m.group(1)), account_code

    # Fallback: unknown/legacy code — try parsing it directly as a rate.
    try:
        return Decimal(account_code), account_code
    except Exception:
        return Decimal('0'), account_code


def _save_doc_line_items(LIModel, fk_field, fk_value, f, doc_type='purchase', source_link_field=None):
    """Generic save for any dedicated line item model.

    source_link_field: optional model attribute name (e.g.
    'purchase_order_line_item_id') populated from the parallel
    'li_source_line_id[]' form field, when the caller needs to record which
    upstream document line each new line was sourced from.
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

    total_bd = Decimal(0)
    total_disc = Decimal(0)
    total_fr = Decimal(0)
    total_vat = Decimal(0)

    for i in range(len(qtys)):
        try:
            # ✅ FIX: Handle empty/None values safely
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

            # Calculate with proper precision
            taxable = max(Decimal('0'), (qty * rate - disc) + fr)
            tax_amt = (taxable * tax_rate / 100).quantize(Decimal('0.01'))  # 2 decimals
            total = (taxable + tax_amt).quantize(Decimal('0.01'))  # 2 decimals
            taxable_rounded = taxable.quantize(Decimal('0.01'))  # 2 decimals

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
            total_bd   += qty * rate
            total_disc += disc
            total_fr   += fr
            total_vat  += tax_amt
        except Exception as e:
            print(f"Error processing line item {i}: {str(e)}")
            import traceback
            traceback.print_exc()

    excl = (total_bd - total_disc) + total_fr
    return {
        'total_before_discount': total_bd.quantize(Decimal('0.01')),
        'total_discount':        total_disc.quantize(Decimal('0.01')),
        'total_freight':         total_fr.quantize(Decimal('0.01')),
        'total_excl_vat':        excl.quantize(Decimal('0.01')),
        'vat_amount':            total_vat.quantize(Decimal('0.01')),
        'total_incl_vat':        (excl + total_vat).quantize(Decimal('0.01')),
    }


def _validate_pr_pq_dates(valid_until, required_date):
    """valid_until must be >= today (document_date); required_date must be <= valid_until."""
    today = date.today()
    if valid_until and valid_until < today:
        return 'Valid Until must be on/after the document date'
    if required_date and valid_until and required_date > valid_until:
        return 'Required Date must be on/before Valid Until'
    return None


def _validate_posting_date(posting_date, required_date):
    """Posting date rule: present, and on/before the required date.

    Per spec: posting date can be the current date or less-than-or-equal to
    the required date.
    """
    if posting_date and required_date and posting_date > required_date:
        return 'Posting Date must be on or before the Required Date'
    return None


def _next_item_code():
    """Generate the next sequential item code. Format: ITM-0001, ITM-0002, ...

    Queries ONLY the item_code column so it works even if the DB is missing
    other (newly added) columns that the ItemMaster model defines.
    """
    prefix = 'ITM'
    codes = [row[0] for row in db.session.query(ItemMaster.item_code)
             .filter(ItemMaster.item_code.like(f'{prefix}-%')).all()]
    max_n = 0
    for code in codes:
        if code:
            try:
                num = int(code.split('-')[-1])
                if num > max_n:
                    max_n = num
            except (ValueError, IndexError):
                continue
    n = max_n + 1
    existing = set(codes)
    code = f'{prefix}-{n:04d}'
    while code in existing:
        n += 1
        code = f'{prefix}-{n:04d}'
    return code


# ══════════════════════════════════════════════════════════════════
# UNIFIED NEXT-DOC-NO ENDPOINT
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/next-doc-no')
@login_required
def next_doc_no():
    """Return next document number for AJAX - works for all types."""
    doc_type = request.args.get('type', 'PO')
    force_new = request.args.get('force_new', 'false').lower() == 'true'
    
    model_map = {
        'PR': PurchaseRequest,
        'PQ': PurchaseQuotation,
        'PO': PurchaseOrder,
        'GRN': GoodsReceiptNote,
        'PINV': PurchaseInvoice,
        'GRR': GoodsReturnRequest,
        'PRN': PurchaseReturnNote,
        'PDM': PurchaseDebitMemo,
    }
    model = model_map.get(doc_type, PurchaseOrder)
    
    doc_no = _next_doc_no(doc_type, model)
    
    return jsonify({
        'ok': True,
        'doc_no': doc_no,
        'force_new': force_new
    })

@pur_bp.route('/purchase/next-no')
@login_required
def purchase_next_no():
    """Legacy endpoint - redirects to next-doc-no."""
    doc_type = request.args.get('type', 'PR')
    model_map = {
        'PR': PurchaseRequest,
        'PQ': PurchaseQuotation,
        'PO': PurchaseOrder,
        'GRN': GoodsReceiptNote,
        'PINV': PurchaseInvoice,
        'GRR': GoodsReturnRequest,
        'PRN': PurchaseReturnNote,
        'PDM': PurchaseDebitMemo,
    }
    model = model_map.get(doc_type, PurchaseRequest)
    doc_no = _next_doc_no(doc_type, model)
    return jsonify({'doc_no': doc_no})


# ══════════════════════════════════════════════════════════════════
# SUPPLIER REGISTRATION
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/suppliers')
@login_required
@permission_required('purchase', 'supplier', 'view')
def supplier_list():
    return render_template('supplier/supplier_list.html')

@pur_bp.route('/purchase/suppliers/<int:id>')
@login_required
@permission_required('purchase', 'supplier', 'view')
def supplier_view(id):
    v = SupplierMaster.query.get_or_404(id)
    return render_template('supplier/supplier_view.html', supplier=v)

@pur_bp.route('/purchase/suppliers/data')
@login_required
@permission_required_json('purchase', 'supplier', 'view')
def supplier_data():
    rows = SupplierMaster.query.order_by(SupplierMaster.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])

@pur_bp.route('/purchase/suppliers/<int:id>/json')
@login_required
@permission_required_json('purchase', 'supplier', 'view')
def supplier_json(id):
    v = SupplierMaster.query.get_or_404(id)
    d = v.to_dict()
    for fld in ['supplier_name_ar','vat_number','crn','phone','fax','email','website',
                'contact_person','street_name','street_name_ar','building_number',
                'additional_number','postal_code','country','country_ar','city','city_ar',
                'district','district_ar','bank_name','bank_branch','swift_code',
                'account_number','iban','invoice_id',
                'payment_term']:
        d[fld] = getattr(v, fld, '') or ''
    return jsonify(d)

@pur_bp.route('/purchase/suppliers/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'add')
def supplier_add():
    f = request.form
    from models import active_fy_year
    _vyear = active_fy_year()
    if not _vyear:
        raise NoActiveFinancialYear()
    _vlike = f'Sup-{_vyear}-%'
    _vmax = 0
    for _vr in SupplierMaster.query.filter(SupplierMaster.supplier_code.like(_vlike)).all():
        try:
            _vn = int((_vr.supplier_code or '').rsplit('-', 1)[1])
            if _vn > _vmax:
                _vmax = _vn
        except (ValueError, IndexError):
            continue
    _vcode = f'Sup-{_vyear}-{_vmax + 1}'
    v = SupplierMaster(
        supplier_code=_vcode,
        supplier_name_en=f.get('supplier_name_en','').strip(),
        supplier_name_ar=f.get('supplier_name_ar','').strip() or None,
        vat_number=f.get('vat_number','').strip() or None,
        crn=f.get('crn','').strip() or None,
        phone=f.get('phone','').strip() or None,
        fax=f.get('fax','').strip() or None,
        email=f.get('email','').strip() or None,
        website=f.get('website','').strip() or None,
        contact_person=f.get('contact_person','').strip() or None,
        street_name=f.get('street_name','').strip() or None,
        street_name_ar=f.get('street_name_ar','').strip() or None,
        building_number=f.get('building_number','').strip() or None,
        additional_number=f.get('additional_number','').strip() or None,
        postal_code=f.get('postal_code','').strip() or None,
        country=f.get('country','Saudi Arabia').strip(),
        country_ar=f.get('country_ar','المملكة العربية السعودية').strip(),
        city=f.get('city','').strip() or None,
        city_ar=f.get('city_ar','').strip() or None,
        district=f.get('district','').strip() or None,
        district_ar=f.get('district_ar','').strip() or None,
        payment_term=f.get('payment_term','').strip() or None,
        levelfive_code=f.get('levelfive_code','').strip() or None,
        levelfive_drawer=f.get('levelfive_drawer','').strip() or None,
        status='active', is_active=True, created_by=current_user.id,
    )
    db.session.add(v); db.session.flush()
    
    bank_names_en = f.getlist('bank_name_en[]')
    bank_names_ar = f.getlist('bank_name_ar[]')
    bank_accounts = f.getlist('bank_account_number[]')
    bank_branches_en = f.getlist('bank_branch_en[]')
    bank_branches_ar = f.getlist('bank_branch_ar[]')
    bank_swifts = f.getlist('bank_swift[]')
    bank_ibans = f.getlist('bank_iban[]')
    bank_primaries = f.getlist('bank_is_primary[]')
    
    for i in range(len(bank_names_en)):
        if not bank_names_en[i].strip():
            continue
        is_primary = bank_primaries[i] == '1' if i < len(bank_primaries) else False
        if is_primary:
            SupplierBank.query.filter_by(supplier_id=v.id, is_primary=True).update({'is_primary': False})
        bank = SupplierBank(
            supplier_id=v.id,
            bank_name_en=bank_names_en[i].strip(),
            bank_name_ar=bank_names_ar[i].strip() if i < len(bank_names_ar) and bank_names_ar[i].strip() else None,
            account_number=bank_accounts[i].strip() if i < len(bank_accounts) and bank_accounts[i].strip() else None,
            branch_en=bank_branches_en[i].strip() if i < len(bank_branches_en) and bank_branches_en[i].strip() else None,
            branch_ar=bank_branches_ar[i].strip() if i < len(bank_branches_ar) and bank_branches_ar[i].strip() else None,
            swift_code=bank_swifts[i].strip() if i < len(bank_swifts) and bank_swifts[i].strip() else None,
            iban=bank_ibans[i].strip() if i < len(bank_ibans) and bank_ibans[i].strip() else None,
            is_primary=is_primary,
        )
        db.session.add(bank)
    
    db.session.commit()
    return jsonify({'ok': True, 'id': v.id, 'supplier_code': v.supplier_code})

@pur_bp.route('/purchase/suppliers/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'edit')
def supplier_edit(id):
    v = SupplierMaster.query.get_or_404(id)
    f = request.form
    for fld in ['supplier_name_en','supplier_name_ar','vat_number','crn','phone','fax','email',
                'website','contact_person','street_name','street_name_ar','building_number',
                'additional_number','postal_code','country','country_ar','city','city_ar',
                'district','district_ar',
                'payment_term', 'levelfive_code', 'levelfive_drawer']:
        setattr(v, fld, f.get(fld,'').strip() or None)
    v.status = f.get('status','active')
    
    SupplierBank.query.filter_by(supplier_id=id).delete()
    
    bank_names_en = f.getlist('bank_name_en[]')
    bank_names_ar = f.getlist('bank_name_ar[]')
    bank_accounts = f.getlist('bank_account_number[]')
    bank_branches_en = f.getlist('bank_branch_en[]')
    bank_branches_ar = f.getlist('bank_branch_ar[]')
    bank_swifts = f.getlist('bank_swift[]')
    bank_ibans = f.getlist('bank_iban[]')
    bank_primaries = f.getlist('bank_is_primary[]')
    
    for i in range(len(bank_names_en)):
        if not bank_names_en[i].strip():
            continue
        is_primary = bank_primaries[i] == '1' if i < len(bank_primaries) else False
        bank = SupplierBank(
            supplier_id=id,
            bank_name_en=bank_names_en[i].strip(),
            bank_name_ar=bank_names_ar[i].strip() if i < len(bank_names_ar) and bank_names_ar[i].strip() else None,
            account_number=bank_accounts[i].strip() if i < len(bank_accounts) and bank_accounts[i].strip() else None,
            branch_en=bank_branches_en[i].strip() if i < len(bank_branches_en) and bank_branches_en[i].strip() else None,
            branch_ar=bank_branches_ar[i].strip() if i < len(bank_branches_ar) and bank_branches_ar[i].strip() else None,
            swift_code=bank_swifts[i].strip() if i < len(bank_swifts) and bank_swifts[i].strip() else None,
            iban=bank_ibans[i].strip() if i < len(bank_ibans) and bank_ibans[i].strip() else None,
            is_primary=is_primary,
        )
        db.session.add(bank)
    
    db.session.commit()
    return jsonify({'ok': True})

@pur_bp.route('/purchase/suppliers/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'delete')
def supplier_delete(id):
    v = SupplierMaster.query.get_or_404(id)
    db.session.delete(v); db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
# SUPPLIER BANKS
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/suppliers/<int:supplier_id>/banks')
@login_required
@permission_required_json('purchase', 'supplier', 'view')
def supplier_banks(supplier_id):
    banks = SupplierBank.query.filter_by(supplier_id=supplier_id).order_by(SupplierBank.is_primary.desc()).all()
    return jsonify([b.to_dict() for b in banks])

@pur_bp.route('/purchase/suppliers/<int:supplier_id>/banks/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'add')
def supplier_bank_add(supplier_id):
    f = request.form
    if f.get('is_primary') == '1':
        SupplierBank.query.filter_by(supplier_id=supplier_id, is_primary=True).update({'is_primary': False})
    bank = SupplierBank(
        supplier_id      = supplier_id,
        bank_name_en   = f.get('bank_name_en','').strip(),
        bank_name_ar   = f.get('bank_name_ar','').strip() or None,
        account_number = f.get('account_number','').strip() or None,
        branch_en      = f.get('branch_en','').strip() or None,
        branch_ar      = f.get('branch_ar','').strip() or None,
        swift_code     = f.get('swift_code','').strip() or None,
        iban           = f.get('iban','').strip() or None,
        is_primary     = f.get('is_primary') == '1',
    )
    db.session.add(bank)
    db.session.commit()
    return jsonify({'ok': True, 'id': bank.id})

@pur_bp.route('/purchase/suppliers/banks/<int:bank_id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'edit')
def supplier_bank_edit(bank_id):
    bank = SupplierBank.query.get_or_404(bank_id)
    f = request.form
    if f.get('is_primary') == '1':
        SupplierBank.query.filter_by(supplier_id=bank.supplier_id, is_primary=True).update({'is_primary': False})
    bank.bank_name_en   = f.get('bank_name_en','').strip()
    bank.bank_name_ar   = f.get('bank_name_ar','').strip() or None
    bank.account_number = f.get('account_number','').strip() or None
    bank.branch_en      = f.get('branch_en','').strip() or None
    bank.branch_ar      = f.get('branch_ar','').strip() or None
    bank.swift_code     = f.get('swift_code','').strip() or None
    bank.iban           = f.get('iban','').strip() or None
    bank.is_primary     = f.get('is_primary') == '1'
    db.session.commit()
    return jsonify({'ok': True})

@pur_bp.route('/purchase/suppliers/banks/<int:bank_id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'delete')
def supplier_bank_delete(bank_id):
    bank = SupplierBank.query.get_or_404(bank_id)
    db.session.delete(bank)
    db.session.commit()
    return jsonify({'ok': True})

@pur_bp.route('/purchase/suppliers/banks/<int:bank_id>/set-primary', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'edit')
def supplier_bank_set_primary(bank_id):
    bank = SupplierBank.query.get_or_404(bank_id)
    SupplierBank.query.filter_by(supplier_id=bank.supplier_id, is_primary=True).update({'is_primary': False})
    bank.is_primary = True
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
# SUPPLIER DOCUMENTS
# ══════════════════════════════════════════════════════════════════

SUPPLIER_DOC_TYPES = [
    'CR / سجل تجاري', 'VAT Certificate / شهادة ضريبة', 'ID / هوية',
    'Contract / عقد', 'License / رخصة', 'Insurance / تأمين',
    'Bank Letter / خطاب بنكي', 'Other / أخرى',
]
SUPPLIER_ALLOWED_EXT = {'pdf','doc','docx','xls','xlsx','jpg','jpeg','png','gif','txt'}

@pur_bp.route('/purchase/suppliers/<int:supplier_id>/documents')
@login_required
@permission_required_json('purchase', 'supplier', 'view')
def supplier_documents(supplier_id):
    docs = SupplierDocument.query.filter_by(supplier_id=supplier_id).order_by(SupplierDocument.uploaded_at.desc()).all()
    return jsonify([d.to_dict() for d in docs])

@pur_bp.route('/purchase/suppliers/<int:supplier_id>/documents/upload', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'add')
def supplier_doc_upload(supplier_id):
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'ok': False, 'error': 'No file selected'}), 400
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in SUPPLIER_ALLOWED_EXT:
        return jsonify({'ok': False, 'error': f'File type .{ext} not allowed'}), 400
    unique_name = f'{uuid.uuid4().hex}.{ext}'
    folder = os.path.join('uploads', 'suppliers', str(supplier_id))
    os.makedirs(folder, exist_ok=True)
    full_path = os.path.join(folder, unique_name)
    file.save(full_path)
    rel_path = os.path.join('suppliers', str(supplier_id), unique_name)
    doc = SupplierDocument(
        supplier_id     = supplier_id,
        document_type = request.form.get('document_type', 'Other / أخرى'),
        document_name = request.form.get('document_name', file.filename).strip() or file.filename,
        file_path     = rel_path,
        file_size     = os.path.getsize(full_path),
        expiry_date   = datetime.strptime(request.form['expiry_date'], '%Y-%m-%d').date() if request.form.get('expiry_date') else None,
        uploaded_by   = current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify({'ok': True, 'id': doc.id, 'doc': doc.to_dict()})

def _supplier_doc_download_name(doc, stored_filename):
    """
    Build a safe download filename.

    ``document_name`` is user-entered and usually has no file extension, which
    makes the downloaded file unopenable (e.g. a PDF saved as just "CCC").
    Always ensure the name carries the real extension from the stored file.
    """
    stored_ext = os.path.splitext(stored_filename)[1]
    name = (doc.document_name or '').strip()
    if not name:
        return stored_filename
    name = os.path.basename(name.replace('\\', '/'))
    if not name:
        return stored_filename
    if stored_ext and name.lower().endswith(stored_ext.lower()):
        return name
    return f'{name}{stored_ext}'


def _supplier_doc_dir_and_name(doc):
    """Resolve the stored supplier document to (absolute_dir, filename)."""
    rel = (doc.file_path or '').replace('\\', '/')
    upload_root = current_app.config.get(
        'UPLOAD_FOLDER', os.path.abspath('uploads')
    )
    full = os.path.join(upload_root, rel)
    return os.path.dirname(full), os.path.basename(full)


@pur_bp.route('/purchase/suppliers/documents/<int:doc_id>/view')
@login_required
@permission_required('purchase', 'supplier', 'view')
def supplier_doc_view(doc_id):
    """Open the document inline when the browser can display it."""
    from flask import send_from_directory
    import mimetypes
    doc = SupplierDocument.query.get_or_404(doc_id)
    directory, fname = _supplier_doc_dir_and_name(doc)
    if not os.path.exists(os.path.join(directory, fname)):
        abort(404)
    mime = mimetypes.guess_type(fname)[0] or 'application/octet-stream'
    inline_ok = mime == 'application/pdf' or mime.startswith('image/') or mime.startswith('text/')
    if inline_ok:
        return send_from_directory(directory, fname, as_attachment=False, mimetype=mime)
    return send_from_directory(
        directory, fname, as_attachment=True,
        download_name=_supplier_doc_download_name(doc, fname), mimetype=mime
    )


@pur_bp.route('/purchase/suppliers/documents/<int:doc_id>/download')
@login_required
@permission_required('purchase', 'supplier', 'view')
def supplier_doc_download(doc_id):
    from flask import send_from_directory
    import mimetypes
    doc = SupplierDocument.query.get_or_404(doc_id)
    directory, fname = _supplier_doc_dir_and_name(doc)
    if not os.path.exists(os.path.join(directory, fname)):
        abort(404)
    mime = mimetypes.guess_type(fname)[0] or 'application/octet-stream'
    return send_from_directory(
        directory, fname, as_attachment=True,
        download_name=_supplier_doc_download_name(doc, fname), mimetype=mime
    )

# ─────────────────────────────────────────────────────────────
# PURCHASE ATTACHMENTS — view / download
# Attachments are saved by _save_attachments() under
#   static/uploads/purchase/<doc_type>/<doc_id>/<filename>
# and that relative path is stored in PurchaseAttachment.filepath.
# These routes serve them behind @login_required.
# ─────────────────────────────────────────────────────────────
def _attachment_dir_and_name(att):
    """Return (absolute_dir, filename) for a stored attachment."""
    rel = (att.filepath or '').replace('\\', '/')
    directory = os.path.abspath(os.path.dirname(rel))
    fname = os.path.basename(rel)
    return directory, fname


@pur_bp.route('/purchase/attachments/<int:att_id>/view')
@login_required
@permission_required('purchase', 'supplier', 'view')
def purchase_attachment_view(att_id):
    """Open the attachment inline when the browser can show it."""
    from flask import send_from_directory
    import mimetypes
    att = PurchaseAttachment.query.get_or_404(att_id)
    directory, fname = _attachment_dir_and_name(att)
    full = os.path.join(directory, fname)
    if not os.path.exists(full):
        abort(404)
    mime = mimetypes.guess_type(fname)[0] or 'application/octet-stream'
    inline_ok = mime in ('application/pdf', 'text/plain', 'text/csv') or mime.startswith('image/')
    return send_from_directory(directory, fname, as_attachment=not inline_ok,
                               download_name=att.filename or fname, mimetype=mime)


@pur_bp.route('/purchase/attachments/<int:att_id>/download')
@login_required
@permission_required('purchase', 'supplier', 'view')
def purchase_attachment_download(att_id):
    """Always download the attachment."""
    from flask import send_from_directory
    import mimetypes
    att = PurchaseAttachment.query.get_or_404(att_id)
    directory, fname = _attachment_dir_and_name(att)
    full = os.path.join(directory, fname)
    if not os.path.exists(full):
        abort(404)
    mime = mimetypes.guess_type(fname)[0] or 'application/octet-stream'
    return send_from_directory(directory, fname, as_attachment=True,
                               download_name=att.filename or fname, mimetype=mime)


@pur_bp.route('/purchase/attachments/<int:att_id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'delete')
def purchase_attachment_delete(att_id):
    """Delete a single attachment: remove the file from disk, then the row."""
    att = PurchaseAttachment.query.get_or_404(att_id)
    try:
        directory, fname = _attachment_dir_and_name(att)
        full = os.path.join(directory, fname)
        if os.path.exists(full):
            os.remove(full)
        db.session.delete(att)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@pur_bp.route('/purchase/suppliers/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'supplier', 'delete')
def supplier_doc_delete(doc_id):
    doc = SupplierDocument.query.get_or_404(doc_id)
    full = os.path.join('uploads', doc.file_path)
    if os.path.exists(full):
        os.remove(full)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
# ITEM MASTER
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/items/next-code')
@login_required
def item_next_code():
    return jsonify({'item_code': _next_item_code()})

@pur_bp.route('/items/data')
@login_required
def item_data():
    rows = ItemMaster.query.filter_by(is_active=True).order_by(ItemMaster.item_code).all()
    return jsonify([r.to_dict() for r in rows])

@pur_bp.route('/items/all')
@login_required
def items_all():
    rows = ItemMaster.query.order_by(ItemMaster.item_code).all()
    return jsonify([r.to_dict() for r in rows])

@pur_bp.route('/items/<int:id>/json')
@login_required
def item_json(id):
    item = ItemMaster.query.get_or_404(id)
    return jsonify(item.to_dict())

@pur_bp.route('/items/add', methods=['POST'])
@login_required
def item_add():
    f = request.form
    try:
        item = ItemMaster(
            item_code=_next_item_code(),
            item_type=f.get('item_type','Product'),
            article_no=f.get('article_no','').strip() or None,
            name_en=f.get('name_en','').strip(),
            name_ar=f.get('name_ar','').strip() or None,
            print_name=f.get('print_name','').strip() or None,
            print_name_en=f.get('print_name_en','').strip() or None,
            print_name_ar=f.get('print_name_ar','').strip() or None,
            item_desc=f.get('item_desc','').strip() or None,
            category_id=int(f.get('category_id')) if f.get('category_id') else None,
            sub_category_id=int(f.get('sub_category_id')) if f.get('sub_category_id') else None,
            supplier_id=int(f.get('supplier_id')) if f.get('supplier_id') else None,
            main_rate=Decimal(f.get('main_rate') or '0'),
            po_rate=Decimal(f.get('po_rate') or '0'),
            last_purchase_rate=Decimal(f.get('last_purchase_rate') or '0'),
            retail_rate=Decimal(f.get('retail_rate') or '0'),
            wholesale_rate=Decimal(f.get('wholesale_rate') or '0'),
            special_rate=Decimal(f.get('special_rate') or '0'),
            mrp=Decimal(f.get('mrp') or '0'),
            minimum_sp=Decimal(f.get('minimum_sp') or '0'),
            is_active=f.get('is_active') == '1',
            levelfive_code       = (f.get('levelfive_code','') or '').strip() or None,
            levelfive_drawer_en  = (f.get('levelfive_drawer_en','') or '').strip() or None,
            levelfive_drawer_ar  = (f.get('levelfive_drawer_ar','') or '').strip() or None,
            store                = (f.get('store','') or '').strip() or None,
            expense_type         = (f.get('expense_type','') or '').strip() or None,
            created_by=current_user.id,
        )
        db.session.add(item)
        db.session.flush()

        # Handle UOMs if provided (first selected becomes the default)
        uom_ids = [uid for uid in f.getlist('uom_ids[]') if uid]
        for i, uom_id in enumerate(uom_ids):
            item_uom = ItemUnitMeasurement(
                item_id=item.id,
                uom_id=int(uom_id),
                is_default=(i == 0)
            )
            db.session.add(item_uom)
        if uom_ids:
            first_unit = ItemUnit.query.get(int(uom_ids[0]))
            if first_unit:
                item.uom = first_unit.name_en
        
        db.session.commit()
        return jsonify({'ok': True, 'id': item.id, 'item_code': item.item_code})
    except Exception as e:
        db.session.rollback()
        print(f"Error adding item: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/items/<int:id>/edit', methods=['POST'])
@login_required
def item_edit(id):
    item = ItemMaster.query.get_or_404(id)
    f = request.form
    try:
        item.item_type = f.get('item_type','Product')
        item.article_no = f.get('article_no','').strip() or None
        item.name_en = f.get('name_en','').strip()
        item.name_ar = f.get('name_ar','').strip() or None
        item.print_name = f.get('print_name','').strip() or None
        item.print_name_en = f.get('print_name_en','').strip() or None
        item.print_name_ar = f.get('print_name_ar','').strip() or None
        if f.get('uom'):
            # Only the UOM-chip endpoints (item_uom_add / item_uom_set_default)
            # own this field now; the edit form no longer submits it, so an
            # absent value here must never clobber the item's real default.
            item.uom = f.get('uom')
        item.item_desc = f.get('item_desc','').strip() or None
        item.category_id = int(f.get('category_id')) if f.get('category_id') else None
        item.sub_category_id = int(f.get('sub_category_id')) if f.get('sub_category_id') else None
        item.supplier_id = int(f.get('supplier_id')) if f.get('supplier_id') else None
        item.main_rate = Decimal(f.get('main_rate') or '0')
        item.po_rate = Decimal(f.get('po_rate') or '0')
        item.last_purchase_rate = Decimal(f.get('last_purchase_rate') or '0')
        item.retail_rate = Decimal(f.get('retail_rate') or '0')
        item.wholesale_rate = Decimal(f.get('wholesale_rate') or '0')
        item.special_rate = Decimal(f.get('special_rate') or '0')
        item.mrp = Decimal(f.get('mrp') or '0')
        item.minimum_sp = Decimal(f.get('minimum_sp') or '0')
        item.is_active = f.get('is_active') == '1'
        item.levelfive_code      = (f.get('levelfive_code','') or '').strip() or None
        item.levelfive_drawer_en = (f.get('levelfive_drawer_en','') or '').strip() or None
        item.levelfive_drawer_ar = (f.get('levelfive_drawer_ar','') or '').strip() or None
        item.store               = (f.get('store','') or '').strip() or None
        item.expense_type        = (f.get('expense_type','') or '').strip() or None
        item.updated_at = datetime.utcnow()
        db.session.flush()

        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        print(f"Error editing item: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# UNIT OF MEASUREMENT
# ══════════════════════════════════════════════════════════════════

def _uom_dict(u):
    """ItemUnit -> the {id, unit_name, unit_name_ar} shape the Item
    Master UOM picker expects (kept stable even though the underlying master
    table's columns are name_en/name_ar, not unit_name/unit_name_ar)."""
    return {'id': u.id, 'unit_name': u.name_en, 'unit_name_ar': u.name_ar or ''}

@pur_bp.route('/items/uom/list')
@login_required
def uom_master_list():
    rows = ItemUnit.query.filter_by(status='Active').order_by(ItemUnit.name_en).all()
    return jsonify([_uom_dict(u) for u in rows])

@pur_bp.route('/items/<int:item_id>/uoms')
@login_required
def item_uom_list(item_id):
    rows = ItemUnitMeasurement.query.filter_by(item_id=item_id).order_by(ItemUnitMeasurement.is_default.desc(), ItemUnitMeasurement.id).all()
    return jsonify([r.to_dict() for r in rows])

@pur_bp.route('/items/<int:item_id>/uoms/add', methods=['POST'])
@login_required
def item_uom_add(item_id):
    item = ItemMaster.query.get_or_404(item_id)
    f = request.form
    uom_id = f.get('uom_id')
    if not uom_id:
        return jsonify({'ok': False, 'error': 'Select a unit'}), 400
    unit = ItemUnit.query.get(int(uom_id))
    if not unit:
        return jsonify({'ok': False, 'error': 'Unit not found'}), 404

    dup = ItemUnitMeasurement.query.filter_by(item_id=item_id, uom_id=unit.id).first()
    if dup:
        return jsonify({'ok': False, 'error': 'This unit is already attached to the item'}), 400

    is_first = ItemUnitMeasurement.query.filter_by(item_id=item_id).count() == 0
    link = ItemUnitMeasurement(item_id=item_id, uom_id=unit.id, is_default=is_first)
    db.session.add(link)
    if is_first:
        item.uom = unit.name_en
    db.session.commit()
    return jsonify({'ok': True, 'item_uom': link.to_dict()})

@pur_bp.route('/items/uoms/<int:item_uom_id>/delete', methods=['POST'])
@login_required
def item_uom_delete(item_uom_id):
    link = ItemUnitMeasurement.query.get_or_404(item_uom_id)
    item_id = link.item_id
    was_default = link.is_default
    db.session.delete(link)
    db.session.flush()
    if was_default:
        nxt = ItemUnitMeasurement.query.filter_by(item_id=item_id).order_by(ItemUnitMeasurement.id).first()
        if nxt:
            nxt.is_default = True
            item = ItemMaster.query.get(item_id)
            if item:
                item.uom = nxt.uom.name_en if nxt.uom else item.uom
    db.session.commit()
    return jsonify({'ok': True})

@pur_bp.route('/items/uoms/<int:item_uom_id>/set-default', methods=['POST'])
@login_required
def item_uom_set_default(item_uom_id):
    link = ItemUnitMeasurement.query.get_or_404(item_uom_id)
    ItemUnitMeasurement.query.filter_by(item_id=link.item_id).update({'is_default': False})
    link.is_default = True
    item = ItemMaster.query.get(link.item_id)
    if item:
        item.uom = link.uom.name_en if link.uom else item.uom
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
# PURCHASE REQUEST (PR)
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/requests')
@login_required
@permission_required('purchase', 'purchase_request', 'view')
def pr_list():
    from models import Owner
    owner = Owner.query.first()
    return render_template('purchase/pr_list.html', suppliers=_supplier_list(),
                           owner_warehouses=_owner_warehouses(owner.id) if owner else [])

@pur_bp.route('/purchase/requests/data')
@login_required
@permission_required_json('purchase', 'purchase_request', 'view')
def pr_data():
    rows = PurchaseRequest.query.order_by(PurchaseRequest.purchase_request_id.desc()).all()
    return jsonify([r.to_dict() for r in rows])

@pur_bp.route('/purchase/requests/<int:id>/json')
@login_required
@permission_required_json('purchase', 'purchase_request', 'view')
def pr_json(id):
    pr = PurchaseRequest.query.get_or_404(id)
    d = pr.to_dict()
    d['items'] = [i.to_dict() for i in PurchaseRequestLineItem.query.filter_by(purchase_request_id=id).order_by(PurchaseRequestLineItem.line_number).all()]
    d['attachments'] = [{'id':a.id,'filename':a.filename,'filepath':a.filepath} for a in
                        PurchaseAttachment.query.filter_by(doc_type='PR', doc_id=id).all()]
    return jsonify(d)

@pur_bp.route('/purchase/requests/<int:id>/view')
@login_required
@permission_required('purchase', 'purchase_request', 'view')
def pr_view(id):
    pr = PurchaseRequest.query.get_or_404(id)
    items = PurchaseRequestLineItem.query.filter_by(purchase_request_id=id).order_by(PurchaseRequestLineItem.line_number).all()
    attachments = PurchaseAttachment.query.filter_by(doc_type='PR', doc_id=id).all()
    return render_template('purchase/pr_view.html', pr=pr, items=items, attachments=attachments)

@pur_bp.route('/purchase/requests/<int:id>/summary')
@login_required
@permission_required_json('purchase', 'purchase_request', 'view')
def pr_summary(id):
    pr = PurchaseRequest.query.get_or_404(id)
    d = pr.to_dict()
    d['items'] = [i.to_dict() for i in PurchaseRequestLineItem.query.filter_by(purchase_request_id=id).order_by(PurchaseRequestLineItem.line_number).all()]
    return jsonify(d)

@pur_bp.route('/purchase/requests/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_request', 'add')
def pr_add():
    f = request.form
    valid_until   = pd(f.get('valid_until'))
    required_date = pd(f.get('required_date'))
    posting_date  = pd(f.get('posting_date')) or date.today()
    err = _validate_pr_pq_dates(valid_until, required_date)
    if not err:
        err = _validate_posting_date(posting_date, required_date)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    pr = PurchaseRequest(
        doc_no=_next_doc_no('PR', PurchaseRequest),
        requester=current_user.username,
        requester_name=current_user.username,
        status=f.get('status','Open'),
        kind=f.get('kind','Goods'),
        purchase_type=resolve_purchase_type(f.get('kind','Goods'), f.get('purchase_type')),
        posting_date=posting_date,
        valid_until=valid_until,
        document_date=date.today(),
        required_date=required_date,
        remarks=f.get('remarks','').strip(),
        account_code=f.get('account_code','').strip() or None,
        terms_conditions=f.get('terms_conditions','').strip() or None,
        approved_by=f.get('approved_by','').strip(),
        created_by=current_user.id,
    )
    db.session.add(pr); db.session.flush()
    tots = _save_doc_line_items(PurchaseRequestLineItem, 'purchase_request_id', pr.purchase_request_id, f, 'purchase')
    for k,v in tots.items(): setattr(pr, k, v)
    _save_attachments('PR', pr.purchase_request_id, request.files.getlist('attachments'))
    db.session.commit()
    return jsonify({'ok': True, 'id': pr.purchase_request_id, 'doc_no': pr.doc_no})

@pur_bp.route('/purchase/requests/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_request', 'edit')
def pr_edit(id):
    pr = PurchaseRequest.query.get_or_404(id)
    f = request.form
    valid_until   = pd(f.get('valid_until'))
    required_date = pd(f.get('required_date'))
    posting_date  = pd(f.get('posting_date')) or date.today()
    err = _validate_pr_pq_dates(valid_until, required_date)
    if not err:
        err = _validate_posting_date(posting_date, required_date)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    for fld in ['status','remarks','approved_by']:
        setattr(pr, fld, f.get(fld,'').strip())
    pr.kind = f.get('kind','Goods')
    pr.purchase_type = resolve_purchase_type(pr.kind, f.get('purchase_type'))
    pr.account_code = f.get('account_code','').strip() or None
    pr.terms_conditions = f.get('terms_conditions','').strip() or None
    if not pr.requester:
        pr.requester = current_user.username
    if not pr.requester_name:
        pr.requester_name = current_user.username
    pr.posting_date  = posting_date
    pr.valid_until    = valid_until
    pr.document_date  = date.today()
    pr.required_date  = required_date
    tots = _save_doc_line_items(PurchaseRequestLineItem, 'purchase_request_id', pr.purchase_request_id, f, 'purchase')
    for k,v in tots.items(): setattr(pr, k, v)
    _save_attachments('PR', id, request.files.getlist('attachments'))
    db.session.commit()
    return jsonify({'ok': True})

@pur_bp.route('/purchase/requests/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_request', 'delete')
def pr_delete(id):
    pr = PurchaseRequest.query.get_or_404(id)
    PurchaseRequestLineItem.query.filter_by(purchase_request_id=id).delete()
    PurchaseAttachment.query.filter_by(doc_type='PR', doc_id=id).delete()
    db.session.delete(pr); db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
# PURCHASE QUOTATION (PQ)
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/quotations')
@login_required
@permission_required('purchase', 'purchase_quotation', 'view')
def pq_list():
    from models import Owner
    prs = [{'id':p.purchase_request_id,'doc_no':p.doc_no} for p in PurchaseRequest.query.filter_by(status='Approved').order_by(PurchaseRequest.purchase_request_id.desc()).all()]
    owner = Owner.query.first()
    return render_template('purchase/pq_list.html', suppliers=_supplier_list(), prs=prs,
                           owner_warehouses=_owner_warehouses(owner.id) if owner else [])

@pur_bp.route('/purchase/quotations/data')
@login_required
@permission_required_json('purchase', 'purchase_quotation', 'view')
def pq_data():
    rows = PurchaseQuotation.query.order_by(PurchaseQuotation.purchase_quotation_id.desc()).all()
    return jsonify([r.to_dict() for r in rows])

@pur_bp.route('/purchase/quotations/<int:id>/json')
@login_required
@permission_required_json('purchase', 'purchase_quotation', 'view')
def pq_json(id):
    pq = PurchaseQuotation.query.get_or_404(id)
    d = pq.to_dict()
    d['items'] = [i.to_dict() for i in PurchaseQuotationLineItem.query.filter_by(purchase_quotation_id=id).order_by(PurchaseQuotationLineItem.line_number).all()]
    d['attachments'] = [{'id':a.id,'filename':a.filename} for a in PurchaseAttachment.query.filter_by(doc_type='PQ', doc_id=id).all()]
    return jsonify(d)

@pur_bp.route('/purchase/quotations/<int:id>/view')
@login_required
@permission_required('purchase', 'purchase_quotation', 'view')
def pq_view(id):
    pq = PurchaseQuotation.query.get_or_404(id)
    items = PurchaseQuotationLineItem.query.filter_by(purchase_quotation_id=id).order_by(PurchaseQuotationLineItem.line_number).all()
    attachments = PurchaseAttachment.query.filter_by(doc_type='PQ', doc_id=id).all()
    return render_template('purchase/pq_view.html', doc=pq, items=items, attachments=attachments, doc_type='PQ')

@pur_bp.route('/purchase/quotations/<int:id>/summary')
@login_required
@permission_required_json('purchase', 'purchase_quotation', 'view')
def pq_summary(id):
    pq = PurchaseQuotation.query.get_or_404(id)
    d = pq.to_dict()
    d['items'] = [i.to_dict() for i in PurchaseQuotationLineItem.query.filter_by(purchase_quotation_id=id).order_by(PurchaseQuotationLineItem.line_number).all()]
    return jsonify(d)

@pur_bp.route('/purchase/quotations/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_quotation', 'add')
def pq_add():
    f = request.form
    pr_id = int(f.get('pr_id')) if f.get('pr_id') else None
    pr = PurchaseRequest.query.get(pr_id) if pr_id else None
    if pr_id and (not pr or pr.status != 'Approved'):
        return jsonify({'ok': False, 'error': 'Selected Purchase Request is not Approved'}), 400

    valid_until   = pd(f.get('valid_until'))
    required_date = pd(f.get('required_date'))
    posting_date  = pd(f.get('posting_date')) or date.today()
    err = _validate_pr_pq_dates(valid_until, required_date)
    if not err:
        err = _validate_posting_date(posting_date, required_date)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    pq = PurchaseQuotation(
        doc_no=_next_doc_no('PQ', PurchaseQuotation),
        pr_doc_no=pr.doc_no if pr else None,
        requester=current_user.username,
        requester_name=current_user.username,
        supplier_id=int(f.get('supplier_id')) if f.get('supplier_id') else None,
        supplier_ref_no=f.get('supplier_ref_no','').strip(),
        status=f.get('status','Open'),
        kind=f.get('kind','Goods'),
        purchase_type=resolve_purchase_type(f.get('kind','Goods'), f.get('purchase_type')),
        posting_date=posting_date,
        valid_until=valid_until,
        document_date=date.today(),
        required_date=required_date,
        remarks=f.get('remarks','').strip(),
        account_code=f.get('account_code','').strip() or None,
        terms_conditions=f.get('terms_conditions','').strip() or None,
        approved_by=f.get('approved_by','').strip(),
        created_by=current_user.id,
    )
    db.session.add(pq)
    db.session.flush()

    tots = _save_doc_line_items(
        PurchaseQuotationLineItem,
        'purchase_quotation_id',
        pq.purchase_quotation_id,
        f,
        'purchase'
    )

    for k, v in tots.items():
        setattr(pq, k, float(v) if isinstance(v, Decimal) else v)

    _save_attachments('PQ', pq.purchase_quotation_id, request.files.getlist('attachments'))
    db.session.commit()
    return jsonify({'ok': True, 'id': pq.purchase_quotation_id, 'doc_no': pq.doc_no})

@pur_bp.route('/purchase/quotations/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_quotation', 'edit')
def pq_edit(id):
    pq = PurchaseQuotation.query.get_or_404(id)
    f = request.form
    valid_until   = pd(f.get('valid_until'))
    required_date = pd(f.get('required_date'))
    posting_date  = pd(f.get('posting_date')) or date.today()
    err = _validate_pr_pq_dates(valid_until, required_date)
    if not err:
        err = _validate_posting_date(posting_date, required_date)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    for fld in ['status','remarks','approved_by']:
        setattr(pq, fld, f.get(fld,'').strip())
    pq.kind = f.get('kind','Goods')
    pq.purchase_type = resolve_purchase_type(pq.kind, f.get('purchase_type'))
    if not pq.requester:
        pq.requester = current_user.username
    if not pq.requester_name:
        pq.requester_name = current_user.username

    pr_id = int(f.get('pr_id')) if f.get('pr_id') else None
    pr = PurchaseRequest.query.get(pr_id) if pr_id else None
    pq.pr_doc_no = pr.doc_no if pr else None
    pq.supplier_id = int(f.get('supplier_id')) if f.get('supplier_id') else None
    pq.supplier_ref_no = f.get('supplier_ref_no','').strip()
    pq.account_code = f.get('account_code','').strip() or None
    pq.terms_conditions = f.get('terms_conditions','').strip() or None
    pq.posting_date  = posting_date
    pq.valid_until    = valid_until
    pq.document_date  = date.today()
    pq.required_date  = required_date

    tots = _save_doc_line_items(
        PurchaseQuotationLineItem,
        'purchase_quotation_id',
        pq.purchase_quotation_id,
        f,
        'purchase'
    )

    for k, v in tots.items():
        setattr(pq, k, float(v) if isinstance(v, Decimal) else v)

    _save_attachments('PQ', id, request.files.getlist('attachments'))
    db.session.commit()
    return jsonify({'ok': True})

@pur_bp.route('/purchase/quotations/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_quotation', 'delete')
def pq_delete(id):
    pq = PurchaseQuotation.query.get_or_404(id)
    PurchaseQuotationLineItem.query.filter_by(purchase_quotation_id=id).delete()
    PurchaseAttachment.query.filter_by(doc_type='PQ', doc_id=id).delete()
    db.session.delete(pq); db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
# PURCHASE ORDER (PO)
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/orders')
@login_required
@permission_required('purchase', 'purchase_order', 'view')
def po_list():
    from models import Owner
    pqs = [{'id': p.purchase_quotation_id, 'doc_no': p.doc_no}
           for p in PurchaseQuotation.query.filter_by(status='Approved').order_by(PurchaseQuotation.purchase_quotation_id.desc()).all()]
    owner = Owner.query.first()
    return render_template('purchase/po_list.html', suppliers=_supplier_list(), pqs=pqs,
                           owner_warehouses=_owner_warehouses(owner.id) if owner else [])

@pur_bp.route('/purchase/orders/data')
@login_required
@permission_required_json('purchase', 'purchase_order', 'view')
def po_data():
    rows = PurchaseOrder.query.order_by(PurchaseOrder.purchase_order_id.desc()).all()
    return jsonify([r.to_dict() for r in rows])

@pur_bp.route('/purchase/orders/<int:id>/json')
@login_required
@permission_required_json('purchase', 'purchase_order', 'view')
def po_json(id):
    po = PurchaseOrder.query.get_or_404(id)
    d = po.to_dict()
    d['items'] = [i.to_dict() for i in PurchaseOrderLineItem.query
                  .filter_by(purchase_order_id=id)
                  .order_by(PurchaseOrderLineItem.line_number).all()]
    d['attachments'] = [{'id': a.id, 'filename': a.filename}
                        for a in PurchaseAttachment.query.filter_by(doc_type='PO', doc_id=id).all()]
    return jsonify(d)

@pur_bp.route('/purchase/orders/<int:id>/summary')
@login_required
@permission_required_json('purchase', 'purchase_order', 'view')
def po_summary(id):
    po = PurchaseOrder.query.get_or_404(id)
    d = po.to_dict()
    d['items'] = [i.to_dict(with_progress=True) for i in PurchaseOrderLineItem.query
                  .filter_by(purchase_order_id=id)
                  .order_by(PurchaseOrderLineItem.line_number).all()]
    return jsonify(d)

@pur_bp.route('/purchase/orders/<int:id>/view')
@login_required
@permission_required('purchase', 'purchase_order', 'view')
def po_view(id):
    po = PurchaseOrder.query.get_or_404(id)
    items = PurchaseOrderLineItem.query.filter_by(purchase_order_id=id).order_by(PurchaseOrderLineItem.line_number).all()
    attachments = PurchaseAttachment.query.filter_by(doc_type='PO', doc_id=id).all()
    return render_template('purchase/po_view.html', doc=po, items=items, attachments=attachments, doc_type='PO')

@pur_bp.route('/purchase/orders/<int:id>/email-info')
@login_required
@permission_required_json('purchase', 'purchase_order', 'view')
def po_email_info(id):
    """Data to pre-fill the Email compose modal: from = owner email,
    to = supplier email, and the list of this PO's uploaded attachments."""
    po = PurchaseOrder.query.get_or_404(id)
    attachments = PurchaseAttachment.query.filter_by(doc_type='PO', doc_id=id).all()
    return jsonify({
        'from_email': (po.owner.email if po.owner else '') or '',
        'from_configured': bool(po.owner and po.owner.smtp_host and po.owner.smtp_username and po.owner.smtp_password),
        'to_email': (po.supplier.email if po.supplier else '') or '',
        'subject': f'Purchase Order {po.doc_no}',
        'attachments': [{'id': a.id, 'filename': a.filename} for a in attachments],
    })


@pur_bp.route('/purchase/orders/<int:id>/email', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_order', 'print')
def po_email_send(id):
    """Send this Purchase Order to the supplier by email, using the owner's
    own SMTP configuration (Owner form -> Email/SMTP Configuration) as the
    sender, and attaching whichever uploaded files the user picked."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    po = PurchaseOrder.query.get_or_404(id)
    owner = po.owner
    if not owner or not owner.smtp_host or not owner.smtp_username or not owner.smtp_password:
        return jsonify({'ok': False, 'error': 'Email is not configured for this Owner. Set SMTP Host/Username/Password in the Owner form first.'}), 400

    f = request.form
    to_email = (f.get('to') or '').strip()
    if not to_email:
        return jsonify({'ok': False, 'error': 'A "To" email address is required.'}), 400
    subject = (f.get('subject') or f'Purchase Order {po.doc_no}').strip()
    body_html = f.get('body') or ''
    att_ids = [int(x) for x in f.getlist('attachment_ids[]') if x]

    msg = MIMEMultipart()
    msg['From'] = owner.email or owner.smtp_username
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body_html, 'html'))

    if att_ids:
        atts = PurchaseAttachment.query.filter(
            PurchaseAttachment.id.in_(att_ids),
            PurchaseAttachment.doc_type == 'PO', PurchaseAttachment.doc_id == id,
        ).all()
        for a in atts:
            try:
                with open(a.filepath, 'rb') as fh:
                    part = MIMEApplication(fh.read(), Name=a.filename)
                part['Content-Disposition'] = f'attachment; filename="{a.filename}"'
                msg.attach(part)
            except OSError:
                continue

    try:
        with smtplib.SMTP(owner.smtp_host, owner.smtp_port or 587, timeout=20) as server:
            if owner.smtp_use_tls:
                server.starttls()
            server.login(owner.smtp_username, owner.smtp_password)
            server.send_message(msg)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Failed to send email: {e}'}), 500

    return jsonify({'ok': True})


@pur_bp.route('/purchase/orders/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_order', 'add')
def po_add():
    try:
        f = request.form
        pq_id = int(f.get('pq_id')) if f.get('pq_id') else None
        pq = PurchaseQuotation.query.get(pq_id) if pq_id else None
        if pq_id and (not pq or pq.status != 'Approved'):
            return jsonify({'ok': False, 'error': 'Selected Purchase Quotation is not Approved'}), 400

        supplier_id = int(f.get('supplier_id')) if f.get('supplier_id') else (pq.supplier_id if pq else None)

        po = PurchaseOrder(
            doc_no=_next_doc_no('PO', PurchaseOrder),
            pq_doc_no=pq.doc_no if pq else None,
            supplier_id=supplier_id,
            supplier_ref_no=f.get('supplier_ref_no', '').strip(),
            remarks=f.get('remarks', '').strip(),
            account_code=f.get('account_code','').strip() or None,
            terms_conditions=f.get('terms_conditions', '').strip() or None,
            status=f.get('status', 'Open'),
            kind=f.get('kind','Goods'),
            purchase_type=resolve_purchase_type(f.get('kind','Goods'), f.get('purchase_type')),
            posting_date=pd(f.get('posting_date')) or date.today(),
            delivery_date=pd(f.get('delivery_date')),
            document_date=date.today(),
            created_by=current_user.id,
        )
        db.session.add(po)
        db.session.flush()

        tots = _save_doc_line_items(PurchaseOrderLineItem, 'purchase_order_id', po.purchase_order_id, f, 'purchase')
        for k, v in tots.items():
            setattr(po, k, v)

        _save_attachments('PO', po.purchase_order_id, request.files.getlist('attachments'))
        
        db.session.commit()
        return jsonify({'ok': True, 'id': po.purchase_order_id, 'doc_no': po.doc_no})
    
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in po_add: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/orders/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_order', 'edit')
def po_edit(id):
    try:
        po = PurchaseOrder.query.get_or_404(id)
        f = request.form
        pq_id = int(f.get('pq_id')) if f.get('pq_id') else None
        pq = PurchaseQuotation.query.get(pq_id) if pq_id else None

        po.pq_doc_no = pq.doc_no if pq else None
        po.supplier_id = int(f.get('supplier_id')) if f.get('supplier_id') else (pq.supplier_id if pq else None)
        po.supplier_ref_no = f.get('supplier_ref_no', '').strip()
        po.remarks = f.get('remarks', '').strip()
        po.account_code = f.get('account_code','').strip() or None
        po.terms_conditions = f.get('terms_conditions', '').strip() or None
        po.status = f.get('status', 'Open')
        po.kind = f.get('kind','Goods')
        po.purchase_type = resolve_purchase_type(po.kind, f.get('purchase_type'))
        po.posting_date  = pd(f.get('posting_date')) or date.today()
        po.delivery_date = pd(f.get('delivery_date'))
        po.document_date = date.today()

        tots = _save_doc_line_items(PurchaseOrderLineItem, 'purchase_order_id', po.purchase_order_id, f, 'purchase')
        for k, v in tots.items():
            setattr(po, k, v)

        _save_attachments('PO', id, request.files.getlist('attachments'))
        
        db.session.commit()
        return jsonify({'ok': True})
    
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in po_edit: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/orders/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_order', 'delete')
def po_delete(id):
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('purchase_order', id)
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# GOODS RECEIPT NOTE (GRN)
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/grn')
@login_required
@permission_required('purchase', 'goods_receipt_note', 'view')
def grn_list():
    pos = [{'id':p.purchase_order_id,'doc_no':p.doc_no} for p in PurchaseOrder.query.filter_by(status='Approved').order_by(PurchaseOrder.purchase_order_id.desc()).all()]
    return render_template('purchase/grn_list.html', suppliers=_supplier_list(), pos=pos)

@pur_bp.route('/purchase/grn/data')
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'view')
def grn_data():
    return jsonify([r.to_dict() for r in GoodsReceiptNote.query.order_by(GoodsReceiptNote.goods_receipt_note_id.desc()).all()])

@pur_bp.route('/purchase/grn/<int:id>/json')
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'view')
def grn_json(id):
    doc = GoodsReceiptNote.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in GoodsReceiptLineItem.query.filter_by(goods_receipt_note_id=id).order_by(GoodsReceiptLineItem.line_number).all()]
    d['attachments'] = [{'id':a.id,'filename':a.filename} for a in PurchaseAttachment.query.filter_by(doc_type='GRN', doc_id=id).all()]
    return jsonify(d)

@pur_bp.route('/purchase/grn/<int:id>/view')
@login_required
@permission_required('purchase', 'goods_receipt_note', 'view')
def grn_view(id):
    doc = GoodsReceiptNote.query.get_or_404(id)
    return render_template('purchase/grn_view.html', doc=doc,
        items=GoodsReceiptLineItem.query.filter_by(goods_receipt_note_id=id).order_by(GoodsReceiptLineItem.line_number).all(),
        attachments=PurchaseAttachment.query.filter_by(doc_type='GRN', doc_id=id).all())

@pur_bp.route('/purchase/grn/<int:id>/summary')
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'view')
def grn_summary(id):
    doc = GoodsReceiptNote.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in GoodsReceiptLineItem.query
                  .filter_by(goods_receipt_note_id=id)
                  .order_by(GoodsReceiptLineItem.line_number).all()]
    return jsonify(d)

@pur_bp.route('/purchase/grn/next-grl-je-no')
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'view')
def grn_next_grl_je_no():
    """Preview the GRL No / Je No a new GRN's GRL would get, shown in Add
    mode before anything is saved. Both underlying numbering functions only
    look at existing rows, so calling this repeatedly (nothing saved yet)
    is safe -- it just keeps returning the same next-available numbers."""
    from models import next_je_no, NoActiveFinancialYearError
    grl_no = _next_grl_no()
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'ok': True, 'grl_no': grl_no, 'je_no': je_no})




def _validate_grn_receipt_quantities(f):
    """Every GRN line quantity must not exceed the remaining quantity of the
    PO line it's sourced from (li_source_line_id[], a parallel array to
    li_qty[]). remaining_quantity() is computed live from Active Store
    Transactions, so a plain Save on some OTHER GRN never affects this check
    -- only quantity that has actually been Posted counts as "received".
    Row-locks each PO line so concurrent posts can't both slip past the cap.
    """
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
        po_line = (PurchaseOrderLineItem.query
                   .filter_by(purchase_order_line_item_id=int(src_id))
                   .with_for_update().first())
        if not po_line:
            continue
        remaining = Decimal(str(po_line.remaining_quantity()))
        if qty > remaining:
            raise ValueError(f'Receipt quantity cannot exceed the remaining PO quantity of {remaining}.')


def _reverse_grn_store_transactions(grn_id):
    """Mark every Active Store Transaction created by this GRN as Reversed
    (never deleted -- full audit history is kept). Used before re-saving or
    deleting an already-Posted GRN, so PO/Store balances (always computed
    live from Active rows only) immediately reflect the reversal."""
    from models import StoreTransaction
    (StoreTransaction.query
     .filter_by(goods_receipt_note_id=grn_id, status='Active')
     .update({'status': 'Reversed'}, synchronize_session=False))


def _create_grn_store_transactions(doc):
    """Move quantity into each line's Store -- the ONLY place stock is ever
    added, called solely when a GRN is Posted (Post & Save), never on a
    plain Save. Each line's Store is resolved automatically from its Item
    Master's Store Type; the user never picks one manually.

    A Service-kind GRN bypasses the store entirely -- a service has no
    physical stock to receive, so no Store Transaction is created for it
    at all (its GL posting still happens normally)."""
    if (doc.kind or '').strip() == 'Services':
        return
    from models import Store, StoreTransaction
    lines = (GoodsReceiptLineItem.query
             .filter_by(goods_receipt_note_id=doc.goods_receipt_note_id).all())
    for li in lines:
        item = ItemMaster.query.filter_by(item_code=li.item_code).first() if li.item_code else None
        store_type = (item.store or '').strip() if item else ''
        store = Store.query.filter_by(name=store_type).first() if store_type else None
        po_line = (PurchaseOrderLineItem.query.get(li.purchase_order_line_item_id)
                   if li.purchase_order_line_item_id else None)
        po = po_line.purchase_order if po_line else doc.purchase_order
        db.session.add(StoreTransaction(
            store_id=store.id if store else None,
            store_type=store_type,
            item_id=item.id if item else None,
            item_code=li.item_code or '',
            item_name=(item.name_en if item else '') or li.description or '',
            uom=li.uom,
            quantity=li.quantity,
            purchase_order_id=po.purchase_order_id if po else None,
            purchase_order_doc_no=po.doc_no if po else '',
            purchase_order_line_item_id=li.purchase_order_line_item_id,
            goods_receipt_note_id=doc.goods_receipt_note_id,
            goods_receipt_note_doc_no=doc.doc_no,
            goods_receipt_line_item_id=li.goods_receipt_line_item_id,
            supplier_id=doc.supplier_id,
            supplier_name=doc.supplier.supplier_name_en if doc.supplier else '',
            unit_price=li.rate,
            total_amount=li.total,
            transaction_type='Purchase Receipt',
            transaction_date=doc.posting_date or date.today(),
            posting_date=doc.posting_date or date.today(),
            created_by=current_user.id,
            status='Active',
        ))


def _apply_grn_fields(doc, f, is_new):
    """Header + line items + attachments, shared by the plain-Save routes
    (grn_add/grn_edit) and grn_post_and_save(). Does NOT touch GRL/Journal
    Entry -- posting is a separate, explicit step (Post & Save only).
    Raises ValueError (caller turns it into a 400) if the PO isn't Approved.
    """
    po_id = int(f.get('po_id')) if f.get('po_id') else None
    if is_new:
        if not po_id:
            raise ValueError('An Approved Purchase Order must be selected')
        po = PurchaseOrder.query.get(po_id)
        if not po or po.status != 'Approved':
            raise ValueError('Selected Purchase Order is not Approved')
    doc.purchase_order_id = po_id
    doc.supplier_id      = int(f.get('supplier_id')) if f.get('supplier_id') else None
    doc.contact_person  = f.get('contact_person','').strip()
    doc.supplier_ref_no   = f.get('supplier_ref_no','').strip()
    doc.account_code    = f.get('account_code','').strip() or None
    doc.status           = f.get('status','Open')
    doc.kind             = f.get('kind','Goods')
    doc.purchase_type    = resolve_purchase_type(doc.kind, f.get('purchase_type'))
    doc.posting_date     = pd(f.get('posting_date')) or date.today()
    doc.delivery_date    = pd(f.get('delivery_date'))
    doc.document_date    = date.today()
    _validate_grn_receipt_quantities(f)
    db.session.flush()   # ensure doc.goods_receipt_note_id exists
    tots = _save_doc_line_items(GoodsReceiptLineItem, 'goods_receipt_note_id', doc.goods_receipt_note_id, f,
                                 'purchase', source_link_field='purchase_order_line_item_id')
    for k, v in tots.items(): setattr(doc, k, v)
    _save_attachments('GRN', doc.goods_receipt_note_id, request.files.getlist('attachments'))


@pur_bp.route('/purchase/grn/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'add')
def grn_add():
    """Save Goods Receipt Note: the document only. No GRL, no Journal Entry --
    posting to accounting only happens via Post & Save (grn_post_and_save)."""
    try:
        doc = GoodsReceiptNote(doc_no=_next_doc_no('GRN', GoodsReceiptNote), created_by=current_user.id)
        db.session.add(doc)
        _apply_grn_fields(doc, request.form, is_new=True)
        db.session.commit()
        return jsonify({'ok':True,'id':doc.goods_receipt_note_id,'doc_no':doc.doc_no})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in grn_add: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/grn/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'edit')
def grn_edit(id):
    """Save Goods Receipt Note: the document only -- see grn_add().

    If this GRN was already Posted, its old Store Transactions are reversed
    FIRST (so the quantity-remaining check below sees the true, un-doubled
    remaining PO quantity) and fresh ones are created for the new line
    quantities immediately after -- i.e. an edit of a Posted GRN stays
    Posted and its stock/PO impact is always in sync with its current lines.
    """
    try:
        doc = GoodsReceiptNote.query.get_or_404(id)
        was_posted = doc.posting_status == 'Posted'
        if was_posted:
            _reverse_grn_store_transactions(id)
        _apply_grn_fields(doc, request.form, is_new=False)
        if was_posted:
            _create_grn_store_transactions(doc)
        db.session.commit(); return jsonify({'ok':True})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in grn_edit: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@pur_bp.route('/purchase/grn/post-and-save', methods=['POST'])
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'post')
@block_in_basic_mode_json
def grn_post_and_save():
    """Post & Save: validate+save the GRN (Step 1), then create/refresh its
    GRL master+detail (Step 3) and linked Journal Entry (Step 2) -- ALL in
    this one transaction, so a failure anywhere rolls back everything (no
    partially-posted document). Rejects re-posting a GRN whose
    posting_status is already 'Posted' (duplicate-posting prevention).
    """
    from models import JournalEntry, JournalEntryDetail, next_je_no, NoActiveFinancialYearError
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = GoodsReceiptNote.query.get_or_404(int(id_str))
            if doc.posting_status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Good Receipt Note has already been posted.',
                    'تم ترحيل إذن الاستلام هذا مسبقاً.')}), 400
            _apply_grn_fields(doc, f, is_new=False)
        else:
            doc = GoodsReceiptNote(doc_no=_next_doc_no('GRN', GoodsReceiptNote), created_by=current_user.id)
            db.session.add(doc)
            _apply_grn_fields(doc, f, is_new=True)

        # ── Step 3: GRL master (Step 2, the Journal Entry, is created right
        # alongside it below -- both under the one commit at the end).
        posting_date  = pd(f.get('grl_posting_date')) or doc.posting_date or date.today()
        due_date      = pd(f.get('grl_due_date')) or doc.delivery_date
        if due_date and posting_date and posting_date > due_date:
            return jsonify({'ok': False, 'error': _t(
                'Posting Date must be on or before the Due Date',
                'يجب أن يكون تاريخ الترحيل قبل أو يساوي تاريخ الاستحقاق')}), 400

        grl = GRL.query.filter_by(goods_receipt_note_id=doc.goods_receipt_note_id).first()
        if not grl:
            grl = GRL(goods_receipt_note_id=doc.goods_receipt_note_id)
            db.session.add(grl)
        if not grl.grl_no:
            grl.grl_no = _next_grl_no()
        grl.origion       = (f.get('grl_origion','') or doc.doc_no or '').strip()
        grl.posting_date  = posting_date
        grl.due_date      = due_date
        grl.document_date = date.today()
        grl.narration     = (f.get('grl_narration','') or '').strip()
        db.session.flush()   # ensure grl.id exists

        # Detail lines arrive as parallel arrays (the two records the GRL
        # section already built/displayed client-side before Post & Save).
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
        # Exactly two GRL records per receipt line item (item account = debit,
        # Auto Code Selection account = the offset) -- was hard-capped at 2
        # total, which silently dropped every line past the first on a
        # multi-item GRN.
        line_count = GoodsReceiptLineItem.query.filter_by(
            goods_receipt_note_id=doc.goods_receipt_note_id).count()
        if len(non_blank) > 2 * max(line_count, 1):
            return jsonify({'ok': False, 'error': 'Unexpected number of GRL detail records.'}), 400

        tot_d = sum(float(_grl_num(debits[i] if i < len(debits) else 0)) for i in range(len(codes)))
        tot_c = sum(float(_grl_num(credits[i] if i < len(credits) else 0)) for i in range(len(codes)))
        if abs(tot_d - tot_c) > 0.005:
            return jsonify({'ok': False, 'error': 'Total Debit must equal Total Credit.'}), 400

        # ── Step 2: Journal Entry -- same je_no across re-Posts (found ->
        # reuse it), first Post for this GRL (not found) -> auto-number one.
        je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
        if not je:
            je = JournalEntry(je_no=next_je_no(), origin_type='GRN', origin_id=doc.goods_receipt_note_id)
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
                continue   # skip blank rows
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
        _create_grn_store_transactions(doc)
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.goods_receipt_note_id, 'doc_no': doc.doc_no, 'grl': grl.to_dict()})
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
        print(f"ERROR in grn_post_and_save: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/grn/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'delete')
def grn_delete(id):
    doc = GoodsReceiptNote.query.get_or_404(id)
    if doc.posting_status == 'Posted':
        _reverse_grn_store_transactions(id)
    GoodsReceiptLineItem.query.filter_by(goods_receipt_note_id=id).delete()
    PurchaseAttachment.query.filter_by(doc_type='GRN', doc_id=id).delete()
    GRL.query.filter_by(goods_receipt_note_id=id).delete()
    db.session.delete(doc); db.session.commit()
    return jsonify({'ok':True})


# ── GRL (general ledger row per postable document) helpers now live in
#    database/routes/shared.py, shared with sales.py -- see that module
#    for _next_grl_no()/_grl_num()/_grl_lines_from().


def _apply_supplier_control_account(lines, supplier, side='credit', kind='Goods'):
    """Purchase Invoice and Purchase Debit Memo only: record 1 normally
    posts the item's own Level Five account as a Debit. When the document's
    supplier has their own control account assigned (via the Supplier
    form's Chart of Account picker, which only offers control accounts),
    record 1 is replaced entirely by that SAME supplier account instead --
    code/account name come from the supplier's own mapping, and the Control
    Account field shows the supplier's own subsidiary code (e.g.
    'Ven-2026-1') for per-supplier traceability. No-op -- record 1 stays
    the item's own account, Debit -- if the supplier has no control account
    configured.

    `side` picks which side of the entry record 1 posts to -- it must be
    whichever side balances against this form's own Auto Code Selection
    nature (record 2), which differs per form: Purchase Invoice's Auto Code
    Selection nature is Debit, so its record 1 posts as a Credit ('credit',
    the default); Purchase Debit Memo's is Credit, so its record 1 must
    post as a Debit ('debit') instead. Each caller supplies whichever
    supplier its own document actually references (this rule is per-form,
    not shared with GRN/PRN or any Sales document unless a caller there is
    added too).

    `kind` is the parent document's own Kind (Goods/Services). Kind =
    Services is always a no-op here regardless of the supplier's control
    account: record 1 must stay exactly what _grl_lines_from() already
    resolved from the line's own chosen Level Five code/name -- there is no
    "item account" to replace with the supplier's AP account in that case,
    since the line's own code already IS a real Chart-of-Accounts account,
    not an Item Master item standing in for one."""
    if kind == 'Services':
        return lines
    if not supplier or not getattr(supplier, 'levelfive_code', None):
        return lines
    supplier_account = LevelFive.query.filter_by(code=supplier.levelfive_code).first()
    if not supplier_account or supplier_account.control_account != 'Yes':
        return lines
    for ln in lines:
        total = float(ln.get('debit') or 0) + float(ln.get('credit') or 0)
        ln['code'] = supplier.levelfive_code
        ln['account_name'] = supplier_account.drawers or ''
        ln['account_name_ar'] = supplier_account.drawers_ar or ''
        # Bug fix: this used to overwrite control_account with the
        # supplier's own subsidiary code (e.g. "Ven-2026-1") -- control_account
        # must only ever be the account's real Yes/No control-account flag
        # (already confirmed 'Yes' above). The supplier's own code belongs in
        # reference_code, a dedicated field, so the two can never be confused.
        ln['control_account'] = supplier_account.control_account or 'Yes'
        ln['reference_code'] = supplier.supplier_code or ''
        if side == 'debit':
            ln['debit'] = total
            ln['credit'] = 0
        else:
            ln['debit'] = 0
            ln['credit'] = total
    return lines


@pur_bp.route('/purchase/grn/<int:grn_id>/grl-build')
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'view')
def grl_build(grn_id):
    """Assemble the GRL view for a GRN entirely from GRN data + Item Master.

    Frontend-only display; nothing is saved. Mapping:
      header.origion  = GRN doc_no          reference = 'Good Receipt Note'
      posting_date    = GRN posting_date    due_date  = GRN delivery_date
      document_date   = GRN document_date
    """
    grn = GoodsReceiptNote.query.get_or_404(grn_id)
    lines = (GoodsReceiptLineItem.query
             .filter_by(goods_receipt_note_id=grn_id)
             .order_by(GoodsReceiptLineItem.line_number).all())

    grl_lines = _grl_lines_from(lines, kind=grn.kind or 'Goods')
    supplier = SupplierMaster.query.get(grn.supplier_id) if grn.supplier_id else None
    _apply_supplier_control_account(grl_lines, supplier, side='debit', kind=grn.kind or 'Goods')

    return jsonify({
        'ok': True,
        'origion': grn.doc_no or '',
        'posting_date': grn.posting_date.isoformat() if grn.posting_date else '',
        'due_date': grn.delivery_date.isoformat() if getattr(grn, 'delivery_date', None) else '',
        'document_date': grn.document_date.isoformat() if getattr(grn, 'document_date', None) else '',
        'lines': grl_lines,
    })


@pur_bp.route('/purchase/orders/<int:po_id>/grl-preview')
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'view')
def grl_preview_from_po(po_id):
    """Preview the two GRL records for a not-yet-saved GRN or Purchase
    Invoice, shown in Add mode as soon as its source Purchase Order is
    selected -- computed from the PO's own line items (which is exactly
    what the GRN's/PINV's line items get pulled from), the same way
    grl_build()/grl_build_pinv() compute them for a saved document.
    Pass ?form=purchase_invoice for the Purchase Invoice's GRL section;
    defaults to 'goods_receipt_note' for the GRN's."""
    po = PurchaseOrder.query.get_or_404(po_id)
    form_code = request.args.get('form', 'goods_receipt_note')
    lines = (PurchaseOrderLineItem.query
             .filter_by(purchase_order_id=po_id)
             .order_by(PurchaseOrderLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code=form_code, kind=po.kind or 'Goods')
    supplier = SupplierMaster.query.get(po.supplier_id) if po.supplier_id else None
    if form_code == 'purchase_invoice':
        _apply_supplier_control_account(grl_lines, supplier, kind=po.kind or 'Goods')
    elif form_code == 'goods_receipt_note':
        # GRN's Auto Code Selection nature is Credit, so its overridden
        # record 1 must post as a Debit (opposite side) to still balance --
        # see _apply_supplier_control_account()'s docstring.
        _apply_supplier_control_account(grl_lines, supplier, side='debit', kind=po.kind or 'Goods')
    return jsonify({'ok': True, 'lines': grl_lines})


@pur_bp.route('/purchase/grn/<int:grn_id>/grl')
@login_required
@permission_required_json('purchase', 'goods_receipt_note', 'view')
def grl_get(grn_id):
    """Return the GRL (with details) for a GRN, or an empty shell."""
    grn = GoodsReceiptNote.query.get_or_404(grn_id)
    grl = GRL.query.filter_by(goods_receipt_note_id=grn_id).first()
    if grl:
        return jsonify({'ok': True, 'grl': grl.to_dict()})
    # empty shell — origin defaults to the GRN's own doc no
    return jsonify({'ok': True, 'grl': {
        'id': None, 'goods_receipt_note_id': grn_id,
        'origion': grn.doc_no or '', 'grl_no': '', 'posting_date': '',
        'due_date': '', 'document_date': '', 'narration': '', 'details': [],
    }})


# ══════════════════════════════════════════════════════════════════
# PURCHASE INVOICE (PINV)
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/invoices')
@login_required
@permission_required('purchase', 'purchase_invoice', 'view')
def pinv_list():
    from models import Owner
    grns = [{'id':g.goods_receipt_note_id,'doc_no':g.doc_no,'purchase_order_id':g.purchase_order_id}
            for g in GoodsReceiptNote.query.filter_by(status='Approved').order_by(GoodsReceiptNote.goods_receipt_note_id.desc()).all()]
    owner = Owner.query.first()
    return render_template('purchase/pinv_list.html', suppliers=_supplier_list(), grns=grns,
                           owner_warehouses=_owner_warehouses(owner.id) if owner else [])

@pur_bp.route('/purchase/invoices/data')
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'view')
def pinv_data():
    return jsonify([r.to_dict() for r in PurchaseInvoice.query.order_by(PurchaseInvoice.purchase_invoice_id.desc()).all()])

@pur_bp.route('/purchase/invoices/<int:id>/json')
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'view')
def pinv_json(id):
    doc = PurchaseInvoice.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in PurchaseInvoiceLineItem.query.filter_by(purchase_invoice_id=id).order_by(PurchaseInvoiceLineItem.line_number).all()]
    d['attachments'] = [{'id':a.id,'filename':a.filename} for a in PurchaseAttachment.query.filter_by(doc_type='PINV', doc_id=id).all()]
    return jsonify(d)

def _zatca_tlv(tag, value):
    """One ZATCA TLV field: 1-byte tag, 1-byte length, value. `value` may be
    a string (UTF-8 encoded, tags 1-5's plain-text fields) or raw bytes
    (tags 6-9's binary hash/signature/key fields, passed through as-is)."""
    value_bytes = value if isinstance(value, (bytes, bytearray)) else (value or '').encode('utf-8')
    return bytes([tag]) + bytes([len(value_bytes)]) + value_bytes


def _zatca_qr_payload(seller_name, vat_number, timestamp_iso, total, vat_amount, extra_tags=None):
    """Base64 TLV payload for a ZATCA-compliant Tax Invoice QR code -- the
    5 Phase-1 fields KSA's e-invoicing spec requires, in order: (1) seller
    name, (2) seller VAT registration number, (3) invoice timestamp,
    (4) invoice total incl. VAT, (5) VAT total.

    `extra_tags`, when given, is a list of (tag, value) pairs appended
    after tag 5 -- used for Phase 2's tags 6-9 (invoice hash, digital
    signature, public key, certificate signature). Existing callers that
    never pass `extra_tags` are completely unaffected."""
    import base64
    payload = (
        _zatca_tlv(1, seller_name) +
        _zatca_tlv(2, vat_number) +
        _zatca_tlv(3, timestamp_iso) +
        _zatca_tlv(4, f'{total:.2f}') +
        _zatca_tlv(5, f'{vat_amount:.2f}')
    )
    for tag, value in (extra_tags or []):
        payload += _zatca_tlv(tag, value)
    return base64.b64encode(payload).decode('ascii')


def _qr_image_b64(data_str):
    """Render `data_str` as a QR code PNG, base64-encoded for an <img> src."""
    import qrcode, base64
    from io import BytesIO
    img = qrcode.make(data_str)
    buf = BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _pinv_qr_b64(doc):
    ts_date = doc.document_date or doc.posting_date or date.today()
    timestamp_iso = datetime.combine(ts_date, datetime.min.time()).strftime('%Y-%m-%dT%H:%M:%SZ')
    qr_payload = _zatca_qr_payload(
        seller_name=doc.supplier.supplier_name_en if doc.supplier else '',
        vat_number=(doc.supplier.vat_number if doc.supplier else '') or '',
        timestamp_iso=timestamp_iso,
        total=float(doc.total_incl_vat or 0),
        vat_amount=float(doc.vat_amount or 0),
    )
    return _qr_image_b64(qr_payload)


@pur_bp.route('/purchase/invoices/<int:id>/view')
@login_required
@permission_required('purchase', 'purchase_invoice', 'view')
def pinv_view(id):
    doc = PurchaseInvoice.query.get_or_404(id)
    return render_template('purchase/pinv_view.html', doc=doc,
        items=PurchaseInvoiceLineItem.query.filter_by(purchase_invoice_id=id).order_by(PurchaseInvoiceLineItem.line_number).all(),
        attachments=PurchaseAttachment.query.filter_by(doc_type='PINV', doc_id=id).all(),
        qr_b64=_pinv_qr_b64(doc))


@pur_bp.route('/purchase/invoices/<int:id>/print')
@login_required
@permission_required('purchase', 'purchase_invoice', 'print')
def pinv_print(id):
    """Formal Tax Invoice print layout -- Seller block from the invoice's
    Supplier, Buyer block from the company's own Owner record (there is
    only ever one), line items from this Purchase Invoice. Any field the
    layout shows that we have no real source for is left blank rather than
    guessed."""
    from models import Owner, SupplierBank
    doc = PurchaseInvoice.query.get_or_404(id)
    owner = Owner.query.first()
    items = PurchaseInvoiceLineItem.query.filter_by(purchase_invoice_id=id).order_by(PurchaseInvoiceLineItem.line_number).all()

    bank = SupplierBank.query.get(doc.bank_account_id) if doc.bank_account_id else None

    item_rows = []
    for li in items:
        m = re.search(r'(\d+(?:\.\d+)?)\s*%', li.tax_code or '')
        tax_pct = f'{m.group(1)}%' if m else (li.tax_code or '—')
        item_rows.append({
            'item_code': li.item_code or '', 'description': li.description or '', 'uom': li.uom or '',
            'quantity': float(li.quantity or 0), 'rate': float(li.rate or 0),
            'discount': float(li.discount or 0), 'taxable': float(li.taxable or 0),
            'tax_pct': tax_pct, 'tax_amount': float(li.tax_amount or 0), 'total': float(li.total or 0),
        })

    return render_template('purchase/purchase_invoice_print.html', doc=doc, owner=owner, item_rows=item_rows,
        bank=bank)

@pur_bp.route('/purchase/invoices/<int:id>/summary')
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'view')
def pinv_summary(id):
    exclude_grr_id = request.args.get('exclude_grr_id', type=int)
    doc = PurchaseInvoice.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict(with_progress=True, exclude_grr_id=exclude_grr_id) for i in PurchaseInvoiceLineItem.query
                  .filter_by(purchase_invoice_id=id)
                  .order_by(PurchaseInvoiceLineItem.line_number).all()]
    return jsonify(d)

def _apply_pinv_fields(doc, f, is_new):
    """Header + line items + attachments, shared by the plain-Save routes
    (pinv_add/pinv_edit) and pinv_post_and_save(). Does NOT touch GRL/Journal
    Entry -- posting is a separate, explicit step (Post & Save only).

    Dual mechanism: the Goods Receipt Note is OPTIONAL. When one is linked,
    it (and the Purchase Order derived from it server-side, never trusted
    from the client) drives the Supplier and the two can never disagree.
    When no GRN is linked, this is a standalone invoice and the Supplier the
    user picked manually in the form is used instead. Raises ValueError
    (caller turns it into a 400) only if a GRN WAS selected but isn't
    Approved.
    """
    grn_id = int(f.get('grn_id')) if f.get('grn_id') else None
    grn = GoodsReceiptNote.query.get(grn_id) if grn_id else None
    if grn_id and (not grn or grn.status != 'Approved'):
        raise ValueError('Selected Goods Receipt Note is not Approved')

    po_id = grn.purchase_order_id if grn else doc.purchase_order_id
    po = PurchaseOrder.query.get(po_id) if po_id else None

    status = f.get('status', 'Open')
    posting_date = pd(f.get('posting_date')) or date.today()
    if status != 'Open' and not posting_date:
        posting_date = date.today()

    from_date = pd(f.get('from_date'))
    to_date = pd(f.get('to_date'))
    if from_date and to_date and from_date > to_date:
        raise ValueError('From Date must be on or before To Date')

    doc.purchase_order_id = po_id
    doc.goods_receipt_note_id = grn_id
    # Dual mechanism: a linked GRN always drives the Supplier (unchanged,
    # never trusted from the client -- mechanism 1). With no GRN, this is a
    # standalone invoice and the Supplier the user picked manually in the
    # form is used instead (mechanism 2).
    manual_supplier_id = int(f.get('supplier_id')) if f.get('supplier_id') else None
    if grn:
        doc.supplier_id = grn.supplier_id
    elif manual_supplier_id:
        doc.supplier_id = manual_supplier_id
    elif is_new:
        doc.supplier_id = po.supplier_id if po else None
    doc.supplier_ref_no = f.get('supplier_ref_no', '').strip()
    doc.account_code = f.get('account_code', '').strip() or None
    doc.status = status
    doc.kind = f.get('kind', 'Goods')
    doc.purchase_type = resolve_purchase_type(doc.kind, f.get('purchase_type'))
    doc.payment_method = f.get('payment_method', 'Credit')
    doc.bank_account_id = int(f.get('bank_account_id')) if f.get('bank_account_id') else None
    doc.posting_date = posting_date
    doc.delivery_date = pd(f.get('delivery_date'))
    doc.document_date = date.today()
    doc.from_date = from_date
    doc.to_date = to_date
    db.session.flush()   # ensure doc.purchase_invoice_id exists
    tots = _save_doc_line_items(PurchaseInvoiceLineItem, 'purchase_invoice_id', doc.purchase_invoice_id, f, 'purchase')
    for k, v in tots.items(): setattr(doc, k, v)
    _save_attachments('PINV', doc.purchase_invoice_id, request.files.getlist('attachments'))


@pur_bp.route('/purchase/invoices/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'add')
def pinv_add():
    """Save Purchase Invoice: the document only. No GRL, no Journal Entry --
    posting to accounting only happens via Post & Save (pinv_post_and_save)."""
    try:
        doc = PurchaseInvoice(doc_no=_next_doc_no('PINV', PurchaseInvoice), created_by=current_user.id)
        db.session.add(doc)
        _apply_pinv_fields(doc, request.form, is_new=True)
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.purchase_invoice_id, 'doc_no': doc.doc_no})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in pinv_add: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/invoices/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'edit')
def pinv_edit(id):
    """Save Purchase Invoice: the document only -- see pinv_add().

    A Purchase Invoice never owns a Store Transaction of its own, whether or
    not a GRN is linked: Store is only ever maintained at the GRN level.
    """
    try:
        doc = PurchaseInvoice.query.get_or_404(id)
        _apply_pinv_fields(doc, request.form, is_new=False)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in pinv_edit: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@pur_bp.route('/purchase/invoices/next-grl-je-no')
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'view')
def pinv_next_grl_je_no():
    """Preview the GRL No / Je No a new Purchase Invoice's GRL would get,
    shown in Add mode before anything is saved -- mirrors
    grn_next_grl_je_no()."""
    from models import next_je_no, NoActiveFinancialYearError
    grl_no = _next_grl_no()
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'ok': True, 'grl_no': grl_no, 'je_no': je_no})


@pur_bp.route('/purchase/invoices/<int:pinv_id>/grl-build')
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'view')
def grl_build_pinv(pinv_id):
    """Assemble the GRL view for a Purchase Invoice entirely from PINV data
    + Item Master -- mirrors grl_build() for GRN. Frontend-only display;
    nothing is saved."""
    pinv = PurchaseInvoice.query.get_or_404(pinv_id)
    lines = (PurchaseInvoiceLineItem.query
             .filter_by(purchase_invoice_id=pinv_id)
             .order_by(PurchaseInvoiceLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code='purchase_invoice', kind=pinv.kind or 'Goods')
    supplier = SupplierMaster.query.get(pinv.supplier_id) if pinv.supplier_id else None
    _apply_supplier_control_account(grl_lines, supplier, kind=pinv.kind or 'Goods')
    return jsonify({
        'ok': True,
        'origion': pinv.doc_no or '',
        'posting_date': pinv.posting_date.isoformat() if pinv.posting_date else '',
        'due_date': pinv.delivery_date.isoformat() if getattr(pinv, 'delivery_date', None) else '',
        'document_date': pinv.document_date.isoformat() if getattr(pinv, 'document_date', None) else '',
        'lines': grl_lines,
    })


@pur_bp.route('/purchase/invoices/<int:pinv_id>/grl')
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'view')
def grl_get_pinv(pinv_id):
    """Return the GRL (with details) for a Purchase Invoice, or an empty
    shell -- mirrors grl_get() for GRN."""
    pinv = PurchaseInvoice.query.get_or_404(pinv_id)
    grl = GRL.query.filter_by(purchase_invoice_id=pinv_id).first()
    if grl:
        return jsonify({'ok': True, 'grl': grl.to_dict()})
    return jsonify({'ok': True, 'grl': {
        'id': None, 'purchase_invoice_id': pinv_id,
        'origion': pinv.doc_no or '', 'grl_no': '', 'posting_date': '',
        'due_date': '', 'document_date': '', 'narration': '', 'details': [],
    }})


@pur_bp.route('/purchase/invoices/post-and-save', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'post')
@block_in_basic_mode_json
def pinv_post_and_save():
    """Post & Save: validate+save the Purchase Invoice (Step 1), then
    create/refresh its GRL master+detail (Step 3) and linked Journal Entry
    (Step 2) -- ALL in this one transaction, so a failure anywhere rolls
    back everything (no partially-posted document). Rejects re-posting a
    PINV whose posting_status is already 'Posted' (duplicate-posting
    prevention). Mirrors grn_post_and_save() exactly, just keyed off
    GRL.purchase_invoice_id / origin_type='PINV' / form_code='purchase_invoice'.
    """
    from models import JournalEntry, JournalEntryDetail, next_je_no, NoActiveFinancialYearError
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = PurchaseInvoice.query.get_or_404(int(id_str))
            if doc.posting_status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Purchase Invoice has already been posted.',
                    'تم ترحيل فاتورة الشراء هذه مسبقاً.')}), 400
            _apply_pinv_fields(doc, f, is_new=False)
        else:
            doc = PurchaseInvoice(doc_no=_next_doc_no('PINV', PurchaseInvoice), created_by=current_user.id)
            db.session.add(doc)
            _apply_pinv_fields(doc, f, is_new=True)

        posting_date  = pd(f.get('grl_posting_date')) or doc.posting_date or date.today()
        due_date      = pd(f.get('grl_due_date')) or doc.delivery_date
        if due_date and posting_date and posting_date > due_date:
            return jsonify({'ok': False, 'error': _t(
                'Posting Date must be on or before the Due Date',
                'يجب أن يكون تاريخ الترحيل قبل أو يساوي تاريخ الاستحقاق')}), 400

        grl = GRL.query.filter_by(purchase_invoice_id=doc.purchase_invoice_id).first()
        if not grl:
            grl = GRL(purchase_invoice_id=doc.purchase_invoice_id)
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
        # Exactly two GRL records per invoice line item (item account = debit,
        # Auto Code Selection account = the offset) -- was hard-capped at 2
        # total, which silently dropped every line past the first on a
        # multi-item Purchase Invoice.
        line_count = PurchaseInvoiceLineItem.query.filter_by(
            purchase_invoice_id=doc.purchase_invoice_id).count()
        if len(non_blank) > 2 * max(line_count, 1):
            return jsonify({'ok': False, 'error': 'Unexpected number of GRL detail records.'}), 400

        tot_d = sum(float(_grl_num(debits[i] if i < len(debits) else 0)) for i in range(len(codes)))
        tot_c = sum(float(_grl_num(credits[i] if i < len(credits) else 0)) for i in range(len(codes)))
        if abs(tot_d - tot_c) > 0.005:
            return jsonify({'ok': False, 'error': 'Total Debit must equal Total Credit.'}), 400

        je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
        if not je:
            je = JournalEntry(je_no=next_je_no(), origin_type='PINV', origin_id=doc.purchase_invoice_id)
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
        # Store is only ever maintained at the GRN level -- a Purchase
        # Invoice never moves stock of its own, whether or not a GRN is
        # linked.
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.purchase_invoice_id, 'doc_no': doc.doc_no, 'grl': grl.to_dict()})
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
        print(f"ERROR in pinv_post_and_save: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@pur_bp.route('/purchase/invoices/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_invoice', 'delete')
def pinv_delete(id):
    try:
        doc = PurchaseInvoice.query.get_or_404(id)
        PurchaseInvoiceLineItem.query.filter_by(purchase_invoice_id=id).delete()
        PurchaseAttachment.query.filter_by(doc_type='PINV', doc_id=id).delete()
        GRL.query.filter_by(purchase_invoice_id=id).delete()
        db.session.delete(doc)
        db.session.commit()
        return jsonify({'ok':True})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in pinv_delete: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# GOODS RETURN REQUEST (GRR)
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/returns')
@login_required
@permission_required('purchase', 'goods_return_request', 'view')
def grr_list():
    pins = [{'id':p.purchase_invoice_id,'doc_no':p.doc_no} for p in PurchaseInvoice.query.filter_by(status='Approved').order_by(PurchaseInvoice.purchase_invoice_id.desc()).all()]
    return render_template('purchase/grr_list.html', suppliers=_supplier_list(), pinvs=pins)

@pur_bp.route('/purchase/returns/data')
@login_required
@permission_required_json('purchase', 'goods_return_request', 'view')
def grr_data():
    return jsonify([r.to_dict() for r in GoodsReturnRequest.query.order_by(GoodsReturnRequest.goods_return_request_id.desc()).all()])

@pur_bp.route('/purchase/returns/<int:id>/json')
@login_required
@permission_required_json('purchase', 'goods_return_request', 'view')
def grr_json(id):
    doc = GoodsReturnRequest.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in GoodsReturnLineItem.query.filter_by(goods_return_request_id=id).order_by(GoodsReturnLineItem.line_number).all()]
    d['attachments'] = [{'id':a.id,'filename':a.filename} for a in PurchaseAttachment.query.filter_by(doc_type='GRR', doc_id=id).all()]
    return jsonify(d)

@pur_bp.route('/purchase/returns/<int:id>/view')
@login_required
@permission_required('purchase', 'goods_return_request', 'view')
def grr_view(id):
    doc = GoodsReturnRequest.query.get_or_404(id)
    return render_template('purchase/grr_view.html', doc=doc,
        items=GoodsReturnLineItem.query.filter_by(goods_return_request_id=id).order_by(GoodsReturnLineItem.line_number).all(),
        attachments=PurchaseAttachment.query.filter_by(doc_type='GRR', doc_id=id).all())

@pur_bp.route('/purchase/returns/<int:id>/summary')
@login_required
@permission_required_json('purchase', 'goods_return_request', 'view')
def grr_summary(id):
    doc = GoodsReturnRequest.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in GoodsReturnLineItem.query.filter_by(goods_return_request_id=id).order_by(GoodsReturnLineItem.line_number).all()]
    return jsonify(d)

@pur_bp.route('/purchase/returns/<int:grr_id>/grl-preview')
@login_required
@permission_required_json('purchase', 'goods_return_request', 'view')
def grl_preview_from_grr(grr_id):
    """Preview the two GRL records for a not-yet-saved Purchase Return Note,
    shown in Add mode as soon as its source Goods Return Request is
    selected -- mirrors grl_preview_from_po() for PO-sourced docs."""
    grr = GoodsReturnRequest.query.get_or_404(grr_id)
    form_code = request.args.get('form', 'purchase_return_note')
    lines = (GoodsReturnLineItem.query
             .filter_by(goods_return_request_id=grr_id)
             .order_by(GoodsReturnLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code=form_code, kind=grr.kind or 'Goods')
    if form_code == 'purchase_return_note':
        supplier = SupplierMaster.query.get(grr.supplier_id) if grr.supplier_id else None
        # Purchase Return Note's Auto Code Selection nature is Credit, so
        # its overridden record 1 must post as a Debit (opposite side).
        _apply_supplier_control_account(grl_lines, supplier, side='debit', kind=grr.kind or 'Goods')
    return jsonify({'ok': True, 'lines': grl_lines})


@pur_bp.route('/purchase/return-notes/<int:prn_id>/grl-preview')
@login_required
@permission_required_json('purchase', 'goods_return_request', 'view')
def grl_preview_from_prn(prn_id):
    """Preview the two GRL records for a not-yet-saved Purchase Debit Memo,
    shown in Add mode as soon as its source Purchase Return Note is
    selected -- mirrors grl_preview_from_grr() for GRR-sourced docs. Pass
    ?form=purchase_debit_memo for the PDM's GRL section."""
    prn = PurchaseReturnNote.query.get_or_404(prn_id)
    form_code = request.args.get('form', 'purchase_debit_memo')
    lines = (PurchaseReturnNoteLineItem.query
             .filter_by(purchase_good_return_note_id=prn_id)
             .order_by(PurchaseReturnNoteLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code=form_code, kind=prn.kind or 'Goods')
    if form_code == 'purchase_debit_memo':
        supplier = SupplierMaster.query.get(prn.supplier_id) if prn.supplier_id else None
        _apply_supplier_control_account(grl_lines, supplier, side='debit', kind=prn.kind or 'Goods')
    return jsonify({'ok': True, 'lines': grl_lines})

def _validate_grr_qty(f, purchase_invoice_id, exclude_grr_id=None):
    """Every GRR line must be sourced from one of the selected invoice's own
    lines (li_source_line_id[], a parallel array to li_qty[]) and its
    quantity must not exceed that invoice line's REMAINING quantity --
    i.e. after subtracting whatever other (non-Cancelled) Goods Return
    Requests have already claimed against it. exclude_grr_id lets an edit
    of an existing GRR exclude its own prior contribution from that sum."""
    if not purchase_invoice_id:
        return None
    src_ids = f.getlist('li_source_line_id[]')
    qtys    = f.getlist('li_qty[]')
    for i in range(len(qtys)):
        src_id = src_ids[i] if i < len(src_ids) and src_ids[i] else None
        try:
            qty = Decimal((qtys[i] or '0').replace(',', '').strip() or '0')
        except Exception:
            qty = Decimal(0)
        if qty <= 0:
            continue
        if not src_id:
            return 'Every return line must be selected from the Purchase Invoice'
        li = (PurchaseInvoiceLineItem.query
              .filter_by(purchase_invoice_line_item_id=int(src_id), purchase_invoice_id=purchase_invoice_id)
              .with_for_update().first())
        if not li:
            return 'Selected line is not part of the chosen Purchase Invoice'
        remaining = Decimal(str(li.remaining_quantity(exclude_grr_id=exclude_grr_id)))
        if qty > remaining:
            return f'Return quantity cannot exceed the remaining invoiced quantity of {remaining}.'
    return None


@pur_bp.route('/purchase/returns/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'goods_return_request', 'add')
def grr_add():
    try:
        f = request.form
        pi_id = int(f.get('pi_id')) if f.get('pi_id') else None
        pinv = PurchaseInvoice.query.get(pi_id) if pi_id else None
        if pi_id and (not pinv or pinv.status != 'Approved'):
            return jsonify({'ok': False, 'error': 'Selected Purchase Invoice is not Approved'}), 400

        err = _validate_grr_qty(f, pi_id)
        if err:
            return jsonify({'ok': False, 'error': err}), 400

        delivery_date = pd(f.get('delivery_date'))
        if delivery_date and delivery_date < date.today():
            return jsonify({'ok': False, 'error': 'Delivery date must be on/after the document date'}), 400

        doc = GoodsReturnRequest(
            doc_no=_next_doc_no('GRR', GoodsReturnRequest),
            purchase_invoice_id=pi_id,
            supplier_id=pinv.supplier_id if pinv else (int(f.get('supplier_id')) if f.get('supplier_id') else None),
            contact_person=f.get('contact_person','').strip(),
            supplier_ref_no=f.get('supplier_ref_no','').strip(),
            account_code=(pinv.account_code if pinv else f.get('account_code','').strip() or None),
            status=f.get('status','Open'),
            kind=pinv.kind if pinv else f.get('kind','Goods'),
            purchase_type=resolve_purchase_type(
                pinv.kind if pinv else f.get('kind','Goods'),
                pinv.purchase_type if pinv else f.get('purchase_type')),
            posting_date=pd(f.get('posting_date')) or date.today(),
            delivery_date=delivery_date,
            document_date=date.today(),
            created_by=current_user.id,
        )
        db.session.add(doc); db.session.flush()
        tots = _save_doc_line_items(GoodsReturnLineItem, 'goods_return_request_id', doc.goods_return_request_id, f,
                                     'purchase', source_link_field='purchase_invoice_line_item_id')
        for k,v in tots.items(): setattr(doc, k, v)
        _save_attachments('GRR', doc.goods_return_request_id, request.files.getlist('attachments'))
        db.session.commit(); return jsonify({'ok':True,'id':doc.goods_return_request_id,'doc_no':doc.doc_no})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in grr_add: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/returns/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'goods_return_request', 'edit')
def grr_edit(id):
    try:
        doc = GoodsReturnRequest.query.get_or_404(id); f = request.form
        pi_id = int(f.get('pi_id')) if f.get('pi_id') else None

        err = _validate_grr_qty(f, pi_id, exclude_grr_id=id)
        if err:
            return jsonify({'ok': False, 'error': err}), 400

        delivery_date = pd(f.get('delivery_date'))
        if delivery_date and delivery_date < date.today():
            return jsonify({'ok': False, 'error': 'Delivery date must be on/after the document date'}), 400

        pinv = PurchaseInvoice.query.get(pi_id) if pi_id else None
        doc.purchase_invoice_id=pi_id
        doc.supplier_id=pinv.supplier_id if pinv else (int(f.get('supplier_id')) if f.get('supplier_id') else None)
        doc.contact_person=f.get('contact_person','').strip()
        doc.supplier_ref_no=f.get('supplier_ref_no','').strip()
        doc.account_code = pinv.account_code if pinv else (f.get('account_code','').strip() or None)
        doc.status=f.get('status','Open')
        doc.kind=pinv.kind if pinv else f.get('kind','Goods')
        doc.purchase_type = resolve_purchase_type(doc.kind, pinv.purchase_type if pinv else f.get('purchase_type'))
        doc.posting_date  = pd(f.get('posting_date')) or date.today()
        doc.delivery_date = delivery_date
        doc.document_date = date.today()
        tots = _save_doc_line_items(GoodsReturnLineItem, 'goods_return_request_id', id, f,
                                     'purchase', source_link_field='purchase_invoice_line_item_id')
        for k,v in tots.items(): setattr(doc, k, v)
        _save_attachments('GRR', id, request.files.getlist('attachments'))
        db.session.commit(); return jsonify({'ok':True})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in grr_edit: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/returns/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'goods_return_request', 'delete')
def grr_delete(id):
    GoodsReturnLineItem.query.filter_by(goods_return_request_id=id).delete()
    PurchaseAttachment.query.filter_by(doc_type='GRR', doc_id=id).delete()
    db.session.delete(GoodsReturnRequest.query.get_or_404(id)); db.session.commit()
    return jsonify({'ok':True})


# ══════════════════════════════════════════════════════════════════
# PURCHASE RETURN NOTE (PRN)
#   Sits between Goods Return Request and Purchase Debit Memo:
#   GRR -> Purchase Return Note -> Purchase Debit Memo
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/return-notes')
@login_required
@permission_required('purchase', 'purchase_return_note', 'view')
def prn_list():
    grrs = [{'id':p.goods_return_request_id,'doc_no':p.doc_no} for p in GoodsReturnRequest.query.filter_by(status='Approved').order_by(GoodsReturnRequest.goods_return_request_id.desc()).all()]
    return render_template('purchase/prn_list.html', suppliers=_supplier_list(), grrs=grrs, owners=_owner_list())

@pur_bp.route('/purchase/return-notes/data')
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'view')
def prn_data():
    return jsonify([r.to_dict() for r in PurchaseReturnNote.query.order_by(PurchaseReturnNote.purchase_good_return_note_id.desc()).all()])

@pur_bp.route('/purchase/return-notes/<int:id>/json')
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'view')
def prn_json(id):
    doc = PurchaseReturnNote.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in PurchaseReturnNoteLineItem.query.filter_by(purchase_good_return_note_id=id).order_by(PurchaseReturnNoteLineItem.line_number).all()]
    d['attachments'] = [{'id':a.id,'filename':a.filename} for a in PurchaseAttachment.query.filter_by(doc_type='PRN', doc_id=id).all()]
    return jsonify(d)

@pur_bp.route('/purchase/return-notes/<int:id>/view')
@login_required
@permission_required('purchase', 'purchase_return_note', 'view')
def prn_view(id):
    doc = PurchaseReturnNote.query.get_or_404(id)
    return render_template('purchase/prn_view.html', doc=doc,
        items=PurchaseReturnNoteLineItem.query.filter_by(purchase_good_return_note_id=id).order_by(PurchaseReturnNoteLineItem.line_number).all(),
        attachments=PurchaseAttachment.query.filter_by(doc_type='PRN', doc_id=id).all())

@pur_bp.route('/purchase/return-notes/<int:id>/summary')
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'view')
def prn_summary(id):
    doc = PurchaseReturnNote.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in PurchaseReturnNoteLineItem.query.filter_by(purchase_good_return_note_id=id).order_by(PurchaseReturnNoteLineItem.line_number).all()]
    return jsonify(d)

def _apply_prn_fields(doc, f, is_new):
    """Header + line items, shared by prn_add/prn_edit. Mirrors
    _apply_grn_fields()/_apply_pinv_fields() for the other doc types."""
    grr_id = int(f.get('grr_id')) if f.get('grr_id') else None
    grr = GoodsReturnRequest.query.get(grr_id) if grr_id else None
    if is_new and grr_id and (not grr or grr.status != 'Approved'):
        raise ValueError('Selected Goods Return Request is not Approved')

    doc.purchase_good_return_request_id = grr_id
    doc.supplier_id      = int(f.get('supplier_id')) if f.get('supplier_id') else None
    doc.contact_person   = f.get('contact_person', '').strip()
    doc.supplier_ref      = f.get('supplier_ref', '').strip()
    doc.owner_id          = int(f.get('owner_id')) if f.get('owner_id') else None
    doc.account_code      = grr.account_code if grr else (f.get('account_code', '').strip() or None)
    doc.status            = f.get('status', 'Open')
    doc.kind              = grr.kind if grr else f.get('kind', 'Goods')
    doc.purchase_type     = resolve_purchase_type(doc.kind, grr.purchase_type if grr else f.get('purchase_type'))
    doc.posting_date      = pd(f.get('posting_date')) or date.today()
    doc.delivery_date     = pd(f.get('delivery_date'))
    doc.document_date     = date.today()
    db.session.flush()   # ensure doc.purchase_good_return_note_id exists
    tots = _save_doc_line_items(PurchaseReturnNoteLineItem, 'purchase_good_return_note_id', doc.purchase_good_return_note_id, f, 'purchase')
    for k, v in tots.items(): setattr(doc, k, v)
    _save_attachments('PRN', doc.purchase_good_return_note_id, request.files.getlist('attachments'))


@pur_bp.route('/purchase/return-notes/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'add')
def prn_add():
    try:
        doc = PurchaseReturnNote(doc_no=_next_doc_no('PRN', PurchaseReturnNote), created_by=current_user.id)
        db.session.add(doc)
        _apply_prn_fields(doc, request.form, is_new=True)
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.purchase_good_return_note_id, 'doc_no': doc.doc_no})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in prn_add: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/return-notes/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'edit')
def prn_edit(id):
    try:
        doc = PurchaseReturnNote.query.get_or_404(id)
        _apply_prn_fields(doc, request.form, is_new=False)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in prn_edit: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/return-notes/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'delete')
def prn_delete(id):
    PurchaseReturnNoteLineItem.query.filter_by(purchase_good_return_note_id=id).delete()
    PurchaseAttachment.query.filter_by(doc_type='PRN', doc_id=id).delete()
    GRL.query.filter_by(purchase_return_note_id=id).delete()
    db.session.delete(PurchaseReturnNote.query.get_or_404(id)); db.session.commit()
    return jsonify({'ok':True})


@pur_bp.route('/purchase/return-notes/next-grl-je-no')
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'view')
def prn_next_grl_je_no():
    """Preview the GRL No / Je No a new Purchase Return Note's GRL would
    get, shown in Add mode before anything is saved -- mirrors
    grn_next_grl_je_no()."""
    from models import next_je_no, NoActiveFinancialYearError
    grl_no = _next_grl_no()
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'ok': True, 'grl_no': grl_no, 'je_no': je_no})


@pur_bp.route('/purchase/return-notes/<int:prn_id>/grl-build')
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'view')
def grl_build_prn(prn_id):
    """Assemble the GRL view for a Purchase Return Note entirely from PRN
    data + Item Master -- mirrors grl_build() for GRN. Frontend-only
    display; nothing is saved."""
    prn = PurchaseReturnNote.query.get_or_404(prn_id)
    lines = (PurchaseReturnNoteLineItem.query
             .filter_by(purchase_good_return_note_id=prn_id)
             .order_by(PurchaseReturnNoteLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code='purchase_return_note', kind=prn.kind or 'Goods')
    supplier = SupplierMaster.query.get(prn.supplier_id) if prn.supplier_id else None
    _apply_supplier_control_account(grl_lines, supplier, side='debit', kind=prn.kind or 'Goods')
    return jsonify({
        'ok': True,
        'origion': prn.doc_no or '',
        'posting_date': prn.posting_date.isoformat() if prn.posting_date else '',
        'due_date': prn.delivery_date.isoformat() if getattr(prn, 'delivery_date', None) else '',
        'document_date': prn.document_date.isoformat() if getattr(prn, 'document_date', None) else '',
        'lines': grl_lines,
    })


@pur_bp.route('/purchase/return-notes/<int:prn_id>/grl')
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'view')
def grl_get_prn(prn_id):
    """Return the GRL (with details) for a Purchase Return Note, or an
    empty shell -- mirrors grl_get() for GRN."""
    prn = PurchaseReturnNote.query.get_or_404(prn_id)
    grl = GRL.query.filter_by(purchase_return_note_id=prn_id).first()
    if grl:
        return jsonify({'ok': True, 'grl': grl.to_dict()})
    return jsonify({'ok': True, 'grl': {
        'id': None, 'purchase_return_note_id': prn_id,
        'origion': prn.doc_no or '', 'grl_no': '', 'posting_date': '',
        'due_date': '', 'document_date': '', 'narration': '', 'details': [],
    }})


@pur_bp.route('/purchase/return-notes/post-and-save', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_return_note', 'post')
@block_in_basic_mode_json
def prn_post_and_save():
    """Post & Save: validate+save the Purchase Return Note (Step 1), then
    create/refresh its GRL master+detail (Step 3) and linked Journal Entry
    (Step 2) -- ALL in this one transaction, so a failure anywhere rolls
    back everything (no partially-posted document). Rejects re-posting a
    PRN whose posting_status is already 'Posted'. Mirrors
    grn_post_and_save()/pinv_post_and_save() exactly, just keyed off
    GRL.purchase_return_note_id / origin_type='PRN' /
    form_code='purchase_return_note'.
    """
    from models import JournalEntry, JournalEntryDetail, next_je_no, NoActiveFinancialYearError
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = PurchaseReturnNote.query.get_or_404(int(id_str))
            if doc.posting_status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Purchase Return Note has already been posted.',
                    'تم ترحيل مذكرة إرجاع الشراء هذه مسبقاً.')}), 400
            _apply_prn_fields(doc, f, is_new=False)
        else:
            doc = PurchaseReturnNote(doc_no=_next_doc_no('PRN', PurchaseReturnNote), created_by=current_user.id)
            db.session.add(doc)
            _apply_prn_fields(doc, f, is_new=True)

        posting_date  = pd(f.get('grl_posting_date')) or doc.posting_date or date.today()
        due_date      = pd(f.get('grl_due_date')) or doc.delivery_date
        if due_date and posting_date and posting_date > due_date:
            return jsonify({'ok': False, 'error': _t(
                'Posting Date must be on or before the Due Date',
                'يجب أن يكون تاريخ الترحيل قبل أو يساوي تاريخ الاستحقاق')}), 400

        grl = GRL.query.filter_by(purchase_return_note_id=doc.purchase_good_return_note_id).first()
        if not grl:
            grl = GRL(purchase_return_note_id=doc.purchase_good_return_note_id)
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
            je = JournalEntry(je_no=next_je_no(), origin_type='PRN', origin_id=doc.purchase_good_return_note_id)
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
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.purchase_good_return_note_id, 'doc_no': doc.doc_no, 'grl': grl.to_dict()})
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
        print(f"ERROR in prn_post_and_save: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# PURCHASE DEBIT MEMO (PDM)
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/debit-memos')
@login_required
@permission_required('purchase', 'purchase_debit_memo', 'view')
def pdm_list():
    prns = [{'id':p.purchase_good_return_note_id,'doc_no':p.doc_no,'goods_return_request_id':p.purchase_good_return_request_id}
            for p in PurchaseReturnNote.query.filter_by(status='Approved').order_by(PurchaseReturnNote.purchase_good_return_note_id.desc()).all()]
    return render_template('purchase/pdm_list.html', suppliers=_supplier_list(), prns=prns, owners=_owner_list())

@pur_bp.route('/purchase/debit-memos/data')
@login_required
@permission_required_json('purchase', 'purchase_debit_memo', 'view')
def pdm_data():
    return jsonify([r.to_dict() for r in PurchaseDebitMemo.query.order_by(PurchaseDebitMemo.purchase_debit_memo_id.desc()).all()])

@pur_bp.route('/purchase/debit-memos/<int:id>/json')
@login_required
@permission_required_json('purchase', 'purchase_debit_memo', 'view')
def pdm_json(id):
    doc = PurchaseDebitMemo.query.get_or_404(id)
    d = doc.to_dict()
    d['items'] = [i.to_dict() for i in PurchaseDebitMemoLineItem.query.filter_by(purchase_debit_memo_id=id).order_by(PurchaseDebitMemoLineItem.line_number).all()]
    d['attachments'] = [{'id':a.id,'filename':a.filename} for a in PurchaseAttachment.query.filter_by(doc_type='PDM', doc_id=id).all()]
    return jsonify(d)

@pur_bp.route('/purchase/debit-memos/<int:id>/view')
@login_required
@permission_required('purchase', 'purchase_debit_memo', 'view')
def pdm_view(id):
    doc = PurchaseDebitMemo.query.get_or_404(id)
    return render_template('purchase/pdm_view.html', doc=doc,
        items=PurchaseDebitMemoLineItem.query.filter_by(purchase_debit_memo_id=id).order_by(PurchaseDebitMemoLineItem.line_number).all(),
        attachments=PurchaseAttachment.query.filter_by(doc_type='PDM', doc_id=id).all())

def _apply_pdm_fields(doc, f, is_new):
    """Header + line items + attachments, shared by the plain-Save routes
    (pdm_add/pdm_edit) and pdm_post_and_save(). Mirrors
    _apply_prn_fields()/_apply_pinv_fields() for the other doc types.
    Sources from a Purchase Return Note (not directly from a Goods Return
    Request -- that's one step further up the chain, reached via the PRN's
    own goods_return_request link if needed)."""
    prn_id = int(f.get('prn_id')) if f.get('prn_id') else None
    prn = PurchaseReturnNote.query.get(prn_id) if prn_id else None
    if is_new and prn_id and (not prn or prn.status != 'Approved'):
        raise ValueError('Selected Purchase Return Note is not Approved')

    doc.purchase_return_note_id = prn_id
    doc.purchase_invoice_id = (prn.goods_return_request.purchase_invoice_id
                                if prn and prn.goods_return_request else None)
    doc.supplier_id = int(f.get('supplier_id')) if f.get('supplier_id') else None
    doc.contact_person = f.get('contact_person', '').strip()
    doc.supplier_ref_no = f.get('supplier_ref_no', '').strip()
    doc.owner_id = int(f.get('owner_id')) if f.get('owner_id') else None
    doc.account_code = prn.account_code if prn else (f.get('account_code', '').strip() or None)
    doc.status = f.get('status', 'Open')
    doc.kind = prn.kind if prn else f.get('kind', 'Goods')
    doc.purchase_type = resolve_purchase_type(doc.kind, prn.purchase_type if prn else f.get('purchase_type'))
    doc.payment_method = f.get('payment_method', 'Credit')
    doc.bank_account_id = int(f.get('bank_account_id')) if f.get('bank_account_id') else None
    doc.posting_date = pd(f.get('posting_date')) or date.today()
    doc.delivery_date = pd(f.get('delivery_date'))
    doc.document_date = date.today()
    db.session.flush()   # ensure doc.purchase_debit_memo_id exists
    tots = _save_doc_line_items(PurchaseDebitMemoLineItem, 'purchase_debit_memo_id', doc.purchase_debit_memo_id, f, 'purchase')
    for k, v in tots.items(): setattr(doc, k, v)
    _save_attachments('PDM', doc.purchase_debit_memo_id, request.files.getlist('attachments'))


@pur_bp.route('/purchase/debit-memos/add', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_debit_memo', 'add')
def pdm_add():
    try:
        doc = PurchaseDebitMemo(doc_no=_next_doc_no('PDM', PurchaseDebitMemo), created_by=current_user.id)
        db.session.add(doc)
        _apply_pdm_fields(doc, request.form, is_new=True)
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.purchase_debit_memo_id, 'doc_no': doc.doc_no})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in pdm_add: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500

@pur_bp.route('/purchase/debit-memos/<int:id>/edit', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_debit_memo', 'edit')
def pdm_edit(id):
    try:
        doc = PurchaseDebitMemo.query.get_or_404(id)
        _apply_pdm_fields(doc, request.form, is_new=False)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        print(f"ERROR in pdm_edit: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@pur_bp.route('/purchase/debit-memos/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_debit_memo', 'delete')
def pdm_delete(id):
    PurchaseDebitMemoLineItem.query.filter_by(purchase_debit_memo_id=id).delete()
    PurchaseAttachment.query.filter_by(doc_type='PDM', doc_id=id).delete()
    GRL.query.filter_by(purchase_debit_memo_id=id).delete()
    db.session.delete(PurchaseDebitMemo.query.get_or_404(id)); db.session.commit()
    return jsonify({'ok':True})


@pur_bp.route('/purchase/debit-memos/next-grl-je-no')
@login_required
@permission_required_json('purchase', 'purchase_debit_memo', 'view')
def pdm_next_grl_je_no():
    """Preview the GRL No / Je No a new Purchase Debit Memo's GRL would
    get, shown in Add mode before anything is saved -- mirrors
    grn_next_grl_je_no()."""
    from models import next_je_no, NoActiveFinancialYearError
    grl_no = _next_grl_no()
    try:
        je_no = next_je_no()
    except NoActiveFinancialYearError:
        je_no = ''
    return jsonify({'ok': True, 'grl_no': grl_no, 'je_no': je_no})


@pur_bp.route('/purchase/debit-memos/<int:pdm_id>/grl-build')
@login_required
@permission_required_json('purchase', 'purchase_debit_memo', 'view')
def grl_build_pdm(pdm_id):
    """Assemble the GRL view for a Purchase Debit Memo entirely from PDM
    data + Item Master -- mirrors grl_build() for GRN. Frontend-only
    display; nothing is saved."""
    pdm = PurchaseDebitMemo.query.get_or_404(pdm_id)
    lines = (PurchaseDebitMemoLineItem.query
             .filter_by(purchase_debit_memo_id=pdm_id)
             .order_by(PurchaseDebitMemoLineItem.line_number).all())
    grl_lines = _grl_lines_from(lines, form_code='purchase_debit_memo', kind=pdm.kind or 'Goods')
    supplier = SupplierMaster.query.get(pdm.supplier_id) if pdm.supplier_id else None
    _apply_supplier_control_account(grl_lines, supplier, side='debit', kind=pdm.kind or 'Goods')
    return jsonify({
        'ok': True,
        'origion': pdm.doc_no or '',
        'posting_date': pdm.posting_date.isoformat() if pdm.posting_date else '',
        'due_date': pdm.delivery_date.isoformat() if getattr(pdm, 'delivery_date', None) else '',
        'document_date': pdm.document_date.isoformat() if getattr(pdm, 'document_date', None) else '',
        'lines': grl_lines,
    })


@pur_bp.route('/purchase/debit-memos/<int:pdm_id>/grl')
@login_required
@permission_required_json('purchase', 'purchase_debit_memo', 'view')
def grl_get_pdm(pdm_id):
    """Return the GRL (with details) for a Purchase Debit Memo, or an
    empty shell -- mirrors grl_get() for GRN."""
    pdm = PurchaseDebitMemo.query.get_or_404(pdm_id)
    grl = GRL.query.filter_by(purchase_debit_memo_id=pdm_id).first()
    if grl:
        return jsonify({'ok': True, 'grl': grl.to_dict()})
    return jsonify({'ok': True, 'grl': {
        'id': None, 'purchase_debit_memo_id': pdm_id,
        'origion': pdm.doc_no or '', 'grl_no': '', 'posting_date': '',
        'due_date': '', 'document_date': '', 'narration': '', 'details': [],
    }})


@pur_bp.route('/purchase/debit-memos/post-and-save', methods=['POST'])
@login_required
@permission_required_json('purchase', 'purchase_debit_memo', 'post')
@block_in_basic_mode_json
def pdm_post_and_save():
    """Post & Save: validate+save the Purchase Debit Memo (Step 1), then
    create/refresh its GRL master+detail (Step 3) and linked Journal Entry
    (Step 2) -- ALL in this one transaction, so a failure anywhere rolls
    back everything (no partially-posted document). Rejects re-posting a
    PDM whose posting_status is already 'Posted'. Mirrors
    grn_post_and_save()/pinv_post_and_save()/prn_post_and_save() exactly,
    just keyed off GRL.purchase_debit_memo_id / origin_type='PDM' /
    form_code='purchase_debit_memo'.
    """
    from models import JournalEntry, JournalEntryDetail, next_je_no, NoActiveFinancialYearError
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = PurchaseDebitMemo.query.get_or_404(int(id_str))
            if doc.posting_status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Purchase Debit Memo has already been posted.',
                    'تم ترحيل إشعار مدين الشراء هذا مسبقاً.')}), 400
            _apply_pdm_fields(doc, f, is_new=False)
        else:
            doc = PurchaseDebitMemo(doc_no=_next_doc_no('PDM', PurchaseDebitMemo), created_by=current_user.id)
            db.session.add(doc)
            _apply_pdm_fields(doc, f, is_new=True)

        posting_date  = pd(f.get('grl_posting_date')) or doc.posting_date or date.today()
        due_date      = pd(f.get('grl_due_date')) or doc.delivery_date
        if due_date and posting_date and posting_date > due_date:
            return jsonify({'ok': False, 'error': _t(
                'Posting Date must be on or before the Due Date',
                'يجب أن يكون تاريخ الترحيل قبل أو يساوي تاريخ الاستحقاق')}), 400

        grl = GRL.query.filter_by(purchase_debit_memo_id=doc.purchase_debit_memo_id).first()
        if not grl:
            grl = GRL(purchase_debit_memo_id=doc.purchase_debit_memo_id)
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
            je = JournalEntry(je_no=next_je_no(), origin_type='PDM', origin_id=doc.purchase_debit_memo_id)
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
        return jsonify({'ok': True, 'id': doc.purchase_debit_memo_id, 'doc_no': doc.doc_no, 'grl': grl.to_dict()})
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
        print(f"ERROR in pdm_post_and_save: {str(e)}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# STORE MANAGEMENT — Stores (the 3 fixed master rows), Store Item Tracking
# (one row per Store Transaction) and PO Quantity Tracking (one row per PO
# line, Original/Received/Remaining computed live from Active Store
# Transactions). All three are read-only reporting grids -- quantity only
# ever moves via a GRN Post & Save (see _create_grn_store_transactions /
# _reverse_grn_store_transactions above), never edited directly here.
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/stores')
@login_required
@permission_required('store', 'stores', 'view')
def stores_master_list():
    return render_template('purchase/stores_list.html')


@pur_bp.route('/purchase/stores/data')
@login_required
@permission_required_json('store', 'stores', 'view')
def stores_master_data():
    from models import Store, StoreTransaction
    rows = []
    for s in Store.query.order_by(Store.id).all():
        balance = (db.session.query(db.func.coalesce(db.func.sum(StoreTransaction.quantity), 0))
                   .filter(StoreTransaction.store_id == s.id, StoreTransaction.status == 'Active')
                   .scalar())
        item_count = (db.session.query(db.func.count(db.func.distinct(StoreTransaction.item_id)))
                      .filter(StoreTransaction.store_id == s.id, StoreTransaction.status == 'Active')
                      .scalar())
        d = s.to_dict()
        d['balance_quantity'] = float(balance or 0)
        d['item_count'] = int(item_count or 0)
        d['created_at'] = s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else ''
        rows.append(d)
    return jsonify(rows)


@pur_bp.route('/purchase/store-tracking')
@login_required
@permission_required('store', 'store_item_tracking', 'view')
def store_tracking_list():
    return render_template('purchase/store_tracking_list.html')


@pur_bp.route('/purchase/store-tracking/data')
@login_required
@permission_required_json('store', 'store_item_tracking', 'view')
def store_tracking_data():
    from models import StoreTransaction
    rows = StoreTransaction.query.order_by(StoreTransaction.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@pur_bp.route('/purchase/po-tracking')
@login_required
@permission_required('purchase', 'po_quantity_tracking', 'view')
def po_tracking_list():
    return render_template('purchase/po_tracking_list.html')


@pur_bp.route('/purchase/po-tracking/data')
@login_required
@permission_required_json('purchase', 'po_quantity_tracking', 'view')
def po_tracking_data():
    from models import StoreTransaction
    rows = (PurchaseOrderLineItem.query
            .join(PurchaseOrder, PurchaseOrder.purchase_order_id == PurchaseOrderLineItem.purchase_order_id)
            .filter(PurchaseOrder.status == 'Approved')
            .order_by(PurchaseOrderLineItem.purchase_order_id.desc(), PurchaseOrderLineItem.line_number).all())
    out = []
    for li in rows:
        d = li.to_dict(with_progress=True)
        po = li.purchase_order
        d['po_doc_no'] = po.doc_no if po else ''
        d['po_status'] = po.status if po else ''
        last_txn = (StoreTransaction.query
                    .filter_by(purchase_order_line_item_id=li.purchase_order_line_item_id, status='Active')
                    .order_by(StoreTransaction.id.desc()).first())
        d['last_grn_doc_no'] = last_txn.goods_receipt_note_doc_no if last_txn else ''
        out.append(d)
    return jsonify(out)


@pur_bp.route('/purchase/po-tracking/<int:po_line_id>/receipts')
@login_required
@permission_required_json('purchase', 'po_quantity_tracking', 'view')
def po_tracking_receipts(po_line_id):
    """The 'View Receipts' drill-down: every Active receipt (Store
    Transaction) ever posted against this one PO line, newest first."""
    from models import StoreTransaction
    rows = (StoreTransaction.query
            .filter_by(purchase_order_line_item_id=po_line_id, status='Active')
            .order_by(StoreTransaction.id.desc()).all())
    return jsonify([r.to_dict() for r in rows])


# ══════════════════════════════════════════════════════════════════
# SUPPLIER ACCOUNT -- read-only ledger of every posting that hit a
# supplier's own control account (Purchase Invoice/Purchase Debit Memo,
# via _apply_supplier_control_account() -- see purchase.py). Sourced from
# JournalEntryDetail rather than GRLDetail since the two are always
# written identically for these three fields; there is no supplier_id FK
# on JournalEntryDetail, so a row is matched back to its supplier the same
# way the posting itself was tagged -- by reference_code == the
# supplier's own supplier_code.
# ══════════════════════════════════════════════════════════════════

@pur_bp.route('/purchase/supplier-account')
@login_required
@permission_required('purchase', 'supplier_account', 'view')
def supplier_account_list():
    return render_template('purchase/supplier_account_list.html')


@pur_bp.route('/purchase/supplier-account/data')
@login_required
@permission_required_json('purchase', 'supplier_account', 'view')
def supplier_account_data():
    from models import JournalEntry, JournalEntryDetail
    rows = (db.session.query(JournalEntryDetail, JournalEntry)
            .join(JournalEntry, JournalEntryDetail.journal_entry_id == JournalEntry.id)
            .filter(JournalEntryDetail.control_account == 'Yes',
                    JournalEntryDetail.reference_code.isnot(None),
                    JournalEntryDetail.reference_code != '')
            .order_by(JournalEntryDetail.reference_code, JournalEntry.posting_date,
                      JournalEntry.id, JournalEntryDetail.id)
            .all())

    ref_codes = {det.reference_code for det, je in rows}
    suppliers = {s.supplier_code: s for s in
                 SupplierMaster.query.filter(SupplierMaster.supplier_code.in_(ref_codes)).all()} if ref_codes else {}

    out = []
    running = {}
    for det, je in rows:
        ref = det.reference_code
        supplier = suppliers.get(ref)
        debit = float(det.debit or 0)
        credit = float(det.credit or 0)
        # Accounts Payable is a liability -- a supplier's balance is a
        # running Credit-minus-Debit, so a positive number reads as
        # "we currently owe this supplier that much".
        running[ref] = running.get(ref, 0.0) + credit - debit
        out.append({
            'detail_id': det.id,
            'reference_code': ref,
            'supplier_id': supplier.id if supplier else None,
            'supplier_name': supplier.supplier_name_en if supplier else '',
            'je_no': je.je_no or '',
            'origion': je.origion or '',
            'origin_type': je.origin_type or '',
            'posting_date': je.posting_date.isoformat() if je.posting_date else '',
            'code': det.code or '',
            'account_name': det.account_name or '',
            'narration': det.narration or '',
            'debit': debit,
            'credit': credit,
            'balance': running[ref],
        })
    return jsonify(out)