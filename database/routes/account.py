"""Self-service account settings: change own password, enroll/disable TOTP
two-factor authentication, and pick a sidebar/main-page color theme.
Distinct from user_management.py, which is the Admin-facing "manage other
users" screen -- nothing here can act on another user's account.
"""
from datetime import datetime
from io import BytesIO
import base64
import os
import re
import uuid

import pyotp
import qrcode
from flask import (Blueprint, render_template, request, redirect, url_for, flash, session,
                    jsonify, current_app, send_from_directory, abort)
from flask_login import login_required, current_user

from models import db, User
from database.routes.audit import log_audit

account_bp = Blueprint('account', __name__, url_prefix='/account')

# Accepts #rgb / #rrggbb / #rrggbbaa or rgb()/rgba() -- nothing else, so a
# theme value can never smuggle extra CSS/HTML through the inline <style>
# block it gets interpolated into.
_COLOR_RE = re.compile(
    r'^(#[0-9a-fA-F]{3,8}'
    r'|rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(,\s*(0|1|0?\.\d+)\s*)?\))$'
)

THEME_FIELDS = ('sidebar_bg', 'sidebar_text', 'sidebar_active_bg', 'sidebar_active_text', 'primary')
OPTIONAL_THEME_FIELDS = ('topbar_bg', 'heading_text')


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


@account_bp.route('/security')
@login_required
def security():
    return render_template('account/security.html')


@account_bp.route('/change-password', methods=['POST'])
@login_required
def change_password():
    current_password = request.form.get('current_password') or ''
    new_password = request.form.get('new_password') or ''
    confirm_password = request.form.get('confirm_password') or ''

    if not current_user.check_password(current_password):
        flash(_t('Current password is incorrect.', 'كلمة المرور الحالية غير صحيحة.'), 'danger')
        return redirect(url_for('account.security'))
    if len(new_password) < 8:
        flash(_t('New password must be at least 8 characters.',
                 'يجب ألا تقل كلمة المرور الجديدة عن 8 أحرف.'), 'danger')
        return redirect(url_for('account.security'))
    if new_password != confirm_password:
        flash(_t('New password and confirmation do not match.',
                 'كلمة المرور الجديدة وتأكيدها غير متطابقين.'), 'danger')
        return redirect(url_for('account.security'))

    current_user.set_password(new_password)
    db.session.commit()
    log_audit('password_change', module_key='account', form_key='security',
              record_id=current_user.id)
    flash(_t('Password updated.', 'تم تحديث كلمة المرور.'), 'success')
    return redirect(url_for('account.security'))


@account_bp.route('/2fa/setup', methods=['GET', 'POST'])
@login_required
def setup_2fa():
    if current_user.totp_enabled:
        flash(_t('Two-factor authentication is already enabled.',
                 'المصادقة الثنائية مفعّلة بالفعل.'), 'info')
        return redirect(url_for('account.security'))

    if request.method == 'POST':
        secret = session.get('pending_totp_secret')
        code = (request.form.get('code') or '').strip()
        if not secret:
            flash(_t('Setup session expired -- start again.',
                     'انتهت جلسة الإعداد — ابدأ من جديد.'), 'danger')
            return redirect(url_for('account.setup_2fa'))
        if pyotp.TOTP(secret).verify(code, valid_window=1):
            current_user.totp_secret = secret
            current_user.totp_enabled = True
            current_user.totp_confirmed_at = datetime.utcnow()
            db.session.commit()
            session.pop('pending_totp_secret', None)
            log_audit('2fa_enable', module_key='account', form_key='security',
                      record_id=current_user.id)
            flash(_t('Two-factor authentication enabled.',
                     'تم تفعيل المصادقة الثنائية.'), 'success')
            return redirect(url_for('account.security'))
        flash(_t('Invalid code -- please try again.', 'رمز غير صحيح — حاول مرة أخرى.'), 'danger')

    secret = session.get('pending_totp_secret')
    if not secret:
        secret = pyotp.random_base32()
        session['pending_totp_secret'] = secret

    uri = pyotp.TOTP(secret).provisioning_uri(name=current_user.username, issuer_name='SellerMS')
    img = qrcode.make(uri)
    buf = BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return render_template('account/setup_2fa.html', secret=secret, qr_b64=qr_b64)


@account_bp.route('/2fa/disable', methods=['POST'])
@login_required
def disable_2fa():
    password = request.form.get('password') or ''
    if not current_user.check_password(password):
        flash(_t('Password is incorrect.', 'كلمة المرور غير صحيحة.'), 'danger')
        return redirect(url_for('account.security'))

    current_user.totp_enabled = False
    current_user.totp_secret = None
    current_user.totp_confirmed_at = None
    db.session.commit()
    log_audit('2fa_disable', module_key='account', form_key='security',
              record_id=current_user.id)
    flash(_t('Two-factor authentication disabled.', 'تم إيقاف المصادقة الثنائية.'), 'success')
    return redirect(url_for('account.security'))


@account_bp.route('/theme/save', methods=['POST'])
@login_required
def theme_save():
    data = request.get_json(silent=True) or {}
    for field in THEME_FIELDS:
        value = (data.get(field) or '').strip()
        if not value or not _COLOR_RE.match(value):
            return jsonify({'ok': False, 'error': f'Invalid color for {field}'}), 400
    # Top bar / page heading were added later than the original 5 sidebar +
    # accent fields -- optional so an older caller sending just those 5
    # still works, but validated the same way whenever they're present.
    for field in OPTIONAL_THEME_FIELDS:
        if field in data:
            value = (data.get(field) or '').strip()
            if not value or not _COLOR_RE.match(value):
                return jsonify({'ok': False, 'error': f'Invalid color for {field}'}), 400
    current_user.theme_sidebar_bg          = data['sidebar_bg'].strip()
    current_user.theme_sidebar_text        = data['sidebar_text'].strip()
    current_user.theme_sidebar_active_bg   = data['sidebar_active_bg'].strip()
    current_user.theme_sidebar_active_text = data['sidebar_active_text'].strip()
    current_user.theme_primary             = data['primary'].strip()
    if 'topbar_bg' in data:
        current_user.theme_topbar_bg = data['topbar_bg'].strip()
    if 'heading_text' in data:
        current_user.theme_heading_text = data['heading_text'].strip()
    db.session.commit()
    return jsonify({'ok': True})


@account_bp.route('/theme/reset', methods=['POST'])
@login_required
def theme_reset():
    current_user.theme_sidebar_bg          = None
    current_user.theme_sidebar_text        = None
    current_user.theme_sidebar_active_bg   = None
    current_user.theme_sidebar_active_text = None
    current_user.theme_primary             = None
    current_user.theme_topbar_bg           = None
    current_user.theme_heading_text        = None
    db.session.commit()
    return jsonify({'ok': True})


_SIGNATURE_EXTENSIONS = {'jpg', 'jpeg', 'png'}


def _allowed_signature_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in _SIGNATURE_EXTENSIONS


@account_bp.route('/signature/save', methods=['POST'])
@login_required
def save_signature():
    """Let the current user upload/replace their own digital signature --
    self-service only, mirrors the password/2FA endpoints above."""
    file = request.files.get('signature')
    if not file or not file.filename or not _allowed_signature_file(file.filename):
        flash(_t('Please choose a JPG or PNG image for your signature.',
                 'يرجى اختيار صورة بصيغة JPG أو PNG للتوقيع.'), 'danger')
        return redirect(url_for('account.security'))

    ext = file.filename.rsplit('.', 1)[1].lower()
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'signatures', str(current_user.id))
    os.makedirs(folder, exist_ok=True)
    unique_name = f'{uuid.uuid4().hex}.{ext}'
    file.save(os.path.join(folder, unique_name))
    current_user.signature_path = os.path.join('signatures', str(current_user.id), unique_name)
    db.session.commit()
    log_audit('signature_save', module_key='account', form_key='security', record_id=current_user.id)
    flash(_t('Signature saved successfully.', 'تم حفظ التوقيع بنجاح.'), 'success')
    return redirect(url_for('account.security'))


@account_bp.route('/signature/<int:user_id>')
@login_required
def view_signature(user_id):
    """Serve a user's saved digital signature image -- readable by any
    logged-in user (not just its owner) so documents that show a signer's
    signature (e.g. a printed quotation) can display it; only the owning
    user can ever change it, via save_signature() above."""
    user = User.query.get_or_404(user_id)
    if not user.signature_path:
        abort(404)
    folder, fname = os.path.split(user.signature_path)
    abs_folder = os.path.join(current_app.config['UPLOAD_FOLDER'], folder)
    if not os.path.exists(os.path.join(abs_folder, fname)):
        abort(404)
    return send_from_directory(abs_folder, fname)


@account_bp.route('/signature/remove', methods=['POST'])
@login_required
def remove_signature():
    current_user.signature_path = None
    db.session.commit()
    flash(_t('Signature removed.', 'تم حذف التوقيع.'), 'success')
    return redirect(url_for('account.security'))
