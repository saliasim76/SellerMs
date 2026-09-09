"""Lookups — blueprint + shared helpers + module registration.

The original 911-line lookups.py was split into focused modules that live
alongside this file in database/routes/:

    professions.py
    allowance_types.py
    buyers.py
    allowances.py
    items.py

They all register their routes on the lookups_bp defined here, so every
endpoint name is unchanged (lookups.list_buyers, lookups.items_list, ...).
"""
from functools import wraps

from flask import Blueprint, request, redirect, url_for, flash, session, jsonify
from flask_login import current_user, login_required

lookups_bp = Blueprint('lookups', __name__)


def admin_required(f):
    @wraps(f)
    def d(*a, **k):
        if not current_user.is_admin():
            flash('Access denied', 'danger')
            return redirect(request.referrer or url_for('dashboard.index'))
        return f(*a, **k)
    return d


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


@lookups_bp.route('/lookups/control-accounts')
@login_required
def control_accounts():
    """Active Level Five accounts flagged as Control Account = Yes, for the
    Chart of Account picker on Buyer/Supplier/Employee master records --
    those link to an AR/AP subsidiary control account, unlike Item Master
    (which links to a regular expense/inventory account and so is never
    restricted to control accounts; see item_level_five_accounts)."""
    from models import LevelFive

    _order = ['A', 'L', 'E', 'R', 'C', 'O', 'F', 'I']

    def _key(r):
        lead = (r.code or '')[:1].upper()
        rank = _order.index(lead) if lead in _order else len(_order)
        return (rank, r.code or '')

    rows = LevelFive.query.filter(
        LevelFive.status == 'active',
        LevelFive.control_account == 'Yes',
    ).all()
    rows = sorted(rows, key=_key)
    return jsonify([{'code': r.code, 'drawer': r.drawers or ''} for r in rows])


from . import professions
from . import allowance_types       
from . import buyers                
from . import allowances            
from . import items                 

__all__ = ['lookups_bp', 'admin_required', '_t']