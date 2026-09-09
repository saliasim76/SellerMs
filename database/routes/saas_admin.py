"""SaaS platform Phase 1: Super Admin management of the module/pricing
catalog (saas_master database). See models.py's SaasModule /
SaasModuleDependency for the data model and the design rationale for why
this catalog is kept separate from the existing RBAC Module/SystemForm/
Permission tables (billing entitlement vs. per-user page permission --
two different concerns).

Gated by super_admin_required, not the older admin_required used
elsewhere -- this configures platform pricing, not tenant business data.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from models import db, SaasModule, SaasModuleDependency, SubscriptionModule
from database.routes.shared import super_admin_required, _t

saas_admin_bp = Blueprint('saas_admin', __name__)


def _module_form_fields(f):
    return dict(
        module_code=f.get('module_code', '').strip().lower(),
        module_name_en=f.get('module_name_en', '').strip(),
        module_name_ar=f.get('module_name_ar', '').strip(),
        description_en=f.get('description_en', '').strip(),
        description_ar=f.get('description_ar', '').strip(),
        monthly_price=f.get('monthly_price') or 0,
        yearly_price=f.get('yearly_price') or 0,
        trial_available=f.get('trial_available') == 'on',
        status='active' if f.get('status') == 'on' else 'inactive',
        display_order=f.get('display_order') or 0,
    )


def _set_dependencies(module, requires_ids):
    """Replace this module's full dependency set with `requires_ids`
    (a list of other SaasModule ids it now requires) -- skips a module
    requiring itself, matching the model's implicit rule."""
    SaasModuleDependency.query.filter_by(module_id=module.id).delete()
    for rid in requires_ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if rid == module.id:
            continue
        db.session.add(SaasModuleDependency(module_id=module.id, requires_module_id=rid))


@saas_admin_bp.route('/saas-admin/modules')
@login_required
@super_admin_required
def list_saas_modules():
    modules = SaasModule.query.order_by(SaasModule.display_order, SaasModule.module_name_en).all()
    return render_template('saas_admin/modules.html', modules=modules)


@saas_admin_bp.route('/saas-admin/modules/add', methods=['POST'])
@login_required
@super_admin_required
def add_saas_module():
    f = request.form
    fields = _module_form_fields(f)
    if not fields['module_code'] or not fields['module_name_en']:
        flash(_t('Module code and English name are required.', 'كود الوحدة والاسم الإنجليزي مطلوبان.'), 'danger')
        return redirect(url_for('saas_admin.list_saas_modules'))
    if SaasModule.query.filter_by(module_code=fields['module_code']).first():
        flash(_t(f'Module code "{fields["module_code"]}" already exists.',
                  f'كود الوحدة "{fields["module_code"]}" موجود بالفعل.'), 'warning')
        return redirect(url_for('saas_admin.list_saas_modules'))
    module = SaasModule(**fields)
    db.session.add(module)
    db.session.flush()
    _set_dependencies(module, f.getlist('requires[]'))
    db.session.commit()
    flash(_t(f'Module "{fields["module_name_en"]}" added.',
              f'تمت إضافة الوحدة "{fields["module_name_en"]}".'), 'success')
    return redirect(url_for('saas_admin.list_saas_modules'))


@saas_admin_bp.route('/saas-admin/modules/<int:id>/edit', methods=['POST'])
@login_required
@super_admin_required
def edit_saas_module(id):
    module = SaasModule.query.get_or_404(id)
    f = request.form
    fields = _module_form_fields(f)
    if not fields['module_code'] or not fields['module_name_en']:
        flash(_t('Module code and English name are required.', 'كود الوحدة والاسم الإنجليزي مطلوبان.'), 'danger')
        return redirect(url_for('saas_admin.list_saas_modules'))
    dup = SaasModule.query.filter(SaasModule.module_code == fields['module_code'], SaasModule.id != id).first()
    if dup:
        flash(_t(f'Module code "{fields["module_code"]}" already exists.',
                  f'كود الوحدة "{fields["module_code"]}" موجود بالفعل.'), 'warning')
        return redirect(url_for('saas_admin.list_saas_modules'))
    for k, v in fields.items():
        setattr(module, k, v)
    _set_dependencies(module, f.getlist('requires[]'))
    db.session.commit()
    flash(_t('Module updated.', 'تم تحديث الوحدة.'), 'success')
    return redirect(url_for('saas_admin.list_saas_modules'))


@saas_admin_bp.route('/saas-admin/modules/<int:id>/delete', methods=['POST'])
@login_required
@super_admin_required
def delete_saas_module(id):
    module = SaasModule.query.get_or_404(id)

    in_use = SubscriptionModule.query.filter_by(module_id=id).count()
    if in_use:
        flash(_t(
            f'Cannot delete "{module.module_name_en}": {in_use} customer subscription(s) reference it.',
            f'تعذر حذف "{module.module_name_en}": يوجد {in_use} اشتراك عميل يشير إليها.'), 'danger')
        return redirect(url_for('saas_admin.list_saas_modules'))

    dependents = (SaasModule.query
                  .join(SaasModuleDependency, SaasModuleDependency.module_id == SaasModule.id)
                  .filter(SaasModuleDependency.requires_module_id == id)
                  .all())
    if dependents:
        names = ', '.join(m.module_name_en for m in dependents)
        flash(_t(
            f'Cannot delete "{module.module_name_en}": required by {names}.',
            f'تعذر حذف "{module.module_name_en}": مطلوبة بواسطة {names}.'), 'danger')
        return redirect(url_for('saas_admin.list_saas_modules'))

    db.session.delete(module)
    db.session.commit()
    flash(_t(f'Module "{module.module_name_en}" deleted.',
              f'تم حذف الوحدة "{module.module_name_en}".'), 'success')
    return redirect(url_for('saas_admin.list_saas_modules'))


@saas_admin_bp.route('/saas-admin/modules/<int:id>/toggle-status', methods=['POST'])
@login_required
@super_admin_required
def toggle_saas_module_status(id):
    module = SaasModule.query.get_or_404(id)
    module.status = 'inactive' if module.status == 'active' else 'active'
    db.session.commit()
    flash(_t('Module status updated.', 'تم تحديث حالة الوحدة.'), 'success')
    return redirect(url_for('saas_admin.list_saas_modules'))
