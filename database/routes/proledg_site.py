"""Phase 4: Proledg's public landing page + trial signup, merged into
SellerMs as a blueprint -- one application, one process, one codebase.

Templates/static live under templates/proledg/ and static/proledg/ (moved
here verbatim from the old standalone Proledg app, with url_for()/extends
references namespaced to this blueprint) since both collide by filename
with SellerMs's own ERP templates/static (base.html, static/css/style.css).

The old standalone app (D:\\Project\\Proledg\\landing page\\app.py) is no
longer run -- this blueprint is the only way the site is served now.
"""
import secrets
import string
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash

from models import db, Customer, Subscription, SubscriptionModule, SaasModule
from database.routes.saas_internal import provision_trial_user_direct

proledg_bp = Blueprint(
    'proledg', __name__,
    template_folder='../../templates/proledg',
    static_folder='../../static/proledg',
    static_url_path='/proledg-static',
)

CONTACT_EMAIL = "support@proledge.com"
CONTACT_LOCATION = "Worldwide — serving customers globally"


@proledg_bp.context_processor
def inject_globals():
    return dict(contact_email=CONTACT_EMAIL, contact_location=CONTACT_LOCATION)


@proledg_bp.route("/")
def home():
    return render_template("index.html", active_page="home")


@proledg_bp.route("/features")
def features():
    return render_template("features.html", active_page="features")


@proledg_bp.route("/modules")
def modules():
    return render_template("modules.html", active_page="modules")


@proledg_bp.route("/pricing")
def pricing():
    saas_modules = (
        SaasModule.query.filter_by(status="active")
        .order_by(SaasModule.display_order, SaasModule.module_name_en)
        .all()
    )
    return render_template("pricing.html", active_page="pricing", saas_modules=saas_modules)


@proledg_bp.route("/security")
def security():
    return render_template("security.html", active_page="security")


@proledg_bp.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("full_name", "").strip()
        email = request.form.get("work_email", "").strip()

        if not name or not email:
            flash("Please fill in your name and work email.", "error")
        else:
            # Placeholder: wire this up to an email/CRM integration later.
            print(f"[contact form] {name} <{email}> — {request.form.get('message', '')}")
            flash("Thanks — we've got your message and will be in touch shortly.", "success")
        return redirect(url_for("proledg.contact"))

    return render_template("contact.html", active_page="contact")


def _generate_temp_password():
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(12))


@proledg_bp.route("/trial-signup", methods=["GET", "POST"])
def trial_signup():
    preselected = [m for m in request.args.get("modules", "").split(",") if m]

    if request.method == "GET":
        saas_modules = (
            SaasModule.query.filter_by(status="active")
            .order_by(SaasModule.display_order, SaasModule.module_name_en)
            .all()
        )
        return render_template(
            "trial_signup.html",
            active_page="pricing",
            saas_modules=saas_modules,
            preselected=preselected,
        )

    company_name = request.form.get("company_name", "").strip()
    customer_name = request.form.get("customer_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    mobile = request.form.get("mobile", "").strip()
    selected_codes = request.form.getlist("modules")

    if not company_name or not customer_name or not email:
        flash("Please fill in your company name, your name and email.", "error")
        return redirect(url_for("proledg.trial_signup", modules=",".join(selected_codes)))

    if Customer.query.filter_by(email=email).first():
        flash("An account with this email already exists. Please log in instead.", "error")
        return redirect(url_for("proledg.trial_signup", modules=",".join(selected_codes)))

    # Only modules the Super Admin has flagged trial_available may be
    # granted on a trial signup, even if the request tried to include more.
    trial_modules = (
        SaasModule.query.filter(
            SaasModule.module_code.in_(selected_codes),
            SaasModule.status == "active",
            SaasModule.trial_available.is_(True),
        ).all()
        if selected_codes
        else []
    )

    temp_password = _generate_temp_password()

    customer = Customer(
        customer_name=customer_name,
        company_name=company_name,
        email=email,
        mobile=mobile,
        account_status="trial",
    )
    db.session.add(customer)
    db.session.flush()

    subscription = Subscription(
        customer_id=customer.id,
        billing_cycle="trial",
        start_date=datetime.utcnow(),
        end_date=datetime.utcnow() + timedelta(days=14),
        status="trial",
        total_amount=0,
    )
    db.session.add(subscription)
    db.session.flush()

    for m in trial_modules:
        db.session.add(SubscriptionModule(
            subscription_id=subscription.id,
            module_id=m.id,
            price=0,
            start_date=datetime.utcnow(),
            end_date=subscription.end_date,
            status="active",
        ))

    try:
        result = provision_trial_user_direct(email, customer_name, mobile, temp_password)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("proledg.trial_signup", modules=",".join(selected_codes)))

    username = result["username"]
    db.session.commit()

    return render_template(
        "trial_success.html",
        active_page="pricing",
        username=username,
        temp_password=temp_password,
        login_url=url_for("auth.login"),
        modules=trial_modules,
    )
