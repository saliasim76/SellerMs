"""Generic Recycle Bin engine.

Mirrors io_tools.py's "register once, engine applies generically" shape:
    register_recyclable(key, model, module_code, form_code, label_fn,
                        children=[{'key','model','fk'}], blocker_fn=None,
                        file_path_fields=None)
then soft_delete(key, pk) / restore_record(bin_id) / permanent_delete_record(bin_id)
apply generically against whatever was registered -- see recycle_registrations.py
for the actual per-module registrations (Employee, PurchaseOrder this phase).

Records stay in the bin for RETENTION_DAYS, after which either the
APScheduler daily job (app.py) or the lazy sweep in recycle_bin_list()
below purges them permanently.
"""
import json
import os
from datetime import datetime, date, timedelta
from decimal import Decimal

from flask import Blueprint, request, jsonify, session, render_template, current_app
from flask_login import login_required, current_user

from models import db, RecycleBin
from database.routes.audit import log_audit
from database.routes.rbac import permission_required, permission_required_json

recycle_bp = Blueprint('recycle_bin', __name__)

RETENTION_DAYS = 7

_REGISTRY = {}


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def register_recyclable(key, model, module_code, form_code, label_fn=None,
                        children=None, blocker_fn=None, file_path_fields=None):
    """
    key        : short id used when calling soft_delete(key, pk)
    model      : the SQLAlchemy model class (the parent/top-level record)
    module_code/form_code : rbac Module/SystemForm codes (permission checks + display)
    label_fn   : callable(obj) -> str, human label shown in the bin list (default: str(pk))
    children   : list of {'key': str, 'model': Model, 'fk': str, 'extra_filter': dict,
                 'parent_attr': str}
                 -- rows keyed by the parent's PK (via filter_by(**{fk: pk}, **extra_filter))
                 that must be archived + removed alongside the parent. extra_filter is
                 optional, for polymorphic child tables like PurchaseAttachment
                 (doc_type='PO', doc_id=<pk>) that aren't a plain single-column FK.
                 parent_attr is optional, for child tables whose FK references a natural
                 key on the parent (e.g. doc_no) rather than its primary key -- when set,
                 children are matched by filter_by(**{fk: getattr(parent, parent_attr)})
                 instead of the parent's PK value.
    blocker_fn : optional callable(pk) -> list[str] of blocking reasons; a non-empty
                 list aborts the delete entirely (e.g. employees.py's existing
                 _employee_delete_blockers)
    file_path_fields : optional list of (child_key, field_name) pairs whose value is a
                 relative file path under UPLOAD_FOLDER -- removed from disk only on
                 permanent delete, never on soft delete or restore
    """
    _REGISTRY[key] = {
        'model': model, 'module_code': module_code, 'form_code': form_code,
        'label_fn': label_fn, 'children': children or [], 'blocker_fn': blocker_fn,
        'file_path_fields': file_path_fields or [],
    }


def _find_spec(module_key, form_key):
    for spec in _REGISTRY.values():
        if spec['module_code'] == module_key and spec['form_code'] == form_key:
            return spec
    return None


def _serial(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _row_to_dict(obj):
    return {c.name: _serial(getattr(obj, c.name)) for c in obj.__table__.columns}


def _deserial(table, key, value):
    """Coerce a JSON-decoded value back to the type its column expects."""
    if value is None:
        return None
    col = table.columns.get(key)
    if col is None:
        return value
    try:
        py_type = col.type.python_type
    except Exception:
        return value
    try:
        if py_type is Decimal:
            return Decimal(str(value))
        if py_type is datetime and isinstance(value, str):
            return datetime.fromisoformat(value)
        if py_type is date and isinstance(value, str):
            return date.fromisoformat(value)
    except Exception:
        pass
    return value


def _current_user_id():
    try:
        if current_user and current_user.is_authenticated:
            return current_user.id
    except RuntimeError:
        pass
    return None


def soft_delete(key, pk_value, actor_username=None):
    """Move one record (+ registered children) into the Recycle Bin.
    Returns the new RecycleBin.id. Raises ValueError with a user-facing
    message if the record isn't found or blocker_fn rejects the delete."""
    spec = _REGISTRY[key]
    model = spec['model']
    obj = model.query.get(pk_value)
    if not obj:
        raise ValueError(_t('Record not found.', 'السجل غير موجود.'))

    if spec['blocker_fn']:
        blockers = spec['blocker_fn'](pk_value)
        if blockers:
            raise ValueError(_t(
                f'Cannot delete: child record(s) found ({", ".join(blockers)}).',
                f'تعذر الحذف: تم العثور على سجلات فرعية ({", ".join(blockers)}).'))

    parent_dict = _row_to_dict(obj)
    label = spec['label_fn'](obj) if spec['label_fn'] else str(pk_value)

    children_data = {}
    for child in spec['children']:
        cmodel = child['model']
        parent_val = getattr(obj, child['parent_attr']) if child.get('parent_attr') else pk_value
        filters = {child['fk']: parent_val, **child.get('extra_filter', {})}
        rows = cmodel.query.filter_by(**filters).all()
        children_data[child['key']] = [_row_to_dict(r) for r in rows]

    deleted_data = json.dumps({'parent': parent_dict, 'children': children_data})

    # Explicit child deletes first (uniform whether or not the parent model
    # also declares an ORM cascade -- if it does, the cascade simply finds
    # nothing left when it runs), then the parent.
    for child in spec['children']:
        cmodel = child['model']
        parent_val = getattr(obj, child['parent_attr']) if child.get('parent_attr') else pk_value
        filters = {child['fk']: parent_val, **child.get('extra_filter', {})}
        cmodel.query.filter_by(**filters).delete(synchronize_session=False)
    db.session.delete(obj)

    bin_row = RecycleBin(
        module_key=spec['module_code'], form_key=spec['form_code'],
        record_pk=pk_value, record_label=(label[:300] if label else None),
        deleted_data=deleted_data,
        deleted_by=_current_user_id(),
        deleted_at=datetime.utcnow(),
        purge_at=datetime.utcnow() + timedelta(days=RETENTION_DAYS),
        status='in_bin',
    )
    db.session.add(bin_row)
    db.session.commit()

    log_audit('delete', module_key=spec['module_code'], form_key=spec['form_code'],
              record_id=pk_value, old_value=label, actor_username=actor_username)
    return bin_row.id


def restore_record(bin_id, actor_username=None):
    bin_row = RecycleBin.query.get(bin_id)
    if not bin_row or bin_row.status != 'in_bin':
        raise ValueError(_t('This record is not available to restore.',
                            'هذا السجل غير متاح للاستعادة.'))

    spec = _find_spec(bin_row.module_key, bin_row.form_key)
    if not spec:
        raise ValueError(_t('Unknown record type.', 'نوع سجل غير معروف.'))

    model = spec['model']
    table = model.__table__

    if model.query.get(bin_row.record_pk):
        raise ValueError(_t(
            'The original record ID is now in use by a newer record -- '
            'permanently delete this bin entry instead of restoring it.',
            'رقم السجل الأصلي مستخدم الآن لسجل جديد — يرجى الحذف النهائي '
            'لهذا العنصر من السلة بدلاً من استعادته.'))

    data = json.loads(bin_row.deleted_data)
    parent_dict = {k: _deserial(table, k, v) for k, v in data.get('parent', {}).items()}

    # MySQL's AUTO_INCREMENT columns accept an explicit value straight in the
    # INSERT -- no MSSQL-style IDENTITY_INSERT toggle needed or supported.
    db.session.execute(table.insert().values(**parent_dict))
    db.session.flush()

    for child in spec['children']:
        cmodel = child['model']
        ctable = cmodel.__table__
        rows = data.get('children', {}).get(child['key'], [])
        if not rows:
            continue
        for row in rows:
            row_vals = {k: _deserial(ctable, k, v) for k, v in row.items()}
            db.session.execute(ctable.insert().values(**row_vals))
        db.session.flush()

    bin_row.status = 'restored'
    bin_row.restored_by = _current_user_id()
    bin_row.restored_at = datetime.utcnow()
    db.session.commit()

    log_audit('restore', module_key=spec['module_code'], form_key=spec['form_code'],
              record_id=bin_row.record_pk, new_value=bin_row.record_label,
              actor_username=actor_username)


def permanent_delete_record(bin_id, actor_username=None):
    """Deletes the recycle_bin row's archived data (marks it 'purged' rather
    than hard-deleting the row itself, so the audit trail of *that* deletion
    survives) plus any on-disk files it referenced. The live table rows were
    already removed at soft-delete time, so there's nothing else to cascade."""
    bin_row = RecycleBin.query.get(bin_id)
    if not bin_row or bin_row.status != 'in_bin':
        raise ValueError(_t('This record is not available to permanently delete.',
                            'هذا السجل غير متاح للحذف النهائي.'))

    spec = _find_spec(bin_row.module_key, bin_row.form_key)
    if spec and spec['file_path_fields']:
        data = json.loads(bin_row.deleted_data)
        for child_key, field_name in spec['file_path_fields']:
            for row in data.get('children', {}).get(child_key, []):
                relpath = row.get(field_name)
                if not relpath:
                    continue
                try:
                    full = os.path.join(current_app.config['UPLOAD_FOLDER'],
                                        *str(relpath).replace('\\', '/').split('/'))
                    if os.path.exists(full):
                        os.remove(full)
                except Exception:
                    pass

    label, module_key, form_key, record_pk = (
        bin_row.record_label, bin_row.module_key, bin_row.form_key, bin_row.record_pk)

    bin_row.status = 'purged'
    bin_row.permanently_deleted_by = _current_user_id()
    bin_row.permanently_deleted_at = datetime.utcnow()
    db.session.commit()

    log_audit('permanent_delete', module_key=module_key, form_key=form_key,
              record_id=record_pk, old_value=label, actor_username=actor_username)


def purge_expired():
    """Bulk-purge every bin row past its retention window. Called by the
    APScheduler daily job (app.py) and, as a belt-and-suspenders backup in
    case the app was down when the job would have fired, lazily every time
    the Recycle Bin screen loads."""
    now = datetime.utcnow()
    rows = RecycleBin.query.filter(RecycleBin.status == 'in_bin', RecycleBin.purge_at < now).all()
    count = 0
    for row in rows:
        try:
            permanent_delete_record(row.id, actor_username='system (auto-purge)')
            count += 1
        except Exception:
            db.session.rollback()
    return count


# ══════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════

@recycle_bp.route('/recycle-bin')
@login_required
@permission_required('administration', 'recycle_bin', 'view')
def recycle_bin_list():
    purge_expired()
    return render_template('admin/recycle_bin.html')


@recycle_bp.route('/recycle-bin/data')
@login_required
@permission_required_json('administration', 'recycle_bin', 'view')
def recycle_bin_data():
    q = RecycleBin.query.filter(RecycleBin.status == 'in_bin')
    module = request.args.get('module')
    user_id = request.args.get('user_id', type=int)
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    if module:
        q = q.filter(RecycleBin.module_key == module)
    if user_id:
        q = q.filter(RecycleBin.deleted_by == user_id)
    if date_from:
        q = q.filter(RecycleBin.deleted_at >= date_from)
    if date_to:
        q = q.filter(RecycleBin.deleted_at <= date_to + ' 23:59:59')
    rows = q.order_by(RecycleBin.deleted_at.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@recycle_bp.route('/recycle-bin/<int:bin_id>/restore', methods=['POST'])
@login_required
@permission_required_json('administration', 'recycle_bin', 'restore')
def recycle_bin_restore(bin_id):
    try:
        restore_record(bin_id)
        return jsonify({'ok': True})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[recycle-bin] restore id=%s failed: %s', bin_id, exc)
        return jsonify({'ok': False, 'error': _t('Restore failed.', 'فشلت الاستعادة.')}), 500


@recycle_bp.route('/recycle-bin/<int:bin_id>/permanent-delete', methods=['POST'])
@login_required
@permission_required_json('administration', 'recycle_bin', 'permanent_delete')
def recycle_bin_permanent_delete(bin_id):
    try:
        permanent_delete_record(bin_id)
        return jsonify({'ok': True})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[recycle-bin] permanent-delete id=%s failed: %s', bin_id, exc)
        return jsonify({'ok': False, 'error': _t('Delete failed.', 'فشل الحذف.')}), 500
