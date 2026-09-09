"""User Management -- CRUD for application users.

Super Admin invisibility is enforced by routing every list/lookup through
rbac.visible_users_query() (the backend filter, not a template conditional),
and every mutation route re-fetches the target user through that same
filtered query -- so a non-Super-Admin can't even load a Super Admin's edit
form by guessing an ID. is_protected accounts (the seeded SuperAdmin) can't
be edited/deactivated/role-changed through any route here, and a user can
never change their own role, status, or permission overrides.
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, session
from flask_login import login_required, current_user
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models import db, User, Role
from database.routes.rbac import permission_required, permission_required_json, visible_users_query
from database.routes.audit import log_audit

user_mgmt_bp = Blueprint('user_mgmt', __name__, url_prefix='/admin/users')


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


# ── Child-record check for user deletion ────────────────────────────
# Discovered dynamically from the live schema (every FK constraint that
# targets users.id) rather than a hand-maintained list, so this stays
# complete as new document types/tables are added later without needing
# to remember to update it here too.
_child_fk_cache = None


def _child_fk_columns():
    global _child_fk_cache
    if _child_fk_cache is None:
        rows = db.session.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'users'"
        )).fetchall()
        _child_fk_cache = [(r[0], r[1]) for r in rows]
    return _child_fk_cache


def user_has_child_records(user_id):
    """True if any table in the live schema has a row referencing this user
    (created_by/uploaded_by/user_id/etc.) -- deleting the user would break
    referential integrity, so the Delete action must be blocked rather than
    left to fail with a raw database error."""
    for table, col in _child_fk_columns():
        row = db.session.execute(
            text(f'SELECT 1 FROM `{table}` WHERE `{col}` = :uid LIMIT 1'), {'uid': user_id}
        ).fetchone()
        if row:
            return True
    return False


def _user_to_dict(u):
    return {
        'id': u.id, 'username': u.username, 'email': u.email or '',
        'full_name': u.full_name or '', 'mobile': u.mobile or '',
        'role': u.role, 'role_id': u.role_id,
        'role_name': (u.role_ref.name if u.role_ref else ''),
        'is_active': u.is_active, 'is_protected': bool(u.is_protected),
        'has_child': user_has_child_records(u.id),
        'last_login_at': u.last_login_at.strftime('%Y-%m-%d %H:%M') if u.last_login_at else '',
        'created_at': u.created_at.strftime('%Y-%m-%d') if u.created_at else '',
    }


@user_mgmt_bp.route('')
@login_required
@permission_required('administration', 'users', 'view')
def list_users():
    # Super Admin role itself excluded from the assignable-role dropdown --
    # that account is seeded once, not created/reassigned through this UI.
    roles = Role.query.filter(Role.code != 'super_admin').order_by(Role.id).all()
    return render_template('admin/users_list.html', roles=roles)


@user_mgmt_bp.route('/data')
@login_required
@permission_required_json('administration', 'users', 'view')
def users_data():
    rows = visible_users_query(current_user).order_by(User.username).all()
    return jsonify([_user_to_dict(u) for u in rows])


@user_mgmt_bp.route('/add', methods=['POST'])
@login_required
@permission_required('administration', 'users', 'add')
def add_user():
    f = request.form
    username = (f.get('username') or '').strip()
    email = (f.get('email') or '').strip()
    password = f.get('password') or ''
    if not username or not email or not password:
        flash(_t('Username, email and password are required.',
                 'اسم المستخدم والبريد الإلكتروني وكلمة المرور مطلوبة.'), 'danger')
        return redirect(url_for('user_mgmt.list_users'))
    if User.query.filter_by(username=username).first():
        flash(_t('Username already exists.', 'اسم المستخدم موجود بالفعل.'), 'danger')
        return redirect(url_for('user_mgmt.list_users'))

    role_id = f.get('role_id', type=int)
    role = Role.query.get(role_id) if role_id else None
    if role and role.code == 'super_admin':
        flash(_t('Super Admin accounts cannot be created here.',
                 'لا يمكن إنشاء حسابات المدير الأعلى من هنا.'), 'danger')
        return redirect(url_for('user_mgmt.list_users'))

    u = User(
        username=username, email=email,
        full_name=(f.get('full_name') or '').strip(),
        mobile=(f.get('mobile') or '').strip(),
        role=('admin' if role and role.code == 'admin' else 'user'),
        role_id=(role.id if role else None),
        is_active=(f.get('is_active') == 'on'),
        created_by=current_user.id,
    )
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    log_audit('user_create', module_key='administration', form_key='users',
              record_id=u.id, new_value=username)
    flash(_t('User created.', 'تم إنشاء المستخدم.'), 'success')
    return redirect(url_for('user_mgmt.list_users'))


@user_mgmt_bp.route('/<int:id>/edit', methods=['POST'])
@login_required
@permission_required('administration', 'users', 'edit')
def edit_user(id):
    u = visible_users_query(current_user).filter_by(id=id).first_or_404()
    if u.is_protected:
        flash(_t('This account is protected and cannot be edited.',
                 'هذا الحساب محمي ولا يمكن تعديله.'), 'danger')
        return redirect(url_for('user_mgmt.list_users'))

    f = request.form
    role_id = f.get('role_id', type=int)
    if role_id and role_id != u.role_id:
        if u.id == current_user.id:
            flash(_t('You cannot change your own role.', 'لا يمكنك تغيير دورك الخاص.'), 'danger')
            return redirect(url_for('user_mgmt.list_users'))
        role = Role.query.get(role_id)
        if role and role.code == 'super_admin':
            flash(_t('Super Admin cannot be assigned here.',
                     'لا يمكن تعيين المدير الأعلى من هنا.'), 'danger')
            return redirect(url_for('user_mgmt.list_users'))
        old_role_name = u.role_ref.name if u.role_ref else ''
        u.role_id = role_id
        if role:
            u.role = 'admin' if role.code == 'admin' else 'user'
        log_audit('role_change', module_key='administration', form_key='users',
                  record_id=u.id, old_value=old_role_name, new_value=(role.name if role else ''))

    u.full_name = (f.get('full_name') or u.full_name or '').strip()
    u.email = (f.get('email') or u.email or '').strip()
    u.mobile = (f.get('mobile') or u.mobile or '').strip()
    new_password = f.get('password') or ''
    if new_password:
        u.set_password(new_password)

    now_active = (f.get('is_active') == 'on')
    if u.is_active != now_active:
        if u.id == current_user.id:
            flash(_t('You cannot change your own status.', 'لا يمكنك تغيير حالتك الخاصة.'), 'danger')
            return redirect(url_for('user_mgmt.list_users'))
        u.is_active = now_active

    db.session.commit()
    log_audit('edit', module_key='administration', form_key='users', record_id=u.id)
    flash(_t('User updated.', 'تم تحديث المستخدم.'), 'success')
    return redirect(url_for('user_mgmt.list_users'))


@user_mgmt_bp.route('/<int:id>/deactivate', methods=['POST'])
@login_required
@permission_required('administration', 'users', 'change_status')
def deactivate_user(id):
    u = visible_users_query(current_user).filter_by(id=id).first_or_404()
    if u.is_protected:
        flash(_t('This account is protected and cannot be deactivated.',
                 'هذا الحساب محمي ولا يمكن إلغاء تفعيله.'), 'danger')
        return redirect(url_for('user_mgmt.list_users'))
    if u.id == current_user.id:
        flash(_t('You cannot deactivate your own account.',
                 'لا يمكنك إلغاء تفعيل حسابك الخاص.'), 'danger')
        return redirect(url_for('user_mgmt.list_users'))
    u.is_active = False
    db.session.commit()
    log_audit('user_deactivate', module_key='administration', form_key='users', record_id=u.id)
    flash(_t('User deactivated.', 'تم إلغاء تفعيل المستخدم.'), 'success')
    return redirect(url_for('user_mgmt.list_users'))


@user_mgmt_bp.route('/<int:id>/delete', methods=['POST'])
@login_required
@permission_required_json('administration', 'users', 'delete')
def delete_user(id):
    u = visible_users_query(current_user).filter_by(id=id).first_or_404()
    if u.is_protected:
        return jsonify({'ok': False, 'error': _t(
            'This account is protected and cannot be deleted.',
            'هذا الحساب محمي ولا يمكن حذفه.')}), 400
    if u.id == current_user.id:
        return jsonify({'ok': False, 'error': _t(
            'You cannot delete your own account.', 'لا يمكنك حذف حسابك الخاص.')}), 400
    if user_has_child_records(u.id):
        return jsonify({'ok': False, 'error': _t(
            'Child record found: this user has related records elsewhere and cannot be deleted. Deactivate the account instead.',
            'تم العثور على سجل فرعي: لهذا المستخدم سجلات مرتبطة في أماكن أخرى ولا يمكن حذفه. قم بإلغاء تفعيل الحساب بدلاً من ذلك.')}), 400

    username = u.username
    try:
        db.session.delete(u)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Child record found: this user has related records elsewhere and cannot be deleted. Deactivate the account instead.',
            'تم العثور على سجل فرعي: لهذا المستخدم سجلات مرتبطة في أماكن أخرى ولا يمكن حذفه. قم بإلغاء تفعيل الحساب بدلاً من ذلك.')}), 400

    log_audit('user_delete', module_key='administration', form_key='users',
              record_id=id, old_value=username)
    return jsonify({'ok': True})


@user_mgmt_bp.route('/<int:id>/reset-password', methods=['POST'])
@login_required
def reset_user_password(id):
    """Hard-gated to Super Admin / Admin only, independent of the Permission
    Matrix's 'edit' grant on the Users form -- resetting another user's
    password is deliberately kept out of the general permission system."""
    if not (current_user.is_super_admin or current_user.is_admin()):
        return jsonify({'ok': False, 'error': _t('Not authorized.', 'غير مصرح.')}), 403

    u = visible_users_query(current_user).filter_by(id=id).first_or_404()
    if u.is_protected:
        return jsonify({'ok': False, 'error': _t('This account is protected and cannot be edited.',
                                                   'هذا الحساب محمي ولا يمكن تعديله.')}), 400

    new_password = (request.form.get('password') or '').strip()
    if len(new_password) < 6:
        return jsonify({'ok': False, 'error': _t('Password must be at least 6 characters.',
                                                   'كلمة المرور يجب أن تكون 6 أحرف على الأقل.')}), 400

    u.set_password(new_password)
    db.session.commit()
    log_audit('password_reset', module_key='administration', form_key='users', record_id=u.id)
    return jsonify({'ok': True})


@user_mgmt_bp.route('/<int:id>/permissions')
@login_required
@permission_required('administration', 'users', 'edit')
def user_permissions_page(id):
    u = visible_users_query(current_user).filter_by(id=id).first_or_404()
    return render_template('admin/user_permissions.html', target_user=u)


@user_mgmt_bp.route('/<int:id>/permissions/data')
@login_required
@permission_required_json('administration', 'users', 'edit')
def user_permissions_data(id):
    from models import Module, SystemForm, Permission, UserPermission, RolePermission
    u = visible_users_query(current_user).filter_by(id=id).first_or_404()

    role_grants = {}
    if u.role_id:
        for rp in RolePermission.query.filter_by(role_id=u.role_id).all():
            role_grants[rp.permission_id] = rp.allowed
    overrides = {up.permission_id: up for up in UserPermission.query.filter_by(user_id=u.id).all()}

    out = []
    for mod in Module.query.order_by(Module.sort_order).all():
        forms_out = []
        for frm in SystemForm.query.filter_by(module_id=mod.id).order_by(SystemForm.sort_order).all():
            actions_out = []
            for perm in Permission.query.filter_by(form_id=frm.id).all():
                override = overrides.get(perm.id)
                actions_out.append({
                    'permission_id': perm.id, 'action_code': perm.action_code,
                    'role_allowed': role_grants.get(perm.id, False),
                    'override_allowed': (override.allowed if override else None),
                })
            forms_out.append({'form_code': frm.code, 'label_en': frm.label_en,
                              'label_ar': frm.label_ar or '', 'actions': actions_out})
        out.append({'module_code': mod.code, 'label_en': mod.label_en,
                    'label_ar': mod.label_ar or '', 'forms': forms_out})
    return jsonify(out)


@user_mgmt_bp.route('/<int:id>/permissions/save', methods=['POST'])
@login_required
@permission_required_json('administration', 'users', 'edit')
def user_permissions_save(id):
    from models import UserPermission
    u = visible_users_query(current_user).filter_by(id=id).first_or_404()
    if u.id == current_user.id:
        return jsonify({'ok': False, 'error': _t('You cannot edit your own permissions.',
                                                  'لا يمكنك تعديل صلاحياتك الخاصة.')}), 403

    data = request.get_json(silent=True) or {}
    changes = data.get('changes') or []  # [{permission_id, allowed: true|false|null}]
    for c in changes:
        perm_id = c.get('permission_id')
        allowed = c.get('allowed')
        existing = UserPermission.query.filter_by(user_id=u.id, permission_id=perm_id).first()
        if allowed is None:
            if existing:
                db.session.delete(existing)
            continue
        if existing:
            existing.allowed = bool(allowed)
            existing.override_type = 'grant' if allowed else 'deny'
        else:
            db.session.add(UserPermission(
                user_id=u.id, permission_id=perm_id, allowed=bool(allowed),
                override_type=('grant' if allowed else 'deny'), created_by=current_user.id))
    db.session.commit()
    log_audit('permission_change', module_key='administration', form_key='users',
              record_id=u.id, new_value=f'{len(changes)} override(s) saved')
    return jsonify({'ok': True})
