import os, uuid
from datetime import datetime, date
from decimal import Decimal
from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, current_app, jsonify, session)
from flask_login import login_required, current_user
from models import (db, Employee, EmployeeAllowance, AllowanceType,
                    EmployeeBank, EmployeeDocument, ProfessionMaster, BuyerMaster,
                    EmployeeProfession, User)


def _emp_profession_str(e, ar=False):
    """Comma-joined profession names from the employee_professions junction."""
    try:
        profs = e.professions.all()
    except Exception:
        profs = []
    names = [((p.name_ar or p.name_en) if ar else p.name_en) for p in profs]
    return ', '.join([n for n in names if n])
from functools import wraps

employees_bp = Blueprint('employees', __name__)

def _t(en, ar): return ar if session.get('lang') == 'ar' else en

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash(_t('Access denied.', 'الوصول مرفوض'), 'danger')
            return redirect(url_for('employees.list_employees'))
        return f(*args, **kwargs)
    return decorated

def generate_code():
    """Next EMP-#### derived from the highest existing employee_code suffix
    -- NOT from Employee.id, which skips ahead whenever an earlier employee
    was deleted (e.g. last code EMP-0047 but id already at 174 would have
    produced EMP-0175 here)."""
    max_num = 0
    for (code,) in db.session.query(Employee.employee_code).all():
        if code and code.upper().startswith('EMP-'):
            try:
                n = int(code.split('-', 1)[1])
                if n > max_num:
                    max_num = n
            except (ValueError, IndexError):
                continue
    num = max_num + 1
    code = f'EMP-{num:04d}'
    retries = 0
    while Employee.query.filter_by(employee_code=code).first() and retries < 100:
        num += 1
        code = f'EMP-{num:04d}'
        retries += 1
    return code

def parse_date(v):
    if not v: return None
    try: return datetime.strptime(v, '%Y-%m-%d').date()
    except (ValueError, TypeError): return None

def save_upload(file, emp_id, subfolder='employees'):
    if not file or not file.filename: return None
    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in {'pdf', 'jpg', 'jpeg', 'png'}: return None
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], subfolder, str(emp_id))
    os.makedirs(folder, exist_ok=True)
    fname = f'{uuid.uuid4().hex}.{ext}'
    file.save(os.path.join(folder, fname))
    return os.path.join(subfolder, str(emp_id), fname)


def _photo_abs_path(emp):
    """Absolute path of an employee's photo, tolerating either slash style."""
    if not emp or not emp.photo_path:
        return None
    parts = emp.photo_path.replace('\\', '/').split('/')
    return os.path.join(current_app.config['UPLOAD_FOLDER'], *parts)


def _photo_url(emp):
    """Return a cache-busted photo URL, or '' when there is no usable file.

    Returning '' for a missing/broken path lets the UI fall back to the
    placeholder instead of rendering a broken <img>.
    """
    full = _photo_abs_path(emp)
    if not full or not os.path.exists(full):
        return ''
    try:
        stamp = int(os.path.getmtime(full))
    except OSError:
        stamp = 0
    return url_for('employees.employee_photo', emp_id=emp.id) + f'?v={stamp}'


# Extensions we refuse outright, regardless of content. Everything else is
# decided by *reading* the bytes: if Pillow can open it as an image, we take
# it. That way new formats (.jfif, .avif, .heic, ...) never need a code change.
BLOCKED_PHOTO_EXT = {
    'exe', 'dll', 'bat', 'cmd', 'com', 'scr', 'msi', 'ps1', 'sh',
    'js', 'jar', 'vbs', 'php', 'py', 'html', 'htm', 'svg',
}

# Formats every browser renders natively — anything else gets converted to JPEG.
BROWSER_SAFE_FORMATS = {'JPEG', 'PNG', 'GIF', 'WEBP'}

# Pillow format -> file extension to store on disk.
FORMAT_EXT = {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp'}

MAX_PHOTO_BYTES = 10 * 1024 * 1024   # 10 MB


def save_photo(emp_id, req):
    """Save the employee photo. Updates employees.photo_path.

    The file is accepted or rejected by *content*, not by filename: whatever
    Pillow can decode as an image is allowed. Formats browsers cannot render
    reliably (AVIF, HEIC, TIFF, BMP, ...) are converted to JPEG, so `.jfif`,
    `.avif` and friends all just work.

    The stored path always uses forward slashes so it is portable between
    Windows and POSIX hosts. Every skip path is logged.
    """
    log = current_app.logger

    f = req.files.get('photo')
    if f is None:
        log.info('[photo] emp=%s: no "photo" key in request.files (keys=%s). '
                 'Is the form enctype="multipart/form-data" and the input name="photo"?',
                 emp_id, list(req.files.keys()))
        return
    if not f.filename:
        log.info('[photo] emp=%s: "photo" field present but empty (no file chosen).', emp_id)
        return

    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext in BLOCKED_PHOTO_EXT:
        log.warning('[photo] emp=%s: rejected "%s" (blocked extension %r).',
                    emp_id, f.filename, ext)
        flash(_t(f'"{f.filename}" is not an image file.',
                 f'"{f.filename}" ليس ملف صورة.'), 'warning')
        return

    try:
        raw = f.read()
        if not raw:
            log.warning('[photo] emp=%s: "%s" is empty (0 bytes).', emp_id, f.filename)
            flash(_t('The selected image is empty.', 'الصورة المختارة فارغة.'), 'warning')
            return
        if len(raw) > MAX_PHOTO_BYTES:
            log.warning('[photo] emp=%s: "%s" too large (%d bytes).',
                        emp_id, f.filename, len(raw))
            flash(_t('The image is larger than 10 MB.',
                     'حجم الصورة أكبر من 10 ميغابايت.'), 'warning')
            return

        final_ext, data = _normalise_image(raw, emp_id, f.filename)
        if data is None:
            return  # _normalise_image already logged + flashed

        folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'employees', str(emp_id))
        os.makedirs(folder, exist_ok=True)
        fname = f'photo_{uuid.uuid4().hex}.{final_ext}'
        dest = os.path.join(folder, fname)
        with open(dest, 'wb') as out:
            out.write(data)

        if not os.path.exists(dest) or os.path.getsize(dest) == 0:
            log.error('[photo] emp=%s: file did not land at %s', emp_id, dest)
            flash(_t('The photo could not be written to disk.',
                     'تعذر حفظ الصورة على القرص.'), 'danger')
            return
    except Exception as exc:  # noqa: BLE001
        log.exception('[photo] emp=%s: save failed: %s', emp_id, exc)
        flash(_t('The photo could not be saved.', 'تعذر حفظ الصورة.'), 'danger')
        return

    emp = Employee.query.get(emp_id)
    if not emp:
        log.error('[photo] emp=%s: employee row not found after upload.', emp_id)
        return

    # Remove the previous photo file so old images don't pile up.
    if emp.photo_path:
        old = os.path.join(current_app.config['UPLOAD_FOLDER'],
                           *emp.photo_path.replace('\\', '/').split('/'))
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass

    emp.photo_path = f'employees/{emp_id}/{fname}'   # always forward slashes
    log.info('[photo] emp=%s: saved -> %s (%d bytes)',
             emp_id, emp.photo_path, os.path.getsize(dest))


def _normalise_image(raw, emp_id, filename):
    """Validate image bytes and return ``(extension, bytes)`` ready to write.

    Decides purely on content. Browser-safe formats pass through untouched;
    everything else Pillow can decode is re-encoded as JPEG. Returns
    ``(None, None)`` when the bytes are not a readable image.
    """
    import io
    log = current_app.logger

    try:
        from PIL import Image
    except ImportError:
        log.error('[photo] emp=%s: Pillow is not installed; cannot validate images.', emp_id)
        flash(_t('Image support is not installed on the server.',
                 'دعم الصور غير مثبت على الخادم.'), 'danger')
        return None, None

    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()                       # cheap integrity check
        img = Image.open(io.BytesIO(raw))    # re-open: verify() exhausts it
        fmt = (img.format or '').upper()
    except Exception as exc:  # noqa: BLE001
        log.warning('[photo] emp=%s: "%s" is not a readable image (%s).',
                    emp_id, filename, exc)
        flash(_t(f'"{filename}" is not a readable image file.',
                 f'"{filename}" ليس ملف صورة صالح.'), 'warning')
        return None, None

    # Already displayable in every browser -> store the original bytes.
    if fmt in BROWSER_SAFE_FORMATS:
        log.info('[photo] emp=%s: accepted %s (%s).', emp_id, filename, fmt)
        return FORMAT_EXT[fmt], raw

    # Anything else (AVIF, HEIC, TIFF, BMP, ICO, ...) -> convert to JPEG.
    try:
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=90, optimize=True)
        log.info('[photo] emp=%s: converted %s (%s) -> jpg', emp_id, filename, fmt or '?')
        return 'jpg', buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.warning('[photo] emp=%s: could not convert %s (%s) to JPEG: %s.',
                    emp_id, filename, fmt or '?', exc)
        flash(_t('This image could not be converted. Try a JPG or PNG.',
                 'تعذر تحويل الصورة. جرّب JPG أو PNG.'), 'warning')
        return None, None


def save_documents(emp_id, req):
    files = req.files.getlist('documents[]')
    types = req.form.getlist('document_type[]')
    emp = Employee.query.get(emp_id)
    for i, f in enumerate(files):
        if not f or not f.filename:
            continue
        path = save_upload(f, emp_id)
        if not path:
            continue
        dtype = types[i] if i < len(types) else ''
        db.session.add(EmployeeDocument(
            employee_id=emp_id,
            document_type=(dtype or '').strip(),
            file_path=path,
            original_name=f.filename,
            uploaded_by=current_user.id,
            employee_code=emp.employee_code if emp else None,
            employee_name=emp.name if emp else None,
            passport_number=emp.passport_number if emp else None,
            iqama_number=emp.iqama_number if emp else None,
        ))


def save_professions(emp_id, req):
    """Save multiple professions from profession_ids form field.
    Stores each profession's name (EN + AR) in employee_professions,
    and mirrors the first selected profession into the employee's single
    profession_id / profession / profession_ar columns (grid & view use them)."""
    from models import ProfessionMaster, Employee
    EmployeeProfession.query.filter_by(employee_id=emp_id).delete()
    prof_ids = req.form.getlist('profession_ids')
    clean_ids = [int(p) for p in prof_ids if p and str(p).strip()]
    for pid in clean_ids:
        pm = ProfessionMaster.query.get(pid)
        db.session.add(EmployeeProfession(
            employee_id=emp_id,
            profession_id=pid,
            profession_name=(pm.name_en if pm else None),
            profession_name_ar=(pm.name_ar if pm else None),
        ))
    # professions are stored only in employee_professions (junction table)


TEXT_FIELDS = [
    'name', 'name_ar', 'kafeel_name', 'kafeel_name_ar', 'kafeel_reference', 'kafeel_reference_ar',
    'nationality', 'nationality_ar', 'passport_number', 'entry_number', 'iqama_number',
    'education', 'education_ar',
    'mobile', 'address', 'address_ar', 'email', 'home_city', 'home_city_ar',
    'employee_reference', 'employee_reference_ar',
    'po_number', 'salary_type', 'salary_category', 'kafalat_number',
    'hostel_name', 'hostel_name_ar', 'room_number',
    'hostel_location', 'hostel_location_ar',
    'crn', 'crn_ar', 'insurance_company', 'insurance_company_ar', 'labour_office',
    'passport_location', 'blood_group',
    'levelfive_code', 'levelfive_drawer',
]
FLOAT_FIELDS = ['po_rate', 'po_ot_rate', 'services_charges', 'basic_salary', 'working_hours', 'overtime_ratio']
DATE_FIELDS  = ['arrival_date', 'birth_date', 'passport_expiry', 'iqama_expiry',
                'joining_date', 'insurance_expiry', 'end_date_work']

def latest_work_allocation(emp_id):
    """The employee's most recent allocation row (read-only link to the
    separate Work Allocation module — company/department/location/shift for
    an employee are entered and maintained there, not on the Employee form).
    """
    from models import EmployeeWorkAllocation
    return (EmployeeWorkAllocation.query
            .filter_by(employee_id=emp_id)
            .order_by(EmployeeWorkAllocation.id.desc())
            .first())

def _wa_department(e, ar=False):
    """Department name for the grid, taken from the employee's latest allocation.

    The department columns were removed from `employees`; department/company/
    location now live on employee_work_allocation only.
    """
    wa = latest_work_allocation(e.id)
    if not wa:
        return ''
    return (wa.buyer_department_ar if ar and wa.buyer_department_ar
            else wa.buyer_department) or ''

def _to_float(v, default=0.0):
    try: return float(v) if v not in (None, '') else default
    except (ValueError, TypeError): return default

# Fields where a blank submission should fall back to a sensible default
# rather than 0 -- a Working Hours or Overtime Ratio of 0 silently zeroes
# out Payroll's Working Hour calculation, which is never actually intended.
# Still fully editable to any other value; this only covers "left blank."
FLOAT_FIELD_DEFAULTS = {'working_hours': 8.0, 'overtime_ratio': 1.5}

def bind_employee(emp, f):
    for field in TEXT_FIELDS:
        setattr(emp, field, (f.get(field, '') or '').strip())
    for field in FLOAT_FIELDS:
        setattr(emp, field, _to_float(f.get(field), FLOAT_FIELD_DEFAULTS.get(field, 0.0)))
    for field in DATE_FIELDS:
        setattr(emp, field, parse_date(f.get(field)))

    emp.is_active = f.get('is_active') == 'on'
    emp.is_muslim = f.get('is_muslim') == 'on'
    emp.auto_code = f.get('auto_code') == 'on'

    # Department/company/location now live only on employee_work_allocation.

    emp.overtime_rate = _calc_overtime_rate(emp)
    emp.net_salary = _to_decimal(emp.basic_salary) + _to_decimal(emp.total_allowances)


def _to_decimal(value):
    """Coerce a form float / DB Decimal / None into a Decimal.

    Form fields arrive as floats while columns already loaded from the DB are
    Decimals, and Python refuses to add the two. Normalising here keeps every
    money calculation on a single numeric type.
    """
    if value is None or value == '':
        return Decimal('0')
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _calc_overtime_rate(emp):
    """Overtime rate = (basic / 30 / 8) * overtime ratio.

    i.e. the hourly rate (monthly basic over 30 days, 8 hours a day)
    multiplied by the overtime ratio.
    """
    basic = float(emp.basic_salary or 0)
    ratio = float(emp.overtime_ratio or 0)
    if basic <= 0 or ratio <= 0:
        return 0
    return round(basic / 30 / 8 * ratio, 2)

def save_allowances(emp_id, f):
    EmployeeAllowance.query.filter_by(employee_id=emp_id).delete()
    type_ids = f.getlist('allow_type_id[]')
    amounts  = f.getlist('allow_amount[]')
    for type_id, amt in zip(type_ids, amounts):
        if not type_id:
            continue
        atype = AllowanceType.query.get(int(type_id))
        if not atype:
            continue
        db.session.add(EmployeeAllowance(
            employee_id=emp_id,
            allowance_type_id=atype.id,
            allowance_code=atype.allowance_code,
            name=atype.allowance_name_en,
            name_ar=atype.allowance_name_ar,
            amount=_to_float(amt),
        ))

def save_banks_from_form(emp_id, f):
    banks = {}
    for key in f:
        if key.startswith('banks['):
            rest = key[6:]
            close = rest.index(']')
            idx = rest[:close]
            field = rest[close+2:-1]
            banks.setdefault(idx, {})[field] = f[key]

    EmployeeBank.query.filter_by(employee_id=emp_id).delete()
    made_primary = False
    for idx in sorted(banks, key=lambda x: int(x) if x.isdigit() else 0):
        d = banks[idx]
        name = (d.get('bank_name') or '').strip()
        if not name:
            continue
        is_primary = str(d.get('is_primary', '')).lower() in ('1', 'true', 'on', 'yes')
        if is_primary and made_primary:
            is_primary = False
        if is_primary:
            made_primary = True
        db.session.add(EmployeeBank(
            employee_id=emp_id,
            bank_name=name,
            bank_name_ar=(d.get('bank_name_ar') or '').strip(),
            branch=(d.get('branch') or '').strip(),
            branch_ar=(d.get('branch_ar') or '').strip(),
            account_number=(d.get('account_number') or '').strip(),
            swift_code=(d.get('swift_code') or '').strip(),
            iban=(d.get('iban') or '').strip(),
            is_primary=is_primary,
        ))


def _recalc_totals(emp_id):
    emp = Employee.query.get(emp_id)
    if not emp:
        return
    total = sum((_to_decimal(a.amount) for a in emp.allowance_rows.all()), Decimal('0'))
    emp.total_allowances = total
    emp.net_salary = _to_decimal(emp.basic_salary) + total


# ═══════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════

@employees_bp.route('/employees')
@login_required
def list_employees():
    return render_template('employees/list.html')

@employees_bp.route('/employees/data')
@login_required
def employees_data():
    lang = session.get('lang', 'en'); ar = lang == 'ar'
    emps = Employee.query.order_by(Employee.created_at.desc()).all()

    # Batch-load professions for every employee in one query instead of one
    # query per row (e.professions.all() per employee). Against SQL Server
    # the per-round-trip latency of that N+1 pattern was slow/large enough
    # for the grid's fetch to time out on bigger employee counts, so rows
    # near the end of the list would silently never arrive in the browser.
    emp_ids = [e.id for e in emps]
    prof_by_emp = {}
    if emp_ids:
        for p in (EmployeeProfession.query
                  .filter(EmployeeProfession.employee_id.in_(emp_ids)).all()):
            prof_by_emp.setdefault(p.employee_id, []).append(p)

    def profession_str(emp_id):
        names = [((p.profession_name_ar or p.profession_name) if ar else p.profession_name)
                 for p in prof_by_emp.get(emp_id, [])]
        return ', '.join([n for n in names if n])

    def d(v):
        return v.strftime('%Y-%m-%d') if v else ''

    rows = []
    for e in emps:
        age = ''
        if e.birth_date:
            today = date.today()
            y = today.year - e.birth_date.year - ((today.month, today.day) < (e.birth_date.month, e.birth_date.day))
            age = f'{y} {"سنة" if ar else "Yrs"}'
        rows.append({
            'id': e.id, 'employee_code': e.employee_code,
            'name': e.name_ar if ar and e.name_ar else e.name,
            'name_en': e.name, 'name_ar': e.name_ar or '',
            'profession': profession_str(e.id),
            'is_active': e.is_active,
            'auto_code': e.auto_code,
            'is_muslim': e.is_muslim,
            'blood_group': e.blood_group or '',
            'nationality': e.nationality or '',
            'nationality_ar': e.nationality_ar or '',
            'education': e.education or '',
            'education_ar': e.education_ar or '',
            'kafeel_name': e.kafeel_name or '',
            'kafeel_name_ar': e.kafeel_name_ar or '',
            'kafeel_reference': e.kafeel_reference or '',
            'kafalat_number': e.kafalat_number or '',
            'passport_number': e.passport_number or '',
            'passport_expiry': d(e.passport_expiry),
            'passport_location': e.passport_location or '',
            'entry_number': e.entry_number or '',
            'iqama_number': e.iqama_number or '',
            'iqama_expiry': d(e.iqama_expiry),
            'arrival_date': d(e.arrival_date),
            'birth_date': d(e.birth_date),
            'age': age,
            'mobile': e.mobile or '',
            'email': e.email or '',
            'address': e.address or '',
            'address_ar': e.address_ar or '',
            'home_city': e.home_city or '',
            'home_city_ar': e.home_city_ar or '',
            'employee_reference': e.employee_reference or '',
            'employee_reference_ar': e.employee_reference_ar or '',
            'salary_category': e.salary_category or '',
            'salary_type': e.salary_type or '',
            'basic_salary': float(e.basic_salary or 0),
            'total_allowances': float(e.total_allowances or 0),
            'net_salary': float(e.net_salary or 0),
            'po_number': e.po_number or '',
            'services_charges': float(e.services_charges or 0),
            'po_rate': float(e.po_rate or 0),
            'po_ot_rate': float(e.po_ot_rate or 0),
            'working_hours': float(e.working_hours or 0),
            'overtime_ratio': float(e.overtime_ratio or 0),
            'overtime_rate': float(e.overtime_rate or 0),
            'hostel_name': e.hostel_name or '',
            'hostel_name_ar': e.hostel_name_ar or '',
            'room_number': e.room_number or '',
            'hostel_location': e.hostel_location or '',
            'hostel_location_ar': e.hostel_location_ar or '',
            'crn': e.crn or '',
            'crn_ar': e.crn_ar or '',
            'insurance_company': e.insurance_company or '',
            'insurance_company_ar': e.insurance_company_ar or '',
            'insurance_expiry': d(e.insurance_expiry),
            'labour_office': e.labour_office or '',
            'levelfive_code': e.levelfive_code or '',
            'levelfive_drawer': e.levelfive_drawer or '',
            'created_at': e.created_at.strftime('%Y-%m-%d %H:%M') if e.created_at else '',
            'updated_at': e.updated_at.strftime('%Y-%m-%d %H:%M') if e.updated_at else '',
        })
    return jsonify(rows)

@employees_bp.route('/employees/<int:id>/json')
@login_required
def employee_json(id):
    e = Employee.query.get_or_404(id)
    try:
        return _employee_json(e)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception('[employee_json] emp=%s failed: %s', id, exc)
        return jsonify({'error': _t('Could not load the employee record.',
                                    'تعذّر تحميل بيانات الموظف.')}), 500


def _employee_json(e):
    def d(v): return v.strftime('%Y-%m-%d') if v else ''
    def g(f): return getattr(e, f, None) or ''
    allowance_rows = [a.to_dict() for a in e.allowance_rows.order_by(EmployeeAllowance.id).all()]

    # Get multiple professions
    profession_list = [{
        'id': p.id,
        'name_en': p.name_en,
        'name_ar': p.name_ar or '',
    } for p in e.professions.all()]
    profession_ids = [p.id for p in e.professions.all()]

    wa = latest_work_allocation(e.id)   # most recent allocation (may be None)
    return jsonify({
        'id': e.id, 'employee_code': e.employee_code, 'is_active': e.is_active, 'is_muslim': e.is_muslim,
        'blood_group': g('blood_group'),
        'name': g('name'), 'name_ar': g('name_ar'),
        'kafeel_name': g('kafeel_name'), 'kafeel_name_ar': g('kafeel_name_ar'),
        'kafeel_reference': g('kafeel_reference'), 'kafeel_reference_ar': g('kafeel_reference_ar'),
        'nationality': g('nationality'), 'nationality_ar': g('nationality_ar'),
        'arrival_date': d(e.arrival_date), 'birth_date': d(e.birth_date),
        'passport_number': g('passport_number'), 'passport_expiry': d(e.passport_expiry),
        'entry_number': g('entry_number'), 'iqama_number': g('iqama_number'), 'iqama_expiry': d(e.iqama_expiry),
        'profession': (profession_list[0]['name_en'] if profession_list else ''), 'profession_ar': (profession_list[0]['name_ar'] if profession_list else ''),
        'professions': profession_list,
        'profession_ids': profession_ids,
        'education': g('education'), 'education_ar': g('education_ar'),
        'mobile': g('mobile'), 'address': g('address'), 'address_ar': g('address_ar'), 'email': g('email'),
        'home_city': g('home_city'), 'home_city_ar': g('home_city_ar'),
        'employee_reference': g('employee_reference'), 'employee_reference_ar': g('employee_reference_ar'),
        'po_rate': float(e.po_rate or 0),
        'po_ot_rate': float(e.po_ot_rate or 0),
        'po_number': g('po_number'), 'services_charges': float(e.services_charges or 0),
        'kafalat_number': g('kafalat_number'),
        'salary_type': g('salary_type') or 'month',
        'salary_category': g('salary_category') or 'salary',
        'basic_salary': float(e.basic_salary or 0),
        'total_allowances': float(e.total_allowances or 0),
        'net_salary': float(e.net_salary or 0),
        'working_hours': float(e.working_hours or 8),
        'overtime_ratio': float(e.overtime_ratio or 1.5),
        'overtime_rate': float(e.overtime_rate or 0),
        'hostel_name': g('hostel_name'), 'hostel_name_ar': g('hostel_name_ar'),
        'room_number': g('room_number'), 'hostel_location': g('hostel_location'), 'hostel_location_ar': g('hostel_location_ar'),
        'crn': g('crn'), 'crn_ar': g('crn_ar'),
        'insurance_company': g('insurance_company'), 'insurance_company_ar': g('insurance_company_ar'),
        'insurance_expiry': d(e.insurance_expiry), 'labour_office': g('labour_office'),
        'levelfive_code': g('levelfive_code'), 'levelfive_drawer': g('levelfive_drawer'),
        'passport_location': g('passport_location') or 'IN',
        'department_id': (wa.buyer_department_id or '') if wa else '',
        # department/company/location come from the latest allocation (wa_* below)
        'photo_path': e.photo_path or '',
        'photo_url': _photo_url(e),
        'allowances': allowance_rows,
        'banks': [b.to_dict() for b in e.banks.order_by(EmployeeBank.id).all()],
        'documents': [d.to_dict() for d in e.documents.order_by(EmployeeDocument.id).all()],
        # last work-allocation entry -> the form shows these in edit mode
        'wa_shift': (wa.shift if wa else 'day'),
        'company': (wa.buyer_name if wa else ''),
        'company_ar': (wa.buyer_name_ar if wa else ''),
        'department': (wa.buyer_department if wa else ''),
        'department_ar': (wa.buyer_department_ar if wa else ''),
        'wa_location': (wa.location if wa else ''),
        'wa_location_ar': (wa.location_ar if wa else ''),
    })

@employees_bp.route('/employees/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add_employee():
    if request.method == 'GET':
        # The "Add Employee" form only exists as a modal on the list page —
        # there's no standalone add page. A GET here (e.g. a page refresh
        # after the POST, or a stray navigation) used to 405; now it just
        # sends the user back to the employee list instead of erroring.
        return redirect(url_for('employees.list_employees'))

    iqama_number = (request.form.get('iqama_number') or '').strip()
    if iqama_number and Employee.query.filter_by(iqama_number=iqama_number).first():
        flash(_t(f'Iqama number "{iqama_number}" already belongs to another employee.',
                 f'رقم الإقامة "{iqama_number}" مسجل لموظف آخر بالفعل.'), 'danger')
        return redirect(url_for('employees.list_employees'))

    emp = Employee(created_by=current_user.id)
    emp.employee_code = generate_code()
    bind_employee(emp, request.form)
    db.session.add(emp)
    db.session.flush()
    save_allowances(emp.id, request.form)
    save_banks_from_form(emp.id, request.form)
    save_documents(emp.id, request)
    save_photo(emp.id, request)
    save_professions(emp.id, request)
    # NOTE: no automatic EmployeeWorkAllocation row is created on add anymore —
    # the person asked for that to stop. It's still created on edit (below).
    _recalc_totals(emp.id)
    db.session.commit()
    flash(_t(f'Employee {emp.employee_code} added.', f'تم إضافة الموظف {emp.employee_code}'), 'success')
    return redirect(url_for('employees.list_employees'))

@employees_bp.route('/employees/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_employee(id):
    emp = Employee.query.get_or_404(id)

    iqama_number = (request.form.get('iqama_number') or '').strip()
    if iqama_number:
        dup = Employee.query.filter(Employee.iqama_number == iqama_number,
                                     Employee.id != emp.id).first()
        if dup:
            flash(_t(f'Iqama number "{iqama_number}" already belongs to another employee.',
                     f'رقم الإقامة "{iqama_number}" مسجل لموظف آخر بالفعل.'), 'danger')
            return redirect(url_for('employees.list_employees'))

    bind_employee(emp, request.form)
    emp.updated_at = datetime.utcnow()
    save_allowances(emp.id, request.form)
    save_banks_from_form(emp.id, request.form)
    save_documents(emp.id, request)
    save_photo(emp.id, request)
    save_professions(emp.id, request)
    # Work Allocation (company/department/location/shift) is owned entirely
    # by the separate Work Allocation module now — the employee form no
    # longer collects or writes that data.
    _recalc_totals(emp.id)
    db.session.commit()
    flash(_t('Employee updated.', 'تم تحديث الموظف'), 'success')
    return redirect(url_for('employees.list_employees'))

def _employee_child_tables():
    """(model, label) pairs with a FK to employees that is NOT cascaded by
    the ORM on delete (unlike allowances/banks/documents/professions, which
    have cascade='all, delete-orphan' on the Employee relationship and are
    removed automatically). Deleting an employee who still has rows in any
    of these tables would otherwise fail with a raw FK constraint error
    from the database instead of a clear message.
    """
    from models import EmployeeWorkAllocation, SalaryConsolidation
    return [
        (EmployeeWorkAllocation, _t('Work Allocation', 'توزيع العمل')),
        (SalaryConsolidation, _t('Payroll', 'الرواتب')),
    ]


def _employee_delete_blockers(emp_id):
    """Labels of child records that would block deleting this employee."""
    return [label for model, label in _employee_child_tables()
            if model.query.filter_by(employee_id=emp_id).first()]


@employees_bp.route('/employees/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_employee(id):
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('employee', id)
        flash(_t('Employee deleted -- moved to Recycle Bin.',
                 'تم حذف الموظف — نُقل إلى سلة المحذوفات.'), 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[delete_employee] id=%s failed: %s', id, exc)
        flash(_t('Cannot delete: child record(s) found for this employee.',
                 'تعذر الحذف: توجد سجلات فرعية مرتبطة بهذا الموظف.'), 'danger')
    return redirect(url_for('employees.list_employees'))


# ─── GRID: CELL-LEVEL EDIT + BULK DELETE ──────────────────────────

# Whitelist of grid columns that may be edited directly from the cell.
# net_salary/total_allowances are derived (basic_salary + allowances) and
# are intentionally excluded — editing them here would just be overwritten
# the next time totals are recalculated.
EDITABLE_CELL_FIELDS = {
    'name', 'name_ar', 'nationality', 'iqama_number',
    'passport_number', 'salary_type', 'basic_salary',
}

def _apply_cell_edit(emp, field, value):
    """Validate and apply one grid cell edit onto an in-memory Employee.

    Raises ValueError (safe to show to the user) on bad input. Does not
    commit or recalc totals — callers do that once, after all edits in a
    batch have been applied.
    """
    if field not in EDITABLE_CELL_FIELDS:
        raise ValueError(_t('This field cannot be edited from the grid.',
                            'لا يمكن تعديل هذا الحقل من الشبكة.'))
    if field == 'basic_salary':
        value = Decimal(str(value or 0))
    else:
        value = (value or '').strip()
        if field == 'salary_type' and value not in ('month', 'hour'):
            raise ValueError(_t('Invalid salary type.', 'نوع راتب غير صالح.'))
        if field == 'name' and not value:
            raise ValueError(_t('Name is required.', 'الاسم مطلوب.'))
    setattr(emp, field, value)
    emp.updated_at = datetime.utcnow()


@employees_bp.route('/employees/<int:id>/update-cell', methods=['POST'])
@login_required
@admin_required
def update_employee_cell(id):
    """Save a single cell edit made directly in the employee grid."""
    emp = Employee.query.get_or_404(id)
    d = request.get_json(silent=True) or {}
    field = d.get('field')
    value = d.get('value')

    try:
        _apply_cell_edit(emp, field, value)
        if field == 'basic_salary':
            _recalc_totals(emp.id)
        db.session.commit()
        return jsonify({'ok': True, 'row': {
            'basic_salary': float(emp.basic_salary or 0),
            'total_allowances': float(emp.total_allowances or 0),
            'net_salary': float(emp.net_salary or 0),
        }})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[update-cell] emp=%s field=%s failed: %s', id, field, exc)
        return jsonify({'ok': False,
                        'error': _t('Could not save the change.', 'تعذر حفظ التغيير.')}), 500


@employees_bp.route('/employees/bulk-update-cells', methods=['POST'])
@login_required
@admin_required
def bulk_update_employee_cells():
    """Flush every pending grid cell edit at once (the toolbar Save button).

    Body: {"changes": [{"id": 12, "field": "name", "value": "..."}, ...]}
    Applies every change it can, skips/reports the rest, and commits once.
    """
    d = request.get_json(silent=True) or {}
    changes = d.get('changes') or []
    if not isinstance(changes, list) or not changes:
        return jsonify({'ok': False,
                        'error': _t('No changes to save.', 'لا توجد تغييرات لحفظها.')}), 400

    failed = []
    saved = 0
    recalc_ids = set()
    emp_cache = {}

    for c in changes:
        field = c.get('field')
        raw_id = c.get('id')
        try:
            emp_id = int(raw_id)
        except (TypeError, ValueError):
            failed.append({'id': raw_id, 'field': field,
                           'error': _t('Invalid employee id.', 'رقم موظف غير صالح.')})
            continue

        emp = emp_cache.get(emp_id)
        if emp is None:
            emp = Employee.query.get(emp_id)
            if not emp:
                failed.append({'id': emp_id, 'field': field,
                               'error': _t('Employee not found.', 'الموظف غير موجود.')})
                continue
            emp_cache[emp_id] = emp

        try:
            _apply_cell_edit(emp, field, c.get('value'))
            if field == 'basic_salary':
                recalc_ids.add(emp_id)
            saved += 1
        except ValueError as exc:
            failed.append({'id': emp_id, 'field': field, 'error': str(exc)})
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('[bulk-update-cells] emp=%s field=%s failed: %s', emp_id, field, exc)
            failed.append({'id': emp_id, 'field': field,
                           'error': _t('Could not save.', 'تعذر الحفظ.')})

    try:
        for emp_id in recalc_ids:
            _recalc_totals(emp_id)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[bulk-update-cells] commit failed: %s', exc)
        return jsonify({'ok': False,
                        'error': _t('Could not save the changes.', 'تعذر حفظ التغييرات.')}), 500

    return jsonify({'ok': saved > 0, 'saved': saved, 'failed': failed})


@employees_bp.route('/employees/bulk-delete', methods=['POST'])
@login_required
@admin_required
def bulk_delete_employees():
    """Delete multiple employees selected via the grid checkboxes."""
    d = request.get_json(silent=True) or {}
    ids = d.get('ids') or []
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({'ok': False,
                        'error': _t('Invalid selection.', 'تحديد غير صالح.')}), 400
    if not ids:
        return jsonify({'ok': False,
                        'error': _t('No employees selected.', 'لم يتم تحديد أي موظف.')}), 400

    from database.routes.recycle_bin import soft_delete

    rows = Employee.query.filter(Employee.id.in_(ids)).all()
    blocked = []
    deletable = []
    for e in rows:
        reasons = _employee_delete_blockers(e.id)
        if reasons:
            blocked.append({'id': e.id, 'name': e.name, 'reasons': reasons})
        else:
            deletable.append(e)

    deleted_count = 0
    try:
        for e in deletable:
            soft_delete('employee', e.id)
            deleted_count += 1
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[bulk-delete] ids=%s failed: %s', ids, exc)
        return jsonify({'ok': False,
                        'error': _t('Could not delete the selected employees.',
                                   'تعذر حذف الموظفين المحددين.')}), 500

    result = {'ok': True, 'deleted': deleted_count}
    if blocked:
        names = ', '.join(b['name'] for b in blocked)
        result['blocked'] = blocked
        result['error'] = _t(
            f'{len(blocked)} employee(s) have child records and were not deleted: {names}.',
            f'{len(blocked)} موظف لديهم سجلات فرعية ولم يتم حذفهم: {names}.')
    return jsonify(result)


# ─── ALLOWANCE API ────────────────────────────────────────────────

@employees_bp.route('/employees/<int:emp_id>/allowances')
@login_required
def get_allowances(emp_id):
    rows = EmployeeAllowance.query.filter_by(employee_id=emp_id).order_by(EmployeeAllowance.id).all()
    return jsonify([r.to_dict() for r in rows])

@employees_bp.route('/employees/<int:emp_id>/allowances/add', methods=['POST'])
@login_required
@admin_required
def add_allowance(emp_id):
    Employee.query.get_or_404(emp_id)
    data = request.get_json() or {}
    type_id = data.get('allowance_type_id')
    amount  = _to_float(data.get('amount'))
    if not type_id:
        return jsonify({'ok': False, 'error': 'Allowance type required'}), 400
    atype = AllowanceType.query.get_or_404(int(type_id))
    if EmployeeAllowance.query.filter_by(employee_id=emp_id, allowance_type_id=atype.id).first():
        return jsonify({'ok': False, 'error': f'Allowance "{atype.allowance_name_en}" already exists.'}), 409
    a = EmployeeAllowance(employee_id=emp_id, allowance_type_id=atype.id,
                          allowance_code=atype.allowance_code,
                          name=atype.allowance_name_en, name_ar=atype.allowance_name_ar, amount=amount)
    db.session.add(a); db.session.commit()
    _recalc_totals(emp_id); db.session.commit()
    return jsonify({'ok': True, 'allowance': a.to_dict()})

@employees_bp.route('/employees/allowances/<int:a_id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_allowance_api(a_id):
    a = EmployeeAllowance.query.get_or_404(a_id)
    data = request.get_json() or {}
    type_id = data.get('allowance_type_id')
    if type_id and int(type_id) != a.allowance_type_id:
        existing = EmployeeAllowance.query.filter_by(employee_id=a.employee_id, allowance_type_id=int(type_id)).first()
        if existing and existing.id != a_id:
            return jsonify({'ok': False, 'error': 'Allowance type already exists for this employee.'}), 409
        atype = AllowanceType.query.get(int(type_id))
        if atype:
            a.allowance_type_id = atype.id
            a.allowance_code = atype.allowance_code
            a.name = atype.allowance_name_en
            a.name_ar = atype.allowance_name_ar
    a.amount = _to_float(data.get('amount', a.amount))
    db.session.commit()
    _recalc_totals(a.employee_id); db.session.commit()
    return jsonify({'ok': True, 'allowance': a.to_dict()})

@employees_bp.route('/employees/allowances/<int:a_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_allowance_api(a_id):
    a = EmployeeAllowance.query.get_or_404(a_id)
    emp_id = a.employee_id
    db.session.delete(a); db.session.commit()
    _recalc_totals(emp_id); db.session.commit()
    return jsonify({'ok': True})


# ─── EMPLOYEE BANK API ────────────────────────────────────────────

@employees_bp.route('/employees/<int:emp_id>/banks')
@login_required
def employee_banks(emp_id):
    Employee.query.get_or_404(emp_id)
    rows = EmployeeBank.query.filter_by(employee_id=emp_id).order_by(EmployeeBank.id).all()
    return jsonify([b.to_dict() for b in rows])

@employees_bp.route('/employees/banks/<int:bank_id>')
@login_required
def employee_bank_get(bank_id):
    b = EmployeeBank.query.get_or_404(bank_id)
    return jsonify(b.to_dict())

@employees_bp.route('/employees/<int:emp_id>/banks/add', methods=['POST'])
@login_required
@admin_required
def add_employee_bank(emp_id):
    Employee.query.get_or_404(emp_id)
    d = request.get_json() or {}
    if not (d.get('bank_name') or '').strip():
        return jsonify({'ok': False, 'error': 'Bank name required'}), 400
    is_primary = bool(d.get('is_primary'))
    if is_primary:
        EmployeeBank.query.filter_by(employee_id=emp_id, is_primary=True).update({'is_primary': False})
    b = EmployeeBank(
        employee_id=emp_id,
        bank_name=(d.get('bank_name') or '').strip(),
        bank_name_ar=(d.get('bank_name_ar') or '').strip(),
        branch=(d.get('branch') or '').strip(),
        branch_ar=(d.get('branch_ar') or '').strip(),
        account_number=(d.get('account_number') or '').strip(),
        swift_code=(d.get('swift_code') or '').strip(),
        iban=(d.get('iban') or '').strip(),
        is_primary=is_primary,
    )
    db.session.add(b); db.session.commit()
    return jsonify({'ok': True, 'bank': b.to_dict()})

@employees_bp.route('/employees/banks/<int:bank_id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_employee_bank(bank_id):
    b = EmployeeBank.query.get_or_404(bank_id)
    d = request.get_json() or {}
    b.bank_name      = (d.get('bank_name', b.bank_name) or '').strip()
    b.bank_name_ar   = (d.get('bank_name_ar', b.bank_name_ar) or '').strip()
    b.branch         = (d.get('branch', b.branch) or '').strip()
    b.branch_ar      = (d.get('branch_ar', b.branch_ar) or '').strip()
    b.account_number = (d.get('account_number', b.account_number) or '').strip()
    b.swift_code     = (d.get('swift_code', b.swift_code) or '').strip()
    b.iban           = (d.get('iban', b.iban) or '').strip()
    if 'is_primary' in d:
        b.is_primary = bool(d.get('is_primary'))
        if b.is_primary:
            EmployeeBank.query.filter(EmployeeBank.employee_id == b.employee_id,
                                      EmployeeBank.id != b.id).update({'is_primary': False})
    db.session.commit()
    return jsonify({'ok': True, 'bank': b.to_dict()})

@employees_bp.route('/employees/banks/<int:bank_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_employee_bank(bank_id):
    b = EmployeeBank.query.get_or_404(bank_id)
    db.session.delete(b); db.session.commit()
    return jsonify({'ok': True})


# ─── COMPANY (BUYER) -> DEPARTMENT CASCADE ────────────────────────

@employees_bp.route('/employees/departments-by-buyer')
@login_required
def departments_by_buyer():
    """Departments belonging to the selected Company/HR (buyer).

    Returns [] when no buyer is supplied, so the Department dropdown can
    never offer a record from an unrelated company.
    """
    from models import BuyerDepartment
    buyer_id = request.args.get('buyer_id', type=int)
    if not buyer_id:
        return jsonify([])
    rows = (BuyerDepartment.query
            .filter_by(buyer_id=buyer_id)
            .order_by(BuyerDepartment.department_name).all())
    return jsonify([{
        'id': r.id,
        'name': r.department_name,
        'name_ar': r.department_name_ar or '',
        'label': r.department_name,
        'location_name': r.location_name or '',
        'location_name_ar': r.location_name_ar or '',
    } for r in rows])


@employees_bp.route('/employees/departments/add', methods=['POST'])
@login_required
def add_department_quick():
    """Create a department under the given Company/HR (buyer) from the
    Employee form's Department (+Add) button, then return it so the dropdown
    can refresh and pre-select it. Errors are returned as JSON messages."""
    from models import BuyerDepartment
    buyer_id = request.form.get('buyer_id', type=int)
    name = (request.form.get('department_name', '') or '').strip()
    name_ar = (request.form.get('department_name_ar', '') or '').strip()
    if not buyer_id:
        return jsonify({'ok': False, 'error': _t('Select a company first.',
                                                 'اختر الشركة أولاً.')}), 400
    if not name:
        return jsonify({'ok': False, 'error': _t('Department name is required.',
                                                 'اسم القسم مطلوب.')}), 400
    existing = (BuyerDepartment.query
                .filter_by(buyer_id=buyer_id, department_name=name).first())
    if existing:
        return jsonify({'ok': False, 'error': _t('This department already exists.',
                                                 'هذا القسم موجود بالفعل.')}), 400
    try:
        dep = BuyerDepartment(buyer_id=buyer_id, department_name=name,
                              department_name_ar=name_ar or None)
        db.session.add(dep)
        db.session.commit()
        return jsonify({'ok': True, 'id': dep.id, 'name': dep.department_name,
                        'name_ar': dep.department_name_ar or ''})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('[dept-add] failed: %s', e)
        return jsonify({'ok': False, 'error': _t('Could not save the department.',
                                                 'تعذّر حفظ القسم.')}), 500


# ─── EMPLOYEE PHOTO ───────────────────────────────────────────────

@employees_bp.route('/employees/<int:emp_id>/photo')
@login_required
def employee_photo(emp_id):
    """Serve the employee's photo. Missing/broken paths return 404 so the
    front-end can show its placeholder instead of a broken image."""
    from flask import send_file, abort
    e = Employee.query.get_or_404(emp_id)
    full = _photo_abs_path(e)
    if not full or not os.path.exists(full):
        abort(404)
    resp = send_file(full)
    # The URL is cache-busted by mtime, but never let a stale copy linger.
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


# ─── EMPLOYEE DOCUMENT API ────────────────────────────────────────

@employees_bp.route('/employees/<int:emp_id>/documents')
@login_required
def employee_documents(emp_id):
    Employee.query.get_or_404(emp_id)
    rows = EmployeeDocument.query.filter_by(employee_id=emp_id).order_by(EmployeeDocument.id).all()
    return jsonify([d.to_dict() for d in rows])

@employees_bp.route('/employees/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_employee_document(doc_id):
    d = EmployeeDocument.query.get_or_404(doc_id)
    try:
        full = os.path.join(current_app.config['UPLOAD_FOLDER'], d.file_path)
        if os.path.exists(full):
            os.remove(full)
    except Exception:
        pass
    db.session.delete(d); db.session.commit()
    return jsonify({'ok': True})


@employees_bp.route('/employee-documents')
@login_required
def employee_documents_page():
    """Standalone page to manage employee documents (separate from the form)."""
    emps = Employee.query.order_by(Employee.name).all()
    return render_template('employees/documents.html', employees=emps)


@employees_bp.route('/employees/<int:emp_id>/documents/upload', methods=['POST'])
@login_required
@admin_required
def upload_employee_document(emp_id):
    """Upload one document for an employee from the standalone page."""
    emp = Employee.query.get_or_404(emp_id)
    f = request.files.get('document')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'No file selected.'}), 400
    path = save_upload(f, emp_id)
    if not path:
        return jsonify({'ok': False, 'error': 'Could not save file.'}), 400
    doc = EmployeeDocument(
        employee_id=emp_id,
        document_type=(request.form.get('document_type', '') or '').strip(),
        file_path=path,
        original_name=f.filename,
        uploaded_by=current_user.id,
        employee_code=emp.employee_code,
        employee_name=emp.name,
        passport_number=emp.passport_number,
        iqama_number=emp.iqama_number,
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify({'ok': True, 'document': doc.to_dict()})


@employees_bp.route('/employees/documents/<int:doc_id>/view')
@login_required
def view_employee_document(doc_id):
    """Serve an employee document file for viewing/download."""
    from flask import send_from_directory
    d = EmployeeDocument.query.get_or_404(doc_id)
    parts = d.file_path.replace('\\', '/').split('/')
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], *parts[:-1])
    return send_from_directory(folder, parts[-1], as_attachment=False)


@employees_bp.route('/employees/documents/<int:doc_id>/download')
@login_required
def download_employee_document(doc_id):
    """Always download the file, never display inline."""
    from flask import send_from_directory
    d = EmployeeDocument.query.get_or_404(doc_id)
    parts = d.file_path.replace('\\', '/').split('/')
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], *parts[:-1])
    return send_from_directory(folder, parts[-1], as_attachment=True,
                                download_name=d.original_name or parts[-1])


def _emp_doc_to_dict(doc):
    """Serialize an EmployeeDocument row.

    employee_code / employee_name / passport_number / iqama_number are
    stored directly on the row as a snapshot taken at upload time, so they
    survive even if the employee's own record later changes or is deleted.
    Rows uploaded before that snapshot existed fall back to a live lookup
    on the employee.
    """
    uploader = User.query.get(doc.uploaded_by) if doc.uploaded_by else None
    emp_code = doc.employee_code
    emp_name = doc.employee_name
    passport = doc.passport_number
    iqama    = doc.iqama_number
    if not (emp_code and emp_name and passport and iqama):
        emp = Employee.query.get(doc.employee_id)
        if emp:
            emp_code = emp_code or emp.employee_code
            emp_name = emp_name or emp.name
            passport = passport or emp.passport_number
            iqama    = iqama or emp.iqama_number
    return {
        'id':                doc.id,
        'employee_id':       doc.employee_id,
        'employee_name':     emp_name or '',
        'employee_code':     emp_code or '',
        'passport_number':   passport or '',
        'iqama_number':      iqama or '',
        'document_type':     doc.document_type or '',
        'original_name':     doc.original_name or '',
        'file_path':         doc.file_path or '',
        'uploaded_at':       doc.uploaded_at.strftime('%Y-%m-%d %H:%M') if doc.uploaded_at else '',
        'uploaded_by':       doc.uploaded_by,
        'uploaded_by_name':  uploader.username if uploader else '',
    }


@employees_bp.route('/employee-documents/data')
@login_required
def employee_documents_data():
    """AG-Grid data feed: every EmployeeDocument row, across all employees."""
    docs = EmployeeDocument.query.order_by(EmployeeDocument.id.desc()).all()
    return jsonify([_emp_doc_to_dict(d) for d in docs])


@employees_bp.route('/employees/documents/<int:doc_id>/json')
@login_required
def employee_document_json(doc_id):
    doc = EmployeeDocument.query.get_or_404(doc_id)
    return jsonify(_emp_doc_to_dict(doc))


@employees_bp.route('/employees/documents/<int:doc_id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_employee_document(doc_id):
    """Only document_type is editable -- the file itself is replaced by
    uploading a new document, not by editing this one."""
    doc = EmployeeDocument.query.get_or_404(doc_id)
    data = request.get_json() or {}
    doc.document_type = (data.get('document_type', '') or '').strip()
    db.session.commit()
    return jsonify({'ok': True, 'document': _emp_doc_to_dict(doc)})


@employees_bp.route('/employees/export')
@login_required
def export_employees():
    import csv, io
    from flask import make_response
    emps = Employee.query.all()
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(['Code', 'Name', 'Nationality', 'Profession', 'Iqama', 'Birth Date',
                'Mobile', 'Dept', 'Salary Type', 'Basic', 'Total Allow', 'Net Salary',
                'Services Charges', 'PO Rate', 'PO OT Rate', 'Status'])
    for e in emps:
        w.writerow([e.employee_code, e.name, e.nationality or '', _emp_profession_str(e),
                    e.iqama_number or '', e.birth_date or '', e.mobile or '',
                    _wa_department(e), e.salary_type or '', e.basic_salary or '',
                    e.total_allowances or 0, e.net_salary or '',
                    e.services_charges or 0, e.po_rate or 0, e.po_ot_rate or 0,
                    'Active' if e.is_active else 'Inactive'])
    resp = make_response(out.getvalue())
    resp.headers['Content-Disposition'] = 'attachment; filename=employees.csv'
    resp.headers['Content-type'] = 'text/csv'
    return resp


# ══════════════════════════════════════════════════════════════════
# EMPLOYEE PROFESSION -- standalone page to manage employee<->profession
# assignments (separate from the multi-select chips embedded in the main
# Employee add/edit form). Both stay in sync since they operate on the
# same employee_professions rows and the Employee form always reloads its
# chips from the current rows before letting the user edit/re-save them.
# ══════════════════════════════════════════════════════════════════

def _emp_prof_to_dict(ep, emp=None):
    emp = emp or Employee.query.get(ep.employee_id)
    return {
        'id': ep.id,
        'employee_id': ep.employee_id,
        'employee_code': emp.employee_code if emp else '',
        'employee_name': emp.name if emp else '',
        'profession_id': ep.profession_id,
        'profession_name': ep.profession_name or '',
        'profession_name_ar': ep.profession_name_ar or '',
        'created_at': ep.created_at.strftime('%Y-%m-%d %H:%M') if ep.created_at else '',
    }


@employees_bp.route('/employee-professions')
@login_required
def employee_professions_page():
    """Standalone page to manage employee professions (separate from the form)."""
    emps = Employee.query.order_by(Employee.name).all()
    professions = ProfessionMaster.query.filter_by(is_active=True).order_by(ProfessionMaster.name_en).all()
    return render_template('employees/employee_professions.html', employees=emps, professions=professions)


@employees_bp.route('/employee-professions/data')
@login_required
def employee_professions_data():
    """AG-Grid data feed: every Employee<->Profession assignment row."""
    rows = (db.session.query(EmployeeProfession, Employee)
            .join(Employee, Employee.id == EmployeeProfession.employee_id)
            .order_by(EmployeeProfession.id.desc()).all())
    return jsonify([_emp_prof_to_dict(ep, emp) for ep, emp in rows])


@employees_bp.route('/employee-professions/<int:id>/json')
@login_required
def employee_profession_json(id):
    ep = EmployeeProfession.query.get_or_404(id)
    return jsonify(_emp_prof_to_dict(ep))


@employees_bp.route('/employee-professions/add', methods=['POST'])
@login_required
@admin_required
def add_employee_profession():
    """Assign one or more professions to one employee. Professions already
    assigned to that employee are silently skipped (the DB's unique
    constraint on (employee_id, profession_id) is the ultimate guard)."""
    emp_id = request.form.get('employee_id', type=int)
    prof_ids = [int(p) for p in request.form.getlist('profession_ids[]') if p]
    if not emp_id:
        return jsonify({'ok': False, 'error': _t('Employee is required.', 'الموظف مطلوب.')}), 400
    if not prof_ids:
        return jsonify({'ok': False, 'error': _t('Select at least one profession.', 'اختر مهنة واحدة على الأقل.')}), 400
    Employee.query.get_or_404(emp_id)
    existing = {pid for (pid,) in db.session.query(EmployeeProfession.profession_id)
                .filter_by(employee_id=emp_id).all()}
    added = 0
    for pid in prof_ids:
        if pid in existing:
            continue
        pm = ProfessionMaster.query.get(pid)
        if not pm:
            continue
        db.session.add(EmployeeProfession(
            employee_id=emp_id, profession_id=pid,
            profession_name=pm.name_en, profession_name_ar=pm.name_ar,
        ))
        existing.add(pid)
        added += 1
    db.session.commit()
    return jsonify({'ok': True, 'added': added})


@employees_bp.route('/employee-professions/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_employee_profession(id):
    ep = EmployeeProfession.query.get_or_404(id)
    emp_id = request.form.get('employee_id', type=int) or ep.employee_id
    prof_id = request.form.get('profession_id', type=int)
    if not prof_id:
        return jsonify({'ok': False, 'error': _t('Profession is required.', 'المهنة مطلوبة.')}), 400
    Employee.query.get_or_404(emp_id)
    pm = ProfessionMaster.query.get_or_404(prof_id)
    dup = EmployeeProfession.query.filter(
        EmployeeProfession.employee_id == emp_id,
        EmployeeProfession.profession_id == prof_id,
        EmployeeProfession.id != id).first()
    if dup:
        return jsonify({'ok': False, 'error': _t(
            'This employee already has that profession assigned.',
            'هذا الموظف لديه بالفعل هذه المهنة.')}), 400
    ep.employee_id = emp_id
    ep.profession_id = prof_id
    ep.profession_name = pm.name_en
    ep.profession_name_ar = pm.name_ar
    db.session.commit()
    return jsonify({'ok': True})


@employees_bp.route('/employee-professions/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_employee_profession(id):
    ep = EmployeeProfession.query.get_or_404(id)
    db.session.delete(ep)
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
# EMPLOYEE ALLOWANCES -- standalone page listing every employee<->
# allowance assignment across all employees. Add/Edit/Delete of a single
# row reuse the existing per-employee allowance API (add_allowance /
# edit_allowance_api / delete_allowance_api above), which already
# validates duplicates and keeps Employee.total_allowances/net_salary in
# sync via _recalc_totals() -- this page only adds the cross-employee list.
# ══════════════════════════════════════════════════════════════════

@employees_bp.route('/employee-allowances')
@login_required
def employee_allowances_page():
    """Standalone page to manage employee allowances (separate from the form)."""
    emps = Employee.query.order_by(Employee.name).all()
    types = AllowanceType.query.filter_by(is_active=True).order_by(AllowanceType.allowance_name_en).all()
    return render_template('employees/employee_allowances.html', employees=emps, allowance_types=types)


@employees_bp.route('/employee-allowances/data')
@login_required
def employee_allowances_data():
    """AG-Grid data feed: every EmployeeAllowance row, across all employees."""
    rows = (db.session.query(EmployeeAllowance, Employee)
            .join(Employee, Employee.id == EmployeeAllowance.employee_id)
            .order_by(EmployeeAllowance.id.desc()).all())
    out = []
    for a, emp in rows:
        d = a.to_dict()
        d['employee_code'] = emp.employee_code
        d['employee_name'] = emp.name
        d['created_at'] = a.created_at.strftime('%Y-%m-%d %H:%M') if a.created_at else ''
        out.append(d)
    return jsonify(out)