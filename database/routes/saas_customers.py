"""SaaS platform: Super Admin screen to onboard a SaaS customer directly
-- either PAID (assign modules with an expiry, provision a dedicated
MySQL database) or TRIAL (shared database, trial-eligible modules only,
mirroring the public /trial-signup flow). See
database/tenant_provisioning.py for the actual database-creation/seeding
mechanics reused here.

Gated by super_admin_required, matching saas_admin.py's module catalog
screen -- this configures platform customers, not tenant business data.
"""
import re
import secrets
import string
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required
from sqlalchemy.exc import IntegrityError

from config import DB_NAME
from models import db, Customer, Subscription, SubscriptionModule, SaasModule, SaasUserDirectory, User
from database.routes.shared import super_admin_required, _t
from database.routes.rbac import DEFAULT_ADMIN_USERNAME
from database.tenant_provisioning import (provision_tenant, sync_tenant_schema, reset_tenant_password,
                                           validate_custom_db_name, check_db_name, test_database_connection,
                                           encrypt_db_password, decrypt_db_password, forget_tenant_connection)
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


_HOST_RE = re.compile(r'^[A-Za-z0-9._:\-]{1,255}$')
_DBUSER_RE = re.compile(r'^[A-Za-z0-9_.@$\-]{1,100}$')


def _read_db_settings(form, keep_password_enc=None):
    """Optional per-customer connection settings from the form (blank = use the
    application's own defaults). Returns ((host, port, user, password_enc), error).
    The password is stored encrypted; leaving it blank while a user is set keeps
    `keep_password_enc` (the one already saved). Without a user of their own the
    application's own password applies, so nothing is stored."""
    host = (form.get('db_host') or '').strip() or None
    port = (form.get('db_port') or '').strip() or None
    user = (form.get('db_user') or '').strip() or None
    password = form.get('db_password') or ''
    if host and not _HOST_RE.match(host):
        return None, 'The database host contains invalid characters.'
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        return None, 'The database port must be a number between 1 and 65535.'
    if user and not _DBUSER_RE.match(user):
        return None, 'The database username contains invalid characters.'
    if user:
        enc = encrypt_db_password(password) if password else keep_password_enc
    else:
        enc = None
    return (host, port, user, enc), None


def _customer_db_password(customer):
    return decrypt_db_password(customer.db_password_enc) if customer.db_password_enc else ''


def _record_db_test(customer, result):
    """Store the outcome of a connection test on the customer."""
    customer.db_status = result['status']
    customer.db_status_message = (result['message'] or '')[:500]
    customer.db_checked_at = datetime.utcnow()
    if result['ok']:
        customer.db_initialized = bool(result['initialized'])


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
    custom_db_name = (f.get('database_name') or '').strip()
    # Creating the customer and creating its physical database are two separate
    # operations. Automatic does both; Manual only saves the customer and the
    # database assignment -- it never creates, checks or builds the database.
    db_method = 'manual' if (f.get('db_method') == 'manual' and not is_trial) else 'automatic'
    db_settings = (None, None, None, None)

    if not company_name or not customer_name or not email:
        flash(_t('Company name, contact name and email are required.',
                  'اسم الشركة واسم جهة الاتصال والبريد الإلكتروني مطلوبة.'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))

    if db_method == 'manual' and not custom_db_name:
        flash(_t('Enter the database name for the Manual method.',
                  'أدخل اسم قاعدة البيانات للطريقة اليدوية.'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))

    if not is_trial and custom_db_name:
        try:
            if db_method == 'manual':
                custom_db_name = check_db_name(custom_db_name)
            else:
                custom_db_name = validate_custom_db_name(custom_db_name)
        except ValueError as exc:
            flash(_t(f'Invalid database name: {exc}.', f'اسم قاعدة بيانات غير صالح: {exc}.'), 'danger')
            return redirect(url_for('saas_customers.list_saas_customers'))
        # One database can belong to only one customer.
        taken = Customer.query.filter(db.func.lower(Customer.database_name) == custom_db_name.lower()).first()
        if taken:
            flash(_t(f'Invalid database name: "{custom_db_name}" is already assigned to '
                      f'"{taken.company_name or taken.customer_name}".',
                      f'اسم قاعدة بيانات غير صالح: "{custom_db_name}" مخصص بالفعل لـ '
                      f'"{taken.company_name or taken.customer_name}".'), 'danger')
            return redirect(url_for('saas_customers.list_saas_customers'))

    if db_method == 'manual':
        db_settings, error = _read_db_settings(f)
        if error:
            flash(_t(error, error), 'danger')
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

    if db_method == 'manual':
        # Manual: the customer is saved right away with the database name and
        # settings the Super Admin entered. The database does not have to exist,
        # and nothing is created -- it is verified later with "Test connection".
        customer.db_method = 'manual'
        customer.database_name = custom_db_name
        customer.db_status = 'not_verified'
        customer.db_initialized = False
        customer.db_host, customer.db_port, customer.db_user, customer.db_password_enc = db_settings
        db.session.commit()
        forget_tenant_connection(custom_db_name)
        flash(_t(
            f'Customer "{company_name}" saved. Database "{custom_db_name}" is assigned (Manual) -- no '
            f'database was created. Status: Not Verified. Create the database yourself if it does not '
            f'exist, then click "Test connection" on this customer\'s row.',
            f'تم حفظ العميل "{company_name}". تم تعيين قاعدة البيانات "{custom_db_name}" (يدوي) -- لم يتم إنشاء '
            f'أي قاعدة بيانات. الحالة: غير موثّق. أنشئ قاعدة البيانات بنفسك إن لم تكن موجودة، ثم اضغط '
            f'"اختبار الاتصال" في صف هذا العميل.',
        ), 'success')
        return redirect(url_for('saas_customers.list_saas_customers'))

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

    # Automatic: the application creates the database, builds it and saves the
    # name. The customer is only kept if ALL of that succeeded.
    try:
        result = provision_tenant(customer, customer_name, email, mobile,
                                  db_name=custom_db_name or None, create_db=True)
    except Exception as exc:
        db.session.delete(customer)  # cascades to subscription + modules
        db.session.commit()
        flash(_t(f'Could not provision the customer database: {exc}',
                  f'تعذر إنشاء قاعدة بيانات العميل: {exc}'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))

    customer.db_method = 'automatic'
    customer.database_name = result['database_name']
    customer.db_status = 'connected'
    customer.db_status_message = 'Database created and initialized by the application.'
    customer.db_checked_at = datetime.utcnow()
    customer.db_initialized = True
    db.session.commit()

    flash(_t(
        f'Customer "{company_name}" provisioned. Organization (enter this at login): {result["database_name"]} | '
        f'Username: {result["username"]} | Password: {result["password"]}.',
        f'تم إنشاء العميل "{company_name}". المؤسسة (تُستخدم عند تسجيل الدخول): {result["database_name"]} | '
        f'اسم المستخدم: {result["username"]} | كلمة المرور: {result["password"]}.',
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

    # A Manual customer's database name/connection settings can be corrected here.
    # (Automatic customers keep the name the application created -- changing it
    # would orphan that database -- and editing never creates a database.)
    old_db_name = customer.database_name
    db_changed = False
    if (customer.db_method or 'automatic') == 'manual':
        new_name = (f.get('database_name') or '').strip()
        if not new_name:
            flash(_t('The database name cannot be empty for a Manual customer.',
                      'لا يمكن ترك اسم قاعدة البيانات فارغًا لعميل يدوي.'), 'danger')
            return redirect(url_for('saas_customers.list_saas_customers'))
        try:
            new_name = check_db_name(new_name)
        except ValueError as exc:
            flash(_t(f'Invalid database name: {exc}.', f'اسم قاعدة بيانات غير صالح: {exc}.'), 'danger')
            return redirect(url_for('saas_customers.list_saas_customers'))
        taken = Customer.query.filter(db.func.lower(Customer.database_name) == new_name.lower(),
                                      Customer.id != id).first()
        if taken:
            flash(_t(f'Invalid database name: "{new_name}" is already assigned to '
                      f'"{taken.company_name or taken.customer_name}".',
                      f'اسم قاعدة بيانات غير صالح: "{new_name}" مخصص بالفعل لـ '
                      f'"{taken.company_name or taken.customer_name}".'), 'danger')
            return redirect(url_for('saas_customers.list_saas_customers'))
        new_settings, error = _read_db_settings(f, keep_password_enc=customer.db_password_enc)
        if error:
            flash(_t(error, error), 'danger')
            return redirect(url_for('saas_customers.list_saas_customers'))
        old_settings = (customer.db_host, customer.db_port, customer.db_user, customer.db_password_enc)
        name_changed = new_name.lower() != (old_db_name or '').lower()
        if name_changed or new_settings != old_settings:
            db_changed = True
            customer.database_name = new_name
            customer.db_host, customer.db_port, customer.db_user, customer.db_password_enc = new_settings
            customer.db_status = 'not_verified'
            customer.db_status_message = ''
            customer.db_checked_at = None
            if name_changed:
                customer.db_initialized = False  # a different database: must be tested/initialized again

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
    if db_changed:
        forget_tenant_connection(old_db_name)
        forget_tenant_connection(customer.database_name)
        flash(_t('Customer updated. The database name/settings changed, so its status is now Not Verified -- '
                  'click "Test connection".',
                  'تم تحديث العميل. تغيّر اسم/إعدادات قاعدة البيانات، لذا أصبحت حالتها غير موثّقة -- '
                  'اضغط "اختبار الاتصال".'), 'success')
    else:
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


@saas_customers_bp.route('/saas-admin/customers/test-database', methods=['POST'])
@login_required
@super_admin_required
def test_database_from_form():
    """Test button on the Add/Edit form: tries the database name and connection
    settings currently typed in, WITHOUT saving anything. When editing (customer_id)
    a blank password means "keep the saved one"."""
    f = request.form
    password = f.get('db_password') or ''
    customer_id = f.get('customer_id', type=int)
    user = (f.get('db_user') or '').strip()
    if customer_id and user and not password:
        saved = Customer.query.get(customer_id)
        if saved and saved.db_user == user:
            password = _customer_db_password(saved)
    result = test_database_connection(f.get('database_name') or '', f.get('db_host'), f.get('db_port'), user, password)
    return jsonify(result)


@saas_customers_bp.route('/saas-admin/customers/<int:id>/test-database', methods=['POST'])
@login_required
@super_admin_required
def test_saas_customer_database(id):
    """Test the customer's saved database name/settings and store the result
    (Connected / Connection Failed). Never changes or deletes the customer."""
    customer = Customer.query.get_or_404(id)
    label = customer.company_name or customer.customer_name
    if not customer.database_name:
        flash(_t(f'"{label}" has no dedicated database (it uses the shared trial database).',
                  f'لا توجد قاعدة بيانات مخصصة لـ "{label}" (يستخدم قاعدة بيانات التجربة المشتركة).'), 'warning')
        return redirect(url_for('saas_customers.list_saas_customers'))
    result = test_database_connection(customer.database_name, customer.db_host, customer.db_port,
                                      customer.db_user, _customer_db_password(customer))
    _record_db_test(customer, result)
    db.session.commit()
    forget_tenant_connection(customer.database_name)
    if result['ok']:
        flash(_t(f'"{label}" -- database "{customer.database_name}": {result["message"]}',
                  f'"{label}" -- قاعدة البيانات "{customer.database_name}": {result["message"]}'), 'success')
    else:
        flash(_t(f'"{label}" -- database "{customer.database_name}": Connection Failed. {result["message"]} '
                  f'The customer was kept; correct the name/settings with Edit and test again.',
                  f'"{label}" -- قاعدة البيانات "{customer.database_name}": فشل الاتصال. {result["message"]} '
                  f'تم الاحتفاظ بالعميل؛ صحّح الاسم/الإعدادات بالتعديل ثم أعد الاختبار.'), 'danger')
    return redirect(url_for('saas_customers.list_saas_customers'))


@saas_customers_bp.route('/saas-admin/customers/<int:id>/initialize-database', methods=['POST'])
@login_required
@super_admin_required
def initialize_saas_customer_database(id):
    """Builds this application's tables and the customer's Admin login inside
    the customer's (manually created) database, exactly like the Automatic flow
    does in a database it just created. The database must be reachable and
    completely empty; nothing is ever created at the MySQL-server level."""
    customer = Customer.query.get_or_404(id)
    label = customer.company_name or customer.customer_name
    db_name = customer.database_name
    if not db_name:
        flash(_t(f'"{label}" has no dedicated database to initialize.',
                  f'لا توجد قاعدة بيانات مخصصة لـ "{label}" لإعدادها.'), 'warning')
        return redirect(url_for('saas_customers.list_saas_customers'))
    result = test_database_connection(db_name, customer.db_host, customer.db_port,
                                      customer.db_user, _customer_db_password(customer))
    _record_db_test(customer, result)
    db.session.commit()
    forget_tenant_connection(db_name)
    if not result['ok']:
        flash(_t(f'Cannot initialize "{db_name}": Connection Failed. {result["message"]}',
                  f'لا يمكن إعداد "{db_name}": فشل الاتصال. {result["message"]}'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))
    if result['initialized']:
        flash(_t(f'"{db_name}" already contains this application\'s tables -- nothing to initialize.',
                  f'"{db_name}" تحتوي بالفعل على جداول هذا التطبيق -- لا يوجد ما يجب إعداده.'), 'info')
        return redirect(url_for('saas_customers.list_saas_customers'))
    if not result['empty']:
        flash(_t(f'Cannot initialize "{db_name}": {result["message"]}',
                  f'لا يمكن إعداد "{db_name}": {result["message"]}'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))

    customer_name, email, mobile = customer.customer_name, customer.email, customer.mobile
    try:
        prov = provision_tenant(customer, customer_name, email, mobile, db_name=db_name, create_db=False)
    except Exception as exc:
        flash(_t(f'Could not initialize "{db_name}": {exc}. It may now be partly built -- drop all its '
                  f'tables (leave the database itself) before trying again.',
                  f'تعذر إعداد "{db_name}": {exc}. قد تكون مبنية جزئيًا -- احذف كل جداولها '
                  f'(مع إبقاء قاعدة البيانات نفسها) قبل المحاولة مرة أخرى.'), 'danger')
        return redirect(url_for('saas_customers.list_saas_customers'))
    customer = Customer.query.get(id)  # provisioning ran in its own app context -- re-read
    customer.db_status = 'connected'
    customer.db_status_message = 'Database initialized (tables and Admin login built).'
    customer.db_checked_at = datetime.utcnow()
    customer.db_initialized = True
    db.session.commit()
    flash(_t(
        f'Database "{db_name}" is initialized for "{label}". Organization (enter this at login): {db_name} | '
        f'Username: {prov["username"]} | Password: {prov["password"]}.',
        f'تم إعداد قاعدة البيانات "{db_name}" لـ "{label}". المؤسسة (تُستخدم عند تسجيل الدخول): {db_name} | '
        f'اسم المستخدم: {prov["username"]} | كلمة المرور: {prov["password"]}.',
    ), 'success')
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
    if not customer.db_initialized:
        flash(_t(f'The database "{customer.database_name}" is not initialized yet -- test its connection and '
                  f'click "Initialize database" first.',
                  f'قاعدة البيانات "{customer.database_name}" لم يتم إعدادها بعد -- اختبر الاتصال ثم اضغط '
                  f'"إعداد قاعدة البيانات" أولاً.'), 'warning')
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
    # A customer whose database is not initialized yet has nothing to sync.
    customers = Customer.query.filter(Customer.database_name.isnot(None),
                                      Customer.db_initialized == True).all()  # noqa: E712
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
    if customer.database_name and not customer.db_initialized:
        flash(_t(f'The database "{customer.database_name}" is not initialized yet, so there is no login to '
                  f'reset -- test its connection and click "Initialize database" first.',
                  f'قاعدة البيانات "{customer.database_name}" لم يتم إعدادها بعد، لذا لا يوجد تسجيل دخول لإعادة '
                  f'تعيينه -- اختبر الاتصال ثم اضغط "إعداد قاعدة البيانات" أولاً.'), 'warning')
        return redirect(url_for('saas_customers.list_saas_customers'))
    new_password = (request.form.get('new_password') or '').strip()
    if new_password and len(new_password) < 8:
        flash(_t('Password must be at least 8 characters.',
                  'يجب أن تتكون كلمة المرور من 8 أحرف على الأقل.'), 'warning')
        return redirect(url_for('saas_customers.list_saas_customers'))
    if not new_password:
        new_password = _generate_temp_password()

    if customer.database_name:
        target_db = customer.database_name
        # Legacy tenants (provisioned before the Organization-based login
        # flow) had their Admin account renamed to a unique username
        # recorded here; new tenants keep the literal "Admin" username
        # instead, so nothing is recorded in the directory for them.
        entry = SaasUserDirectory.query.filter_by(database_name=target_db).first()
        username = entry.username if entry else DEFAULT_ADMIN_USERNAME
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
