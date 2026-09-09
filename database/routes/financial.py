from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required, current_user
from datetime import datetime
from models import (db, FinancialYear, FinancialYearDetail, build_financial_months)

financial_bp = Blueprint('financial', __name__)


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


# ── Page ────────────────────────────────────────────────────────
@financial_bp.route('/financial-years')
@login_required
def fy_page():
    return render_template('financial/fy_list.html')


# ── Financial Year: list / next-suggestion / CRUD ───────────────
@financial_bp.route('/financial-years/data')
@login_required
def fy_data():
    rows = FinancialYear.query.order_by(FinancialYear.year.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@financial_bp.route('/financial-years/next')
@login_required
def fy_next():
    """Suggest the next year based on the latest financial year on record."""
    latest = FinancialYear.query.order_by(FinancialYear.year.desc()).first()
    year = (latest.year + 1) if latest and latest.year else datetime.utcnow().year
    return jsonify({
        'year': year,
        'financial_year': f'FY-{year}',
        'range': f'01-Jan-{year} → 31-Dec-{year}',
        'status': 'Open',
    })


@financial_bp.route('/financial-years/<int:id>/months')
@login_required
def fy_months(id):
    FinancialYear.query.get_or_404(id)
    rows = (FinancialYearDetail.query
            .filter_by(financial_year_id=id)
            .order_by(FinancialYearDetail.month_no).all())
    return jsonify([r.to_dict() for r in rows])


@financial_bp.route('/financial-years/add', methods=['POST'])
@login_required
def fy_add():
    f = request.form
    try:
        year = int(f.get('year'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': _t('A valid year is required.', 'السنة مطلوبة')}), 400

    fy_str = f'FY-{year}'

    # Duplicate guard
    if FinancialYear.query.filter_by(financial_year=fy_str).first():
        return jsonify({'ok': False, 'error': _t(f'{fy_str} already exists.', f'{fy_str} موجود بالفعل')}), 400

    # Continuity guard: years cannot be skipped. The first year can be anything,
    # but every subsequent year must be exactly (latest existing year + 1).
    latest = FinancialYear.query.order_by(FinancialYear.year.desc()).first()
    if latest and latest.year is not None:
        # Previous financial year must be fully Closed before opening a new one.
        if (latest.status or 'Open') != 'Closed':
            return jsonify({'ok': False, 'error': _t(
                f'Close {latest.financial_year} first (all 12 months, then the year) '
                f'before adding a new financial year.',
                f'أغلق {latest.financial_year} أولاً (جميع الأشهر الـ12 ثم السنة) '
                f'قبل إضافة سنة مالية جديدة.')}), 400
        expected = latest.year + 1
        if year != expected:
            return jsonify({'ok': False, 'error': _t(
                f'You must create FY-{expected} next (years cannot be skipped).',
                f'يجب إنشاء FY-{expected} التالي (لا يمكن تخطي السنوات).')}), 400

    status = f.get('status', 'Open')
    if status not in ('Open', 'Closed'):
        status = 'Open'

    try:
        # A new Financial Year always starts Open — its 12 months are created
        # Open, and the year's status is derived from them (see fm_edit).
        fy = FinancialYear(
            financial_year=fy_str,
            range=f'01-Jan-{year} → 31-Dec-{year}',
            year=year,
            status='Open',
        )
        db.session.add(fy)
        db.session.flush()   # get fy.id

        # 12 financial months in the same transaction
        for m in build_financial_months(year):
            db.session.add(FinancialYearDetail(financial_year_id=fy.id, **m))

        db.session.commit()
        return jsonify({'ok': True, 'id': fy.id, 'financial_year': fy_str})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@financial_bp.route('/financial-years/<int:id>/edit', methods=['POST'])
@login_required
def fy_edit(id):
    fy = FinancialYear.query.get_or_404(id)
    f = request.form
    status = f.get('status', fy.status)
    if status not in ('Open', 'Closed'):
        status = fy.status
    try:
        if status == 'Closed':
            # A financial year can be closed only when ALL 12 months are Closed.
            months = FinancialYearDetail.query.filter_by(financial_year_id=id).all()
            all_closed = bool(months) and all(
                (m.status or 'Open') == 'Closed' for m in months)
            if not all_closed:
                open_count = sum(1 for m in months if (m.status or 'Open') != 'Closed')
                return jsonify({'ok': False, 'error': _t(
                    f'Close all 12 months first — {open_count} month(s) still Open.',
                    f'أغلق جميع الأشهر الـ12 أولاً — {open_count} شهر لا يزال مفتوحًا.')}), 400
        if status == 'Open':
            # ensure only this one is Open
            FinancialYear.query.filter(FinancialYear.id != id, FinancialYear.status == 'Open')\
                               .update({'status': 'Closed'})
        fy.status = status
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@financial_bp.route('/financial-years/<int:id>/delete', methods=['POST'])
@login_required
def fy_delete(id):
    fy = FinancialYear.query.get_or_404(id)
    try:
        db.session.delete(fy)   # cascade removes the 12 months
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Financial Month: status-only edit ───────────────────────────
@financial_bp.route('/financial-months/<int:id>/edit', methods=['POST'])
@login_required
def fm_edit(id):
    fm = FinancialYearDetail.query.get_or_404(id)
    status = request.form.get('status', fm.status)
    if status not in ('Open', 'Closed'):
        status = fm.status
    try:
        fm.status = status
        db.session.flush()
        # The year is closed MANUALLY (via fy_edit) once all months are closed.
        # Here we only enforce the invariant in the other direction: a Closed
        # year cannot keep an Open month, so reopening a month reopens the year.
        parent = FinancialYear.query.get(fm.financial_year_id)
        if parent and status == 'Open' and (parent.status or 'Open') == 'Closed':
            parent.status = 'Open'
        db.session.commit()
        return jsonify({'ok': True, 'year_status': parent.status if parent else None})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500