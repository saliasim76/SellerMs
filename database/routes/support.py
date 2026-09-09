"""Customer Support: a SaaS tenant submits a ticket TO Proledg, and
Proledg's own team (Super Admin) sees every tenant's tickets in one queue.

Tickets live in the shared 'saas' database (SupportTicket, see models.py),
not inside the tenant's own database -- mirrors how Customer/Subscription
already work, so this is genuinely "going to Proledg" rather than staying
trapped inside one tenant's isolated data.
"""
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required, current_user

from models import db, SupportTicket, SupportTicketMessage, next_ticket_no
from database.routes.shared import _t, super_admin_required, current_tenant_customer

support_bp = Blueprint('support', __name__)

CATEGORIES = [
    'inquiry', 'complaint', 'billing', 'technical', 'account',
    'package_update', 'renewal', 'feature_request', 'reporting',
]
CATEGORY_LABELS = {
    'inquiry':         ('General Inquiry', 'استفسار عام'),
    'complaint':       ('Complaint', 'شكوى'),
    'billing':         ('Billing & Invoice Issues', 'مشاكل الفواتير والدفع'),
    'technical':       ('Technical Support', 'الدعم الفني'),
    'account':         ('Account Management', 'إدارة الحساب'),
    'package_update':  ('Package Update', 'تحديث الباقة'),
    'renewal':         ('Renewal Process', 'عملية التجديد'),
    'feature_request': ('Feature Request', 'طلب ميزة'),
    'reporting':       ('Data & Reporting Issues', 'مشاكل البيانات والتقارير'),
}
PRIORITIES = ['low', 'medium', 'high', 'urgent']
PRIORITY_LABELS = {
    'low':    ('Low', 'منخفضة'),
    'medium': ('Medium - Standard', 'متوسطة - عادية'),
    'high':   ('High', 'عالية'),
    'urgent': ('Urgent', 'عاجلة'),
}
STATUSES = ['open', 'in_progress', 'resolved', 'closed']


# ══════════════════════════════════════════════════════════════════
# TENANT-FACING: submit a ticket, view "My Tickets" (this tenant's own)
# ══════════════════════════════════════════════════════════════════

@support_bp.route('/support')
@login_required
def customer_support():
    customer = current_tenant_customer()
    return render_template('support/customer_support.html',
                           customer=customer, categories=CATEGORIES,
                           category_labels=CATEGORY_LABELS,
                           priority_labels=PRIORITY_LABELS)


@support_bp.route('/support/faq')
@login_required
def faq():
    return render_template('support/faq.html')


@support_bp.route('/support/tickets/data')
@login_required
def support_tickets_data():
    customer = current_tenant_customer()
    if not customer:
        return jsonify([])
    rows = (SupportTicket.query.filter_by(customer_id=customer.id)
            .order_by(SupportTicket.created_at.desc()).all())
    return jsonify([r.to_dict() for r in rows])


@support_bp.route('/support/tickets/add', methods=['POST'])
@login_required
def add_support_ticket():
    customer = current_tenant_customer()
    if not customer:
        return jsonify({'ok': False, 'error': _t(
            'Support tickets are only available for a provisioned SaaS account.',
            'تذاكر الدعم متاحة فقط لحساب SaaS تم تزويده.')}), 400

    f = request.form
    full_name = (f.get('full_name') or '').strip()
    email = (f.get('email') or '').strip()
    category = (f.get('category') or '').strip()
    message = (f.get('message') or '').strip()
    priority = (f.get('priority') or 'medium').strip()

    if not full_name or not email or not category or not message:
        return jsonify({'ok': False, 'error': _t(
            'Full name, email, category and message are required.',
            'الاسم الكامل والبريد الإلكتروني والفئة والرسالة مطلوبة.')}), 400
    if category not in CATEGORIES:
        return jsonify({'ok': False, 'error': _t('Invalid category.', 'فئة غير صالحة.')}), 400
    if priority not in PRIORITIES:
        priority = 'medium'

    ticket = SupportTicket(
        ticket_no=next_ticket_no(),
        customer_id=customer.id,
        submitted_by_user_id=current_user.id,
        full_name=full_name, email=email,
        phone=(f.get('phone') or '').strip() or None,
        category=category, priority=priority, message=message,
    )
    db.session.add(ticket)
    db.session.commit()
    return jsonify({'ok': True, 'ticket_no': ticket.ticket_no})


@support_bp.route('/support/tickets/<int:id>/messages')
@login_required
def support_ticket_messages(id):
    """The two-way conversation for one of THIS tenant's own tickets --
    scoped to customer_id so a tenant can never read another tenant's
    thread by guessing a ticket id."""
    customer = current_tenant_customer()
    ticket = SupportTicket.query.filter_by(id=id, customer_id=customer.id if customer else -1).first_or_404()
    rows = SupportTicketMessage.query.filter_by(ticket_id=ticket.id).order_by(SupportTicketMessage.created_at).all()
    return jsonify([m.to_dict() for m in rows])


@support_bp.route('/support/tickets/<int:id>/messages/add', methods=['POST'])
@login_required
def add_support_ticket_message(id):
    customer = current_tenant_customer()
    ticket = SupportTicket.query.filter_by(id=id, customer_id=customer.id if customer else -1).first_or_404()
    message = (request.form.get('message') or '').strip()
    if not message:
        return jsonify({'ok': False, 'error': _t('Message cannot be empty.', 'لا يمكن أن تكون الرسالة فارغة.')}), 400
    msg = SupportTicketMessage(
        ticket_id=ticket.id, sender_type='customer',
        sender_name=current_user.full_name or current_user.username,
        message=message,
    )
    db.session.add(msg)
    ticket.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'message': msg.to_dict()})


# ══════════════════════════════════════════════════════════════════
# SUPER ADMIN: queue of every tenant's tickets
# ══════════════════════════════════════════════════════════════════

@support_bp.route('/saas-admin/support-tickets')
@login_required
@super_admin_required
def support_tickets_queue():
    return render_template('saas_admin/support_tickets.html')


@support_bp.route('/saas-admin/support-tickets/data')
@login_required
@super_admin_required
def support_tickets_queue_data():
    q = SupportTicket.query
    status = request.args.get('status')
    category = request.args.get('category')
    customer_id = request.args.get('customer_id', type=int)
    if status:
        q = q.filter(SupportTicket.status == status)
    if category:
        q = q.filter(SupportTicket.category == category)
    if customer_id:
        q = q.filter(SupportTicket.customer_id == customer_id)
    rows = q.order_by(SupportTicket.created_at.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@support_bp.route('/saas-admin/support-tickets/<int:id>/status', methods=['POST'])
@login_required
@super_admin_required
def update_support_ticket_status(id):
    ticket = SupportTicket.query.get_or_404(id)
    status = (request.form.get('status') or '').strip()
    if status not in STATUSES:
        return jsonify({'ok': False, 'error': _t('Invalid status.', 'حالة غير صالحة.')}), 400
    ticket.status = status
    notes = request.form.get('admin_notes')
    if notes is not None:
        ticket.admin_notes = notes.strip() or None
    ticket.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@support_bp.route('/saas-admin/support-tickets/<int:id>/messages')
@login_required
@super_admin_required
def support_ticket_messages_admin(id):
    SupportTicket.query.get_or_404(id)
    rows = SupportTicketMessage.query.filter_by(ticket_id=id).order_by(SupportTicketMessage.created_at).all()
    return jsonify([m.to_dict() for m in rows])


@support_bp.route('/saas-admin/support-tickets/<int:id>/messages/add', methods=['POST'])
@login_required
@super_admin_required
def add_support_ticket_message_admin(id):
    ticket = SupportTicket.query.get_or_404(id)
    message = (request.form.get('message') or '').strip()
    if not message:
        return jsonify({'ok': False, 'error': _t('Message cannot be empty.', 'لا يمكن أن تكون الرسالة فارغة.')}), 400
    msg = SupportTicketMessage(
        ticket_id=ticket.id, sender_type='proledg',
        sender_name=current_user.full_name or current_user.username,
        message=message,
    )
    db.session.add(msg)
    ticket.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'message': msg.to_dict()})
