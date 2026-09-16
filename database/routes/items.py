"""Lookups: items routes.

Split out of the original monolithic lookups.py.
Routes register on the shared lookups_bp, so endpoint names are unchanged
(url_for('lookups.list_buyers') etc. keep working).
"""
from flask import (render_template, request, jsonify,
                   redirect, url_for, flash, session)
from flask_login import login_required

from models import db
from .lookups import lookups_bp, admin_required, _t


# ══════════════════════════════════════════════════════════════════
# ITEM MASTER
# ══════════════════════════════════════════════════════════════════
import urllib.request, urllib.parse, json as _json
from models import ItemMaster, ItemCategory, ItemSubCategory, SupplierMaster, LevelFive, StoreTransaction

@lookups_bp.route('/items/<path:item_code>/stock')
@login_required
def item_stock(item_code):
    """Current on-hand stock balance for one item -- same query shape as
    _check_dn_stock_availability() in database/routes/sales.py, exposed so
    forms that let a user freely pick an item (not derived from a locked
    parent document) can warn about insufficient stock up front."""
    balance = (db.session.query(db.func.coalesce(db.func.sum(StoreTransaction.quantity), 0))
               .filter(StoreTransaction.item_code == item_code, StoreTransaction.status == 'Active')
               .scalar())
    return jsonify({'item_code': item_code, 'stock_available': float(balance or 0)})

@lookups_bp.route('/items')
@login_required
def items_list():
    cats    = ItemCategory.query.order_by(ItemCategory.name_en).all()
    subcats = ItemSubCategory.query.order_by(ItemSubCategory.name_en).all()
    suppliers = SupplierMaster.query.order_by(SupplierMaster.supplier_name_en).all()
    uoms    = ['unit','hour','day','month','kg','gram','meter','liter','box','piece','set','pair','dozen']
    return render_template('item_master/items.html',
        cats=cats, subcats=subcats, suppliers=suppliers, uoms=uoms)

@lookups_bp.route('/items/data')
@login_required
def items_data():
    return jsonify([i.to_dict() for i in ItemMaster.query.order_by(ItemMaster.id.desc()).all()])

@lookups_bp.route('/items/<int:id>/json')
@login_required
def item_json(id):
    return jsonify(ItemMaster.query.get_or_404(id).to_dict())

@lookups_bp.route('/items/level-five-accounts')
@login_required
def item_level_five_accounts():
    """All active Level Five accounts (no Control Account filter), for the
    Item Master Chart-of-Account picker. Sorted by the fixed COA drawer
    order (A,L,E,R,C,O,F,I) so the list matches the rest of the app."""
    _order = ['A', 'L', 'E', 'R', 'C', 'O', 'F', 'I']

    def _key(r):
        lead = (r.code or '')[:1].upper()
        rank = _order.index(lead) if lead in _order else len(_order)
        return (rank, r.code or '')

    rows = LevelFive.query.filter(LevelFive.status == 'active').all()
    rows = sorted(rows, key=_key)
    return jsonify([{
        'code': r.code,
        'drawer_en': r.drawers or '',
        'drawer_ar': r.drawers_ar or '',
    } for r in rows])

@lookups_bp.route('/items/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def item_delete(id):
    from flask import current_app
    from database.routes.recycle_bin import soft_delete
    try:
        soft_delete('item', id)
        return jsonify({'ok': True})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('[item_delete] id=%s failed: %s', id, exc)
        return jsonify({'ok': False, 'error': 'Delete failed.'}), 500

@lookups_bp.route('/items/categories/data')
@login_required
def item_cats_data():
    return jsonify([c.to_dict() for c in ItemCategory.query.order_by(ItemCategory.name_en).all()])

@lookups_bp.route('/items/categories/add', methods=['POST'])
@login_required
def item_category_add():
    """Quick-add a category from the Item Master form popup."""
    f = request.form
    name_en = f.get('name_en', '').strip()
    if not name_en:
        return jsonify({'ok': False, 'error': 'Category name (EN) is required'}), 400
    cat = ItemCategory(name_en=name_en, name_ar=f.get('name_ar', '').strip() or None)
    db.session.add(cat)
    db.session.commit()
    return jsonify({'ok': True, 'category': cat.to_dict()})

@lookups_bp.route('/items/sub-categories/<int:cat_id>')
@login_required
def item_subcats(cat_id):
    return jsonify([s.to_dict() for s in ItemSubCategory.query.filter_by(category_id=cat_id).all()])

@lookups_bp.route('/items/sub-categories/add', methods=['POST'])
@login_required
def item_subcategory_add():
    """Quick-add a sub-category from the Item Master form popup."""
    f = request.form
    category_id = f.get('category_id')
    name_en = f.get('name_en', '').strip()
    if not category_id:
        return jsonify({'ok': False, 'error': 'Category is required'}), 400
    if not name_en:
        return jsonify({'ok': False, 'error': 'Sub-category name (EN) is required'}), 400
    sub = ItemSubCategory(
        category_id=int(category_id),
        name_en=name_en,
        name_ar=f.get('name_ar', '').strip() or None,
    )
    db.session.add(sub)
    db.session.commit()
    return jsonify({'ok': True, 'sub_category': sub.to_dict()})

# ── Translation API ─────────────────────────────────────────────────
# Uses the free unofficial Google Translate web endpoint (same pattern
# as sellers.py's translate_text route) — no external package needed,
# no API key, no GoogleTranslator import that was previously missing.
@lookups_bp.route('/items/translate', methods=['POST'])
@login_required
def item_translate():
    data      = request.get_json(silent=True) or {}
    text      = (data.get('text') or request.args.get('text') or '').strip()
    direction = data.get('dir') or request.args.get('dir', 'en2ar')

    if not text:
        return jsonify({'ok': True, 'translated': ''})

    src, tgt = ('en', 'ar') if direction == 'en2ar' else ('ar', 'en')

    # Primary: unofficial Google Translate endpoint
    try:
        params = urllib.parse.urlencode({'client': 'gtx', 'sl': src, 'tl': tgt, 'dt': 't', 'q': text})
        url    = f'https://translate.googleapis.com/translate_a/single?{params}'
        req    = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=6) as r:
            result = _json.loads(r.read().decode('utf-8'))
        translated = ''.join(chunk[0] for chunk in result[0] if chunk[0])
        if translated:
            return jsonify({'ok': True, 'translated': translated.strip()})
    except Exception:
        pass

    # Fallback: MyMemory (also free, no key)
    try:
        q   = urllib.parse.quote(text)
        url = f'https://api.mymemory.translated.net/get?q={q}&langpair={src}|{tgt}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            result = _json.loads(r.read().decode())
        translated = result.get('responseData', {}).get('translatedText', '')
        return jsonify({'ok': True, 'translated': translated})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'translated': ''}), 502