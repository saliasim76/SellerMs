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
import time
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
    already present. Used ONLY for the automatically generated name (Add
    Customer's Database Name left blank) -- a name the operator types is
    always used exactly as typed, never prefixed (see _check_db_name)."""
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


def _uri_for(db_name, params):
    host, port, user, password = params
    return (
        f"mysql+pymysql://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}"
        f"@{host}:{port}/{db_name}?charset=utf8mb4"
    )


def check_db_name(db_name):
    """Trim, lower-case and validate a database name (format, reserved names).
    Returns the final name; raises ValueError with a user-facing reason."""
    return _check_db_name(db_name)


def decrypt_db_password(token):
    return _decrypt_db_password(token)


def _default_conn_params():
    return (DB_HOST, str(DB_PORT), DB_USER, DB_PASSWORD)


def _secret_key():
    try:
        from flask import current_app
        return current_app.config['SECRET_KEY']
    except RuntimeError:
        return Config.SECRET_KEY


def encrypt_db_password(plaintext):
    """Encrypt a customer's database password for storage (same Fernet scheme,
    derived from SECRET_KEY, that the ZATCA private keys use). None passes through."""
    from database.zatca.engine import encrypt_secret
    return encrypt_secret(plaintext, _secret_key())


def _decrypt_db_password(token):
    from database.zatca.engine import decrypt_secret
    return decrypt_secret(token, _secret_key())


# Connection settings saved on a customer are looked up by database name and
# cached briefly, so routing a request does not query the platform database
# every time; changes made in another worker process are picked up within the TTL.
_CONN_TTL_SECONDS = 30
_conn_cache = {}


def tenant_connection_params(db_name):
    """(host, port, user, password) used to open `db_name`: the settings saved
    on the customer that owns it when it has its own, otherwise the
    application's defaults. A customer with a user of its own gets that user's
    stored password (blank if none); host/port/user each fall back on their own."""
    now = time.time()
    hit = _conn_cache.get(db_name)
    if hit and hit[0] > now:
        return hit[1]
    try:
        from models import db
        with db.engines['saas'].connect() as conn:
            row = conn.execute(text(
                'SELECT db_host, db_port, db_user, db_password_enc '
                'FROM proledge_saas_customers WHERE database_name = :n'), {'n': db_name}).first()
    except Exception:
        return _default_conn_params()   # no app context / table not there yet -- not cached
    params = _default_conn_params()
    if row and (row[0] or row[1] or row[2] or row[3]):
        host = row[0] or DB_HOST
        port = str(row[1] or DB_PORT)
        if row[2]:
            user, password = row[2], (_decrypt_db_password(row[3]) if row[3] else '')
        else:
            user, password = DB_USER, DB_PASSWORD
        params = (host, port, user, password)
    _conn_cache[db_name] = (now + _CONN_TTL_SECONDS, params)
    return params


def forget_tenant_connection(db_name):
    """Drop the cached settings for `db_name` (call after they are edited)."""
    _conn_cache.pop(db_name, None)


def build_tenant_uri(db_name):
    return _uri_for(db_name, tenant_connection_params(db_name))


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
        raise ValueError(
            f'this host does not let the application create databases. Create an empty '
            f'database in your hosting panel (cPanel > MySQL Databases), add the '
            f'application\'s database user to it with ALL PRIVILEGES, then submit this form '
            f'again with Database Creation Method set to Manual and the database\'s full '
            f'name exactly as the panel shows it (cPanel adds your account prefix itself)'
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


def _check_db_name(db_name):
    """Format + reserved-name checks shared by both validators below;
    returns the final database name. The name is used exactly as typed,
    except that it is trimmed and lower-cased: Linux MySQL treats
    "Noman" and "noman" as two different databases, so one fixed
    lower-case spelling avoids ever mixing them up. No prefix is added."""
    db_name = (db_name or '').strip().lower()
    if not _DB_NAME_RE.match(db_name):
        raise ValueError(
            'must start with a letter and contain only letters, numbers, '
            'and underscores (3-64 characters total)'
        )
    if db_name.lower() in _RESERVED_DB_NAMES:
        raise ValueError(f'"{db_name}" is a reserved name and cannot be used')
    return db_name


def _visible_databases(limit=20):
    """Names of the non-system databases the application's own MySQL user can
    see (MySQL only lists a database to a user that has some privilege on it).
    [] if the lookup itself fails."""
    engine = create_engine(_server_uri())
    try:
        with engine.connect() as conn:
            names = [row[0] for row in conn.execute(text('SHOW DATABASES'))]
    except Exception:
        return []
    finally:
        engine.dispose()
    system = {'information_schema', 'mysql', 'performance_schema', 'sys'}
    return sorted(n for n in names if n.lower() not in system)[:limit]


def _explain_connection_error(exc, db_name, user, list_visible):
    code = _mysql_errno(exc)
    if code == 1045:
        return f'Access denied for the MySQL user "{user}" -- the username or password is wrong.'
    if code in (1044, 1049):
        msg = (f'The MySQL user "{user}" cannot open a database named "{db_name}": it does not exist '
               f'yet, the name is misspelt, or that user has not been given access to it.')
        if list_visible:
            visible = _visible_databases()
            msg += (f' The databases that user can currently see are: {", ".join(visible)}.'
                    if visible else ' That user cannot see any database besides the system ones.')
        return msg
    if code in (2003, 2005, 2006, 2013):
        return f'Cannot reach the MySQL server ({str(getattr(exc, "orig", exc))[:120]}).'
    return f'Connection failed: {str(getattr(exc, "orig", exc))[:200]}'


def test_database_connection(db_name, host=None, port=None, user=None, password=None):
    """Try to open `db_name` with the given settings (blank host/port/user =
    the application's own defaults) and report what is in it. Never raises.

    Returns {'ok', 'status' ('connected' | 'failed'), 'message', 'database_name',
    'tables', 'empty', 'initialized'}; `initialized` means this application's own
    tables are already there. Nothing is created or changed in the database."""
    result = {'ok': False, 'status': 'failed', 'message': '', 'database_name': db_name,
              'tables': 0, 'empty': False, 'initialized': False}
    try:
        db_name = _check_db_name(db_name)
    except ValueError as exc:
        result['message'] = f'Invalid database name: {exc}.'
        return result
    result['database_name'] = db_name
    host = (host or '').strip() or DB_HOST
    port = str(port or '').strip() or str(DB_PORT)
    user = (user or '').strip()
    own_user = bool(user)
    if not own_user:
        user, password = DB_USER, DB_PASSWORD
    engine = create_engine(_uri_for(db_name, (host, port, user, password or '')),
                           connect_args={'connect_timeout': 8})
    try:
        with engine.connect() as conn:
            tables = {row[0] for row in conn.execute(text(
                'SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()'))}
    except OperationalError as exc:
        result['message'] = _explain_connection_error(exc, db_name, user, not own_user and host == DB_HOST)
        return result
    except Exception as exc:
        result['message'] = f'Connection failed: {str(exc)[:200]}'
        return result
    finally:
        engine.dispose()

    result.update(ok=True, status='connected', tables=len(tables), empty=not tables)
    if not tables:
        result['message'] = ('Connected. The database is empty -- click "Initialize database" to build '
                             "the tables and the customer's Admin login.")
    elif 'users' in tables:
        result['initialized'] = True
        result['message'] = f"Connected. This application's tables are already there ({len(tables)} tables)."
    else:
        result['message'] = (f"Connected, but the database already contains {len(tables)} tables that are "
                             f"not this application's -- it cannot be initialized.")
    return result


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
    # else (manual): the operator created the (empty) database by hand; the
    # caller has already verified it is reachable and empty.

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
