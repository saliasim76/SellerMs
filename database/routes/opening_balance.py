"""
Opening Balance — the one-time (or occasional-correction) mechanism for
entering every Chart-of-Accounts head's starting Debit/Credit balance when
this software first goes live, or when a new account is discovered
mid-migration.

Master/detail, like every other GL-posting document in this app:
    opening_balances(id, doc_no, balance_date, status, narration,
                      journal_entry_id, grl_id, ...)
    opening_balance_details(id, opening_balance_id FK, code, account_name,
                            debit, credit)

Follows this app's standing post-only rule: Save (Draft) never touches
JournalEntry/GRL; only Post & Save does. Once Posted, a document is
locked -- correcting it means posting a fresh Opening Balance entry with
the adjusting difference, exactly like every other GL document here (no
in-place edit of a Posted document exists anywhere in this codebase).
"""

from datetime import date, datetime

from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user

from models import (db, OpeningBalance, OpeningBalanceDetail, LevelFive,
                    GRL, GRLDetail, JournalEntry, JournalEntryDetail,
                    next_je_no, next_ob_no, NoActiveFinancialYearError)
from database.routes.shared import _next_grl_no, _grl_num, _t

ob_bp = Blueprint('opening_balance', __name__, url_prefix='/coa/opening-balance')


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        return None


@ob_bp.route('/')
@login_required
def ob_list():
    return render_template('coa/opening_balance_list.html')


@ob_bp.route('/data')
@login_required
def ob_data():
    rows = OpeningBalance.query.order_by(OpeningBalance.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@ob_bp.route('/accounts')
@login_required
def ob_accounts():
    """Every active Chart-of-Accounts head, plus whatever opening balance
    (if any) was already Posted for it -- shown as a reference column so
    re-running this screen doesn't silently double an account's balance."""
    from models import LevelOne
    l1_by_code = {r.code: r for r in LevelOne.query.all()}

    existing = {}
    posted_lines = (db.session.query(OpeningBalanceDetail)
                    .join(OpeningBalance, OpeningBalanceDetail.opening_balance_id == OpeningBalance.id)
                    .filter(OpeningBalance.status == 'Posted').all())
    for line in posted_lines:
        existing[line.code] = existing.get(line.code, 0.0) + float(line.debit or 0) - float(line.credit or 0)

    rows = LevelFive.query.filter(LevelFive.status == 'active').order_by(LevelFive.code).all()
    out = []
    for r in rows:
        nature_code = (r.code or '')[:1]
        l1 = l1_by_code.get(nature_code)
        out.append({
            'code': r.code, 'name': r.drawers or '', 'name_ar': r.drawers_ar or '',
            'account_type_name': l1.drawers if l1 else '',
            'existing_balance': round(existing.get(r.code, 0.0), 2),
        })
    return jsonify(out)


@ob_bp.route('/<int:id>/json')
@login_required
def ob_json(id):
    doc = OpeningBalance.query.get_or_404(id)
    return jsonify(doc.to_dict())


def _apply_lines(doc, form):
    codes = form.getlist('code[]')
    names = form.getlist('account_name[]')
    debits = form.getlist('debit[]')
    credits = form.getlist('credit[]')

    OpeningBalanceDetail.query.filter_by(opening_balance_id=doc.id).delete()
    total_debit = total_credit = 0.0
    for i in range(len(codes)):
        code = (codes[i] or '').strip()
        debit = _grl_num(debits[i] if i < len(debits) else 0)
        credit = _grl_num(credits[i] if i < len(credits) else 0)
        if not code or (abs(debit) < 0.005 and abs(credit) < 0.005):
            continue
        db.session.add(OpeningBalanceDetail(
            opening_balance_id=doc.id, code=code,
            account_name=(names[i] if i < len(names) else '').strip(),
            debit=debit, credit=credit,
        ))
        total_debit += debit
        total_credit += credit
    return round(total_debit, 2), round(total_credit, 2)


@ob_bp.route('/save', methods=['POST'])
@login_required
def ob_save():
    """Draft save only -- never touches JournalEntry/GRL, per this app's
    post-only rule."""
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = OpeningBalance.query.get_or_404(int(id_str))
            if doc.status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Opening Balance has already been posted.',
                    'تم ترحيل رصيد الافتتاح هذا مسبقاً.')}), 400
            doc.updated_by = current_user.id
        else:
            doc = OpeningBalance(status='Draft', created_by=current_user.id)
            db.session.add(doc)
            db.session.flush()

        doc.balance_date = _parse_date(f.get('balance_date')) or doc.balance_date or date.today()
        doc.narration = (f.get('narration') or '').strip()
        _apply_lines(doc, f)
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.id, 'doc_no': doc.doc_no or '', 'status': doc.status})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@ob_bp.route('/post', methods=['POST'])
@login_required
def ob_post_and_save():
    f = request.form
    id_str = (f.get('id') or '').strip()
    try:
        if id_str:
            doc = OpeningBalance.query.get_or_404(int(id_str))
            if doc.status == 'Posted':
                return jsonify({'ok': False, 'error': _t(
                    'This Opening Balance has already been posted.',
                    'تم ترحيل رصيد الافتتاح هذا مسبقاً.')}), 400
            doc.updated_by = current_user.id
        else:
            doc = OpeningBalance(status='Draft', created_by=current_user.id)
            db.session.add(doc)
            db.session.flush()

        balance_date = _parse_date(f.get('balance_date')) or doc.balance_date or date.today()
        doc.balance_date = balance_date
        doc.narration = (f.get('narration') or '').strip()

        total_debit, total_credit = _apply_lines(doc, f)
        if abs(total_debit) < 0.005 and abs(total_credit) < 0.005:
            return jsonify({'ok': False, 'error': _t(
                'Enter at least one account balance before posting.',
                'أدخل رصيد حساب واحد على الأقل قبل الترحيل.')}), 400
        if abs(total_debit - total_credit) > 0.005:
            return jsonify({'ok': False, 'error': _t(
                f'Total Debit ({total_debit:.2f}) must equal Total Credit ({total_credit:.2f}) before posting.',
                f'يجب أن يتساوى إجمالي المدين ({total_debit:.2f}) مع إجمالي الدائن ({total_credit:.2f}) قبل الترحيل.')}), 400

        if not doc.doc_no:
            doc.doc_no = next_ob_no()

        grl = GRL.query.filter_by(opening_balance_id=doc.id).first()
        if not grl:
            grl = GRL(opening_balance_id=doc.id)
            db.session.add(grl)
        if not grl.grl_no:
            grl.grl_no = _next_grl_no()
        grl.origion = doc.doc_no
        grl.posting_date = balance_date
        grl.document_date = date.today()
        grl.narration = doc.narration or f'Opening Balance {doc.doc_no}'
        db.session.flush()

        je = JournalEntry.query.get(grl.journal_entry_id) if grl.journal_entry_id else None
        if not je:
            je = JournalEntry(je_no=next_je_no(), origin_type='OB', origin_id=doc.id)
            db.session.add(je)
            db.session.flush()
            grl.journal_entry_id = je.id
        je.origion = grl.origion
        je.posting_date = grl.posting_date
        je.document_date = grl.document_date
        je.narration = grl.narration

        GRLDetail.query.filter_by(grl_id=grl.id).delete()
        JournalEntryDetail.query.filter_by(journal_entry_id=je.id).delete()
        for line in doc.lines:
            db.session.add(GRLDetail(
                grl_id=grl.id, code=line.code, account_name=line.account_name,
                debit=line.debit, credit=line.credit, narration=grl.narration,
            ))
            db.session.add(JournalEntryDetail(
                journal_entry_id=je.id, code=line.code, account_name=line.account_name,
                debit=line.debit, credit=line.credit, narration=grl.narration,
            ))

        doc.grl_id = grl.id
        doc.journal_entry_id = je.id
        doc.status = 'Posted'
        db.session.commit()
        return jsonify({'ok': True, 'id': doc.id, 'doc_no': doc.doc_no, 'status': doc.status})
    except NoActiveFinancialYearError:
        db.session.rollback()
        return jsonify({'ok': False, 'error': _t(
            'Please activate your financial year first.',
            'الرجاء تفعيل السنة المالية أولاً.')}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@ob_bp.route('/<int:id>/delete', methods=['POST'])
@login_required
def ob_delete(id):
    doc = OpeningBalance.query.get_or_404(id)
    if doc.status == 'Posted':
        return jsonify({'ok': False, 'error': _t(
            'A Posted Opening Balance cannot be deleted.',
            'لا يمكن حذف رصيد افتتاح مرحّل.')}), 400
    try:
        db.session.delete(doc)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@ob_bp.route('/<int:id>/view')
@login_required
def ob_view(id):
    doc = OpeningBalance.query.get_or_404(id)
    return render_template('coa/opening_balance_view.html', doc=doc)
