"""SaaS platform: Super Admin screen to onboard a SaaS customer directly
-- either PAID (assign modules with an expiry, provision a dedicated
MySQL database) or TRIAL (shared database, trial-eligible modules only,
mirroring the public /trial-signup flow). See
database/tenant_provisioning.py for the actual database-creation/seeding
mechanics reused here.

Gated by super_admin_required, matching saas_admin.py's module catalog
screen -- this configures platform customers, not tenant business data.
"""
import secrets
import string
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from sqlalchemy.exc import IntegrityError

from config import DB_NAME
from models import db, Customer, Subscription, SubscriptionModule, SaasModule, SaasUserDirectory, User
from database.routes.shared import super_admin_required, _t
from database.tenant_provisioning import provision_tenant, sync_tenant_schema, reset_tenant_password
from database.routes.saas_internal import provision_trial_user_direct

saas_customers_bp = Blueprint('saas_customers', __name__)


def _generate_temp_password():
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(12))


def _parse_date(value):
    if not value:
        return None
    return datetime.strptime(value, '%Y-%m-%d')


def _set_subscription_modules(subscription, form, is_trial=False):
    """Replace this subscription's full module set from the submitted
    form, mirroring saas_admin.py's _set_dependencies delete+recreate
    pattern. Each module checkbox `modules` may have a matching per-module
    expiry override `module_expiry_<id>`; falls back to the subscription's
    own end_date when left blank. For a trial subscription, modules the
    Super Admin hasn't flagged trial_available are silently skipped and
    price is always 0 -- mirroring the public /trial-signup flow exactly."""
    SubscriptionModule.query.filter_by(subscription_id=subscription.id).delete()
    for module_id in form.getlist('modules'):
        try:
            module_id = int(module_id)
        except (TypeError, ValueError):
            continue
        module = SaasModule.query.get(module_id)
        if not module:
            continue
        if is_trial and not module.trial_available:
            continue
        override = _parse_date(form.get(f'module_expiry_{module_id}'))
        price = 0 if is_trial else (module.monthly_price if subscription.billing_cycle == 'monthly' else module.yearly_price)
        db.session.add(SubscriptionModule(
            subscription_id=subscription.id,
            module_id=module.id,
            price=price or 0,
            start_date=datetime.utcnow(),
            end_date=override or subscription.end_date,
            status='active',
        ))


@saas_customers_bp.route('/saas-admin/customers')
@login_required
@super_admin_required
def list_saas_customers():
    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    modules = SaasModule.query.filter_by(status='active').order_by(SaasModule.display_order, SaasModule.module_name_en).all()
    return render_template('saas_admin/customers.html', customers=customers, modules=modules)


@saas_customers_bp.route('/saas-admin/customers/add', methods=['POST'])
@login_required
@super_admin_required
def add_saas_customer():
    f = request.form
    company_name = (f.get('company_name') or '').strip()
    customer_name = (f.get('customer_name') or '').strip()
    email = (f.get('email') or '').strip().lower()
    mobile = (f.get('mobile') or '').strip()
    is_trial = f.get('is_trial') == 'on'
    billing_cycle = 'trial' if is_trial else (f.get('billing_cycle') or 'monthly')
    end_date = (datetime.utcnow() + timedelta(days=14)) if is_trial else _parse_date(f.get('end_date'))
    access_mode = f.get('access_mode') if f.get('access_mode') in ('basic', 'expert') else 'expert'

    if not company_name or not customer_name or not email:
        flash(_t('Company name, contact name and email are required.',
                  'اسم الشركة واسم جهة الاتصال والبريد الإلكتروني مطلوبة.'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))

    if Customer.query.filter_by(email=email).first():
        flash(_t(f'A customer with email "{email}" already exists.',
                  f'يوجد عميل بالفعل بالبريد الإلكتروني "{email}".'), 'warning')
        return redirect(url_for('saas_customers.list_saas_customers'))

    customer = Customer(
        customer_name=customer_name, company_name=company_name,
        email=email, mobile=mobile, account_status='trial' if is_trial else 'active',
        access_mode=access_mode,
    )
    db.session.add(customer)
    try:
        db.session.flush()  # need customer.id for the tenant db name + FKs below
    except IntegrityError:
        # A near-simultaneous duplicate submission (e.g. the Save button
        # clicked more than once while provisioning was still running) can
        # slip past the check above -- fail gracefully instead of a 500.
        db.session.rollback()
        flash(_t(f'A customer with email "{email}" already exists.',
                  f'يوجد عميل بالفعل بالبريد الإلكتروني "{email}".'), 'warning')
        return redirect(url_for('saas_customers.list_saas_customers'))

    subscription = Subscription(
        customer_id=customer.id, billing_cycle=billing_cycle,
        start_date=datetime.utcnow(), end_date=end_date,
        status='trial' if is_trial else 'active', total_amount=0,
    )
    db.session.add(subscription)
    db.session.flush()

    _set_subscription_modules(subscription, f, is_trial=is_trial)
    subscription.total_amount = sum(
        (sm.price or 0) for sm in SubscriptionModule.query.filter_by(subscription_id=subscription.id).all()
    )

    # Commit the customer/subscription/modules BEFORE provisioning (rather
    # than just flushing) so nothing pending is left in the shared session
    # when provision_tenant() opens its own nested tenant app context --
    # that context does its own commit() against the tenant database, and
    # Flask-SQLAlchemy's session is scoped per-thread, not per-app, so any
    # still-pending outer objects could otherwise be swept into that
    # unrelated commit. If provisioning then fails, the customer row is
    # explicitly deleted below rather than relying on a rollback that can
    # no longer undo an already-committed transaction.
    db.session.commit()

    if is_trial:
        # Trial customers share SellerMs's single database -- no dedicated
        # database is provisioned, mirroring the public /trial-signup flow
        # exactly (database/routes/proledg_site.py).
        temp_password = _generate_temp_password()
        try:
            result = provision_trial_user_direct(email, customer_name, mobile, temp_password)
        except ValueError as exc:
            db.session.delete(customer)  # cascades to subscription + modules
            db.session.commit()
            flash(str(exc), 'danger')
            return redirect(url_for('saas_customers.list_saas_customers'))
        flash(_t(
            f'Trial customer "{company_name}" created (shared database). '
            f'Username: {result["username"]} | Temporary password: {temp_password} '
            f'(shown once -- copy it now).',
            f'تم إنشاء عميل تجريبي "{company_name}" (قاعدة بيانات مشتركة). '
            f'اسم المستخدم: {result["username"]} | كلمة المرور المؤقتة: {temp_password} '
            f'(تظهر مرة واحدة فقط -- انسخها الآن).',
        ), 'success')
        return redirect(url_for('saas_customers.list_saas_customers'))

    try:
        result = provision_tenant(customer, customer_name, email, mobile)
    except Exception as exc:
        db.session.delete(customer)  # cascades to subscription + modules
        db.session.commit()
        flash(_t(f'Could not provision the customer database: {exc}',
                  f'تعذر إنشاء قاعدة بيانات العميل: {exc}'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))

    customer.database_name = result['database_name']
    db.session.commit()

    flash(_t(
        f'Customer "{company_name}" provisioned. Database: {result["database_name"]} | '
        f'Username: {result["username"]} | Temporary password: {result["password"]} '
        f'(shown once -- copy it now).',
        f'تم إنشاء العميل "{company_name}". قاعدة البيانات: {result["database_name"]} | '
        f'اسم المستخدم: {result["username"]} | كلمة المرور المؤقتة: {result["password"]} '
        f'(تظهر مرة واحدة فقط -- انسخها الآن).',
    ), 'success')
    return redirect(url_for('saas_customers.list_saas_customers'))


@saas_customers_bp.route('/saas-admin/customers/<int:id>/edit', methods=['POST'])
@login_required
@super_admin_required
def edit_saas_customer(id):
    customer = Customer.query.get_or_404(id)
    f = request.form
    company_name = (f.get('company_name') or '').strip()
    customer_name = (f.get('customer_name') or '').strip()
    email = (f.get('email') or '').strip().lower()
    mobile = (f.get('mobile') or '').strip()

    if not company_name or not customer_name or not email:
        flash(_t('Company name, contact name and email are required.',
                  'اسم الشركة واسم جهة الاتصال والبريد الإلكتروني مطلوبة.'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))

    dup = Customer.query.filter(Customer.email == email, Customer.id != id).first()
    if dup:
        flash(_t(f'A customer with email "{email}" already exists.',
                  f'يوجد عميل بالفعل بالبريد الإلكتروني "{email}".'), 'warning')
        return redirect(url_for('saas_customers.list_saas_customers'))

    customer.company_name = company_name
    customer.customer_name = customer_name
    customer.email = email
    customer.mobile = mobile
    if f.get('access_mode') in ('basic', 'expert'):
        customer.access_mode = f.get('access_mode')

    subscription = Subscription.query.filter_by(customer_id=customer.id).order_by(Subscription.id.desc()).first()
    if subscription:
        subscription.billing_cycle = f.get('billing_cycle') or subscription.billing_cycle
        subscription.end_date = _parse_date(f.get('end_date')) or subscription.end_date
        _set_subscription_modules(subscription, f)
        subscription.total_amount = sum(
            (sm.price or 0) for sm in SubscriptionModule.query.filter_by(subscription_id=subscription.id).all()
        )

    db.session.commit()
    flash(_t('Customer updated.', 'تم تحديث العميل.'), 'success')
    return redirect(url_for('saas_customers.list_saas_customers'))


@saas_customers_bp.route('/saas-admin/customers/<int:id>/toggle-status', methods=['POST'])
@login_required
@super_admin_required
def toggle_saas_customer_status(id):
    customer = Customer.query.get_or_404(id)
    customer.account_status = 'suspended' if customer.account_status == 'active' else 'active'
    db.session.commit()
    flash(_t('Customer status updated.', 'تم تحديث حالة العميل.'), 'success')
    return redirect(url_for('saas_customers.list_saas_customers'))


@saas_customers_bp.route('/saas-admin/customers/<int:id>/sync-schema', methods=['POST'])
@login_required
@super_admin_required
def sync_saas_customer_schema(id):
    """Applies any new tables/columns the main sellerms database has
    gained since this tenant was provisioned. Purely additive -- never
    drops, overwrites, or re-seeds anything, so existing tenant data and
    the tenant admin's own customized login are never touched."""
    customer = Customer.query.get_or_404(id)
    if not customer.database_name:
        flash(_t('This customer has no dedicated database to sync (still on the shared trial database).',
                  'لا توجد قاعدة بيانات مخصصة لهذا العميل لمزامنتها (لا يزال على قاعدة البيانات المشتركة للتجربة).'), 'warning')
        return redirect(url_for('saas_customers.list_saas_customers'))
    try:
        sync_tenant_schema(customer.database_name)
    except Exception as exc:
        flash(_t(f'Could not sync schema for "{customer.database_name}": {exc}',
                  f'تعذرت مزامنة المخطط لـ "{customer.database_name}": {exc}'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))
    flash(_t(f'Schema synced for "{customer.database_name}". No data was changed or removed.',
              f'تمت مزامنة المخطط لـ "{customer.database_name}". لم يتم تغيير أو حذف أي بيانات.'), 'success')
    return redirect(url_for('saas_customers.list_saas_customers'))


@saas_customers_bp.route('/saas-admin/customers/sync-schema-all', methods=['POST'])
@login_required
@super_admin_required
def sync_all_saas_customer_schemas():
    """Same as sync_saas_customer_schema() but applied to every customer
    that has a dedicated database -- for after a schema change to the
    main app that every existing tenant needs to catch up on."""
    customers = Customer.query.filter(Customer.database_name.isnot(None)).all()
    synced, failed = [], []
    for customer in customers:
        try:
            sync_tenant_schema(customer.database_name)
            synced.append(customer.database_name)
        except Exception as exc:
            failed.append(f'{customer.database_name} ({exc})')
    if synced:
        flash(_t(f'Schema synced for {len(synced)} database(s): {", ".join(synced)}. No data was changed or removed.',
                  f'تمت مزامنة المخطط لعدد {len(synced)} من قواعد البيانات: {", ".join(synced)}. لم يتم تغيير أو حذف أي بيانات.'), 'success')
    if failed:
        flash(_t(f'Failed to sync {len(failed)} database(s): {"; ".join(failed)}',
                  f'فشلت مزامنة {len(failed)} من قواعد البيانات: {"; ".join(failed)}'), 'danger')
    if not customers:
        flash(_t('No customers with a dedicated database to sync.', 'لا يوجد عملاء لديهم قاعدة بيانات مخصصة للمزامنة.'), 'warning')
    return redirect(url_for('saas_customers.list_saas_customers'))


@saas_customers_bp.route('/saas-admin/customers/<int:id>/reset-password', methods=['POST'])
@login_required
@super_admin_required
def reset_saas_customer_password(id):
    """Super Admin sets or resets a customer's login password on demand.
    Never stores the password anywhere -- only updates the password hash
    and shows the new plaintext value once, in the flash message, exactly
    like at initial creation."""
    customer = Customer.query.get_or_404(id)
    new_password = (request.form.get('new_password') or '').strip()
    if new_password and len(new_password) < 8:
        flash(_t('Password must be at least 8 characters.',
                  'يجب أن تتكون كلمة المرور من 8 أحرف على الأقل.'), 'warning')
        return redirect(url_for('saas_customers.list_saas_customers'))
    if not new_password:
        new_password = _generate_temp_password()

    if customer.database_name:
        target_db = customer.database_name
        entry = SaasUserDirectory.query.filter_by(database_name=target_db).first()
        if not entry:
            flash(_t('No login is recorded for this customer.', 'لا يوجد تسجيل دخول مسجل لهذا العميل.'), 'danger')
            return redirect(url_for('saas_customers.list_saas_customers'))
        username = entry.username
    else:
        target_db = DB_NAME  # trial customer -- shares SellerMs's own database
        user = User.query.filter_by(email=customer.email).first()
        if not user:
            flash(_t('No login is recorded for this customer.', 'لا يوجد تسجيل دخول مسجل لهذا العميل.'), 'danger')
            return redirect(url_for('saas_customers.list_saas_customers'))
        username = user.username

    try:
        reset_tenant_password(target_db, username, new_password)
    except Exception as exc:
        flash(_t(f'Could not reset password: {exc}', f'تعذر إعادة تعيين كلمة المرور: {exc}'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))

    flash(_t(
        f'Password reset for "{username}". New password: {new_password} (shown once -- copy it now).',
        f'تم إعادة تعيين كلمة المرور لـ "{username}". كلمة المرور الجديدة: {new_password} (تظهر مرة واحدة فقط -- انسخها الآن).',
    ), 'success')
    return redirect(url_for('saas_customers.list_saas_customers'))


@saas_customers_bp.route('/saas-admin/customers/<int:id>/delete', methods=['POST'])
@login_required
@super_admin_required
def delete_saas_customer(id):
    """Removes the customer/subscription/module records (and their
    SaasUserDirectory login-routing entry) from saas_master. Deliberately
    does NOT drop the customer's tenant database -- their business data is
    preserved, matching this project's migration-safety principle of never
    deleting data outright. A Super Admin who genuinely wants the database
    gone can drop it separately once they've confirmed it's no longer
    needed."""
    customer = Customer.query.get_or_404(id)
    company_name = customer.company_name or customer.customer_name
    database_name = customer.database_name
    db.session.delete(customer)  # cascades to subscriptions/modules/directory entry
    db.session.commit()
    if database_name:
        flash(_t(
            f'Customer "{company_name}" removed. Their database ({database_name}) was left in place.',
            f'تمت إزالة العميل "{company_name}". تم الإبقاء على قاعدة بياناته ({database_name}).',
        ), 'success')
    else:
        flash(_t(f'Customer "{company_name}" removed.', f'تمت إزالة العميل "{company_name}".'), 'success')
    return redirect(url_for('saas_customers.list_saas_customers'))
