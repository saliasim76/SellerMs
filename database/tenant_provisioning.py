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
database) is a separate, deferred phase -- this module only provisions
the database and records it in SaasUserDirectory for that later phase to
read from.
"""
import re
import secrets
import string
import urllib.parse

from flask import Flask
from sqlalchemy import create_engine, text

from config import Config, DB_HOST, DB_PORT, DB_USER, DB_PASSWORD

# Phase 4 renamed the naming convention to customer_NNNNNN going forward;
# erp_client_NNNNNN (Phase 3) is kept accepted here since 4 real customers
# already have databases under that name and nothing re-validates an
# already-stored database_name against this pattern after the fact.
_DB_NAME_RE = re.compile(r'^(erp_client|customer)_\d{6}$')


def generate_tenant_db_name(customer_id):
    return f"customer_{customer_id:06d}"


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


def create_tenant_database(db_name):
    if not _DB_NAME_RE.match(db_name):
        # db_name is always code-generated (generate_tenant_db_name), never
        # user input -- this is defense in depth before it's interpolated
        # into DDL, which can't be parameterized.
        raise ValueError(f"Refusing to create database with unexpected name: {db_name!r}")
    engine = create_engine(_server_uri())
    try:
        with engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4"))
            conn.commit()
    finally:
        engine.dispose()


def _generate_password(length=12):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def provision_tenant(customer, admin_full_name, admin_email, admin_mobile):
    """Create a brand-new tenant database, seed it with the full ERP schema
    + default reference data, and replace its default admin account with
    the real customer admin's identity. `customer` must already have a
    real `.id` (flush the Customer row first). Returns
    {'database_name', 'username', 'password'}."""
    from app import init_db
    from models import db, User, SaasUserDirectory
    from database.routes.rbac import SUPER_ADMIN_USERNAME
    from database.routes.saas_internal import _unique_username

    db_name = generate_tenant_db_name(customer.id)
    create_tenant_database(db_name)

    # Generated in the CALLER's app context (which has the 'saas' bind
    # configured) since SaasUserDirectory lives there -- the tenant app
    # context below deliberately has no 'saas' bind at all. Checked against
    # BOTH the directory and the shared database's own users table, so a
    # generated tenant username can never collide with an existing
    # shared-DB username (e.g. "admin") -- Phase 3.5's login routing keys
    # off username alone, so this uniqueness must be global.
    username = _unique_username(
        admin_email,
        exists_fn=lambda name: (
            SaasUserDirectory.query.filter_by(username=name).first() is not None
            or User.query.filter_by(username=name).first() is not None
        ),
    )
    password = _generate_password()

    tenant_app = Flask(f'tenant_{db_name}')
    tenant_app.config.from_object(Config)
    tenant_app.config['SQLALCHEMY_DATABASE_URI'] = build_tenant_uri(db_name)
    tenant_app.config['SQLALCHEMY_BINDS'] = {}
    db.init_app(tenant_app)

    with tenant_app.app_context():
        init_db(tenant_app)

        # Defensive neutralization: seed_rbac_catalog() (run by init_db())
        # creates a local SuperAdmin in every fresh database, including
        # this tenant's. It must never be mistaken for real platform-level
        # Super Admin access once per-tenant login routing exists.
        super_admin = User.query.filter_by(username=SUPER_ADMIN_USERNAME).first()
        if super_admin:
            super_admin.is_super_admin = False

        # Overwrite the auto-seeded default admin with the real customer
        # admin's identity.
        admin_user = User.query.filter_by(username='admin').first()
        admin_user.username = username
        admin_user.email = admin_email
        admin_user.full_name = admin_full_name or None
        admin_user.mobile = admin_mobile or None
        admin_user.set_password(password)
        db.session.commit()

    # Back on the main app's own session -- saas_master bind.
    db.session.add(SaasUserDirectory(username=username, customer_id=customer.id, database_name=db_name))

    return {'database_name': db_name, 'username': username, 'password': password}


def sync_tenant_schema(db_name):
    """Applies SellerMs's current schema (new tables + new columns only)
    to an already-provisioned tenant database -- for whenever the main
    sellerms schema changes later (e.g. ensure_schema() in models.py
    gains a new column) and existing tenants need to catch up.

    Deliberately does NOT call the full init_db() -- that also seeds a
    default 'admin'/'staff' user and the RBAC catalog, and since a
    tenant's admin account is renamed away from 'admin' during
    provisioning, re-running init_db() would incorrectly recreate a
    fresh default admin account (known password) in an already-customized
    tenant database. This only touches structure, never seed data or
    users.
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
