"""Trial-user ERP login provisioning.

Phase 2 originally exposed this as a secret-key-protected HTTP endpoint,
since Proledg and SellerMs were separate processes. Phase 4 merged them
into one application/process, so the network hop is gone -- Proledg's
trial-signup view (database/routes/proledg_site.py) now calls
provision_trial_user_direct() directly.
"""
import re

from models import db, User


def _unique_username(base, exists_fn=None):
    """Generate a username from the local part of an email, suffixed until
    unique. `exists_fn(candidate) -> bool` defaults to checking the local
    User table; Phase 3's tenant provisioning passes a check against the
    cross-tenant SaasUserDirectory instead, since a brand-new tenant
    database's own users table is always empty."""
    exists_fn = exists_fn or (lambda name: User.query.filter_by(username=name).first() is not None)
    base = re.sub(r'[^a-zA-Z0-9_.]', '', base.split('@')[0].lower()) or 'user'
    candidate = base
    n = 1
    while exists_fn(candidate):
        n += 1
        candidate = f'{base}{n}'
    return candidate


def provision_trial_user_direct(email, full_name, mobile, password):
    """Create a trial user's ERP login. Returns {'username': ...} on
    success; raises ValueError with a user-facing message on failure."""
    email = (email or '').strip().lower()
    full_name = (full_name or '').strip()
    mobile = (mobile or '').strip()
    if not email or not password:
        raise ValueError('email and password are required')
    if User.query.filter_by(email=email).first():
        raise ValueError('a user with this email already exists')

    username = _unique_username(email)
    user = User(
        username=username, email=email, role='admin', is_active=True,
        full_name=full_name or None, mobile=mobile or None,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return {'username': username}
