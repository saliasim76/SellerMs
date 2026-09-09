from datetime import datetime

import pyotp
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User, SaasUserDirectory
from forms import LoginForm, TwoFactorForm
from database.routes.audit import log_login_success, log_login_failed, log_logout

def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en

auth_bp = Blueprint('auth', __name__)


def _complete_login(user, remember):
    login_user(user, remember=remember)
    user.last_login_at = datetime.utcnow()
    user.last_login_ip = request.remote_addr
    user.failed_login_count = 0
    db.session.commit()
    log_login_success(user)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data
        user = User.query.filter_by(username=username).first()
        if not user:
            # Not in the shared database -- check whether this username
            # belongs to a Phase-3-provisioned tenant with its own
            # dedicated database. The login form itself never changes;
            # this only affects which database gets queried.
            entry = SaasUserDirectory.query.filter_by(username=username).first()
            if entry:
                session['tenant_db'] = entry.database_name
                user = User.query.filter_by(username=username).first()

        if user and user.check_password(form.password.data) and user.is_active:
            if user.totp_enabled:
                session['pending_2fa_user_id'] = user.id
                session['pending_2fa_remember'] = bool(form.remember_me.data)
                session['pending_2fa_next'] = request.args.get('next')
                return redirect(url_for('auth.verify_2fa'))
            _complete_login(user, form.remember_me.data)
            next_page = request.args.get('next')
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(next_page or url_for('dashboard.index'))

        # Any failure from here must not leave tenant routing active for
        # this (still-anonymous) session.
        session.pop('tenant_db', None)
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            db.session.commit()
        log_login_failed(username)
        flash(_t('Invalid username or password.', 'اسم المستخدم أو كلمة المرور غير صحيحة'), 'danger')
    return render_template('auth/login.html', form=form)


@auth_bp.route('/login/2fa', methods=['GET', 'POST'])
def verify_2fa():
    user_id = session.get('pending_2fa_user_id')
    if not user_id:
        session.pop('tenant_db', None)
        return redirect(url_for('auth.login'))
    user = User.query.get(user_id)
    if not user or not user.totp_enabled:
        session.pop('pending_2fa_user_id', None)
        session.pop('tenant_db', None)
        return redirect(url_for('auth.login'))

    form = TwoFactorForm()
    if form.validate_on_submit():
        totp = pyotp.TOTP(user.totp_secret)
        if totp.verify(form.code.data.strip(), valid_window=1):
            remember = session.pop('pending_2fa_remember', False)
            next_page = session.pop('pending_2fa_next', None)
            session.pop('pending_2fa_user_id', None)
            _complete_login(user, remember)
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(next_page or url_for('dashboard.index'))
        log_login_failed(user.username)
        flash(_t('Invalid authentication code.', 'رمز التحقق غير صحيح'), 'danger')
    return render_template('auth/verify_2fa.html', form=form)


@auth_bp.route('/logout')
@login_required
def logout():
    log_logout(current_user)
    logout_user()
    session.pop('tenant_db', None)
    flash(_t('You have been logged out.', 'تم تسجيل خروجك بنجاح'), 'info')
    return redirect(url_for('auth.login'))

@auth_bp.route('/set_language/<lang>')
def set_language(lang):
    from flask import current_app
    supported = current_app.config.get('LANGUAGES', {'en': 'English', 'ar': 'العربية'})
    if lang in supported:
        session['lang'] = lang
    return redirect(request.referrer or url_for('dashboard.index'))