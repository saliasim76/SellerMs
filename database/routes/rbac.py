"""Role & Permission core engine.

Central home for:
  - the fixed action catalog + the module/form catalog seed data
  - seed_rbac_catalog(): idempotent seeder, called once from app.py's init_db()
  - user_has_permission(): the single source of truth for "can this user do X"
  - permission_required / permission_required_json: route decorators
  - can(): the callable injected into every template via a context processor
  - visible_users_query(): the backend-level Super Admin invisibility filter

Nothing here caches a permission result across requests -- every check re-reads
role_permissions/user_permissions fresh, so permission changes take effect on
the very next request with no restart, matching the security requirement.
"""
import os
from functools import wraps
from flask import redirect, url_for, flash, jsonify, request, session
from flask_login import current_user

from models import db, Module, SystemForm, Permission, Role, RolePermission, UserPermission, User


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


# ── Fixed action catalog (spec section 6) ──────────────────────────
ACTION_CATALOG = [
    ('view',              'View',              'عرض'),
    ('add',                'Add',               'إضافة'),
    ('edit',               'Edit',              'تعديل'),
    ('delete',             'Delete',            'حذف'),
    ('restore',            'Restore',           'استعادة'),
    ('permanent_delete',   'Permanent Delete',  'حذف نهائي'),
    ('change_status',      'Change Status',     'تغيير الحالة'),
    ('approve',            'Approve',           'اعتماد'),
    ('post',               'Post',              'ترحيل'),
    ('print',              'Print',             'طباعة'),
    ('export',             'Export',            'تصدير'),
    ('report',             'Report',            'تقرير'),
    ('visible',            'Visible',           'ظاهر'),
]
ACTION_CODES = [a[0] for a in ACTION_CATALOG]

# ── Module -> Form catalog (spec section 6/8; used for seeding + the
#    Permission Matrix UI). Only 'employee_master' and 'purchase_order' are
#    actually enforced by a route decorator this phase -- the rest exist so
#    the matrix screen is immediately useful and the pattern is ready to
#    extend module-by-module without another schema change. ──
MODULE_FORM_CATALOG = [
    ('dashboard', 'Dashboard', 'لوحة التحكم', [
        ('dashboard', 'Dashboard', 'لوحة التحكم'),
    ]),
    ('owner', 'Owner', 'المالك', [
        ('owner_master', 'Owner', 'المالك'),
        ('owner_documents', 'Owner Documents', 'مستندات المالك'),
    ]),
    ('store', 'Store', 'المخزن', [
        ('stores', 'Stores', 'المخازن'),
        ('store_item_tracking', 'Store Item Tracking', 'تتبع أصناف المخزن'),
    ]),
    ('purchase', 'Purchase', 'المشتريات', [
        ('supplier', 'Supplier', 'المورد'),
        ('supplier_account', 'Supplier Account', 'حساب المورد'),
        ('purchase_request', 'Purchase Request', 'طلب شراء'),
        ('purchase_quotation', 'Purchase Quotation', 'عرض سعر شراء'),
        ('purchase_order', 'Purchase Order', 'أمر شراء'),
        ('goods_receipt_note', 'Goods Receipt Note', 'إذن استلام'),
        ('purchase_invoice', 'Purchase Invoice', 'فاتورة شراء'),
        ('goods_return_request', 'Goods Return Request', 'طلب إرجاع بضاعة'),
        ('purchase_return_note', 'Purchase Return Note', 'مذكرة إرجاع الشراء'),
        ('purchase_debit_memo', 'Purchase Debit Memo', 'إشعار مدين شراء'),
        ('po_quantity_tracking', 'PO Quantity Tracking', 'تتبع كميات أمر الشراء'),
    ]),
    ('sale', 'Sale', 'المبيعات', [
        ('buyer', 'Buyer', 'المشتري'),
        # Mirror Purchase's own purchase_request/purchase_quotation forms --
        # added alongside wiring real @permission_required enforcement onto
        # sales.py's sr_*/sq_* routes, which previously had no catalog home
        # at all (unlike every other Sale document type).
        ('sales_request', 'Sales Request', 'طلب بيع'),
        ('sales_quotation', 'Sales Quotation', 'عرض سعر بيع'),
        ('sales_order', 'Sales Order', 'أمر بيع'),
        ('delivery_note', 'Delivery Note', 'إذن تسليم'),
        ('sales_invoice', 'Sales Invoice', 'فاتورة بيع'),
        ('sales_return_request', 'Sales Return Request', 'طلب إرجاع بيع'),
        ('sales_return_note', 'Sale Return Note', 'مذكرة إرجاع البيع'),
        ('sales_credit_memo', 'Sales Credit Memo', 'إشعار دائن بيع'),
        ('so_quantity_tracking', 'SO Quantity Tracking', 'تتبع كميات أمر البيع'),
    ]),
    ('employee', 'Employee', 'الموظفون', [
        ('employee_master', 'Employee Master', 'بيانات الموظف'),
        ('employee_profession', 'Employee Profession', 'مهنة الموظف'),
        ('profession_master', 'Profession Master', 'المهن الرئيسية'),
        ('employee_documents', 'Employee Documents', 'مستندات الموظف'),
        ('allowance_type_master', 'Allowance Type', 'نوع البدل'),
        ('employee_allowances', 'Employee Allowances', 'بدلات الموظفين'),
        ('work_allocation', 'Work Allocation', 'توزيع العمل'),
        ('payroll', 'Payroll', 'الرواتب'),
        # Auto Code Selection config points for Payroll's GRL/Journal Entry
        # attachment (Debit Salary Expense / Credit Salaries Payable, both
        # lump totals for the whole batch) -- not separately visible list
        # pages, same as the outgoing_payment_* entries under cash_bank.
        ('payroll_salary_expense', 'Payroll - Salary Expense', 'الرواتب - مصروف الرواتب'),
        ('payroll_salary_payable', 'Payroll - Salaries Payable', 'الرواتب - رواتب مستحقة الدفع'),
    ]),
    ('cash_bank', 'Cash & Bank', 'النقدية والبنوك', [
        ('outgoing_payment', 'Outgoing Payment', 'دفعة صادرة'),
        # Auto Code Selection config points, one Outstanding-settlement +
        # one Advance control account per Pay To type (the generic Debit
        # side of an Outgoing Payment -- Supplier's AP control account vs.
        # its separate Advance-to-Supplier account, and likewise for
        # Buyer/Employee), plus the Input VAT account for GL Account
        # Detail Table 2 lines. Not separately visible list pages -- these
        # exist purely so an admin can configure their GL code via the
        # existing Auto Code Selection screen. GL Account itself needs no
        # shared fallback here -- each Detail Table 2 line names its own
        # Level Five code directly.
        ('outgoing_payment_supplier', 'Outgoing Payment - Supplier (AP)', 'دفعة صادرة - المورد (الذمم الدائنة)'),
        ('outgoing_payment_supplier_advance', 'Outgoing Payment - Supplier Advance', 'دفعة صادرة - سلفة المورد'),
        ('outgoing_payment_buyer', 'Outgoing Payment - Buyer (AR)', 'دفعة صادرة - المشتري (الذمم المدينة)'),
        ('outgoing_payment_buyer_advance', 'Outgoing Payment - Buyer Advance', 'دفعة صادرة - سلفة المشتري'),
        ('outgoing_payment_employee', 'Outgoing Payment - Employee Payable', 'دفعة صادرة - مستحقات الموظف'),
        ('outgoing_payment_employee_advance', 'Outgoing Payment - Employee Advance', 'دفعة صادرة - سلفة الموظف'),
        ('outgoing_payment_input_vat', 'Outgoing Payment - Input VAT', 'دفعة صادرة - ضريبة القيمة المضافة المدخلات'),
        ('incoming_payment', 'Incoming Payment', 'دفعة واردة'),
        # Same Outstanding-settlement + Advance pair per Receive From type,
        # mirrored for the AR side (the generic Credit side of an Incoming
        # Payment -- Buyer's AR control account vs. its separate Advance-
        # from-Buyer account, and likewise for Supplier/Employee), plus the
        # Output VAT account for GL Account Detail Table 2 lines -- the
        # exact mirror of the outgoing_payment_* set above.
        ('incoming_payment_buyer', 'Incoming Payment - Buyer (AR)', 'دفعة واردة - المشتري (الذمم المدينة)'),
        ('incoming_payment_buyer_advance', 'Incoming Payment - Buyer Advance', 'دفعة واردة - سلفة من المشتري'),
        ('incoming_payment_supplier', 'Incoming Payment - Supplier (Refund)', 'دفعة واردة - المورد (استرداد)'),
        ('incoming_payment_supplier_advance', 'Incoming Payment - Supplier Advance', 'دفعة واردة - سلفة من المورد'),
        ('incoming_payment_employee', 'Incoming Payment - Employee (Recovery)', 'دفعة واردة - الموظف (استرداد)'),
        ('incoming_payment_employee_advance', 'Incoming Payment - Employee Advance', 'دفعة واردة - سلفة من الموظف'),
        ('incoming_payment_output_vat', 'Incoming Payment - Output VAT', 'دفعة واردة - ضريبة القيمة المضافة المخرجات'),
    ]),
    ('coa', 'Chart of Accounts', 'دليل الحسابات', [
        ('level_one', 'Level One', 'المستوى الأول'),
        ('level_two', 'Level Two', 'المستوى الثاني'),
        ('level_three', 'Level Three', 'المستوى الثالث'),
        ('level_four', 'Level Four', 'المستوى الرابع'),
        ('level_five', 'Level Five', 'المستوى الخامس'),
    ]),
    ('financial', 'Financial', 'السنة المالية', [
        ('financial_year', 'Financial Year', 'السنة المالية'),
    ]),
    ('journal', 'Journal', 'اليومية', [
        ('journal_entry', 'Journal Entry', 'قيد اليومية'),
    ]),
    ('administration', 'Administration', 'الإدارة', [
        ('users', 'Users', 'المستخدمون'),
        ('roles_permissions', 'Roles & Permissions', 'الأدوار والصلاحيات'),
        ('recycle_bin', 'Recycle Bin', 'سلة المحذوفات'),
        ('audit_log', 'Audit Log', 'سجل التدقيق'),
    ]),
]

SUPER_ADMIN_USERNAME = 'SuperAdmin'
# Only ever used once, to set the password when this account is first
# created (see seed_rbac_catalog() below) -- never re-applied on a later
# restart, so changing this env var has no effect on an already-seeded
# database. Override it in production via a real SUPER_ADMIN_PASSWORD
# environment variable rather than relying on this literal, especially
# since it must never match the database's own DB_PASSWORD.
SUPER_ADMIN_PASSWORD = os.environ.get('SUPER_ADMIN_PASSWORD', 'Naqvi@76').strip()


def seed_rbac_catalog():
    """Idempotent: safe to call on every startup. Get-or-creates the module/
    form/permission catalog, the 4 system roles + their default grants,
    backfills role_id on any pre-existing user, and seeds the dedicated
    SuperAdmin account. Mirrors the shape of seed_chart_of_accounts()/
    seed_tax_codes() already called from app.py's init_db()."""
    modules_by_code = {m.code: m for m in Module.query.all()}
    for order, (mcode, men, mar, forms) in enumerate(MODULE_FORM_CATALOG):
        mod = modules_by_code.get(mcode)
        if not mod:
            mod = Module(code=mcode, label_en=men, label_ar=mar, sort_order=order)
            db.session.add(mod)
            db.session.flush()
            modules_by_code[mcode] = mod

        forms_by_code = {f.code: f for f in SystemForm.query.filter_by(module_id=mod.id).all()}
        for forder, (fcode, fen, far) in enumerate(forms):
            frm = forms_by_code.get(fcode)
            if not frm:
                frm = SystemForm(module_id=mod.id, code=fcode, label_en=fen, label_ar=far, sort_order=forder)
                db.session.add(frm)
                db.session.flush()
                forms_by_code[fcode] = frm

            existing_actions = {p.action_code for p in Permission.query.filter_by(form_id=frm.id).all()}
            for acode, aen, _aar in ACTION_CATALOG:
                if acode not in existing_actions:
                    db.session.add(Permission(form_id=frm.id, action_code=acode,
                                              description=f'{fen} - {aen}'))
    db.session.commit()

    # Roles
    role_defs = [
        ('super_admin', 'Super Admin', 'Full, unrestricted system access.'),
        ('admin', 'Admin', 'Module/form access configured by Super Admin.'),
        ('power_user', 'Power User', 'View/add/edit/print/export/report by default.'),
        ('user', 'User', 'View/print/export by default.'),
    ]
    roles = {r.code: r for r in Role.query.all()}
    for code, name, desc in role_defs:
        if code not in roles:
            r = Role(code=code, name=name, description=desc, is_system=True)
            db.session.add(r)
            db.session.flush()
            roles[code] = r
    db.session.commit()

    # Default role_permissions (only fills gaps -- never overwrites a grant
    # an admin has since changed via the Permission Matrix screen).
    existing_rp = {(rp.role_id, rp.permission_id) for rp in RolePermission.query.all()}

    def grant(role, perm, allowed):
        key = (role.id, perm.id)
        if key in existing_rp:
            return
        db.session.add(RolePermission(role_id=role.id, permission_id=perm.id, allowed=allowed))
        existing_rp.add(key)

    for perm in Permission.query.all():
        grant(roles['super_admin'], perm, True)
        # The 'Admin' role gets every permission, including roles_permissions
        # (managing its own Roles & Permission Matrix) -- each SaaS tenant's
        # own admin is the sole administrator of their own database (the
        # real is_super_admin account is neutralized there), so they must
        # be able to manage their own app users and roles without needing
        # real Super Admin access.
        grant(roles['admin'], perm, True)
        grant(roles['power_user'], perm,
              perm.action_code in ('view', 'add', 'edit', 'print', 'export', 'report', 'visible'))
        grant(roles['user'], perm,
              perm.action_code in ('view', 'print', 'export', 'visible'))
    db.session.commit()

    # Backfill role_id on any user that predates this system.
    changed = False
    for u in User.query.filter(User.role_id.is_(None)).all():
        u.role_id = roles['admin'].id if u.role == 'admin' else roles['user'].id
        changed = True
    if changed:
        db.session.commit()

    # Dedicated Super Admin account (not a promotion of an existing user).
    if not User.query.filter_by(username=SUPER_ADMIN_USERNAME).first():
        sa = User(
            username=SUPER_ADMIN_USERNAME,
            email='superadmin@sellerms.local',
            full_name='Super Admin',
            role='admin',            # keeps the legacy is_admin() check true too
            role_id=roles['super_admin'].id,
            is_super_admin=True,
            is_protected=True,
            is_active=True,
        )
        sa.set_password(SUPER_ADMIN_PASSWORD)
        db.session.add(sa)
        db.session.commit()
        print(f'RBAC: seeded {SUPER_ADMIN_USERNAME} account.')


def _get_permission(module_code, form_code, action_code):
    return (Permission.query
            .join(SystemForm, Permission.form_id == SystemForm.id)
            .join(Module, SystemForm.module_id == Module.id)
            .filter(Module.code == module_code,
                    SystemForm.code == form_code,
                    Permission.action_code == action_code)
            .first())


def user_has_permission(user, module_code, form_code, action_code):
    """The single source of truth for "can this user do X". Checked fresh
    on every call -- no caching, so permission edits apply immediately."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_super_admin', False):
        return True

    perm = _get_permission(module_code, form_code, action_code)
    if not perm:
        return False  # unregistered permission -> fail closed

    override = UserPermission.query.filter_by(user_id=user.id, permission_id=perm.id).first()
    if override is not None:
        return bool(override.allowed)

    if not user.role_id:
        return False
    rp = RolePermission.query.filter_by(role_id=user.role_id, permission_id=perm.id).first()
    return bool(rp.allowed) if rp is not None else False


def can(module_code, form_code, action_code='visible'):
    """Injected into every template via app.py's context_processor."""
    try:
        return user_has_permission(current_user, module_code, form_code, action_code)
    except Exception:
        return False


def permission_required(module_code, form_code, action_code):
    """HTML-route decorator: redirect + flash on denial (mirrors the
    existing local admin_required in employees.py/sellers.py/etc.)."""
    def outer(f):
        @wraps(f)
        def inner(*args, **kwargs):
            if not user_has_permission(current_user, module_code, form_code, action_code):
                flash(_t('Access denied.', 'الوصول مرفوض'), 'danger')
                return redirect(request.referrer or url_for('dashboard.index'))
            return f(*args, **kwargs)
        return inner
    return outer


def permission_required_json(module_code, form_code, action_code):
    """AJAX/API-route decorator: 403 JSON on denial (mirrors the existing
    local admin_required in employee_import.py)."""
    def outer(f):
        @wraps(f)
        def inner(*args, **kwargs):
            if not user_has_permission(current_user, module_code, form_code, action_code):
                return jsonify({'ok': False, 'error': _t('Access denied.', 'الوصول مرفوض')}), 403
            return f(*args, **kwargs)
        return inner
    return outer


def visible_users_query(viewer):
    """Every 'list users' query in the app must go through this -- the
    backend-level Super Admin invisibility filter. Not just a template
    conditional: this is the query itself, so no route/report/dropdown can
    accidentally leak a Super Admin row to a non-Super-Admin viewer."""
    q = User.query
    if not (viewer and getattr(viewer, 'is_super_admin', False)):
        q = q.filter(db.or_(User.is_super_admin == False, User.is_super_admin.is_(None)))  # noqa: E712 -- mssql rejects "IS 0"
    return q
