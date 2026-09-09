"""Role list + the Role Permission Matrix screen (module -> form -> action
grid, per role). Per-user overrides live in user_management.py instead
(a different screen, since the spec explicitly distinguishes "permission
comes from Role" vs "Individual User Override").

Reaching /admin/permissions/save at all already requires the
administration/roles_permissions/edit permission, which the seeded default
grants ONLY to Super Admin (see rbac.seed_rbac_catalog) -- this is what
actually prevents an Admin from raising their own role's power, not an
extra check in this file.
"""
from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required, current_user

from models import db, Role, Module, SystemForm, Permission, RolePermission
from database.routes.rbac import permission_required, permission_required_json
from database.routes.audit import log_audit

rbac_ui_bp = Blueprint('rbac_ui', __name__, url_prefix='/admin')


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


@rbac_ui_bp.route('/roles')
@login_required
@permission_required('administration', 'roles_permissions', 'view')
def roles_list():
    roles = Role.query.order_by(Role.id).all()
    return render_template('admin/roles_list.html', roles=[r.to_dict() for r in roles])


@rbac_ui_bp.route('/permissions')
@login_required
@permission_required('administration', 'roles_permissions', 'view')
def permission_matrix():
    roles = Role.query.order_by(Role.id).all()
    return render_template('admin/permission_matrix.html', roles=roles)


@rbac_ui_bp.route('/permissions/data')
@login_required
@permission_required_json('administration', 'roles_permissions', 'view')
def permission_matrix_data():
    role_id = request.args.get('role_id', type=int)
    if not role_id:
        return jsonify({'ok': False, 'error': _t('Select a role.', 'اختر دوراً.')}), 400
    role = Role.query.get_or_404(role_id)

    grants = {rp.permission_id: rp.allowed for rp in RolePermission.query.filter_by(role_id=role_id).all()}

    out = []
    for mod in Module.query.order_by(Module.sort_order).all():
        forms_out = []
        for frm in SystemForm.query.filter_by(module_id=mod.id).order_by(SystemForm.sort_order).all():
            actions_out = [{
                'permission_id': p.id, 'action_code': p.action_code,
                'allowed': grants.get(p.id, False),
            } for p in Permission.query.filter_by(form_id=frm.id).all()]
            forms_out.append({'form_code': frm.code, 'label_en': frm.label_en,
                              'label_ar': frm.label_ar or '', 'actions': actions_out})
        out.append({'module_code': mod.code, 'label_en': mod.label_en,
                    'label_ar': mod.label_ar or '', 'forms': forms_out})
    return jsonify({'ok': True, 'role': role.to_dict(), 'modules': out})


@rbac_ui_bp.route('/permissions/save', methods=['POST'])
@login_required
@permission_required_json('administration', 'roles_permissions', 'edit')
def permission_matrix_save():
    data = request.get_json(silent=True) or {}
    role_id = data.get('role_id')
    role = Role.query.get(role_id) if role_id else None
    if not role:
        return jsonify({'ok': False, 'error': _t('Role not found.', 'الدور غير موجود.')}), 404
    if role.code == 'super_admin':
        return jsonify({'ok': False, 'error': _t(
            'Super Admin permissions cannot be edited -- that role always has full access.',
            'لا يمكن تعديل صلاحيات المدير الأعلى — هذا الدور يملك دائماً صلاحية كاملة.')}), 400

    changes = data.get('changes') or []  # [{permission_id, allowed}]
    changed_count = 0
    for c in changes:
        perm_id = c.get('permission_id')
        allowed = bool(c.get('allowed'))
        rp = RolePermission.query.filter_by(role_id=role.id, permission_id=perm_id).first()
        if rp:
            if rp.allowed == allowed:
                continue
            old = rp.allowed
            rp.allowed = allowed
        else:
            old = None
            db.session.add(RolePermission(role_id=role.id, permission_id=perm_id, allowed=allowed))
        changed_count += 1
        log_audit('permission_change', module_key='administration', form_key='roles_permissions',
                  record_id=perm_id, old_value=old, new_value=allowed,
                  detail=f'role={role.code}')
    db.session.commit()
    return jsonify({'ok': True, 'changed': changed_count})
