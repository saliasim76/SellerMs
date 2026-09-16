"""Auto Code Selection: maps a Module + Form pair to a Level Five (Chart of
Accounts) code and its Debit/Credit nature. Lives under the Chart of
Accounts sidebar section since it is COA-adjacent configuration data.
"""
from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required, current_user
from models import db, AutoCodeSelection, Module, SystemForm, LevelFour, LevelFive
from database.routes.shared import block_trial_write_json

# Cash & Bank is not configured through this screen -- its Outgoing/
# Incoming Payment forms now have their own "GL Account" dropdown, scoped
# to Mode of Payment/Receipt directly (see PAYMENT_MODE_NAME_LEVEL_FOUR in
# database/routes/cash_bank.py), making a module/form mapping here
# redundant for that module. Excluded from the Module dropdown entirely
# (see auto_code_modules()); any Form under it is excluded too as a result.

auto_code_bp = Blueprint('auto_code', __name__)


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


# ── Page ────────────────────────────────────────────────────────
@auto_code_bp.route('/auto-code-selection')
@login_required
def auto_code_page():
    return render_template('coa/auto_code_selection.html')


# ── Grid data ───────────────────────────────────────────────────
@auto_code_bp.route('/auto-code-selection/data')
@login_required
def auto_code_data():
    rows = AutoCodeSelection.query.order_by(AutoCodeSelection.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


# ── Picker: modules ─────────────────────────────────────────────
# Cash & Bank is excluded -- see the module-level comment above.
@auto_code_bp.route('/auto-code-selection/modules')
@login_required
def auto_code_modules():
    rows = (Module.query.filter(Module.code != 'cash_bank')
            .order_by(Module.sort_order, Module.label_en).all())
    return jsonify([{'id': m.id, 'code': m.code, 'label_en': m.label_en,
                      'label_ar': m.label_ar or ''} for m in rows])


# Only these Purchase forms are offered in the Form dropdown when the
# selected Module is Purchase -- Purchase Request/Quotation/Order, Goods
# Return Request, Stores and PO Quantity Tracking are excluded since they
# never look up an Auto Code Selection mapping (see _grl_lines_from() in
# database/routes/purchase.py for the forms that do). Supplier is also
# excluded: a Supplier's own control account is now configured directly on
# the Supplier master record's own Chart of Account picker, not here.
PURCHASE_AUTO_CODE_FORMS = {
    'goods_receipt_note', 'purchase_invoice', 'purchase_return_note', 'purchase_debit_memo',
}

# Only these Sale forms are offered in the Form dropdown when the selected
# Module is Sale -- Sales Order and Sales Return Request are excluded since
# they never look up an Auto Code Selection mapping (see _grl_lines_from()
# call sites in database/routes/sales.py for the forms that do). Buyer is
# also excluded: a Buyer's own control account is now configured directly
# on the Buyer master record's own Chart of Account picker, not here.
SALE_AUTO_CODE_FORMS = {
    'delivery_note', 'sales_invoice', 'sales_return_note', 'sales_credit_memo',
}

# ── Picker: forms for a module (cascades off Module) ────────────
@auto_code_bp.route('/auto-code-selection/forms')
@login_required
def auto_code_forms():
    module_id = request.args.get('module_id', type=int)
    q = SystemForm.query
    if module_id:
        q = q.filter_by(module_id=module_id)
        module = Module.query.get(module_id)
        if module and module.code == 'purchase':
            q = q.filter(SystemForm.code.in_(PURCHASE_AUTO_CODE_FORMS))
        elif module and module.code == 'sale':
            q = q.filter(SystemForm.code.in_(SALE_AUTO_CODE_FORMS))
    rows = q.order_by(SystemForm.sort_order, SystemForm.label_en).all()
    return jsonify([{'id': f.id, 'code': f.code, 'label_en': f.label_en,
                      'label_ar': f.label_ar or ''} for f in rows])


# ── Picker: Level Four accounts (code / drawer_en / drawer_ar) ──
# Optional context on the row, not itself looked up by any posting logic --
# a plain, unrestricted list of every active Level Four heading.
@auto_code_bp.route('/auto-code-selection/level-four')
@login_required
def auto_code_level_four():
    _order = ['A', 'L', 'E', 'R', 'C', 'O', 'F', 'I']

    def _key(r):
        lead = (r.code or '')[:1].upper()
        rank = _order.index(lead) if lead in _order else len(_order)
        return (rank, r.code or '')

    rows = sorted(LevelFour.query.filter(LevelFour.status == 'active').all(), key=_key)
    return jsonify([{
        'code': r.code,
        'drawer_en': r.drawers or '',
        'drawer_ar': r.drawers_ar or '',
    } for r in rows])


# ── Picker: Level Five accounts (code / drawer_en / drawer_ar) ──
# Cascades directly off whichever Level Four code the admin has typed/
# picked -- pass it as `levelfour_code` and only its own children come
# back; leave it out (Level Four not set yet) for the full, unrestricted
# list.
@auto_code_bp.route('/auto-code-selection/level-five')
@login_required
def auto_code_level_five():
    _order = ['A', 'L', 'E', 'R', 'C', 'O', 'F', 'I']

    def _key(r):
        lead = (r.code or '')[:1].upper()
        rank = _order.index(lead) if lead in _order else len(_order)
        return (rank, r.code or '')

    # Control accounts (control_account == 'Yes') are excluded -- those are
    # subsidiary-ledger accounts meant to be posted per supplier/customer,
    # not a generic module-level default. Auto Code Selection's offsetting
    # entry should always be a regular account (e.g. "Other Credit - Goods
    # Received Not Invoiced").
    q = LevelFive.query.filter(
        LevelFive.status == 'active',
        LevelFive.control_account != 'Yes',
    )

    l4_code = (request.args.get('levelfour_code') or '').strip()
    if l4_code:
        q = q.filter(LevelFive.level_four_code == l4_code)

    rows = sorted(q.all(), key=_key)
    return jsonify([{
        'code': r.code,
        'drawer_en': r.drawers or '',
        'drawer_ar': r.drawers_ar or '',
    } for r in rows])


def _validate():
    module_id = request.form.get('module_id', type=int)
    form_id   = request.form.get('form_id', type=int)
    lv_code   = (request.form.get('levelfive_code', '') or '').strip()
    nature    = (request.form.get('nature', '') or '').strip()
    l4_code   = (request.form.get('levelfour_code', '') or '').strip()

    if not module_id:
        return None, _t('Module is required.', 'الوحدة مطلوبة.')
    if not form_id:
        return None, _t('Form is required.', 'النموذج مطلوب.')
    if not lv_code:
        return None, _t('Level Five Code is required.', 'كود المستوى الخامس مطلوب.')
    if nature not in ('Debit', 'Credit'):
        return None, _t('Nature must be Debit or Credit.', 'الطبيعة يجب أن تكون مدين أو دائن.')
    if not Module.query.get(module_id):
        return None, _t('Selected module does not exist.', 'الوحدة المختارة غير موجودة.')
    if not SystemForm.query.get(form_id):
        return None, _t('Selected form does not exist.', 'النموذج المختار غير موجود.')

    l4_drawer_en = (request.form.get('levelfour_drawer_en', '') or '').strip()
    l4_drawer_ar = (request.form.get('levelfour_drawer_ar', '') or '').strip()

    # Level Five must be a child of whichever Level Four is on this row
    # (once Level Four is set at all) -- mirrors the picker's own
    # `levelfour_code` filter, so a stale Level Five value from before
    # Level Four changed can never slip through.
    if l4_code:
        acc = LevelFive.query.filter_by(code=lv_code).first()
        if not acc or acc.level_four_code != l4_code:
            return None, _t(
                f'Level Five Code must be under Level Four {l4_code}.',
                f'يجب أن يكون كود المستوى الخامس ضمن المستوى الرابع {l4_code}.')

    return {
        'module_id': module_id,
        'form_id': form_id,
        'levelfour_code': l4_code,
        'levelfour_drawer_en': l4_drawer_en,
        'levelfour_drawer_ar': l4_drawer_ar,
        'levelfive_code': lv_code,
        'levelfive_drawer_en': (request.form.get('levelfive_drawer_en', '') or '').strip(),
        'levelfive_drawer_ar': (request.form.get('levelfive_drawer_ar', '') or '').strip(),
        'nature': nature,
    }, None


# ── Create ──────────────────────────────────────────────────────
@auto_code_bp.route('/auto-code-selection/add', methods=['POST'])
@login_required
@block_trial_write_json
def auto_code_add():
    data, err = _validate()
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    try:
        row = AutoCodeSelection(created_by=current_user.id, **data)
        db.session.add(row)
        db.session.commit()
        return jsonify({'ok': True, 'id': row.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Update ──────────────────────────────────────────────────────
@auto_code_bp.route('/auto-code-selection/<int:id>/edit', methods=['POST'])
@login_required
def auto_code_edit(id):
    row = AutoCodeSelection.query.get_or_404(id)
    data, err = _validate()
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    try:
        for k, v in data.items():
            setattr(row, k, v)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Delete ──────────────────────────────────────────────────────
@auto_code_bp.route('/auto-code-selection/<int:id>/delete', methods=['POST'])
@login_required
@block_trial_write_json
def auto_code_delete(id):
    row = AutoCodeSelection.query.get_or_404(id)
    try:
        db.session.delete(row)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
