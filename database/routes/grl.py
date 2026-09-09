"""
GRL Account — VIEW ONLY grid of General Receipt Ledger entries.

Entries are created from the Goods Receipt Note form's GRL Detail section
(see database/routes/purchase.py: grl_save). This page just lists and
displays them, same pattern as the Journal Entries page.
"""

from flask import Blueprint, render_template, jsonify, session
from flask_login import login_required

from models import db, GRL


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en

grl_bp = Blueprint('grl', __name__, url_prefix='/grl-account')


@grl_bp.route('/')
@login_required
def grl_list():
    return render_template('grl/list.html')


@grl_bp.route('/data')
@login_required
def grl_data():
    """One grid ROW per GRL detail LINE (not one row per GRL header) --
    a GRL can carry more than one detail-line pair (one per source document
    line item) since each posts its own Code/Control Account/Debit/Credit,
    and joining them into one cell hid that. Header fields (grl_no, origion,
    posting_date, ...) are repeated on every line belonging to that header;
    `id` stays the GRL header id (so the View button still opens the full
    breakdown) while `detail_id` identifies this specific line."""
    from database.routes.shared import GRL_FK_ORIGIN_MAP, origin_document_exists
    rows = GRL.query.order_by(GRL.id.desc()).all()
    out = []
    for g in rows:
        header = g.to_dict()
        details = header.pop('details', [])
        has_child = False
        for fk_attr, (model_name, pk_attr) in GRL_FK_ORIGIN_MAP.items():
            doc_id = getattr(g, fk_attr, None)
            if doc_id and origin_document_exists(model_name, pk_attr, doc_id):
                has_child = True
                break
        header['has_child'] = has_child
        if not details:
            row = dict(header)
            row.update({'detail_id': None, 'code': '', 'reference_code': '', 'account_name': '',
                        'control_account': '', 'debit': 0, 'credit': 0, 'line_narration': ''})
            out.append(row)
            continue
        for det in details:
            row = dict(header)
            row.update({
                'detail_id': det['id'],
                'code': det['code'],
                'reference_code': det.get('reference_code', ''),
                'account_name': det['account_name'],
                'control_account': det['control_account'],
                'debit': det['debit'],
                'credit': det['credit'],
                'line_narration': det['narration'],
            })
            out.append(row)
    return jsonify(out)


@grl_bp.route('/<int:grl_id>/json')
@login_required
def grl_json(grl_id):
    grl = GRL.query.get_or_404(grl_id)
    return jsonify(grl.to_dict())


@grl_bp.route('/<int:grl_id>/delete', methods=['POST'])
@login_required
def grl_delete(grl_id):
    """Blocked while this GRL's source document (the GRN/Purchase
    Invoice/... it was posted from) still exists -- the correct way to
    remove a GRL is to delete/unpost that document itself, which already
    reverses everything correctly. This route is only for genuinely
    orphaned entries."""
    from database.routes.shared import GRL_FK_ORIGIN_MAP, origin_document_exists
    grl = GRL.query.get_or_404(grl_id)
    for fk_attr, (model_name, pk_attr) in GRL_FK_ORIGIN_MAP.items():
        doc_id = getattr(grl, fk_attr, None)
        if doc_id and origin_document_exists(model_name, pk_attr, doc_id):
            return jsonify({'ok': False, 'error': _t(
                'Child record found: the source document for this entry still exists. Delete or unpost it first.',
                'تم العثور على سجل فرعي: لا يزال المستند المصدر لهذا القيد موجودًا. احذفه أو ألغِ ترحيله أولاً.')}), 400
    try:
        db.session.delete(grl)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
