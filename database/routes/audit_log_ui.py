"""Read-only Audit Log viewer. No edit/delete route exists here at all --
that's what actually satisfies "the audit log should not be editable or
deletable by normal users" (nothing to authorize around, because there is
no such endpoint), not merely a permission check that could be misconfigured.

The one exception is time-based retention: entries older than
AUDIT_LOG_RETENTION_DAYS are purged automatically by the system itself (the
APScheduler daily job in app.py, plus a lazy sweep on page load below,
mirroring the Recycle Bin's own purge_expired() in recycle_bin.py) -- this
is a system-initiated retention policy, not a delete capability exposed to
any user, so it doesn't weaken the guarantee above.
"""
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required

from models import db, User, ActivityLog
from database.routes.rbac import permission_required, permission_required_json, visible_users_query

audit_ui_bp = Blueprint('audit_ui', __name__, url_prefix='/admin/audit-log')

AUDIT_LOG_RETENTION_DAYS = 7


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def purge_expired_audit_log():
    """Bulk-delete every audit-log row older than the retention window.
    Called by the APScheduler daily job (app.py) and, as a belt-and-
    suspenders backup in case the app was down when the job would have
    fired, lazily every time the Audit Log screen loads."""
    cutoff = datetime.utcnow() - timedelta(days=AUDIT_LOG_RETENTION_DAYS)
    count = ActivityLog.query.filter(ActivityLog.created_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return count


def _entry_to_dict(e):
    return {
        'id': e.id, 'user_id': e.user_id,
        'username': e.username_snapshot or '',
        'action': e.action, 'module': e.target or '',
        'record_id': e.target_id, 'detail': e.detail or '',
        'old_value': e.old_value or '', 'new_value': e.new_value or '',
        'status': e.status or 'success', 'ip_address': e.ip_address or '',
        'created_at': e.created_at.strftime('%Y-%m-%d %H:%M:%S') if e.created_at else '',
    }


@audit_ui_bp.route('')
@login_required
@permission_required('administration', 'audit_log', 'view')
def audit_log_list():
    from flask_login import current_user
    purge_expired_audit_log()
    users = visible_users_query(current_user).order_by(User.username).all()
    return render_template('admin/audit_log.html', users=users)


@audit_ui_bp.route('/data')
@login_required
@permission_required_json('administration', 'audit_log', 'view')
def audit_log_data():
    q = ActivityLog.query
    user_id = request.args.get('user_id', type=int)
    action = request.args.get('action')
    module = request.args.get('module')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    if user_id:
        q = q.filter(ActivityLog.user_id == user_id)
    if action:
        q = q.filter(ActivityLog.action == action)
    if module:
        q = q.filter(ActivityLog.target == module)
    if date_from:
        q = q.filter(ActivityLog.created_at >= date_from)
    if date_to:
        q = q.filter(ActivityLog.created_at <= date_to + ' 23:59:59')
    rows = q.order_by(ActivityLog.created_at.desc()).limit(500).all()
    return jsonify([_entry_to_dict(e) for e in rows])
