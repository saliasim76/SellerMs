from flask import Blueprint, render_template
from flask_login import login_required
from models import db, Owner, OwnerDocument
from datetime import datetime, timedelta
from sqlalchemy import func

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/dashboard')
@login_required
def index():
    total_owners = Owner.query.count()
    active_owners = Owner.query.filter_by(status='active').count()
    inactive_owners = Owner.query.filter_by(status='inactive').count()

    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
    new_this_month = Owner.query.filter(Owner.created_at >= month_start).count()

    expiry_threshold = datetime.utcnow().date() + timedelta(days=30)
    expiring_docs = OwnerDocument.query.filter(
        OwnerDocument.expiry_date != None,
        OwnerDocument.expiry_date <= expiry_threshold,
        OwnerDocument.expiry_date >= datetime.utcnow().date()
    ).count()

    recent_owners = Owner.query.order_by(Owner.created_at.desc()).limit(8).all()

    stats = {
        'total': total_owners,
        'active': active_owners,
        'inactive': inactive_owners,
        'new_month': new_this_month,
        'expiring_docs': expiring_docs,
    }
    return render_template('dashboard/index.html', stats=stats, recent_owners=recent_owners)