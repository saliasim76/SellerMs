"""Centralized audit logging. Writes into the existing ActivityLog model
(table `activity_logs`) -- extended, not replaced (see models.py).

Call log_audit() explicitly at each mutation point (add/edit/delete/restore/
permanent-delete/status-change/permission-change/role-change/user-create/
user-deactivate), matching the manual-call-site pattern sellers.py already
established with its own log_activity() helper -- this just centralizes it
so every route file can use the same one instead of re-inventing it.

An audit-log write failure must never block the business operation it is
recording, so every function here swallows its own exceptions after
logging them.
"""
from flask import request, current_app
from flask_login import current_user

from models import db, ActivityLog


def _current_user_fields():
    """Safe even outside a request context (e.g. the APScheduler purge job,
    which only pushes an app context) -- current_user is request-local."""
    try:
        if current_user and getattr(current_user, 'is_authenticated', False):
            return current_user.id, current_user.username
    except RuntimeError:
        pass
    return None, None


def _remote_addr():
    try:
        return request.remote_addr
    except RuntimeError:
        return None


def log_audit(action, module_key=None, form_key=None, record_id=None,
              old_value=None, new_value=None, status='success', detail='',
              actor_username=None):
    """Write one audit-log row. Never raises. Safe to call from a plain
    Flask request OR from a background job with only an app context (no
    request) -- e.g. the Recycle Bin's scheduled purge.

    `actor_username` overrides the current_user-derived username -- needed
    for a failed login (no authenticated current_user yet) and for
    system-initiated actions like the auto-purge job.
    """
    try:
        user_id, username = _current_user_fields()
        username = actor_username or username
        full_detail = f'[{form_key}] {detail}'.strip() if form_key else (detail or None)
        entry = ActivityLog(
            user_id=user_id,
            username_snapshot=username,
            action=action,
            target=module_key,
            target_id=record_id,
            detail=full_detail or None,
            ip_address=_remote_addr(),
            old_value=(str(old_value) if old_value is not None else None),
            new_value=(str(new_value) if new_value is not None else None),
            status=status,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        try:
            current_app.logger.exception('[audit] failed to write log entry: %s', exc)
        except Exception:
            pass


def log_login_success(user):
    log_audit('login', module_key='auth', form_key='login', record_id=user.id,
              status='success', detail=f'{user.username} logged in')


def log_login_failed(username):
    log_audit('login_failed', module_key='auth', form_key='login', record_id=None,
              status='failed', detail=f'failed login attempt for "{username}"',
              actor_username=username)


def log_logout(user):
    log_audit('logout', module_key='auth', form_key='login', record_id=user.id,
              status='success', detail=f'{user.username} logged out')
