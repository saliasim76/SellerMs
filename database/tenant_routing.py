"""SaaS platform Phase 3.5: per-request routing of default-bind (tenant
ERP schema) queries to a paid customer's own dedicated database, based on
flask.session['tenant_db'] (set at login -- see database/routes/auth.py).

saas_master-bind models (SaasModule, Customer, ...) are never affected:
flask_sqlalchemy's own Session.get_bind() already resolves those via the
mapped table's bind_key BEFORE any fallback to the default bind, so this
override only ever applies to models with no __bind_key__ at all.

This subclasses flask_sqlalchemy.session.Session and is wired in via the
library's own documented customization point: SQLAlchemy(session_options=
{'class_': TenantAwareSession}) in models.py. Outside of a real Flask
request (startup, migrations, Phase 3's own tenant-provisioning code,
one-off scripts using only app.app_context()), has_request_context() is
False and this always defers to the exact existing behavior.
"""
import threading

import sqlalchemy as sa
from flask import session as flask_session, has_request_context
from flask_sqlalchemy.session import Session as _FSASession

from database.tenant_provisioning import tenant_connection_params, _uri_for

# One engine per (database, connection settings). The settings are the ones
# saved on the customer that owns the database (see tenant_connection_params),
# so editing them gives a NEW key and the stale engine is disposed of, rather
# than the old host/user/password being used until the process restarts.
_engine_cache = {}
_engine_cache_lock = threading.Lock()


def get_tenant_engine(db_name):
    params = tenant_connection_params(db_name)
    key = (db_name, params)
    engine = _engine_cache.get(key)
    if engine is None:
        with _engine_cache_lock:
            engine = _engine_cache.get(key)
            if engine is None:
                for stale in [k for k in _engine_cache if k[0] == db_name]:
                    _engine_cache.pop(stale).dispose()
                engine = sa.create_engine(
                    _uri_for(db_name, params), pool_pre_ping=True, pool_recycle=280,
                )
                _engine_cache[key] = engine
    return engine


class TenantAwareSession(_FSASession):
    def get_bind(self, mapper=None, clause=None, bind=None, **kwargs):
        if bind is None and mapper is not None and has_request_context():
            tenant_db = flask_session.get('tenant_db')
            if tenant_db:
                table = sa.inspect(mapper).local_table
                bind_key = table.metadata.info.get('bind_key') if table is not None else None
                if bind_key is None:  # default bind only -- 'saas' bind is untouched
                    return get_tenant_engine(tenant_db)
        return super().get_bind(mapper=mapper, clause=clause, bind=bind, **kwargs)
