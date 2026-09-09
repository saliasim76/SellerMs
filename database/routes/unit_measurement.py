"""
Unit of Measurement master — CRUD.

Table: item_unit
Fields: code (auto UOM-0001), name_en, name_ar, pac_size_en, pac_size_ar,
        multiply, status (Active / Inactive)
"""

from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required

from models import db, ItemUnit, next_uom_code

uom_bp = Blueprint('uom', __name__)


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ── Page ────────────────────────────────────────────────────────
@uom_bp.route('/unit-measurement')
@login_required
def uom_page():
    return render_template('unit_measurement/list.html')


# ── Data ────────────────────────────────────────────────────────
@uom_bp.route('/unit-measurement/data')
@login_required
def uom_data():
    rows = ItemUnit.query.order_by(ItemUnit.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


# ── Next code (for the "auto generated" field) ──────────────────
@uom_bp.route('/unit-measurement/next-code')
@login_required
def uom_next_code():
    return jsonify({'code': next_uom_code()})


# ── Create ──────────────────────────────────────────────────────
@uom_bp.route('/unit-measurement/add', methods=['POST'])
@login_required
def uom_add():
    f = request.form
    name_en = f.get('name_en', '').strip()
    name_ar = f.get('name_ar', '').strip()
    pac_size_en = f.get('pac_size_en', '').strip()
    pac_size_ar = f.get('pac_size_ar', '').strip()
    multiply = _num(f.get('multiply'))
    status = f.get('status', 'Active')
    if status not in ('Active', 'Inactive'):
        status = 'Active'

    if not name_en:
        return jsonify({'ok': False, 'error': _t('Name (English) is required.',
                                                 'الاسم (إنجليزي) مطلوب')}), 400

    row = ItemUnit(
        code=next_uom_code(),
        name_en=name_en, name_ar=name_ar or None,
        pac_size_en=pac_size_en or None, pac_size_ar=pac_size_ar or None,
        multiply=multiply, status=status,
    )
    try:
        db.session.add(row)
        db.session.commit()
        return jsonify({'ok': True, 'row': row.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Update ──────────────────────────────────────────────────────
@uom_bp.route('/unit-measurement/<int:id>/edit', methods=['POST'])
@login_required
def uom_edit(id):
    row = ItemUnit.query.get_or_404(id)
    f = request.form
    name_en = f.get('name_en', '').strip()
    if not name_en:
        return jsonify({'ok': False, 'error': _t('Name (English) is required.',
                                                 'الاسم (إنجليزي) مطلوب')}), 400

    row.name_en = name_en
    row.name_ar = f.get('name_ar', '').strip() or None
    row.pac_size_en = f.get('pac_size_en', '').strip() or None
    row.pac_size_ar = f.get('pac_size_ar', '').strip() or None
    row.multiply = _num(f.get('multiply'))
    status = f.get('status', row.status)
    row.status = status if status in ('Active', 'Inactive') else row.status
    try:
        db.session.commit()
        return jsonify({'ok': True, 'row': row.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Delete ──────────────────────────────────────────────────────
@uom_bp.route('/unit-measurement/<int:id>/delete', methods=['POST'])
@login_required
def uom_delete(id):
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('unit_measurement', id)
        return jsonify({'ok': True})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500