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
"""
import os
import sys

# So `import app`, `import models`, etc. resolve regardless of whatever
# working directory Passenger starts this file from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app, init_db

os.makedirs('database', exist_ok=True)
os.makedirs('uploads', exist_ok=True)
os.makedirs('uploads/suppliers', exist_ok=True)
os.makedirs('uploads/buyers', exist_ok=True)
os.makedirs('static/uploads/purchase', exist_ok=True)
os.makedirs('static/uploads/sales', exist_ok=True)

application = create_app('production')
init_db(application)
