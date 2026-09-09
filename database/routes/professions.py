"""Lookups: professions routes.

Split out of the original monolithic lookups.py.
Routes register on the shared lookups_bp, so endpoint names are unchanged
(url_for('lookups.list_buyers') etc. keep working).
"""
from flask import (render_template, request, jsonify,
                   redirect, url_for, flash, session)
from flask_login import login_required, current_user

from models import db, ProfessionMaster, EmployeeProfession
from .lookups import lookups_bp, admin_required, _t


def _profession_delete_blockers(pk):
    """Label of the child rows that would block deleting this profession."""
    if EmployeeProfession.query.filter_by(profession_id=pk).first():
        return [_t('employee professions', 'مهن الموظفين')]
    return []


@lookups_bp.route('/professions')
@login_required
def list_professions():
    items = ProfessionMaster.query.order_by(ProfessionMaster.name_en).all()
    return render_template('employees/professions.html', items=items)

@lookups_bp.route('/professions/add', methods=['POST'])
@login_required
@admin_required
def add_profession():
    en = request.form.get('profession_en','').strip()
    ar = request.form.get('profession_ar','').strip()
    if not en:
        flash(_t('English name required','الاسم الإنجليزي مطلوب'),'danger')
        return redirect(url_for('lookups.list_professions'))
    if ProfessionMaster.query.filter_by(name_en=en).first():
        flash(_t(f'"{en}" already exists.',f'"{en}" موجود بالفعل'),'warning')
        return redirect(url_for('lookups.list_professions'))
    db.session.add(ProfessionMaster(name_en=en, name_ar=ar))
    db.session.commit()
    flash(_t(f'Profession "{en}" added.',f'تم إضافة المهنة "{ar or en}"'),'success')
    return redirect(url_for('lookups.list_professions'))

@lookups_bp.route('/professions/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_profession(id):
    p = ProfessionMaster.query.get_or_404(id)
    p.name_en = request.form.get('profession_en', p.name_en).strip()
    p.name_ar = request.form.get('profession_ar', p.name_ar or '').strip()
    p.is_active = request.form.get('is_active') == 'on'
    db.session.commit()
    flash(_t('Updated.','تم التحديث'),'success')
    return redirect(url_for('lookups.list_professions'))

@lookups_bp.route('/professions/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_profession(id):
    from flask import current_app
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('profession', id)
        flash(_t('Profession deleted -- moved to Recycle Bin.',
                 'تم حذف المهنة — نُقلت إلى سلة المحذوفات.'), 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[delete_profession] id=%s failed: %s', id, exc)
        flash(_t('Delete failed.', 'فشل الحذف.'), 'danger')
    return redirect(url_for('lookups.list_professions'))

@lookups_bp.route('/professions/add-quick', methods=['POST'])
@login_required
def add_profession_quick():
    d = request.get_json(silent=True) or {}
    name_en = (d.get('profession_en') or d.get('name_en') or '').strip()
    name_ar = (d.get('profession_ar') or d.get('name_ar') or '').strip()
    if not name_en:
        return jsonify({'ok': False, 'error': 'English name is required'}), 400
    existing = ProfessionMaster.query.filter(
        db.func.lower(ProfessionMaster.name_en) == name_en.lower()
    ).first()
    if existing:
        return jsonify({'ok': True, 'id': existing.id, 'duplicate': True})
    p = ProfessionMaster(name_en=name_en, name_ar=name_ar, is_active=True)
    db.session.add(p)
    db.session.commit()
    return jsonify({'ok': True, 'id': p.id})
 

@lookups_bp.route('/professions/data')
@login_required
def professions_data():
    rows = ProfessionMaster.query.filter_by(is_active=True).order_by(ProfessionMaster.name_en).all()
    return jsonify([
        {'id': p.id, 'label': p.name_en, 'en': p.name_en, 'ar': p.name_ar or ''}
        for p in rows
    ])