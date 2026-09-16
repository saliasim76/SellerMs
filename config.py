import os
import urllib.parse
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Load variables from a local .env file (if present) into the process
# environment before anything below reads os.environ.get(...). Safe to call
# even when no .env exists (no-op), and never overrides a variable that's
# already set in the real environment (e.g. by a production host).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, '.env'))
except ImportError:
    pass

# ─────────────────────────────────────────────────────────────
# MySQL connection
# Override any of these with environment variables if needed,
# e.g. set DB_HOST=yourhost DB_PORT=3306
# ─────────────────────────────────────────────────────────────
# .strip() on every one of these: a stray leading/trailing space pasted into
# cPanel's "Environment variables" field (invisible in that UI) otherwise
# reaches MySQL as part of the value -- e.g. DB_NAME "mydb " gets rejected
# outright with "Incorrect database name", crashing the app on every single
# request before it ever creates a table.
DB_HOST     = os.environ.get('DB_HOST', 'localhost').strip()
DB_PORT     = os.environ.get('DB_PORT', '3306').strip()
DB_NAME     = os.environ.get('DB_NAME', 'sellerms').strip()
# No real-credential fallback here on purpose -- a missing DB_USER/DB_PASSWORD
# env var must fail loudly (wrong username/password against MySQL) rather
# than silently connect with a hardcoded account. Every real environment
# (local dev via .env, or the host's env vars) is expected to set these.
DB_USER     = os.environ.get('DB_USER', 'set-DB_USER-in-.env').strip()
DB_PASSWORD = os.environ.get('DB_PASSWORD', 'set-DB_PASSWORD-in-.env').strip()

# cPanel-style shared hosting auto-prefixes every MySQL database (and user)
# with the cPanel account name, e.g. a "Noman" database really ends up
# created as "proledg_Noman" -- the DB_USER itself is scoped by that same
# host to only databases matching its own prefix. Set this (e.g.
# "proledg_") on such a host so every SaaS tenant database
# tenant_provisioning.py creates is automatically named within what the
# host actually allows; leave blank for local dev / hosts with no such
# restriction, where a tenant database is just named exactly as typed.
TENANT_DB_PREFIX = os.environ.get('TENANT_DB_PREFIX', '').strip()

MYSQL_URI = (
    f"mysql+pymysql://{urllib.parse.quote_plus(DB_USER)}:{urllib.parse.quote_plus(DB_PASSWORD)}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
)

# ─────────────────────────────────────────────────────────────
# SaaS control tables (module catalog, customers, subscriptions,
# payments, user directory) live physically inside THIS SAME
# `sellerms` database as of the Phase 4 single-application merge --
# the 'saas' bind key below is kept as a separate Flask-SQLAlchemy
# bind (pointed at the exact same URI as the default bind) purely so
# TenantAwareSession (database/tenant_routing.py) can keep telling
# "central control table" apart from "tenant business table" even
# though they're no longer in physically separate databases.
#
# The pre-merge `saas_master` database (used through Phase 1-3.5) is
# left on the server, untouched, as a migration safety net -- it is
# no longer read from or written to. Its name is kept here only so
# the one-off merge migration (models.merge_saas_master_into_sellerms)
# knows where to copy the old rows from.
# ─────────────────────────────────────────────────────────────
OLD_SAAS_MASTER_DB_NAME = os.environ.get('OLD_SAAS_MASTER_DB_NAME', 'saas_master')

class Config:
    SECRET_KEY = (os.environ.get('SECRET_KEY') or 'seller-ms-secret-key-2024-change-in-production').strip()
    # ZATCA Phase 2: gates the actual outbound HTTP calls to ZATCA's
    # Compliance-Check/Production-CSID/Clearance/Reporting endpoints. Off by
    # default -- invoices are still built, hashed, and signed locally
    # (needed for the QR/XML/chain-state to work at all) even while this is
    # off; only the live network round-trip is held back until real
    # sandbox/production credentials have been verified.
    ZATCA_LIVE_CALLS_ENABLED = os.environ.get('ZATCA_LIVE_CALLS_ENABLED', 'false').strip().lower() == 'true'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', MYSQL_URI).strip()
    # pool_pre_ping: test each pooled connection with a cheap query before
    # handing it to a request, transparently reconnecting if it's dead (e.g.
    # killed server-side, or dropped after MySQL's wait_timeout). Without
    # this, the first request after any such disconnect fails outright.
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 280}
    # Flask-SQLAlchemy builds each bind's engine from ITS OWN dict here --
    # it does NOT inherit SQLALCHEMY_ENGINE_OPTIONS above (that only applies
    # to the default, un-bound engine). A bind given as a bare URL string
    # therefore gets NO pool_pre_ping/pool_recycle at all, so its connections
    # silently go stale after MySQL's wait_timeout with nothing to catch it.
    # Repeat the same options explicitly for every bind to avoid that.
    SQLALCHEMY_BINDS = {
        'saas': {
            'url': os.environ.get('DATABASE_URL', MYSQL_URI).strip(),
            'pool_pre_ping': True,
            'pool_recycle': 280,
        },
    }
    WTF_CSRF_ENABLED = True
    # Total request size cap (form + line items + ALL attachments in one POST).
    # Must be comfortably larger than the per-file limit (16MB in the upload
    # routes), otherwise a single max-size file can never be saved: the form
    # data pushes the request over the cap and Flask returns a 413 before the
    # route runs.
    MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 64MB max per request
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
    ALLOWED_MIMETYPES = {
    'application/pdf',
    'image/jpeg',
    'image/png',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',  # .docx
    'application/msword',  # .doc - add this
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',  # .xlsx
    'application/vnd.ms-excel',  # .xls 
}
    REMEMBER_COOKIE_DURATION = timedelta(days=30)

    # Flask-Babel
    BABEL_DEFAULT_LOCALE = 'en'
    BABEL_DEFAULT_TIMEZONE = 'UTC'
    # All languages selectable from the Main Dashboard's language switcher.
    LANGUAGES = {'en': 'English', 'ar': 'العربية', 'fr': 'Français', 'ur': 'اردو', 'hi': 'हिन्दी', 'bn': 'বাংলা'}
    # Owner Form "Second Language" dropdown options -> locale code. English
    # is intentionally excluded: English is always the fixed first language.
    SECOND_LANGUAGES = {'Arabic': 'ar', 'French': 'fr', 'Urdu': 'ur', 'Hindi': 'hi', 'Bengali': 'bn'}

    ITEMS_PER_PAGE = 15

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}