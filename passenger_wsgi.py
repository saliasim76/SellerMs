"""
Phusion Passenger WSGI entry point (cPanel "Setup Python App" hosting).

Passenger looks for a WSGI `application` object in this exact file. This
project uses the application-factory pattern (create_app() in app.py, not a
bare module-level `app = Flask(...)`), so this file just calls that factory
-- production config, not the dev default -- and runs the same one-time
setup (ensure_schema, default users, ...) that app.py's own
`if __name__ == '__main__':` block already does via init_db() for local dev.

Requires these environment variables to be set on the host (cPanel's Setup
Python App page has an "Environment variables" section for this -- or drop
a real .env file next to this one; config.py loads it automatically either
way): DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, SECRET_KEY. See
.env.example in this same directory for the full list with placeholders.

If the app fails to start, the full traceback is appended to
startup_error.log next to this file. Shared hosting otherwise shows only a
generic error page and writes no application log at all.
"""
import datetime
import os
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# So `import app`, `import models`, etc. resolve regardless of whatever
# working directory Passenger starts this file from.
sys.path.insert(0, BASE_DIR)

def _database_hint(exc):
    """A plain-language DATABASE CHECK for the startup log when the failure is a
    MySQL access problem (1044 no permission / 1045 bad login / 1049 no such
    database / 2003 cannot reach the server). It says which user tried which
    database and, when the server accepts that user, which databases and
    privileges that user actually has -- exactly what a wrong DB_NAME spelling
    or a missing "add user to database" step looks like. Never prints the
    password, and never raises (the real traceback is written regardless)."""
    try:
        code = None
        cause = exc
        while cause is not None and code is None:
            args = getattr(cause, 'args', ())
            if args and isinstance(args[0], int):
                code = args[0]
            cause = getattr(cause, 'orig', None) or getattr(cause, '__cause__', None)
        if code not in (1044, 1045, 1049, 2003):
            return ''
        import pymysql
        from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
        lines = ['', 'DATABASE CHECK (added by passenger_wsgi.py):',
                 f'  The application tried to open database "{DB_NAME}" as MySQL user "{DB_USER}" '
                 f'on {DB_HOST}:{DB_PORT}  (MySQL error {code}).']
        if os.environ.get('DATABASE_URL'):
            lines.append('  NOTE: DATABASE_URL is set, and it overrides DB_HOST/DB_NAME/DB_USER/DB_PASSWORD.')
        try:
            conn = pymysql.connect(host=DB_HOST, port=int(DB_PORT), user=DB_USER,
                                   password=DB_PASSWORD, connect_timeout=8)
        except Exception as e:
            lines.append(f'  Connecting to the MySQL server itself (no database chosen) as "{DB_USER}" '
                         f'also failed: {e}')
            lines.append('  -> the MySQL user name, password or host is wrong, or that user does not exist.')
            return '\n'.join(lines)
        try:
            cur = conn.cursor()
            cur.execute('SHOW DATABASES')
            seen = sorted(r[0] for r in cur.fetchall() if r[0].lower() != 'information_schema')
            lines.append(f'  The server accepts "{DB_USER}". Databases it can see: {", ".join(seen) or "(none)"}')
            if DB_NAME in seen:
                lines.append(f'  "{DB_NAME}" IS in that list, so the name is right and the user can see it -- '
                             f'check the privileges below.')
            else:
                lines.append(f'  "{DB_NAME}" is NOT in that list: either no database exists with exactly this '
                             f'spelling (capital letters matter), or "{DB_USER}" has not been added to it with '
                             f'privileges (cPanel > MySQL Databases > Add User To Database > ALL PRIVILEGES).')
            try:
                cur.execute('SHOW GRANTS FOR CURRENT_USER()')
                import re
                for (grant,) in cur.fetchall():
                    grant = re.sub(r"IDENTIFIED BY (PASSWORD )?'[^']*'", "IDENTIFIED BY <hidden>", grant)
                    lines.append(f'  GRANT: {grant}')
            except Exception:
                pass
        finally:
            conn.close()
        return '\n'.join(lines)
    except Exception:
        return ''


try:
    from app import create_app, init_db

    for _folder in ('database', 'uploads', 'uploads/suppliers', 'uploads/buyers',
                    'static/uploads/purchase', 'static/uploads/sales'):
        os.makedirs(os.path.join(BASE_DIR, _folder), exist_ok=True)

    application = create_app('production')
    init_db(application)
except Exception as _exc:
    _hint = _database_hint(_exc)
    try:
        with open(os.path.join(BASE_DIR, 'startup_error.log'), 'a', encoding='utf-8') as _log:
            _log.write(f"\n--- {datetime.datetime.utcnow().isoformat()}Z ---\n")
            _log.write(_hint)
            _log.write('\n\n' + traceback.format_exc())
    except OSError:
        pass
    if _hint:
        # Passenger copies stderr into the host's error log (cPanel > Metrics >
        # Errors), which is where most people look first.
        print(_hint, file=sys.stderr, flush=True)
    raise
