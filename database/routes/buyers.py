"""Lookups: buyers routes.

Split out of the original monolithic lookups.py.
Routes register on the shared lookups_bp, so endpoint names are unchanged
(url_for('lookups.list_buyers') etc. keep working).
"""
from flask import (render_template, request, jsonify,
                   redirect, url_for, flash, session)
from flask_login import login_required, current_user

from models import db, BuyerMaster, BuyerBank
from .lookups import lookups_bp, admin_required, _t


@lookups_bp.route('/buyers/<int:buyer_id>/departments')
@login_required
def buyer_departments(buyer_id):
    """Departments belonging to a buyer (used to populate the edit form)."""
    from models import BuyerDepartment
    rows = (BuyerDepartment.query
            .filter_by(buyer_id=buyer_id)
            .order_by(BuyerDepartment.id).all())
    return jsonify([r.to_dict() for r in rows])


def _save_departments_from_form(buyer_id):
    """Replace a buyer's departments from `departments[i][field]` form fields.

    Rows the form no longer contains are deleted, so the DB always mirrors
    what the user sees in the table.
    """
    from models import BuyerDepartment
    import re as _re

    # Group the flat `departments[0][department_name]` keys by index.
    grouped = {}
    pattern = _re.compile(r'^departments\[(\d+)\]\[([a-zA-Z_]+)\]$')
    for key, val in request.form.items():
        m = pattern.match(key)
        if m:
            grouped.setdefault(int(m.group(1)), {})[m.group(2)] = (val or '').strip()

    submitted_ids = set()
    for _, row in sorted(grouped.items()):
        name = (row.get('department_name') or '').strip()
        if not name:
            continue                      # ignore blank rows
        loc_name    = (row.get('location_name') or '').strip()
        loc_name_ar = (row.get('location_name_ar') or '').strip()
        db_id = row.get('_dbId') or ''

        if db_id.isdigit():
            dep = BuyerDepartment.query.filter_by(id=int(db_id), buyer_id=buyer_id).first()
            if dep:
                dep.department_name = name
                dep.department_name_ar = (row.get('department_name_ar') or '').strip()
                dep.location_name = loc_name
                dep.location_name_ar = loc_name_ar
                submitted_ids.add(dep.id)
                continue

        dep = BuyerDepartment(
            buyer_id=buyer_id,
            department_name=name,
            department_name_ar=(row.get('department_name_ar') or '').strip(),
            location_name=loc_name,
            location_name_ar=loc_name_ar,
        )
        db.session.add(dep)
        db.session.flush()
        submitted_ids.add(dep.id)

    # Delete rows the user removed from the table.
    for dep in BuyerDepartment.query.filter_by(buyer_id=buyer_id).all():
        if dep.id not in submitted_ids:
            db.session.delete(dep)

    db.session.commit()



# ── BUYERS ───────────────────────────────────────────────────────────
@lookups_bp.route('/buyers')
@login_required
def list_buyers():
    buyers = BuyerMaster.query.order_by(BuyerMaster.buyer_name_en).all()
    return render_template('buyers/buyers.html', buyers=buyers)

@lookups_bp.route('/buyers/add', methods=['POST'])
@login_required
@admin_required
def add_buyer():
    from datetime import datetime as dt

    en = request.form.get('buyer_name_en','').strip()
    if not en:
        flash(_t('Buyer name is required.','اسم المشتري مطلوب.'),'danger')
        return redirect(url_for('lookups.list_buyers'))
    
    # Generate code — BUY-<active FY year>-<n>
    from models import active_fy_year
    _byear = active_fy_year()
    if not _byear:
        flash(_t('No active (Open) financial year. Please open a financial year first.',
                 'لا توجد سنة مالية مفتوحة. الرجاء فتح سنة مالية أولاً.'), 'danger')
        return redirect(url_for('lookups.list_buyers'))
    _blike = f'BUY-{_byear}-%'
    _bmax = 0
    for _br in BuyerMaster.query.filter(BuyerMaster.buyer_code.like(_blike)).all():
        try:
            _bn = int((_br.buyer_code or '').rsplit('-', 1)[1])
            if _bn > _bmax:
                _bmax = _bn
        except (ValueError, IndexError):
            continue
    code = f'BUY-{_byear}-{_bmax + 1}'
    
    b = BuyerMaster(
        buyer_code=code,
        buyer_name_en=en,
        buyer_name_ar=request.form.get('buyer_name_ar','').strip(),
        vat_number=request.form.get('vat_number','').strip(),
        crn=request.form.get('crn','').strip(),
        # department fields removed
        phone=request.form.get('phone','').strip(),
        fax=request.form.get('fax','').strip(),
        email=request.form.get('email','').strip(),
        website=request.form.get('website','').strip(),
        report_color=request.form.get('report_color','#2563eb').strip(),
        street_name=request.form.get('street_name','').strip(),
        street_name_ar=request.form.get('street_name_ar','').strip(),
        building_number=request.form.get('building_number','').strip(),
        additional_number=request.form.get('additional_number','').strip(),
        postal_code=request.form.get('postal_code','').strip(),
        country=request.form.get('country','Saudi Arabia').strip(),
        country_ar=request.form.get('country_ar','').strip(),
        city=request.form.get('city','').strip(),
        city_ar=request.form.get('city_ar','').strip(),
        district=request.form.get('district','').strip(),
        district_ar=request.form.get('district_ar','').strip(),
        status=request.form.get('status','active').strip(),
        is_active=request.form.get('status','active')=='active',
        salary_order=int(request.form.get('salary_order', 1)),
        levelfive_code=request.form.get('levelfive_code','').strip() or None,
        levelfive_drawer=request.form.get('levelfive_drawer','').strip() or None,
        created_by=current_user.id,
    )
    db.session.add(b)
    db.session.flush()  # Get b.id without committing
    
    # ── Save banks from form ──
    _save_banks_from_form(b.id)
    _save_departments_from_form(b.id)
    
    db.session.commit()
    flash(_t(f'Buyer {code} added.', f'تم إضافة المشتري {code}.'),'success')
    return redirect(url_for('lookups.list_buyers'))

@lookups_bp.route('/buyers/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_buyer(id):
    b = BuyerMaster.query.get_or_404(id)

    b.buyer_name_en  = request.form.get('buyer_name_en', b.buyer_name_en).strip()
    b.buyer_name_ar  = request.form.get('buyer_name_ar', b.buyer_name_ar or '').strip()
    b.vat_number     = request.form.get('vat_number','').strip()
    b.crn            = request.form.get('crn','').strip()
    # department fields removed
    b.phone          = request.form.get('phone','').strip()
    b.fax            = request.form.get('fax','').strip()
    b.email          = request.form.get('email','').strip()
    b.website        = request.form.get('website','').strip()
    b.report_color   = request.form.get('report_color','#2563eb').strip()
    b.street_name    = request.form.get('street_name','').strip()
    b.street_name_ar = request.form.get('street_name_ar','').strip()
    b.building_number= request.form.get('building_number','').strip()
    b.additional_number = request.form.get('additional_number','').strip()
    b.postal_code    = request.form.get('postal_code','').strip()
    b.country        = request.form.get('country','Saudi Arabia').strip()
    b.country_ar     = request.form.get('country_ar','').strip()
    b.city           = request.form.get('city','').strip()
    b.city_ar        = request.form.get('city_ar','').strip()
    b.district       = request.form.get('district','').strip()
    b.district_ar    = request.form.get('district_ar','').strip()
    b.status         = request.form.get('status','active').strip()
    b.is_active      = b.status == 'active'
    b.salary_order   = int(request.form.get('salary_order', 1))
    b.levelfive_code   = request.form.get('levelfive_code','').strip() or None
    b.levelfive_drawer = request.form.get('levelfive_drawer','').strip() or None

    # ── Save banks from form ──
    _save_banks_from_form(id)
    _save_departments_from_form(id)
    
    db.session.commit()
    flash(_t('Buyer updated.','تم تحديث المشتري.'),'success')
    return redirect(url_for('lookups.list_buyers'))


def _save_banks_from_form(buyer_id):
    """Process banks[] hidden fields from form submission.
    
    Expected format:
    banks[0][bank_name] = Al Rajhi
    banks[0][bank_name_ar] = الراجحي
    banks[0][account_number] = 12345
    banks[0][branch] = Main
    banks[0][branch_ar] = الرئيسي
    banks[0][swift_code] = RJHI
    banks[0][iban] = SA123
    banks[0][is_primary] = true
    banks[0][_dbId] = 5   (or empty for new)
    """
    submitted_ids = set()
    
    # Parse all form keys matching banks[index][field]
    banks_data = {}
    for key in request.form:
        if key.startswith('banks['):
            # Parse: banks[0][bank_name]
            rest = key[6:]  # Remove 'banks['
            close_bracket = rest.index(']')
            idx = rest[:close_bracket]
            field = rest[close_bracket+2:-1]  # Remove '][' and trailing ']'
            
            if idx not in banks_data:
                banks_data[idx] = {}
            banks_data[idx][field] = request.form[key]
    
    # Process each bank entry
    for idx, data in banks_data.items():
        bank_name = data.get('bank_name', '').strip()
        if not bank_name:
            print(f"  Skipping index {idx} - no bank_name")
            continue
        
        db_id = data.get('_dbId', '').strip()
        print(f"  Processing bank: name={bank_name}, _dbId={db_id}")
        
        if db_id:
            # Update existing bank
            bank = BuyerBank.query.get(int(db_id))
            if bank and bank.buyer_id == buyer_id:
                print(f"    Updating existing bank id={db_id}")
                bank.bank_name      = bank_name
                bank.bank_name_ar   = data.get('bank_name_ar', '').strip()
                bank.account_number = data.get('account_number', '').strip()
                bank.branch         = data.get('branch', '').strip()
                bank.branch_ar      = data.get('branch_ar', '').strip()
                bank.swift_code     = data.get('swift_code', '').strip()
                bank.iban           = data.get('iban', '').strip()
                bank.is_primary     = data.get('is_primary', 'false').lower() == 'true'
                submitted_ids.add(int(db_id))
            else:
                print(f"    Bank id={db_id} not found or wrong buyer, creating new")
                db_id = ''  # Force create new
        
        if not db_id:
            # Create new bank
            print(f"    Creating new bank")
            bank = BuyerBank(
                buyer_id=buyer_id,
                bank_name=bank_name,
                bank_name_ar=data.get('bank_name_ar', '').strip(),
                account_number=data.get('account_number', '').strip(),
                branch=data.get('branch', '').strip(),
                branch_ar=data.get('branch_ar', '').strip(),
                swift_code=data.get('swift_code', '').strip(),
                iban=data.get('iban', '').strip(),
                is_primary=data.get('is_primary', 'false').lower() == 'true',
            )
            db.session.add(bank)
            db.session.flush()
            submitted_ids.add(bank.id)
            print(f"    Created bank id={bank.id}")
    
    # Delete banks that were removed from the array
    existing_banks = BuyerBank.query.filter_by(buyer_id=buyer_id).all()
    for bank in existing_banks:
        if bank.id not in submitted_ids:
            print(f"  Deleting bank id={bank.id} (not in submitted_ids)")
            db.session.delete(bank)
    
    # Enforce single-primary rule
    primary_banks = BuyerBank.query.filter_by(buyer_id=buyer_id, is_primary=True).all()
    if len(primary_banks) > 1:
        print(f"  Fixing {len(primary_banks)} primary banks - keeping only last one")
        for b in primary_banks[:-1]:
            b.is_primary = False


@lookups_bp.route('/buyers/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_buyer(id):
    from flask import current_app
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('buyer', id)
        flash(_t('Buyer deleted -- moved to Recycle Bin.',
                 'تم حذف المشتري — نُقل إلى سلة المحذوفات.'), 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[delete_buyer] id=%s failed: %s', id, exc)
        flash(_t('Delete failed.', 'فشل الحذف.'), 'danger')
    return redirect(url_for('lookups.list_buyers'))

@lookups_bp.route('/buyers/<int:id>/json')
@login_required
def buyer_json(id):
    b = BuyerMaster.query.get_or_404(id)
    def g(f): return getattr(b,f,None) or ''
    return jsonify({
        'id':b.id,'buyer_code':b.buyer_code or '',
        'buyer_name_en':g('buyer_name_en'),'buyer_name_ar':g('buyer_name_ar'),
        'vat_number':g('vat_number'),'crn':g('crn'),
        # department fields removed
        'phone':g('phone'),'fax':g('fax'),'email':g('email'),'website':g('website'),
        'report_color':g('report_color') or '#2563eb',
        'street_name':g('street_name'),'street_name_ar':g('street_name_ar'),
        'building_number':g('building_number'),
        'additional_number':g('additional_number'),
        'postal_code':g('postal_code'),
        'country':g('country') or 'Saudi Arabia','country_ar':g('country_ar'),
        'city':g('city'),'city_ar':g('city_ar'),
        'district':g('district'),'district_ar':g('district_ar'),
        'status':g('status') or 'active',
        'salary_order': b.salary_order or 1,
    })

@lookups_bp.route('/buyers/data')
@login_required
def buyers_data():
    lang  = session.get('lang','en')
    items = BuyerMaster.query.order_by(BuyerMaster.buyer_name_en).all()
    return jsonify([{
        'id':       b.id,
        'buyer_code': b.buyer_code or '',
        'en':       b.buyer_name_en,
        'ar':       b.buyer_name_ar or b.buyer_name_en,
        'buyer_name_en': b.buyer_name_en or '',
        'buyer_name_ar': b.buyer_name_ar or '',
        'label':    b.buyer_name_ar if lang=='ar' and b.buyer_name_ar else b.buyer_name_en,
        'vat_number': b.vat_number or '',
        'crn':      b.crn or '',
        # department fields removed
        'phone':    b.phone or '',
        'email':    b.email or '',
        'status':   b.status or 'active',
        'is_active':b.is_active,
        'report_color': b.report_color or '#2563eb',
        'salary_order': b.salary_order or 1,
    } for b in items])

# ── BUYER BANKS API (for loading banks on edit) ──
@lookups_bp.route('/buyers/<int:buyer_id>/banks')
@login_required
def buyer_banks(buyer_id):
    """Returns banks for loading into JS array on edit"""
    banks = BuyerBank.query.filter_by(buyer_id=buyer_id).order_by(BuyerBank.id).all()
    return jsonify([b.to_dict() for b in banks])

# Legacy API endpoints kept for backward compatibility
@lookups_bp.route('/buyers/<int:buyer_id>/banks/add', methods=['POST'])
@login_required
@admin_required
def add_buyer_bank(buyer_id):
    BuyerMaster.query.get_or_404(buyer_id)
    data = request.get_json() or {}
    b = BuyerBank(
        buyer_id=buyer_id,
        bank_name=data.get('bank_name','').strip(),
        bank_name_ar=data.get('bank_name_ar','').strip(),
        account_number=data.get('account_number','').strip(),
        branch=data.get('branch','').strip(),
        branch_ar=data.get('branch_ar','').strip(),
        swift_code=data.get('swift_code','').strip(),
        iban=data.get('iban','').strip(),
        is_primary=bool(data.get('is_primary',False)),
    )
    if b.is_primary:
        BuyerBank.query.filter_by(buyer_id=buyer_id, is_primary=True).update({'is_primary':False})
    db.session.add(b)
    db.session.commit()
    return jsonify({'ok':True, 'bank':b.to_dict()})

@lookups_bp.route('/buyers/banks/<int:bank_id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_buyer_bank(bank_id):
    b = BuyerBank.query.get_or_404(bank_id)
    data = request.get_json() or {}
    b.bank_name     = data.get('bank_name', b.bank_name).strip()
    b.bank_name_ar  = data.get('bank_name_ar', b.bank_name_ar or '').strip()
    b.account_number= data.get('account_number', b.account_number or '').strip()
    b.branch        = data.get('branch', b.branch or '').strip()
    b.branch_ar     = data.get('branch_ar', b.branch_ar or '').strip()
    b.swift_code    = data.get('swift_code', b.swift_code or '').strip()
    b.iban          = data.get('iban', b.iban or '').strip()
    b.is_primary    = bool(data.get('is_primary', b.is_primary))
    if b.is_primary:
        BuyerBank.query.filter_by(buyer_id=b.buyer_id, is_primary=True).update({'is_primary':False})
        b.is_primary = True
    db.session.commit()
    return jsonify({'ok':True, 'bank':b.to_dict()})

@lookups_bp.route('/buyers/banks/<int:bank_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_buyer_bank(bank_id):
    b = BuyerBank.query.get_or_404(bank_id)
    db.session.delete(b)
    db.session.commit()
    return jsonify({'ok':True})


    