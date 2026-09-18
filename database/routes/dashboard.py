from flask import Blueprint, render_template
from flask_login import login_required
from models import (db, Owner, OwnerDocument, SalesInvoice, PurchaseInvoice,
                     BuyerMaster, SupplierMaster, IncomingPayment, OutgoingPayment,
                     LevelOne, LevelTwo, LevelThree, LevelFour, LevelFive, JournalEntryDetail)
from datetime import datetime, timedelta
from sqlalchemy import func

dashboard_bp = Blueprint('dashboard', __name__)


def _month_bounds(year, month):
    start = datetime(year, month, 1).date()
    end = datetime(year + 1, 1, 1).date() if month == 12 else datetime(year, month + 1, 1).date()
    return start, end


def _last_n_months(n):
    """[(year, month), ...] for the last n months including the current one, oldest first."""
    today = datetime.utcnow().date()
    months = []
    y, m = today.year, today.month
    for _ in range(n):
        months.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(months))


# ── Tab 1: Owner (treasury overview) ─────────────────────────────
def _owner_tab_data():
    # "Liquid cash" = current balance of the two Cash/Bank Level Five accounts
    # (see PAYMENT_MODE_NAME_LEVEL_FOUR in cash_bank.py -- every payment posts
    # to a Level Five account under one of these two Level Four headings).
    cash_codes = [r.code for r in LevelFive.query.filter(
        LevelFive.level_four_code.in_(['A2-07-01', 'A2-07-02'])).all()]
    liquid_cash = 0.0
    if cash_codes:
        td, tc = (db.session.query(func.sum(JournalEntryDetail.debit), func.sum(JournalEntryDetail.credit))
                  .filter(JournalEntryDetail.code.in_(cash_codes)).first())
        liquid_cash = float(td or 0) - float(tc or 0)

    ar_outstanding = float(db.session.query(
        func.sum(SalesInvoice.total_incl_vat - SalesInvoice.paid_amount)
    ).filter(SalesInvoice.posting_status == 'Posted').scalar() or 0)

    ap_outstanding = float(db.session.query(
        func.sum(PurchaseInvoice.total_incl_vat - PurchaseInvoice.paid_amount)
    ).filter(PurchaseInvoice.posting_status == 'Posted').scalar() or 0)

    trend = []
    for (y, m) in _last_n_months(6):
        start, end = _month_bounds(y, m)
        inc = float(db.session.query(func.sum(IncomingPayment.total_amount_including_vat)).filter(
            IncomingPayment.status == 'Posted',
            IncomingPayment.posting_date >= start, IncomingPayment.posting_date < end).scalar() or 0)
        out = float(db.session.query(func.sum(OutgoingPayment.total_amount_including_vat)).filter(
            OutgoingPayment.status == 'Posted',
            OutgoingPayment.posting_date >= start, OutgoingPayment.posting_date < end).scalar() or 0)
        trend.append({'label': start.strftime('%b %Y'), 'incoming': inc, 'outgoing': out})

    incoming_this_month = trend[-1]['incoming'] if trend else 0.0
    outgoing_this_month = trend[-1]['outgoing'] if trend else 0.0

    return {
        'liquid_cash': liquid_cash,
        'ar_outstanding': ar_outstanding,
        'ap_outstanding': ap_outstanding,
        'incoming_this_month': incoming_this_month,
        'outgoing_this_month': outgoing_this_month,
        'net_this_month': incoming_this_month - outgoing_this_month,
        'trend': trend,
    }


# ── Tab 2: Buyer ──────────────────────────────────────────────────
def _buyer_tab_data():
    rows = (db.session.query(
                BuyerMaster.id, BuyerMaster.buyer_name_en,
                func.count(SalesInvoice.sales_invoice_id),
                func.sum(SalesInvoice.total_excl_vat),
                func.sum(SalesInvoice.vat_amount),
                func.sum(SalesInvoice.total_incl_vat),
                func.sum(SalesInvoice.paid_amount))
            .join(SalesInvoice, SalesInvoice.buyer_id == BuyerMaster.id)
            .filter(SalesInvoice.posting_status == 'Posted')
            .group_by(BuyerMaster.id, BuyerMaster.buyer_name_en)
            .all())

    buyers = []
    for (bid, name, cnt, excl, vat, incl, paid) in rows:
        excl, vat, incl, paid = float(excl or 0), float(vat or 0), float(incl or 0), float(paid or 0)
        buyers.append({
            'id': bid, 'name': name or '', 'invoice_count': cnt,
            'total_excl_vat': excl, 'vat_amount': vat, 'total_incl_vat': incl,
            'paid_amount': paid, 'balance': incl - paid,
        })
    buyers.sort(key=lambda b: b['balance'], reverse=True)

    totals = {
        'total_sales': sum(b['total_excl_vat'] for b in buyers),
        'total_vat': sum(b['vat_amount'] for b in buyers),
        'total_outstanding': sum(b['balance'] for b in buyers),
        'total_paid': sum(b['paid_amount'] for b in buyers),
    }
    return {'buyers': buyers, 'top': buyers[:8], 'totals': totals}


# ── Tab 3: Purchase ───────────────────────────────────────────────
def _purchase_tab_data():
    rows = (db.session.query(
                SupplierMaster.id, SupplierMaster.supplier_name_en,
                func.count(PurchaseInvoice.purchase_invoice_id),
                func.sum(PurchaseInvoice.total_excl_vat),
                func.sum(PurchaseInvoice.vat_amount),
                func.sum(PurchaseInvoice.total_incl_vat),
                func.sum(PurchaseInvoice.paid_amount))
            .join(PurchaseInvoice, PurchaseInvoice.supplier_id == SupplierMaster.id)
            .filter(PurchaseInvoice.posting_status == 'Posted')
            .group_by(SupplierMaster.id, SupplierMaster.supplier_name_en)
            .all())

    suppliers = []
    for (sid, name, cnt, excl, vat, incl, paid) in rows:
        excl, vat, incl, paid = float(excl or 0), float(vat or 0), float(incl or 0), float(paid or 0)
        suppliers.append({
            'id': sid, 'name': name or '', 'invoice_count': cnt,
            'total_excl_vat': excl, 'vat_amount': vat, 'total_incl_vat': incl,
            'paid_amount': paid, 'balance': incl - paid,
        })
    suppliers.sort(key=lambda s: s['balance'], reverse=True)

    totals = {
        'total_purchases': sum(s['total_excl_vat'] for s in suppliers),
        'total_vat': sum(s['vat_amount'] for s in suppliers),
        'total_outstanding': sum(s['balance'] for s in suppliers),
        'total_paid': sum(s['paid_amount'] for s in suppliers),
    }
    return {'suppliers': suppliers, 'top': suppliers[:8], 'totals': totals}


# ── Tab 4: VAT Summary ────────────────────────────────────────────
def _vat_tab_data():
    trend = []
    for (y, m) in _last_n_months(12):
        start, end = _month_bounds(y, m)
        output_vat = float(db.session.query(func.sum(SalesInvoice.vat_amount)).filter(
            SalesInvoice.posting_status == 'Posted',
            SalesInvoice.document_date >= start, SalesInvoice.document_date < end).scalar() or 0)
        input_vat = float(db.session.query(func.sum(PurchaseInvoice.vat_amount)).filter(
            PurchaseInvoice.posting_status == 'Posted',
            PurchaseInvoice.document_date >= start, PurchaseInvoice.document_date < end).scalar() or 0)
        trend.append({'label': start.strftime('%b %Y'), 'output_vat': output_vat, 'input_vat': input_vat})

    total_output = sum(t['output_vat'] for t in trend)
    total_input = sum(t['input_vat'] for t in trend)
    return {
        'trend': trend,
        'total_output_vat': total_output,
        'total_input_vat': total_input,
        'net_vat': total_output - total_input,
    }


# ── Tab 5: Financial Statement ────────────────────────────────────
def _financial_statement_tab_data():
    agg = (db.session.query(JournalEntryDetail.code,
                             func.sum(JournalEntryDetail.debit),
                             func.sum(JournalEntryDetail.credit))
           .group_by(JournalEntryDetail.code).all())
    bal_by_code = {code: (float(d or 0), float(c or 0)) for code, d, c in agg}

    l4_by_id = {r.id: r for r in LevelFour.query.all()}
    l3_by_id = {r.id: r for r in LevelThree.query.all()}
    l2_by_id = {r.id: r for r in LevelTwo.query.all()}
    l1_by_id = {r.id: r for r in LevelOne.query.all()}

    # Nature (Level One code: A/L/E/R/C/O/F/I) -> (total debit, total credit)
    # across every Level Five account under it that has any posted activity.
    nature_totals = {}
    for r5 in LevelFive.query.all():
        d, c = bal_by_code.get(r5.code, (0.0, 0.0))
        if not d and not c:
            continue
        r4 = l4_by_id.get(r5.level_four_id)
        r3 = l3_by_id.get(r4.level_three_id) if r4 else None
        r2 = l2_by_id.get(r3.level_two_id) if r3 else None
        r1 = l1_by_id.get(r2.level_one_id) if r2 else None
        nature = r1.code if r1 else '?'
        nd, nc = nature_totals.get(nature, (0.0, 0.0))
        nature_totals[nature] = (nd + d, nc + c)

    def balance(nature, credit_normal=False):
        d, c = nature_totals.get(nature, (0.0, 0.0))
        return (c - d) if credit_normal else (d - c)

    revenue = balance('R', credit_normal=True)
    cost_of_revenue = balance('C')
    operating_cost = balance('O')
    finance_cost = balance('F')
    gross_profit = revenue - cost_of_revenue
    net_profit = gross_profit - operating_cost - finance_cost

    return {
        'revenue': revenue, 'cost_of_revenue': cost_of_revenue,
        'operating_cost': operating_cost, 'finance_cost': finance_cost,
        'gross_profit': gross_profit, 'net_profit': net_profit,
        'assets': balance('A'), 'liabilities': balance('L', credit_normal=True),
        'equity': balance('E', credit_normal=True),
    }


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

    stats = {
        'total': total_owners,
        'active': active_owners,
        'inactive': inactive_owners,
        'new_month': new_this_month,
        'expiring_docs': expiring_docs,
    }

    return render_template(
        'dashboard/index.html',
        stats=stats,
        owner_tab=_owner_tab_data(),
        buyer_tab=_buyer_tab_data(),
        purchase_tab=_purchase_tab_data(),
        vat_tab=_vat_tab_data(),
        fs_tab=_financial_statement_tab_data(),
    )
