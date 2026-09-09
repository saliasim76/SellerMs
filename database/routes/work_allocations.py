from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, session
from flask_login import login_required, current_user
from models import db, EmployeeWorkAllocation as WorkAllocation, Employee
from datetime import datetime, date
import calendar

wa_bp = Blueprint('work_allocations', __name__)

def _t(en, ar): return ar if session.get('lang') == 'ar' else en
def _last_wa(emp_id):
    """The employee's most recent allocation (department/company now live here)."""
    return (WorkAllocation.query
            .filter_by(employee_id=emp_id)
            .order_by(WorkAllocation.id.desc())
            .first())
def admin_required(f):
    from functools import wraps
    @wraps(f)
    def dec(*a, **kw):
        if not current_user.is_admin():
            flash(_t('Admin access required.','مطلوب صلاحية مدير.'),'danger')
            return redirect(url_for('work_allocations.list_wa'))
        return f(*a, **kw)
    return dec

@wa_bp.route('/work-allocations')
@login_required
def list_wa():
    return render_template('employees/work_allocation.html')

@wa_bp.route('/work-allocations/data')
@login_required
def wa_data():
    lang = session.get('lang', 'en')
    rows = WorkAllocation.query.order_by(WorkAllocation.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])

@wa_bp.route('/work-allocations/employees')
@login_required
def wa_employees():
    """Return active employees for dropdown"""
    lang = session.get('lang', 'en')
    emps = Employee.query.filter_by(is_active=True).order_by(Employee.employee_code).all()
    return jsonify([{
        'id': e.id,
        'employee_code': e.employee_code,
        'name': e.name,
        'name_ar': e.name_ar or '',
        'nationality': e.nationality or '',
        'passport_number': e.passport_number or '',
        'iqama_number': e.iqama_number or '',
        'profession': ', '.join([p.name_en for p in e.professions.all()]),
        'kafeel_name': e.kafeel_name or '',
        'kafeel_name_ar': e.kafeel_name_ar or '',
        # department now lives on employee_work_allocation (latest entry)
        'department': (_last_wa(e.id).buyer_department if _last_wa(e.id) else ''),
        'department_ar': (_last_wa(e.id).buyer_department_ar if _last_wa(e.id) else ''),
        # previous-record info: does this employee already have an allocation?
        'has_history': _last_wa(e.id) is not None,
        'last_wa_id': (_last_wa(e.id).id if _last_wa(e.id) else None),
        'last_end_date': (_last_wa(e.id).end_date.strftime('%Y-%m-%d')
                          if _last_wa(e.id) and _last_wa(e.id).end_date else ''),
        'label': f"{e.employee_code} — {e.name_ar if lang=='ar' and e.name_ar else e.name}",
    } for e in emps])

@wa_bp.route('/work-allocations/add', methods=['POST'])
@login_required
@admin_required
def add_wa():
    f = request.form
    # Support multiple employee IDs
    employee_ids = f.getlist('employee_id[]') or ([f.get('employee_id')] if f.get('employee_id') else [])
    employee_ids = [int(x) for x in employee_ids if x and str(x).isdigit()]
    if not employee_ids:
        flash(_t('At least one employee required.','مطلوب موظف واحد على الأقل.'),'danger')
        return redirect(url_for('work_allocations.list_wa'))
    def parse_date(val):
        if not val: return None
        try: return datetime.strptime(val, '%Y-%m-%d').date()
        except: return None
    from models import BuyerMaster
    _bid = f.get('buyer_id', type=int)
    _buyer = BuyerMaster.query.get(_bid) if _bid else None

    new_joining = parse_date(f.get('joining_date'))
    mode = (f.get('wa_mode') or 'new').strip().lower()

    with_history = [eid for eid in employee_ids if _last_wa(eid) is not None]

    if mode == 'old':
        # Old mode: exactly ONE employee, and it must already have history.
        if len(employee_ids) != 1:
            flash(_t('Old Employee mode: add one employee at a time.',
                     'وضع الموظف القديم: أضف موظفاً واحداً في كل مرة.'), 'danger')
            return redirect(url_for('work_allocations.list_wa'))
        if not with_history:
            flash(_t('Selected employee has no previous record for Old Employee mode.',
                     'الموظف المحدد ليس له سجل سابق.'), 'danger')
            return redirect(url_for('work_allocations.list_wa'))
        prev_end = parse_date(f.get('prev_end_date'))
        prev_wa_id = f.get('prev_wa_id', type=int)
        if prev_end and new_joining and prev_end > new_joining:
            flash(_t('Previous record end date cannot be later than the new joining date.',
                     'لا يمكن أن يكون تاريخ انتهاء السجل السابق بعد تاريخ الانضمام الجديد.'), 'danger')
            return redirect(url_for('work_allocations.list_wa'))
        prev = (WorkAllocation.query.get(prev_wa_id) if prev_wa_id
                else _last_wa(employee_ids[0]))
        if prev and prev_end:
            prev.end_date = prev_end
    else:
        # New mode: every selected employee must be new (no history).
        if with_history:
            flash(_t('New Employee mode: those employees already have a work allocation.',
                     'وضع الموظف الجديد: هؤلاء الموظفون لديهم توزيع عمل بالفعل.'), 'danger')
            return redirect(url_for('work_allocations.list_wa'))

    added = 0
    for emp_id in employee_ids:
        emp = Employee.query.get(emp_id)
        shift = (f.get('shift_type') or f.get('shift') or 'day').strip().lower()
        if shift not in ('day', 'night'):
            shift = 'day'
        wa = WorkAllocation(
            employee_id=emp_id,
            buyer_id=f.get('buyer_id', type=int),
            status='active',
            # company -> buyer_name, section -> location, shift_type -> shift
            buyer_name=(f.get('company','').strip() or (_buyer.buyer_name_en if _buyer else '')),
            buyer_name_ar=(f.get('company_ar','').strip() or ((_buyer.buyer_name_ar or '') if _buyer else '')),
            buyer_department=f.get('department','').strip(),
            buyer_department_ar=f.get('department_ar','').strip(),
            location=f.get('section','').strip() or f.get('location','').strip(),
            location_ar=f.get('section_ar','').strip() or f.get('location_ar','').strip(),
            shift=shift,
            joining_date=new_joining,
            end_date=None,  # new record always stays active
            name=emp.name if emp else '',
            nationality=emp.nationality if emp else '',
            profession=(', '.join([p.name_en for p in emp.professions.all()]) if emp else ''),
            iqama=emp.iqama_number if emp else '',
            kafeel=emp.kafeel_name if emp else '',
            created_by=current_user.id,
        )
        db.session.add(wa)
        added += 1
    db.session.commit()
    flash(_t(f'{added} work allocation(s) added.',f'تم إضافة {added} توزيع/توزيعات عمل.'),'success')
    return redirect(url_for('work_allocations.list_wa'))

@wa_bp.route('/work-allocations/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_wa(id):
    wa = WorkAllocation.query.get_or_404(id)
    f = request.form
    def parse_date(val):
        if not val: return None
        try: return datetime.strptime(val, '%Y-%m-%d').date()
        except: return None
    wa.status              = f.get('status', wa.status)
    wa.buyer_name          = f.get('company', wa.buyer_name or '').strip()
    wa.buyer_name_ar       = f.get('company_ar', wa.buyer_name_ar or '').strip()
    wa.buyer_department    = f.get('department', wa.buyer_department or '').strip()
    wa.buyer_department_ar = f.get('department_ar', wa.buyer_department_ar or '').strip()
    wa.location            = f.get('section', wa.location or '').strip()
    wa.location_ar         = f.get('section_ar', wa.location_ar or '').strip()
    _sh = (f.get('shift_type') or wa.shift or 'day').strip().lower()
    wa.shift               = _sh if _sh in ('day', 'night') else 'day'
    wa.joining_date        = parse_date(f.get('joining_date'))
    wa.end_date            = parse_date(f.get('end_date'))
    buyer_id = f.get('buyer_id', type=int)
    if buyer_id: wa.buyer_id = buyer_id
    # Batch edit extra employees with same details
    extra_ids = [int(x) for x in f.getlist('extra_employee_id[]') if x and str(x).isdigit()]
    for eid in extra_ids:
        _e = Employee.query.get(eid)
        extra = WorkAllocation(
            employee_id=eid,
            buyer_id=buyer_id,
            status=wa.status,
            buyer_name=wa.buyer_name, buyer_name_ar=wa.buyer_name_ar,
            buyer_department=wa.buyer_department,
            buyer_department_ar=wa.buyer_department_ar,
            location=wa.location, location_ar=wa.location_ar,
            shift=wa.shift,
            joining_date=wa.joining_date, end_date=wa.end_date,
            name=_e.name if _e else '',
            nationality=_e.nationality if _e else '',
            iqama=_e.iqama_number if _e else '',
            kafeel=_e.kafeel_name if _e else '',
            created_by=current_user.id,
        )
        db.session.add(extra)
    db.session.commit()
    flash(_t('Work allocation updated.','تم تحديث توزيع العمل.'),'success')
    return redirect(url_for('work_allocations.list_wa'))

@wa_bp.route('/work-allocations/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_wa(id):
    wa = WorkAllocation.query.get_or_404(id)
    db.session.delete(wa)
    db.session.commit()
    flash(_t('Work allocation deleted.','تم حذف توزيع العمل.'),'success')
    return redirect(url_for('work_allocations.list_wa'))

@wa_bp.route('/work-allocations/bulk-delete', methods=['POST'])
@login_required
def bulk_delete_wa():
    """Delete every selected row from the grid's multi-select checkboxes in
    one request, instead of one confirm+redirect per row. Kept as a plain
    @login_required route (not @admin_required) because that decorator
    flashes+redirects on failure -- an HTML response the AJAX caller can't
    parse as JSON -- so the admin check here returns JSON directly instead."""
    if not current_user.is_admin():
        return jsonify({'ok': False, 'error': _t('Admin access required.', 'مطلوب صلاحية مدير.')}), 403
    ids = request.form.getlist('ids[]', type=int)
    if not ids:
        return jsonify({'ok': False, 'error': _t('No records selected.', 'لم يتم تحديد أي سجل.')}), 400
    try:
        count = WorkAllocation.query.filter(WorkAllocation.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, 'deleted': count})

@wa_bp.route('/work-allocations/<int:id>/json')
@login_required
def wa_json(id):
    wa = WorkAllocation.query.get_or_404(id)
    return jsonify(wa.to_dict())