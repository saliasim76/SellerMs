from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, session
from flask_login import login_required, current_user
from models import db, BankMaster
from functools import wraps

banks_bp = Blueprint('banks', __name__)

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash('Access denied.', 'danger')
            return redirect(url_for('banks.list_banks'))
        return f(*args, **kwargs)
    return decorated

def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en

@banks_bp.route('/banks')
@login_required
def list_banks():
    banks = BankMaster.query.order_by(BankMaster.bank_name_en).all()
    return render_template('banks/list.html', banks=banks)

@banks_bp.route('/banks/add', methods=['POST'])
@login_required
@admin_required
def add_bank():
    bank_name_en = request.form.get('bank_name_en', '').strip()
    bank_name_ar = request.form.get('bank_name_ar', '').strip()
    swift_code   = request.form.get('swift_code', '').strip()
    country      = request.form.get('country', 'Saudi Arabia').strip()
    if not bank_name_en:
        flash(_t('Bank name (English) is required.', 'اسم البنك بالإنجليزية مطلوب'), 'danger')
        return redirect(url_for('banks.list_banks'))
    # Check duplicate
    if BankMaster.query.filter_by(bank_name_en=bank_name_en).first():
        flash(_t(f'{bank_name_en} already exists.', f'{bank_name_en} موجود بالفعل'), 'warning')
        return redirect(url_for('banks.list_banks'))
    bank = BankMaster(bank_name_en=bank_name_en, bank_name_ar=bank_name_ar,
                      swift_code=swift_code, country=country)
    db.session.add(bank)
    db.session.commit()
    flash(_t(f'Bank "{bank_name_en}" added successfully.', f'تم إضافة البنك "{bank_name_ar or bank_name_en}" بنجاح'), 'success')
    return redirect(url_for('banks.list_banks'))

@banks_bp.route('/banks/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_bank(id):
    bank = BankMaster.query.get_or_404(id)
    bank.bank_name_en = request.form.get('bank_name_en', bank.bank_name_en).strip()
    bank.bank_name_ar = request.form.get('bank_name_ar', bank.bank_name_ar).strip()
    bank.swift_code   = request.form.get('swift_code', bank.swift_code).strip()
    bank.country      = request.form.get('country', bank.country).strip()
    bank.is_active    = request.form.get('is_active') == 'on'
    db.session.commit()
    flash(_t('Bank updated.', 'تم تحديث البنك'), 'success')
    return redirect(url_for('banks.list_banks'))

@banks_bp.route('/banks/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_bank_master(id):
    bank = BankMaster.query.get_or_404(id)
    db.session.delete(bank)
    db.session.commit()
    flash(_t('Bank deleted.', 'تم حذف البنك'), 'success')
    return redirect(url_for('banks.list_banks'))

@banks_bp.route('/banks/data')
@login_required
def banks_data():
    """API endpoint for owner form bank dropdown"""
    banks = BankMaster.query.filter_by(is_active=True).order_by(BankMaster.bank_name_en).all()
    lang  = session.get('lang', 'en')
    return jsonify([{
        'id': b.id,
        'en': b.bank_name_en,
        'ar': b.bank_name_ar or b.bank_name_en,
        'label': b.bank_name_ar if lang == 'ar' else b.bank_name_en,
        'swift': b.swift_code or '',
    } for b in banks])

@banks_bp.route('/banks/<int:id>/json')
@login_required
def bank_json(id):
    bank = BankMaster.query.get_or_404(id)
    return jsonify({'id': bank.id, 'bank_name_en': bank.bank_name_en,
                    'bank_name_ar': bank.bank_name_ar or '', 'swift_code': bank.swift_code or '',
                    'country': bank.country or '', 'is_active': bank.is_active})