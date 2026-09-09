"""Lookups: allowance types routes.

Split out of the original monolithic lookups.py.
Routes register on the shared lookups_bp, so endpoint names are unchanged
(url_for('lookups.list_buyers') etc. keep working).
"""
from flask import (render_template, request, jsonify,
                   redirect, url_for, flash, session)
from flask_login import login_required, current_user

from models import db, AllowanceType
from .lookups import lookups_bp, admin_required, _t


def _next_allowance_code():
    """Generate the next sequential allowance code. Format: ALW-0001, ALW-0002, ..."""
    prefix = 'ALW'
    like = f'{prefix}-%'
    last = db.session.query(AllowanceType).filter(
        AllowanceType.allowance_code.like(like)
    ).order_by(AllowanceType.id.desc()).first()
    n = 1
    if last and last.allowance_code:
        try:
            n = int(last.allowance_code.split('-')[-1]) + 1
        except (ValueError, IndexError):
            n = AllowanceType.query.count() + 1
    code = f'{prefix}-{n:04d}'
    while AllowanceType.query.filter_by(allowance_code=code).first():
        n += 1
        code = f'{prefix}-{n:04d}'
    return code


@lookups_bp.route('/allowance-types/next-code')
@login_required
def allowance_type_next_code():
    return jsonify({'allowance_code': _next_allowance_code()})


@lookups_bp.route('/allowance-types/add-quick', methods=['POST'])
@login_required
def add_allowance_type_quick():
    d = request.get_json(silent=True) or {}
    code    = (d.get('allowance_code') or '').strip().upper()
    name_en = (d.get('allowance_name_en') or '').strip()
    name_ar = (d.get('allowance_name_ar') or '').strip()
    if not name_en:
        return jsonify({'ok': False, 'error': 'English name is required'}), 400
    existing = AllowanceType.query.filter(
        db.func.lower(AllowanceType.allowance_name_en) == name_en.lower()
    ).first()
    if existing:
        return jsonify({'ok': True, 'id': existing.id, 'duplicate': True})
    a = AllowanceType(
        allowance_code=code or _next_allowance_code(),
        allowance_name_en=name_en,
        allowance_name_ar=name_ar,
        is_active=True,
    )
    db.session.add(a)
    db.session.commit()
    return jsonify({'ok': True, 'id': a.id})
 

@lookups_bp.route('/allowance-types/data')
@login_required
def allowance_types_data():
    rows = AllowanceType.query.filter_by(is_active=True).order_by(AllowanceType.allowance_name_en).all()
    return jsonify([
        {'id': a.id, 'label': a.allowance_name_en, 'en': a.allowance_name_en,
         'ar': a.allowance_name_ar or '', 'code': a.allowance_code or ''}
        for a in rows
    ])

# ── ALLOWANCE TYPES MASTER ────────────────────────────────────────────
@lookups_bp.route('/allowance-types')
@login_required
def list_allowance_types():
    types = AllowanceType.query.order_by(AllowanceType.allowance_code).all()
    return render_template('employees/allowance_types.html', types=types)

@lookups_bp.route('/allowance-types/add', methods=['POST'])
@login_required
@admin_required
def add_allowance_type():
    # Code is auto-generated (see _next_allowance_code) and read-only on the
    # form -- if it's missing or was somehow already taken, generate a fresh
    # one here rather than rejecting the save.
    code = request.form.get('allowance_code','').strip().upper()
    en   = request.form.get('allowance_name_en','').strip()
    ar_n = request.form.get('allowance_name_ar','').strip()
    desc = request.form.get('description','').strip()
    active = request.form.get('is_active') == 'on'
    if not en:
        flash(_t('English name is required.','الاسم الإنجليزي مطلوب.'),'danger')
        return redirect(url_for('lookups.list_allowance_types'))
    if not code or AllowanceType.query.filter_by(allowance_code=code).first():
        code = _next_allowance_code()
    db.session.add(AllowanceType(allowance_code=code,allowance_name_en=en,allowance_name_ar=ar_n,description=desc,is_active=active))
    db.session.commit()
    flash(_t(f'Allowance type "{en}" added.',f'تم إضافة نوع البدل "{ar_n or en}".'),'success')
    return redirect(url_for('lookups.list_allowance_types'))

@lookups_bp.route('/allowance-types/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_allowance_type(id):
    t = AllowanceType.query.get_or_404(id)
    t.allowance_code    = request.form.get('allowance_code', t.allowance_code).strip().upper()
    t.allowance_name_en = request.form.get('allowance_name_en', t.allowance_name_en).strip()
    t.allowance_name_ar = request.form.get('allowance_name_ar', t.allowance_name_ar or '').strip()
    t.description       = request.form.get('description', t.description or '').strip()
    t.is_active         = request.form.get('is_active') == 'on'
    db.session.commit()
    flash(_t('Allowance type updated.','تم تحديث نوع البدل.'),'success')
    return redirect(url_for('lookups.list_allowance_types'))

def _allowance_type_delete_blockers(pk):
    """Label of the child rows that would block deleting this allowance type."""
    obj = AllowanceType.query.get(pk)
    if obj and obj.employee_allowances:
        return [_t('employee allowances', 'بدلات الموظفين')]
    return []


@lookups_bp.route('/allowance-types/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_allowance_type(id):
    from flask import current_app
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('allowance_type', id)
        flash(_t('Allowance type deleted -- moved to Recycle Bin.',
                 'تم حذف نوع البدل — نُقل إلى سلة المحذوفات.'), 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[delete_allowance_type] id=%s failed: %s', id, exc)
        flash(_t('Delete failed.', 'فشل الحذف.'), 'danger')
    return redirect(url_for('lookups.list_allowance_types'))