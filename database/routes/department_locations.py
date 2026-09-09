"""Lookups: department locations routes.

Split out of the original monolithic lookups.py.
Routes register on the shared lookups_bp, so endpoint names are unchanged
(url_for('lookups.list_buyers') etc. keep working).
"""
from flask import (render_template, request, jsonify,
                   redirect, url_for, flash, session)
from flask_login import login_required, current_user

from models import db
from .lookups import lookups_bp, admin_required, _t


@lookups_bp.route('/department-locations/data')
@login_required
def department_locations_data():
    from models import DepartmentLocation
    rows = DepartmentLocation.query.filter_by(is_active=True).order_by(DepartmentLocation.location_name).all()
    return jsonify([
        {'id': r.id, 'label': r.location_name, 'en': r.location_name, 'ar': r.location_name_ar or ''}
        for r in rows
    ])

@lookups_bp.route('/department-locations/add-quick', methods=['POST'])
@login_required
def add_department_location_quick():
    from models import DepartmentLocation
    data = request.get_json(silent=True) or {}
    en = (data.get('location_name') or data.get('name_en') or '').strip()
    ar = (data.get('location_name_ar') or data.get('name_ar') or '').strip()
    if not en:
        return jsonify({'ok': False, 'error': 'Location name is required'}), 400

    existing = DepartmentLocation.query.filter(
        db.func.lower(DepartmentLocation.location_name) == en.lower()
    ).first()
    if existing:
        return jsonify({'ok': True, 'id': existing.id, 'duplicate': True})

    loc = DepartmentLocation(location_name=en, location_name_ar=ar or en, is_active=True)
    db.session.add(loc)
    db.session.commit()
    return jsonify({'ok': True, 'id': loc.id})