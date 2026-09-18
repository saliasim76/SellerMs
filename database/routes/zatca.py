"""ZATCA e-Invoicing settings/onboarding screen -- generates the EC
keypair/CSR, requests the sandbox Compliance CSID (needs a human-supplied
OTP from the Fatoora portal), and tracks onboarding progress. See
database/zatca/engine.py for the underlying cryptography/XML logic and
database/routes/sales.py's sinv_zatca_generate() for where a Posted Sales
Invoice actually gets signed and (optionally) submitted using what's
configured here.
"""
from flask import Blueprint, render_template, request, jsonify, current_app
from flask_login import login_required, current_user
from datetime import datetime
import uuid

from database.routes.rbac import permission_required, permission_required_json
from models import db, Owner, ZatcaSettings, ZatcaCertificateHistory
from database.zatca import engine as zengine

zatca_bp = Blueprint('zatca', __name__)


def _t(en, ar):
    from flask import session
    return ar if session.get('lang') == 'ar' else en


def _customer_scope_id():
    """The current SaaS customer's id, or None for the platform's own
    account (real Super Admin, or a standalone/non-SaaS install) -- see
    ZatcaSettings.customer_id for why this scoping exists."""
    from database.routes.shared import current_saas_customer
    customer = current_saas_customer()
    return customer.id if customer else None


def zatca_settings_query():
    """ZatcaSettings filtered to the signed-in SaaS customer, or to the
    platform's own (customer_id IS NULL) row outside any customer context.
    The single place every route in this file and sales.py's ZATCA
    invoice signing must go through instead of ZatcaSettings.query
    directly, so a trial customer (sharing the platform database with
    every other trial customer) never sees or edits another customer's
    VAT number, CSR, private key, or certificates."""
    return ZatcaSettings.query.filter_by(customer_id=_customer_scope_id())


def _get_or_build_settings():
    """The current customer's one ZatcaSettings row, created (but not yet
    persisted) on first view so the settings page always has something to
    render/edit."""
    settings = zatca_settings_query().first()
    if not settings:
        owner = Owner.query.first()
        settings = ZatcaSettings(
            customer_id=_customer_scope_id(),
            owner_id=owner.id if owner else None,
            csr_organization_identity=(owner.vat_number if owner else '') or '',
            csr_organization_name=(owner.name if owner else '') or '',
        )
    return settings


@zatca_bp.route('/zatca/settings')
@login_required
@permission_required('zatca', 'zatca_settings', 'view')
def zatca_settings_page():
    settings = _get_or_build_settings()
    history = []
    if settings.id:
        history = (ZatcaCertificateHistory.query
                   .filter_by(zatca_settings_id=settings.id)
                   .order_by(ZatcaCertificateHistory.id.desc()).all())
    owner = Owner.query.first()
    live_enabled = bool(current_app.config.get('ZATCA_LIVE_CALLS_ENABLED'))
    return render_template('zatca/settings.html', settings=settings, history=history,
                           owner=owner, live_enabled=live_enabled)


@zatca_bp.route('/zatca/settings/save-environment', methods=['POST'])
@login_required
@permission_required_json('zatca', 'zatca_settings', 'edit')
def zatca_save_environment():
    settings = zatca_settings_query().first()
    if not settings:
        settings = ZatcaSettings(customer_id=_customer_scope_id())
        db.session.add(settings)

    owner = Owner.query.first()
    settings.owner_id = owner.id if owner else None
    settings.environment = request.form.get('environment', 'sandbox').strip() or 'sandbox'

    # CSR subject fields are only editable before a CSR has actually been
    # generated -- once generated, the CSR itself is the record of truth
    # and must never silently drift from what was submitted to ZATCA.
    if not settings.csr_pem:
        settings.csr_common_name = request.form.get('csr_common_name', '').strip()
        settings.csr_organization_identity = (owner.vat_number if owner else '') or ''
        settings.csr_organization_unit = request.form.get('csr_organization_unit', '').strip()
        settings.csr_organization_name = (owner.name if owner else '') or ''
        settings.csr_country = request.form.get('csr_country', 'SA').strip() or 'SA'
        settings.csr_invoice_type = request.form.get('csr_invoice_type', '1100').strip() or '1100'
        settings.csr_location = request.form.get('csr_location', '').strip()
        settings.csr_industry = request.form.get('csr_industry', '').strip()

    settings.updated_at = datetime.utcnow()
    settings.updated_by = current_user.id
    db.session.commit()
    return jsonify({'ok': True, 'settings': settings.to_dict()})


@zatca_bp.route('/zatca/settings/generate-csr', methods=['POST'])
@login_required
@permission_required_json('zatca', 'zatca_settings', 'edit')
def zatca_generate_csr():
    settings = zatca_settings_query().first()
    if not settings:
        return jsonify({'ok': False, 'error': _t(
            'Save the environment/CSR subject fields first.',
            'يرجى حفظ بيانات البيئة وموضوع الشهادة أولاً.')}), 400
    if settings.csr_pem:
        return jsonify({'ok': False, 'error': _t(
            'A CSR already exists. Onboarding does not support regenerating it once issued.',
            'توجد بالفعل طلب توقيع شهادة (CSR). لا يمكن إعادة إنشائه بعد إصداره.')}), 400

    # EGS serial number is ZATCA's own auto-generated device identifier
    # (its spec: "Automatically filled and not by the taxpayer"), format
    # "1-<solution name>|2-<version>|3-<uuid>" -- generated once and then
    # kept stable across CSR regenerations for this settings row.
    if not settings.csr_serial_number:
        settings.csr_serial_number = f'1-SellerMs|2-1.0|3-{uuid.uuid4()}'

    try:
        private_key, public_key = zengine.generate_keypair()
        subject = {
            'common_name': settings.csr_common_name,
            'organization_identity': settings.csr_organization_identity,
            'organization_unit': settings.csr_organization_unit,
            'organization_name': settings.csr_organization_name,
            'country': settings.csr_country,
            'serial_number': settings.csr_serial_number,
            'invoice_type': settings.csr_invoice_type,
            'location': settings.csr_location,
            'industry': settings.csr_industry,
        }
        csr_pem = zengine.build_csr(private_key, subject, environment=settings.environment or 'sandbox')
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Failed to generate keypair/CSR: {e}'}), 500

    settings.private_key_pem_enc = zengine.encrypt_secret(
        zengine.private_key_to_pem(private_key), current_app.config['SECRET_KEY'])
    settings.public_key_pem = zengine.public_key_to_pem(public_key)
    settings.cert_public_key_b64 = zengine.public_key_raw_b64(public_key)
    settings.csr_pem = csr_pem
    settings.onboarding_stage = 'csr_generated'
    settings.updated_at = datetime.utcnow()
    settings.updated_by = current_user.id
    db.session.commit()
    return jsonify({'ok': True, 'csr_pem': csr_pem, 'settings': settings.to_dict()})


@zatca_bp.route('/zatca/settings/clear-csr', methods=['POST'])
@login_required
@permission_required_json('zatca', 'zatca_settings', 'edit')
def zatca_clear_csr():
    """Wipes the current keypair/CSR (generate-csr refuses to run again
    while one already exists) so a corrected or fresh one can be
    generated -- the counterpart to that guard. Deliberately narrow: does
    NOT touch an already-issued compliance/production CSID, the ICV/
    invoice-hash chain, or the CSR subject fields (company name, VAT,
    location, ...), so generating again immediately after reuses the same
    subject data and EGS serial number without retyping anything."""
    settings = zatca_settings_query().first()
    if not settings or not settings.csr_pem:
        return jsonify({'ok': False, 'error': _t(
            'There is no CSR to clear.', 'لا يوجد طلب توقيع شهادة (CSR) لمسحه.')}), 400

    settings.private_key_pem_enc = None
    settings.public_key_pem = None
    settings.cert_public_key_b64 = None
    settings.csr_pem = None
    settings.onboarding_stage = 'not_started'
    settings.updated_at = datetime.utcnow()
    settings.updated_by = current_user.id
    db.session.commit()
    return jsonify({'ok': True, 'settings': settings.to_dict()})


@zatca_bp.route('/zatca/settings/csr/download')
@login_required
@permission_required('zatca', 'zatca_settings', 'view')
def zatca_csr_download():
    from flask import Response, abort
    settings = zatca_settings_query().first()
    if not settings or not settings.csr_pem:
        abort(404)
    return Response(settings.csr_pem, mimetype='application/pkcs10',
                    headers={'Content-Disposition': 'attachment; filename=zatca.csr'})


@zatca_bp.route('/zatca/settings/history/<int:id>/download')
@login_required
@permission_required('zatca', 'zatca_settings', 'view')
def zatca_history_download(id):
    """Download one certificate history row's actual certificate as a
    standard .pem file -- re-encoded from whatever single/double-base64
    form ZATCA's API or a manual import stored it in, so the downloaded
    file opens directly in a certificate viewer/openssl/browser import
    rather than handing back an opaque stored text blob."""
    from flask import Response, abort
    from werkzeug.utils import secure_filename
    settings = zatca_settings_query().first()
    if not settings:
        abort(404)
    hist = ZatcaCertificateHistory.query.filter_by(id=id, zatca_settings_id=settings.id).first()
    if not hist:
        abort(404)
    pem_bytes = zengine.certificate_to_pem_bytes(hist.csid_binary) if hist.csid_binary else None
    if not pem_bytes:
        abort(404)
    label = hist.common_name or hist.request_id or str(hist.id)
    fname = secure_filename(f'{hist.cert_type}_{label}') or f'certificate_{hist.id}'
    return Response(pem_bytes, mimetype='application/x-pem-file',
                    headers={'Content-Disposition': f'attachment; filename="{fname}.pem"'})


@zatca_bp.route('/zatca/settings/request-compliance-csid', methods=['POST'])
@login_required
@permission_required_json('zatca', 'zatca_settings', 'edit')
def zatca_request_compliance_csid():
    """Calls ZATCA's Compliance CSID endpoint using a human-supplied OTP
    from the Fatoora portal. Written to spec; its actual success cannot be
    verified without a real OTP -- see the implementation plan."""
    settings = zatca_settings_query().first()
    if not settings or not settings.csr_pem:
        return jsonify({'ok': False, 'error': _t(
            'Generate a CSR first.', 'يرجى إنشاء طلب توقيع الشهادة (CSR) أولاً.')}), 400
    otp = (request.form.get('otp') or '').strip()
    if not otp:
        return jsonify({'ok': False, 'error': _t(
            'Enter the OTP you obtained from the Fatoora portal.',
            'أدخل رمز التحقق (OTP) الذي حصلت عليه من بوابة فاتورة.')}), 400

    try:
        result = zengine.request_compliance_csid(settings.environment or 'sandbox', settings.csr_pem, otp)
    except Exception as e:
        return jsonify({'ok': False, 'error': _t(
            f'ZATCA Compliance CSID request failed: {e}',
            f'فشل طلب شهادة الامتثال من زاتكا: {e}')}), 502

    binary_token = result.get('binarySecurityToken')
    secret = result.get('secret')
    request_id = result.get('requestID') or result.get('requestId')
    if not binary_token or not secret:
        return jsonify({'ok': False, 'error': _t(
            'ZATCA did not return a usable certificate. Check the OTP and try again.',
            'لم يُرجع زاتكا شهادة صالحة للاستخدام. تحقق من رمز التحقق وحاول مرة أخرى.'),
            'raw_response': result}), 502

    settings.compliance_request_id = request_id
    settings.compliance_csid_binary = binary_token
    settings.compliance_csid_secret_enc = zengine.encrypt_secret(secret, current_app.config['SECRET_KEY'])
    settings.compliance_issued_at = datetime.utcnow()
    settings.onboarding_stage = 'compliance_csid_issued'
    ca_sig = zengine.extract_cert_ca_signature_b64(binary_token)
    if ca_sig:
        settings.cert_ca_signature_b64 = ca_sig
    settings.updated_at = datetime.utcnow()
    settings.updated_by = current_user.id
    db.session.add(ZatcaCertificateHistory(
        zatca_settings_id=settings.id, cert_type='compliance', environment=settings.environment,
        request_id=request_id, csid_binary=binary_token,
        csid_secret_enc=settings.compliance_csid_secret_enc, csr_pem_snapshot=settings.csr_pem,
        status='active', created_by=current_user.id,
    ))
    db.session.commit()
    return jsonify({'ok': True, 'settings': settings.to_dict()})


@zatca_bp.route('/zatca/settings/import-certificate', methods=['POST'])
@login_required
@permission_required_json('zatca', 'zatca_settings', 'edit')
def zatca_import_certificate():
    """Import a certificate/secret/private key obtained OUTSIDE this
    system's own CSR flow -- e.g. a live Compliance or Production CSID a
    taxpayer already holds from a prior onboarding elsewhere. Bypasses
    "Generate CSR" entirely: the private key supplied here is what gets
    used to sign every subsequent invoice, so it (not just the
    certificate) is required and must be the exact key ZATCA issued this
    certificate against -- pasting a certificate without its matching
    private key cannot work, since the two are a cryptographic pair."""
    settings = _get_or_build_settings()
    if not settings.id:
        db.session.add(settings)
        db.session.flush()

    cert_type = (request.form.get('cert_type') or 'compliance').strip()
    if cert_type not in ('compliance', 'production'):
        cert_type = 'compliance'
    environment = (request.form.get('environment') or settings.environment or 'sandbox').strip()
    binary_token = (request.form.get('binary_token') or '').strip()
    secret = (request.form.get('secret') or '').strip()
    private_key_pem = (request.form.get('private_key_pem') or '').strip()
    csr_pem = (request.form.get('csr_pem') or '').strip()
    request_id = (request.form.get('request_id') or '').strip()

    if not binary_token or not secret or not private_key_pem:
        return jsonify({'ok': False, 'error': _t(
            'Certificate, secret, and the matching private key are all required.',
            'الشهادة والسر والمفتاح الخاص المطابق كلها مطلوبة.')}), 400

    try:
        private_key = zengine.load_private_key_from_pem(private_key_pem)
        public_key = private_key.public_key()
    except Exception as e:
        return jsonify({'ok': False, 'error': _t(
            f'The private key could not be read: {e}',
            f'تعذّرت قراءة المفتاح الخاص: {e}')}), 400

    settings.environment = environment
    settings.private_key_pem_enc = zengine.encrypt_secret(private_key_pem, current_app.config['SECRET_KEY'])
    settings.public_key_pem = zengine.public_key_to_pem(public_key)
    settings.cert_public_key_b64 = zengine.public_key_raw_b64(public_key)
    # Never overwrite a CSR this system already generated for itself --
    # only fill it in when nothing is on file yet, so the "Keypair & CSR"
    # card and onboarding stepper can reflect it too.
    if csr_pem and not settings.csr_pem:
        settings.csr_pem = csr_pem
        if settings.onboarding_stage in (None, 'not_started'):
            settings.onboarding_stage = 'csr_generated'

    ca_sig = zengine.extract_cert_ca_signature_b64(binary_token)
    if ca_sig:
        settings.cert_ca_signature_b64 = ca_sig

    secret_enc = zengine.encrypt_secret(secret, current_app.config['SECRET_KEY'])
    if cert_type == 'production':
        settings.production_request_id = request_id or None
        settings.production_csid_binary = binary_token
        settings.production_csid_secret_enc = secret_enc
        settings.production_issued_at = datetime.utcnow()
        settings.onboarding_stage = 'production_csid_issued'
    else:
        settings.compliance_request_id = request_id or None
        settings.compliance_csid_binary = binary_token
        settings.compliance_csid_secret_enc = secret_enc
        settings.compliance_issued_at = datetime.utcnow()
        if settings.onboarding_stage in (None, 'not_started', 'csr_generated'):
            settings.onboarding_stage = 'compliance_csid_issued'

    settings.updated_at = datetime.utcnow()
    settings.updated_by = current_user.id
    db.session.add(ZatcaCertificateHistory(
        zatca_settings_id=settings.id, cert_type=cert_type, environment=environment,
        request_id=request_id, csid_binary=binary_token, csid_secret_enc=secret_enc,
        csr_pem_snapshot=csr_pem or settings.csr_pem, status='active',
        notes='Imported manually (existing certificate, not generated via this system\'s CSR flow)',
        created_by=current_user.id,
    ))
    db.session.commit()
    return jsonify({'ok': True, 'settings': settings.to_dict()})


@zatca_bp.route('/zatca/settings/run-compliance-checks', methods=['POST'])
@login_required
@permission_required_json('zatca', 'zatca_settings', 'edit')
def zatca_run_compliance_checks():
    if not current_app.config.get('ZATCA_LIVE_CALLS_ENABLED'):
        return jsonify({'ok': False, 'error': _t(
            'Live ZATCA calls are disabled until Phase 2 is enabled for this deployment.',
            'المكالمات المباشرة مع زاتكا معطلة حتى يتم تفعيل المرحلة الثانية لهذا النظام.')}), 400
    return jsonify({'ok': False, 'error': 'Not implemented in this phase.'}), 501


@zatca_bp.route('/zatca/settings/request-production-csid', methods=['POST'])
@login_required
@permission_required_json('zatca', 'zatca_settings', 'edit')
def zatca_request_production_csid():
    if not current_app.config.get('ZATCA_LIVE_CALLS_ENABLED'):
        return jsonify({'ok': False, 'error': _t(
            'Live ZATCA calls are disabled until Phase 2 is enabled for this deployment.',
            'المكالمات المباشرة مع زاتكا معطلة حتى يتم تفعيل المرحلة الثانية لهذا النظام.')}), 400
    return jsonify({'ok': False, 'error': 'Not implemented in this phase.'}), 501


@zatca_bp.route('/zatca/settings/data')
@login_required
@permission_required_json('zatca', 'zatca_settings', 'view')
def zatca_settings_data():
    settings = zatca_settings_query().first()
    return jsonify(settings.to_dict() if settings else {'onboarding_stage': 'not_started'})
