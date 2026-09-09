from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required
from models import db, SalesTaxCode
import re

sales_tax_bp = Blueprint('sales_tax', __name__)


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


# ── Page ────────────────────────────────────────────────────────
@sales_tax_bp.route('/sales-tax-codes')
@login_required
def sales_tax_page():
    return render_template('sales/sale_taxcode.html',
                           kind='sales', title_en='Sales Tax Codes',
                           title_ar='رموز ضريبة البيع')


# ── Data ────────────────────────────────────────────────────────
@sales_tax_bp.route('/sales-tax-codes/data')
@login_required
def sales_tax_data():
    rows = SalesTaxCode.query.order_by(SalesTaxCode.tax_code).all()
    return jsonify([r.to_dict() for r in rows])


# ── Create ──────────────────────────────────────────────────────
@sales_tax_bp.route('/sales-tax-codes/add', methods=['POST'])
@login_required
def sales_tax_add():
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
        return jsonify({'ok': False, 'error': _t('Tax Code must contain only letters and numbers (e.g., S1)',
                                                  'الرمز الضريبي يجب أن يحتوي على حروف وأرقام فقط (مثال: S1)')}), 400

    if SalesTaxCode.query.filter_by(tax_code=tax_code).first():
        return jsonify({'ok': False, 'error': _t(f'Tax Code "{tax_code}" already exists.',
                                                  f'الرمز الضريبي "{tax_code}" موجود بالفعل')}), 400
    try:
        db.session.add(SalesTaxCode(account_code=account_code, tax_code=tax_code,
                                    section=section, status=status))
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Update (account_code, section, status; tax_code stays fixed) ─
@sales_tax_bp.route('/sales-tax-codes/<int:id>/edit', methods=['POST'])
@login_required
def sales_tax_edit(id):
    row = SalesTaxCode.query.get_or_404(id)
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
@sales_tax_bp.route('/sales-tax-codes/<int:id>/delete', methods=['POST'])
@login_required
def sales_tax_delete(id):
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('sales_tax_code', id)
        return jsonify({'ok': True})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500