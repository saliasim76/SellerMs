from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

from database.tenant_routing import TenantAwareSession

db = SQLAlchemy(session_options={'class_': TenantAwareSession})

# ─────────────────────────────────────────────────────────────────
# USER
# ─────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80),  unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role          = db.Column(db.String(20), default='user')
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    # ── RBAC (additive — the columns above are untouched so every existing
    #    current_user.is_admin()/.role check in the app keeps working as-is) ──
    role_id             = db.Column(db.Integer, db.ForeignKey('user_role.id'))
    is_super_admin      = db.Column(db.Boolean, default=False)
    is_protected        = db.Column(db.Boolean, default=False)
    last_login_at       = db.Column(db.DateTime)
    last_login_ip       = db.Column(db.String(45))
    failed_login_count  = db.Column(db.Integer, default=0)
    full_name           = db.Column(db.String(150))
    mobile              = db.Column(db.String(30))
    signature_path      = db.Column(db.String(500))
    created_by          = db.Column(db.Integer, db.ForeignKey('users.id'))

    # ── Two-factor auth (TOTP, additive) ──
    totp_secret         = db.Column(db.String(64))
    totp_enabled        = db.Column(db.Boolean, default=False)
    totp_confirmed_at   = db.Column(db.DateTime)

    # ── Theme preference (additive; null = use the app default) ──
    theme_sidebar_bg          = db.Column(db.String(20))
    theme_sidebar_text        = db.Column(db.String(20))
    theme_sidebar_active_bg   = db.Column(db.String(40))
    theme_sidebar_active_text = db.Column(db.String(20))
    theme_primary             = db.Column(db.String(20))
    theme_topbar_bg           = db.Column(db.String(20))
    theme_heading_text        = db.Column(db.String(20))

    role_ref = db.relationship('Role', foreign_keys=[role_id], lazy=True)

    def set_password(self, pw):   self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)
    def is_admin(self):           return self.role == 'admin'

    @property
    def has_super_admin_role(self):
        """True if this row's ROLE is Super Admin (so role_permissions
        grants it every permission -- see seed_rbac_catalog()), regardless
        of the separate is_super_admin flag. That flag is only ever True
        for the one real platform account; a SaaS tenant's own local
        SuperAdmin keeps this role but has the flag deliberately set to
        False (see tenant_provisioning.py), so it's never mistaken for
        real platform-level access from outside that tenant's own
        database. Use effectively_super_admin (below) for a within-this-
        database "should this user have full, unrestricted access" check."""
        return bool(self.role_ref and self.role_ref.code == 'super_admin')

    @property
    def effectively_super_admin(self):
        """Full, unrestricted access WITHIN whichever database this row
        lives in -- the real platform Super Admin (is_super_admin=True)
        or a tenant's own local SuperAdmin (has_super_admin_role, flag
        off). Use this for any in-app restriction a Super Admin should
        always bypass (e.g. is_basic_mode() in shared.py). Never use it
        for a genuinely platform-wide gate (SaaS Admin screens, whole-
        database Backup/Restore) that a tenant's own SuperAdmin must not
        reach -- those stay keyed on is_super_admin alone."""
        return bool(self.is_super_admin or self.has_super_admin_role)

    def get_id(self):
        """Flask-Login persists whatever this returns into both the session
        cookie and the long-lived remember-cookie. Encoding the active
        tenant database into it (only when one is active -- see auth.py's
        login()) is what makes a tenant admin's login durable across a
        remember-cookie restore, with zero change for shared-DB users
        (whose id is the exact plain str(self.id) as always)."""
        from flask import session as _flask_session
        tenant_db = _flask_session.get('tenant_db')
        return f"{tenant_db}:{self.id}" if tenant_db else str(self.id)

    def to_dict(self):
        return {'id': self.id, 'username': self.username, 'email': self.email,
                'role': self.role, 'is_active': self.is_active}


# ─────────────────────────────────────────────────────────────────
# OWNER
# ─────────────────────────────────────────────────────────────────
class Owner(db.Model):
    __tablename__ = 'owners'
    id                  = db.Column(db.Integer, primary_key=True)
    owner_code          = db.Column(db.String(20), unique=True)
    name                = db.Column(db.String(200), nullable=False)
    name_ar             = db.Column(db.Unicode(200))
    vat_number          = db.Column(db.String(50))
    crn                 = db.Column(db.String(50))
    phone               = db.Column(db.String(30))
    fax                 = db.Column(db.String(30))
    email               = db.Column(db.String(120))
    website             = db.Column(db.String(200))
    smtp_host           = db.Column(db.String(200))
    smtp_port           = db.Column(db.Integer, default=587)
    smtp_username       = db.Column(db.String(200))
    smtp_password       = db.Column(db.String(300))
    smtp_use_tls        = db.Column(db.Boolean, default=True)
    report_color        = db.Column(db.String(10), default='#2563eb')
    logo_path           = db.Column(db.String(500))
    bg_logo_path        = db.Column(db.String(500))
    header_path         = db.Column(db.String(500))
    footer_path         = db.Column(db.String(500))
    stamp_path          = db.Column(db.String(500))
    sq_default_terms_conditions = db.Column(db.Text)  # Sales Quotation: default Terms & Conditions content for new documents
    sq_default_sign_stamp       = db.Column(db.Text)  # Sales Quotation: default Sign & Stamp content for new documents
    # Which Sales Invoice print/PDF template is active: 'formal' (the
    # default -- detailed bilingual tables, Owner logo/watermark/color),
    # 'classic' (the original bilingual-card design), 'letterhead' (uses
    # header_path/footer_path images instead of the built-in header),
    # 'compact' (a denser single-column layout). Both Print and the PDF
    # download read this same field via sales.py's _sinv_print_html(), so
    # they can never show a different template from one another.
    sinv_print_template = db.Column(db.String(20), default='formal')
    street_name         = db.Column(db.String(200))
    building_number     = db.Column(db.String(50))
    additional_number   = db.Column(db.String(50))
    district            = db.Column(db.String(100))
    city                = db.Column(db.String(100))
    postal_code         = db.Column(db.String(20))
    # National Address "short address" code (e.g. "RRRD2929") -- a fixed
    # alphanumeric code, not bilingual text, so it has no matching _ar
    # column unlike street_name/district/city/country above.
    short_address       = db.Column(db.String(20))
    country             = db.Column(db.String(100), default='Saudi Arabia')
    street_name_ar      = db.Column(db.Unicode(200))
    district_ar         = db.Column(db.Unicode(100))
    city_ar             = db.Column(db.Unicode(100))
    country_ar          = db.Column(db.Unicode(100))
    status              = db.Column(db.String(20), default='active')
    second_language     = db.Column(db.String(30), default='Arabic')
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow)
    created_by          = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Relationships
    banks = db.relationship('OwnerBank', backref='owner', lazy='dynamic',
                            foreign_keys='OwnerBank.owner_id',
                            cascade='all, delete-orphan')
    documents = db.relationship('OwnerDocument', backref='owner', lazy='dynamic',
                                foreign_keys='OwnerDocument.owner_id',
                                cascade='all, delete-orphan')
    creator = db.relationship('User', foreign_keys=[created_by], lazy=True)

    def to_dict(self):
        return {
            'id': self.id, 'owner_code': self.owner_code or '',
            'name': self.name, 'name_ar': self.name_ar or '',
            'vat_number': self.vat_number or '', 'crn': self.crn or '',
            'phone': self.phone or '', 'email': self.email or '',
            'city': self.city or '', 'status': self.status,
            'report_color': self.report_color or '#2563eb',
            'second_language': self.second_language or 'Arabic',
        }


# ─────────────────────────────────────────────────────────────────
# OWNER BANK   (stored in owner_banks)
# ─────────────────────────────────────────────────────────────────
class OwnerBank(db.Model):
    __tablename__ = 'owner_banks'
    id             = db.Column(db.Integer, primary_key=True)
    owner_id       = db.Column(db.Integer, db.ForeignKey('owners.id'), nullable=False)
    bank_name      = db.Column(db.String(150), nullable=False)
    bank_name_ar   = db.Column(db.Unicode(150))
    account_number = db.Column(db.String(50))
    branch         = db.Column(db.String(100))
    branch_ar      = db.Column(db.Unicode(100))
    swift_code     = db.Column(db.String(20))
    iban           = db.Column(db.String(50))
    is_primary     = db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'owner_id': self.owner_id,
            'bank_name': self.bank_name,
            'bank_name_ar': self.bank_name_ar or '',
            'account_number': self.account_number or '',
            'branch': self.branch or '',
            'branch_ar': self.branch_ar or '',
            'swift_code': self.swift_code or '',
            'iban': self.iban or '', 'is_primary': self.is_primary,
        }


# ─────────────────────────────────────────────────────────────────
# OWNER DOCUMENT
# ─────────────────────────────────────────────────────────────────
class OwnerDocument(db.Model):
    __tablename__ = 'owner_documents'
    id            = db.Column(db.Integer, primary_key=True)
    owner_id      = db.Column(db.Integer, db.ForeignKey('owners.id'), nullable=False)
    document_type = db.Column(db.String(100), nullable=False)
    document_name = db.Column(db.String(200), nullable=False)
    file_path     = db.Column(db.String(500), nullable=False)
    file_size     = db.Column(db.Integer)
    issue_date    = db.Column(db.Date)
    expiry_date   = db.Column(db.Date)
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by   = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Relationship to User who uploaded
    uploader = db.relationship('User', foreign_keys=[uploaded_by], lazy=True)


# ─────────────────────────────────────────────────────────────────
# ZATCA (Saudi e-invoicing) INTEGRATION — Phase 2 onboarding + chain state.
# Singleton settings row (one per company, mirrors the Owner singleton
# pattern) + a certificate history audit trail. See database/zatca/engine.py
# for the cryptography/XML logic and database/routes/zatca.py for the
# onboarding screen that populates this. Values here NEVER get hardcoded
# defaults that look like real credentials -- an empty/None field always
# means "onboarding not done yet", checked explicitly wherever it matters.
# ─────────────────────────────────────────────────────────────────
class ZatcaSettings(db.Model):
    __tablename__ = 'zatca_settings'
    id                          = db.Column(db.Integer, primary_key=True)
    # Scopes this row to one SaaS customer. Paid tenants already get their
    # own dedicated database (see tenant_provisioning.py), so this is
    # redundant-but-harmless there -- it matters for trial customers, who
    # all share SellerMs's single platform database: without this column,
    # ZatcaSettings.query.first() would hand every trial customer the same
    # row (VAT number, CSR, private key, certificates), so whichever one
    # onboarded first would silently determine what every other trial
    # customer saw/edited. NULL for the platform's own default install
    # (no SaaS Customer at all, or the platform Super Admin's own account).
    customer_id                 = db.Column(db.Integer)
    owner_id                    = db.Column(db.Integer, db.ForeignKey('owners.id'))
    environment                 = db.Column(db.String(20), default='sandbox')  # sandbox | simulation | production
    onboarding_stage            = db.Column(db.String(30), default='not_started')
    # not_started -> csr_generated -> compliance_csid_issued ->
    # compliance_checks_passed -> production_csid_issued

    # CSR subject fields (organization_identity/organization_name are always
    # re-derived from Owner.vat_number/Owner.name when a CSR is generated --
    # kept as columns here only so the generated CSR can be displayed/audited
    # without re-joining Owner every time).
    csr_common_name             = db.Column(db.String(200))
    csr_serial_number           = db.Column(db.String(100))
    csr_organization_identity   = db.Column(db.String(20))
    csr_organization_unit       = db.Column(db.String(200))
    csr_organization_name       = db.Column(db.String(200))
    csr_country                 = db.Column(db.String(2), default='SA')
    csr_invoice_type            = db.Column(db.String(10), default='1100')
    csr_location                = db.Column(db.String(200))
    csr_industry                = db.Column(db.String(200))

    # Keypair / CSR material. The private key is Fernet-encrypted at rest
    # (see database/zatca/engine.py encrypt_secret/decrypt_secret) -- a
    # deliberate departure from this codebase's existing cleartext-secret
    # convention (e.g. Owner.smtp_password), justified because this key
    # underwrites the legal cryptographic signature on every invoice.
    private_key_pem_enc         = db.Column(db.Text)
    public_key_pem              = db.Column(db.Text)
    csr_pem                     = db.Column(db.Text)

    # Compliance CSID (sandbox onboarding step 1).
    compliance_request_id       = db.Column(db.String(100))
    compliance_csid_binary      = db.Column(db.Text)
    compliance_csid_secret_enc  = db.Column(db.Text)
    compliance_issued_at        = db.Column(db.DateTime)
    compliance_checks_passed    = db.Column(db.Boolean, default=False)

    # Production CSID -- populated only once Phase 2 (live calls) is enabled
    # and compliance checks have passed.
    production_request_id       = db.Column(db.String(100))
    production_csid_binary      = db.Column(db.Text)
    production_csid_secret_enc  = db.Column(db.Text)
    production_issued_at        = db.Column(db.DateTime)

    # Cached from the active certificate -- feed QR tags 8/9 without
    # re-parsing the certificate on every invoice print.
    cert_public_key_b64         = db.Column(db.Text)
    cert_ca_signature_b64       = db.Column(db.Text)

    # Invoice hash chain state -- last_icv is the running Invoice Counter
    # Value (never reused, never reset); last_invoice_hash is the chain tip
    # (None means "use the genesis hash" for the very next invoice).
    last_icv                    = db.Column(db.BigInteger, default=0)
    last_invoice_hash           = db.Column(db.String(200))

    created_at                  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at                  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by                  = db.Column(db.Integer, db.ForeignKey('users.id'))

    def active_csid(self):
        """The CSID currently usable for signing: production if issued,
        else compliance, else None (onboarding not far enough along)."""
        if self.production_csid_binary:
            return 'production', self.production_csid_binary, self.production_csid_secret_enc
        if self.compliance_csid_binary:
            return 'compliance', self.compliance_csid_binary, self.compliance_csid_secret_enc
        return None, None, None

    # Real properties (not just to_dict() keys) so templates that render the
    # ORM object directly -- e.g. zatca/settings.html, which is handed
    # `settings` itself, not settings.to_dict() -- can use settings.has_csr
    # etc. and actually get a real True/False rather than Jinja's silently
    # falsy Undefined for a nonexistent attribute.
    @property
    def has_csr(self):
        return bool(self.csr_pem)

    @property
    def has_compliance_csid(self):
        return bool(self.compliance_csid_binary)

    @property
    def has_production_csid(self):
        return bool(self.production_csid_binary)

    @property
    def has_private_key(self):
        return bool(self.private_key_pem_enc)

    def to_dict(self):
        cert_type, _, _ = self.active_csid()
        return {
            'id': self.id, 'environment': self.environment or 'sandbox',
            'onboarding_stage': self.onboarding_stage or 'not_started',
            'csr_common_name': self.csr_common_name or '',
            'csr_organization_identity': self.csr_organization_identity or '',
            'csr_organization_name': self.csr_organization_name or '',
            'csr_organization_unit': self.csr_organization_unit or '',
            'csr_country': self.csr_country or 'SA',
            'csr_invoice_type': self.csr_invoice_type or '1100',
            'csr_location': self.csr_location or '',
            'csr_industry': self.csr_industry or '',
            'has_csr': self.has_csr,
            'has_private_key': self.has_private_key,
            'has_compliance_csid': self.has_compliance_csid,
            'has_production_csid': self.has_production_csid,
            'active_csid_type': cert_type,
            'compliance_checks_passed': bool(self.compliance_checks_passed),
            'last_icv': int(self.last_icv or 0),
            'updated_at': self.updated_at.strftime('%d/%m/%Y %H:%M') if self.updated_at else '',
        }


class ZatcaCertificateHistory(db.Model):
    """Audit trail of every CSID (re)issued against ZatcaSettings -- expected
    to accumulate repeatedly during sandbox testing/onboarding retries, kept
    forever rather than overwritten so a past certificate's provenance is
    never lost."""
    __tablename__ = 'zatca_certificate_history'
    id                  = db.Column(db.Integer, primary_key=True)
    zatca_settings_id   = db.Column(db.Integer, db.ForeignKey('zatca_settings.id'), nullable=False)
    cert_type           = db.Column(db.String(20), nullable=False)  # compliance | production
    environment         = db.Column(db.String(20))
    request_id          = db.Column(db.String(100))
    csid_binary         = db.Column(db.Text)
    csid_secret_enc     = db.Column(db.Text)
    csr_pem_snapshot    = db.Column(db.Text)
    issued_at           = db.Column(db.DateTime, default=datetime.utcnow)
    revoked_at          = db.Column(db.DateTime)
    status              = db.Column(db.String(20), default='active')  # active | superseded | revoked
    notes               = db.Column(db.Text)
    created_by          = db.Column(db.Integer, db.ForeignKey('users.id'))

    @property
    def common_name(self):
        """The certificate's Subject Common Name, parsed from the stored
        binary token -- lets a certificate history list show a real,
        human-meaningful label (e.g. "1234567890 - 05172026 - Live")
        instead of just its type and date. None if csid_binary is empty
        or doesn't parse as a certificate."""
        if not self.csid_binary:
            return None
        from database.zatca import engine as zengine
        return zengine.certificate_common_name(self.csid_binary)

    def to_dict(self):
        return {
            'id': self.id, 'cert_type': self.cert_type, 'environment': self.environment or '',
            'request_id': self.request_id or '', 'status': self.status or 'active',
            'issued_at': self.issued_at.strftime('%d/%m/%Y %H:%M') if self.issued_at else '',
            'revoked_at': self.revoked_at.strftime('%d/%m/%Y %H:%M') if self.revoked_at else '',
            'notes': self.notes or '', 'common_name': self.common_name or '',
            'has_binary': bool(self.csid_binary),
        }


class ActivityLog(db.Model):
    """The app's audit log. Kept under its original name/table (owners.py
    already writes to it via log_activity()) and extended in place rather
    than replaced, so nothing that already uses it needs to change."""
    __tablename__ = 'activity_logs'
    id         = db.Column(db.Integer, primary_key=True)
    # Nullable: a failed login against a username that doesn't exist has no
    # real user to attach to, but must still be logged (username_snapshot
    # below carries what was typed).
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action     = db.Column(db.String(50), nullable=False)
    target     = db.Column(db.String(50))
    target_id  = db.Column(db.Integer)
    detail     = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ── Audit log extensions (additive) ──
    old_value         = db.Column(db.Text)
    new_value         = db.Column(db.Text)
    status            = db.Column(db.String(20), default='success')
    username_snapshot = db.Column(db.String(80))


# ═════════════════════════════════════════════════════════════════
#  ROLE & PERMISSION SYSTEM
#  Module -> Form -> Permission(action) catalog, with role-level grants
#  (role_permissions) and per-user overrides (user_permissions) layered
#  on top. See database/routes/rbac.py for the seeding + resolution logic.
# ═════════════════════════════════════════════════════════════════
class Role(db.Model):
    __tablename__ = 'user_role'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(80), nullable=False)
    code        = db.Column(db.String(30), unique=True, nullable=False)  # super_admin/admin/power_user/user
    description = db.Column(db.String(300))
    is_system   = db.Column(db.Boolean, default=False)   # system roles can't be deleted from the Role UI
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'code': self.code,
                'description': self.description or '', 'is_system': self.is_system}


class Module(db.Model):
    __tablename__ = 'auto_modules'
    id         = db.Column(db.Integer, primary_key=True)
    code       = db.Column(db.String(50), unique=True, nullable=False)
    label_en   = db.Column(db.String(100), nullable=False)
    label_ar   = db.Column(db.Unicode(100))
    sort_order = db.Column(db.Integer, default=0)

    forms = db.relationship('SystemForm', backref='module_ref', lazy='dynamic',
                            cascade='all, delete-orphan')


class SystemForm(db.Model):
    """A form/page within a module. Named SystemForm (not Form) to avoid
    clashing with WTForms Form classes / the HTML <form> concept."""
    __tablename__ = 'auto_form'
    id         = db.Column(db.Integer, primary_key=True)
    module_id  = db.Column(db.Integer, db.ForeignKey('auto_modules.id'), nullable=False)
    code       = db.Column(db.String(50), nullable=False)
    label_en   = db.Column(db.String(100), nullable=False)
    label_ar   = db.Column(db.Unicode(100))
    sort_order = db.Column(db.Integer, default=0)
    __table_args__ = (db.UniqueConstraint('module_id', 'code', name='uq_form_module_code'),)

    permissions = db.relationship('Permission', backref='form_ref', lazy='dynamic',
                                  cascade='all, delete-orphan')


class Permission(db.Model):
    """One catalog row per (form, action) pair -- NOT a grant by itself.
    Grants live in role_permissions / user_permissions below."""
    __tablename__ = 'user_permission_master'
    id          = db.Column(db.Integer, primary_key=True)
    form_id     = db.Column(db.Integer, db.ForeignKey('auto_form.id'), nullable=False)
    action_code = db.Column(db.String(30), nullable=False)
    description = db.Column(db.String(200))
    __table_args__ = (db.UniqueConstraint('form_id', 'action_code', name='uq_perm_form_action'),)


class RolePermission(db.Model):
    __tablename__ = 'user_role_permission'
    id            = db.Column(db.Integer, primary_key=True)
    role_id       = db.Column(db.Integer, db.ForeignKey('user_role.id'), nullable=False)
    permission_id = db.Column(db.Integer, db.ForeignKey('user_permission_master.id'), nullable=False)
    allowed       = db.Column(db.Boolean, default=True)
    __table_args__ = (db.UniqueConstraint('role_id', 'permission_id', name='uq_role_perm'),)


class UserPermission(db.Model):
    """A per-user override on top of the role's permissions. The presence
    of a row IS the override; `allowed` distinguishes an explicit grant
    from an explicit deny. No row for a given permission = fall through
    to the user's role_permissions."""
    __tablename__ = 'user_permissions'
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    permission_id = db.Column(db.Integer, db.ForeignKey('user_permission_master.id'), nullable=False)
    allowed       = db.Column(db.Boolean, default=True)
    override_type = db.Column(db.String(20), default='grant')  # 'grant' or 'deny', mirrors `allowed`
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    created_by    = db.Column(db.Integer, db.ForeignKey('users.id'))
    __table_args__ = (db.UniqueConstraint('user_id', 'permission_id', name='uq_user_perm'),)


class AutoCodeSelection(db.Model):
    """Maps a module/form pair to the Level Five (Chart of Accounts) code
    that should be auto-selected for it, with the account's Debit/Credit
    nature. Drives auto-journal-posting configuration."""
    __tablename__ = 'auto_code_selection'
    id                   = db.Column(db.Integer, primary_key=True)
    module_id            = db.Column(db.Integer, db.ForeignKey('auto_modules.id'), nullable=False)
    form_id              = db.Column(db.Integer, db.ForeignKey('auto_form.id'), nullable=False)
    # Level Four is optional extra context on top of the (required) Level
    # Five code below -- e.g. showing which heading account (like "Cash at
    # Bank") a given Level Five code sits under, without that heading being
    # looked up by anything itself.
    levelfour_code       = db.Column(db.String(30))
    levelfour_drawer_en  = db.Column(db.String(200))
    levelfour_drawer_ar  = db.Column(db.Unicode(200))
    levelfive_code       = db.Column(db.String(40), nullable=False)
    levelfive_drawer_en  = db.Column(db.String(250))
    levelfive_drawer_ar  = db.Column(db.Unicode(250))
    nature               = db.Column(db.String(10), nullable=False, default='Debit')  # Debit | Credit
    # Workflow status -- 'Approved' is the only value offered today; kept as
    # a real column (rather than assumed) so a future status can be added
    # without a schema change.
    status               = db.Column(db.String(20), nullable=False, default='Approved')
    # Which Payment Mode this mapping applies to, when the module/form is
    # payment-mode-specific (e.g. Cash & Bank's Outgoing/Incoming Payment) --
    # blank for mappings that aren't payment-mode-specific.
    payment_mode         = db.Column(db.String(20))
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)
    created_by           = db.Column(db.Integer, db.ForeignKey('users.id'))

    module = db.relationship('Module', foreign_keys=[module_id])
    form   = db.relationship('SystemForm', foreign_keys=[form_id])

    def to_dict(self):
        return {
            'id': self.id,
            'module_id': self.module_id,
            'module_name': self.module.label_en if self.module else '',
            'form_id': self.form_id,
            'form_name': self.form.label_en if self.form else '',
            'levelfour_code': self.levelfour_code or '',
            'levelfour_drawer_en': self.levelfour_drawer_en or '',
            'levelfour_drawer_ar': self.levelfour_drawer_ar or '',
            'levelfive_code': self.levelfive_code or '',
            'levelfive_drawer_en': self.levelfive_drawer_en or '',
            'levelfive_drawer_ar': self.levelfive_drawer_ar or '',
            'nature': self.nature or 'Debit',
            'status': self.status or 'Approved',
            'payment_mode': self.payment_mode or '',
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
        }


class RecycleBin(db.Model):
    __tablename__ = 'recycle_bin'
    id           = db.Column(db.Integer, primary_key=True)
    module_key   = db.Column(db.String(50), nullable=False)
    form_key     = db.Column(db.String(50), nullable=False)
    record_pk    = db.Column(db.Integer, nullable=False)
    record_label = db.Column(db.String(300))
    deleted_data = db.Column(db.Text, nullable=False)   # JSON: {"parent":{...},"children":{"<key>":[...]}}

    deleted_by   = db.Column(db.Integer, db.ForeignKey('users.id'))
    deleted_at   = db.Column(db.DateTime, default=datetime.utcnow)
    purge_at     = db.Column(db.DateTime)

    restored_by  = db.Column(db.Integer, db.ForeignKey('users.id'))
    restored_at  = db.Column(db.DateTime)

    permanently_deleted_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    permanently_deleted_at = db.Column(db.DateTime)

    status = db.Column(db.String(20), default='in_bin')  # in_bin / restored / purged

    deleter    = db.relationship('User', foreign_keys=[deleted_by], lazy=True)

    def to_dict(self):
        return {
            'id': self.id, 'module_key': self.module_key, 'form_key': self.form_key,
            'record_pk': self.record_pk, 'record_label': self.record_label or '',
            'deleted_by': self.deleted_by,
            'deleted_by_name': (self.deleter.username if self.deleter else ''),
            'deleted_at': self.deleted_at.strftime('%d/%m/%Y %H:%M') if self.deleted_at else '',
            'purge_at': self.purge_at.strftime('%d/%m/%Y %H:%M') if self.purge_at else '',
            'status': self.status,
        }


# ─────────────────────────────────────────────────────────────────
# OWNER WAREHOUSE  (one owner -> many warehouses)
# ─────────────────────────────────────────────────────────────────
class OwnerWarehouse(db.Model):
    __tablename__ = 'owner_warehouses'
    id                = db.Column(db.Integer, primary_key=True)
    owner_id          = db.Column(db.Integer, db.ForeignKey('owners.id', ondelete='CASCADE'), nullable=False)
    warehouse_name    = db.Column(db.String(200), nullable=False)
    warehouse_name_ar = db.Column(db.Unicode(200))
    location          = db.Column(db.String(200))
    location_ar       = db.Column(db.Unicode(200))
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)

    owner = db.relationship('Owner', backref=db.backref('warehouses', cascade='all, delete-orphan', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'owner_id': self.owner_id,
            'warehouse_name': self.warehouse_name,
            'warehouse_name_ar': self.warehouse_name_ar or '',
            'location': self.location or '',
            'location_ar': self.location_ar or '',
        }

# BUYER MASTER
# ─────────────────────────────────────────────────────────────────
class BuyerMaster(db.Model):
    __tablename__ = 'buyers'
    id                   = db.Column(db.Integer, primary_key=True)
    buyer_code           = db.Column(db.String(20), unique=True)
    buyer_name_en        = db.Column(db.String(200), nullable=False)
    buyer_name_ar        = db.Column(db.Unicode(200))
    vat_number           = db.Column(db.String(50))
    crn                  = db.Column(db.String(50))
    salary_order         = db.Column(db.Integer, default=1)
    phone                = db.Column(db.String(30))
    fax                  = db.Column(db.String(30))
    email                = db.Column(db.String(120))
    website              = db.Column(db.String(200))
    report_color         = db.Column(db.String(10), default='#2563eb')
    street_name          = db.Column(db.String(200))
    street_name_ar       = db.Column(db.Unicode(200))
    building_number      = db.Column(db.String(50))
    additional_number    = db.Column(db.String(50))
    postal_code          = db.Column(db.String(20))
    country              = db.Column(db.String(100), default='Saudi Arabia')
    country_ar           = db.Column(db.Unicode(100))
    city                 = db.Column(db.String(100))
    city_ar              = db.Column(db.Unicode(100))
    district             = db.Column(db.String(100))
    district_ar          = db.Column(db.Unicode(100))
    status               = db.Column(db.String(20), default='active')
    is_active            = db.Column(db.Boolean, default=True)
    levelfive_code       = db.Column(db.String(40))
    levelfive_drawer     = db.Column(db.String(250))
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at           = db.Column(db.DateTime, default=datetime.utcnow)
    created_by           = db.Column(db.Integer, db.ForeignKey('users.id'))

    def to_dict(self):
        return {
            'id': self.id, 'buyer_code': self.buyer_code or '',
            'buyer_name_en': self.buyer_name_en, 'buyer_name_ar': self.buyer_name_ar or '',
            'vat_number': self.vat_number or '', 'crn': self.crn or '',
            # department and department_ar removed from to_dict
            'phone': self.phone or '', 'email': self.email or '',
            'city': self.city or '', 'is_active': self.is_active,
            'salary_order': self.salary_order or 1,
            'levelfive_code': self.levelfive_code or '',
            'levelfive_drawer': self.levelfive_drawer or '',
        }
    
# ─────────────────────────────────────────────────────────────────
# BUYER BANK   (stored in buyer_banks)
# ─────────────────────────────────────────────────────────────────
class BuyerBank(db.Model):
    __tablename__ = 'buyer_banks'
    id             = db.Column(db.Integer, primary_key=True)
    buyer_id       = db.Column(db.Integer, db.ForeignKey('buyers.id', ondelete='CASCADE'), nullable=False)
    bank_name      = db.Column(db.String(150), nullable=False)
    bank_name_ar   = db.Column(db.Unicode(150))
    account_number = db.Column(db.String(50))
    branch         = db.Column(db.String(100))
    branch_ar      = db.Column(db.Unicode(100))
    swift_code     = db.Column(db.String(20))
    iban           = db.Column(db.String(50))
    is_primary     = db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    buyer = db.relationship('BuyerMaster', backref=db.backref('banks', cascade='all,delete-orphan', lazy=True))

    def to_dict(self):
        return {
            'id': self.id, 'buyer_id': self.buyer_id,
            'bank_name': self.bank_name, 
            'bank_name_ar': self.bank_name_ar or '',
            'account_number': self.account_number or '',
            'branch': self.branch or '', 
            'branch_ar': self.branch_ar or '',
            'swift_code': self.swift_code or '',
            'iban': self.iban or '', 
            'is_primary': self.is_primary,
        }
# PROFESSION MASTER
# ─────────────────────────────────────────────────────────────────
class ProfessionMaster(db.Model):
    __tablename__ = 'employee_profession_master'
    id        = db.Column(db.Integer, primary_key=True)
    name_en   = db.Column(db.String(150), nullable=False)
    name_ar   = db.Column(db.Unicode(150))
    is_active = db.Column(db.Boolean, default=True)
    created_at= db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name_en': self.name_en,
                'name_ar': self.name_ar or '', 'is_active': self.is_active}

# ═══════════════════════════════════════════════════════════════════
# REPLACE the existing Employee, AllowanceType, and EmployeeAllowance
# classes in models.py with the versions below.
# (EmployeeBank and WorkAllocation stay as they are.)
#
# Notes:
#  - Columns cover every field bind_employee() / employee_json() touch,
#    so saving no longer silently drops data.
#  - employee_type is REMOVED per request.
#  - overtime_rate column added (your formula writes to it).
#  - `name`/`name_ar` are the primary name columns the routes use
#    (the old model called them name_en/name_ar — routes use `name`).
#    A `name_en` hybrid alias is provided so any code using name_en
#    still works.
# ═══════════════════════════════════════════════════════════════════

# EMPLOYEE ↔ PROFESSION JUNCTION TABLE (multi-select support)
# ═══════════════════════════════════════════════════════════════
class EmployeeProfession(db.Model):
    __tablename__ = 'employee_professions'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='CASCADE'), nullable=False)
    profession_id = db.Column(db.Integer, db.ForeignKey('employee_profession_master.id', ondelete='CASCADE'), nullable=False)
    profession_name    = db.Column(db.String(150))
    profession_name_ar = db.Column(db.Unicode(150))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('employee_id', 'profession_id', name='uq_emp_prof'),)


class Employee(db.Model):
    __tablename__ = 'employees'
    id               = db.Column(db.Integer, primary_key=True)
    employee_code    = db.Column(db.String(20), unique=True, nullable=False)
    auto_code        = db.Column(db.Boolean, default=True)
    is_active        = db.Column(db.Boolean, default=True)
    is_muslim        = db.Column(db.Boolean, default=False)
    blood_group      = db.Column(db.String(10))

    # ── Identity (bilingual) ──
    name             = db.Column(db.String(200), nullable=False)
    name_ar          = db.Column(db.Unicode(200))
    nationality      = db.Column(db.String(100))
    nationality_ar   = db.Column(db.Unicode(100))
    education        = db.Column(db.String(150))
    education_ar     = db.Column(db.Unicode(150))

    # ── Kafeel / sponsor ──
    kafeel_name          = db.Column(db.String(200))
    kafeel_name_ar       = db.Column(db.Unicode(200))
    kafeel_reference     = db.Column(db.String(100))
    kafeel_reference_ar  = db.Column(db.Unicode(100))
    kafalat_number       = db.Column(db.String(100))

    # ── Documents / IDs ──
    passport_number  = db.Column(db.String(50))
    passport_expiry  = db.Column(db.Date)
    passport_location= db.Column(db.String(20), default='IN')
    entry_number     = db.Column(db.String(50))
    iqama_number     = db.Column(db.String(50))
    iqama_expiry     = db.Column(db.Date)

    # ── Dates ──
    arrival_date     = db.Column(db.Date)
    birth_date       = db.Column(db.Date)

    # ── Contact ──
    mobile           = db.Column(db.String(30))
    email            = db.Column(db.String(120))
    address          = db.Column(db.String(300))
    address_ar       = db.Column(db.Unicode(300))
    home_city        = db.Column(db.String(120))
    home_city_ar     = db.Column(db.Unicode(120))

    # ── Employment / references ──
    employee_reference    = db.Column(db.String(120))
    employee_reference_ar = db.Column(db.Unicode(120))

    # ── Payroll ──
    salary_category  = db.Column(db.String(20))   # 'Salary' or 'Azad'
    salary_type      = db.Column(db.String(30), default='salary')
    basic_salary     = db.Column(db.Numeric(12, 2), default=0)
    total_allowances = db.Column(db.Numeric(12, 2), default=0)
    net_salary       = db.Column(db.Numeric(12, 2), default=0)
    po_number        = db.Column(db.String(80))
    services_charges = db.Column(db.Numeric(12, 2), default=0)
    po_rate          = db.Column(db.Numeric(12, 2), default=0)
    po_ot_rate       = db.Column(db.Numeric(12, 2), default=0)
    working_hours    = db.Column(db.Numeric(6, 2), default=8)
    overtime_ratio   = db.Column(db.Numeric(6, 2), default=1.5)
    overtime_rate    = db.Column(db.Numeric(12, 2), default=0)

    # ── Hostel ──
    hostel_name        = db.Column(db.String(200))
    hostel_name_ar     = db.Column(db.Unicode(200))
    room_number        = db.Column(db.String(50))
    hostel_location    = db.Column(db.String(200))
    hostel_location_ar = db.Column(db.Unicode(200))

    # ── Compliance ──
    crn                   = db.Column(db.String(60))
    crn_ar                = db.Column(db.Unicode(60))
    insurance_company     = db.Column(db.String(200))
    insurance_company_ar  = db.Column(db.Unicode(200))
    insurance_expiry      = db.Column(db.Date)
    labour_office         = db.Column(db.String(150))

    # ── Chart of Account ──
    levelfive_code        = db.Column(db.String(40))
    levelfive_drawer      = db.Column(db.String(250))

    # ── Misc / relations ──
    document_path    = db.Column(db.String(300))
    photo_path       = db.Column(db.String(300))
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow)
    created_by       = db.Column(db.Integer, db.ForeignKey('users.id'))

    # ── Relationships ──
    # NOTE: no direct buyer/company link on Employee — company, department,
    # location and shift are owned entirely by the separate Work Allocation
    # module (EmployeeWorkAllocation, linked via employee_id below).

    # Multi-select professions (junction table)
    professions = db.relationship('ProfessionMaster',
                    secondary='employee_professions',
                    lazy='dynamic',
                    backref=db.backref('employee_list', lazy='dynamic'))

    allowance_rows = db.relationship('EmployeeAllowance',
                        backref='employee_ref',
                        lazy='dynamic',
                        cascade='all, delete-orphan')

    banks = db.relationship('EmployeeBank',
                        backref='employee_ref',
                        lazy='dynamic',
                        cascade='all, delete-orphan')

    documents = db.relationship('EmployeeDocument',
                        backref='employee_ref',
                        lazy='dynamic',
                        cascade='all, delete-orphan')

    @property
    def name_en(self):
        return self.name
    @name_en.setter
    def name_en(self, v):
        self.name = v

    def to_dict(self):
        return {
            'id': self.id, 'employee_code': self.employee_code,
            'name': self.name, 'name_ar': self.name_ar or '',
            'nationality': self.nationality or '',
            'profession': ', '.join([p.name_en for p in self.professions.all()]) if hasattr(self,'professions') else '',
            'basic_salary': float(self.basic_salary or 0),
            'total_allowances': float(self.total_allowances or 0),
            'net_salary': float(self.net_salary or 0),
            'mobile': self.mobile or '', 'email': self.email or '',
            'is_active': self.is_active,
        }

class AllowanceType(db.Model):
    __tablename__ = 'employee_allowance_types'
    id                = db.Column(db.Integer, primary_key=True)
    allowance_code    = db.Column(db.String(20))
    allowance_name_en = db.Column(db.String(150), nullable=False)
    allowance_name_ar = db.Column(db.Unicode(150))
    description       = db.Column(db.String(255))
    is_active         = db.Column(db.Boolean, default=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id,
                'allowance_code': self.allowance_code or '',
                'allowance_name_en': self.allowance_name_en,
                'allowance_name_ar': self.allowance_name_ar or '',
                'description': self.description or '',
                'is_active': self.is_active}

class EmployeeAllowance(db.Model):
    __tablename__ = 'employee_allowances'
    id                = db.Column(db.Integer, primary_key=True)
    employee_id       = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='CASCADE'), nullable=False)
    allowance_type_id = db.Column(db.Integer, db.ForeignKey('employee_allowance_types.id'))   
    allowance_code    = db.Column(db.String(20))
    name              = db.Column(db.String(150))
    name_ar           = db.Column(db.Unicode(150))
    amount            = db.Column(db.Numeric(12, 2), default=0)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)

    allowance_type = db.relationship('AllowanceType', backref=db.backref('employee_allowances', lazy=True))

   
    def to_dict(self):
        return {
            'id': self.id, 'employee_id': self.employee_id,
            'allowance_type_id': self.allowance_type_id,
            'allowance_code': self.allowance_code or (self.allowance_type.allowance_code if self.allowance_type else ''),
            'name': self.name or (self.allowance_type.allowance_name_en if self.allowance_type else ''),
            'name_ar': self.name_ar or (self.allowance_type.allowance_name_ar if self.allowance_type else ''),
            'amount': float(self.amount or 0),
        }


class EmployeeBank(db.Model):
    __tablename__ = 'employee_banks'
    id             = db.Column(db.Integer, primary_key=True)
    employee_id    = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='CASCADE'), nullable=False)
    bank_name      = db.Column(db.String(150), nullable=False)
    bank_name_ar   = db.Column(db.Unicode(150))
    branch         = db.Column(db.String(120))
    branch_ar      = db.Column(db.Unicode(120))
    account_number = db.Column(db.String(60))
    swift_code     = db.Column(db.String(30))
    iban           = db.Column(db.String(60))
    is_primary     = db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'employee_id': self.employee_id,
            'bank_name': self.bank_name, 'bank_name_ar': self.bank_name_ar or '',
            'branch': self.branch or '', 'branch_ar': self.branch_ar or '',
            'account_number': self.account_number or '',
            'swift_code': self.swift_code or '', 'iban': self.iban or '',
            'is_primary': self.is_primary,
        }


class EmployeeDocument(db.Model):
    __tablename__ = 'employee_documents'
    id            = db.Column(db.Integer, primary_key=True)
    employee_id   = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='CASCADE'), nullable=False)
    document_type = db.Column(db.String(80))
    file_path     = db.Column(db.String(300), nullable=False)
    original_name = db.Column(db.String(200))
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by   = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Snapshot of the employee's identity at upload time -- kept even if the
    # employee's own name/code/passport/iqama later changes or the employee
    # is removed.
    employee_code   = db.Column(db.String(20))
    employee_name   = db.Column(db.String(200))
    passport_number = db.Column(db.String(50))
    iqama_number    = db.Column(db.String(50))

    def to_dict(self):
        return {
            'id': self.id, 'employee_id': self.employee_id,
            'document_type': self.document_type or '',
            'file_path': self.file_path,
            'original_name': self.original_name or '',
            'uploaded_at': self.uploaded_at.strftime('%d/%m/%Y %H:%M') if self.uploaded_at else '',
            'uploaded_by': self.uploaded_by,
            'employee_code': self.employee_code or '',
            'employee_name': self.employee_name or '',
            'passport_number': self.passport_number or '',
            'iqama_number': self.iqama_number or '',
        }


# ─────────────────────────────────────────────────────────────────
class SupplierMaster(db.Model):
    __tablename__ = 'suppliers'
    id                = db.Column(db.Integer, primary_key=True)
    supplier_code       = db.Column(db.String(20), unique=True)
    supplier_name_en    = db.Column(db.String(200), nullable=False)
    supplier_name_ar    = db.Column(db.Unicode(200))
    vat_number        = db.Column(db.String(50))
    crn               = db.Column(db.String(50))
    phone             = db.Column(db.String(30))
    fax               = db.Column(db.String(30))
    email             = db.Column(db.String(120))
    website           = db.Column(db.String(200))
    contact_person    = db.Column(db.String(150))
    street_name       = db.Column(db.String(200))
    street_name_ar    = db.Column(db.Unicode(200))
    building_number   = db.Column(db.String(50))
    additional_number = db.Column(db.String(50))
    postal_code       = db.Column(db.String(20))
    country           = db.Column(db.String(100), default='Saudi Arabia')
    country_ar        = db.Column(db.Unicode(100))
    city              = db.Column(db.String(100))
    city_ar           = db.Column(db.Unicode(100))
    district          = db.Column(db.String(100))
    district_ar       = db.Column(db.Unicode(100))
    status            = db.Column(db.String(20), default='active')
    payment_term        = db.Column(db.String(40))
    is_active         = db.Column(db.Boolean, default=True)
    levelfive_code    = db.Column(db.String(40))
    levelfive_drawer  = db.Column(db.String(250))
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    created_by        = db.Column(db.Integer, db.ForeignKey('users.id'))

    def to_dict(self):
        return {
            'id': self.id, 'supplier_code': self.supplier_code or '',
            'supplier_name_en': self.supplier_name_en, 'supplier_name_ar': self.supplier_name_ar or '',
            'vat_number': self.vat_number or '', 'crn': self.crn or '',
            'phone': self.phone or '', 'email': self.email or '',
            'city': self.city or '', 'status': self.status,
            'contact_person': self.contact_person or '', 'is_active': self.is_active,
            'levelfive_code': self.levelfive_code or '',
            'levelfive_drawer': self.levelfive_drawer or '',
        }


# ─────────────────────────────────────────────────────────────────
# SUPPLIER BANK   (stored in supplier_banks)
# ─────────────────────────────────────────────────────────────────
class SupplierBank(db.Model):
    __tablename__ = 'supplier_banks'
    id             = db.Column(db.Integer, primary_key=True)
    supplier_id      = db.Column(db.Integer, db.ForeignKey('suppliers.id', ondelete='CASCADE'), nullable=False)
    bank_name_en   = db.Column(db.String(150), nullable=False)
    bank_name_ar   = db.Column(db.Unicode(150))
    account_number = db.Column(db.String(50))
    branch_en      = db.Column(db.String(100))
    branch_ar      = db.Column(db.Unicode(100))
    swift_code     = db.Column(db.String(20))
    iban           = db.Column(db.String(50))
    is_primary     = db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    supplier = db.relationship('SupplierMaster', backref=db.backref('banks', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id, 'supplier_id': self.supplier_id,
            'bank_name_en': self.bank_name_en, 'bank_name_ar': self.bank_name_ar or '',
            'account_number': self.account_number or '',
            'branch_en': self.branch_en or '', 'branch_ar': self.branch_ar or '',
            'swift_code': self.swift_code or '', 'iban': self.iban or '',
            'is_primary': self.is_primary,
        }


# ─────────────────────────────────────────────────────────────────
# CASH & BANK -- Outgoing Payment: a full accounting transaction (Draft ->
# Approved -> Posted -> Cancelled), not a plain voucher. Pay To Type is
# Supplier/Buyer/Employee/GL Account; Payment Type (Advance/Outstanding)
# controls whether Supplier/Buyer/Employee settle against outstanding
# Purchase Invoices/Sales Invoices/Payroll Payable rows (Detail Table 1 --
# OutgoingPaymentAdjustment) or post a single lump sum straight to that
# party's own Advance control account with no detail lines. GL Account
# always uses a direct multi-line GL/expense payment instead (Detail
# Table 2 -- OutgoingPaymentGLDetail). GL posting (GRL + JournalEntry)
# only ever happens on Post, and Cancel creates a reversing GRL/
# JournalEntry pair rather than deleting anything -- matching this
# project's rule that only an explicit Post/Cancel action may move
# accounting balances, and that posted accounting history is never
# destroyed, only reversed.
# ─────────────────────────────────────────────────────────────────
class PaymentMode(db.Model):
    """Configurable Payment Mode -> GL Account map (Check/Bank Transfer/
    Cash/Credit Card by default). The admin can repoint the GL account per
    mode; Outgoing Payment snapshots the resolved code at post time so a
    later config change never rewrites historical postings."""
    __tablename__ = 'payment_modes'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(50), unique=True, nullable=False)
    gl_account_id = db.Column(db.String(40))   # LevelFive.code
    active        = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        account = LevelFive.query.filter_by(code=self.gl_account_id).first() if self.gl_account_id else None
        return {
            'id': self.id, 'name': self.name or '',
            'gl_account_id': self.gl_account_id or '',
            'gl_account_name': (account.drawers if account else '') or '',
            'active': bool(self.active),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'updated_at': self.updated_at.strftime('%d/%m/%Y %H:%M') if self.updated_at else '',
        }


DEFAULT_PAYMENT_MODES = ['Check', 'Bank Transfer', 'Cash', 'Credit Card', 'POS']


def seed_payment_modes():
    """Idempotently seed the 4 default Payment Modes (GL account left blank
    -- an admin must configure it via the Payment Mode settings screen
    before that mode can be used to post an Outgoing Payment)."""
    added = 0
    existing = {p.name for p in PaymentMode.query.all()}
    for name in DEFAULT_PAYMENT_MODES:
        if name not in existing:
            db.session.add(PaymentMode(name=name, active=True))
            added += 1
    if added:
        db.session.commit()
    return added


class OutgoingPayment(db.Model):
    __tablename__ = 'outgoing_payment'
    id                          = db.Column(db.Integer, primary_key=True)
    payment_no                  = db.Column(db.String(20), unique=True)
    payment_date                = db.Column(db.Date)
    posting_date                = db.Column(db.Date)
    pay_to_type                 = db.Column(db.String(20), nullable=False)   # Supplier | Buyer | Employee | GL Account
    pay_to_id                   = db.Column(db.String(40))   # entity PK (Supplier/Buyer/Employee id); NULL for GL Account
    payment_type                = db.Column(db.String(20))   # Advance | Outstanding; unused for GL Account
    payment_method_id           = db.Column(db.Integer, db.ForeignKey('payment_modes.id'))
    # The Cash/Bank Level Five account this payment is actually paid FROM,
    # scoped to the Level Four heading the selected Payment Method implies
    # (see PAYMENT_MODE_NAME_LEVEL_FOUR in database/routes/cash_bank.py).
    payment_account_id          = db.Column(db.String(40))
    reference_no                = db.Column(db.String(100))
    narration                   = db.Column(db.Text)
    status                      = db.Column(db.String(10), default='Draft')  # Draft | Approved | Posted | Cancelled
    total_amount                = db.Column(db.Numeric(14, 2), default=0)   # sum before VAT
    total_vat                   = db.Column(db.Numeric(14, 2), default=0)
    total_amount_including_vat  = db.Column(db.Numeric(14, 2), default=0)
    journal_entry_id            = db.Column(db.Integer, db.ForeignKey('journal_entries.id'))
    grl_id                      = db.Column(db.Integer, db.ForeignKey('journal_ledger.id'))
    created_by                  = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at                  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_by                  = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_at                  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])
    updater = db.relationship('User', foreign_keys=[updated_by])
    payment_method = db.relationship('PaymentMode', foreign_keys=[payment_method_id])
    adjustment_lines = db.relationship('OutgoingPaymentAdjustment', backref='payment', lazy=True,
                            cascade='all, delete-orphan', order_by='OutgoingPaymentAdjustment.detail_id')
    gl_lines = db.relationship('OutgoingPaymentGLDetail', backref='payment', lazy=True,
                            cascade='all, delete-orphan', order_by='OutgoingPaymentGLDetail.detail_id')

    def _pay_to_name(self):
        if not self.pay_to_id:
            return ''
        try:
            if self.pay_to_type == 'Supplier':
                s = SupplierMaster.query.get(int(self.pay_to_id))
                return s.supplier_name_en if s else ''
            if self.pay_to_type == 'Buyer':
                b = BuyerMaster.query.get(int(self.pay_to_id))
                return b.buyer_name_en if b else ''
            if self.pay_to_type == 'Employee':
                e = Employee.query.get(int(self.pay_to_id))
                return e.name if e else ''
        except (ValueError, TypeError):
            return ''
        return ''

    def to_dict(self):
        acc = LevelFive.query.filter_by(code=self.payment_account_id).first() if self.payment_account_id else None
        return {
            'id': self.id, 'payment_no': self.payment_no or '',
            'pay_to_type': self.pay_to_type or '', 'pay_to_id': self.pay_to_id or '',
            'pay_to_name': self._pay_to_name(),
            'payment_type': self.payment_type or '',
            'payment_date': str(self.payment_date) if self.payment_date else '',
            'posting_date': str(self.posting_date) if self.posting_date else '',
            'payment_method_id': self.payment_method_id,
            'payment_method_name': self.payment_method.name if self.payment_method else '',
            'payment_account_id': self.payment_account_id or '',
            'payment_account_name': (acc.drawers if acc else '') or '',
            'reference_no': self.reference_no or '',
            'narration': self.narration or '',
            'status': self.status or 'Draft',
            'total_amount': float(self.total_amount or 0),
            'total_vat': float(self.total_vat or 0),
            'total_amount_including_vat': float(self.total_amount_including_vat or 0),
            'journal_entry_id': self.journal_entry_id,
            'grl_id': self.grl_id,
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by_name': self.creator.username if self.creator else '',
            'adjustment_lines': [ln.to_dict() for ln in self.adjustment_lines],
            'gl_lines': [ln.to_dict() for ln in self.gl_lines],
        }


class OutgoingPaymentAdjustment(db.Model):
    """Detail Table 1 -- one row per outstanding Purchase Invoice / Sales
    Invoice / Payroll Payable row being settled (fully or partially) by
    this payment. Only used when OutgoingPayment.payment_type ==
    'Outstanding'; empty for 'Advance' (a single lump sum with no detail
    lines) and for pay_to_type == 'GL Account' (uses
    OutgoingPaymentGLDetail instead)."""
    __tablename__ = 'outgoing_payment_adjustment'
    id                  = db.Column(db.Integer, primary_key=True)
    outgoing_payment_id = db.Column(db.Integer, db.ForeignKey('outgoing_payment.id', ondelete='CASCADE'), nullable=False)
    detail_id           = db.Column(db.Integer, default=1)   # 1-based line sequence within this payment
    document_type       = db.Column(db.String(30))   # Purchase Invoice | Sales Invoice | Payroll Payable
    document_id         = db.Column(db.Integer)   # source row PK -- purchase_invoice_id / sales_invoice_id / salary_consolidation.id
    document_no         = db.Column(db.String(30))
    document_date       = db.Column(db.Date)
    due_date            = db.Column(db.Date)
    outstanding_amount  = db.Column(db.Numeric(14, 2), default=0)   # snapshot when this line was added
    payment_amount      = db.Column(db.Numeric(14, 2), default=0)
    remaining_amount    = db.Column(db.Numeric(14, 2), default=0)   # snapshot: outstanding_amount - payment_amount
    reference_no        = db.Column(db.String(100))
    narration           = db.Column(db.String(300))
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'outgoing_payment_id': self.outgoing_payment_id,
            'detail_id': self.detail_id or 1,
            'document_type': self.document_type or '',
            'document_id': self.document_id,
            'document_no': self.document_no or '',
            'document_date': str(self.document_date) if self.document_date else '',
            'due_date': str(self.due_date) if self.due_date else '',
            'outstanding_amount': float(self.outstanding_amount or 0),
            'payment_amount': float(self.payment_amount or 0),
            'remaining_amount': float(self.remaining_amount or 0),
            'reference_no': self.reference_no or '',
            'narration': self.narration or '',
        }


class OutgoingPaymentGLDetail(db.Model):
    """Detail Table 2 -- one row per direct GL/expense line, only used
    when OutgoingPayment.pay_to_type == 'GL Account'. Each line names its
    own Level Five code directly (no shared fallback account), so one
    payment can cover several unrelated expenses in a single voucher."""
    __tablename__ = 'outgoing_payment_gl_detail'
    id                  = db.Column(db.Integer, primary_key=True)
    outgoing_payment_id = db.Column(db.Integer, db.ForeignKey('outgoing_payment.id', ondelete='CASCADE'), nullable=False)
    detail_id           = db.Column(db.Integer, default=1)
    code                = db.Column(db.String(40))   # LevelFive.code
    name                = db.Column(db.String(250))  # snapshot of LevelFive.drawers -- never user-typed
    document_no         = db.Column(db.String(30))
    purpose             = db.Column(db.String(300))
    amount              = db.Column(db.Numeric(14, 2), default=0)
    vat                 = db.Column(db.Numeric(14, 2), default=0)
    total               = db.Column(db.Numeric(14, 2), default=0)   # amount + vat
    narration           = db.Column(db.String(300))
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'outgoing_payment_id': self.outgoing_payment_id,
            'detail_id': self.detail_id or 1,
            'code': self.code or '', 'name': self.name or '',
            'document_no': self.document_no or '', 'purpose': self.purpose or '',
            'amount': float(self.amount or 0), 'vat': float(self.vat or 0),
            'total': float(self.total or 0), 'narration': self.narration or '',
        }


class IncomingPayment(db.Model):
    """A full accounting transaction (Draft -> Approved -> Posted ->
    Cancelled) -- the exact mirror of OutgoingPayment, with every debit/
    credit side reversed. Receive From Type is Buyer/Supplier/Employee/GL
    Account; Payment Type (Advance/Outstanding) controls whether Buyer/
    Supplier/Employee settle against outstanding Sales Invoices/Purchase
    Invoices/Payroll Payable rows (Detail Table 1 --
    IncomingPaymentAdjustment) or post a single lump sum straight to that
    party's own Advance control account with no detail lines. GL Account
    always uses a direct multi-line GL/income receipt instead (Detail
    Table 2 -- IncomingPaymentGLDetail), with VAT split onto its own
    Output VAT line when applicable. GL posting (GRL + JournalEntry) only
    ever happens on Post, and Cancel creates a reversing GRL/JournalEntry
    pair rather than deleting anything -- matching this project's rule
    that only an explicit Post/Cancel action may move accounting
    balances, and that posted accounting history is never destroyed,
    only reversed."""
    __tablename__ = 'incoming_payment'
    id                          = db.Column(db.Integer, primary_key=True)
    payment_no                  = db.Column(db.String(20), unique=True)
    payment_date                = db.Column(db.Date)
    posting_date                = db.Column(db.Date)
    receive_from_type           = db.Column(db.String(20), nullable=False)   # Buyer | Supplier | Employee | GL Account
    receive_from_id             = db.Column(db.String(40))   # entity PK (Buyer/Supplier/Employee id); NULL for GL Account
    payment_type                = db.Column(db.String(20))   # Advance | Outstanding; unused for GL Account
    payment_method_id           = db.Column(db.Integer, db.ForeignKey('payment_modes.id'))
    # The Cash/Bank Level Five account this receipt is actually received
    # INTO, scoped to the Level Four heading the selected Payment Method
    # implies (see PAYMENT_MODE_NAME_LEVEL_FOUR in database/routes/cash_bank.py).
    payment_account_id          = db.Column(db.String(40))
    reference_no                = db.Column(db.String(100))
    narration                   = db.Column(db.Text)
    status                      = db.Column(db.String(10), default='Draft')  # Draft | Approved | Posted | Cancelled
    total_amount                = db.Column(db.Numeric(14, 2), default=0)   # sum before VAT
    total_vat                   = db.Column(db.Numeric(14, 2), default=0)
    total_amount_including_vat  = db.Column(db.Numeric(14, 2), default=0)
    journal_entry_id            = db.Column(db.Integer, db.ForeignKey('journal_entries.id'))
    grl_id                      = db.Column(db.Integer, db.ForeignKey('journal_ledger.id'))
    created_by                  = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at                  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_by                  = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_at                  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])
    updater = db.relationship('User', foreign_keys=[updated_by])
    payment_method = db.relationship('PaymentMode', foreign_keys=[payment_method_id])
    adjustment_lines = db.relationship('IncomingPaymentAdjustment', backref='payment', lazy=True,
                            cascade='all, delete-orphan', order_by='IncomingPaymentAdjustment.detail_id')
    gl_lines = db.relationship('IncomingPaymentGLDetail', backref='payment', lazy=True,
                            cascade='all, delete-orphan', order_by='IncomingPaymentGLDetail.detail_id')

    def _receive_from_name(self):
        if not self.receive_from_id:
            return ''
        try:
            if self.receive_from_type == 'Buyer':
                b = BuyerMaster.query.get(int(self.receive_from_id))
                return b.buyer_name_en if b else ''
            if self.receive_from_type == 'Supplier':
                s = SupplierMaster.query.get(int(self.receive_from_id))
                return s.supplier_name_en if s else ''
            if self.receive_from_type == 'Employee':
                e = Employee.query.get(int(self.receive_from_id))
                return e.name if e else ''
        except (ValueError, TypeError):
            return ''
        return ''

    def to_dict(self):
        acc = LevelFive.query.filter_by(code=self.payment_account_id).first() if self.payment_account_id else None
        return {
            'id': self.id, 'payment_no': self.payment_no or '',
            'receive_from_type': self.receive_from_type or '', 'receive_from_id': self.receive_from_id or '',
            'receive_from_name': self._receive_from_name(),
            'payment_type': self.payment_type or '',
            'payment_date': str(self.payment_date) if self.payment_date else '',
            'posting_date': str(self.posting_date) if self.posting_date else '',
            'payment_method_id': self.payment_method_id,
            'payment_method_name': self.payment_method.name if self.payment_method else '',
            'payment_account_id': self.payment_account_id or '',
            'payment_account_name': (acc.drawers if acc else '') or '',
            'reference_no': self.reference_no or '',
            'narration': self.narration or '',
            'status': self.status or 'Draft',
            'total_amount': float(self.total_amount or 0),
            'total_vat': float(self.total_vat or 0),
            'total_amount_including_vat': float(self.total_amount_including_vat or 0),
            'journal_entry_id': self.journal_entry_id,
            'grl_id': self.grl_id,
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by_name': self.creator.username if self.creator else '',
            'adjustment_lines': [ln.to_dict() for ln in self.adjustment_lines],
            'gl_lines': [ln.to_dict() for ln in self.gl_lines],
        }


class IncomingPaymentAdjustment(db.Model):
    """Detail Table 1 -- one row per outstanding Sales Invoice / Purchase
    Invoice / Payroll Payable row being settled (fully or partially) by
    this receipt. Only used when IncomingPayment.payment_type ==
    'Outstanding'; empty for 'Advance' (a single lump sum with no detail
    lines) and for receive_from_type == 'GL Account' (uses
    IncomingPaymentGLDetail instead)."""
    __tablename__ = 'incoming_payment_adjustment'
    id                  = db.Column(db.Integer, primary_key=True)
    incoming_payment_id = db.Column(db.Integer, db.ForeignKey('incoming_payment.id', ondelete='CASCADE'), nullable=False)
    detail_id           = db.Column(db.Integer, default=1)   # 1-based line sequence within this payment
    document_type       = db.Column(db.String(30))   # Sales Invoice | Purchase Invoice | Payroll Payable
    document_id         = db.Column(db.Integer)   # source row PK -- sales_invoice_id / purchase_invoice_id / salary_consolidation.id
    document_no         = db.Column(db.String(30))
    document_date       = db.Column(db.Date)
    due_date            = db.Column(db.Date)
    outstanding_amount  = db.Column(db.Numeric(14, 2), default=0)   # snapshot when this line was added
    payment_amount      = db.Column(db.Numeric(14, 2), default=0)
    remaining_amount    = db.Column(db.Numeric(14, 2), default=0)   # snapshot: outstanding_amount - payment_amount
    reference_no        = db.Column(db.String(100))
    narration           = db.Column(db.String(300))
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'incoming_payment_id': self.incoming_payment_id,
            'detail_id': self.detail_id or 1,
            'document_type': self.document_type or '',
            'document_id': self.document_id,
            'document_no': self.document_no or '',
            'document_date': str(self.document_date) if self.document_date else '',
            'due_date': str(self.due_date) if self.due_date else '',
            'outstanding_amount': float(self.outstanding_amount or 0),
            'payment_amount': float(self.payment_amount or 0),
            'remaining_amount': float(self.remaining_amount or 0),
            'reference_no': self.reference_no or '',
            'narration': self.narration or '',
        }


class IncomingPaymentGLDetail(db.Model):
    """Detail Table 2 -- one row per direct GL/income line, only used
    when IncomingPayment.receive_from_type == 'GL Account'. Each line
    names its own Level Five code directly (no shared fallback account),
    so one receipt can cover several unrelated income items in a single
    voucher."""
    __tablename__ = 'incoming_payment_gl_detail'
    id                  = db.Column(db.Integer, primary_key=True)
    incoming_payment_id = db.Column(db.Integer, db.ForeignKey('incoming_payment.id', ondelete='CASCADE'), nullable=False)
    detail_id           = db.Column(db.Integer, default=1)
    code                = db.Column(db.String(40))   # LevelFive.code
    name                = db.Column(db.String(250))  # snapshot of LevelFive.drawers -- never user-typed
    document_no         = db.Column(db.String(30))
    purpose             = db.Column(db.String(300))
    amount              = db.Column(db.Numeric(14, 2), default=0)
    vat                 = db.Column(db.Numeric(14, 2), default=0)
    total               = db.Column(db.Numeric(14, 2), default=0)   # amount + vat
    narration           = db.Column(db.String(300))
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'incoming_payment_id': self.incoming_payment_id,
            'detail_id': self.detail_id or 1,
            'code': self.code or '', 'name': self.name or '',
            'document_no': self.document_no or '', 'purpose': self.purpose or '',
            'amount': float(self.amount or 0), 'vat': float(self.vat or 0),
            'total': float(self.total or 0), 'narration': self.narration or '',
        }


# ─────────────────────────────────────────────────────────────────
# SUPPLIER DOCUMENT
# ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────
# SUPPLIER DOCUMENT
# ─────────────────────────────────────────────────────────────────
class SupplierDocument(db.Model):
    __tablename__ = 'supplier_documents'
    id            = db.Column(db.Integer, primary_key=True)
    supplier_id     = db.Column(db.Integer, db.ForeignKey('suppliers.id', ondelete='CASCADE'), nullable=False)
    document_type = db.Column(db.String(100))
    document_name = db.Column(db.String(255))
    issue_date    = db.Column(db.Date, nullable=True)
    expiry_date   = db.Column(db.Date, nullable=True)
    file_path     = db.Column(db.String(500))
    file_size     = db.Column(db.Integer)
    uploaded_by   = db.Column(db.Integer, db.ForeignKey('users.id'))
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)

    supplier   = db.relationship('SupplierMaster', backref=db.backref('documents', lazy=True, cascade='all,delete-orphan'))
    uploader = db.relationship('User', foreign_keys=[uploaded_by])

    def to_dict(self):
        return {
            'id': self.id,
            'supplier_id': self.supplier_id,
            'supplier_code': self.supplier.supplier_code if self.supplier else '',
            'document_type': self.document_type or '',
            'document_name': self.document_name or '',
            'issue_date': str(self.issue_date) if self.issue_date else '',
            'expiry_date': str(self.expiry_date) if self.expiry_date else '',
            'file_path': self.file_path or '',
            'file_size_kb': round((self.file_size or 0) / 1024, 1),
            'uploaded_by': self.uploaded_by,
            'uploaded_by_name': self.uploader.username if self.uploader else '',
            'uploaded_at': self.uploaded_at.strftime('%d/%m/%Y %H:%M') if self.uploaded_at else '',
        }


# ─────────────────────────────────────────────────────────────────
# BUYER DOCUMENT
# ─────────────────────────────────────────────────────────────────
class BuyerDocument(db.Model):
    __tablename__ = 'buyer_documents'
    id            = db.Column(db.Integer, primary_key=True)
    buyer_id      = db.Column(db.Integer, db.ForeignKey('buyers.id', ondelete='CASCADE'), nullable=False)
    document_type = db.Column(db.String(100))
    document_name = db.Column(db.String(255))
    issue_date    = db.Column(db.Date, nullable=True)
    expiry_date   = db.Column(db.Date, nullable=True)
    file_path     = db.Column(db.String(500))
    file_size     = db.Column(db.Integer)
    uploaded_by   = db.Column(db.Integer, db.ForeignKey('users.id'))
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)

    buyer    = db.relationship('BuyerMaster', backref=db.backref('documents', lazy=True, cascade='all,delete-orphan'))
    uploader = db.relationship('User', foreign_keys=[uploaded_by])

    def to_dict(self):
        return {
            'id': self.id,
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer.buyer_name_en if self.buyer else '',
            'buyer_code': self.buyer.buyer_code if self.buyer else '',
            'document_type': self.document_type or '',
            'document_name': self.document_name or '',
            'issue_date': str(self.issue_date) if self.issue_date else '',
            'expiry_date': str(self.expiry_date) if self.expiry_date else '',
            'file_path': self.file_path or '',
            'file_size_kb': round((self.file_size or 0) / 1024, 1),
            'uploaded_by': self.uploaded_by,
            'uploaded_by_name': self.uploader.username if self.uploader else '',
            'uploaded_at': self.uploaded_at.strftime('%d/%m/%Y %H:%M') if self.uploaded_at else '',
        }

# ═══════════════════════════════════════════════════════════════════
# PURCHASE MODULE
# Flow: PR(1) → PQ(2) → PO(3) → GRN(4) → PINV(5)
#                                        ↓
#                               GRR(6) → PDM(7)
# ═══════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────
# 1. PURCHASE REQUEST
# ─────────────────────────────────────────────────────────────────
class PurchaseRequest(db.Model):
    __tablename__ = 'purchase_requests'
    purchase_request_id   = db.Column(db.Integer, primary_key=True)
    doc_no                = db.Column(db.String(20), unique=True)
    purchase_type         = db.Column(db.String(20))   # Assets / Expense
    kind                  = db.Column(db.String(20), default='Goods')
    requester             = db.Column(db.String(150))
    requester_name        = db.Column(db.String(200))
    status                = db.Column(db.String(20), default='Open')
    posting_date          = db.Column(db.Date)
    valid_until           = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    required_date         = db.Column(db.Date)
    remarks               = db.Column(db.Text)
    account_code          = db.Column(db.String(20))
    terms_conditions      = db.Column(db.Text)
    approved_by           = db.Column(db.String(150))
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'purchase_type': self.purchase_type or '',
            'kind': self.kind or 'Goods',
            'id': self.purchase_request_id,
            'purchase_request_id': self.purchase_request_id,
            'doc_no': self.doc_no or '', 'requester': self.requester or '',
            'requester_name': self.requester_name or '',
            'status': self.status,
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'valid_until':   str(self.valid_until)   if self.valid_until   else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'required_date': str(self.required_date) if self.required_date else '',
            'remarks': self.remarks or '',
            'account_code': self.account_code or '',
            'terms_conditions': self.terms_conditions or '',
            'approved_by': self.approved_by or '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 1L. PURCHASE REQUEST LINE ITEMS
#      PK: purchase_request_line_item_id
#      FK: purchase_request_id → purchase_requests
# ─────────────────────────────────────────────────────────────────
class PurchaseRequestLineItem(db.Model):
    __tablename__ = 'purchase_request_line_items'
    purchase_request_line_item_id = db.Column(db.Integer, primary_key=True)
    purchase_request_id = db.Column(db.Integer, db.ForeignKey('purchase_requests.purchase_request_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    purchase_request = db.relationship('PurchaseRequest', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.purchase_request_line_item_id,
            'purchase_request_line_item_id': self.purchase_request_line_item_id,
            'purchase_request_id': self.purchase_request_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# 2. PURCHASE QUOTATION
#      FK: purchase_request_id → purchase_requests
# ─────────────────────────────────────────────────────────────────
class PurchaseQuotation(db.Model):
    __tablename__ = 'purchase_quotations'
    purchase_quotation_id = db.Column(db.Integer, primary_key=True)
    doc_no                = db.Column(db.String(20), unique=True)
    purchase_type         = db.Column(db.String(20))   # Assets / Expense
    kind                  = db.Column(db.String(20), default='Goods')
    pr_doc_no             = db.Column(db.String(20), db.ForeignKey('purchase_requests.doc_no'))
    requester             = db.Column(db.String(150))
    requester_name        = db.Column(db.String(200))
    supplier_id             = db.Column(db.Integer, db.ForeignKey('suppliers.id'))
    supplier_ref_no         = db.Column(db.String(100))
    status                = db.Column(db.String(20), default='Open')
    posting_date          = db.Column(db.Date)
    valid_until           = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    required_date         = db.Column(db.Date)
    remarks               = db.Column(db.Text)
    account_code          = db.Column(db.String(20))
    terms_conditions      = db.Column(db.Text)
    approved_by           = db.Column(db.String(150))
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    supplier = db.relationship('SupplierMaster', backref=db.backref('purchase_quotations', lazy=True))
    purchase_request = db.relationship('PurchaseRequest', backref=db.backref('purchase_quotations', lazy=True))
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'purchase_type': self.purchase_type or '',
            'kind': self.kind or 'Goods',
            'id': self.purchase_quotation_id,
            'purchase_quotation_id': self.purchase_quotation_id,
            'doc_no': self.doc_no or '',
            'purchase_request_id': self.purchase_request.purchase_request_id if self.purchase_request else None,
            'pr_doc_no': self.pr_doc_no or '',
            'requester': self.requester or '',
            'requester_name': self.requester_name or '',
            'supplier_id': self.supplier_id,
            'supplier_name': self.supplier.supplier_name_en if self.supplier else '',
            'supplier_ref_no': self.supplier_ref_no or '',
            'status': self.status,
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'valid_until':   str(self.valid_until)   if self.valid_until   else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'required_date': str(self.required_date) if self.required_date else '',
            'remarks': self.remarks or '',
            'account_code': self.account_code or '',
            'terms_conditions': self.terms_conditions or '',
            'approved_by': self.approved_by or '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 2L. PURCHASE QUOTATION LINE ITEMS
#      PK: purchase_quotation_line_item_id
#      FK: purchase_quotation_id → purchase_quotations
# ─────────────────────────────────────────────────────────────────
class PurchaseQuotationLineItem(db.Model):
    __tablename__ = 'purchase_quotation_line_items'
    purchase_quotation_line_item_id = db.Column(db.Integer, primary_key=True)
    purchase_quotation_id = db.Column(db.Integer, db.ForeignKey('purchase_quotations.purchase_quotation_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    purchase_quotation = db.relationship('PurchaseQuotation', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.purchase_quotation_line_item_id,
            'purchase_quotation_line_item_id': self.purchase_quotation_line_item_id,
            'purchase_quotation_id': self.purchase_quotation_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────
# PURCHASE ORDER HEADER
# ─────────────────────────────────────────────────────────────────
class PurchaseOrder(db.Model):
    __tablename__ = 'purchase_orders'
    purchase_order_id     = db.Column(db.Integer, primary_key=True)
    doc_no                = db.Column(db.String(20), unique=True)
    purchase_type         = db.Column(db.String(20))   # Assets / Expense
    kind                  = db.Column(db.String(20), default='Goods')
    pq_doc_no             = db.Column(db.String(20), db.ForeignKey('purchase_quotations.doc_no'))
    supplier_id             = db.Column(db.Integer, db.ForeignKey('suppliers.id'))
    supplier_ref_no         = db.Column(db.String(100))
    remarks               = db.Column(db.Text)
    account_code          = db.Column(db.String(20))
    terms_conditions      = db.Column(db.Text)
    status                = db.Column(db.String(20), default='Open')
    posting_date          = db.Column(db.Date)
    delivery_date         = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    supplier = db.relationship('SupplierMaster', backref=db.backref('purchase_orders', lazy=True))
    pq = db.relationship('PurchaseQuotation', backref=db.backref('purchase_orders', lazy=True))
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'purchase_type': self.purchase_type or '',
            'kind': self.kind or 'Goods',
            'id': self.purchase_order_id,
            'purchase_order_id': self.purchase_order_id,
            'doc_no': self.doc_no or '',
            'purchase_quotation_id': self.pq.purchase_quotation_id if self.pq else None,
            'pq_id': self.pq.purchase_quotation_id if self.pq else None,
            'pq_doc_no': self.pq_doc_no or '',
            'supplier_id': self.supplier_id,
            'supplier_name': self.supplier.supplier_name_en if self.supplier else '',
            'supplier_ref_no': self.supplier_ref_no or '',
            'remarks': self.remarks or '',
            'account_code': self.account_code or '',
            'terms_conditions': self.terms_conditions or '',
            'status': self.status,
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# PURCHASE ORDER LINE ITEMS
# ─────────────────────────────────────────────────────────────────
class PurchaseOrderLineItem(db.Model):
    __tablename__ = 'purchase_order_line_items'
    purchase_order_line_item_id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey('purchase_orders.purchase_order_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    purchase_order = db.relationship('PurchaseOrder', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def received_quantity(self):
        """Total quantity actually POSTED against this PO line -- i.e. summed
        from Active Store Transactions only (created solely when a GRN is
        Posted), never from a merely-saved/draft GRN line. This is the
        authoritative "received" figure the whole PO->GRN->Store chain relies on."""
        total = (db.session.query(db.func.coalesce(db.func.sum(StoreTransaction.quantity), 0))
                 .filter(StoreTransaction.purchase_order_line_item_id == self.purchase_order_line_item_id,
                         StoreTransaction.transaction_type == 'Purchase Receipt',
                         StoreTransaction.status == 'Active')
                 .scalar())
        return float(total or 0)

    def remaining_quantity(self):
        return max(0.0, float(self.quantity or 0) - self.received_quantity())

    def receipt_status(self):
        received = self.received_quantity()
        remaining = max(0.0, float(self.quantity or 0) - received)
        if remaining <= 0:
            return 'Fully Received'
        if received > 0:
            return 'Partially Received'
        return 'Open'

    def to_dict(self, with_progress=False):
        d = {
            'id': self.purchase_order_line_item_id,
            'purchase_order_line_item_id': self.purchase_order_line_item_id,
            'purchase_order_id': self.purchase_order_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '',
            'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '',
            'uom': self.uom,
            'quantity': float(self.quantity or 0),
            'rate': float(self.rate or 0),
            'discount': float(self.discount or 0),
            'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0),
            'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0),
            'total': float(self.total or 0),
        }
        if with_progress:
            received = self.received_quantity()
            remaining = max(0.0, float(self.quantity or 0) - received)
            d['received_quantity'] = received
            d['remaining_quantity'] = remaining
            d['receipt_status'] = ('Fully Received' if remaining <= 0 else
                                    'Partially Received' if received > 0 else 'Open')
            item = ItemMaster.query.filter_by(item_code=self.item_code).first() if self.item_code else None
            d['store_type'] = (item.store or '') if item else ''
        return d

# 4. GOODS RECEIPT NOTE
#      FK: purchase_order_id → purchase_orders
# ─────────────────────────────────────────────────────────────────
class GoodsReceiptNote(db.Model):
    __tablename__ = 'purchase_goods_receipt_notes'
    goods_receipt_note_id = db.Column(db.Integer, primary_key=True)
    doc_no                = db.Column(db.String(20), unique=True)
    purchase_type         = db.Column(db.String(20))   # Assets / Expense
    kind                  = db.Column(db.String(20), default='Goods')
    purchase_order_id     = db.Column(db.Integer, db.ForeignKey('purchase_orders.purchase_order_id'))
    supplier_id             = db.Column(db.Integer, db.ForeignKey('suppliers.id'))
    contact_person        = db.Column(db.String(150))
    supplier_ref_no         = db.Column(db.String(100))
    account_code          = db.Column(db.String(20))
    status                = db.Column(db.String(20), default='Open')
    posting_status        = db.Column(db.String(10), nullable=False, default='Saved')  # Saved | Posted -- accounting state (GRL + Journal Entry created)
    posting_date          = db.Column(db.Date)
    delivery_date         = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    supplier = db.relationship('SupplierMaster', backref=db.backref('grns', lazy=True))
    purchase_order = db.relationship('PurchaseOrder', backref=db.backref('grn_docs', lazy=True))
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'purchase_type': self.purchase_type or '',
            'kind': self.kind or 'Goods',
            'id': self.goods_receipt_note_id,
            'goods_receipt_note_id': self.goods_receipt_note_id,
            'doc_no': self.doc_no or '',
            'purchase_order_id': self.purchase_order_id,
            'po_no': self.purchase_order.doc_no if self.purchase_order else '',
            'supplier_id': self.supplier_id,
            'supplier_name': self.supplier.supplier_name_en if self.supplier else '',
            'supplier_ref_no': self.supplier_ref_no or '',
            'account_code': self.account_code or '',
            'contact_person': self.contact_person or '', 'status': self.status,
            'posting_status': self.posting_status or 'Saved',
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 4L. GOODS RECEIPT LINE ITEMS
#      PK: goods_receipt_line_item_id
#      FK: goods_receipt_note_id → goods_receipt_notes
# ─────────────────────────────────────────────────────────────────
class GoodsReceiptLineItem(db.Model):
    __tablename__ = 'purchase_goods_receipt_notes_line_item'
    goods_receipt_line_item_id = db.Column(db.Integer, primary_key=True)
    goods_receipt_note_id = db.Column(db.Integer, db.ForeignKey('purchase_goods_receipt_notes.goods_receipt_note_id', ondelete='CASCADE'), nullable=False)
    purchase_order_line_item_id = db.Column(db.Integer, db.ForeignKey('purchase_order_line_items.purchase_order_line_item_id'))
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    goods_receipt_note = db.relationship('GoodsReceiptNote', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))
    purchase_order_line_item = db.relationship('PurchaseOrderLineItem')

    def to_dict(self):
        return {
            'id': self.goods_receipt_line_item_id,
            'goods_receipt_line_item_id': self.goods_receipt_line_item_id,
            'goods_receipt_note_id': self.goods_receipt_note_id,
            'purchase_order_line_item_id': self.purchase_order_line_item_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# 5. PURCHASE INVOICE
#      FK: purchase_order_id → purchase_orders
#      FK: goods_receipt_note_id → goods_receipt_notes
# ─────────────────────────────────────────────────────────────────
class PurchaseInvoice(db.Model):
    __tablename__ = 'purchase_invoices'
    purchase_invoice_id   = db.Column(db.Integer, primary_key=True)
    doc_no                = db.Column(db.String(20), unique=True)
    purchase_type         = db.Column(db.String(20))   # Assets / Expense
    kind                  = db.Column(db.String(20), default='Goods')
    payment_method        = db.Column(db.String(20), default='Credit')
    bank_account_id       = db.Column(db.Integer)
    purchase_order_id     = db.Column(db.Integer, db.ForeignKey('purchase_orders.purchase_order_id'))
    goods_receipt_note_id = db.Column(db.Integer, db.ForeignKey('purchase_goods_receipt_notes.goods_receipt_note_id'))
    supplier_id             = db.Column(db.Integer, db.ForeignKey('suppliers.id'))
    supplier_ref_no         = db.Column(db.String(100))
    tax_code              = db.Column(db.String(20))
    account_code          = db.Column(db.String(20))
    status                = db.Column(db.String(20), default='Open')
    posting_status        = db.Column(db.String(10), nullable=False, default='Saved')  # Saved | Posted -- accounting state (GRL + Journal Entry created)
    posting_date          = db.Column(db.Date)
    delivery_date         = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    from_date             = db.Column(db.Date)   # Invoice Period start
    to_date               = db.Column(db.Date)   # Invoice Period end -- its month is shown as the invoice's billing month
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    # Amount settled by posted Outgoing Payments against this invoice.
    # balance_due is intentionally NOT stored -- always computed as
    # total_incl_vat - paid_amount so it can never drift from the source total.
    paid_amount           = db.Column(db.Numeric(14, 2), default=0)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    supplier = db.relationship('SupplierMaster', backref=db.backref('purchase_invoices', lazy=True))
    purchase_order = db.relationship('PurchaseOrder', backref=db.backref('invoices', lazy=True))
    goods_receipt_note = db.relationship('GoodsReceiptNote', backref=db.backref('invoices', lazy=True))
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'purchase_type': self.purchase_type or '',
            'kind': self.kind or 'Goods',
            'payment_method': self.payment_method or 'Credit',
            'bank_account_id': self.bank_account_id,
            'id': self.purchase_invoice_id,
            'purchase_invoice_id': self.purchase_invoice_id,
            'doc_no': self.doc_no or '',
            'purchase_order_id': self.purchase_order_id,
            'po_no': self.purchase_order.doc_no if self.purchase_order else '',
            'goods_receipt_note_id': self.goods_receipt_note_id,
            'grn_no': self.goods_receipt_note.doc_no if self.goods_receipt_note else '',
            'supplier_id': self.supplier_id,
            'supplier_name': self.supplier.supplier_name_en if self.supplier else '',
            'supplier_ref_no': self.supplier_ref_no or '',
            'tax_code': self.tax_code or '', 'account_code': self.account_code or '', 'status': self.status,
            'posting_status': self.posting_status or 'Saved',
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'from_date': str(self.from_date) if self.from_date else '',
            'to_date': str(self.to_date) if self.to_date else '',
            'invoice_month': self.to_date.strftime('%B %Y') if self.to_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'paid_amount': float(self.paid_amount or 0),
            'balance_due': float(self.total_incl_vat or 0) - float(self.paid_amount or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 5L. PURCHASE INVOICE LINE ITEMS
#      PK: purchase_invoice_line_item_id
#      FK: purchase_invoice_id → purchase_invoices
# ─────────────────────────────────────────────────────────────────
class PurchaseInvoiceLineItem(db.Model):
    __tablename__ = 'purchase_invoice_line_items'
    purchase_invoice_line_item_id = db.Column(db.Integer, primary_key=True)
    purchase_invoice_id = db.Column(db.Integer, db.ForeignKey('purchase_invoices.purchase_invoice_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    purchase_invoice = db.relationship('PurchaseInvoice', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def returned_quantity(self, exclude_grr_id=None):
        """Total quantity already claimed against this invoice line by OTHER
        (non-Cancelled) Goods Return Requests. Goods Return Request has no
        Post/Draft distinction of its own, so every non-Cancelled row counts
        -- unlike GRN/PO, there is no separate "Posted" gate here."""
        q = (db.session.query(db.func.coalesce(db.func.sum(GoodsReturnLineItem.quantity), 0))
             .join(GoodsReturnRequest,
                   GoodsReturnRequest.goods_return_request_id == GoodsReturnLineItem.goods_return_request_id)
             .filter(GoodsReturnLineItem.purchase_invoice_line_item_id == self.purchase_invoice_line_item_id,
                     GoodsReturnRequest.status != 'Cancelled'))
        if exclude_grr_id:
            q = q.filter(GoodsReturnLineItem.goods_return_request_id != exclude_grr_id)
        return float(q.scalar() or 0)

    def remaining_quantity(self, exclude_grr_id=None):
        return max(0.0, float(self.quantity or 0) - self.returned_quantity(exclude_grr_id))

    def to_dict(self, with_progress=False, exclude_grr_id=None):
        d = {
            'id': self.purchase_invoice_line_item_id,
            'purchase_invoice_line_item_id': self.purchase_invoice_line_item_id,
            'purchase_invoice_id': self.purchase_invoice_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }
        if with_progress:
            returned = self.returned_quantity(exclude_grr_id)
            d['returned_quantity'] = returned
            d['remaining_quantity'] = max(0.0, float(self.quantity or 0) - returned)
        return d


# ─────────────────────────────────────────────────────────────────
# 6. GOODS RETURN REQUEST
#      FK: purchase_invoice_id → purchase_invoices
# ─────────────────────────────────────────────────────────────────
class GoodsReturnRequest(db.Model):
    __tablename__ = 'purchase_goods_return_requests'
    goods_return_request_id = db.Column(db.Integer, primary_key=True)
    doc_no                  = db.Column(db.String(20), unique=True)
    purchase_type         = db.Column(db.String(20))   # Assets / Expense
    kind                  = db.Column(db.String(20), default='Goods')
    purchase_invoice_id     = db.Column(db.Integer, db.ForeignKey('purchase_invoices.purchase_invoice_id'))
    supplier_id               = db.Column(db.Integer, db.ForeignKey('suppliers.id'))
    contact_person          = db.Column(db.String(150))
    supplier_ref_no           = db.Column(db.String(100))
    tax_code                 = db.Column(db.String(20))
    account_code             = db.Column(db.String(20))
    status                  = db.Column(db.String(20), default='Open')
    posting_date            = db.Column(db.Date)
    delivery_date           = db.Column(db.Date)
    document_date           = db.Column(db.Date)
    total_before_discount   = db.Column(db.Numeric(14, 2), default=0)
    total_discount          = db.Column(db.Numeric(14, 2), default=0)
    total_freight           = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat          = db.Column(db.Numeric(14, 2), default=0)
    vat_amount              = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat          = db.Column(db.Numeric(14, 2), default=0)
    created_at              = db.Column(db.DateTime, default=datetime.utcnow)
    created_by              = db.Column(db.Integer, db.ForeignKey('users.id'))

    supplier = db.relationship('SupplierMaster', backref=db.backref('grrs', lazy=True))
    purchase_invoice = db.relationship('PurchaseInvoice', backref=db.backref('return_requests', lazy=True))
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'purchase_type': self.purchase_type or '',
            'kind': self.kind or 'Goods',
            'id': self.goods_return_request_id,
            'goods_return_request_id': self.goods_return_request_id,
            'doc_no': self.doc_no or '',
            'purchase_invoice_id': self.purchase_invoice_id,
            'pi_no': self.purchase_invoice.doc_no if self.purchase_invoice else '',
            'supplier_id': self.supplier_id,
            'supplier_name': self.supplier.supplier_name_en if self.supplier else '',
            'supplier_ref_no': self.supplier_ref_no or '',
            'tax_code': self.tax_code or '',
            'account_code': self.account_code or '',
            'contact_person': self.contact_person or '', 'status': self.status,
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 6L. GOODS RETURN LINE ITEMS
#      PK: goods_return_line_item_id
#      FK: goods_return_request_id → goods_return_requests
# ─────────────────────────────────────────────────────────────────
class GoodsReturnLineItem(db.Model):
    __tablename__ = 'purchase_goods_return_notes_line_item'
    goods_return_line_item_id = db.Column(db.Integer, primary_key=True)
    goods_return_request_id = db.Column(db.Integer, db.ForeignKey('purchase_goods_return_requests.goods_return_request_id', ondelete='CASCADE'), nullable=False)
    purchase_invoice_line_item_id = db.Column(db.Integer, db.ForeignKey('purchase_invoice_line_items.purchase_invoice_line_item_id', ondelete='SET NULL'))
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    goods_return_request = db.relationship('GoodsReturnRequest', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))
    purchase_invoice_line_item = db.relationship('PurchaseInvoiceLineItem')

    def to_dict(self):
        return {
            'id': self.goods_return_line_item_id,
            'goods_return_line_item_id': self.goods_return_line_item_id,
            'goods_return_request_id': self.goods_return_request_id,
            'purchase_invoice_line_item_id': self.purchase_invoice_line_item_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# 6B. PURCHASE RETURN NOTE
#      Sits between Goods Return Request and Purchase Debit Memo in the
#      returns chain (GRR -> Purchase Return Note -> Purchase Debit Memo),
#      mirroring how Goods Receipt Note sits between Purchase Order and
#      Purchase Invoice on the goods-receiving side.
#      FK: purchase_good_return_request_id -> purchase_goods_return_requests
# ─────────────────────────────────────────────────────────────────
class PurchaseReturnNote(db.Model):
    __tablename__ = 'purchase_return_notes'
    purchase_good_return_note_id     = db.Column(db.Integer, primary_key=True)
    doc_no                           = db.Column(db.String(20), unique=True)
    purchase_type                    = db.Column(db.String(20))   # Assets / Expense
    kind                              = db.Column(db.String(20), default='Goods')
    purchase_good_return_request_id  = db.Column(db.Integer,
                                          db.ForeignKey('purchase_goods_return_requests.goods_return_request_id'))
    supplier_id                      = db.Column(db.Integer, db.ForeignKey('suppliers.id'))
    contact_person                   = db.Column(db.String(150))
    supplier_ref                     = db.Column(db.String(100))
    owner_id                         = db.Column(db.Integer, db.ForeignKey('owners.id'))
    account_code                     = db.Column(db.String(20))
    status                           = db.Column(db.String(20), default='Open')
    posting_status                   = db.Column(db.String(10), nullable=False, default='Saved')  # Saved | Posted -- accounting state (GRL + Journal Entry created)
    posting_date                     = db.Column(db.Date)
    delivery_date                    = db.Column(db.Date)
    document_date                    = db.Column(db.Date)
    total_before_discount            = db.Column(db.Numeric(14, 2), default=0)
    total_discount                   = db.Column(db.Numeric(14, 2), default=0)
    total_freight                    = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat                   = db.Column(db.Numeric(14, 2), default=0)
    vat_amount                       = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat                   = db.Column(db.Numeric(14, 2), default=0)
    created_at                       = db.Column(db.DateTime, default=datetime.utcnow)
    created_by                       = db.Column(db.Integer, db.ForeignKey('users.id'))

    supplier = db.relationship('SupplierMaster', backref=db.backref('purchase_return_notes', lazy=True))
    goods_return_request = db.relationship('GoodsReturnRequest', backref=db.backref('return_notes', lazy=True))
    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'id': self.purchase_good_return_note_id,
            'purchase_good_return_note_id': self.purchase_good_return_note_id,
            'doc_no': self.doc_no or '',
            'purchase_type': self.purchase_type or '',
            'kind': self.kind or 'Goods',
            'purchase_good_return_request_id': self.purchase_good_return_request_id,
            'grr_no': self.goods_return_request.doc_no if self.goods_return_request else '',
            'supplier_id': self.supplier_id,
            'supplier_name': self.supplier.supplier_name_en if self.supplier else '',
            'contact_person': self.contact_person or '',
            'supplier_ref': self.supplier_ref or '',
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'account_code': self.account_code or '',
            'status': self.status,
            'posting_status': self.posting_status or 'Saved',
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 6C. PURCHASE RETURN NOTE LINE ITEMS
#      PK: purchase_good_return_note_line_item_id
#      FK: purchase_good_return_note_id -> purchase_return_notes
# ─────────────────────────────────────────────────────────────────
class PurchaseReturnNoteLineItem(db.Model):
    __tablename__ = 'purchase_return_note_line_items'
    purchase_good_return_note_line_item_id = db.Column(db.Integer, primary_key=True)
    purchase_good_return_note_id = db.Column(db.Integer,
                                      db.ForeignKey('purchase_return_notes.purchase_good_return_note_id', ondelete='CASCADE'),
                                      nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    purchase_return_note = db.relationship('PurchaseReturnNote', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.purchase_good_return_note_line_item_id,
            'purchase_good_return_note_line_item_id': self.purchase_good_return_note_line_item_id,
            'purchase_good_return_note_id': self.purchase_good_return_note_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# 7. PURCHASE DEBIT MEMO
#      FK: purchase_return_note_id → purchase_return_notes
#      FK: purchase_invoice_id     → purchase_invoices
# ─────────────────────────────────────────────────────────────────
class PurchaseDebitMemo(db.Model):
    __tablename__ = 'purchase_debit_memos'
    purchase_debit_memo_id  = db.Column(db.Integer, primary_key=True)
    doc_no                  = db.Column(db.String(20), unique=True)
    purchase_type         = db.Column(db.String(20))   # Assets / Expense
    kind                  = db.Column(db.String(20), default='Goods')
    payment_method        = db.Column(db.String(20), default='Credit')
    bank_account_id       = db.Column(db.Integer)
    purchase_return_note_id = db.Column(db.Integer, db.ForeignKey('purchase_return_notes.purchase_good_return_note_id'))
    purchase_invoice_id     = db.Column(db.Integer, db.ForeignKey('purchase_invoices.purchase_invoice_id'))
    supplier_id               = db.Column(db.Integer, db.ForeignKey('suppliers.id'))
    contact_person          = db.Column(db.String(150))
    supplier_ref_no           = db.Column(db.String(100))
    owner_id                 = db.Column(db.Integer, db.ForeignKey('owners.id'))
    tax_code                 = db.Column(db.String(20))
    account_code             = db.Column(db.String(20))
    status                  = db.Column(db.String(20), default='Open')
    posting_status          = db.Column(db.String(10), nullable=False, default='Saved')  # Saved | Posted -- accounting state (GRL + Journal Entry created)
    posting_date            = db.Column(db.Date)
    delivery_date           = db.Column(db.Date)
    document_date           = db.Column(db.Date)
    total_before_discount   = db.Column(db.Numeric(14, 2), default=0)
    total_discount          = db.Column(db.Numeric(14, 2), default=0)
    total_freight           = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat          = db.Column(db.Numeric(14, 2), default=0)
    vat_amount              = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat          = db.Column(db.Numeric(14, 2), default=0)
    created_at              = db.Column(db.DateTime, default=datetime.utcnow)
    created_by              = db.Column(db.Integer, db.ForeignKey('users.id'))

    supplier = db.relationship('SupplierMaster', backref=db.backref('pdms', lazy=True))
    purchase_return_note = db.relationship('PurchaseReturnNote', backref=db.backref('debit_memos', lazy=True))
    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'purchase_type': self.purchase_type or '',
            'kind': self.kind or 'Goods',
            'payment_method': self.payment_method or 'Credit',
            'bank_account_id': self.bank_account_id,
            'id': self.purchase_debit_memo_id,
            'purchase_debit_memo_id': self.purchase_debit_memo_id,
            'doc_no': self.doc_no or '',
            'purchase_return_note_id': self.purchase_return_note_id,
            'return_note_no': self.purchase_return_note.doc_no if self.purchase_return_note else '',
            'purchase_invoice_id': self.purchase_invoice_id,
            'supplier_id': self.supplier_id,
            'supplier_name': self.supplier.supplier_name_en if self.supplier else '',
            'supplier_ref_no': self.supplier_ref_no or '',
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'tax_code': self.tax_code or '',
            'account_code': self.account_code or '',
            'contact_person': self.contact_person or '', 'status': self.status,
            'posting_status': self.posting_status or 'Saved',
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 7L. PURCHASE DEBIT MEMO LINE ITEMS
#      PK: purchase_debit_memo_line_item_id
#      FK: purchase_debit_memo_id → purchase_debit_memos
# ─────────────────────────────────────────────────────────────────
class PurchaseDebitMemoLineItem(db.Model):
    __tablename__ = 'purchase_debit_memo_line_items'
    purchase_debit_memo_line_item_id = db.Column(db.Integer, primary_key=True)
    purchase_debit_memo_id = db.Column(db.Integer, db.ForeignKey('purchase_debit_memos.purchase_debit_memo_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    purchase_debit_memo = db.relationship('PurchaseDebitMemo', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.purchase_debit_memo_line_item_id,
            'purchase_debit_memo_line_item_id': self.purchase_debit_memo_line_item_id,
            'purchase_debit_memo_id': self.purchase_debit_memo_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# PURCHASE ATTACHMENTS  (shared — doc_type + doc_id)
# ─────────────────────────────────────────────────────────────────
class PurchaseAttachment(db.Model):
    __tablename__ = 'purchase_attachments'
    id          = db.Column(db.Integer, primary_key=True)
    doc_type    = db.Column(db.String(10), nullable=False)
    doc_id      = db.Column(db.Integer,    nullable=False)
    filename    = db.Column(db.String(255), nullable=False)
    filepath    = db.Column(db.String(500), nullable=False)
    file_size   = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    def to_dict(self):
        return {
            'id': self.id, 'doc_type': self.doc_type, 'doc_id': self.doc_id,
            'filename': self.filename, 'filepath': self.filepath,
            'file_size': self.file_size or 0,
        }


# ─────────────────────────────────────────────────────────────────
# ITEM MASTER
# ─────────────────────────────────────────────────────────────────
class ItemCategory(db.Model):
    __tablename__ = 'item_categories'
    id         = db.Column(db.Integer, primary_key=True)
    name_en    = db.Column(db.String(100), nullable=False)
    name_ar    = db.Column(db.Unicode(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name_en': self.name_en, 'name_ar': self.name_ar or ''}


class ItemSubCategory(db.Model):
    __tablename__ = 'item_sub_categories'
    id          = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('item_categories.id'), nullable=False)
    name_en     = db.Column(db.String(100), nullable=False)
    name_ar     = db.Column(db.Unicode(100))
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    category = db.relationship('ItemCategory', backref=db.backref('sub_categories', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {'id': self.id, 'category_id': self.category_id,
                'name_en': self.name_en, 'name_ar': self.name_ar or ''}


class ItemMaster(db.Model):
    __tablename__ = 'item_master'
    id                 = db.Column(db.Integer, primary_key=True)
    item_code          = db.Column(db.String(50), unique=True, nullable=False)
    item_type          = db.Column(db.String(20), default='Product')  # Product / Service
    article_no         = db.Column(db.String(50))
    name_en            = db.Column(db.String(200), nullable=False)
    name_ar            = db.Column(db.Unicode(200))
    print_name         = db.Column(db.String(200))
    print_name_en      = db.Column(db.String(200))
    print_name_ar      = db.Column(db.Unicode(200))
    uom                = db.Column(db.String(20), default='unit')
    item_desc          = db.Column(db.Text)
    category_id        = db.Column(db.Integer, db.ForeignKey('item_categories.id'))
    sub_category_id    = db.Column(db.Integer, db.ForeignKey('item_sub_categories.id'))
    supplier_id          = db.Column(db.Integer, db.ForeignKey('suppliers.id'))
    store              = db.Column(db.String(40))   # Fixed Asset / Consumable / Non-Consumable Store
    expense_type       = db.Column(db.String(20))   # Assets / Expense
    main_rate          = db.Column(db.Numeric(14, 2), default=0)
    po_rate            = db.Column(db.Numeric(14, 2), default=0)
    last_purchase_rate = db.Column(db.Numeric(14, 2), default=0)
    retail_rate        = db.Column(db.Numeric(14, 2), default=0)
    wholesale_rate     = db.Column(db.Numeric(14, 2), default=0)
    special_rate       = db.Column(db.Numeric(14, 2), default=0)
    mrp                = db.Column(db.Numeric(14, 2), default=0)
    minimum_sp         = db.Column(db.Numeric(14, 2), default=0)
    is_active          = db.Column(db.Boolean, default=True)
    levelfive_code       = db.Column(db.String(40))
    levelfive_drawer_en  = db.Column(db.String(250))
    levelfive_drawer_ar  = db.Column(db.Unicode(250))
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)
    created_by         = db.Column(db.Integer, db.ForeignKey('users.id'))

    category     = db.relationship('ItemCategory',    backref=db.backref('items', lazy=True))
    sub_category = db.relationship('ItemSubCategory', backref=db.backref('items', lazy=True))
    supplier       = db.relationship('SupplierMaster',    backref=db.backref('items',  lazy=True))

    def to_dict(self):
        return {
            'id': self.id, 'item_code': self.item_code,
            'item_type': self.item_type or 'Product',
            'article_no': self.article_no or '',
            'name_en': self.name_en, 'name_ar': self.name_ar or '',
            'print_name': self.print_name or '',
            'print_name_en': self.print_name_en or '',
            'print_name_ar': self.print_name_ar or '',
            'uom': self.uom or 'unit', 'item_desc': self.item_desc or '',
            'category_id': self.category_id,
            'category_name': self.category.name_en if self.category else '',
            'sub_category_id': self.sub_category_id,
            'sub_category_name': self.sub_category.name_en if self.sub_category else '',
            'tax_rate': 15,
            'supplier_id': self.supplier_id,
            'main_rate':          float(self.main_rate          or 0),
            'po_rate':            float(self.po_rate            or 0),
            'last_purchase_rate': float(self.last_purchase_rate or 0),
            'retail_rate':        float(self.retail_rate        or 0),
            'wholesale_rate':     float(self.wholesale_rate     or 0),
            'special_rate':       float(self.special_rate       or 0),
            'mrp':                float(self.mrp                or 0),
            'minimum_sp':         float(self.minimum_sp         or 0),
            'is_active': self.is_active,
            'levelfive_code': self.levelfive_code or '',
            'levelfive_drawer_en': self.levelfive_drawer_en or '',
            'levelfive_drawer_ar': self.levelfive_drawer_ar or '',
            'store': self.store or '',
            'expense_type': self.expense_type or '',
            'uoms': [u.to_dict() for u in self.uoms] if self.uoms else [],
        }


# ─────────────────────────────────────────────────────────────────
# STORES — Fixed Asset / Consumable / Non-Consumable (3 fixed master rows).
# Item Master's `store` field ("Store Type") is the item's permanent
# destination store; a Purchase Good Receipt Note never lets the user pick
# one manually -- it's always resolved automatically from the item.
# ─────────────────────────────────────────────────────────────────
STORE_TYPES = ['Fixed Asset Store', 'Consumable Store', 'Non-Consumable Store']


class Store(db.Model):
    __tablename__ = 'stores'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(60), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name': self.name}


def seed_stores():
    """Idempotent: called from init_db() so the 3 fixed stores always exist."""
    existing = {s.name for s in Store.query.all()}
    for name in STORE_TYPES:
        if name not in existing:
            db.session.add(Store(name=name))
    db.session.commit()


class StoreTransaction(db.Model):
    """One row per quantity movement into a Store. Created only when a
    Purchase Good Receipt Note is Posted (never on a plain Save/Draft) --
    the durable audit trail linking PO -> GRN -> Store for every quantity
    that ever moved. If a Posted GRN is later edited or deleted, its
    transactions are marked Reversed (never deleted), then fresh ones are
    created for the new quantities -- so PO/Store balances (always computed
    live from Active rows only) and the full history both stay correct."""
    __tablename__ = 'store_transactions'
    id                          = db.Column(db.Integer, primary_key=True)
    store_id                    = db.Column(db.Integer, db.ForeignKey('stores.id', ondelete='SET NULL'))
    store_type                  = db.Column(db.String(60))
    item_id                     = db.Column(db.Integer, db.ForeignKey('item_master.id', ondelete='SET NULL'))
    item_code                   = db.Column(db.String(50))
    item_name                   = db.Column(db.String(200))
    uom                         = db.Column(db.String(20))
    quantity                    = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    purchase_order_id           = db.Column(db.Integer, db.ForeignKey('purchase_orders.purchase_order_id', ondelete='SET NULL'))
    purchase_order_doc_no       = db.Column(db.String(20))
    purchase_order_line_item_id = db.Column(db.Integer, db.ForeignKey('purchase_order_line_items.purchase_order_line_item_id', ondelete='SET NULL'))
    goods_receipt_note_id       = db.Column(db.Integer, db.ForeignKey('purchase_goods_receipt_notes.goods_receipt_note_id', ondelete='SET NULL'))
    goods_receipt_note_doc_no   = db.Column(db.String(20))
    goods_receipt_line_item_id  = db.Column(db.Integer, db.ForeignKey('purchase_goods_receipt_notes_line_item.goods_receipt_line_item_id', ondelete='SET NULL'))
    supplier_id                 = db.Column(db.Integer, db.ForeignKey('suppliers.id', ondelete='SET NULL'))
    supplier_name               = db.Column(db.String(200))
    unit_price                  = db.Column(db.Numeric(14, 4), default=0)
    total_amount                = db.Column(db.Numeric(14, 2), default=0)
    transaction_type            = db.Column(db.String(30), nullable=False, default='Purchase Receipt')
    transaction_date            = db.Column(db.Date)
    posting_date                = db.Column(db.Date)
    created_by                  = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at                  = db.Column(db.DateTime, default=datetime.utcnow)
    status                      = db.Column(db.String(20), nullable=False, default='Active')  # Active | Reversed

    # ── Sales-side columns (Delivery Note stock decrement) -- mirror the
    #    Purchase-side columns above; a Delivery row sets these, leaving
    #    the Purchase-side FKs null, and vice versa. Delivered quantity is
    #    stored NEGATIVE (transaction_type='Sales Delivery') so the
    #    existing Purchase-side SUM(quantity) balance queries above net
    #    correctly with zero changes to that code. ──
    sales_order_id              = db.Column(db.Integer, db.ForeignKey('sales_orders.sales_order_id', ondelete='SET NULL'))
    sales_order_doc_no          = db.Column(db.String(20))
    sales_order_line_item_id    = db.Column(db.Integer, db.ForeignKey('sales_order_line_items.sales_order_line_item_id', ondelete='SET NULL'))
    delivery_note_id            = db.Column(db.Integer, db.ForeignKey('sale_delivery_notes.delivery_note_id', ondelete='SET NULL'))
    delivery_note_doc_no        = db.Column(db.String(20))
    delivery_line_item_id       = db.Column(db.Integer, db.ForeignKey('sale_delivery_line_items.delivery_line_item_id', ondelete='SET NULL'))
    buyer_id                    = db.Column(db.Integer, db.ForeignKey('buyers.id', ondelete='SET NULL'))
    buyer_name                  = db.Column(db.String(200))

    # ── Standalone Purchase Invoice stock receipt (no GRN in the chain) --
    #    set ONLY when a Purchase Invoice with no linked GRN is Posted, since
    #    that is then the sole document ever receiving the goods. A
    #    GRN-linked invoice never sets these -- its GRN already owns the
    #    stock movement, so posting the invoice must not add stock again. ──
    purchase_invoice_id           = db.Column(db.Integer, db.ForeignKey('purchase_invoices.purchase_invoice_id', ondelete='SET NULL'))
    purchase_invoice_doc_no       = db.Column(db.String(20))
    purchase_invoice_line_item_id = db.Column(db.Integer, db.ForeignKey('purchase_invoice_line_items.purchase_invoice_line_item_id', ondelete='SET NULL'))

    store   = db.relationship('Store')
    item    = db.relationship('ItemMaster')
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'id': self.id,
            'store_id': self.store_id,
            'store_name': self.store.name if self.store else (self.store_type or ''),
            'store_type': self.store_type or '',
            'item_id': self.item_id,
            'item_code': self.item_code or '',
            'item_name': self.item_name or '',
            'uom': self.uom or '',
            'quantity': float(self.quantity or 0),
            'purchase_order_id': self.purchase_order_id,
            'purchase_order_doc_no': self.purchase_order_doc_no or '',
            'purchase_order_line_item_id': self.purchase_order_line_item_id,
            'goods_receipt_note_id': self.goods_receipt_note_id,
            'goods_receipt_note_doc_no': self.goods_receipt_note_doc_no or '',
            'goods_receipt_line_item_id': self.goods_receipt_line_item_id,
            'purchase_invoice_id': self.purchase_invoice_id,
            # Set directly when THIS row was created by a standalone (no-GRN)
            # Purchase Invoice being Posted; otherwise (a GRN-linked receipt)
            # derived by looking up whichever invoice(s) later got linked to
            # the same GRN, purely informational and never a stock owner.
            'purchase_invoice_doc_no': self.purchase_invoice_doc_no or (', '.join(
                pi.doc_no for pi in PurchaseInvoice.query.filter_by(goods_receipt_note_id=self.goods_receipt_note_id).all() if pi.doc_no
            ) if self.goods_receipt_note_id else ''),
            'purchase_invoice_line_item_id': self.purchase_invoice_line_item_id,
            'supplier_id': self.supplier_id,
            'supplier_name': self.supplier_name or '',
            'sales_order_id': self.sales_order_id,
            'sales_order_doc_no': self.sales_order_doc_no or '',
            'sales_order_line_item_id': self.sales_order_line_item_id,
            'delivery_note_id': self.delivery_note_id,
            'delivery_note_doc_no': self.delivery_note_doc_no or '',
            'delivery_line_item_id': self.delivery_line_item_id,
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer_name or '',
            'unit_price': float(self.unit_price or 0),
            'total_amount': float(self.total_amount or 0),
            'transaction_type': self.transaction_type,
            'transaction_date': str(self.transaction_date) if self.transaction_date else '',
            'posting_date': str(self.posting_date) if self.posting_date else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'status': self.status,
        }


# ─────────────────────────────────────────────────────────────────
# UNIT OF MEASUREMENT (master list) + ITEM ↔ UOM (multi per item)
# ─────────────────────────────────────────────────────────────────
class ItemUnitMeasurement(db.Model):
    __tablename__ = 'item_unit_measurement'
    id         = db.Column(db.Integer, primary_key=True)
    item_id    = db.Column(db.Integer, db.ForeignKey('item_master.id', ondelete='CASCADE'), nullable=False)
    uom_id     = db.Column(db.Integer, db.ForeignKey('item_unit.id'), nullable=False)
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship('ItemMaster', backref=db.backref('uoms', lazy=True, cascade='all,delete-orphan'))
    uom  = db.relationship('ItemUnit', backref=db.backref('item_links', lazy=True))

    def to_dict(self):
        return {
            'id': self.id,
            'item_id': self.item_id,
            'uom_id': self.uom_id,
            'unit_name': self.uom.name_en if self.uom else '',
            'unit_name_ar': (self.uom.name_ar or '') if self.uom else '',
            'is_default': bool(self.is_default),
        }

# ═════════════════════════════════════════════════════════════════
#  SALES DOCUMENTS  (mirror of the purchase chain)
#  SR → SQ → SO → DN → SINV → SRR → SCM   —  all reference buyers
#  Auto-generated to match the purchase module structure.
# ═════════════════════════════════════════════════════════════════


class SalesRequest(db.Model):
    __tablename__ = 'sales_requests'
    sales_request_id   = db.Column(db.Integer, primary_key=True)
    doc_no                = db.Column(db.String(20), unique=True)
    kind                  = db.Column(db.String(20), default='Goods')
    requester             = db.Column(db.String(150))
    requester_name        = db.Column(db.String(200))
    owner_id              = db.Column(db.Integer, db.ForeignKey('owners.id'))
    status                = db.Column(db.String(20), default='Open')
    posting_date          = db.Column(db.Date)
    valid_until           = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    required_date         = db.Column(db.Date)
    remarks               = db.Column(db.Text)
    tax_code              = db.Column(db.String(20))
    account_code          = db.Column(db.String(20))
    approved_by           = db.Column(db.String(150))
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'kind': self.kind or 'Goods',
            'id': self.sales_request_id,
            'sales_request_id': self.sales_request_id,
            'doc_no': self.doc_no or '', 'requester': self.requester or '',
            'requester_name': self.requester_name or '',
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'status': self.status,
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'valid_until':   str(self.valid_until)   if self.valid_until   else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'required_date': str(self.required_date) if self.required_date else '',
            'remarks': self.remarks or '', 'tax_code': self.tax_code or '',
            'account_code': self.account_code or '',
            'approved_by': self.approved_by or '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 1L. PURCHASE REQUEST LINE ITEMS
#      PK: sales_request_line_item_id
#      FK: sales_request_id → sales_requests
# ─────────────────────────────────────────────────────────────────
class SalesRequestLineItem(db.Model):
    __tablename__ = 'sales_request_line_items'
    sales_request_line_item_id = db.Column(db.Integer, primary_key=True)
    sales_request_id = db.Column(db.Integer, db.ForeignKey('sales_requests.sales_request_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    sales_request = db.relationship('SalesRequest', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.sales_request_line_item_id,
            'sales_request_line_item_id': self.sales_request_line_item_id,
            'sales_request_id': self.sales_request_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# 2. PURCHASE QUOTATION
#      FK: sales_request_id → sales_requests
# ─────────────────────────────────────────────────────────────────
class SalesQuotation(db.Model):
    __tablename__ = 'sales_quotations'
    sales_quotation_id = db.Column(db.Integer, primary_key=True)
    doc_no                = db.Column(db.String(20), unique=True)
    kind                  = db.Column(db.String(20), default='Goods')
    sales_request_id   = db.Column(db.Integer, db.ForeignKey('sales_requests.sales_request_id'))
    requester             = db.Column(db.String(150))
    requester_name        = db.Column(db.String(200))
    buyer_id             = db.Column(db.Integer, db.ForeignKey('buyers.id'))
    buyer_ref_no         = db.Column(db.String(100))
    owner_id              = db.Column(db.Integer, db.ForeignKey('owners.id'))
    status                = db.Column(db.String(20), default='Open')
    posting_date          = db.Column(db.Date)
    valid_until           = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    required_date         = db.Column(db.Date)
    subject               = db.Column(db.String(300))
    remarks               = db.Column(db.Text)
    body                  = db.Column(db.Text)
    tax_code              = db.Column(db.String(20))
    account_code          = db.Column(db.String(20))
    report_style          = db.Column(db.String(20), default='header_footer')  # 'header_footer' (Owner's Header/Footer images) | 'default' (built-in letterhead)
    item_summary_display  = db.Column(db.String(10), default='on')  # 'on' | 'off' -- whether the print/Word output shows the line-item Scope & Rate table + totals
    terms_conditions      = db.Column(db.Text)
    sign_stamp            = db.Column(db.Text)
    approved_by           = db.Column(db.String(150))
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    buyer = db.relationship('BuyerMaster', backref=db.backref('sales_quotations', lazy=True))
    sales_request = db.relationship('SalesRequest', backref=db.backref('sales_quotations', lazy=True))
    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'kind': self.kind or 'Goods',
            'id': self.sales_quotation_id,
            'sales_quotation_id': self.sales_quotation_id,
            'doc_no': self.doc_no or '',
            'sales_request_id': self.sales_request_id,
            'pr_doc_no': self.sales_request.doc_no if self.sales_request else '',
            'requester': self.requester or '',
            'requester_name': self.requester_name or '',
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer.buyer_name_en if self.buyer else '',
            'buyer_ref_no': self.buyer_ref_no or '',
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'status': self.status,
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'valid_until':   str(self.valid_until)   if self.valid_until   else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'required_date': str(self.required_date) if self.required_date else '',
            'subject': self.subject or '',
            'remarks': self.remarks or '', 'body': self.body or '', 'tax_code': self.tax_code or '',
            'account_code': self.account_code or '',
            'report_style': self.report_style or 'header_footer',
            'item_summary_display': self.item_summary_display or 'on',
            'terms_conditions': self.terms_conditions or '',
            'sign_stamp': self.sign_stamp or '',
            'approved_by': self.approved_by or '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 2L. PURCHASE QUOTATION LINE ITEMS
#      PK: sales_quotation_line_item_id
#      FK: sales_quotation_id → sales_quotations
# ─────────────────────────────────────────────────────────────────
class SalesQuotationLineItem(db.Model):
    __tablename__ = 'sales_quotation_line_items'
    sales_quotation_line_item_id = db.Column(db.Integer, primary_key=True)
    sales_quotation_id = db.Column(db.Integer, db.ForeignKey('sales_quotations.sales_quotation_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    sales_quotation = db.relationship('SalesQuotation', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.sales_quotation_line_item_id,
            'sales_quotation_line_item_id': self.sales_quotation_line_item_id,
            'sales_quotation_id': self.sales_quotation_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────
# PURCHASE ORDER HEADER
# ─────────────────────────────────────────────────────────────────
class SalesOrder(db.Model):
    __tablename__ = 'sales_orders'
    sales_order_id     = db.Column(db.Integer, primary_key=True)
    doc_no                = db.Column(db.String(20), unique=True)
    kind                  = db.Column(db.String(20), default='Goods')
    sales_quotation_id = db.Column(db.Integer, db.ForeignKey('sales_quotations.sales_quotation_id'))
    buyer_id             = db.Column(db.Integer, db.ForeignKey('buyers.id'))
    buyer_ref_no         = db.Column(db.String(100))
    owner_id              = db.Column(db.Integer, db.ForeignKey('owners.id'))
    remarks               = db.Column(db.Text)
    tax_code              = db.Column(db.String(20))
    account_code          = db.Column(db.String(20))
    status                = db.Column(db.String(20), default='Open')
    posting_date          = db.Column(db.Date)
    delivery_date         = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    buyer = db.relationship('BuyerMaster', backref=db.backref('sales_orders', lazy=True))
    sq = db.relationship('SalesQuotation', backref=db.backref('sales_orders', lazy=True))
    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'kind': self.kind or 'Goods',
            'id': self.sales_order_id,
            'sales_order_id': self.sales_order_id,
            'doc_no': self.doc_no or '',
            'sales_quotation_id': self.sales_quotation_id,
            'sq_id': self.sales_quotation_id,
            'sq_doc_no': self.sq.doc_no if self.sq else '',
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer.buyer_name_en if self.buyer else '',
            'buyer_ref_no': self.buyer_ref_no or '',
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'remarks': self.remarks or '', 'tax_code': self.tax_code or '',
            'account_code': self.account_code or '',
            'status': self.status,
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# PURCHASE ORDER LINE ITEMS
# ─────────────────────────────────────────────────────────────────
class SalesOrderLineItem(db.Model):
    __tablename__ = 'sales_order_line_items'
    sales_order_line_item_id = db.Column(db.Integer, primary_key=True)
    sales_order_id = db.Column(db.Integer, db.ForeignKey('sales_orders.sales_order_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    sales_order = db.relationship('SalesOrder', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def delivered_quantity(self):
        """Total quantity actually POSTED (Delivery Note Posted) against
        this SO line -- summed from Active Store Transactions only, never
        from a merely-saved/draft Delivery Note. Mirrors
        PurchaseOrderLineItem.received_quantity(); delivery rows are stored
        as NEGATIVE quantities (see StoreTransaction), hence abs()."""
        total = (db.session.query(db.func.coalesce(db.func.sum(StoreTransaction.quantity), 0))
                 .filter(StoreTransaction.sales_order_line_item_id == self.sales_order_line_item_id,
                         StoreTransaction.transaction_type == 'Sales Delivery',
                         StoreTransaction.status == 'Active')
                 .scalar())
        return abs(float(total or 0))

    def remaining_quantity(self):
        return max(0.0, float(self.quantity or 0) - self.delivered_quantity())

    def delivery_status(self):
        delivered = self.delivered_quantity()
        remaining = max(0.0, float(self.quantity or 0) - delivered)
        if remaining <= 0:
            return 'Fully Delivered'
        if delivered > 0:
            return 'Partially Delivered'
        return 'Open'

    def to_dict(self, with_progress=False):
        d = {
            'id': self.sales_order_line_item_id,
            'sales_order_line_item_id': self.sales_order_line_item_id,
            'sales_order_id': self.sales_order_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '',
            'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '',
            'uom': self.uom,
            'quantity': float(self.quantity or 0),
            'rate': float(self.rate or 0),
            'discount': float(self.discount or 0),
            'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0),
            'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0),
            'total': float(self.total or 0),
        }
        if with_progress:
            delivered = self.delivered_quantity()
            remaining = max(0.0, float(self.quantity or 0) - delivered)
            d['delivered_quantity'] = delivered
            d['remaining_quantity'] = remaining
            d['delivery_status'] = ('Fully Delivered' if remaining <= 0 else
                                     'Partially Delivered' if delivered > 0 else 'Open')
            item = ItemMaster.query.filter_by(item_code=self.item_code).first() if self.item_code else None
            d['store_type'] = (item.store or '') if item else ''
            # Current on-hand stock balance for this item -- same query shape
            # as _check_dn_stock_availability() in database/routes/sales.py,
            # so the Delivery Note UI can show/cap against it up front
            # instead of the user only finding out at Save/Post time.
            balance = (db.session.query(db.func.coalesce(db.func.sum(StoreTransaction.quantity), 0))
                       .filter(StoreTransaction.item_code == self.item_code,
                               StoreTransaction.status == 'Active')
                       .scalar()) if self.item_code else 0
            d['stock_available'] = float(balance or 0)
        return d
# 4. GOODS RECEIPT NOTE
#      FK: sales_order_id → sales_orders
# ─────────────────────────────────────────────────────────────────
class DeliveryNote(db.Model):
    __tablename__ = 'sale_delivery_notes'
    delivery_note_id = db.Column(db.Integer, primary_key=True)
    doc_no                = db.Column(db.String(20), unique=True)
    kind                  = db.Column(db.String(20), default='Goods')
    sales_order_id     = db.Column(db.Integer, db.ForeignKey('sales_orders.sales_order_id'))
    buyer_id             = db.Column(db.Integer, db.ForeignKey('buyers.id'))
    contact_person        = db.Column(db.String(150))
    buyer_ref_no         = db.Column(db.String(100))
    owner_id              = db.Column(db.Integer, db.ForeignKey('owners.id'))
    tax_code              = db.Column(db.String(20))
    account_code          = db.Column(db.String(20))
    status                = db.Column(db.String(20), default='Open')
    posting_status        = db.Column(db.String(10), nullable=False, default='Saved')  # Saved | Posted -- accounting + stock state
    posting_date          = db.Column(db.Date)
    delivery_date         = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    buyer = db.relationship('BuyerMaster', backref=db.backref('delivery_notes', lazy=True))
    sales_order = db.relationship('SalesOrder', backref=db.backref('dn_docs', lazy=True))
    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'kind': self.kind or 'Goods',
            'id': self.delivery_note_id,
            'delivery_note_id': self.delivery_note_id,
            'doc_no': self.doc_no or '',
            'sales_order_id': self.sales_order_id,
            'so_no': self.sales_order.doc_no if self.sales_order else '',
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer.buyer_name_en if self.buyer else '',
            'buyer_ref_no': self.buyer_ref_no or '',
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'tax_code': self.tax_code or '',
            'account_code': self.account_code or '',
            'contact_person': self.contact_person or '', 'status': self.status,
            'posting_status': self.posting_status or 'Saved',
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 4L. GOODS RECEIPT LINE ITEMS
#      PK: delivery_line_item_id
#      FK: delivery_note_id → delivery_notes
# ─────────────────────────────────────────────────────────────────
class DeliveryLineItem(db.Model):
    __tablename__ = 'sale_delivery_line_items'
    delivery_line_item_id = db.Column(db.Integer, primary_key=True)
    delivery_note_id = db.Column(db.Integer, db.ForeignKey('sale_delivery_notes.delivery_note_id', ondelete='CASCADE'), nullable=False)
    sales_order_line_item_id = db.Column(db.Integer, db.ForeignKey('sales_order_line_items.sales_order_line_item_id'))
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    delivery_note = db.relationship('DeliveryNote', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.delivery_line_item_id,
            'delivery_line_item_id': self.delivery_line_item_id,
            'delivery_note_id': self.delivery_note_id,
            'sales_order_line_item_id': self.sales_order_line_item_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# 5. PURCHASE INVOICE
#      FK: sales_order_id → sales_orders
#      FK: delivery_note_id → delivery_notes
# ─────────────────────────────────────────────────────────────────
class SalesInvoice(db.Model):
    __tablename__ = 'sales_invoices'
    sales_invoice_id   = db.Column(db.Integer, primary_key=True)
    # Widened from the original VARCHAR(20): document numbers now embed the
    # full transaction_type as their prefix (e.g. STD-INV-2026-1000), which
    # a plain SLI-2026-1000 number never needed as much room for.
    doc_no                = db.Column(db.String(40), unique=True)
    kind                  = db.Column(db.String(20), default='Goods')
    payment_method        = db.Column(db.String(20), default='Credit')
    bank_account_id       = db.Column(db.Integer)
    owner_id              = db.Column(db.Integer, db.ForeignKey('owners.id'))
    sales_order_id     = db.Column(db.Integer, db.ForeignKey('sales_orders.sales_order_id'))
    delivery_note_id = db.Column(db.Integer, db.ForeignKey('sale_delivery_notes.delivery_note_id'))
    buyer_id             = db.Column(db.Integer, db.ForeignKey('buyers.id'))
    buyer_ref_no         = db.Column(db.String(100))
    # STD-INV, STD-DR, STD-CR, SIM-INV, SIM-DR, SIM-CR (current dropdown
    # values -- see database/zatca/engine.py's SINV_TRANSACTION_TYPES);
    # older invoices may still carry the legacy STD_INV/STD_CR/STD_DR/
    # SMP_INV/SMP_CR/SMP_DR underscore-separated values, never migrated.
    transaction_type     = db.Column(db.String(20))
    invoice_category     = db.Column(db.String(20))   # standard | simplified -- derived from transaction_type, not independently user-editable
    reference_invoices   = db.Column(db.String(300))  # comma-separated sales_invoice_id list
    project_ref          = db.Column(db.String(150))
    tax_code              = db.Column(db.String(20))
    account_code          = db.Column(db.String(20))
    status                = db.Column(db.String(20), default='Open')
    posting_status        = db.Column(db.String(10), nullable=False, default='Saved')  # Saved | Posted -- accounting state (GRL + Journal Entry created)
    posting_date          = db.Column(db.Date)
    delivery_date         = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    from_date             = db.Column(db.Date)   # Invoice Period start
    to_date               = db.Column(db.Date)   # Invoice Period end -- its month is shown as the invoice's billing month
    total_before_discount = db.Column(db.Numeric(14, 2), default=0)
    total_discount        = db.Column(db.Numeric(14, 2), default=0)
    total_freight         = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat        = db.Column(db.Numeric(14, 2), default=0)
    vat_amount            = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat        = db.Column(db.Numeric(14, 2), default=0)
    paid_amount           = db.Column(db.Numeric(14, 2), default=0)   # cumulative Incoming Payments settled against this invoice
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'))

    # ── ZATCA Phase 2 chain state (see database/zatca/engine.py) ──
    zatca_uuid             = db.Column(db.String(36))
    zatca_icv              = db.Column(db.BigInteger)
    zatca_pih              = db.Column(db.String(200))
    zatca_invoice_hash     = db.Column(db.String(200))
    zatca_xml_signature    = db.Column(db.Text)
    zatca_qr_code          = db.Column(db.Text)
    zatca_status           = db.Column(db.String(20), default='not_generated')
    zatca_submission_type  = db.Column(db.String(20))
    zatca_cleared_xml_path = db.Column(db.String(500))
    zatca_response_message = db.Column(db.Text)
    zatca_generated_at     = db.Column(db.DateTime)
    zatca_submitted_at     = db.Column(db.DateTime)
    zatca_attachment_id    = db.Column(db.Integer)

    buyer = db.relationship('BuyerMaster', backref=db.backref('sales_invoices', lazy=True))
    sales_order = db.relationship('SalesOrder', backref=db.backref('sales_invoices_link', lazy=True))
    delivery_note = db.relationship('DeliveryNote', backref=db.backref('sales_invoices_link', lazy=True))
    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'kind': self.kind or 'Goods',
            'payment_method': self.payment_method or 'Credit',
            'bank_account_id': self.bank_account_id,
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'tax_code': self.tax_code or '',
            'account_code': self.account_code or '',
            'id': self.sales_invoice_id,
            'sales_invoice_id': self.sales_invoice_id,
            'doc_no': self.doc_no or '',
            'sales_order_id': self.sales_order_id,
            'so_no': self.sales_order.doc_no if self.sales_order else '',
            'delivery_note_id': self.delivery_note_id,
            'dn_no': self.delivery_note.doc_no if self.delivery_note else '',
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer.buyer_name_en if self.buyer else '',
            'buyer_ref_no': self.buyer_ref_no or '', 'status': self.status,
            'transaction_type': self.transaction_type or '',
            'invoice_category': self.invoice_category or '',
            'reference_invoices': self.reference_invoices or '',
            'project_ref': self.project_ref or '',
            'posting_status': self.posting_status or 'Saved',
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'from_date': str(self.from_date) if self.from_date else '',
            'to_date': str(self.to_date) if self.to_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'paid_amount': float(self.paid_amount or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
            'zatca_uuid': self.zatca_uuid or '',
            'zatca_icv': self.zatca_icv,
            'zatca_status': self.zatca_status or 'not_generated',
            'zatca_submission_type': self.zatca_submission_type or '',
            'zatca_response_message': self.zatca_response_message or '',
            'zatca_attachment_id': self.zatca_attachment_id,
            'zatca_generated_at': self.zatca_generated_at.strftime('%d/%m/%Y %H:%M') if self.zatca_generated_at else '',
        }


# ─────────────────────────────────────────────────────────────────
# 5L. PURCHASE INVOICE LINE ITEMS
#      PK: sales_invoice_line_item_id
#      FK: sales_invoice_id → sales_invoices
# ─────────────────────────────────────────────────────────────────
class SalesInvoiceLineItem(db.Model):
    __tablename__ = 'sales_invoice_line_items'
    sales_invoice_line_item_id = db.Column(db.Integer, primary_key=True)
    sales_invoice_id = db.Column(db.Integer, db.ForeignKey('sales_invoices.sales_invoice_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    sales_invoice = db.relationship('SalesInvoice', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.sales_invoice_line_item_id,
            'sales_invoice_line_item_id': self.sales_invoice_line_item_id,
            'sales_invoice_id': self.sales_invoice_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# 6. GOODS RETURN REQUEST
#      FK: sales_invoice_id → sales_invoices
# ─────────────────────────────────────────────────────────────────
class SalesReturnRequest(db.Model):
    __tablename__ = 'sales_return_requests'
    sales_return_request_id = db.Column(db.Integer, primary_key=True)
    doc_no                  = db.Column(db.String(20), unique=True)
    kind                  = db.Column(db.String(20), default='Goods')
    sales_invoice_id     = db.Column(db.Integer, db.ForeignKey('sales_invoices.sales_invoice_id'))
    buyer_id               = db.Column(db.Integer, db.ForeignKey('buyers.id'))
    contact_person          = db.Column(db.String(150))
    buyer_ref_no           = db.Column(db.String(100))
    owner_id                = db.Column(db.Integer, db.ForeignKey('owners.id'))
    tax_code                = db.Column(db.String(20))
    account_code            = db.Column(db.String(20))
    status                  = db.Column(db.String(20), default='Open')
    posting_date            = db.Column(db.Date)
    delivery_date           = db.Column(db.Date)
    document_date           = db.Column(db.Date)
    total_before_discount   = db.Column(db.Numeric(14, 2), default=0)
    total_discount          = db.Column(db.Numeric(14, 2), default=0)
    total_freight           = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat          = db.Column(db.Numeric(14, 2), default=0)
    vat_amount              = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat          = db.Column(db.Numeric(14, 2), default=0)
    created_at              = db.Column(db.DateTime, default=datetime.utcnow)
    created_by              = db.Column(db.Integer, db.ForeignKey('users.id'))

    buyer = db.relationship('BuyerMaster', backref=db.backref('sales_return_requests', lazy=True))
    sales_invoice = db.relationship('SalesInvoice', backref=db.backref('sales_return_requests_link', lazy=True))
    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'kind': self.kind or 'Goods',
            'id': self.sales_return_request_id,
            'sales_return_request_id': self.sales_return_request_id,
            'doc_no': self.doc_no or '',
            'sales_invoice_id': self.sales_invoice_id,
            'si_no': self.sales_invoice.doc_no if self.sales_invoice else '',
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer.buyer_name_en if self.buyer else '',
            'buyer_ref_no': self.buyer_ref_no or '',
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'tax_code': self.tax_code or '',
            'account_code': self.account_code or '',
            'contact_person': self.contact_person or '', 'status': self.status,
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 6L. GOODS RETURN LINE ITEMS
#      PK: sales_return_line_item_id
#      FK: sales_return_request_id → sales_return_requests
# ─────────────────────────────────────────────────────────────────
class SalesReturnLineItem(db.Model):
    __tablename__ = 'sales_return_line_items'
    sales_return_line_item_id = db.Column(db.Integer, primary_key=True)
    sales_return_request_id = db.Column(db.Integer, db.ForeignKey('sales_return_requests.sales_return_request_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    sales_return_request = db.relationship('SalesReturnRequest', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.sales_return_line_item_id,
            'sales_return_line_item_id': self.sales_return_line_item_id,
            'sales_return_request_id': self.sales_return_request_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# 6M. SALE RETURN NOTE
#      Sits between Sales Return Request and Sales Credit Memo:
#      SRR -> Sale Return Note -> Sales Credit Memo
#      Mirrors PurchaseReturnNote (purchase side), including GL posting
#      support via GRL.sales_return_note_id.
#      FK: sales_return_request_id -> sales_return_requests
# ─────────────────────────────────────────────────────────────────
class SalesReturnNote(db.Model):
    __tablename__ = 'sales_return_notes'
    sales_return_note_id    = db.Column(db.Integer, primary_key=True)
    doc_no                  = db.Column(db.String(20), unique=True)
    kind                    = db.Column(db.String(20), default='Goods')
    sales_return_request_id = db.Column(db.Integer,
                                  db.ForeignKey('sales_return_requests.sales_return_request_id'))
    buyer_id                = db.Column(db.Integer, db.ForeignKey('buyers.id'))
    contact_person          = db.Column(db.String(150))
    buyer_ref               = db.Column(db.String(100))
    owner_id                = db.Column(db.Integer, db.ForeignKey('owners.id'))
    account_code            = db.Column(db.String(20))
    status                  = db.Column(db.String(20), default='Open')
    posting_status          = db.Column(db.String(10), nullable=False, default='Saved')  # Saved | Posted -- accounting state (GRL + Journal Entry created)
    posting_date            = db.Column(db.Date)
    delivery_date           = db.Column(db.Date)
    document_date           = db.Column(db.Date)
    total_before_discount   = db.Column(db.Numeric(14, 2), default=0)
    total_discount          = db.Column(db.Numeric(14, 2), default=0)
    total_freight            = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat          = db.Column(db.Numeric(14, 2), default=0)
    vat_amount              = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat          = db.Column(db.Numeric(14, 2), default=0)
    created_at              = db.Column(db.DateTime, default=datetime.utcnow)
    created_by              = db.Column(db.Integer, db.ForeignKey('users.id'))

    buyer = db.relationship('BuyerMaster', backref=db.backref('sales_return_notes', lazy=True))
    sales_return_request = db.relationship('SalesReturnRequest', backref=db.backref('return_notes', lazy=True))
    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'id': self.sales_return_note_id,
            'sales_return_note_id': self.sales_return_note_id,
            'doc_no': self.doc_no or '',
            'kind': self.kind or 'Goods',
            'sales_return_request_id': self.sales_return_request_id,
            'srr_no': self.sales_return_request.doc_no if self.sales_return_request else '',
            'sales_invoice_id': (self.sales_return_request.sales_invoice_id if self.sales_return_request else None),
            'si_no': (self.sales_return_request.sales_invoice.doc_no
                      if self.sales_return_request and self.sales_return_request.sales_invoice else ''),
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer.buyer_name_en if self.buyer else '',
            'contact_person': self.contact_person or '',
            'buyer_ref': self.buyer_ref or '',
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'account_code': self.account_code or '',
            'status': self.status,
            'posting_status': self.posting_status or 'Saved',
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 6N. SALE RETURN NOTE LINE ITEMS
#      PK: sales_return_note_line_item_id
#      FK: sales_return_note_id -> sales_return_notes
# ─────────────────────────────────────────────────────────────────
class SalesReturnNoteLineItem(db.Model):
    __tablename__ = 'sales_return_note_line_items'
    sales_return_note_line_item_id = db.Column(db.Integer, primary_key=True)
    sales_return_note_id = db.Column(db.Integer,
                                  db.ForeignKey('sales_return_notes.sales_return_note_id', ondelete='CASCADE'),
                                  nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    sales_return_note = db.relationship('SalesReturnNote', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.sales_return_note_line_item_id,
            'sales_return_note_line_item_id': self.sales_return_note_line_item_id,
            'sales_return_note_id': self.sales_return_note_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


# ─────────────────────────────────────────────────────────────────
# 7. PURCHASE DEBIT MEMO
#      FK: sales_return_request_id → sales_return_requests
#      FK: sales_invoice_id     → sales_invoices
# ─────────────────────────────────────────────────────────────────
class SalesCreditMemo(db.Model):
    __tablename__ = 'sales_credit_memos'
    sales_credit_memo_id  = db.Column(db.Integer, primary_key=True)
    doc_no                  = db.Column(db.String(20), unique=True)
    kind                  = db.Column(db.String(20), default='Goods')
    payment_method        = db.Column(db.String(20), default='Credit')
    bank_account_id       = db.Column(db.Integer)
    owner_id              = db.Column(db.Integer, db.ForeignKey('owners.id'))
    sales_return_request_id = db.Column(db.Integer, db.ForeignKey('sales_return_requests.sales_return_request_id'))
    sales_return_note_id = db.Column(db.Integer, db.ForeignKey('sales_return_notes.sales_return_note_id'))
    sales_invoice_id     = db.Column(db.Integer, db.ForeignKey('sales_invoices.sales_invoice_id'))
    buyer_id               = db.Column(db.Integer, db.ForeignKey('buyers.id'))
    contact_person          = db.Column(db.String(150))
    buyer_ref_no           = db.Column(db.String(100))
    tax_code                = db.Column(db.String(20))
    account_code            = db.Column(db.String(20))
    status                  = db.Column(db.String(20), default='Open')
    posting_status          = db.Column(db.String(10), nullable=False, default='Saved')  # Saved | Posted -- accounting state (GRL + Journal Entry created)
    posting_date            = db.Column(db.Date)
    delivery_date           = db.Column(db.Date)
    document_date           = db.Column(db.Date)
    total_before_discount   = db.Column(db.Numeric(14, 2), default=0)
    total_discount          = db.Column(db.Numeric(14, 2), default=0)
    total_freight           = db.Column(db.Numeric(14, 2), default=0)
    total_excl_vat          = db.Column(db.Numeric(14, 2), default=0)
    vat_amount              = db.Column(db.Numeric(14, 2), default=0)
    total_incl_vat          = db.Column(db.Numeric(14, 2), default=0)
    created_at              = db.Column(db.DateTime, default=datetime.utcnow)
    created_by              = db.Column(db.Integer, db.ForeignKey('users.id'))

    buyer = db.relationship('BuyerMaster', backref=db.backref('sales_credit_memos', lazy=True))
    sales_return_request = db.relationship('SalesReturnRequest', backref=db.backref('sales_credit_memos_link', lazy=True))
    sales_return_note = db.relationship('SalesReturnNote', backref=db.backref('sales_credit_memos', lazy=True))
    sales_invoice = db.relationship('SalesInvoice', foreign_keys=[sales_invoice_id])
    owner = db.relationship('Owner', foreign_keys=[owner_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'kind': self.kind or 'Goods',
            'payment_method': self.payment_method or 'Credit',
            'bank_account_id': self.bank_account_id,
            'owner_id': self.owner_id,
            'owner_name': self.owner.name if self.owner else '',
            'tax_code': self.tax_code or '',
            'account_code': self.account_code or '',
            'id': self.sales_credit_memo_id,
            'sales_credit_memo_id': self.sales_credit_memo_id,
            'doc_no': self.doc_no or '',
            'sales_return_request_id': self.sales_return_request_id,
            'srr_no': self.sales_return_request.doc_no if self.sales_return_request else '',
            'sales_return_note_id': self.sales_return_note_id,
            'srn_no': self.sales_return_note.doc_no if self.sales_return_note else '',
            'sales_invoice_id': self.sales_invoice_id,
            'si_no': self.sales_invoice.doc_no if self.sales_invoice else '',
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer.buyer_name_en if self.buyer else '',
            'buyer_ref_no': self.buyer_ref_no or '',
            'contact_person': self.contact_person or '', 'status': self.status,
            'posting_status': self.posting_status or 'Saved',
            'posting_date':  str(self.posting_date)  if self.posting_date  else '',
            'delivery_date': str(self.delivery_date) if self.delivery_date else '',
            'document_date': str(self.document_date) if self.document_date else '',
            'total_before_discount': float(self.total_before_discount or 0),
            'total_discount': float(self.total_discount or 0),
            'total_freight':  float(self.total_freight  or 0),
            'total_excl_vat': float(self.total_excl_vat or 0),
            'vat_amount':     float(self.vat_amount     or 0),
            'total_incl_vat': float(self.total_incl_vat or 0),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'created_by': self.created_by,
            'created_by_name': self.creator.username if self.creator else '',
        }


# ─────────────────────────────────────────────────────────────────
# 7L. PURCHASE DEBIT MEMO LINE ITEMS
#      PK: sales_credit_memo_line_item_id
#      FK: sales_credit_memo_id → sales_credit_memos
# ─────────────────────────────────────────────────────────────────
class SalesCreditMemoLineItem(db.Model):
    __tablename__ = 'sales_credit_memo_line_items'
    sales_credit_memo_line_item_id = db.Column(db.Integer, primary_key=True)
    sales_credit_memo_id = db.Column(db.Integer, db.ForeignKey('sales_credit_memos.sales_credit_memo_id', ondelete='CASCADE'), nullable=False)
    line_number   = db.Column(db.Integer, nullable=False, default=1)
    item_code     = db.Column(db.String(50))
    description   = db.Column(db.String(500))
    warehouse     = db.Column(db.String(150))
    uom           = db.Column(db.String(20),    nullable=False, default='unit')
    quantity      = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    rate          = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    discount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    freight       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable       = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    tax_code      = db.Column(db.String(20),    nullable=False, default='VAT15')
    tax_amount    = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total         = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    sales_credit_memo = db.relationship('SalesCreditMemo', backref=db.backref('line_items', lazy=True, cascade='all,delete-orphan'))

    def to_dict(self):
        return {
            'id': self.sales_credit_memo_line_item_id,
            'sales_credit_memo_line_item_id': self.sales_credit_memo_line_item_id,
            'sales_credit_memo_id': self.sales_credit_memo_id,
            'line_number': self.line_number,
            'item_code': self.item_code or '', 'item_desc': self.description or '',
            'description': self.description or '',
            'warehouse': self.warehouse or '', 'uom': self.uom,
            'quantity': float(self.quantity or 0), 'rate': float(self.rate or 0),
            'discount': float(self.discount or 0), 'freight': float(self.freight or 0),
            'taxable': float(self.taxable or 0), 'tax_code': self.tax_code,
            'tax_amount': float(self.tax_amount or 0), 'total': float(self.total or 0),
        }


class SalesAttachment(db.Model):
    __tablename__ = 'sales_attachments'
    id          = db.Column(db.Integer, primary_key=True)
    doc_type    = db.Column(db.String(10), nullable=False)
    doc_id      = db.Column(db.Integer,    nullable=False)
    filename    = db.Column(db.String(255), nullable=False)
    filepath    = db.Column(db.String(500), nullable=False)
    file_size   = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    def to_dict(self):
        return {
            'id': self.id, 'doc_type': self.doc_type, 'doc_id': self.doc_id,
            'filename': self.filename, 'filepath': self.filepath,
            'file_size': self.file_size or 0,
        }



# ═════════════════════════════════════════════════════════════════
#  CHART OF ACCOUNTS  —  Level 1 (level_one) & Level 2 (level_two)
#  Level 1 = the fixed financial-statement elements (A, L, E, R, ...)
#  Level 2 = heading accounts under each Level 1, auto-coded A1, A2, ...
# ═════════════════════════════════════════════════════════════════

class LevelOne(db.Model):
    """Chart of Accounts — Level 1 (top-level financial statement elements).

    ``code`` is a single, fixed letter (A, L, E, ...) and cannot be edited
    once created. ``code_length`` is always 1.
    """
    __tablename__ = 'level_one'

    id          = db.Column(db.Integer, primary_key=True)
    code_length = db.Column(db.Integer, nullable=False, default=1)          # fixed = 1
    code        = db.Column(db.String(1), nullable=False, unique=True)      # A, L, E, ...
    drawers     = db.Column(db.String(100), nullable=False)
    drawers_ar  = db.Column(db.Unicode(100))
    description = db.Column(db.String(255), nullable=False)
    description_ar = db.Column(db.Unicode(255))
    status      = db.Column(db.String(10), nullable=False, default='active')  # active | inactive
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    # One Level 1 has many Level 2 rows.
    level_twos  = db.relationship(
        'LevelTwo',
        backref=db.backref('level_one', lazy=True),
        lazy=True,
        cascade='all, delete-orphan',
        order_by='LevelTwo.id',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'code_length': self.code_length,
            'code': self.code,
            'drawers': self.drawers,
            'drawers_ar': self.drawers_ar or '',
            'description': self.description,
            'description_ar': self.description_ar or '',
            'status': self.status or 'active',
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'level_two_count': len(self.level_twos) if self.level_twos is not None else 0,
        }


class LevelTwo(db.Model):
    """Chart of Accounts — Level 2 (heading accounts under a Level 1).

    ``code`` is generated automatically as ``<LevelOneCode><n>`` (A1, A2, ...)
    with an independent sequence per Level 1. ``code_length`` is always 2 and
    ``description`` is always 'Heading Account'.
    """
    __tablename__ = 'level_two'

    id             = db.Column(db.Integer, primary_key=True)
    code_length    = db.Column(db.Integer, nullable=False, default=2)       # fixed = 2
    level_one_id   = db.Column(db.Integer, db.ForeignKey('level_one.id', ondelete='CASCADE'), nullable=False)
    level_one_code = db.Column(db.String(1), nullable=False)                # denormalised for fast search
    code           = db.Column(db.String(10), nullable=False, unique=True)  # A1, A2, ...
    drawers        = db.Column(db.String(150), nullable=False)
    drawers_ar     = db.Column(db.Unicode(150))
    description    = db.Column(db.String(255), nullable=False, default='Heading Account')
    description_ar = db.Column(db.Unicode(255))
    status         = db.Column(db.String(10), nullable=False, default='active')  # active | inactive
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'code_length': self.code_length,
            'level_one_id': self.level_one_id,
            'level_one_code': self.level_one_code,
            'code': self.code,
            'drawers': self.drawers,
            'drawers_ar': self.drawers_ar or '',
            'description': self.description,
            'description_ar': self.description_ar or '',
            'status': self.status or 'active',
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
        }


# ── Default data for the two tables ──────────────────────────────
# (code_length, code, drawers, description)
LEVEL_ONE_DEFAULTS = [
    (1, 'A', 'Asset',             'Elements of Financial Statements'),
    (1, 'L', 'Liabilities',       'Elements of Financial Statements'),
    (1, 'E', 'Equity & Reserve',  'Elements of Financial Statements'),
    (1, 'R', 'Revenue',           'Elements of Financial Statements'),
    (1, 'C', 'Cost of Revenue',   'Elements of Financial Statements'),
    (1, 'O', 'Operating Cost',    'Elements of Financial Statements'),
    (1, 'F', 'Finance Cost',      'Elements of Financial Statements'),
    (1, 'I', 'OCI',               'Elements of Financial Statements'),
]

# (level_one_code, code, drawers)  — description is always 'Heading Account'
LEVEL_TWO_DEFAULTS = [
    ('A', 'A1', 'Non-current Assets'),
    ('A', 'A2', 'Current Assets'),
    ('L', 'L1', 'Non-current Liabilities'),
    ('L', 'L2', 'Current Liabilities'),
    ('E', 'E1', 'Equity'),
    ('E', 'E2', 'Reserves'),
    ('R', 'R1', 'Operating Revenue'),
    ('R', 'R2', 'Non-operative Revenue'),
    ('C', 'C1', 'Cost of Revenue'),
    ('O', 'O1', 'Operating Cost'),
    ('F', 'F1', 'Finance Cost'),
]


def seed_chart_of_accounts():
    """Idempotently insert the default Level 1 and Level 2 records.

    Safe to call on every startup — existing rows (matched by ``code``) are
    never duplicated. Must be called inside an application context.
    """
    inserted_l1 = 0
    for code_length, code, drawers, description in LEVEL_ONE_DEFAULTS:
        if not LevelOne.query.filter_by(code=code).first():
            db.session.add(LevelOne(
                code_length=1,               # always 1
                code=code,
                drawers=drawers,
                description=description,
            ))
            inserted_l1 += 1
    if inserted_l1:
        db.session.commit()

    # Map Level 1 code -> id for the Level 2 foreign keys.
    l1_by_code = {l1.code: l1 for l1 in LevelOne.query.all()}

    inserted_l2 = 0
    for l1_code, code, drawers in LEVEL_TWO_DEFAULTS:
        parent = l1_by_code.get(l1_code)
        if not parent:
            continue
        if not LevelTwo.query.filter_by(code=code).first():
            db.session.add(LevelTwo(
                code_length=2,               # always 2
                level_one_id=parent.id,
                level_one_code=parent.code,
                code=code,
                drawers=drawers,
                description='Heading Account',
            ))
            inserted_l2 += 1
    if inserted_l2:
        db.session.commit()

    return {'level_one_inserted': inserted_l1, 'level_two_inserted': inserted_l2}


# ═════════════════════════════════════════════════════════════════
#  CHART OF ACCOUNTS — Levels 3, 4, 5
#  L3: <L2code>-NN        e.g. A1-01   (code_length 5,  Heading Account)
#  L4: <L3code>-NN        e.g. A1-01-01(code_length 8,  Heading Account)
#  L5: <L4code>-NNN       e.g. A1-01-01-001 (code_length 12, Transactional Account)
# ═════════════════════════════════════════════════════════════════

class LevelThree(db.Model):
    """Level 3 — heading accounts under a Level 2 (code: A1-01)."""
    __tablename__ = 'level_three'

    id             = db.Column(db.Integer, primary_key=True)
    code_length    = db.Column(db.Integer, nullable=False, default=5)        # fixed = 5
    level_two_id   = db.Column(db.Integer, db.ForeignKey('level_two.id', ondelete='RESTRICT'), nullable=False)
    level_two_code = db.Column(db.String(10), nullable=False)                # denormalised for search
    code           = db.Column(db.String(20), nullable=False, unique=True)
    drawers        = db.Column(db.String(200), nullable=False)
    drawers_ar     = db.Column(db.Unicode(200))
    description    = db.Column(db.String(255), nullable=False, default='Heading Account')
    description_ar = db.Column(db.Unicode(255))
    status         = db.Column(db.String(10), nullable=False, default='active')  # active | inactive
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    level_two   = db.relationship('LevelTwo', backref=db.backref('level_threes', lazy=True))
    level_fours = db.relationship('LevelFour', backref=db.backref('level_three', lazy=True), lazy=True)

    def to_dict(self):
        return {
            'id': self.id, 'code_length': self.code_length,
            'level_two_id': self.level_two_id, 'level_two_code': self.level_two_code,
            'code': self.code, 'drawers': self.drawers, 'drawers_ar': self.drawers_ar or '',
            'description': self.description, 'description_ar': self.description_ar or '',
            'status': self.status or 'active',
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'child_count': len(self.level_fours) if self.level_fours is not None else 0,
        }


class LevelFour(db.Model):
    """Level 4 — heading accounts under a Level 3 (code: A1-01-01)."""
    __tablename__ = 'level_four'

    id               = db.Column(db.Integer, primary_key=True)
    code_length      = db.Column(db.Integer, nullable=False, default=8)      # fixed = 8
    level_three_id   = db.Column(db.Integer, db.ForeignKey('level_three.id', ondelete='RESTRICT'), nullable=False)
    level_three_code = db.Column(db.String(20), nullable=False)
    code             = db.Column(db.String(30), nullable=False, unique=True)
    drawers          = db.Column(db.String(200), nullable=False)
    drawers_ar       = db.Column(db.Unicode(200))
    description      = db.Column(db.String(255), nullable=False, default='Heading Account')
    description_ar   = db.Column(db.Unicode(255))
    status           = db.Column(db.String(10), nullable=False, default='active')  # active | inactive
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    level_fives = db.relationship('LevelFive', backref=db.backref('level_four', lazy=True), lazy=True)

    def to_dict(self):
        return {
            'id': self.id, 'code_length': self.code_length,
            'level_three_id': self.level_three_id, 'level_three_code': self.level_three_code,
            'code': self.code, 'drawers': self.drawers, 'drawers_ar': self.drawers_ar or '',
            'description': self.description, 'description_ar': self.description_ar or '',
            'status': self.status or 'active',
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'child_count': len(self.level_fives) if self.level_fives is not None else 0,
        }


class LevelFive(db.Model):
    """Level 5 — transactional accounts under a Level 4 (code: A1-01-01-001)."""
    __tablename__ = 'level_five'

    id              = db.Column(db.Integer, primary_key=True)
    code_length     = db.Column(db.Integer, nullable=False, default=12)      # fixed = 12
    level_four_id   = db.Column(db.Integer, db.ForeignKey('level_four.id', ondelete='RESTRICT'), nullable=False)
    level_four_code = db.Column(db.String(30), nullable=False)
    code            = db.Column(db.String(40), nullable=False, unique=True)
    drawers         = db.Column(db.String(250), nullable=False)
    drawers_ar      = db.Column(db.Unicode(250))
    description     = db.Column(db.String(255), nullable=False, default='Transactional Account')
    description_ar  = db.Column(db.Unicode(255))
    control_account = db.Column(db.String(3), nullable=False, default='No')  # Yes | No
    status          = db.Column(db.String(10), nullable=False, default='active')  # active | inactive
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'code_length': self.code_length,
            'level_four_id': self.level_four_id, 'level_four_code': self.level_four_code,
            'code': self.code, 'drawers': self.drawers, 'drawers_ar': self.drawers_ar or '',
            'description': self.description, 'description_ar': self.description_ar or '',
            'control_account': self.control_account or 'No',
            'status': self.status or 'active',
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
        }


# ── Auto code generation (shared by routes and the seeder) ───────
def _next_child_code(parent_code, model, code_col, parent_id_col, parent_id, width):
    """Return ``<parent_code>-<n>`` zero-padded to ``width`` digits.

    Scans existing children of ``parent_id`` for the highest numeric suffix and
    increments it. Starts at 1 when the parent has no children, so the first
    child of A1 is A1-01 and the first child of A1-01-01 is A1-01-01-001.
    """
    import re as _re
    rows = model.query.filter(parent_id_col == parent_id).all()
    pattern = _re.compile(r'^' + _re.escape(parent_code) + r'-(\d+)$')
    highest = 0
    for r in rows:
        m = pattern.match(getattr(r, 'code') or '')
        if m:
            highest = max(highest, int(m.group(1)))
    candidate = f'{parent_code}-{highest + 1:0{width}d}'
    # Guard against collisions from manually inserted codes.
    while model.query.filter(code_col == candidate).first():
        highest += 1
        candidate = f'{parent_code}-{highest + 1:0{width}d}'
    return candidate


def next_level_three_code(level_two):
    return _next_child_code(level_two.code, LevelThree, LevelThree.code,
                            LevelThree.level_two_id, level_two.id, 2)


def next_level_four_code(level_three):
    return _next_child_code(level_three.code, LevelFour, LevelFour.code,
                            LevelFour.level_three_id, level_three.id, 2)


def next_level_five_code(level_four):
    return _next_child_code(level_four.code, LevelFive, LevelFive.code,
                            LevelFive.level_four_id, level_four.id, 3)


def seed_coa_levels_3_4_5():
    """Idempotently insert the Level 3/4/5 defaults.

    Records are matched on (parent, drawers); codes are generated by the same
    algorithm the UI uses, so re-running never creates duplicates.
    """
    from database.routes.coa_seed_data import (LEVEL_THREE_SEED, LEVEL_FOUR_SEED, LEVEL_FIVE_SEED)
    counts = {'level_three': 0, 'level_four': 0, 'level_five': 0}

    # ---- Level 3 (parent = level_two.code) ----
    l2_by_code = {r.code: r for r in LevelTwo.query.all()}
    for parent_code, drawers in LEVEL_THREE_SEED:
        parent = l2_by_code.get(parent_code)
        if not parent:
            continue
        exists = LevelThree.query.filter_by(level_two_id=parent.id, drawers=drawers).first()
        if exists:
            continue
        db.session.add(LevelThree(
            code_length=5, level_two_id=parent.id, level_two_code=parent.code,
            code=next_level_three_code(parent), drawers=drawers,
            description='Heading Account',
        ))
        db.session.flush()          # so the next code sees this row
        counts['level_three'] += 1
    if counts['level_three']:
        db.session.commit()

    # ---- Level 4 (parent = level_three.code) ----
    l3_by_code = {r.code: r for r in LevelThree.query.all()}
    for parent_code, drawers in LEVEL_FOUR_SEED:
        parent = l3_by_code.get(parent_code)
        if not parent:
            continue
        exists = LevelFour.query.filter_by(level_three_id=parent.id, drawers=drawers).first()
        if exists:
            continue
        db.session.add(LevelFour(
            code_length=8, level_three_id=parent.id, level_three_code=parent.code,
            code=next_level_four_code(parent), drawers=drawers,
            description='Heading Account',
        ))
        db.session.flush()
        counts['level_four'] += 1
    if counts['level_four']:
        db.session.commit()

    # ---- Level 5 (parent = level_four.code) ----
    l4_by_code = {r.code: r for r in LevelFour.query.all()}
    for parent_code, drawers in LEVEL_FIVE_SEED:
        parent = l4_by_code.get(parent_code)
        if not parent:
            continue
        exists = LevelFive.query.filter_by(level_four_id=parent.id, drawers=drawers).first()
        if exists:
            continue
        db.session.add(LevelFive(
            code_length=12, level_four_id=parent.id, level_four_code=parent.code,
            code=next_level_five_code(parent), drawers=drawers,
            description='Transactional Account',
        ))
        db.session.flush()
        counts['level_five'] += 1
    if counts['level_five']:
        db.session.commit()

    return counts


# ═════════════════════════════════════════════════════════════════
#  DEPARTMENT LOCATION  +  BUYER DEPARTMENT
#  A buyer (company / HR) has many departments; each department sits
#  at one location. Locations are a shared lookup with quick-add.
# ═════════════════════════════════════════════════════════════════

class BuyerDepartment(db.Model):
    """A department belonging to a buyer. The location is entered manually
    on the buyer form and stored here (no lookup table)."""
    __tablename__ = 'buyer_departments'

    id                 = db.Column(db.Integer, primary_key=True)
    buyer_id           = db.Column(db.Integer, db.ForeignKey('buyers.id', ondelete='CASCADE'),
                                   nullable=False)
    department_name    = db.Column(db.String(150), nullable=False)
    department_name_ar = db.Column(db.Unicode(150))
    location_name      = db.Column(db.String(150))
    location_name_ar   = db.Column(db.Unicode(150))
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)

    buyer    = db.relationship('BuyerMaster',
                               backref=db.backref('departments', lazy=True,
                                                  cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id,
            'buyer_id': self.buyer_id,
            'department_name': self.department_name or '',
            'department_name_ar': self.department_name_ar or '',
            'location_name': self.location_name or '',
            'location_name_ar': self.location_name_ar or '',
        }


# ═════════════════════════════════════════════════════════════════
# FINANCIAL YEAR (Master) + FINANCIAL YEAR DETAIL (Financial Months)
# ═════════════════════════════════════════════════════════════════

import calendar as _calendar

_MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']


class FinancialYear(db.Model):
    __tablename__ = 'financial_year'
    id             = db.Column(db.Integer, primary_key=True)
    financial_year = db.Column(db.String(20), unique=True, nullable=False)  # FY-2026
    range          = db.Column(db.String(60))                               # 01-Jan-2026 → 31-Dec-2026
    year           = db.Column(db.Integer)                                  # 2026
    status         = db.Column(db.String(10), default='Open')               # Open / Closed
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    months = db.relationship('FinancialYearDetail', backref='parent',
                             cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'financial_year': self.financial_year,
            'range': self.range or '',
            'year': self.year,
            'status': self.status or 'Open',
            'months_count': len(self.months),
        }


class FinancialYearDetail(db.Model):
    __tablename__ = 'financial_year_detail'
    id                = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer,
                                  db.ForeignKey('financial_year.id', ondelete='CASCADE'),
                                  nullable=False)
    financial_year    = db.Column(db.String(20))   # FY-2026 (copied from parent)
    label             = db.Column(db.String(30))    # FM-Jan-2026
    range             = db.Column(db.String(60))    # 01-Jan-2026 → 31-Jan-2026
    month_no          = db.Column(db.Integer)       # 1..12
    status            = db.Column(db.String(10), default='Open')
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'financial_year_id': self.financial_year_id,
            'financial_year': self.financial_year or '',
            'label': self.label or '',
            'range': self.range or '',
            'month_no': self.month_no,
            'status': self.status or 'Open',
        }


def build_financial_months(year):
    """Return a list of 12 dicts describing each financial month for a year,
    with correct last-day handling (incl. leap-year February)."""
    fy = f'FY-{year}'
    out = []
    for m in range(1, 13):
        last = _calendar.monthrange(year, m)[1]     # correct leap-year Feb
        abbr = _MONTH_ABBR[m - 1]
        rng  = f'01-{abbr}-{year} → {last:02d}-{abbr}-{year}'
        out.append({
            'financial_year': fy,
            'label': f'FM-{abbr}-{year}',
            'range': rng,
            'month_no': m,
            'status': 'Open',
        })
    return out


# ═════════════════════════════════════════════════════════════════
# PURCHASE / SALES TAX CODES
# ═════════════════════════════════════════════════════════════════

class PurchaseTaxCode(db.Model):
    __tablename__ = 'purchase_tax_code'
    id           = db.Column(db.Integer, primary_key=True)
    tax_code     = db.Column(db.String(60), nullable=False, index=True)   # VAT 15%
    account_code = db.Column(db.String(20), unique=True, nullable=False, index=True)  # P1
    section      = db.Column(db.String(255), nullable=False)
    status       = db.Column(db.String(10), default='Active')             # Active / Inactive
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'account_code': self.account_code,
            'tax_code': self.tax_code, 'section': self.section,
            'status': self.status or 'Active',
        }


class SalesTaxCode(db.Model):
    __tablename__ = 'sales_tax_code'
    id           = db.Column(db.Integer, primary_key=True)
    tax_code     = db.Column(db.String(60), nullable=False, index=True)   # VAT 15%
    account_code = db.Column(db.String(20), unique=True, nullable=False, index=True)  # S1
    section      = db.Column(db.String(255), nullable=False)
    status       = db.Column(db.String(10), default='Active')
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # ZATCA UBL tax-category classification, admin-set once per code --
    # see database/zatca/engine.py::ubl_tax_category().
    zatca_category         = db.Column(db.String(2))   # S | Z | E | O
    zatca_exemption_reason = db.Column(db.String(20))  # e.g. VATEX-SA-29

    def to_dict(self):
        return {
            'id': self.id, 'account_code': self.account_code,
            'tax_code': self.tax_code, 'section': self.section,
            'status': self.status or 'Active',
            'zatca_category': self.zatca_category or '',
            'zatca_exemption_reason': self.zatca_exemption_reason or '',
        }


DEFAULT_PURCHASE_TAX_CODES = [
    ('VAT 15%',      'P1',   'Standard rated domestic purchases'),
    ('VAT 15%',      'PC',   'Imports subject to VAT paid at customs'),
    ('Import',       'PRCM', 'Imports subject to VAT accounted for through Reverse Charge Mechanism (RCM)'),
    ('VAT 0%',       'P0',   'Zero rated purchases'),
    ('Exempt',       'PE',   'Exempt purchases'),
    ('Out of Scope', 'POC',  'Out of Scope expenses'),
]

DEFAULT_SALES_TAX_CODES = [
    ('VAT 15%',         'S1',  'Standard rated supplies'),
    ('VAT 0%',          'S0',  'Zero rated domestic supplies'),
    ('VAT 0%',          'SE',  'Exports'),
    ('Exempt Supplies', 'SES', 'Exempt supplies'),
    ('Out of Scope',    'SOC', 'Out of Scope expenses'),
]


def seed_tax_codes():
    """Idempotently seed default purchase & sales tax codes."""
    added = 0
    for acc, code, section in DEFAULT_PURCHASE_TAX_CODES:
        if not PurchaseTaxCode.query.filter_by(account_code=code).first():
            db.session.add(PurchaseTaxCode(account_code=code, tax_code=acc,
                                           section=section, status='Active'))
            added += 1
    for acc, code, section in DEFAULT_SALES_TAX_CODES:
        if not SalesTaxCode.query.filter_by(account_code=code).first():
            db.session.add(SalesTaxCode(account_code=code, tax_code=acc,
                                        section=section, status='Active'))
            added += 1
    if added:
        db.session.commit()
    return added


# Default Auto Code Selection mappings for the 8 standard Purchase/Sale
# forms that consume one -- see database/routes/shared.py's
# _grl_lines_from() and database/routes/auto_code_selection.py's
# PURCHASE_AUTO_CODE_FORMS/SALE_AUTO_CODE_FORMS. Without a mapping, that
# form's auto-journal posting writes a blank offsetting account, so a
# brand-new tenant needs *something* here to post at all.
#
# `nature` is NOT a free styling choice: _grl_lines_from()'s `r1_credit`
# already fixes each form's own first GRL record (the item's account) to
# a specific Debit or Credit side, so this offsetting record must always
# be the opposite of that or the two-line entry will never balance --
# Purchase Invoice/Delivery Note/Sales Return Note/Sales Credit Memo post
# record 1 as Credit (so this must be Debit); every other form here posts
# record 1 as Debit (so this must be Credit).
#
# Level Five accounts are matched by drawer NAME below (not a
# hand-typed code), since seed_coa_levels_3_4_5() generates codes at seed
# time from insertion order -- looking up by the exact name it seeds is
# far less fragile than guessing the resulting code string. These are
# generic holding/clearing accounts already present in that seed data
# (see coa_seed_data.py) chosen as a reasonable starting default, not a
# purpose-built account per form (e.g. no dedicated COGS or Unbilled
# Sales account exists in that data) -- review/adjust per form via the
# Auto Code Selection screen once real business needs are known.
AUTO_CODE_SELECTION_DEFAULTS = [
    # (module_code, form_code, level_five_drawer_name, nature)
    ('purchase', 'goods_receipt_note',   'Other Credit - Other Payables', 'Credit'),
    ('purchase', 'purchase_invoice',     'Other Credit - Other Payables', 'Debit'),
    ('purchase', 'purchase_return_note', 'Other Credit - Other Payables', 'Credit'),
    ('purchase', 'purchase_debit_memo',  'Other Credit - Other Payables', 'Credit'),
    ('sale', 'delivery_note',            'Other Debit - Receipts Clearing Account', 'Debit'),
    ('sale', 'sales_invoice',            'Other Income', 'Credit'),
    ('sale', 'sales_return_note',        'Other Debit - Receipts Clearing Account', 'Debit'),
    ('sale', 'sales_credit_memo',        'Other Debit - Receipts Clearing Account', 'Debit'),
]


def seed_auto_code_selection():
    """Idempotently insert the default module/form -> Level Five mappings
    above. Deliberately NOT called from app.py's init_db() -- only from
    tenant_provisioning.py's provision_tenant(), since a brand-new SaaS
    tenant needs a working default here while the main app's own
    installation is configured by hand through the Auto Code Selection
    screen. Silently skips any row whose module/form/account can't be
    found (e.g. Chart of Accounts wasn't seeded first)."""
    added = 0
    for module_code, form_code, l5_drawer, nature in AUTO_CODE_SELECTION_DEFAULTS:
        module = Module.query.filter_by(code=module_code).first()
        if not module:
            continue
        form = SystemForm.query.filter_by(module_id=module.id, code=form_code).first()
        if not form:
            continue
        if AutoCodeSelection.query.filter_by(module_id=module.id, form_id=form.id).first():
            continue
        l5 = LevelFive.query.filter_by(drawers=l5_drawer).first()
        if not l5:
            continue
        l4 = LevelFour.query.get(l5.level_four_id)
        db.session.add(AutoCodeSelection(
            module_id=module.id, form_id=form.id,
            levelfour_code=l4.code if l4 else None,
            levelfour_drawer_en=l4.drawers if l4 else None,
            levelfour_drawer_ar=l4.drawers_ar if l4 else None,
            levelfive_code=l5.code,
            levelfive_drawer_en=l5.drawers,
            levelfive_drawer_ar=l5.drawers_ar,
            nature=nature,
            status='Approved',
        ))
        added += 1
    if added:
        db.session.commit()
    return added


# ═════════════════════════════════════════════════════════════════
#  JOURNAL ENTRIES  (accounting module)
#  Single-row-per-entry as specified: each row carries one debit/credit
#  line plus the auto-filled Chart-of-Accounts drawer hierarchy taken
#  from the selected Level 5 account.
# ═════════════════════════════════════════════════════════════════
class JournalEntry(db.Model):
    """Journal Entry — master. Master-detail structure (like GRL)."""
    __tablename__ = 'journal_entries'
    id            = db.Column(db.Integer, primary_key=True)
    je_no         = db.Column(db.String(40))    # auto number, e.g. JEV-2026-1
    origion       = db.Column(db.String(40))    # source doc no (e.g. GRN-2026-1)
    origin_type   = db.Column(db.String(20))    # source kind (e.g. 'GRN', 'GRL')
    origin_id     = db.Column(db.Integer)       # source record id (FK reference)
    refrence      = db.Column(db.String(200))
    posting_date  = db.Column(db.Date)
    due_date      = db.Column(db.Date)
    document_date = db.Column(db.Date)
    narration     = db.Column(db.String(500))
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    details = db.relationship('JournalEntryDetail', backref='journal_entry',
                              lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'je_no': self.je_no or '',
            'origion': self.origion or '',
            'origin_type': self.origin_type or '',
            'origin_id': self.origin_id,
            'refrence': self.refrence or '',
            'posting_date': self.posting_date.isoformat() if self.posting_date else '',
            'due_date': self.due_date.isoformat() if self.due_date else '',
            'document_date': self.document_date.isoformat() if self.document_date else '',
            'narration': self.narration or '',
            'details': [d.to_dict() for d in self.details],
        }


class JournalEntryDetail(db.Model):
    __tablename__ = 'journal_entry_detail'
    id                = db.Column(db.Integer, primary_key=True)
    journal_entry_id  = db.Column(db.Integer,
                                  db.ForeignKey('journal_entries.id', ondelete='CASCADE'))
    code              = db.Column(db.String(40), db.ForeignKey('level_five.code'))
    # Mirrors GRLDetail.reference_code exactly -- a vendor/customer reference
    # number, never a Chart-of-Accounts code, kept separate from `code` so
    # the two can never be confused with each other.
    reference_code    = db.Column(db.String(100))
    account_name      = db.Column(db.String(250))
    control_account   = db.Column(db.String(40))
    debit             = db.Column(db.Numeric(14, 2), default=0)
    credit            = db.Column(db.Numeric(14, 2), default=0)
    narration         = db.Column(db.String(500))

    def to_dict(self):
        return {
            'id': self.id,
            'journal_entry_id': self.journal_entry_id,
            'code': self.code or '',
            'reference_code': self.reference_code or '',
            'account_name': self.account_name or '',
            'control_account': self.control_account or '',
            'debit': float(self.debit) if self.debit is not None else 0,
            'credit': float(self.credit) if self.credit is not None else 0,
            'narration': self.narration or '',
        }


class NoActiveFinancialYearError(Exception):
    """Raised when there is no Open financial year to number a journal entry."""
    pass


def next_je_no():
    """Next Journal Entry number: JEV-<active FY year>-<n>  e.g. JEV-2026-1.

    The year comes from the active (Open) financial year. Raises
    NoActiveFinancialYearError if none is open.
    """
    year = active_fy_year()
    if not year:
        raise NoActiveFinancialYearError()
    prefix = 'JEV'
    like = f'{prefix}-{year}-%'
    max_num = 0
    for je in JournalEntry.query.filter(JournalEntry.je_no.like(like)).all():
        if je.je_no:
            try:
                num = int(je.je_no.rsplit('-', 1)[1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue
    return f'{prefix}-{year}-{max_num + 1}'


# ═════════════════════════════════════════════════════════════════
#  OPENING BALANCE
#  The one-time (or occasional-correction) mechanism for entering each
#  Chart-of-Accounts head's starting Debit/Credit balance when this
#  software first goes live, or when a new account is discovered mid-
#  migration. A plain Save is a Draft (no GL impact, per this app's
#  standing post-only rule); Post & Save posts one JournalEntryDetail
#  (+ matching GRLDetail, for parity with every other document type)
#  per non-zero line, dated `balance_date` -- so the Ledger's own
#  "everything before From Date is Opening Balance" logic picks these
#  rows up automatically with no special-casing anywhere else.
# ═════════════════════════════════════════════════════════════════
class OpeningBalance(db.Model):
    __tablename__ = 'opening_balances'
    id               = db.Column(db.Integer, primary_key=True)
    doc_no           = db.Column(db.String(40), unique=True)   # OB-<FY>-<n>
    balance_date     = db.Column(db.Date)
    status           = db.Column(db.String(10), default='Draft')  # Draft | Posted
    narration        = db.Column(db.String(500))
    journal_entry_id = db.Column(db.Integer, db.ForeignKey('journal_entries.id'))
    grl_id           = db.Column(db.Integer, db.ForeignKey('journal_ledger.id'))
    created_by       = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_by       = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lines = db.relationship('OpeningBalanceDetail', backref='opening_balance',
                            lazy=True, cascade='all, delete-orphan')
    creator = db.relationship('User', foreign_keys=[created_by], lazy=True)

    def to_dict(self):
        total_debit = sum(float(l.debit or 0) for l in self.lines)
        total_credit = sum(float(l.credit or 0) for l in self.lines)
        return {
            'id': self.id,
            'doc_no': self.doc_no or '',
            'balance_date': self.balance_date.isoformat() if self.balance_date else '',
            'status': self.status or 'Draft',
            'narration': self.narration or '',
            'total_debit': round(total_debit, 2),
            'total_credit': round(total_credit, 2),
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'lines': [l.to_dict() for l in self.lines],
        }


class OpeningBalanceDetail(db.Model):
    __tablename__ = 'opening_balance_details'
    id                 = db.Column(db.Integer, primary_key=True)
    opening_balance_id = db.Column(db.Integer,
                                   db.ForeignKey('opening_balances.id', ondelete='CASCADE'))
    code               = db.Column(db.String(40), db.ForeignKey('level_five.code'))
    account_name       = db.Column(db.String(250))
    debit              = db.Column(db.Numeric(14, 2), default=0)
    credit             = db.Column(db.Numeric(14, 2), default=0)

    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code or '',
            'account_name': self.account_name or '',
            'debit': float(self.debit) if self.debit is not None else 0,
            'credit': float(self.credit) if self.credit is not None else 0,
        }


def next_ob_no():
    """Next Opening Balance number: OB-<active FY year>-<n>, mirroring
    next_je_no(). Raises NoActiveFinancialYearError if none is open."""
    year = active_fy_year()
    if not year:
        raise NoActiveFinancialYearError()
    prefix = 'OB'
    like = f'{prefix}-{year}-%'
    max_num = 0
    for ob in OpeningBalance.query.filter(OpeningBalance.doc_no.like(like)).all():
        if ob.doc_no:
            try:
                num = int(ob.doc_no.rsplit('-', 1)[1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue
    return f'{prefix}-{year}-{max_num + 1}'


# ═════════════════════════════════════════════════════════════════
#  SALARY CONSOLIDATION  (payroll)
#  One row per employee per payroll run. Most fields are copied from
#  the Employee record at generation time; day/OT/salary figures are
#  computed. Editable fields (absent, holidays, bonus, advance) let the
#  user adjust before finalising.  See SALARY_STATUS.md for the exact
#  default formulas used.
# ═════════════════════════════════════════════════════════════════
class SalaryConsolidation(db.Model):
    """Payroll module (3-stage workflow). Field names match the payroll spec exactly.

    Data sources:
        filter (which employees qualify) -> EmployeeWorkAllocation
            (kafeel/buyer/department/location/status -- filtering only)
        all displayed/stored employee info -> Employee (live, refreshed on demand
            via the Refresh button, never auto-overwritten silently)
        bank_code/iban -> EmployeeBank (primary)
        buyer_name display -> BuyerMaster.buyer_name_en
    """
    __tablename__ = 'salary_consolidation'

    id                    = db.Column(db.Integer, primary_key=True)
    payroll_id            = db.Column(db.String(20), index=True)          # PR-1, PR-2, ...
    payroll_status        = db.Column(db.String(10), default='Initial')   # Initial / Ready / Post
    salary_order          = db.Column(db.String(30))
    month_from            = db.Column(db.Date)
    month_to              = db.Column(db.Date)
    # Per-employee payable period, distinct from month_from/month_to (which
    # always stay the payroll BATCH's master period, unchanged, on every
    # row -- other code such as the payroll list summary's min/max period
    # and the "add employee to an existing payroll" flow depend on that).
    # emp_from_date defaults to MAX(month_from, employee's latest
    # EmployeeWorkAllocation joining date) so a mid-period joiner is only
    # paid from their actual joining date, editable by the user afterwards.
    # emp_to_date normally just mirrors month_to.
    emp_from_date         = db.Column(db.Date)
    emp_to_date           = db.Column(db.Date)
    month                 = db.Column(db.String(20))                     # e.g. July-2026
    kafeel                = db.Column(db.String(200))
    buyer_id              = db.Column(db.Integer)                        # internal filter/link, not in spec grid
    buyer_name            = db.Column(db.String(200))
    buyer_department      = db.Column(db.String(150))
    location              = db.Column(db.String(150))
    sheet_no              = db.Column(db.String(30))
    employee_id           = db.Column(db.Integer, db.ForeignKey('employees.id'))
    employee_code         = db.Column(db.String(20))
    employee_name         = db.Column(db.String(200))
    profession            = db.Column(db.String(150))
    nationality           = db.Column(db.String(100))
    iqama                 = db.Column(db.String(50))
    salary_category       = db.Column(db.String(30))
    salary_type           = db.Column(db.String(20))                     # Per Month / Per Day / Per Hour
    day_hour              = db.Column(db.Numeric(10, 2), default=0)      # working hours per day
    basic_salary          = db.Column(db.Numeric(12, 2), default=0)
    allowance             = db.Column(db.Numeric(12, 2), default=0)
    # Breakdown of the SAME total already carried by `allowance` above
    # (Employee.total_allowances) -- these three never change that total or
    # any salary/OT/invoice formula in _recalc(); an employee with no
    # EmployeeAllowance row of one of these three types gets 0, never blank
    # (see _employee_allowance_breakdown() in database/routes/payroll.py).
    food                  = db.Column(db.Numeric(12, 2), default=0)
    house_rent            = db.Column(db.Numeric(12, 2), default=0)
    transportation        = db.Column(db.Numeric(12, 2), default=0)
    days                  = db.Column(db.Integer, default=0)
    fridays               = db.Column(db.Integer, default=0)
    holidays              = db.Column(db.Integer, default=0)
    absent                = db.Column(db.Integer, default=0)
    monthly_salary        = db.Column(db.Numeric(12, 2), default=0)
    total_hours           = db.Column(db.Numeric(10, 2), default=0)
    working_hour          = db.Column(db.Numeric(10, 2), default=0)
    ot_hour               = db.Column(db.Numeric(10, 2), default=0)
    extra_ot              = db.Column(db.Numeric(10, 2), default=0)
    ot_rate               = db.Column(db.Numeric(12, 2), default=0)
    ot_amount             = db.Column(db.Numeric(12, 2), default=0)
    bonus                 = db.Column(db.Numeric(12, 2), default=0)
    deduction             = db.Column(db.Numeric(12, 2), default=0)
    total_salary          = db.Column(db.Numeric(12, 2), default=0)
    advance               = db.Column(db.Numeric(12, 2), default=0)
    credit                = db.Column(db.Numeric(12, 2), default=0)
    salary_payable        = db.Column(db.Numeric(12, 2), default=0)
    paid                  = db.Column(db.Numeric(12, 2), default=0)
    balance               = db.Column(db.Numeric(12, 2), default=0)
    employ_payroll_status = db.Column(db.String(10), default='Single')   # Single / Double
    payment_status        = db.Column(db.String(10), default='Ready')   # Ready / Hold
    iqama_expiry          = db.Column(db.Date)
    status                = db.Column(db.String(20), default='Active')   # employee status
    bank_code             = db.Column(db.String(60))
    iban_no               = db.Column(db.String(60))
    po_rate               = db.Column(db.Numeric(12, 2), default=0)
    po_ot_rate            = db.Column(db.Numeric(12, 2), default=0)
    services_charges      = db.Column(db.Numeric(12, 2), default=0)
    invoice_amount        = db.Column(db.Numeric(12, 2), default=0)

    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    created_by            = db.Column(db.Integer)

    def to_dict(self):
        def _f(v): return float(v) if v is not None else 0.0
        return {
            'id': self.id,
            'payroll_id': self.payroll_id or '',
            'payroll_status': self.payroll_status or 'Initial',
            'salary_order': self.salary_order or '',
            'month_from': self.month_from.strftime('%d-%b-%y') if self.month_from else '',
            'month_to': self.month_to.strftime('%d-%b-%y') if self.month_to else '',
            'emp_from_date': self.emp_from_date.strftime('%Y-%m-%d') if self.emp_from_date else '',
            'emp_to_date': self.emp_to_date.strftime('%Y-%m-%d') if self.emp_to_date else '',
            'month': self.month or '',
            'kafeel': self.kafeel or '',
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer_name or '',
            'buyer_department': self.buyer_department or '',
            'location': self.location or '',
            'sheet_no': self.sheet_no or '',
            'employee_id': self.employee_id,
            'employee_code': self.employee_code or '',
            'employee_name': self.employee_name or '',
            'profession': self.profession or '',
            'nationality': self.nationality or '',
            'iqama': self.iqama or '',
            'salary_category': self.salary_category or '',
            'salary_type': self.salary_type or '',
            'day_hour': _f(self.day_hour),
            'basic_salary': _f(self.basic_salary),
            'allowance': _f(self.allowance),
            'food': _f(self.food),
            'house_rent': _f(self.house_rent),
            'transportation': _f(self.transportation),
            'days': self.days or 0,
            'fridays': self.fridays or 0,
            'holidays': self.holidays or 0,
            'absent': self.absent or 0,
            'monthly_salary': _f(self.monthly_salary),
            'total_hours': _f(self.total_hours),
            'working_hour': _f(self.working_hour),
            'ot_hour': _f(self.ot_hour),
            'extra_ot': _f(self.extra_ot),
            'ot_rate': _f(self.ot_rate),
            'ot_amount': _f(self.ot_amount),
            'bonus': _f(self.bonus),
            'deduction': _f(self.deduction),
            'total_salary': _f(self.total_salary),
            'advance': _f(self.advance),
            'credit': _f(self.credit),
            'salary_payable': _f(self.salary_payable),
            'paid': _f(self.paid),
            'balance': _f(self.balance),
            'employ_payroll_status': self.employ_payroll_status or 'Single',
            'payment_status': self.payment_status or 'Ready',
            'iqama_expiry': self.iqama_expiry.strftime('%d-%b-%y') if self.iqama_expiry else '',
            'status': self.status or '',
            'bank_code': self.bank_code or '',
            'iban_no': self.iban_no or '',
            'po_rate': _f(self.po_rate),
            'po_ot_rate': _f(self.po_ot_rate),
            'services_charges': _f(self.services_charges),
            'invoice_amount': _f(self.invoice_amount),
        }


def next_payroll_id():
    """PR-1, PR-2, PR-3, ... (per spec example)."""
    last = (SalaryConsolidation.query
            .filter(SalaryConsolidation.payroll_id.isnot(None))
            .order_by(SalaryConsolidation.id.desc()).first())
    n = 1
    if last and last.payroll_id and last.payroll_id.startswith('PR-'):
        try:
            n = int(last.payroll_id.split('-')[1]) + 1
        except (IndexError, ValueError):
            n = (SalaryConsolidation.query
                 .filter(SalaryConsolidation.payroll_id.isnot(None))
                 .with_entities(db.func.count(db.func.distinct(SalaryConsolidation.payroll_id)))
                 .scalar() or 0) + 1
    return f'PR-{n}'


def next_salary_order():
    """Next payroll batch reference in SAL-000001 format."""
    last = SalaryConsolidation.query.order_by(SalaryConsolidation.id.desc()).first()
    n = 1
    if last and last.salary_order and last.salary_order.startswith('SAL-'):
        try:
            n = int(last.salary_order.split('-')[1]) + 1
        except (ValueError, IndexError):
            n = (last.id or 0) + 1
    return f'SAL-{n:06d}'

# ═════════════════════════════════════════════════════════════════
#  EMPLOYEE WORK ALLOCATION  (replaces the old work_allocations table)
#  One row per employee assignment. Multiple employees are added on the
#  work-allocation form (each becomes a row). Fields mirror the employee
#  plus the assignment specifics (buyer, department, location, shift).
# ═════════════════════════════════════════════════════════════════
class EmployeeWorkAllocation(db.Model):
    __tablename__ = 'employee_work_allocation'

    id                = db.Column(db.Integer, primary_key=True)
    employee_id       = db.Column(db.Integer, db.ForeignKey('employees.id'))
    kafeel            = db.Column(db.String(200))
    name              = db.Column(db.String(200))
    nationality       = db.Column(db.String(100))
    profession        = db.Column(db.String(150))
    iqama             = db.Column(db.String(50))
    joining_date      = db.Column(db.Date)
    end_date          = db.Column(db.Date)
    buyer_id            = db.Column(db.Integer, db.ForeignKey('buyers.id'))
    buyer_name          = db.Column(db.String(200))
    buyer_name_ar       = db.Column(db.Unicode(200))
    buyer_department_id = db.Column(db.Integer, db.ForeignKey('buyer_departments.id'))
    buyer_department    = db.Column(db.String(150))
    buyer_department_ar = db.Column(db.Unicode(150))
    # Cascades from the selected Buyer (BuyerMaster.salary_order) at the
    # moment this allocation is saved -- the same "1 or 2" batching value
    # payroll itself uses (see database/routes/payroll.py _buyer_salary_order),
    # just captured here per-allocation instead of re-derived every time.
    salary_order        = db.Column(db.Integer, default=1)
    location            = db.Column(db.String(150))
    location_ar         = db.Column(db.Unicode(150))
    shift             = db.Column(db.String(10))        # 'day' or 'night'
    status            = db.Column(db.String(20), default='active')
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    created_by        = db.Column(db.Integer, db.ForeignKey('users.id'))

    employee = db.relationship('Employee', foreign_keys=[employee_id], lazy=True)
    buyer    = db.relationship('BuyerMaster', foreign_keys=[buyer_id], lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'employee_id': self.employee_id,
            'kafeel': self.kafeel or '',
            'name': self.name or '',
            'nationality': self.nationality or '',
            'profession': self.profession or '',
            'iqama': self.iqama or '',
            'joining_date': self.joining_date.strftime('%Y-%m-%d') if self.joining_date else '',
            'end_date': self.end_date.strftime('%Y-%m-%d') if self.end_date else '',
            'buyer_id': self.buyer_id,
            'buyer_name': self.buyer_name or '',
            'buyer_name_ar': self.buyer_name_ar or '',
            'buyer_department_id': self.buyer_department_id,
            'buyer_department': self.buyer_department or '',
            'buyer_department_ar': self.buyer_department_ar or '',
            'salary_order': self.salary_order or 1,
            'location': self.location or '',
            'location_ar': self.location_ar or '',
            'shift': self.shift or '',
            'status': self.status or 'active',
            # ── aliases so the existing Work Allocation grid keeps working ──
            'company': self.buyer_name or '',
            'company_ar': self.buyer_name_ar or '',
            'department': self.buyer_department or '',
            'department_ar': self.buyer_department_ar or '',
            'section': self.location or '',
            'section_ar': self.location_ar or '',
            'shift_type': self.shift or '',
            # employee details the grid shows
            'employee_code': (self.employee.employee_code if self.employee else ''),
            'name_ar': (self.employee.name_ar if self.employee else ''),
            'passport_number': (self.employee.passport_number if self.employee else ''),
            'kafeel_name': self.kafeel or '',
            'iqama_number': self.iqama or '',
        }

class ItemUnit(db.Model):
    __tablename__ = 'item_unit'
    id           = db.Column(db.Integer, primary_key=True)
    code         = db.Column(db.String(20), unique=True, index=True)   # auto: UOM-0001
    name_en      = db.Column(db.String(150), nullable=False)
    name_ar      = db.Column(db.Unicode(150))
    pac_size_en  = db.Column(db.String(150))
    pac_size_ar  = db.Column(db.Unicode(150))
    multiply     = db.Column(db.Numeric(12, 4), default=0)
    status       = db.Column(db.String(10), default='Active')          # Active / Inactive
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code or '',
            'name_en': self.name_en or '',
            'name_ar': self.name_ar or '',
            'pac_size_en': self.pac_size_en or '',
            'pac_size_ar': self.pac_size_ar or '',
            'multiply': float(self.multiply) if self.multiply is not None else 0,
            'status': self.status or 'Active',
        }


def next_uom_code():
    """Next auto code in UOM-0001 format."""
    last = (ItemUnit.query
            .filter(ItemUnit.code.like('UOM-%'))
            .order_by(ItemUnit.id.desc())
            .first())
    n = 1
    if last and last.code and last.code.startswith('UOM-'):
        try:
            n = int(last.code.split('-')[1]) + 1
        except (ValueError, IndexError):
            n = (last.id or 0) + 1
    return f'UOM-{n:04d}'


def active_financial_year():
    """Return the active (Open) FinancialYear, or None if none is open.

    'Active' = status is not 'Closed'. If several are open, the most recent
    year wins.
    """
    return (FinancialYear.query
            .filter(FinancialYear.status != 'Closed')
            .order_by(FinancialYear.year.desc())
            .first())


def active_fy_year():
    """The numeric year of the active financial year, or None."""
    fy = active_financial_year()
    return fy.year if fy else None


class GRL(db.Model):
    """General ledger row attached to a postable document. Originally
    Purchase-only ("Goods Receipt Ledger"), now a shared per-document-type
    ledger table used by both Purchase and Sales documents -- one FK
    column per document type, exactly one of which is set on any given
    row."""
    __tablename__ = 'journal_ledger'
    id                    = db.Column(db.Integer, primary_key=True)
    goods_receipt_note_id = db.Column(db.Integer,
                                      db.ForeignKey('purchase_goods_receipt_notes.goods_receipt_note_id',
                                                    ondelete='CASCADE'))
    purchase_invoice_id   = db.Column(db.Integer,
                                      db.ForeignKey('purchase_invoices.purchase_invoice_id',
                                                    ondelete='CASCADE'))
    purchase_return_note_id = db.Column(db.Integer,
                                      db.ForeignKey('purchase_return_notes.purchase_good_return_note_id',
                                                    ondelete='CASCADE'))
    purchase_debit_memo_id  = db.Column(db.Integer,
                                      db.ForeignKey('purchase_debit_memos.purchase_debit_memo_id',
                                                    ondelete='CASCADE'))
    sales_return_note_id  = db.Column(db.Integer,
                                      db.ForeignKey('sales_return_notes.sales_return_note_id',
                                                    ondelete='CASCADE'))
    delivery_note_id      = db.Column(db.Integer,
                                      db.ForeignKey('sale_delivery_notes.delivery_note_id',
                                                    ondelete='CASCADE'))
    sales_invoice_id      = db.Column(db.Integer,
                                      db.ForeignKey('sales_invoices.sales_invoice_id',
                                                    ondelete='CASCADE'))
    sales_credit_memo_id  = db.Column(db.Integer,
                                      db.ForeignKey('sales_credit_memos.sales_credit_memo_id',
                                                    ondelete='CASCADE'))
    # Outgoing Payment can post TWO GRL rows against the same payment (the
    # original Posted entry, then a reversal on Cancel) -- unlike every other
    # document type here, this FK is not unique per outgoing_payment_id.
    outgoing_payment_id   = db.Column(db.Integer,
                                      db.ForeignKey('outgoing_payment.id', ondelete='CASCADE'))
    incoming_payment_id   = db.Column(db.Integer,
                                      db.ForeignKey('incoming_payment.id', ondelete='CASCADE'))
    opening_balance_id    = db.Column(db.Integer,
                                      db.ForeignKey('opening_balances.id', ondelete='CASCADE'))
    # Payroll is a BATCH of many salary_consolidation rows sharing one
    # string payroll_id (e.g. "PR-3") -- there is no single row representing
    # "the batch" to hang an Integer FK off of like every other document
    # type above, so this is a plain string reference instead of a
    # db.ForeignKey. Not unique per payroll_id for the same reason
    # outgoing_payment_id isn't: the same GRL row is found-or-created by
    # this value each time the batch is (re-)posted.
    payroll_id            = db.Column(db.String(20))
    # Direct entity reference (Supplier/Buyer/Employee), independent of which
    # document-type FK above is set -- lets a GRL row be queried by "which
    # supplier/buyer/employee is this for" without joining through every
    # possible source document. No ondelete action (matches how every other
    # document's own supplier_id/buyer_id/employee_id FK behaves in this
    # codebase): a GRL row is posted accounting history and must never be
    # silently cascaded away if the entity is later deleted.
    supplier_id           = db.Column(db.Integer, db.ForeignKey('suppliers.id'))
    buyer_id              = db.Column(db.Integer, db.ForeignKey('buyers.id'))
    employee_id           = db.Column(db.Integer, db.ForeignKey('employees.id'))
    origion               = db.Column(db.String(40))    # origin doc no (e.g. GRN-2026-1)
    grl_no                = db.Column(db.String(40))     # auto: GRL-<FY>-<n>
    journal_entry_id      = db.Column(db.Integer, db.ForeignKey('journal_entries.id'))
    posting_date          = db.Column(db.Date)
    due_date              = db.Column(db.Date)
    document_date         = db.Column(db.Date)
    narration             = db.Column(db.String(500))
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at            = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    details = db.relationship('GRLDetail', backref='grl', lazy=True,
                              cascade='all, delete-orphan')
    journal_entry = db.relationship('JournalEntry', foreign_keys=[journal_entry_id])

    def to_dict(self):
        return {
            'id': self.id,
            'goods_receipt_note_id': self.goods_receipt_note_id,
            'purchase_invoice_id': self.purchase_invoice_id,
            'outgoing_payment_id': self.outgoing_payment_id,
            'incoming_payment_id': self.incoming_payment_id,
            'opening_balance_id': self.opening_balance_id,
            'payroll_id': self.payroll_id,
            'supplier_id': self.supplier_id,
            'buyer_id': self.buyer_id,
            'employee_id': self.employee_id,
            'origion': self.origion or '',
            'grl_no': self.grl_no or '',
            'je_no': self.journal_entry.je_no if self.journal_entry else '',
            'posting_date': self.posting_date.isoformat() if self.posting_date else '',
            'due_date': self.due_date.isoformat() if self.due_date else '',
            'document_date': self.document_date.isoformat() if self.document_date else '',
            'narration': self.narration or '',
            'details': [d.to_dict() for d in self.details],
        }


class GRLDetail(db.Model):
    __tablename__ = 'journal_ledger_detail'
    id              = db.Column(db.Integer, primary_key=True)
    grl_id          = db.Column(db.Integer,
                                db.ForeignKey('journal_ledger.id', ondelete='CASCADE'))
    code            = db.Column(db.String(40))
    # A vendor/customer reference number (e.g. the supplier's own invoice
    # number) is NOT a Chart-of-Accounts code and must never be written into
    # `code` -- this is its own field precisely so the two can't be confused.
    reference_code  = db.Column(db.String(100))
    account_name    = db.Column(db.String(250))
    account_name_ar = db.Column(db.Unicode(250))
    control_account = db.Column(db.String(40))
    debit           = db.Column(db.Numeric(14, 2), default=0)
    credit          = db.Column(db.Numeric(14, 2), default=0)
    narration       = db.Column(db.String(500))

    def to_dict(self):
        return {
            'id': self.id,
            'grl_id': self.grl_id,
            'code': self.code or '',
            'reference_code': self.reference_code or '',
            'account_name': self.account_name or '',
            'account_name_ar': self.account_name_ar or '',
            'control_account': self.control_account or '',
            'debit': float(self.debit) if self.debit is not None else 0,
            'credit': float(self.credit) if self.credit is not None else 0,
            'narration': self.narration or '',
        }


# ══════════════════════════════════════════════════════════════════
#  SAAS PLATFORM -- Phase 1: module/pricing catalog
#  Lives in the separate `saas_master` database (see SQLALCHEMY_BINDS
#  in config.py), not the tenant ERP database these other models live
#  in. This is a coarser, billing-oriented catalog ("Accounting",
#  "Sales", "Payroll", ...) -- distinct from the existing RBAC
#  Module/SystemForm/Permission tables above, which gate individual
#  pages for an already-logged-in user and have nothing to do with
#  what a customer has paid for. Later phases will map a subscribed
#  SaasModule to the RBAC-covered routes/blueprints it unlocks; Phase 1
#  only lets the Super Admin define and price the catalog itself.
# ══════════════════════════════════════════════════════════════════
class SaasModule(db.Model):
    __bind_key__ = 'saas'
    __tablename__ = 'saas_modules'
    id               = db.Column(db.Integer, primary_key=True)
    module_code      = db.Column(db.String(30), unique=True, nullable=False)
    module_name_en   = db.Column(db.String(150), nullable=False)
    module_name_ar   = db.Column(db.Unicode(150))
    description_en   = db.Column(db.Text)
    description_ar   = db.Column(db.Unicode(500))
    monthly_price    = db.Column(db.Numeric(10, 2), default=0)
    yearly_price     = db.Column(db.Numeric(10, 2), default=0)
    trial_available  = db.Column(db.Boolean, default=True)
    status           = db.Column(db.String(20), default='active')  # active | inactive
    display_order    = db.Column(db.Integer, default=0)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow)

    # Modules this one depends on (e.g. Payroll -> requires -> HR).
    requires = db.relationship(
        'SaasModuleDependency',
        foreign_keys='SaasModuleDependency.module_id',
        backref='module', cascade='all, delete-orphan', lazy=True,
    )

    # RBAC Module.code values this SaaS product unlocks -- see
    # SaasModuleRbacLink's own docstring below for why this bridge exists.
    rbac_links = db.relationship(
        'SaasModuleRbacLink', cascade='all, delete-orphan', lazy=True,
    )

    def to_dict(self):
        return {
            'id': self.id,
            'module_code': self.module_code,
            'module_name_en': self.module_name_en,
            'module_name_ar': self.module_name_ar or '',
            'description_en': self.description_en or '',
            'description_ar': self.description_ar or '',
            'monthly_price': float(self.monthly_price or 0),
            'yearly_price': float(self.yearly_price or 0),
            'trial_available': bool(self.trial_available),
            'status': self.status or 'active',
            'display_order': self.display_order or 0,
            'requires': [d.requires_module_id for d in self.requires],
            'rbac_modules': [link.rbac_module_code for link in self.rbac_links],
        }


class SaasModuleDependency(db.Model):
    __bind_key__ = 'saas'
    __tablename__ = 'proledge_saas_modules_dependencies'
    id                 = db.Column(db.Integer, primary_key=True)
    module_id          = db.Column(db.Integer, db.ForeignKey('saas_modules.id', ondelete='CASCADE'), nullable=False)
    requires_module_id = db.Column(db.Integer, db.ForeignKey('saas_modules.id', ondelete='CASCADE'), nullable=False)

    requires_module = db.relationship('SaasModule', foreign_keys=[requires_module_id])

    __table_args__ = (
        db.UniqueConstraint('module_id', 'requires_module_id', name='uq_saas_module_dependency'),
    )


class SaasModuleRbacLink(db.Model):
    """Which RBAC page-permission module(s) (models.py's Module.code, the
    tenant-local catalog rbac.py's MODULE_FORM_CATALOG seeds) a billable
    SaasModule unlocks. These are two genuinely different catalogs -- a
    SaasModule is "the product a customer pays for" (e.g. "Accounting"),
    an RBAC Module is "a page-permission group" (e.g. 'coa', 'financial',
    'journal') -- and their codes do not line up 1:1 (only 'purchase'
    happens to match by coincidence), so this table is the explicit,
    Super-Admin-editable bridge between them. See
    rbac.py's customer_active_rbac_modules() for how this is enforced.

    An RBAC module code with NO row here at all (in either direction --
    not linked from any SaasModule) is treated as ungated: always
    accessible regardless of subscription. This is deliberate for
    platform-level modules like 'dashboard' and 'administration' that
    every tenant needs regardless of which paid products they bought."""
    __bind_key__ = 'saas'
    __tablename__ = 'proledge_saas_module_rbac_link'
    id               = db.Column(db.Integer, primary_key=True)
    saas_module_id   = db.Column(db.Integer, db.ForeignKey('saas_modules.id', ondelete='CASCADE'), nullable=False)
    rbac_module_code = db.Column(db.String(50), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('saas_module_id', 'rbac_module_code', name='uq_saas_rbac_link'),
    )


# ══════════════════════════════════════════════════════════════════
#  SAAS PLATFORM -- Phase 2: customers, subscriptions, payments
#  Also `saas_master`/bind_key='saas'. SellerMs is the sole owner of
#  this schema (creates/migrates it via ensure_saas_schema() below);
#  Proledg only ever reads/writes rows through its own mirrored model
#  classes, never runs create_all()/migrations against these tables.
# ══════════════════════════════════════════════════════════════════
class Customer(db.Model):
    __bind_key__ = 'saas'
    __tablename__ = 'proledge_saas_customers'
    id            = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(150), nullable=False)
    company_name  = db.Column(db.String(150))
    email         = db.Column(db.String(150), unique=True, nullable=False)
    mobile        = db.Column(db.String(30))
    account_status = db.Column(db.String(20), default='trial')  # trial | active | suspended | cancelled
    database_name = db.Column(db.String(100))  # NULL until a paid tenant DB is provisioned (Phase 3)
    # basic  -- Chart of Accounts is fully locked and every Post & Save
    #           action is blocked for everyone in this tenant except a
    #           Super Admin user (User.is_super_admin, checked in that
    #           tenant's own database -- see is_basic_mode() in
    #           database/routes/shared.py).
    # expert -- no restriction; normal RBAC applies as today.
    access_mode   = db.Column(db.String(20), nullable=False, default='expert')
    # How this customer's database came to be assigned. Creating the customer
    # and creating the physical database are two separate operations:
    #   automatic -- the application creates the database, builds its tables
    #                and saves the name (the original behaviour).
    #   manual    -- the Super Admin creates the database outside the
    #                application; only its name (and optional connection
    #                settings) are saved, and nothing is created.
    db_method     = db.Column(db.String(20), nullable=False, default='automatic')
    # Result of the last connection test: not_verified | connected | failed.
    db_status     = db.Column(db.String(20), nullable=False, default='not_verified')
    db_status_message = db.Column(db.String(500))   # the last test's outcome / error text
    db_checked_at = db.Column(db.DateTime)
    # False until the database holds this application's tables and the
    # customer's Admin login (built automatically, or by "Initialize
    # database", or found already present by a connection test) -- a customer
    # cannot log in before that.
    db_initialized = db.Column(db.Boolean, nullable=False, default=True)
    # Optional per-customer connection settings. Blank = use the
    # application's own defaults (DB_HOST/DB_PORT/DB_USER/DB_PASSWORD). The
    # password is stored encrypted, never in plain text.
    db_host       = db.Column(db.String(255))
    db_port       = db.Column(db.String(10))
    db_user       = db.Column(db.String(100))
    db_password_enc = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    subscriptions = db.relationship('Subscription', backref='customer', cascade='all, delete-orphan', lazy=True)

    def to_dict(self):
        sub = Subscription.query.filter_by(customer_id=self.id).order_by(Subscription.id.desc()).first()
        return {
            'id': self.id,
            'customer_name': self.customer_name,
            'company_name': self.company_name or '',
            'email': self.email,
            'mobile': self.mobile or '',
            'account_status': self.account_status or 'active',
            'database_name': self.database_name or '',
            'access_mode': self.access_mode or 'expert',
            'db_method': self.db_method or 'automatic',
            'db_status': self.db_status or 'not_verified',
            'db_status_message': self.db_status_message or '',
            'db_checked_at': self.db_checked_at.strftime('%Y-%m-%d %H:%M') if self.db_checked_at else '',
            'db_initialized': bool(self.db_initialized) if self.db_initialized is not None else True,
            'db_host': self.db_host or '',
            'db_port': self.db_port or '',
            'db_user': self.db_user or '',
            'has_db_password': bool(self.db_password_enc),
            'billing_cycle': sub.billing_cycle if sub else 'monthly',
            'end_date': sub.end_date.strftime('%Y-%m-%d') if sub and sub.end_date else '',
            'modules': [
                {
                    'module_id': sm.module_id,
                    'end_date': sm.end_date.strftime('%Y-%m-%d') if sm.end_date else '',
                }
                for sm in (sub.subscription_modules if sub else [])
            ],
        }


class Subscription(db.Model):
    __bind_key__ = 'saas'
    __tablename__ = 'proledge_saas_subscriptions'
    id            = db.Column(db.Integer, primary_key=True)
    customer_id   = db.Column(db.Integer, db.ForeignKey('proledge_saas_customers.id', ondelete='CASCADE'), nullable=False)
    billing_cycle = db.Column(db.String(20), default='trial')  # trial | monthly | yearly
    start_date    = db.Column(db.DateTime, default=datetime.utcnow)
    end_date      = db.Column(db.DateTime)
    status        = db.Column(db.String(20), default='trial')  # trial | active | expired | cancelled
    total_amount  = db.Column(db.Numeric(10, 2), default=0)

    subscription_modules = db.relationship('SubscriptionModule', backref='subscription', cascade='all, delete-orphan', lazy=True)


class SubscriptionModule(db.Model):
    __bind_key__ = 'saas'
    __tablename__ = 'proledge_saas_subscription_modules'
    id              = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('proledge_saas_subscriptions.id', ondelete='CASCADE'), nullable=False)
    module_id       = db.Column(db.Integer, db.ForeignKey('saas_modules.id'), nullable=False)
    price           = db.Column(db.Numeric(10, 2), default=0)  # snapshot at subscribe time
    start_date      = db.Column(db.DateTime, default=datetime.utcnow)
    end_date        = db.Column(db.DateTime)
    status          = db.Column(db.String(20), default='active')  # active | inactive

    module = db.relationship('SaasModule')


class Payment(db.Model):
    __bind_key__ = 'saas'
    __tablename__ = 'proledge_saas_payments'
    id                    = db.Column(db.Integer, primary_key=True)
    customer_id           = db.Column(db.Integer, db.ForeignKey('proledge_saas_customers.id', ondelete='CASCADE'), nullable=False)
    subscription_id       = db.Column(db.Integer, db.ForeignKey('proledge_saas_subscriptions.id', ondelete='CASCADE'), nullable=False)
    amount                = db.Column(db.Numeric(10, 2), default=0)
    payment_status        = db.Column(db.String(20), default='pending')  # pending | paid | failed
    transaction_reference = db.Column(db.String(150))
    payment_date          = db.Column(db.DateTime)


class SaasUserDirectory(db.Model):
    """Phase 3: username -> tenant database lookup. Populated whenever a
    tenant user is provisioned; unique on username so it can safely be used
    later (Phase 3.5) to resolve which database a login attempt belongs to
    before falling back to today's single shared-database lookup."""
    __bind_key__ = 'saas'
    __tablename__ = 'proledge_saas_user_directory'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    customer_id   = db.Column(db.Integer, db.ForeignKey('proledge_saas_customers.id', ondelete='CASCADE'), nullable=False)
    database_name = db.Column(db.String(100), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)


class SupportTicket(db.Model):
    """A customer's support request TO Proledg -- lives in the shared saas
    database (like Customer/Subscription), not inside the tenant's own
    database, so Proledg's own team can see every customer's tickets in one
    place (database/routes/support.py's Super Admin queue) rather than
    having to log into each tenant separately."""
    __bind_key__ = 'saas'
    __tablename__ = 'proledge_saas_support_tickets'
    id                   = db.Column(db.Integer, primary_key=True)
    ticket_no            = db.Column(db.String(20), unique=True, nullable=False)
    customer_id          = db.Column(db.Integer, db.ForeignKey('proledge_saas_customers.id', ondelete='CASCADE'), nullable=False)
    # The tenant's own user id (that database's users.id) -- never a real FK
    # since it lives in a different database entirely; kept only so "my
    # tickets" could later be narrowed to one submitter if ever needed.
    submitted_by_user_id = db.Column(db.Integer)
    full_name            = db.Column(db.String(150), nullable=False)
    email                = db.Column(db.String(150), nullable=False)
    phone                = db.Column(db.String(30))
    category             = db.Column(db.String(50), nullable=False)
    priority             = db.Column(db.String(20), nullable=False, default='medium')  # low | medium | high | urgent
    message              = db.Column(db.Text, nullable=False)
    status               = db.Column(db.String(20), nullable=False, default='open')    # open | in_progress | resolved | closed
    admin_notes          = db.Column(db.Text)
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at           = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship('Customer', backref=db.backref('support_tickets', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id,
            'ticket_no': self.ticket_no,
            'customer_id': self.customer_id,
            'customer_name': (self.customer.company_name or self.customer.customer_name) if self.customer else '',
            'full_name': self.full_name,
            'email': self.email,
            'phone': self.phone or '',
            'category': self.category,
            'priority': self.priority,
            'message': self.message,
            'status': self.status,
            'admin_notes': self.admin_notes or '',
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
            'updated_at': self.updated_at.strftime('%d/%m/%Y %H:%M') if self.updated_at else '',
        }


class SupportTicketMessage(db.Model):
    """One message in a ticket's two-way conversation, following the
    ticket's own original SupportTicket.message -- the customer can send
    follow-up messages and Proledg's own team (Super Admin) can reply, both
    visible to each other on the same thread."""
    __bind_key__ = 'saas'
    __tablename__ = 'proledge_saas_support_ticket_messages'
    id          = db.Column(db.Integer, primary_key=True)
    ticket_id   = db.Column(db.Integer, db.ForeignKey('proledge_saas_support_tickets.id', ondelete='CASCADE'), nullable=False)
    sender_type = db.Column(db.String(20), nullable=False)  # customer | proledg
    sender_name = db.Column(db.String(150))
    message     = db.Column(db.Text, nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    ticket = db.relationship('SupportTicket', backref=db.backref(
        'messages', lazy=True, cascade='all, delete-orphan',
        order_by='SupportTicketMessage.created_at'))

    def to_dict(self):
        return {
            'id': self.id, 'ticket_id': self.ticket_id,
            'sender_type': self.sender_type, 'sender_name': self.sender_name or '',
            'message': self.message,
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else '',
        }


def next_ticket_no():
    """Auto ticket number: TCK-<year>-<n>, scoped to the saas bind."""
    year = datetime.utcnow().year
    like = f'TCK-{year}-%'
    max_num = 0
    for t in SupportTicket.query.filter(SupportTicket.ticket_no.like(like)).all():
        try:
            num = int(t.ticket_no.rsplit('-', 1)[1])
            if num > max_num:
                max_num = num
        except (ValueError, IndexError):
            continue
    return f'TCK-{year}-{max_num + 1}'


def ensure_saas_schema():
    """Additive schema guard for the saas_master database, mirroring the
    ensure_schema() pattern above but pointed at the 'saas' bind's own
    engine -- db.session's raw SQL below always targets whichever
    database that connection is on, which is the saas bind here."""
    from sqlalchemy import text
    engine = db.engines['saas']
    with engine.connect() as conn:
        def table_exists(table):
            row = conn.execute(text(
                "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME=:t"
            ), {'t': table}).fetchone()
            return row is not None

        if (not table_exists('saas_modules') or not table_exists('proledge_saas_modules_dependencies')
                or not table_exists('proledge_saas_user_directory')):
            # Tables not created yet (db.create_all() runs before this) --
            # nothing to migrate on a brand-new database.
            return

        if table_exists('proledge_saas_customers'):
            cols = {row[0] for row in conn.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='proledge_saas_customers'"
            )).fetchall()}
            for column, ddl in (('access_mode', "VARCHAR(20) NOT NULL DEFAULT 'expert'"),
                                ('db_method', "VARCHAR(20) NOT NULL DEFAULT 'automatic'"),
                                ('db_status', "VARCHAR(20) NOT NULL DEFAULT 'not_verified'"),
                                ('db_status_message', 'VARCHAR(500)'),
                                ('db_checked_at', 'DATETIME'),
                                ('db_initialized', 'TINYINT(1) NOT NULL DEFAULT 1'),
                                ('db_host', 'VARCHAR(255)'),
                                ('db_port', 'VARCHAR(10)'),
                                ('db_user', 'VARCHAR(100)'),
                                ('db_password_enc', 'TEXT')):
                if column not in cols:
                    try:
                        conn.execute(text(f"ALTER TABLE proledge_saas_customers ADD {column} {ddl}"))
                        conn.commit()
                        print(f'ensure_saas_schema: added proledge_saas_customers.{column}')
                    except Exception as e:
                        print(f'ensure_saas_schema: could not add {column}: {e}')


def merge_saas_master_into_sellerms():
    """One-off, idempotent migration (Phase 4 -- single application merge):
    copies every row from the pre-merge `saas_master` database into these
    same-named/renamed tables now living inside `sellerms` itself. Safe to
    run on every startup -- each table is only copied while the new
    table's row count is still lower than the old table's, so re-running
    (or a Super Admin's later edits) never duplicates or overwrites
    anything. The old `saas_master` database is left untouched afterward,
    not dropped, as a migration safety net."""
    from sqlalchemy import text
    from config import OLD_SAAS_MASTER_DB_NAME as OLD_DB

    engine = db.engines['saas']
    # (old table name in saas_master, new table name in sellerms) --
    # dependency order matters for the FK constraints below.
    table_map = [
        ('saas_modules', 'saas_modules'),
        ('saas_customers', 'proledge_saas_customers'),
        ('saas_subscriptions', 'proledge_saas_subscriptions'),
        ('saas_subscription_modules', 'proledge_saas_subscription_modules'),
        ('saas_module_dependencies', 'proledge_saas_modules_dependencies'),
        ('saas_payments', 'proledge_saas_payments'),
        ('saas_user_directory', 'proledge_saas_user_directory'),
    ]

    def table_exists(conn, schema, table):
        row = conn.execute(text(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t"
        ), {'s': schema, 't': table}).fetchone()
        return row is not None

    with engine.connect() as conn:
        if not table_exists(conn, OLD_DB, 'saas_customers'):
            return  # old database/tables don't exist -- nothing to migrate (e.g. a brand-new install)
        current_db = conn.execute(text("SELECT DATABASE()")).scalar()

    # One committed transaction per table (not one shared across all 7) --
    # avoids any risk of a later table's exists/count check running inside
    # a transaction snapshot taken before an earlier table's insert in this
    # same call was visible.
    for old_table, new_table in table_map:
        with engine.begin() as conn:
            if not table_exists(conn, OLD_DB, old_table) or not table_exists(conn, current_db, new_table):
                continue
            old_count = conn.execute(text(f"SELECT COUNT(*) FROM `{OLD_DB}`.`{old_table}`")).scalar()
            new_count = conn.execute(text(f"SELECT COUNT(*) FROM `{new_table}`")).scalar()
            if new_count >= old_count:
                continue
            conn.execute(text(f"INSERT INTO `{new_table}` SELECT * FROM `{OLD_DB}`.`{old_table}`"))


def seed_saas_modules():
    """Idempotent seed of the initial SaaS module catalog -- only inserts
    rows that don't already exist (matched by module_code), so re-running
    on every startup (same convention as seed_rbac_catalog()) never
    duplicates or overwrites a Super Admin's later edits."""
    catalog = [
        # (code, name_en, name_ar, monthly, yearly, trial, order)
        ('accounting',   'Accounting',   'المحاسبة',       100, 1000, True,  10),
        ('sales',        'Sales',        'المبيعات',        80,  800, True,  20),
        ('purchase',     'Purchase',     'المشتريات',       80,  800, True,  30),
        ('inventory',    'Inventory',    'المخزون',        100, 1000, True,  40),
        ('hr',           'HR',           'الموارد البشرية',  80,  800, True,  50),
        ('payroll',      'Payroll',      'الرواتب',        100, 1000, False, 60),
        ('fixed_assets', 'Fixed Assets', 'الأصول الثابتة',  60,  600, False, 70),
        ('crm',          'CRM',          'إدارة العملاء',   80,  800, False, 80),
        ('projects',     'Projects',     'المشاريع',        80,  800, False, 90),
        ('reports',      'Reports',      'التقارير',        50,  500, True, 100),
    ]
    inserted = 0
    for code, name_en, name_ar, monthly, yearly, trial, order in catalog:
        if SaasModule.query.filter_by(module_code=code).first():
            continue
        db.session.add(SaasModule(
            module_code=code, module_name_en=name_en, module_name_ar=name_ar,
            monthly_price=monthly, yearly_price=yearly,
            trial_available=trial, status='active', display_order=order,
        ))
        inserted += 1
    if inserted:
        db.session.commit()
        # Payroll requires HR, matching the spec's example dependency.
        payroll = SaasModule.query.filter_by(module_code='payroll').first()
        hr = SaasModule.query.filter_by(module_code='hr').first()
        if payroll and hr and not SaasModuleDependency.query.filter_by(
            module_id=payroll.id, requires_module_id=hr.id
        ).first():
            db.session.add(SaasModuleDependency(module_id=payroll.id, requires_module_id=hr.id))
            db.session.commit()
    return inserted


# Default SaasModule -> RBAC Module.code links (see SaasModuleRbacLink's
# own docstring for why these two catalogs need an explicit bridge at
# all). This is a best-effort starting map based on what each SaaS
# product plausibly unlocks, NOT a business decision only Super Admin
# can make -- it is fully editable afterward from the SaaS Module
# Catalog screen, same as pricing or trial availability. Two intentional
# omissions:
#   - 'payroll' has no RBAC module of its own to link -- its forms live
#     inside the 'employee' RBAC module alongside HR's own forms (see
#     rbac.py's MODULE_FORM_CATALOG), and Payroll already requires HR
#     as a SaasModuleDependency above, so a Payroll subscriber already
#     has 'employee' unlocked via HR. Payroll's own page-level gating is
#     enforced separately, at the form level, by its own
#     @permission_required decorators (database/routes/payroll.py).
#   - 'fixed_assets', 'crm', 'projects', 'reports' have no matching RBAC
#     module in the catalog yet, so they stay unlinked until one exists.
SAAS_MODULE_RBAC_DEFAULTS = {
    'accounting': ['coa', 'financial', 'journal', 'cash_bank'],
    'sales':      ['sale'],
    'purchase':   ['purchase'],
    'inventory':  ['store'],
    'hr':         ['employee'],
}


def seed_saas_module_rbac_links():
    """Idempotent: only inserts a link that doesn't already exist, so a
    Super Admin's later edits (adding/removing a link on the SaaS Module
    Catalog screen) are never overwritten by a later restart."""
    inserted = 0
    for module_code, rbac_codes in SAAS_MODULE_RBAC_DEFAULTS.items():
        saas_module = SaasModule.query.filter_by(module_code=module_code).first()
        if not saas_module:
            continue
        for rbac_code in rbac_codes:
            if SaasModuleRbacLink.query.filter_by(
                saas_module_id=saas_module.id, rbac_module_code=rbac_code
            ).first():
                continue
            db.session.add(SaasModuleRbacLink(saas_module_id=saas_module.id, rbac_module_code=rbac_code))
            inserted += 1
    if inserted:
        db.session.commit()
    return inserted


# ══════════════════════════════════════════════════════════════════
#  Lightweight startup schema guard (MySQL ADD COLUMN, idempotent)
# ══════════════════════════════════════════════════════════════════
def ensure_schema():
    """Add any newly-introduced columns that an older database may lack.

    Idempotent and safe to call on every startup: each column is added
    only if the table exists and the column is missing. This keeps schema
    upgrades in the model layer instead of separate migration scripts.
    """
    from sqlalchemy import text

    # (table, column, column definition)
    wanted = [
        ('employee_documents', 'uploaded_by',    'INTEGER'),
        ('employee_documents', 'employee_code',  'VARCHAR(20)'),
        ('employee_documents', 'employee_name',  'VARCHAR(200)'),
        ('employee_documents', 'passport_number','VARCHAR(50)'),
        ('employee_documents', 'iqama_number',   'VARCHAR(50)'),
        ('users', 'theme_sidebar_bg',          'VARCHAR(20)'),
        ('users', 'theme_sidebar_text',        'VARCHAR(20)'),
        ('users', 'theme_sidebar_active_bg',   'VARCHAR(40)'),
        ('users', 'theme_sidebar_active_text', 'VARCHAR(20)'),
        ('users', 'theme_primary',             'VARCHAR(20)'),
        ('users', 'theme_topbar_bg',           'VARCHAR(20)'),
        ('users', 'theme_heading_text',        'VARCHAR(20)'),
        ('item_master', 'levelfive_code',      'VARCHAR(40)'),
        ('item_master', 'levelfive_drawer_en', 'VARCHAR(250)'),
        ('item_master', 'levelfive_drawer_ar', 'VARCHAR(250)'),
        ('employees',   'blood_group',         'VARCHAR(10)'),
        ('employees',   'services_charges',    'DECIMAL(12,2) DEFAULT 0'),
        ('employees',   'levelfive_code',      'VARCHAR(40)'),
        ('employees',   'levelfive_drawer',    'VARCHAR(250)'),
        ('employees',   'po_ot_rate',          'DECIMAL(12,2) DEFAULT 0'),
        ('buyers',      'levelfive_code',      'VARCHAR(40)'),
        ('buyers',      'levelfive_drawer',    'VARCHAR(250)'),
        ('suppliers',   'levelfive_code',      'VARCHAR(40)'),
        ('suppliers',   'levelfive_drawer',    'VARCHAR(250)'),
        ('salary_consolidation', 'services_charges', 'DECIMAL(12,2) DEFAULT 0'),
        ('salary_consolidation', 'po_ot_rate', 'DECIMAL(12,2) DEFAULT 0'),
        ('employee_work_allocation', 'buyer_department_id', 'INTEGER'),
        ('item_master', 'store',               'VARCHAR(40)'),
        ('item_master', 'expense_type',        'VARCHAR(20)'),
        ('purchase_requests',     'purchase_type', 'VARCHAR(20)'),
        ('purchase_quotations',   'purchase_type', 'VARCHAR(20)'),
        ('purchase_orders',       'purchase_type', 'VARCHAR(20)'),
        ('goods_receipt_notes',   'purchase_type', 'VARCHAR(20)'),
        ('purchase_invoices',     'purchase_type', 'VARCHAR(20)'),
        ('goods_return_requests', 'purchase_type', 'VARCHAR(20)'),
        ('purchase_debit_memos',  'purchase_type', 'VARCHAR(20)'),
        ('grl',        'grl_no',               'VARCHAR(40)'),
        ('grl_detail', 'account_name_ar',      'VARCHAR(250)'),
        ('purchase_invoices', 'posting_status', "VARCHAR(10) NOT NULL DEFAULT 'Saved'"),
        ('journal_ledger',    'purchase_invoice_id', 'INTEGER'),
        ('purchase_debit_memos', 'purchase_return_note_id', 'INTEGER'),
        ('purchase_return_notes', 'posting_status', "VARCHAR(10) NOT NULL DEFAULT 'Saved'"),
        ('purchase_debit_memos',  'posting_status', "VARCHAR(10) NOT NULL DEFAULT 'Saved'"),
        ('journal_ledger', 'purchase_return_note_id', 'INTEGER'),
        ('journal_ledger', 'purchase_debit_memo_id',  'INTEGER'),
        ('journal_ledger', 'sales_return_note_id',    'INTEGER'),
        ('sales_credit_memos', 'sales_return_note_id', 'INTEGER'),
        # Delivery Note / Sales Invoice / Sales Credit Memo GL posting.
        ('sale_delivery_notes', 'posting_status', "VARCHAR(10) NOT NULL DEFAULT 'Saved'"),
        ('sales_invoices',      'posting_status', "VARCHAR(10) NOT NULL DEFAULT 'Saved'"),
        ('sales_credit_memos',  'posting_status', "VARCHAR(10) NOT NULL DEFAULT 'Saved'"),
        ('journal_ledger', 'delivery_note_id',     'INTEGER'),
        ('journal_ledger', 'sales_invoice_id',     'INTEGER'),
        ('journal_ledger', 'sales_credit_memo_id', 'INTEGER'),
        # Delivery Note stock decrement (mirrors GRN's stock increment).
        ('sale_delivery_line_items', 'sales_order_line_item_id', 'INTEGER'),
        ('store_transactions', 'sales_order_id',           'INTEGER'),
        ('store_transactions', 'sales_order_doc_no',       'VARCHAR(20)'),
        ('store_transactions', 'sales_order_line_item_id', 'INTEGER'),
        ('store_transactions', 'delivery_note_id',         'INTEGER'),
        ('store_transactions', 'delivery_note_doc_no',     'VARCHAR(20)'),
        ('store_transactions', 'delivery_line_item_id',    'INTEGER'),
        ('store_transactions', 'buyer_id',                 'INTEGER'),
        ('store_transactions', 'buyer_name',                'VARCHAR(200)'),
        # Standalone (no-GRN) Purchase Invoice stock receipt.
        ('store_transactions', 'purchase_invoice_id',            'INTEGER'),
        ('store_transactions', 'purchase_invoice_doc_no',        'VARCHAR(20)'),
        ('store_transactions', 'purchase_invoice_line_item_id',  'INTEGER'),
        ('item_master', 'print_name_en',       'VARCHAR(200)'),
        ('item_master', 'print_name_ar',       'VARCHAR(200)'),
        ('purchase_goods_receipt_notes_line_item', 'purchase_order_line_item_id', 'INTEGER'),
        ('purchase_goods_return_notes_line_item', 'purchase_invoice_line_item_id', 'INTEGER'),
        ('employee_allowance_types', 'description', 'VARCHAR(255)'),
        ('purchase_invoices', 'from_date', 'DATE'),
        ('purchase_invoices', 'to_date', 'DATE'),
        ('sales_quotations', 'terms_conditions', 'TEXT'),
        ('sales_quotations', 'subject', 'VARCHAR(300)'),
        ('owners', 'header_path', 'VARCHAR(500)'),
        ('owners', 'footer_path', 'VARCHAR(500)'),
        ('owners', 'stamp_path', 'VARCHAR(500)'),
        ('sales_quotations', 'report_style', "VARCHAR(20) DEFAULT 'header_footer'"),
        ('users', 'signature_path', 'VARCHAR(500)'),
        ('sales_quotations', 'body', 'TEXT'),
        ('sales_quotations', 'sign_stamp', 'TEXT'),
        ('sales_quotations', 'item_summary_display', "VARCHAR(10) DEFAULT 'on'"),
        ('owners', 'sq_default_terms_conditions', 'TEXT'),
        ('owners', 'sq_default_sign_stamp', 'TEXT'),
        ('owners', 'sinv_print_template', "VARCHAR(20) DEFAULT 'formal'"),
        # ── Role & Permission system (additive on the pre-existing users/activity_logs tables) ──
        ('users', 'role_id',            'INTEGER'),
        ('users', 'is_super_admin',     'TINYINT(1) DEFAULT 0'),
        ('users', 'is_protected',       'TINYINT(1) DEFAULT 0'),
        ('users', 'last_login_at',      'DATETIME'),
        ('users', 'last_login_ip',      'VARCHAR(45)'),
        ('users', 'failed_login_count', 'INTEGER DEFAULT 0'),
        ('users', 'full_name',          'VARCHAR(150)'),
        ('users', 'mobile',             'VARCHAR(30)'),
        ('users', 'created_by',         'INTEGER'),
        # ── Two-factor auth (TOTP, additive) ──
        ('users', 'totp_secret',        'VARCHAR(64)'),
        ('users', 'totp_enabled',       'TINYINT(1) DEFAULT 0'),
        ('users', 'totp_confirmed_at',  'DATETIME'),
        ('activity_logs', 'old_value',         'LONGTEXT'),
        ('activity_logs', 'new_value',         'LONGTEXT'),
        ('activity_logs', 'status',            "VARCHAR(20) DEFAULT 'success'"),
        ('activity_logs', 'username_snapshot', 'VARCHAR(80)'),
        # ── Outgoing Payment: full AP-settlement module (replaces the
        #    earlier simple voucher) ──
        ('purchase_invoices', 'paid_amount',      'DECIMAL(14,2) DEFAULT 0'),
        ('journal_ledger',    'outgoing_payment_id', 'INTEGER'),
        # ── Incoming Payment: full AR-settlement module, the mirror of
        #    Outgoing Payment above (replaces the earlier simple voucher) ──
        ('sales_invoices',    'paid_amount',      'DECIMAL(14,2) DEFAULT 0'),
        ('journal_ledger',    'incoming_payment_id', 'INTEGER'),
        # ── Payroll: GRL/Journal Entry attachment (Debit Salary Expense /
        #    Credit Salaries Payable) when a batch is set to Post ──
        ('journal_ledger',    'payroll_id',       'VARCHAR(20)'),
        # ── GRL: direct entity reference + a dedicated vendor/customer
        #    reference field on each detail line, kept separate from the
        #    GL account `code` so the two can never be confused with
        #    each other again. ──
        ('journal_ledger',        'supplier_id',    'INTEGER'),
        ('journal_ledger',        'buyer_id',       'INTEGER'),
        ('journal_ledger',        'employee_id',    'INTEGER'),
        ('journal_ledger_detail', 'reference_code', 'VARCHAR(100)', 'code'),
        ('journal_entry_detail',  'reference_code', 'VARCHAR(100)', 'code'),
        # ── Opening Balance ──
        ('journal_ledger', 'opening_balance_id', 'INTEGER'),
        # ── Auto Code Selection: optional Level Four context above the
        #    existing Level Five code, plus Status (currently only
        #    'Approved') and an optional Payment Mode scope ──
        ('auto_code_selection', 'levelfour_code',      'VARCHAR(30)'),
        ('auto_code_selection', 'levelfour_drawer_en',  'VARCHAR(200)'),
        ('auto_code_selection', 'levelfour_drawer_ar',  'VARCHAR(200)'),
        ('auto_code_selection', 'status',              "VARCHAR(20) NOT NULL DEFAULT 'Approved'"),
        ('auto_code_selection', 'payment_mode',        'VARCHAR(20)'),
        # ── Payroll: per-employee payable period (joining-date-aware
        #    eligibility/attendance), distinct from the batch's month_from/
        #    month_to which stay the master period on every row ──
        ('salary_consolidation', 'emp_from_date', 'DATE', 'month_to'),
        ('salary_consolidation', 'emp_to_date',   'DATE', 'emp_from_date'),
        # ── ZATCA: scope each settings row to one SaaS customer, so trial
        #    customers sharing the platform database never see or edit
        #    another trial customer's VAT number/CSR/certificates. See the
        #    ZatcaSettings.customer_id column comment for the full reason. ──
        ('zatca_settings', 'customer_id', 'INTEGER'),
        # ── ZATCA Phase 2: per-invoice cryptographic chain state, generated
        #    only once a Sales Invoice is Posted and "Create ZATCA Invoice"
        #    is run -- see database/zatca/engine.py and
        #    database/routes/zatca.py. ──
        ('sales_invoices', 'zatca_uuid',             'VARCHAR(36)'),
        ('sales_invoices', 'zatca_icv',              'BIGINT'),
        ('sales_invoices', 'zatca_pih',               'VARCHAR(200)'),
        ('sales_invoices', 'zatca_invoice_hash',     'VARCHAR(200)'),
        ('sales_invoices', 'zatca_xml_signature',    'LONGTEXT'),
        ('sales_invoices', 'zatca_qr_code',          'LONGTEXT'),
        ('sales_invoices', 'zatca_status',           "VARCHAR(20) DEFAULT 'not_generated'"),
        ('sales_invoices', 'zatca_submission_type',  'VARCHAR(20)'),
        ('sales_invoices', 'zatca_cleared_xml_path', 'VARCHAR(500)'),
        ('sales_invoices', 'zatca_response_message', 'TEXT'),
        ('sales_invoices', 'zatca_generated_at',     'DATETIME'),
        ('sales_invoices', 'zatca_submitted_at',     'DATETIME'),
        ('sales_invoices', 'zatca_attachment_id',    'INTEGER'),
        # ── ZATCA UBL tax-category classification: the existing tax_code
        #    column is free text ("VAT 15%") and cannot be reliably reverse-
        #    mapped to a UBL tax category by pattern-matching alone. These
        #    let an admin classify each tax code once. ──
        ('sales_tax_code', 'zatca_category',         'VARCHAR(2)'),
        ('sales_tax_code', 'zatca_exemption_reason', 'VARCHAR(20)'),
        # ── Sales Invoice: Invoice Period (From Date/To Date), mirroring
        #    the same fields already on purchase_invoices -- the invoice's
        #    billing month is always derived display-only from To Date. ──
        ('sales_invoices', 'from_date', 'DATE'),
        ('sales_invoices', 'to_date',   'DATE'),
        ('sales_invoices', 'project_ref', 'VARCHAR(150)'),
        ('employee_work_allocation', 'salary_order', 'INTEGER DEFAULT 1'),
        ('owners', 'short_address', 'VARCHAR(20)'),
        ('salary_consolidation', 'food', 'DECIMAL(12,2) DEFAULT 0'),
        ('salary_consolidation', 'house_rent', 'DECIMAL(12,2) DEFAULT 0'),
        ('salary_consolidation', 'transportation', 'DECIMAL(12,2) DEFAULT 0'),
    ]

    def table_exists(table):
        # MySQL's INFORMATION_SCHEMA is shared across every database on the
        # server (unlike SQL Server, where each database has its own catalog
        # views), so TABLE_SCHEMA must be pinned to the current database --
        # otherwise a same-named table elsewhere on the server would match.
        row = db.session.execute(text(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME=:t"
        ), {'t': table}).fetchone()
        return row is not None

    def existing_columns(table):
        rows = db.session.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME=:t"
        ), {'t': table}).fetchall()
        return {row[0] for row in rows}

    def add_column(table, column, ddl, after=None):
        stmt = f'ALTER TABLE {table} ADD {column} {ddl}'
        if after:
            stmt += f' AFTER {after}'
        db.session.execute(text(stmt))

    for entry in wanted:
        # Most entries are (table, column, ddl); a 4th element positions the
        # new column right after a named existing one (MySQL appends to the
        # end of the table by default otherwise) -- optional, so every
        # pre-existing 3-tuple entry above is untouched.
        table, column, ddl = entry[:3]
        after = entry[3] if len(entry) > 3 else None
        try:
            if not table_exists(table):
                continue
            if column in existing_columns(table):
                continue
            add_column(table, column, ddl, after)
            db.session.commit()
            print(f'ensure_schema: added {table}.{column}')
        except Exception as e:
            db.session.rollback()
            print(f'ensure_schema: could not add {table}.{column}: {e}')

    # sales_invoices.doc_no predates the per-transaction-type numbering
    # scheme (STD-INV-2026-1000 etc., embedding the full transaction_type
    # as its prefix) and was created VARCHAR(20), too narrow for the new
    # longer format -- widen it on existing databases rather than risk a
    # silent truncation error the first time a sequence runs long.
    try:
        if table_exists('sales_invoices'):
            row = db.session.execute(text(
                "SELECT CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='sales_invoices' AND COLUMN_NAME='doc_no'"
            )).fetchone()
            if row and row[0] is not None and row[0] < 40:
                db.session.execute(text('ALTER TABLE sales_invoices MODIFY COLUMN doc_no VARCHAR(40)'))
                db.session.commit()
                print('ensure_schema: widened sales_invoices.doc_no to VARCHAR(40)')
    except Exception as e:
        db.session.rollback()
        print(f'ensure_schema: could not widen sales_invoices.doc_no: {e}')

    # activity_logs.user_id predates the RBAC audit log and was created
    # NOT NULL; a failed login against a nonexistent username has no real
    # user to attach to, so the constraint must be relaxed on existing DBs.
    try:
        if table_exists('activity_logs'):
            row = db.session.execute(text(
                "SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='activity_logs' AND COLUMN_NAME='user_id'"
            )).fetchone()
            if row and row[0] == 'NO':
                # MySQL uses MODIFY COLUMN, not MSSQL's ALTER COLUMN, to change a
                # column's definition (nullability included).
                db.session.execute(text('ALTER TABLE activity_logs MODIFY COLUMN user_id INTEGER NULL'))
                db.session.commit()
                print('ensure_schema: relaxed activity_logs.user_id to nullable')
    except Exception as e:
        db.session.rollback()
        print(f'ensure_schema: could not relax activity_logs.user_id: {e}')

    # outgoing_payments/outgoing_payment_details -- the earlier "AP-
    # settlement" Outgoing Payment schema -- is fully replaced by the new
    # outgoing_payment / outgoing_payment_adjustment / outgoing_payment_gl_detail
    # schema (one master + two detail tables, Advance/Outstanding
    # payment_type, GL Account direct multi-line expense posting). The old
    # table held only 3 header rows / 6 detail rows with no real GRL/
    # JournalEntry backing 2 of the "Posted" ones (confirmed before this
    # migration was written) -- low enough risk to drop outright rather
    # than attempt a field-by-field migration into the new, differently-
    # shaped schema. journal_ledger.outgoing_payment_id's FK constraint
    # references the OLD table by name; it must be dropped before
    # outgoing_payments can be dropped, and re-created afterward pointing
    # at the new table, since create_all() only creates missing tables --
    # it never alters an existing column's FK target.
    try:
        if table_exists('outgoing_payments'):
            if table_exists('journal_ledger') and 'outgoing_payment_id' in existing_columns('journal_ledger'):
                fk_row = db.session.execute(text(
                    "SELECT CONSTRAINT_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='journal_ledger' "
                    "AND COLUMN_NAME='outgoing_payment_id' AND REFERENCED_TABLE_NAME IS NOT NULL"
                )).fetchone()
                if fk_row:
                    db.session.execute(text(f'ALTER TABLE journal_ledger DROP FOREIGN KEY {fk_row[0]}'))
            if table_exists('outgoing_payment_details'):
                db.session.execute(text('DROP TABLE outgoing_payment_details'))
            db.session.execute(text('DROP TABLE outgoing_payments'))
            db.session.commit()
            db.create_all(bind_key=None)
            if table_exists('journal_ledger') and table_exists('outgoing_payment'):
                db.session.execute(text(
                    'ALTER TABLE journal_ledger ADD FOREIGN KEY (outgoing_payment_id) '
                    'REFERENCES outgoing_payment(id) ON DELETE CASCADE'
                ))
                db.session.commit()
            print('ensure_schema: replaced outgoing_payments with the new outgoing_payment/adjustment/gl_detail schema')
    except Exception as e:
        db.session.rollback()
        print(f'ensure_schema: could not migrate outgoing_payments to the new schema: {e}')

    # incoming_payments/incoming_payment_details -- the earlier "AR-
    # settlement" Incoming Payment schema -- is fully replaced by the new
    # incoming_payment / incoming_payment_adjustment / incoming_payment_gl_detail
    # schema, the exact mirror of the outgoing_payment migration above.
    # The old table was empty (confirmed before this migration was written)
    # -- safe to drop outright. journal_ledger.incoming_payment_id's FK
    # constraint references the OLD table by name; it must be dropped
    # before incoming_payments can be dropped, and re-created afterward
    # pointing at the new table.
    try:
        if table_exists('incoming_payments'):
            if table_exists('journal_ledger') and 'incoming_payment_id' in existing_columns('journal_ledger'):
                fk_row = db.session.execute(text(
                    "SELECT CONSTRAINT_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='journal_ledger' "
                    "AND COLUMN_NAME='incoming_payment_id' AND REFERENCED_TABLE_NAME IS NOT NULL"
                )).fetchone()
                if fk_row:
                    db.session.execute(text(f'ALTER TABLE journal_ledger DROP FOREIGN KEY {fk_row[0]}'))
            if table_exists('incoming_payment_details'):
                db.session.execute(text('DROP TABLE incoming_payment_details'))
            db.session.execute(text('DROP TABLE incoming_payments'))
            db.session.commit()
            db.create_all(bind_key=None)
            if table_exists('journal_ledger') and table_exists('incoming_payment'):
                db.session.execute(text(
                    'ALTER TABLE journal_ledger ADD FOREIGN KEY (incoming_payment_id) '
                    'REFERENCES incoming_payment(id) ON DELETE CASCADE'
                ))
                db.session.commit()
            print('ensure_schema: replaced incoming_payments with the new incoming_payment/adjustment/gl_detail schema')
    except Exception as e:
        db.session.rollback()
        print(f'ensure_schema: could not migrate incoming_payments to the new schema: {e}')

    # Ledger System: account code lookups and chronological ordering (posting
    # date, then JE id, then line id -- the id columns are already indexed
    # via their PK/FK constraints) are the two hot paths for every
    # per-account running-balance query, so both get an explicit index.
    wanted_indexes = [
        ('journal_entry_detail', 'ix_jed_code', ['code']),
        ('journal_entry_detail', 'ix_jed_reference_code', ['reference_code']),
        ('journal_entries', 'ix_je_posting_date', ['posting_date']),
        ('journal_entries', 'ix_je_origin_type', ['origin_type']),
    ]

    def existing_indexes(table):
        rows = db.session.execute(text(
            "SELECT DISTINCT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME=:t"
        ), {'t': table}).fetchall()
        return {row[0] for row in rows}

    for table, index_name, columns in wanted_indexes:
        try:
            if not table_exists(table):
                continue
            if index_name in existing_indexes(table):
                continue
            cols = ', '.join(columns)
            db.session.execute(text(f'CREATE INDEX {index_name} ON {table} ({cols})'))
            db.session.commit()
            print(f'ensure_schema: created index {index_name} on {table}')
        except Exception as e:
            db.session.rollback()
            print(f'ensure_schema: could not create index {index_name} on {table}: {e}')