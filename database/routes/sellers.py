import os, uuid
from datetime import datetime
from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, current_app, send_from_directory, jsonify, session)

def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en

from flask_login import login_required, current_user
from models import db, Owner, OwnerBank, OwnerDocument, ActivityLog, OwnerWarehouse
from forms import OwnerForm, BankForm, SearchForm
from functools import wraps

sellers_bp = Blueprint('sellers', __name__)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash(_t('Access denied. Admin privileges required.',
                     'الوصول مرفوض. يلزم صلاحيات المدير.'), 'danger')
            return redirect(url_for('sellers.list_owners'))
        return f(*args, **kwargs)
    return decorated


def _resolve_second_language(current_value=None):
    """Second Language is Super-Admin-only (Owner Form spec). Non-super-admin
    submissions are ignored here too, even if the disabled <select> in the
    UI were bypassed client-side -- the request field is only honored when
    the acting user is a Super Admin, and only for a value from the app's
    configured language list (English excluded)."""
    allowed = current_app.config.get('SECOND_LANGUAGES', {'Arabic': 'ar'})
    if not getattr(current_user, 'is_super_admin', False):
        return current_value or 'Arabic'
    submitted = (request.form.get('second_language') or '').strip()
    return submitted if submitted in allowed else (current_value or 'Arabic')


def generate_owner_code():
    last = Owner.query.order_by(Owner.id.desc()).first()
    num  = (last.id + 1) if last else 1
    return f'OWN-{num:05d}'


def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in \
           current_app.config.get('ALLOWED_EXTENSIONS',
                                   {'jpg', 'jpeg', 'png', 'pdf', 'docx', 'xlsx', 'doc'})


def save_file(file, owner_id):
    ext         = file.filename.rsplit('.', 1)[1].lower()
    unique_name = f'{uuid.uuid4().hex}.{ext}'
    folder      = os.path.join(current_app.config['UPLOAD_FOLDER'], str(owner_id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, unique_name)
    file.save(path)
    return os.path.join(str(owner_id), unique_name)


def log_activity(action, target, target_id, detail=''):
    try:
        db.session.add(ActivityLog(
            user_id    = current_user.id,
            action     = action,
            target     = target,
            target_id  = target_id,
            detail     = detail,
            ip_address = request.remote_addr,
        ))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# SERIALIZER
# ─────────────────────────────────────────────────────────────────────

def _doc_to_dict(doc):
    """Serialize a OwnerDocument for the frontend."""
    uploader = None
    if doc.uploaded_by:
        from models import User
        uploader = User.query.get(doc.uploaded_by)
    return {
        'id':               doc.id,
        'owner_id':        doc.owner_id,
        'document_type':    doc.document_type or '',
        'doc_type':         doc.document_type or '',
        'document_name':    doc.document_name or '',
        'doc_name':         doc.document_name or '',
        'issue_date':       str(getattr(doc, 'issue_date', '') or ''),
        'expiry_date':      str(doc.expiry_date or '') if doc.expiry_date else '',
        'uploaded_at':      doc.uploaded_at.strftime('%Y-%m-%d %H:%M') if doc.uploaded_at else '',
        'uploaded_by':      doc.uploaded_by,
        'uploaded_by_name': uploader.username if uploader else '',
        'file_path':        doc.file_path or '',
        'file_size_kb':     round((doc.file_size or 0) / 1024, 1),
    }


# ─────────────────────────────────────────────────────────────────────
# FORM HELPERS
# ─────────────────────────────────────────────────────────────────────

def _save_docs_from_form(owner_id):
    from datetime import date as _date
    i = 0
    submitted_ids = set()

    while True:
        prefix   = f'docs[{i}]'
        doc_type = request.form.get(f'{prefix}[doc_type]', '').strip()
        doc_name = request.form.get(f'{prefix}[doc_name]', '').strip()
        if not doc_type and not doc_name:
            break

        db_id_str  = request.form.get(f'{prefix}[_db_id]', '').strip()
        issue_raw  = request.form.get(f'{prefix}[issue_date]',  '').strip()
        expiry_raw = request.form.get(f'{prefix}[expiry_date]', '').strip()
        file_obj   = request.files.get(f'{prefix}[file]')

        if db_id_str:
            submitted_ids.add(int(db_id_str))
            doc = OwnerDocument.query.get(int(db_id_str))
            if doc and doc.owner_id == owner_id:
                doc.document_type = doc_type or doc.document_type
                doc.document_name = doc_name or doc.document_name
                if issue_raw:
                    try:   doc.issue_date = _date.fromisoformat(issue_raw)
                    except (ValueError, AttributeError): pass
                if expiry_raw:
                    try:   doc.expiry_date = _date.fromisoformat(expiry_raw)
                    except ValueError: pass
                if file_obj and file_obj.filename and allowed_file(file_obj.filename):
                    old = os.path.join(current_app.config['UPLOAD_FOLDER'], doc.file_path)
                    if os.path.exists(old): os.remove(old)
                    doc.file_path = save_file(file_obj, owner_id)
                    doc.file_size = os.path.getsize(
                        os.path.join(current_app.config['UPLOAD_FOLDER'], doc.file_path))
                db.session.add(doc)
        else:
            if not doc_type or not doc_name:
                i += 1; continue

            path      = ''
            file_size = 0
            if file_obj and file_obj.filename and allowed_file(file_obj.filename):
                path      = save_file(file_obj, owner_id)
                file_size = os.path.getsize(
                    os.path.join(current_app.config['UPLOAD_FOLDER'], path))

            doc = OwnerDocument(
                owner_id     = owner_id,
                document_type = doc_type,
                document_name = doc_name,
                file_path     = path,
                file_size     = file_size,
                uploaded_by   = current_user.id,
                uploaded_at   = datetime.utcnow(),
            )
            if issue_raw:
                try:   doc.issue_date = _date.fromisoformat(issue_raw)
                except (ValueError, AttributeError): pass
            if expiry_raw:
                try:   doc.expiry_date = _date.fromisoformat(expiry_raw)
                except ValueError: pass
            db.session.add(doc)
        i += 1

    if submitted_ids:
        existing_ids = {d.id for d in OwnerDocument.query.filter_by(owner_id=owner_id).all()}
        to_delete    = existing_ids - submitted_ids
        if to_delete:
            for doc in OwnerDocument.query.filter(
                    OwnerDocument.id.in_(to_delete),
                    OwnerDocument.owner_id == owner_id).all():
                if doc.file_path:
                    fpath = os.path.join(current_app.config['UPLOAD_FOLDER'], doc.file_path)
                    if os.path.exists(fpath): os.remove(fpath)
                db.session.delete(doc)


def _save_banks_from_form(owner_id):
    """
    Reads bank fields from request.form and saves/updates OwnerBank rows.
    Uses only existing fields: bank_name, account_number, branch, swift_code, iban
    """
    # Check for array format first (banks[0][bank_name])
    bank_name_array = request.form.get('banks[0][bank_name]', '').strip()
    
    if bank_name_array:
        # Array format - process banks[0] through banks[N]
        i = 0
        submitted_ids = set()
        
        while True:
            prefix = f'banks[{i}]'
            bank_name = request.form.get(f'{prefix}[bank_name]', '').strip()
            
            if not bank_name:
                break
            
            db_id_str = request.form.get(f'{prefix}[_db_id]', '').strip()
            is_primary = request.form.get(f'{prefix}[is_primary]', '0') == '1'
            account_number = request.form.get(f'{prefix}[account_number]', '').strip()
            swift_code = request.form.get(f'{prefix}[swift_code]', '').strip()
            iban = request.form.get(f'{prefix}[iban]', '').strip()
            branch = request.form.get(f'{prefix}[branch]', '').strip()
            
            if db_id_str:
                # Update existing bank
                submitted_ids.add(int(db_id_str))
                bank = OwnerBank.query.get(int(db_id_str))
                if bank and bank.owner_id == owner_id:
                    if is_primary:
                        OwnerBank.query.filter_by(owner_id=owner_id).update({'is_primary': False})
                    bank.bank_name = bank_name
                    bank.account_number = account_number
                    bank.swift_code = swift_code
                    bank.iban = iban
                    bank.branch = branch
                    bank.is_primary = is_primary
                    db.session.add(bank)
            else:
                # Create new bank
                if is_primary:
                    OwnerBank.query.filter_by(owner_id=owner_id).update({'is_primary': False})
                bank = OwnerBank(
                    owner_id=owner_id,
                    bank_name=bank_name,
                    account_number=account_number,
                    swift_code=swift_code,
                    iban=iban,
                    branch=branch,
                    is_primary=is_primary,
                )
                db.session.add(bank)
                db.session.flush()
                submitted_ids.add(bank.id)
            i += 1
        
        # Delete banks removed from form
        if i > 0:
            existing_ids = {b.id for b in OwnerBank.query.filter_by(owner_id=owner_id).all()}
            to_delete = existing_ids - submitted_ids
            if to_delete:
                OwnerBank.query.filter(
                    OwnerBank.id.in_(to_delete),
                    OwnerBank.owner_id == owner_id,
                ).delete(synchronize_session='fetch')
                
    else:
        # Simple format - single bank using direct field names
        bank_name = request.form.get('bank_name', '').strip()
        if bank_name:
            # Check if there's an existing bank
            existing_bank = OwnerBank.query.filter_by(owner_id=owner_id).first()
            
            is_primary = request.form.get('is_primary', '1') == '1'
            account_number = request.form.get('account_number', '').strip()
            swift_code = request.form.get('swift_code', '').strip()
            iban = request.form.get('iban', '').strip()
            branch = request.form.get('branch', '').strip()
            
            if existing_bank:
                # Update existing bank
                if is_primary:
                    OwnerBank.query.filter_by(owner_id=owner_id).update({'is_primary': False})
                existing_bank.bank_name = bank_name
                existing_bank.account_number = account_number
                existing_bank.swift_code = swift_code
                existing_bank.iban = iban
                existing_bank.branch = branch
                existing_bank.is_primary = is_primary
                db.session.add(existing_bank)
            else:
                # Create new bank
                if is_primary:
                    OwnerBank.query.filter_by(owner_id=owner_id).update({'is_primary': False})
                bank = OwnerBank(
                    owner_id=owner_id,
                    bank_name=bank_name,
                    account_number=account_number,
                    swift_code=swift_code,
                    iban=iban,
                    branch=branch,
                    is_primary=is_primary,
                )
                db.session.add(bank)


def _save_warehouses_from_form(owner_id):
    i = 0
    submitted_ids = set()
    any_rows = False

    while True:
        prefix = f'warehouses[{i}]'
        if f'{prefix}[warehouse_name]' not in request.form:
            break
        any_rows = True
        wh_name = request.form.get(f'{prefix}[warehouse_name]', '').strip()
        if not wh_name:
            i += 1
            continue

        db_id_str  = request.form.get(f'{prefix}[_db_id]', '').strip()
        wh_name_ar = request.form.get(f'{prefix}[warehouse_name_ar]', '').strip()
        location   = request.form.get(f'{prefix}[location]', '').strip()
        location_ar = request.form.get(f'{prefix}[location_ar]', '').strip()

        if db_id_str:
            submitted_ids.add(int(db_id_str))
            wh = OwnerWarehouse.query.get(int(db_id_str))
            if wh and wh.owner_id == owner_id:
                wh.warehouse_name    = wh_name
                wh.warehouse_name_ar = wh_name_ar
                wh.location          = location
                wh.location_ar       = location_ar
                db.session.add(wh)
        else:
            wh = OwnerWarehouse(
                owner_id         = owner_id,
                warehouse_name    = wh_name,
                warehouse_name_ar = wh_name_ar,
                location           = location,
                location_ar        = location_ar,
            )
            db.session.add(wh)
            db.session.flush()
            submitted_ids.add(wh.id)
        i += 1

    if any_rows or request.form.get('warehouses_submitted') == '1':
        existing_ids = {w.id for w in OwnerWarehouse.query.filter_by(owner_id=owner_id).all()}
        to_delete = existing_ids - submitted_ids
        if to_delete:
            OwnerWarehouse.query.filter(
                OwnerWarehouse.id.in_(to_delete),
                OwnerWarehouse.owner_id == owner_id,
            ).delete(synchronize_session='fetch')


# ═════════════════════════════════════════════════════════════════════
# SELLER ROUTES
# ═════════════════════════════════════════════════════════════════════

@sellers_bp.route('/owners')
@login_required
def list_owners():
    owner_form = OwnerForm()
    bank_form   = BankForm()
    return render_template('owners/list.html',
                           owner_form=owner_form, bank_form=bank_form)


@sellers_bp.route('/owners/add', methods=['POST'])
@login_required
@admin_required
def add_owner():
    if Owner.query.first():
        flash(_t('Only one Owner can be added.',
                 'يمكن إضافة مالك واحد فقط.'), 'danger')
        return redirect(url_for('sellers.list_owners'))

    owner = Owner(
        owner_code          = generate_owner_code(),
        created_by           = current_user.id,
        name                 = request.form.get('name',             '').strip(),
        name_ar              = request.form.get('name_ar',          '').strip(),
        vat_number           = request.form.get('vat_number',       '').strip(),
        crn                  = request.form.get('crn',              '').strip(),
        phone                = request.form.get('phone',            '').strip(),
        fax                  = request.form.get('fax',              '').strip(),
        email                = request.form.get('email',            '').strip(),
        website              = request.form.get('website',          '').strip(),
        smtp_host            = request.form.get('smtp_host',        '').strip() or None,
        smtp_port            = int(request.form.get('smtp_port') or 587),
        smtp_username        = request.form.get('smtp_username',    '').strip() or None,
        smtp_password        = request.form.get('smtp_password',    '').strip() or None,
        smtp_use_tls         = request.form.get('smtp_use_tls') == 'on',
        report_color         = request.form.get('report_color', '#16a34a').strip(),
        status               = request.form.get('status',      'active').strip(),
        street_name          = request.form.get('street_name',      '').strip(),
        street_name_ar       = request.form.get('street_name_ar',   '').strip(),
        building_number      = request.form.get('building_number',  '').strip(),
        additional_number    = request.form.get('additional_number',  '').strip(),
        district             = request.form.get('district',         '').strip(),
        district_ar          = request.form.get('district_ar',      '').strip(),
        city                 = request.form.get('city',             '').strip(),
        city_ar              = request.form.get('city_ar',          '').strip(),
        postal_code          = request.form.get('postal_code',      '').strip(),
        country              = request.form.get('country', 'Saudi Arabia').strip(),
        country_ar           = request.form.get('country_ar',       '').strip(),
        second_language      = _resolve_second_language(),
    )

    errors = []
    if not owner.name:  errors.append('Name is required')
    if not owner.email: errors.append('Email is required')
    if errors:
        for e in errors: flash(e, 'danger')
        return redirect(url_for('sellers.list_owners'))

    db.session.add(owner)
    db.session.flush()

    logo    = request.files.get('logo')
    bg_logo = request.files.get('bg_logo')
    header  = request.files.get('header')
    footer  = request.files.get('footer')
    stamp   = request.files.get('stamp')
    if logo    and logo.filename    and allowed_file(logo.filename):
        owner.logo_path    = save_file(logo,    owner.id)
    if bg_logo and bg_logo.filename and allowed_file(bg_logo.filename):
        owner.bg_logo_path = save_file(bg_logo, owner.id)
    if header  and header.filename  and allowed_file(header.filename):
        owner.header_path  = save_file(header,  owner.id)
    if footer  and footer.filename  and allowed_file(footer.filename):
        owner.footer_path  = save_file(footer,  owner.id)
    if stamp   and stamp.filename   and allowed_file(stamp.filename):
        owner.stamp_path   = save_file(stamp,   owner.id)

    log_activity('CREATE', 'owner', owner.id, f'Owner {owner.owner_code} created')
    _save_banks_from_form(owner.id)
    _save_docs_from_form(owner.id)
    _save_warehouses_from_form(owner.id)
    db.session.commit()
    flash(_t(f'Owner {owner.owner_code} added successfully!',
             f'تم إضافة المالك {owner.owner_code} بنجاح'), 'success')
    return redirect(url_for('sellers.list_owners'))


@sellers_bp.route('/owners/<int:id>/json')
@login_required
def owner_json(id):
    s = Owner.query.get_or_404(id)
    g = lambda f: getattr(s, f, None) or ''
    
    # Get primary bank or first bank
    primary_bank = OwnerBank.query.filter_by(owner_id=id, is_primary=True).first()
    if not primary_bank:
        primary_bank = OwnerBank.query.filter_by(owner_id=id).first()
    
    try:
        banks_data = [
            {
                'id': b.id,
                'bank_name': b.bank_name or '',
                'account_number': b.account_number or '',
                'swift_code': b.swift_code or '',
                'iban': b.iban or '',
                'branch': b.branch or '',
                'is_primary': b.is_primary,
            }
            for b in s.banks
        ]
    except Exception:
        banks_data = []
    
    try:
        docs_data = [_doc_to_dict(d) for d in s.documents]
    except Exception:
        docs_data = []
    
    return jsonify({
        'id':                    s.id,
        'owner_code':           g('owner_code'),
        'name':                  g('name'),             
        'name_ar':              g('name_ar'),
        'vat_number':            g('vat_number'),       
        'crn':                  g('crn'),
        'phone':                 g('phone'),             
        'fax':                  g('fax'),
        'email':                 g('email'),
        'website':              g('website'),
        'smtp_host':             g('smtp_host'),
        'smtp_port':             s.smtp_port or 587,
        'smtp_username':         g('smtp_username'),
        'smtp_password_set':     bool(s.smtp_password),
        'smtp_use_tls':          bool(s.smtp_use_tls) if s.smtp_use_tls is not None else True,
        'report_color':          g('report_color') or '#16a34a',
        'street_name':           g('street_name'),      
        'street_name_ar':       g('street_name_ar'),
        'building_number':       g('building_number'),  
        'additional_number':     g('additional_number'),
        'district':              g('district'),          
        'district_ar':          g('district_ar'),
        'city':                  g('city'),              
        'city_ar':              g('city_ar'),
        'postal_code':           g('postal_code'),       
        'country':               g('country'),           
        'country_ar':           g('country_ar'),
        'status':                g('status'),
        'second_language':       g('second_language') or 'Arabic',
        'banks':                 banks_data,
        # Direct bank fields for the form
        'bank_name':             getattr(primary_bank, 'bank_name', '') if primary_bank else '',
        'iban':                  getattr(primary_bank, 'iban', '') if primary_bank else '',
        'swift_code':            getattr(primary_bank, 'swift_code', '') if primary_bank else '',
        'account_number':        getattr(primary_bank, 'account_number', '') if primary_bank else '',
        'branch':                getattr(primary_bank, 'branch', '') if primary_bank else '',
        'documents':             docs_data,
    })


@sellers_bp.route('/owners/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_owner(id):
    owner = Owner.query.get_or_404(id)

    owner.name                 = request.form.get('name',             '').strip() or owner.name
    owner.name_ar              = request.form.get('name_ar',          '').strip()
    owner.vat_number           = request.form.get('vat_number',       '').strip()
    owner.crn                  = request.form.get('crn',              '').strip()
    owner.phone                = request.form.get('phone',            '').strip()
    owner.fax                  = request.form.get('fax',              '').strip()
    owner.email                = request.form.get('email',            '').strip() or owner.email
    owner.website              = request.form.get('website',          '').strip()
    owner.smtp_host            = request.form.get('smtp_host',        '').strip() or None
    owner.smtp_port            = int(request.form.get('smtp_port') or 587)
    owner.smtp_username        = request.form.get('smtp_username',    '').strip() or None
    owner.smtp_use_tls         = request.form.get('smtp_use_tls') == 'on'
    new_smtp_password = request.form.get('smtp_password', '').strip()
    if new_smtp_password:
        owner.smtp_password = new_smtp_password
    owner.report_color         = request.form.get('report_color', '#16a34a').strip()
    owner.status               = request.form.get('status',      'active').strip()
    owner.street_name          = request.form.get('street_name',      '').strip()
    owner.street_name_ar       = request.form.get('street_name_ar',   '').strip()
    owner.building_number      = request.form.get('building_number',  '').strip()
    owner.additional_number    = request.form.get('additional_number',   '').strip()
    owner.district             = request.form.get('district',         '').strip()
    owner.district_ar          = request.form.get('district_ar',      '').strip()
    owner.city                 = request.form.get('city',             '').strip()
    owner.city_ar              = request.form.get('city_ar',          '').strip()
    owner.postal_code          = request.form.get('postal_code',      '').strip()
    owner.country              = request.form.get('country',          '').strip()
    owner.country_ar           = request.form.get('country_ar',       '').strip()
    owner.second_language      = _resolve_second_language(owner.second_language)
    owner.updated_at           = datetime.utcnow()

    logo    = request.files.get('logo')
    bg_logo = request.files.get('bg_logo')
    header  = request.files.get('header')
    footer  = request.files.get('footer')
    stamp   = request.files.get('stamp')
    if logo    and logo.filename    and allowed_file(logo.filename):
        owner.logo_path    = save_file(logo,    owner.id)
    if bg_logo and bg_logo.filename and allowed_file(bg_logo.filename):
        owner.bg_logo_path = save_file(bg_logo, owner.id)
    if header  and header.filename  and allowed_file(header.filename):
        owner.header_path  = save_file(header,  owner.id)
    if footer  and footer.filename  and allowed_file(footer.filename):
        owner.footer_path  = save_file(footer,  owner.id)
    if stamp   and stamp.filename   and allowed_file(stamp.filename):
        owner.stamp_path   = save_file(stamp,   owner.id)

    log_activity('EDIT', 'owner', owner.id, f'Owner {owner.owner_code} updated')
    _save_banks_from_form(owner.id)
    _save_docs_from_form(owner.id)
    _save_warehouses_from_form(owner.id)
    db.session.commit()
    flash(_t('Owner updated successfully!', 'تم تحديث المالك بنجاح'), 'success')
    return redirect(url_for('sellers.list_owners'))


@sellers_bp.route('/owners/<int:id>/stamp')
@login_required
def owner_stamp_image(id):
    """Serve an Owner's uploaded stamp image directly -- used by the
    Sales/Purchase Quotation Terms & Conditions rich-text editor to insert
    the stamp inline as a normal <img src="...">."""
    from flask import abort
    owner = Owner.query.get_or_404(id)
    if not owner.stamp_path:
        abort(404)
    folder, fname = os.path.split(owner.stamp_path)
    abs_folder = os.path.join(current_app.config['UPLOAD_FOLDER'], folder)
    abs_path = os.path.join(abs_folder, fname)
    if not os.path.exists(abs_path):
        abort(404)
    return send_from_directory(abs_folder, fname)


@sellers_bp.route('/owners/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_owner(id):
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('owner', id)
        flash(_t('Owner deleted -- moved to Recycle Bin.',
                 'تم حذف المالك — نُقل إلى سلة المحذوفات.'), 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[delete_owner] id=%s failed: %s', id, exc)
        flash(_t('Delete failed.', 'فشل الحذف.'), 'danger')
    return redirect(url_for('sellers.list_owners'))


@sellers_bp.route('/owners/<int:id>/view')
@login_required
def view_owner(id):
    owner    = Owner.query.get_or_404(id)
    documents = (OwnerDocument.query.filter_by(owner_id=id)
                 .order_by(OwnerDocument.id.desc()).all())
    banks     = (OwnerBank.query.filter_by(owner_id=id)
                 .order_by(OwnerBank.id).all())
    activity  = (ActivityLog.query
                 .filter_by(target='owner', target_id=id)
                 .order_by(ActivityLog.created_at.desc())
                 .limit(50).all())
    return render_template('owners/view.html',
                           owner=owner, documents=documents,
                           banks=banks, activity=activity)


# ═════════════════════════════════════════════════════════════════════
# BANK ROUTES - FIXED: Proper routes for bank operations
# ═════════════════════════════════════════════════════════════════════

@sellers_bp.route('/owners/<int:id>/banks')
@login_required
def list_owner_banks(id):
    """List all banks for a owner"""
    banks = OwnerBank.query.filter_by(owner_id=id).order_by(OwnerBank.id).all()
    return jsonify([{
        'id': b.id,
        'bank_name': b.bank_name or '',
        'account_number': b.account_number or '',
        'swift_code': b.swift_code or '',
        'iban': b.iban or '',
        'branch': b.branch or '',
        'is_primary': b.is_primary,
    } for b in banks])


@sellers_bp.route('/owners/banks/<int:bid>/json')
@login_required
def owner_bank_json(bid):
    """Get single bank details"""
    bank = OwnerBank.query.get_or_404(bid)
    return jsonify({
        'id': bank.id,
        'bank_name': bank.bank_name or '',
        'account_number': bank.account_number or '',
        'swift_code': bank.swift_code or '',
        'iban': bank.iban or '',
        'branch': bank.branch or '',
        'is_primary': bank.is_primary,
    })


@sellers_bp.route('/owners/<int:id>/banks/add', methods=['POST'])
@login_required
@admin_required
def add_bank(id):
    """Add a new bank for a owner"""
    Owner.query.get_or_404(id)
    data = request.get_json() or {}
    
    if data.get('is_primary') in [True, 'true', '1', 'on']:
        OwnerBank.query.filter_by(owner_id=id).update({'is_primary': False})
    
    bank = OwnerBank(
        owner_id      = id,
        bank_name      = data.get('bank_name', '').strip(),
        account_number = data.get('account_number', '').strip(),
        branch         = data.get('branch', '').strip(),
        swift_code     = data.get('swift_code', '').strip(),
        iban           = data.get('iban', '').strip(),
        is_primary     = data.get('is_primary') in [True, 'true', '1', 'on'],
    )
    db.session.add(bank)
    db.session.commit()
    
    log_activity('ADD_BANK', 'owner', id, f'Added bank: {bank.bank_name}')
    return jsonify({'ok': True, 'id': bank.id})


@sellers_bp.route('/owners/banks/<int:bid>/edit', methods=['POST'])
@login_required
@admin_required
def edit_owner_bank(bid):
    """Edit an existing bank"""
    bank = OwnerBank.query.get_or_404(bid)
    data = request.get_json() or {}
    
    if data.get('is_primary') in [True, 'true', '1', 'on']:
        OwnerBank.query.filter_by(owner_id=bank.owner_id).update({'is_primary': False})
    
    bank.bank_name = data.get('bank_name', bank.bank_name).strip()
    bank.account_number = data.get('account_number', bank.account_number or '').strip()
    bank.branch = data.get('branch', bank.branch or '').strip()
    bank.swift_code = data.get('swift_code', bank.swift_code or '').strip()
    bank.iban = data.get('iban', bank.iban or '').strip()
    bank.is_primary = data.get('is_primary') in [True, 'true', '1', 'on']
    
    db.session.commit()
    log_activity('EDIT_BANK', 'owner', bank.owner_id, f'Edited bank: {bank.bank_name}')
    return jsonify({'ok': True})


@sellers_bp.route('/owners/banks/<int:bid>/delete', methods=['POST'])
@login_required
@admin_required
def delete_bank(bid):
    """Delete a bank"""
    bank = OwnerBank.query.get_or_404(bid)
    owner_id = bank.owner_id
    bank_name = bank.bank_name
    db.session.delete(bank)
    db.session.commit()
    log_activity('DELETE_BANK', 'owner', owner_id, f'Deleted bank: {bank_name}')
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════════
# DOCUMENT ROUTES
# ═════════════════════════════════════════════════════════════════════

@sellers_bp.route('/owners/<int:id>/documents/upload', methods=['POST'])
@login_required
def upload_document(id):
    is_ajax = bool(
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in request.headers.get('Accept', '')
        or request.headers.get('X-CSRFToken')
    )

    def _fail(msg):
        if is_ajax:
            return jsonify({'ok': False, 'error': msg})
        flash(msg, 'danger')
        return redirect(url_for('sellers.view_owner', id=id))

    Owner.query.get_or_404(id)
    file = request.files.get('file')
    if not file or not file.filename:
        return _fail(_t('No file selected.', 'لم يتم اختيار ملف'))
    if not allowed_file(file.filename):
        return _fail(_t('File type not allowed.', 'نوع الملف غير مسموح به'))

    from datetime import date as _date
    path = save_file(file, id)
    doc  = OwnerDocument(
        owner_id     = id,
        document_type = request.form.get('document_type', 'Other'),
        document_name = request.form.get('document_name', file.filename),
        file_path     = path,
        file_size     = os.path.getsize(
            os.path.join(current_app.config['UPLOAD_FOLDER'], path)),
        uploaded_by   = current_user.id,
        uploaded_at   = datetime.utcnow(),
    )
    raw_issue  = request.form.get('issue_date',  '')
    raw_expiry = request.form.get('expiry_date', '')
    if raw_issue:
        try:   doc.issue_date = _date.fromisoformat(raw_issue)
        except (ValueError, AttributeError): pass
    if raw_expiry:
        try:   doc.expiry_date = _date.fromisoformat(raw_expiry)
        except ValueError: pass

    db.session.add(doc)
    db.session.commit()
    log_activity('UPLOAD', 'owner', id, f'Document "{doc.document_name}" uploaded')

    if is_ajax:
        return jsonify({'ok': True, 'doc': _doc_to_dict(doc)})
    flash(_t('Document uploaded successfully.', 'تم رفع المستند بنجاح'), 'success')
    return redirect(url_for('sellers.view_owner', id=id))


@sellers_bp.route('/owners/<int:id>/documents/json')
@login_required
def list_owner_documents(id):
    docs = (OwnerDocument.query.filter_by(owner_id=id)
            .order_by(OwnerDocument.id).all())
    return jsonify([_doc_to_dict(d) for d in docs])


def _resolve_doc_path(doc):
    rel = (doc.file_path or '').replace('\\', '/')
    parts = rel.split('/')
    filename = parts[-1]
    abs_folder = os.path.join(
        current_app.config['UPLOAD_FOLDER'],
        *parts[:-1]
    )
    abs_filepath = os.path.join(abs_folder, filename)
    return abs_folder, filename, abs_filepath


@sellers_bp.route('/owners/documents/<int:did>/view')
@login_required
def view_document(did):
    doc = OwnerDocument.query.get_or_404(did)
    abs_folder, fname, abs_filepath = _resolve_doc_path(doc)

    if not os.path.exists(abs_filepath):
        from flask import abort
        abort(404, description=f'File not found on server: {abs_filepath}')

    ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''

    if ext in ('xlsx', 'xls', 'xlsm', 'xlsb', 'ods'):
        file_url  = url_for('sellers.download_document_alt', did=did, _external=True)
        friendly  = _friendly_name(doc, fname)
        html = f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>{friendly}</title>
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:'Segoe UI',sans-serif;background:#f1f5f9;}}
  header{{background:#1e3a5f;color:#fff;padding:12px 20px;display:flex;align-items:center;gap:12px;}}
  header h1{{font-size:15px;font-weight:700;margin:0;flex:1;}}
  header a{{background:#16a34a;color:#fff;padding:6px 16px;border-radius:8px;text-decoration:none;font-size:12px;font-weight:600;}}
  #sheet-tabs{{background:#fff;border-bottom:1px solid #e2e8f0;padding:8px 16px;display:flex;gap:6px;overflow-x:auto;}}
  .tab{{padding:5px 14px;border-radius:6px;border:1.5px solid #d1d5db;font-size:12px;cursor:pointer;background:#f8fafc;}}
  .tab.active{{background:#2563eb;color:#fff;border-color:#2563eb;}}
  #tbl-wrap{{overflow:auto;padding:16px;max-height:calc(100vh - 110px);}}
  table{{border-collapse:collapse;font-size:12.5px;background:#fff;width:100%;}}
  th{{background:#1e3a5f;color:#fff;padding:8px 12px;text-align:left;white-space:nowrap;font-size:11px;text-transform:uppercase;}}
  td{{padding:7px 12px;border-bottom:1px solid #f1f5f9;white-space:nowrap;}}
  tr:hover td{{background:#eff6ff;}}
  #loading{{text-align:center;padding:60px;color:#6b7280;font-size:14px;}}
</style>
</head><body>
<header>
  <h1>&#128196; {friendly}</h1>
  <a href="{file_url}" download="{friendly}">&#8595; Download</a>
</header>
<div id="sheet-tabs"></div>
<div id="tbl-wrap"><div id="loading">&#9203; Loading spreadsheet...</div></div>
<script>
fetch("{file_url}")
  .then(r=>r.arrayBuffer())
  .then(buf=>{{
    const wb = XLSX.read(new Uint8Array(buf),{{type:'array'}});
    const tabs = document.getElementById('sheet-tabs');
    wb.SheetNames.forEach((name,i)=>{{
      const btn=document.createElement('button');
      btn.className='tab'+(i===0?' active':'');
      btn.textContent=name;
      btn.onclick=()=>{{document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));btn.classList.add('active');render(wb,name);}};
      tabs.appendChild(btn);
    }});
    render(wb, wb.SheetNames[0]);
  }})
  .catch(e=>{{document.getElementById('loading').textContent='Error loading file: '+e;}});

function render(wb,name){{
  const ws=wb.Sheets[name];
  document.getElementById('tbl-wrap').innerHTML=XLSX.utils.sheet_to_html(ws,{{header:'',editable:false}});
}}
</script>
</body></html>"""
        from flask import Response
        return Response(html, mimetype='text/html')

    mime_map = {
        'pdf':'application/pdf',
        'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png',
        'gif':'image/gif','webp':'image/webp','svg':'image/svg+xml','bmp':'image/bmp',
    }
    if ext in mime_map:
        from flask import Response as _Resp
        with open(abs_filepath, 'rb') as f:
            data = f.read()
        resp = _Resp(data, mimetype=mime_map[ext])
        resp.headers['Content-Disposition'] = (
            f'inline; filename="{_friendly_name(doc, fname)}"'
        )
        return resp

    return send_from_directory(abs_folder, fname, as_attachment=True,
                               download_name=_friendly_name(doc, fname))


def _friendly_name(doc, raw_fname):
    ext  = raw_fname.rsplit('.', 1)[-1].lower() if '.' in raw_fname else ''
    base = (doc.document_name or '').strip()
    if not base:
        return raw_fname
    if ext and base.lower().endswith('.' + ext):
        return base
    return f'{base}.{ext}' if ext else base


@sellers_bp.route('/owners/documents/<int:did>/download')
@login_required
def download_document_alt(did):
    doc = OwnerDocument.query.get_or_404(did)
    abs_folder, fname, abs_filepath = _resolve_doc_path(doc)
    if not os.path.exists(abs_filepath):
        from flask import abort
        abort(404, description='File not found on server')
    return send_from_directory(abs_folder, fname,
                               as_attachment=True,
                               download_name=_friendly_name(doc, fname))


@sellers_bp.route('/documents/<int:did>/download')
@login_required
def download_document(did):
    return download_document_alt(did)


@sellers_bp.route('/documents/<int:did>/delete', methods=['POST'])
@login_required
@admin_required
def delete_document(did):
    doc = OwnerDocument.query.get_or_404(did)
    _, _, abs_filepath = _resolve_doc_path(doc)
    if os.path.exists(abs_filepath):
        os.remove(abs_filepath)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════════
# SELLER DOCUMENTS — STANDALONE AG-GRID PAGE
# ═════════════════════════════════════════════════════════════════════

@sellers_bp.route('/owner-documents')
@login_required
def list_owner_documents_page():
    return render_template('owners/owner_doc.html')


@sellers_bp.route('/owner-documents/data')
@login_required
def owner_documents_data():
    docs = OwnerDocument.query.order_by(OwnerDocument.id.desc()).all()
    rows = []
    for d in docs:
        from models import User
        owner   = Owner.query.get(d.owner_id) if d.owner_id else None
        uploader = User.query.get(d.uploaded_by)  if d.uploaded_by else None
        rows.append({
            'id':               d.id,
            'owner_id':        d.owner_id or '',
            'owner_name':      owner.name        if owner   else '',
            'owner_code':      owner.owner_code if owner   else '',
            'document_type':    d.document_type    or '',
            'doc_type':         d.document_type    or '',
            'document_name':    d.document_name    or '',
            'doc_name':         d.document_name    or '',
            'issue_date':       str(getattr(d, 'issue_date', '') or ''),
            'expiry_date':      str(d.expiry_date) if d.expiry_date else '',
            'uploaded_at':      d.uploaded_at.strftime('%Y-%m-%d %H:%M') if d.uploaded_at else '',
            'uploaded_by_name': uploader.username  if uploader else '',
            'file_size_kb':     round((d.file_size or 0) / 1024, 1),
            'file_path':        d.file_path        or '',
        })
    return jsonify(rows)


@sellers_bp.route('/owner-documents/<int:did>/json')
@login_required
def owner_document_json(did):
    doc = OwnerDocument.query.get_or_404(did)
    return jsonify(_doc_to_dict(doc))


@sellers_bp.route('/owner-documents/<int:did>/edit', methods=['POST'])
@login_required
@admin_required
def edit_owner_document(did):
    from datetime import date as _date
    doc  = OwnerDocument.query.get_or_404(did)
    data = request.get_json() or {}

    doc.document_type = data.get('document_type', doc.document_type) or doc.document_type
    doc.document_name = data.get('document_name', doc.document_name) or doc.document_name

    raw_issue  = data.get('issue_date',  '')
    raw_expiry = data.get('expiry_date', '')
    if raw_issue:
        try:   doc.issue_date = _date.fromisoformat(raw_issue)
        except (ValueError, AttributeError): pass
    try:       doc.expiry_date = _date.fromisoformat(raw_expiry) if raw_expiry else None
    except ValueError: pass

    db.session.commit()
    log_activity('EDIT', 'owner_document', did, f'Edited "{doc.document_name}"')
    return jsonify({'ok': True, 'doc': _doc_to_dict(doc)})


# ═════════════════════════════════════════════════════════════════════
# AG-GRID SELLERS DATA  /  EXPORT  /  TRANSLATE
# ═════════════════════════════════════════════════════════════════════

@sellers_bp.route('/owners/data')
@login_required
def owners_data():
    owners = Owner.query.order_by(Owner.created_at.desc()).all()
    rows = []
    for s in owners:
        rows.append({
            'id':                s.id,
            'owner_code':       s.owner_code       or '',
            'name':              s.name               or '',
            'name_ar':           getattr(s,'name_ar','') or '',
            'vat_number':        s.vat_number         or '',
            'crn':               s.crn                or '',
            'phone':             s.phone              or '',
            'fax':               s.fax                or '',
            'email':             s.email              or '',
            'website':           s.website            or '',
            'city':              s.city               or '',
            'country':           s.country            or '',
            'report_color':      s.report_color       or '#16a34a',
            'status':            s.status             or '',
            'created_at':        s.created_at.strftime('%Y-%m-%d') if s.created_at else '',
        })
    return jsonify(rows)


@sellers_bp.route('/owners/export')
@login_required
def export_owners():
    import csv, io
    from flask import make_response
    owners = Owner.query.all()
    output  = io.StringIO()
    writer  = csv.writer(output)
    writer.writerow(['Code','Name','VAT Number','CRN','Phone','Fax',
                     'Email','Website','City','Country','Status','Created'])
    for s in owners:
        writer.writerow([s.owner_code, s.name, s.vat_number or '', s.crn or '',
                         s.phone or '', s.fax or '', s.email or '', s.website or '',
                         s.city or '', s.country or '', s.status,
                         s.created_at.strftime('%Y-%m-%d') if s.created_at else ''])
    response = make_response(output.getvalue())
    response.headers['Content-Disposition'] = 'attachment; filename=sellers_export.csv'
    response.headers['Content-type'] = 'text/csv'
    return response


@sellers_bp.route('/translate', methods=['POST'])
@login_required
def translate_text():
    import urllib.request, urllib.parse, json as _json
    data      = request.get_json() or {}
    text      = (data.get('text') or '').strip()
    direction = data.get('direction', 'en|ar')
    if not text:
        return jsonify({'translated': ''})
    src, tgt = direction.split('|')
    try:
        params = urllib.parse.urlencode({'client':'gtx','sl':src,'tl':tgt,'dt':'t','q':text})
        url    = f'https://translate.googleapis.com/translate_a/single?{params}'
        req    = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=6) as r:
            result = _json.loads(r.read().decode('utf-8'))
        translated = ''.join(chunk[0] for chunk in result[0] if chunk[0])
        if translated:
            return jsonify({'translated': translated.strip()})
    except Exception:
        pass
    try:
        q   = urllib.parse.quote(text)
        url = f'https://api.mymemory.translated.net/get?q={q}&langpair={direction}'
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            result = _json.loads(r.read().decode())
        return jsonify({'translated': result.get('responseData',{}).get('translatedText','')})
    except Exception as e:
        return jsonify({'translated': '', 'error': str(e)})

# ═════════════════════════════════════════════════════════════════════
# WAREHOUSE ROUTES
# ═════════════════════════════════════════════════════════════════════

@sellers_bp.route('/owners/<int:id>/warehouses')
@login_required
def owner_warehouses(id):
    Owner.query.get_or_404(id)
    whs = OwnerWarehouse.query.filter_by(owner_id=id).order_by(OwnerWarehouse.id).all()
    return jsonify([w.to_dict() for w in whs])