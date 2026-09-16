from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required
from models import db, PurchaseTaxCode
from database.routes.shared import block_trial_write_json
import re

purchase_tax_bp = Blueprint('purchase_tax', __name__)


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


# ── Page ────────────────────────────────────────────────────────
@purchase_tax_bp.route('/purchase-tax-codes')
@login_required
def purchase_tax_page():
    return render_template('purchase/purchase_taxcode.html',
                           kind='purchase', title_en='Purchase Tax Codes',
                           title_ar='رموز ضريبة الشراء')


# ── Data ────────────────────────────────────────────────────────
@purchase_tax_bp.route('/purchase-tax-codes/data')
@login_required
def purchase_tax_data():
    rows = PurchaseTaxCode.query.order_by(PurchaseTaxCode.tax_code).all()
    return jsonify([r.to_dict() for r in rows])


# ── Create ──────────────────────────────────────────────────────
@purchase_tax_bp.route('/purchase-tax-codes/add', methods=['POST'])
@login_required
@block_trial_write_json
def purchase_tax_add():
    f = request.form
    account_code = f.get('account_code', '').strip()
    tax_code     = f.get('tax_code', '').strip()
    section      = f.get('section', '').strip()
    status       = f.get('status', 'Active')
    if status not in ('Active', 'Inactive'):
        status = 'Active'

    # Validation
    if not account_code or not tax_code or not section:
        return jsonify({'ok': False, 'error': _t('Account Code, Tax Code and Section are required.',
                                                  'رمز الحساب والرمز الضريبي والوصف مطلوبة')}), 400

    # Store the account code as a percentage. The edit route does the same,
    # so append % rather than rejecting a value that arrives without it.
    if account_code and not account_code.endswith('%'):
        account_code = account_code + '%'

    # Tax code must be alphanumeric
    if not re.match(r'^[A-Za-z0-9]+$', tax_code):
        return jsonify({'ok': False, 'error': _t('Tax Code must contain only letters and numbers (e.g., P1)',
                                                  'الرمز الضريبي يجب أن يحتوي على حروف وأرقام فقط (مثال: P1)')}), 400

    if PurchaseTaxCode.query.filter_by(tax_code=tax_code).first():
        return jsonify({'ok': False, 'error': _t(f'Tax Code "{tax_code}" already exists.',
                                                  f'الرمز الضريبي "{tax_code}" موجود بالفعل')}), 400
    try:
        db.session.add(PurchaseTaxCode(account_code=account_code, tax_code=tax_code,
                                       section=section, status=status))
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Update (account_code, section, status; tax_code stays fixed) ─
@purchase_tax_bp.route('/purchase-tax-codes/<int:id>/edit', methods=['POST'])
@login_required
def purchase_tax_edit(id):
    row = PurchaseTaxCode.query.get_or_404(id)
    f = request.form
    account_code = f.get('account_code', '').strip()
    section      = f.get('section', '').strip()
    status = f.get('status', row.status)

    # Account code must end with %
    if account_code and not account_code.endswith('%'):
        account_code = account_code + '%'

    row.account_code = account_code
    row.section      = section
    row.status = status if status in ('Active', 'Inactive') else row.status
    try:
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Delete ──────────────────────────────────────────────────────
@purchase_tax_bp.route('/purchase-tax-codes/<int:id>/delete', methods=['POST'])
@login_required
@block_trial_write_json
def purchase_tax_delete(id):
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('purchase_tax_code', id)
        return jsonify({'ok': True})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500