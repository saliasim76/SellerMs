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

try:
    from app import create_app, init_db

    for _folder in ('database', 'uploads', 'uploads/suppliers', 'uploads/buyers',
                    'static/uploads/purchase', 'static/uploads/sales'):
        os.makedirs(os.path.join(BASE_DIR, _folder), exist_ok=True)

    application = create_app('production')
    init_db(application)
except Exception:
    try:
        with open(os.path.join(BASE_DIR, 'startup_error.log'), 'a', encoding='utf-8') as _log:
            _log.write(f"\n--- {datetime.datetime.utcnow().isoformat()}Z ---\n")
            _log.write(traceback.format_exc())
    except OSError:
        pass
    raise
