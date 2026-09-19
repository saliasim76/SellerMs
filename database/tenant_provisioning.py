"""SaaS platform Phase 3: on-demand provisioning of a dedicated MySQL
database for a paid customer, with the full ERP schema pre-seeded and a
real admin login inside it.

Deliberately reuses SellerMs's own already-proven fresh-install sequence
(app.init_db()) UNMODIFIED, run against a throwaway Flask app instance
whose config points only at the new tenant database. Flask-SQLAlchemy's
db.init_app() is designed to be called on more than one Flask app sharing
the same SQLAlchemy() object (the "app factory / multiple apps" pattern):
each app gets its own isolated engine/session state keyed by that app
instance, so this carries zero risk to the main running app's own
connection and avoids duplicating any seed logic.

Login routing (making SellerMs's /login actually connect to a tenant's
database) is handled by the login screen's Organization field (see
database/routes/auth.py), which is set to this tenant's database_name --
NOT by a globally-unique username, since every tenant's standard
SuperAdmin/Admin accounts share the same two literal usernames (seeded by
database/routes/rbac.py's seed_rbac_catalog(), called from init_db()
below; its generic "User" account is deleted right after for SaaS
tenants -- see provision_tenant()).
"""
import re
import urllib.parse

from flask import Flask
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from config import Config, DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, OLD_SAAS_MASTER_DB_NAME, TENANT_DB_PREFIX

# Any admin-chosen or auto-generated tenant database name must match this:
# starts with a letter, then letters/digits/underscores only, 3-64 chars
# total. db_name is either code-generated (generate_tenant_db_name) or
# validated via validate_custom_db_name() before reaching here -- this is
# defense in depth before it's interpolated into DDL, which can't be
# parameterized.
_DB_NAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{2,63}$')

_RESERVED_DB_NAMES = {'mysql', 'information_schema', 'performance_schema', 'sys',
                       DB_NAME.lower(), OLD_SAAS_MASTER_DB_NAME.lower()}


def apply_tenant_db_prefix(name):
    """Prepend TENANT_DB_PREFIX (config.py) if one is configured and not
    already present -- see that var's own comment for why (cPanel-style
    shared hosting auto-scopes every database, and the DB_USER itself, to
    one prefix; a raw "Noman" is unreachable there even once created,
    since the DB_USER was never granted access to anything outside its
    own prefix)."""
    if not TENANT_DB_PREFIX or name.startswith(TENANT_DB_PREFIX):
        return name
    return f'{TENANT_DB_PREFIX}{name}'


def generate_tenant_db_name(customer_id):
    return apply_tenant_db_prefix(f"customer_{customer_id:06d}")


def _server_uri():
    """Bare-server MySQL URI (no database segment) -- MySQL requires SOME
    connection target even to issue CREATE DATABASE."""
    return (
        f"mysql+pymysql://{urllib.parse.quote_plus(DB_USER)}:{urllib.parse.quote_plus(DB_PASSWORD)}"
        f"@{DB_HOST}:{DB_PORT}/?charset=utf8mb4"
    )


def build_tenant_uri(db_name):
    return (
        f"mysql+pymysql://{urllib.parse.quote_plus(DB_USER)}:{urllib.parse.quote_plus(DB_PASSWORD)}"
        f"@{DB_HOST}:{DB_PORT}/{db_name}?charset=utf8mb4"
    )


def _mysql_errno(exc):
    args = getattr(getattr(exc, 'orig', None), 'args', ())
    return args[0] if args and isinstance(args[0], int) else None


def create_tenant_database(db_name):
    if not _DB_NAME_RE.match(db_name):
        raise ValueError(f"Refusing to create database with unexpected name: {db_name!r}")
    engine = create_engine(_server_uri())
    try:
        with engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4"))
            conn.commit()
    except OperationalError as exc:
        # 1044/1142/1227: this MySQL user isn't allowed to run CREATE DATABASE.
        # cPanel-style shared hosts only let databases be created from their own
        # control panel, so a database made there by hand (and already granted to
        # this user, hence visible below) is used as-is instead of failing.
        if _mysql_errno(exc) not in (1044, 1142, 1227):
            raise
        if database_name_exists(db_name):
            return
        short = db_name[len(TENANT_DB_PREFIX):] if TENANT_DB_PREFIX and db_name.startswith(TENANT_DB_PREFIX) else db_name
        raise ValueError(
            f'this host does not let the application create databases. In your hosting '
            f'control panel (cPanel > MySQL Databases) create a database named "{db_name}" '
            f'(type only "{short}" there if the panel adds the prefix itself), add the '
            f'application\'s database user to it with ALL PRIVILEGES, then submit this form again'
        ) from exc
    finally:
        engine.dispose()


def database_is_empty(db_name):
    """True if `db_name` can be opened with the application's own MySQL user
    and contains no tables at all. Raises if it can't be opened (no such
    database, or the user has no access to it)."""
    engine = create_engine(build_tenant_uri(db_name))
    try:
        with engine.connect() as conn:
            count = conn.execute(text(
                'SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()'
            )).scalar()
            return count == 0
    finally:
        engine.dispose()


def database_name_exists(db_name):
    """True if a database with this exact name already exists on the
    MySQL server -- checked live rather than only against Customer.
    database_name, since delete_saas_customer() deliberately leaves a
    removed customer's database in place, so its name could still be
    taken even though no Customer row references it any more."""
    engine = create_engine(_server_uri())
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :n'),
                {'n': db_name},
            ).first()
            return row is not None
    finally:
        engine.dispose()


def _check_db_name(db_name, add_prefix=True):
    """Prefix + format + reserved-name checks shared by both validators
    below; returns the final database name. `add_prefix=False` keeps the
    name exactly as typed (see validate_manual_db_name)."""
    if add_prefix:
        db_name = apply_tenant_db_prefix(db_name)
    if not _DB_NAME_RE.match(db_name):
        raise ValueError(
            'must start with a letter and contain only letters, numbers, '
            'and underscores (3-64 characters total)'
        )
    if db_name.lower() in _RESERVED_DB_NAMES:
        raise ValueError(f'"{db_name}" is a reserved name and cannot be used')
    return db_name


def validate_manual_db_name(db_name):
    """For the "I created this database myself" option on the Add Customer
    form (hosts such as cPanel only let databases be made from their own
    panel). The application never tries to CREATE it: it must already exist,
    be openable by the application's own MySQL user, and be completely
    empty. Returns the final database name, or raises ValueError with a
    user-facing message. The name is used EXACTLY as typed -- TENANT_DB_PREFIX
    is deliberately not applied, since the operator is naming a database that
    already exists, and a wrong/stale prefix setting must not be able to turn
    the real name into a different one."""
    db_name = _check_db_name(db_name, add_prefix=False)
    try:
        empty = database_is_empty(db_name)
    except OperationalError as exc:
        raise ValueError(
            f'the application\'s database user cannot open a database named "{db_name}". '
            f'Create it in your hosting panel (cPanel > MySQL Databases), add the database '
            f'user to it with ALL PRIVILEGES, and type its name exactly as the panel shows '
            f'it (capital letters matter)'
        ) from exc
    if not empty:
        raise ValueError(
            f'database "{db_name}" already contains tables (it may hold another '
            f'customer\'s data) -- use a new, empty database'
        )
    return db_name


def validate_custom_db_name(db_name):
    """Raises ValueError with a user-facing message if `db_name` (an
    admin-supplied tenant database name from the Add Customer form) is
    unsafe, reserved, or already taken. Must be called -- and its RETURN
    VALUE used, not the original argument -- before create_tenant_database()
    is ever reached, since TENANT_DB_PREFIX (config.py) may have changed
    what the actual database name needs to be."""
    db_name = _check_db_name(db_name)
    if database_name_exists(db_name):
        # A database created by hand (see create_tenant_database) is fine to use
        # while it is completely empty. One that already holds tables may belong
        # to a removed customer (delete_saas_customer() leaves theirs in place),
        # and a new customer must never be attached to that data.
        try:
            empty = database_is_empty(db_name)
        except Exception:
            empty = False
        if not empty:
            raise ValueError(
                f'database "{db_name}" already exists and is not empty '
                f'(it may hold another customer\'s data)'
            )
    return db_name


def provision_tenant(customer, admin_full_name, admin_email, admin_mobile, db_name=None, create_db=True):
    """Create a brand-new tenant database and seed it with the full ERP
    schema + default reference data -- Chart of Accounts (Levels 1-5) and
    Purchase/Sales Tax Codes included, so a new customer can start posting
    documents immediately without building their own COA from scratch.
    Only two logins are kept -- Super Admin and Admin; the generic seeded
    "User" account is removed right after provisioning (see below).
    `customer` must already have a real `.id` (flush the Customer row
    first).

    `db_name`: an admin-chosen database name, already validated by
    validate_custom_db_name() at the route layer; auto-generated from the
    customer's id when omitted. Returns {'database_name', 'username',
    'password'} -- the login credentials are the tenant's standard Admin
    account and its platform-wide default password (rbac.
    DEFAULT_ADMIN_PASSWORD), NOT randomly generated, since every tenant's
    Admin account is reached by the same literal username -- see this
    module's docstring on why the login screen's Organization field (this
    tenant's database_name) is what disambiguates which tenant that login
    applies to."""
    from app import init_db
    from models import db, User, Role
    from database.routes.rbac import (
        SUPER_ADMIN_USERNAME, DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD, DEFAULT_USER_USERNAME,
    )

    db_name = db_name or generate_tenant_db_name(customer.id)
    if create_db:
        create_tenant_database(db_name)
    # else: the operator created the (empty) database by hand and it was
    # already checked by validate_manual_db_name() at the route layer.

    tenant_app = Flask(f'tenant_{db_name}')
    tenant_app.config.from_object(Config)
    tenant_app.config['SQLALCHEMY_DATABASE_URI'] = build_tenant_uri(db_name)
    tenant_app.config['SQLALCHEMY_BINDS'] = {}
    db.init_app(tenant_app)

    with tenant_app.app_context():
        init_db(tenant_app)

        # Chart of Accounts auto-seeding is deliberately OFF in the main
        # app's own fresh-install sequence (app.py's init_db() -- its
        # Levels 1-5 were manually cleared there and must never be
        # silently regenerated), but a brand-new SaaS tenant has no
        # Chart of Accounts at all otherwise, so it's seeded explicitly
        # here instead. Also seeds the default Purchase/Sales Tax Codes
        # again defensively (init_db() already calls seed_tax_codes()
        # unconditionally, so this is normally a no-op).
        from models import seed_chart_of_accounts, seed_coa_levels_3_4_5, seed_tax_codes, seed_auto_code_selection
        seed_chart_of_accounts()
        seed_coa_levels_3_4_5()
        seed_tax_codes()
        seed_auto_code_selection()  # must run after the Chart of Accounts above -- it looks up Level Five accounts by name

        # SaaS tenants only need Super Admin + Admin. init_db() gives a
        # tenant app its own legacy 'admin'/'staff' pair (app.py's
        # init_db() only skips that pair for the main shared app, never
        # for a tenant one), and MySQL's default case-insensitive
        # collation then makes seed_rbac_catalog()'s own
        # `User.query.filter_by(username='Admin')` check see that
        # lowercase 'admin' row as an already-existing "Admin" and skip
        # seeding a real one -- so every tenant would otherwise end up
        # with NO proper Admin account at all. Delete the legacy pair and
        # the generic seeded "User" account, then build the real Admin
        # account here directly, personalized with the real customer
        # contact from the start.
        for legacy_username in ('admin', 'staff', DEFAULT_USER_USERNAME):
            legacy_user = User.query.filter_by(username=legacy_username).first()
            if legacy_user:
                db.session.delete(legacy_user)
        db.session.commit()

        # Defensive neutralization: seed_rbac_catalog() (run by init_db())
        # creates a local SuperAdmin in every fresh database, including
        # this tenant's. It must never be mistaken for real platform-level
        # Super Admin access once per-tenant login routing exists. It
        # keeps its super_admin ROLE, though, so rbac.visible_users_query()
        # still hides it from this tenant's own Admin in the Users grid.
        super_admin = User.query.filter_by(username=SUPER_ADMIN_USERNAME).first()
        if super_admin:
            super_admin.is_super_admin = False

        # Build the tenant's own Admin account, personalized with the
        # real customer contact's identity from the start. The username
        # deliberately stays the literal "Admin" -- the login screen's
        # Organization field is what disambiguates this tenant's "Admin"
        # from every other tenant's "Admin", and the password stays the
        # shared platform default so it doesn't need to be shown/copied
        # once like a generated secret would. Mirrors seed_rbac_catalog()'s
        # own (skipped) Admin-account shape exactly, including
        # is_protected=True so it can't be edited/deactivated/deleted
        # through the ordinary Users screen, same as SuperAdmin.
        admin_role = Role.query.filter_by(code='admin').first()
        admin_user = User(
            username=DEFAULT_ADMIN_USERNAME, email=admin_email,
            full_name=admin_full_name or None, mobile=admin_mobile or None,
            role='admin', role_id=admin_role.id if admin_role else None,
            is_super_admin=False, is_protected=True, is_active=True,
        )
        admin_user.set_password(DEFAULT_ADMIN_PASSWORD)
        db.session.add(admin_user)
        db.session.commit()

    return {'database_name': db_name, 'username': DEFAULT_ADMIN_USERNAME, 'password': DEFAULT_ADMIN_PASSWORD}


def sync_tenant_schema(db_name):
    """Applies SellerMs's current schema (new tables + new columns only)
    to an already-provisioned tenant database -- for whenever the main
    sellerms schema changes later (e.g. ensure_schema() in models.py
    gains a new column) and existing tenants need to catch up.

    Deliberately does NOT call the full init_db() -- that also seeds the
    RBAC catalog and the legacy 'admin'/'staff' pair provision_tenant()
    already deletes, so re-running it would resurrect that pair (with a
    known default password) in an already-customized tenant database.
    This only touches structure, never seed data or users.
    """
    from sqlalchemy.exc import OperationalError
    from models import db, ensure_schema

    tenant_app = Flask(f'tenant_sync_{db_name}')
    tenant_app.config.from_object(Config)
    tenant_app.config['SQLALCHEMY_DATABASE_URI'] = build_tenant_uri(db_name)
    tenant_app.config['SQLALCHEMY_BINDS'] = {}
    db.init_app(tenant_app)

    with tenant_app.app_context():
        try:
            # db.create_all() (no args) iterates every bind key ever seen
            # on ANY model process-wide, including 'saas' -- but this
            # tenant app deliberately has no 'saas' bind configured (see
            # module docstring), so it must be scoped to the default bind
            # only, matching init_db()'s own guard for the same reason.
            db.create_all(bind_key=None)
        except OperationalError as exc:
            if 'already exists' not in str(exc).lower():
                raise
        ensure_schema()


def reset_tenant_password(database_name, username, new_password):
    """Directly updates one user's password in a tenant (or the shared)
    database via a lightweight raw connection -- a single UPDATE doesn't
    need the full throwaway-Flask-app/db.init_app() machinery the other
    functions in this module use for running the whole init_db() sequence.
    Raises ValueError if no matching username is found in that database."""
    from werkzeug.security import generate_password_hash

    engine = create_engine(build_tenant_uri(database_name))
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text('UPDATE users SET password_hash = :h WHERE username = :u'),
                {'h': generate_password_hash(new_password), 'u': username},
            )
            if result.rowcount == 0:
                raise ValueError(f'No user "{username}" found in database "{database_name}".')
    finally:
        engine.dispose()
