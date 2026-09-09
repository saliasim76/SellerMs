"""Lookups: allowances routes.

Split out of the original monolithic lookups.py.
Routes register on the shared lookups_bp, so endpoint names are unchanged
(url_for('lookups.list_buyers') etc. keep working).
"""
from flask import (render_template, request, jsonify,
                   redirect, url_for, flash, session)
from flask_login import login_required, current_user

from models import db, Employee, EmployeeAllowance, AllowanceType
from .lookups import lookups_bp, admin_required, _t


# ── EMPLOYEE ALLOWANCES ──────────────────────────────────────────────
@lookups_bp.route('/allowances')
@login_required
def list_allowances():
    employees = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()
    # Build allowances with employee names
    rows = []
    for emp in employees:
        for a in emp.allowance_rows.order_by(EmployeeAllowance.id).all():
            rows.append({'allowance': a, 'employee': emp})
    return render_template('employees/allowances.html', rows=rows, employees=employees)

@lookups_bp.route('/allowances/add', methods=['POST'])
@login_required
@admin_required
def add_allowance():
    emp_id           = request.form.get('employee_id','').strip()
    allowance_type_id= request.form.get('allowance_type_id','').strip()
    amount           = request.form.get('amount','0').strip()
    if not emp_id or not allowance_type_id:
        flash(_t('Employee and allowance type are required.',
                 'الموظف ونوع البدل مطلوبان.'),'danger')
        return redirect(url_for('lookups.list_allowances'))
    emp = Employee.query.get_or_404(int(emp_id))
    atype = AllowanceType.query.get_or_404(int(allowance_type_id))
    # Unique check
    existing = EmployeeAllowance.query.filter_by(employee_id=emp.id, allowance_type_id=atype.id).first()
    if existing:
        flash(_t(f'Allowance type "{atype.allowance_name_en}" already exists for this employee.',
                 f'نوع البدل "{atype.allowance_name_ar or atype.allowance_name_en}" موجود مسبقاً لهذا الموظف.'),'danger')
        return redirect(url_for('lookups.list_allowances'))
    try: amount = float(amount)
    except: amount = 0.0
    db.session.add(EmployeeAllowance(employee_id=emp.id, allowance_type_id=atype.id,
                                     name=atype.allowance_name_en, name_ar=atype.allowance_name_ar, amount=amount))
    _recalc(emp)
    db.session.commit()
    flash(_t(f'Allowance "{atype.allowance_name_en}" added.',
             f'تم إضافة البدل "{atype.allowance_name_ar or atype.allowance_name_en}".'),'success')
    return redirect(url_for('lookups.list_allowances'))

@lookups_bp.route('/allowances/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_allowance(id):
    a = EmployeeAllowance.query.get_or_404(id)
    a.name    = request.form.get('name', a.name).strip()
    a.name_ar = request.form.get('name_ar', a.name_ar or '').strip()
    try: a.amount = float(request.form.get('amount', a.amount))
    except: pass
    _recalc(a.employee)
    db.session.commit()
    flash(_t('Allowance updated.','تم تحديث البدل.'),'success')
    return redirect(url_for('lookups.list_allowances'))

@lookups_bp.route('/allowances/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_allowance(id):
    a = EmployeeAllowance.query.get_or_404(id)
    emp = a.employee
    db.session.delete(a)
    _recalc(emp)
    db.session.commit()
    flash(_t('Allowance deleted.','تم حذف البدل.'),'success')
    return redirect(url_for('lookups.list_allowances'))

@lookups_bp.route('/allowances/data')
@login_required
def allowances_data():
    lang = session.get('lang','en')
    rows = EmployeeAllowance.query.join(Employee).order_by(Employee.name, EmployeeAllowance.id).all()
    return jsonify([{
        'id': a.id,
        'employee_id': a.employee_id,
        'employee_name': (a.employee.name_ar if lang=='ar' and a.employee.name_ar else a.employee.name),
        'employee_code': a.employee.employee_code,
        'name': a.name_ar if lang=='ar' and a.name_ar else a.name,
        'name_en': a.name,
        'name_ar': a.name_ar or '',
        'amount': a.amount,
    } for a in rows])

def _recalc(emp):
    """Recalculate total_allowances and net_salary for an employee."""
    total = sum(a.amount for a in emp.allowance_rows.all())
    emp.total_allowances = total
    emp.net_salary = (emp.basic_salary or 0) + total