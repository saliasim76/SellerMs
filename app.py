import os
from flask import Flask, session, render_template
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from config import config
from models import db, User, ActivityLog

try:
    from flask_migrate import Migrate
except Exception as exc:  # pragma: no cover - defensive for env issues
    Migrate = None
    MIGRATE_IMPORT_ERROR = exc
else:
    MIGRATE_IMPORT_ERROR = None

# Import document blueprints
from database.routes.supplier_doc import supplier_doc_bp
from database.routes.buyer_doc import buyer_doc_bp

# Try to import Flask-Babel; gracefully degrade if missing
try:
    import importlib
    flask_babel = importlib.import_module('flask_babel')
    Babel = flask_babel.Babel
    BABEL_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    Babel = None
    BABEL_AVAILABLE = False

login_manager = LoginManager()
csrf = CSRFProtect()
migrate = Migrate() if Migrate is not None else None


def get_locale():
    return session.get('lang', 'en')


def create_app(config_name='default'):
    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # config.py's SECRET_KEY/DB_PASSWORD fall back to hardcoded dev defaults
    # so local dev keeps working with zero setup -- but starting a real
    # public deployment with those same known values is a real credential-
    # exposure risk, so production refuses to boot until the host actually
    # sets them as real environment variables (or a real .env file).
    if config_name == 'production':
        missing = []
        if not os.environ.get('SECRET_KEY'):
            missing.append('SECRET_KEY')
        if not os.environ.get('DATABASE_URL') and not os.environ.get('DB_PASSWORD'):
            missing.append('DB_PASSWORD (or DATABASE_URL)')
        if missing:
            raise RuntimeError(
                'Refusing to start with production config: missing real value(s) for '
                + ', '.join(missing) +
                '. Set them as real environment variables on the host (or a real .env '
                'file next to app.py) before starting the app -- see .env.example.'
            )

    db.init_app(app)

    # Phusion Passenger (cPanel's Python App hosting) uses fork-based "smart
    # spawning" by default: it loads this module ONCE, creating the engines
    # above and their live pooled DB connections, then forks additional
    # worker processes from that same loaded state. Every forked child
    # inherits the exact same open TCP sockets as the parent, so as soon as
    # two processes read/write the same connection the MySQL protocol
    # desyncs -- surfacing as random "MySQL server has gone away" /
    # BrokenPipeError crashes that pool_pre_ping cannot catch (the socket
    # looks alive right up until another process touches it). Disposing
    # each engine's pool immediately after a fork (child side only) forces
    # every child to open its own fresh connections instead of sharing the
    # parent's. This is SQLAlchemy's own documented fix for pooling across
    # os.fork() -- see "Using Connection Pools with Multiprocessing or
    # os.fork()" in the SQLAlchemy pooling docs. os.fork()/register_at_fork
    # don't exist on Windows, so this is a no-op for local dev.
    if hasattr(os, 'register_at_fork'):
        with app.app_context():
            _engines_to_dispose_after_fork = list(db.engines.values())

        def _dispose_db_engines_after_fork():
            for _engine in _engines_to_dispose_after_fork:
                try:
                    _engine.dispose(close=False)
                except Exception:
                    pass
        os.register_at_fork(after_in_child=_dispose_db_engines_after_fork)

    login_manager.init_app(app)
    csrf.init_app(app)
    if migrate is not None:
        migrate.init_app(app, db)

    if BABEL_AVAILABLE:
        babel = Babel()
        babel.init_app(app, locale_selector=get_locale)

    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'warning'

    @login_manager.user_loader
    def load_user(user_id):
        if ':' in user_id:
            # Composite id from a tenant login (see User.get_id()) -- restore
            # tenant routing before loading, so this also works when only
            # the long-lived remember-cookie survived (e.g. the plain
            # session cookie itself already expired).
            tenant_db, real_id = user_id.split(':', 1)
            session['tenant_db'] = tenant_db
            return db.session.get(User, int(real_id))
        return db.session.get(User, int(user_id))

    def _sidebar_org_name():
        from database.routes.shared import current_saas_customer
        try:
            customer = current_saas_customer()
        except Exception:
            customer = None
        if customer:
            return customer.company_name or customer.customer_name or 'Proledge'
        return 'Proledge'

    @app.context_processor
    def inject_globals():
        from database.routes.rbac import can
        from database.routes.shared import is_basic_mode
        from database.i18n import t as _caption, dual as _dual, t_other as _t_other, RTL_LANGS, LANG_NATIVE
        from models import Owner

        selected_locale = session.get('lang', 'en')
        second_lang_map = app.config.get('SECOND_LANGUAGES', {})

        # The Owner's "Second Language" is the single official translation
        # configuration for the whole app (see Owner Form -> Second
        # Language, Super-Admin-only). With multiple Owner records this
        # falls back to the first one (by id) -- there is no per-user/
        # per-session Owner context anywhere else in the app either.
        try:
            primary_owner = Owner.query.order_by(Owner.id).first()
            configured_name = (primary_owner.second_language or '').strip() if primary_owner else ''
        except Exception:
            configured_name = ''
        configured_lang = second_lang_map.get(configured_name, 'ar')

        # Deep, page-level bilingual content (every existing
        # `{% if current_locale == 'ar' %}` / `{% set ar = ... %}` check)
        # only activates when the selected language IS the configured
        # Second Language -- so `current_locale` keeps meaning exactly what
        # it always has, and none of those templates need to change.
        current_locale = selected_locale if selected_locale == configured_lang else 'en'

        return dict(
            current_locale=current_locale,
            selected_locale=selected_locale,
            configured_lang=configured_lang,
            configured_lang_native=LANG_NATIVE.get(configured_lang, configured_lang),
            # True only when the ACTIVE language (current_locale) is a
            # right-to-left script (Arabic, Urdu). French/Hindi/Bengali are
            # LTR even when they are the active configured Second Language.
            is_rtl=current_locale in RTL_LANGS,
            languages=app.config.get('LANGUAGES', {}),
            can=can,
            # Captions gate exactly like deep page content: they only
            # translate when the selected language IS the Owner's
            # configured Second Language (current_locale), not whatever
            # else the user picked in the dashboard's language dropdown.
            t=lambda key: _caption(key, current_locale),
            # Bilingual "English / Second-Language" field labels, always in
            # the app's two official languages regardless of the viewer's
            # own dashboard pick.
            dual=lambda key: _dual(key, current_locale, configured_lang),
            t_other=lambda key: _t_other(key, current_locale, configured_lang),
            # SaaS Basic/Expert plan gate -- True locks Chart of Accounts
            # and every Post & Save button for everyone in this tenant
            # except a Super Admin user (see is_basic_mode() in
            # database/routes/shared.py).
            is_basic_mode=is_basic_mode(),
            # Subscription-expiry renewal popup -- set once by
            # auth.py's _complete_login() on every login, consumed
            # (popped) here on the very next page render so it shows
            # exactly once per login rather than on every page view
            # within that session, but reappears the next time this
            # user logs in since it's recomputed fresh each time.
            subscription_warning=session.pop('subscription_warning', None),
            # Sidebar branding line under the SellerMS logo: the SaaS
            # customer's own company name when logged into a tenant
            # (paid or trial), or "Proledge" when logged into the
            # platform itself (Super Admin, no tenant/customer context).
            sidebar_org_name=_sidebar_org_name(),
        )

    # Register blueprints
    from database.routes.auth import auth_bp
    from database.routes.dashboard import dashboard_bp
    from database.routes.sellers import sellers_bp
    from database.routes.employees import employees_bp
    from database.routes.work_allocations import wa_bp
    from database.routes.purchase import pur_bp
    from database.routes.sales import sale_bp
    from database.routes.coa import coa_bp
    from database.routes.journal import journal_bp
    from database.routes.grl import grl_bp
    from database.routes.opening_balance import ob_bp
    from database.routes.payroll import payroll_bp
    from database.routes import lookups_bp
    from database.routes.employee_import import emp_import_bp
    from database.routes.purchase_tax_code import purchase_tax_bp
    from database.routes.sales_tax_code import sales_tax_bp
    from database.routes.unit_measurement import uom_bp
    from database.routes.financial import financial_bp  # ✅ ADD THIS
    from database.routes.auto_code_selection import auto_code_bp
    from database.routes.db_backup import db_backup_bp

    # Role & Permission system: Recycle Bin, User Management, Permission
    # Matrix, Audit Log -- see database/routes/rbac.py for the core engine.
    from database.routes.recycle_bin import recycle_bp
    from database.routes.user_management import user_mgmt_bp
    from database.routes.role_permission_matrix import rbac_ui_bp
    from database.routes.audit_log_ui import audit_ui_bp
    from database.routes.account import account_bp

    # SaaS platform Phase 1: Super Admin module/pricing catalog.
    from database.routes.saas_admin import saas_admin_bp
    # SaaS platform Phase 3: Super Admin paid-customer onboarding + dedicated
    # per-tenant database provisioning.
    from database.routes.saas_customers import saas_customers_bp
    # Customer Support: tenant ticket submission + Super Admin ticket queue.
    from database.routes.support import support_bp
    # Cash & Bank: Outgoing / Incoming Payment vouchers.
    from database.routes.cash_bank import cash_bank_bp
    # Phase 4: Proledg landing page + trial signup, merged into this same
    # application/process.
    from database.routes.proledg_site import proledg_bp
    # ZATCA e-Invoicing (Phase 2): onboarding/settings screen.
    from database.routes.zatca import zatca_bp

    app.register_blueprint(emp_import_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(sellers_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(wa_bp)
    app.register_blueprint(pur_bp)
    app.register_blueprint(sale_bp)
    app.register_blueprint(coa_bp)
    app.register_blueprint(journal_bp)
    app.register_blueprint(grl_bp)
    app.register_blueprint(ob_bp)
    app.register_blueprint(payroll_bp, url_prefix='/payroll')
    app.register_blueprint(lookups_bp)
    app.register_blueprint(supplier_doc_bp)
    app.register_blueprint(buyer_doc_bp)
    app.register_blueprint(financial_bp)  # ✅ ADD THIS
    app.register_blueprint(purchase_tax_bp)
    app.register_blueprint(sales_tax_bp)
    app.register_blueprint(uom_bp)
    app.register_blueprint(auto_code_bp)
    app.register_blueprint(db_backup_bp)
    app.register_blueprint(recycle_bp)
    app.register_blueprint(user_mgmt_bp)
    app.register_blueprint(rbac_ui_bp)
    app.register_blueprint(audit_ui_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(saas_admin_bp)
    app.register_blueprint(support_bp)
    app.register_blueprint(cash_bank_bp)
    app.register_blueprint(saas_customers_bp)
    app.register_blueprint(proledg_bp)
    app.register_blueprint(zatca_bp)

    # Generic import/export for master pages (excludes purchases & sales).
    from database.routes.io_tools import io_bp
    from database.routes.io_registrations import register_all
    app.register_blueprint(io_bp)
    register_all()

    # Recycle Bin registrations (which models/children are soft-deletable).
    from database.routes.recycle_registrations import register_all_recyclables
    register_all_recyclables()

    # Error handlers
    @app.errorhandler(404)
    def not_found(e):
        return render_template('errors/404.html'), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template('errors/403.html'), 403

    # Jinja filters
    @app.template_filter('filesizeformat')
    def filesizeformat_filter(value):
        if value is None:
            return 'N/A'
        if value < 1024:
            return f'{value} B'
        elif value < 1024 * 1024:
            return f'{value/1024:.1f} KB'
        else:
            return f'{value/(1024*1024):.1f} MB'

    _AR_DIGIT_MAP = str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩')

    @app.template_filter('ar_digits')
    def ar_digits_filter(value):
        """Renders any Western digits in `value` as Arabic-Indic numerals
        (٠-٩) -- for numbers shown next to an Arabic caption/label, e.g. a
        VAT or CRN number on a bilingual document's Arabic side. Mirrors
        sinv_list.html's own client-side toArabicDigits() JS helper, used
        for the Seller/Buyer detail cards, but as a server-side Jinja
        filter for use in print templates instead."""
        if value is None:
            return '—'
        return str(value).translate(_AR_DIGIT_MAP)

    # Recycle Bin + Audit Log: daily auto-purge of records past their 7-day
    # retention (both screens also do a lazy sweep on load as a backup).
    # Guarded against Flask's debug reloader, which runs create_app() in
    # two processes -- WERKZEUG_RUN_MAIN is only set in the child that
    # actually serves requests, so the job starts exactly once.
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            def _purge_job():
                with app.app_context():
                    from database.routes.recycle_bin import purge_expired
                    purge_expired()

            def _audit_log_purge_job():
                with app.app_context():
                    from database.routes.audit_log_ui import purge_expired_audit_log
                    purge_expired_audit_log()

            def _pq_sq_auto_pending_job():
                with app.app_context():
                    from database.routes.purchase import auto_pend_expired_pq
                    from database.routes.sales import auto_pend_expired_sq
                    auto_pend_expired_pq()
                    auto_pend_expired_sq()

            scheduler = BackgroundScheduler(daemon=True)
            scheduler.add_job(_purge_job, 'interval', days=1, id='recycle_bin_purge')
            scheduler.add_job(_audit_log_purge_job, 'interval', days=1, id='audit_log_purge')
            scheduler.add_job(_pq_sq_auto_pending_job, 'interval', days=1, id='pq_sq_auto_pending')
            scheduler.start()
        except Exception as exc:
            print(f'Recycle Bin scheduler not started: {exc}')

    return app


def init_db(app):
    """Initialize database and create default admin user."""
    from sqlalchemy.exc import OperationalError

    with app.app_context():
        try:
            # Flask-SQLAlchemy's db.create_all() (no args) iterates every
            # bind key it has ever seen declared on ANY model (db.metadatas
            # is a process-global registry, not per-app), so it needs the
            # 'saas' bind configured even when the currently-active app
            # doesn't want it -- e.g. a Phase 3 tenant-provisioning app,
            # which only wants the default-bind tenant schema. When 'saas'
            # isn't in this app's own config, create only the default bind
            # so init_db() stays reusable unmodified for that case; the
            # main app (which always configures the 'saas' bind) keeps its
            # exact original behavior.
            if 'saas' in app.config.get('SQLALCHEMY_BINDS', {}):
                db.create_all()
            else:
                db.create_all(bind_key=None)
        except OperationalError as exc:
            if 'already exists' in str(exc).lower():
                print('Database already initialized, skipping create_all.')
            else:
                raise

        # Add any newly-introduced columns an older DB may be missing.
        try:
            from models import ensure_schema
            ensure_schema()
        except Exception as _e:
            print(f'ensure_schema skipped: {_e}')

        # Create default 'admin'/'staff' users -- but NOT for the main
        # shared app, which per the "SuperAdmin is the only shared-database
        # login" requirement should never get one. Tenant apps (no 'saas'
        # bind configured) still need this default 'admin' account, since
        # provision_tenant() renames it into the real tenant admin's
        # identity right after this runs.
        if 'saas' not in app.config.get('SQLALCHEMY_BINDS', {}) and not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@sellerms.com', role='admin')
            admin.set_password('Admin@123')
            db.session.add(admin)

            staff = User(username='staff', email='staff@sellerms.com', role='staff')
            staff.set_password('Staff@123')
            db.session.add(staff)
            db.session.commit()
            print('Default users created: admin / Admin@123 | staff / Staff@123')

        # Add tax_rate column to existing tax code tables (idempotent).
        try:
            from sqlalchemy import text

            def _has_column(table, column):
                # TABLE_SCHEMA must be pinned to the current database -- MySQL's
                # INFORMATION_SCHEMA is shared across every database on the server.
                row = db.session.execute(text(
                    "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME=:t AND COLUMN_NAME=:c"
                ), {'t': table, 'c': column}).fetchone()
                return row is not None

            for tax_table in ('purchase_tax_code', 'sales_tax_code'):
                if not _has_column(tax_table, 'tax_rate'):
                    db.session.execute(text(
                        f"ALTER TABLE {tax_table} ADD tax_rate NUMERIC(10, 4) DEFAULT 0"
                    ))
                    db.session.commit()
                    print(f"Added tax_rate column to {tax_table}")

            # Update existing tax codes with default rates
            try:
                db.session.execute(text(
                    "UPDATE purchase_tax_code SET tax_rate = 15 WHERE tax_code IN ('15', '15C', '15R')"
                ))
                db.session.execute(text(
                    "UPDATE purchase_tax_code SET tax_rate = 0 WHERE tax_code IN ('0', '0E', '0O')"
                ))
                db.session.execute(text(
                    "UPDATE sales_tax_code SET tax_rate = 15 WHERE tax_code IN ('15', '15C', '15R')"
                ))
                db.session.execute(text(
                    "UPDATE sales_tax_code SET tax_rate = 0 WHERE tax_code IN ('0', '0E', '0O', '0S')"
                ))
                db.session.commit()
                print("Updated existing tax codes with default tax rates")
            except Exception as e:
                db.session.rollback()
                print(f"Note: Could not update tax rates: {e}")
        except Exception as e:
            db.session.rollback()
            print(f"Note: Tax rate migration skipped: {e}")

        # Chart of Accounts auto-seeding disabled -- Levels 1-5 were deliberately
        # cleared and must be built manually from here on; do not reintroduce the
        # default seed data on startup.
        # try:
        #     from models import seed_chart_of_accounts, seed_coa_levels_3_4_5
        #     result = seed_chart_of_accounts()
        #     if result['level_one_inserted'] or result['level_two_inserted']:
        #         print(f"Chart of Accounts seeded: "
        #               f"{result['level_one_inserted']} Level 1, "
        #               f"{result['level_two_inserted']} Level 2 records inserted.")
        #     deep = seed_coa_levels_3_4_5()
        #     if any(deep.values()):
        #         print(f"Chart of Accounts seeded: "
        #               f"{deep['level_three']} Level 3, {deep['level_four']} Level 4, "
        #               f"{deep['level_five']} Level 5 records inserted.")
        # except Exception as exc:
        #     print(f'Chart of Accounts seed skipped: {exc}')

        # Seed tax codes (idempotent)
        try:
            from models import seed_tax_codes
            n = seed_tax_codes()
            if n:
                print(f'Tax codes seeded: {n} records inserted.')
        except Exception as exc:
            print(f'Tax code seed skipped: {exc}')

        # Seed the 4 default Payment Modes (idempotent).
        try:
            from models import seed_payment_modes
            n = seed_payment_modes()
            if n:
                print(f'Payment modes seeded: {n} records inserted.')
        except Exception as exc:
            print(f'Payment mode seed skipped: {exc}')

        # Seed the Role & Permission catalog + the dedicated SuperAdmin
        # account (idempotent -- safe on every startup).
        try:
            from database.routes.rbac import seed_rbac_catalog
            seed_rbac_catalog()
        except Exception as exc:
            print(f'RBAC catalog seed skipped: {exc}')

        # Seed the 3 fixed Stores (Fixed Asset / Consumable / Non-Consumable).
        try:
            from models import seed_stores
            seed_stores()
        except Exception as exc:
            print(f'Store seed skipped: {exc}')

        # SaaS platform Phase 1: module/pricing catalog in the saas_master
        # database (idempotent -- safe on every startup). Requires the
        # saas_master schema to already exist on the MySQL server.
        try:
            from models import (
                ensure_saas_schema, merge_saas_master_into_sellerms, seed_saas_modules,
                seed_saas_module_rbac_links,
            )
            ensure_saas_schema()
            merge_saas_master_into_sellerms()
            n = seed_saas_modules()
            if n:
                print(f'SaaS module catalog seeded: {n} modules inserted.')
            n2 = seed_saas_module_rbac_links()
            if n2:
                print(f'SaaS module -> RBAC module links seeded: {n2} links inserted.')
        except Exception as exc:
            print(f'SaaS module catalog seed skipped: {exc}')


if __name__ == '__main__':
    os.makedirs('database', exist_ok=True)
    os.makedirs('uploads', exist_ok=True)
    # Create uploads subdirectories
    os.makedirs('uploads/suppliers', exist_ok=True)
    os.makedirs('uploads/buyers', exist_ok=True)
    os.makedirs('static/uploads/purchase', exist_ok=True)
    os.makedirs('static/uploads/sales', exist_ok=True)
    
    app = create_app()
    init_db(app)
    app.run(debug=True, port=5000)