"""ZATCA (Saudi e-invoicing) Phase 2 engine.

Framework- and DB-context-free by design: every function here takes plain
values/ORM objects it's handed and returns plain values, so it can be
exercised in a unit test without a running Flask app or a live database
transaction. `database/routes/zatca.py` and the Sales Invoice "Create
ZATCA Invoice" route in `database/routes/sales.py` are the only callers.

`build_invoice_xml()`'s element shape/order, `_party_block()`,
`_line_item_block()`, and `sign_and_finalize()`'s XAdES ds:Signature
structure (element names, order, namespaces, algorithm URIs, and the
CertDigest/SignedProperties hex-then-base64 digest quirk) were all
reconstructed against a real, signed-and-accepted ZATCA invoice sample
rather than guessed, and are a direct, verified structural match to it.
Two things remain genuinely unverified against ZATCA's own compliance-
check tool: `canonicalize()`'s exact byte-level output (lxml's
non-exclusive C14N 1.0 is used as a practical stand-in for the C14N 1.1
the sample's own CanonicalizationMethod declares -- identical for any
document without xml:base/xml:id, which is every invoice this engine
generates, but not independently confirmed against ZATCA's own
canonicalizer), and the exact canonicalization of the isolated
xades:SignedProperties fragment referenced by the second ds:Reference
(no sample transform declares this explicitly either way). `build_csr()`'s
non-standard subjectAltName/certificateTemplateName profile is
reverse-engineered from cross-checking several independent real-world
implementations (see its own docstring) rather than a ZATCA-published
spec, and is the other genuinely unverified piece -- a live 400 from
ZATCA's compliance endpoint is the signal to revisit it first. Everything
else (hash chaining, QR TLV encoding, UBL well-formedness, and the ECDSA
signature's own cryptographic self-consistency) is deterministic and
fully covered by local tests.
"""
import base64
import hashlib
import uuid as uuid_lib
from datetime import datetime, timezone
from decimal import Decimal

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.x509.oid import NameOID
from cryptography.x509.name import _ASN1Type
from lxml import etree

UBL_NSMAP = {
    None: 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
    'ext': 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2',
}
ROOT_NS = {
    'Invoice': 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
    'CreditNote': 'urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2',
    'DebitNote': 'urn:oasis:names:specification:ubl:schema:xsd:DebitNote-2',
}
CAC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
CBC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
EXT = 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2'

# transaction_type -> (UBL root element, type-code element name, numeric code,
# category). The prefix selects Standard vs Simplified (drives the
# InvoiceTypeCode "name" subtype flag below); INV/CR/DR selects the UBL
# document itself. Both the current Transaction Type dropdown's hyphenated
# values (STD-INV, SIM-CR, ...) and the older underscore-separated values
# already stored on invoices created before the dropdown was consolidated
# to a single field (STD_INV, SMP_CR, ...) are mapped here to the exact
# same tuples, so existing rows keep working unchanged -- historical
# documents are never migrated -- while every new Sales Invoice uses only
# the current SINV_TRANSACTION_TYPES values below.
TXN_TYPE_MAP = {
    'STD-INV': ('Invoice',    'InvoiceTypeCode',    '388', 'standard'),
    'STD-CR':  ('CreditNote', 'CreditNoteTypeCode',  '381', 'standard'),
    'STD-DR':  ('DebitNote',  'DebitNoteTypeCode',   '383', 'standard'),
    'SIM-INV': ('Invoice',    'InvoiceTypeCode',    '388', 'simplified'),
    'SIM-CR':  ('CreditNote', 'CreditNoteTypeCode',  '381', 'simplified'),
    'SIM-DR':  ('DebitNote',  'DebitNoteTypeCode',   '383', 'simplified'),
    # Legacy aliases -- values stored by Sales Invoices created before the
    # Transaction Type dropdown was consolidated to the hyphenated form
    # above. Never used for new documents; kept so old rows still build a
    # valid UBL document if their XML is ever (re)rendered.
    'STD_INV': ('Invoice',    'InvoiceTypeCode',    '388', 'standard'),
    'STD_CR':  ('CreditNote', 'CreditNoteTypeCode',  '381', 'standard'),
    'STD_DR':  ('DebitNote',  'DebitNoteTypeCode',   '383', 'standard'),
    'SMP_INV': ('Invoice',    'InvoiceTypeCode',    '388', 'simplified'),
    'SMP_CR':  ('CreditNote', 'CreditNoteTypeCode',  '381', 'simplified'),
    'SMP_DR':  ('DebitNote',  'DebitNoteTypeCode',   '383', 'simplified'),
}
# NOTE (unverified assumption): the 7-digit InvoiceTypeCode "name" subtype
# flag's exact digit positions/meaning should be re-checked against ZATCA's
# current technical spec before relying on this for a real submission --
# these two literal values are the commonly-published ones for a plain
# (non-third-party, non-nominal, non-export, non-summary) invoice.
INVOICE_NAME_SUBTYPE = {'standard': '0100000', 'simplified': '0200000'}

# The only 6 values the Sales Invoice "Transaction Type" dropdown may submit
# for a NEW document (database/routes/sales.py's sinv_add()/sinv_edit()/
# _apply_sinv_fields() validate against this exact tuple). Each one is also
# used verbatim as the document number's prefix -- see
# database/routes/sales.py's _next_sinv_doc_no().
SINV_TRANSACTION_TYPES = ('STD-INV', 'STD-DR', 'STD-CR', 'SIM-INV', 'SIM-DR', 'SIM-CR')


# ─────────────────────────────────────────────────────────────────
# Hash chain primitives
# ─────────────────────────────────────────────────────────────────

def new_uuid():
    return str(uuid_lib.uuid4())


def genesis_hash():
    """The fixed chain-start value every taxpayer's very first invoice uses
    as its Previous Invoice Hash -- computed (never a hardcoded literal) so
    there is no transcription-error risk."""
    return base64.b64encode(hashlib.sha256(b'0').digest()).decode('ascii')


def compute_invoice_hash(canonical_bytes):
    return base64.b64encode(hashlib.sha256(canonical_bytes).digest()).decode('ascii')


# ─────────────────────────────────────────────────────────────────
# Secret encryption at rest (private key / CSID secrets)
# ─────────────────────────────────────────────────────────────────

def derive_fernet_key(app_secret_key):
    """Deterministically derive a 32-byte urlsafe-base64 Fernet key from the
    Flask app's own SECRET_KEY, so no separate key-management scheme is
    needed for this first pass. (A dedicated ZATCA_ENCRYPTION_KEY env var
    can override this later without changing any stored ciphertext format,
    since Fernet keys are just swapped, not derived from stored data.)"""
    digest = hashlib.sha256(app_secret_key.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plaintext, app_secret_key):
    if plaintext is None:
        return None
    f = Fernet(derive_fernet_key(app_secret_key))
    return f.encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt_secret(ciphertext, app_secret_key):
    if not ciphertext:
        return None
    f = Fernet(derive_fernet_key(app_secret_key))
    return f.decrypt(ciphertext.encode('ascii')).decode('utf-8')


# ─────────────────────────────────────────────────────────────────
# EC keypair + CSR (onboarding)
# ─────────────────────────────────────────────────────────────────

def generate_keypair():
    """secp256k1, the curve ZATCA requires for e-invoicing CSIDs."""
    private_key = ec.generate_private_key(ec.SECP256K1())
    return private_key, private_key.public_key()


def private_key_to_pem(private_key):
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode('ascii')


def public_key_to_pem(public_key):
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode('ascii')


def public_key_raw_b64(public_key):
    """Raw uncompressed EC point bytes (X9.62), base64-encoded -- this is
    what ZATCA's QR tag 8 (public key) expects, NOT a PEM-wrapped key.
    Feeds ZatcaSettings.cert_public_key_b64, set whenever a keypair is
    generated or a certificate/key pair is imported."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.b64encode(raw).decode('ascii')


def load_certificate(binary_token):
    """Parse a ZATCA "Binary Security Token" (or any taxpayer-supplied
    certificate) into a `cryptography.x509.Certificate`. ZATCA's real CSID
    responses wrap the certificate in an extra layer of base64 on top of
    its own base64 (i.e. base64(base64(DER)), not just base64(DER)) -- a
    documented quirk of their API, not a corrupted value -- so a plain
    single-layer certificate is tried first and a double-decoded one
    second. Returns None if nothing parses as a certificate -- never
    raises, since callers use this for optional, best-effort display/QR
    data that must never block an otherwise-successful action."""
    import re
    if not binary_token:
        return None
    token = binary_token.strip()
    if '-----BEGIN CERTIFICATE-----' in token:
        try:
            return x509.load_pem_x509_certificate(token.encode('utf-8'))
        except Exception:
            return None
    try:
        der = base64.b64decode(re.sub(r'\s+', '', token))
    except Exception:
        return None
    for candidate in (der, _try_b64_decode(der)):
        if not candidate:
            continue
        try:
            return x509.load_der_x509_certificate(candidate)
        except Exception:
            continue
    return None


def extract_cert_ca_signature_b64(binary_token):
    """Pulls the CA's own signature bytes off an X.509 certificate -- what
    ZATCA's QR tag 9 expects. Returns the signature, base64-encoded, or
    None if `binary_token` can't be parsed as a certificate."""
    cert = load_certificate(binary_token)
    return base64.b64encode(cert.signature).decode('ascii') if cert else None


def certificate_common_name(binary_token):
    """The certificate's Subject Common Name (e.g. "1234567890 - 05172026
    - Live" for a ZATCA CSID) -- a human-meaningful label for a
    certificate history list. None if unparseable."""
    cert = load_certificate(binary_token)
    if not cert:
        return None
    try:
        attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return attrs[0].value if attrs else None
    except Exception:
        return None


def certificate_to_pem_bytes(binary_token):
    """Re-encode a stored ZATCA binary token as standard PEM bytes (with
    -----BEGIN/END CERTIFICATE----- headers) regardless of whether it was
    stored single- or double-base64-encoded -- so downloading it gives a
    file any standard tool (a certificate viewer, openssl, browser
    import) can open directly. None if unparseable."""
    cert = load_certificate(binary_token)
    if not cert:
        return None
    return cert.public_bytes(encoding=serialization.Encoding.PEM)


def _try_b64_decode(data):
    """Best-effort: treat `data` as base64 text and decode one more layer.
    Returns None (never raises) if it isn't valid base64."""
    try:
        return base64.b64decode(data, validate=True)
    except Exception:
        return None


def load_private_key_from_pem(pem_text):
    """Load a private key pasted as PEM text. Several independent failure
    classes are handled before giving up:

    0. The pasted text has no -----BEGIN...----- marker at all -- it's a
       bare base64-encoded DER key (SEC1 or PKCS8), which some tools/API
       responses hand out without ever wrapping it in PEM headers. This is
       tried first via `_try_load_bare_base64_der()`.
    1. Paste corruption -- a leading byte-order-mark, typographic dashes a
       rich-text editor substituted for the plain ASCII hyphens PEM markers
       require, or literal HTML-entity-encoded newlines (`&#10;`) -- all
       silently produce `cryptography`'s same generic "could not
       deserialize" error with no hint as to the real cause. These are
       auto-repaired via `_normalize_pem_text()` before the first attempt.
    2. `cryptography` deliberately refuses to parse an EC key encoded with
       EXPLICIT curve parameters rather than a named-curve OID reference (a
       security policy of the library itself, not a real corruption of the
       key) -- a common outcome when a key was generated via `openssl
       ecparam ... -param_enc explicit`, a pattern several ZATCA onboarding
       walkthroughs happen to use. When that's the failure, fall back to
       re-encoding the key with the system `openssl` CLI (if present).

    If neither recovery applies and the pasted text has a BEGIN marker
    without its matching END marker (or vice versa), that specific,
    actionable diagnosis -- an incomplete paste -- is reported instead of
    the generic message, since that's indistinguishable from other causes
    otherwise and can't be auto-repaired."""
    pem_text = _normalize_pem_text(pem_text)
    if '-----BEGIN' not in pem_text:
        bare = _try_load_bare_base64_der(pem_text)
        if bare is not None:
            return bare
    try:
        return serialization.load_pem_private_key(pem_text.encode('utf-8'), password=None)
    except (ValueError, TypeError) as e:
        converted = _try_convert_explicit_ec_params(pem_text)
        if converted is not None:
            try:
                return serialization.load_pem_private_key(converted.encode('utf-8'), password=None)
            except Exception:
                pass
        if _pem_looks_truncated(pem_text):
            raise ValueError(
                "The pasted key looks incomplete: it has a -----BEGIN...----- line "
                "but the matching -----END...----- line is missing (or vice versa). "
                "This usually means the copy/paste was cut off partway through. "
                "Re-open the key file and copy its FULL contents, including both the "
                "BEGIN and END lines."
            ) from e
        if 'password' in str(e).lower() or 'encrypted' in str(e).lower():
            raise ValueError(
                "This private key is password-protected, and this app does not accept "
                "a passphrase for import. Remove the passphrase first with your own "
                "OpenSSL -- `openssl ec -in your_key.pem -out decrypted_key.pem` (it "
                "will prompt you for the current passphrase) -- then paste "
                "decrypted_key.pem's contents instead."
            ) from e
        raise ValueError(
            str(e) + " This commonly happens when the key was generated with explicit EC "
            "curve parameters instead of a named curve (e.g. via `openssl ecparam "
            "-param_enc explicit`). Re-encode it with your own OpenSSL first -- "
            "`openssl ec -in your_key.pem -out fixed_key.pem -param_enc named_curve` -- "
            "then paste fixed_key.pem's contents instead. If that's not the issue, "
            "double-check the key isn't password-protected and that you pasted the "
            "private key (not the certificate) into this field."
        ) from e


def _normalize_pem_text(pem_text):
    """Best-effort repair of common copy/paste artifacts before attempting
    to parse a PEM block: a leading byte-order-mark, typographic dashes a
    rich-text editor substituted for the plain ASCII hyphens PEM markers
    require, and literal HTML-entity-encoded newlines from a paste that
    passed through an HTML-escaping step somewhere. Never raises -- an
    input this doesn't recognize is returned unchanged."""
    text = pem_text.lstrip('﻿')
    if '&#10;' in text or '&#13;' in text:
        text = text.replace('&#13;', '\r').replace('&#10;', '\n')
    for dash in ('—', '–', '‒', '‑'):
        text = text.replace(dash, '-')
    return text


def _try_load_bare_base64_der(pem_text):
    """The pasted text has no PEM markers at all -- try treating it as a
    bare base64-encoded DER key (SEC1 `EC PRIVATE KEY` or PKCS8 `PRIVATE
    KEY` layout; `load_der_private_key` auto-detects between them).
    Returns the loaded key, or None if the text isn't valid base64 or
    doesn't decode to a loadable key -- never raises."""
    import re
    candidate = re.sub(r'\s+', '', pem_text)
    if not candidate or not re.fullmatch(r'[A-Za-z0-9+/]+=*', candidate):
        return None
    try:
        der = base64.b64decode(candidate, validate=True)
    except Exception:
        return None
    try:
        return serialization.load_der_private_key(der, password=None)
    except Exception:
        return None


def _pem_looks_truncated(pem_text):
    """True when a BEGIN marker is present without its matching END marker
    (or vice versa) -- i.e. the paste was cut off partway through, as
    opposed to some other kind of malformed/corrupted content."""
    import re
    begins = sorted(re.findall(r'-----BEGIN ([A-Z0-9 ]+)-----', pem_text))
    ends = sorted(re.findall(r'-----END ([A-Z0-9 ]+)-----', pem_text))
    return bool(begins) and begins != ends


def _try_convert_explicit_ec_params(pem_text):
    """Best-effort: shell out to the system `openssl` CLI, if present, to
    re-encode an EC private key's curve parameters from explicit form to a
    named-curve reference, which `cryptography` can then load. Returns the
    converted PEM text, or None if openssl isn't available or the
    conversion itself doesn't succeed -- never raises."""
    import os
    import shutil
    import subprocess
    import tempfile

    openssl_path = shutil.which('openssl')
    if not openssl_path:
        return None

    try:
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, 'in.pem')
            out_path = os.path.join(tmp, 'out.pem')
            with open(in_path, 'w', encoding='utf-8') as f:
                f.write(pem_text)
            result = subprocess.run(
                [openssl_path, 'ec', '-in', in_path, '-out', out_path, '-param_enc', 'named_curve'],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0 or not os.path.exists(out_path):
                return None
            with open(out_path, 'r', encoding='utf-8') as f:
                return f.read()
    except Exception:
        return None


_ZATCA_UID_OID = x509.ObjectIdentifier('0.9.2342.19200300.100.1.1')      # UID (RFC 4519) -- carries the VAT/organization identifier
_ZATCA_REGISTERED_ADDRESS_OID = x509.ObjectIdentifier('2.5.4.26')         # registeredAddress -- carries location
_ZATCA_BUSINESS_CATEGORY_OID = x509.ObjectIdentifier('2.5.4.15')          # businessCategory -- carries industry
_ZATCA_TEMPLATE_NAME_OID = x509.ObjectIdentifier('1.3.6.1.4.1.311.20.2')  # Microsoft "Certificate Template Name", repurposed by ZATCA
_ZATCA_TEMPLATE_NAME_BY_ENV = {
    # (DER tag byte, value) -- per ZATCA's own documentation (confirmed via
    # the Fatoora Developer Community): PrintableString, WITH hyphens.
    # "sandbox" here corresponds to what ZATCA calls "development" --
    # explicitly deprecated in that same documentation, which directs all
    # QA/staging/dev/testing to "simulation" instead. A third-party onboarding
    # tool's own config had this hyphen-free, which turned out to be wrong.
    'sandbox':    (0x13, 'TSTZATCA-Code-Signing'),
    'simulation': (0x13, 'PREZATCA-Code-Signing'),
    'production': (0x13, 'ZATCA-Code-Signing'),
}

import re as _re
_PRINTABLE_STRING_RE = _re.compile(r"^[A-Za-z0-9 '()+,\-./:=?]*$")


def _zatca_name_attribute(oid, value):
    """Every real ZATCA CSR implementation (community packages, NetSuite's
    published config) generates its CSR with OpenSSL's `utf8 = no`, which
    makes OpenSSL encode each DN/SAN string as PrintableString whenever the
    value's characters allow it, falling back to a wider encoding only when
    they don't (e.g. '&' in a company name, '|' in the EGS serial number).
    `cryptography`'s default is UTF8String for nearly every attribute
    regardless of content -- a byte-level mismatch from what ZATCA's own
    validator was built and tested against, and the most likely reason a
    structurally-correct-looking CSR still comes back 'Invalid-CSR'."""
    value = value or ''
    asn1_type = _ASN1Type.PrintableString if _PRINTABLE_STRING_RE.match(value) else _ASN1Type.UTF8String
    return x509.NameAttribute(oid, value, _type=asn1_type)


_ZATCA_CN_ENV_PREFIX = {'sandbox': 'TST', 'simulation': 'PRE', 'production': ''}


def _zatca_common_name(environment, serial_number, organization_identity):
    """The Common Name is NOT the company/solution name -- confirmed by
    decoding a real, ZATCA-issued certificate, whose CN was the structured
    ID "TST-886431145-399999999900003" with no trace of the company name
    anywhere in it. That matches ZATCA's own spec wording for this field
    ("Unique Name OR ASSET TRACKING NUMBER of the Solution Unit"): the
    working convention uses the tracking-number form, not a human name.
    The middle segment's exact meaning is unconfirmed (looks like an
    arbitrary device/asset id, per spec "free text... Manual"), so a
    stable number derived from this settings row's own EGS serial is used
    here rather than inventing an unrelated one on every CSR."""
    digest = hashlib.sha256((serial_number or '').encode('utf-8')).hexdigest()
    tracking_number = str(int(digest[:12], 16))[:9].zfill(9)
    prefix = _ZATCA_CN_ENV_PREFIX.get(environment, 'TST')
    parts = [p for p in (prefix, tracking_number, organization_identity) if p]
    return '-'.join(parts)


def build_csr(private_key, subject, environment='sandbox'):
    """subject: dict with organization_identity (15-digit VAT number),
    organization_unit (branch/location name), organization_name, country,
    serial_number (EGS serial, format "1-<solution name>|2-<version>|3-
    <uuid>"), invoice_type (4-digit capability code e.g. "1100"), location,
    industry. `common_name` is accepted but NOT used -- see
    _zatca_common_name(). Returns the CSR as a PEM string.

    This profile was cross-checked against two independent, real pieces of
    ground truth (not third-party blog guesses, unlike several earlier,
    unsuccessful attempts at this function):
      1. A working third-party ZATCA onboarding tool's actual openssl.cnf,
         which had just obtained a real sandbox Compliance CSID for the
         taxpayer this engine was being tested against. It settled the SAN
         field order (SN, UID, title, registeredAddress, businessCategory
         -- config-file `[alt_names]` sections apply in the literal
         top-to-bottom order written, NOT the reversed order an RFC4514
         string would imply) and confirmed PrintableString + the exact,
         hyphen-free "TSTZATCACodeSigning"-style certificateTemplateName
         value.
      2. A real, ZATCA-issued certificate (decoded directly), which
         corrected two fields that openssl.cnf source got wrong for this
         engine's purposes: Common Name is a structured tracking-number ID
         (see _zatca_common_name), not the free-text company/solution
         name; and Organizational Unit is the branch/location name (this
         sample's was "Riyadh Branch"), not the organization identifier --
         the openssl.cnf apparently used a repurposed OU for its own
         internal reasons, but this issued certificate is proof of what
         ZATCA's own CA actually accepted from a real request.
    Structure:
      - Subject DN: C, OU (branch/location name), O (organization name),
        CN (structured tracking ID) -- in that order, no RFC4514 reversal.
      - A single non-critical subjectAltName `dirName` GeneralName wrapping
        a separate Name: SN (EGS serial -- OpenSSL's "SN" shorthand is the
        "surname" OID 2.5.4.4, not serialNumber 2.5.4.5), UID
        (organization identifier / VAT number, OID
        0.9.2342.19200300.100.1.1), title (invoice type, OID 2.5.4.12),
        registeredAddress (location, OID 2.5.4.26), businessCategory
        (industry, OID 2.5.4.15) -- in that order.
      - A non-critical custom extension at OID 1.3.6.1.4.1.311.20.2
        ("certificateTemplateName"), PrintableString, value depends on
        environment -- see _ZATCA_TEMPLATE_NAME_BY_ENV.
    """
    common_name = _zatca_common_name(environment, subject.get('serial_number'), subject.get('organization_identity'))

    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, subject.get('country') or 'SA'),
        _zatca_name_attribute(NameOID.ORGANIZATIONAL_UNIT_NAME, subject.get('organization_unit')),
        _zatca_name_attribute(NameOID.ORGANIZATION_NAME, subject.get('organization_name')),
        _zatca_name_attribute(NameOID.COMMON_NAME, common_name),
    ])

    alt_name = x509.Name([
        _zatca_name_attribute(NameOID.SURNAME, subject.get('serial_number')),
        _zatca_name_attribute(_ZATCA_UID_OID, subject.get('organization_identity')),
        _zatca_name_attribute(NameOID.TITLE, subject.get('invoice_type') or '1100'),
        _zatca_name_attribute(_ZATCA_REGISTERED_ADDRESS_OID, subject.get('location')),
        _zatca_name_attribute(_ZATCA_BUSINESS_CATEGORY_OID, subject.get('industry')),
    ])

    tag, text = _ZATCA_TEMPLATE_NAME_BY_ENV.get(environment, _ZATCA_TEMPLATE_NAME_BY_ENV['sandbox'])
    template_der = bytes([tag, len(text)]) + text.encode('ascii')

    builder = (x509.CertificateSigningRequestBuilder()
        .subject_name(name)
        .add_extension(x509.SubjectAlternativeName([x509.DirectoryName(alt_name)]), critical=False)
        .add_extension(x509.UnrecognizedExtension(_ZATCA_TEMPLATE_NAME_OID, template_der), critical=False))
    csr = builder.sign(private_key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM).decode('ascii')


def csr_is_valid(csr_pem):
    """Structural round-trip check: parses the CSR back and verifies its own
    signature -- confirms well-formedness only, NOT ZATCA's acceptance of
    its content."""
    csr = x509.load_pem_x509_csr(csr_pem.encode('utf-8'))
    return csr.is_signature_valid


# ─────────────────────────────────────────────────────────────────
# UBL tax-category mapping
# ─────────────────────────────────────────────────────────────────

def ubl_tax_category(tax_display_text, tax_master_row=None):
    """Maps a line item's resolved tax_code display text (e.g. "15%",
    "0%", "Exempt", "Out of Scope" -- see _resolve_tax_code() in
    database/routes/sales.py, which is what actually gets stored on
    SalesInvoiceLineItem.tax_code) to a (category_code, percent,
    exemption_reason) tuple. Prefers an admin-set SalesTaxCode.zatca_category/
    zatca_exemption_reason when the matching master row is supplied,
    falling back to a text heuristic otherwise."""
    if tax_master_row is not None and getattr(tax_master_row, 'zatca_category', None):
        percent = _percent_from_text(tax_display_text)
        return tax_master_row.zatca_category, percent, (tax_master_row.zatca_exemption_reason or None)

    text = (tax_display_text or '').strip().lower()
    percent = _percent_from_text(tax_display_text)
    if percent and percent > 0:
        return 'S', percent, None
    if 'exempt' in text:
        return 'E', 0, None
    if 'out of scope' in text:
        return 'O', 0, None
    # "0%" and anything else defaults to zero-rated.
    return 'Z', 0, None


def _percent_from_text(text):
    import re
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', text or '')
    return float(m.group(1)) if m else 0.0


# ─────────────────────────────────────────────────────────────────
# UBL 2.1 XML generation
# ─────────────────────────────────────────────────────────────────

def _el(tag, ns, text=None, **attrs):
    e = etree.Element('{%s}%s' % (ns, tag) if ns else tag)
    for k, v in attrs.items():
        e.set(k, v)
    if text is not None:
        e.text = str(text)
    return e


def _sub(parent, tag, ns, text=None, **attrs):
    e = _el(tag, ns, text, **attrs)
    parent.append(e)
    return e


def _party_block(tag, party_kind, entity, is_owner):
    """Builds a cac:AccountingSupplierParty/AccountingCustomerParty from
    either an Owner (seller) or a BuyerMaster (customer) ORM object. Reads
    the ORM object directly (never Owner.to_dict(), which omits most
    address fields -- see the implementation plan's own finding).

    Element shape/order -- PartyIdentification (CRN) -> PartyName ->
    PostalAddress -> PartyTaxScheme (VAT) -> PartyLegalEntity -- matches a
    real signed-and-accepted ZATCA invoice sample rather than a guess; the
    CRN belongs in PartyIdentification, not PartyLegalEntity/CompanyID,
    and the party's name is carried in BOTH PartyName and
    PartyLegalEntity/RegistrationName."""
    party_wrap = etree.Element('{%s}%s' % (CAC, tag))
    party = _sub(party_wrap, 'Party', CAC)

    crn = getattr(entity, 'crn', '') or ''
    if crn:
        ident = _sub(party, 'PartyIdentification', CAC)
        _sub(ident, 'ID', CBC, crn, schemeID='CRN')

    name = getattr(entity, 'name', None) or getattr(entity, 'buyer_name_en', '') or ''
    party_name = _sub(party, 'PartyName', CAC)
    _sub(party_name, 'Name', CBC, name)

    postal = _sub(party, 'PostalAddress', CAC)
    _sub(postal, 'StreetName', CBC, getattr(entity, 'street_name', '') or '')
    _sub(postal, 'BuildingNumber', CBC, getattr(entity, 'building_number', '') or '')
    additional = getattr(entity, 'additional_number', '') or ''
    if additional:
        _sub(postal, 'PlotIdentification', CBC, additional)
    district = getattr(entity, 'district', '') or ''
    if district:
        _sub(postal, 'CitySubdivisionName', CBC, district)
    _sub(postal, 'CityName', CBC, getattr(entity, 'city', '') or '')
    _sub(postal, 'PostalZone', CBC, getattr(entity, 'postal_code', '') or '')
    _sub(postal, 'CountrySubentity', CBC, 'Saudi Arabia')
    country = _sub(postal, 'Country', CAC)
    _sub(country, 'IdentificationCode', CBC, 'SA')

    vat_number = getattr(entity, 'vat_number', '') or ''
    if vat_number:
        tax_scheme = _sub(party, 'PartyTaxScheme', CAC)
        _sub(tax_scheme, 'CompanyID', CBC, vat_number)
        scheme = _sub(tax_scheme, 'TaxScheme', CAC)
        _sub(scheme, 'ID', CBC, 'VAT')

    legal_entity = _sub(party, 'PartyLegalEntity', CAC)
    _sub(legal_entity, 'RegistrationName', CBC, name)

    return party_wrap


def _line_item_block(idx, li, tax_master_row=None):
    """Element shape -- a line-level AllowanceCharge (discount), a bare
    TaxTotal carrying only TaxAmount + RoundingAmount (the per-category
    breakdown lives under Item/ClassifiedTaxCategory instead, not a
    TaxSubtotal here), and BaseQuantity under Price -- matches a real
    signed-and-accepted ZATCA invoice sample rather than the earlier,
    sparser reconstruction. RoundingAmount's exact intended meaning isn't
    independently confirmed; using this line's own rounded total (taxable
    + tax) is the most defensible reading of "the line's final rounded
    amount" and matches the reference for its one line item."""
    category, percent, exemption_reason = ubl_tax_category(li.tax_code, tax_master_row)
    line = etree.Element('{%s}InvoiceLine' % CAC)
    _sub(line, 'ID', CBC, str(idx))
    _sub(line, 'InvoicedQuantity', CBC, f'{float(li.quantity or 0):.4f}', unitCode=li.uom or 'PCE')
    _sub(line, 'LineExtensionAmount', CBC, f'{float(li.taxable or 0):.2f}', currencyID='SAR')

    allowance = _sub(line, 'AllowanceCharge', CAC)
    _sub(allowance, 'ChargeIndicator', CBC, 'false')
    _sub(allowance, 'AllowanceChargeReasonCode', CBC, '95')
    _sub(allowance, 'AllowanceChargeReason', CBC, 'Discount')
    _sub(allowance, 'Amount', CBC, f'{float(li.discount or 0):.2f}', currencyID='SAR')

    tax_total = _sub(line, 'TaxTotal', CAC)
    _sub(tax_total, 'TaxAmount', CBC, f'{float(li.tax_amount or 0):.2f}', currencyID='SAR')
    _sub(tax_total, 'RoundingAmount', CBC, f'{float(li.total or 0):.2f}', currencyID='SAR')

    item = _sub(line, 'Item', CAC)
    _sub(item, 'Name', CBC, li.description or li.item_code or '')
    cat = _sub(item, 'ClassifiedTaxCategory', CAC)
    _sub(cat, 'ID', CBC, category)
    _sub(cat, 'Percent', CBC, f'{percent:.2f}')
    if exemption_reason:
        _sub(cat, 'TaxExemptionReasonCode', CBC, exemption_reason)
    scheme = _sub(cat, 'TaxScheme', CAC)
    _sub(scheme, 'ID', CBC, 'VAT')

    price = _sub(line, 'Price', CAC)
    _sub(price, 'PriceAmount', CBC, f'{float(li.rate or 0):.4f}', currencyID='SAR')
    _sub(price, 'BaseQuantity', CBC, '1.0000')
    return line


def build_invoice_xml(doc, items, owner, buyer, settings, uuid_str, icv, pih, referenced_uuid=None):
    """Builds the full UBL 2.1 document (unsigned -- no XAdES ds:Signature
    populated yet, that's sign_and_finalize()'s job; the QR
    AdditionalDocumentReference is left as an empty placeholder for
    set_qr_reference() to fill in afterward, since the QR's tags 6-7 need
    the signature this document doesn't have until it's been signed).
    Returns (root_element, submission_type) -- 'clearance' or 'reporting',
    derived once here from transaction_type's Standard/Simplified prefix
    (the same value embedded in the XML's own cbc:ProfileID), so the
    caller persists and acts on the exact same value the XML itself
    declares rather than re-deriving it a second time from a different
    field that could in principle drift out of sync.

    `doc` is a SalesInvoice ORM instance, `items` its SalesInvoiceLineItem
    rows, `owner`/`buyer` the seller/customer ORM objects, `settings` the
    ZatcaSettings singleton (used only for its ICV/PIH context, not
    mutated here), `referenced_uuid` the prior invoice's UUID for a
    CreditNote/DebitNote's BillingReference.

    Element shape/order (ProfileID, LineCountNumeric, the QR reference
    placeholder, cac:Signature, Delivery, PaymentMeans, a document-level
    AllowanceCharge, two TaxTotal blocks -- a bare summary one and a
    detailed one with one TaxSubtotal per distinct tax category present --
    and a full 6-field LegalMonetaryTotal) matches a real signed-and-
    accepted ZATCA invoice sample rather than the earlier, sparser
    reconstruction. Header totals are read from `doc`'s own already-saved,
    already-reconciled columns (see _save_doc_line_items() in sales.py)
    rather than re-summed independently here, so these figures can never
    drift from what the printed invoice and the line items below agree on.

    Raises ValueError if `doc.transaction_type` isn't a recognized code, or
    (for CR/DR) if `referenced_uuid` wasn't supplied.
    """
    txn = (doc.transaction_type or '').strip()
    if txn not in TXN_TYPE_MAP:
        raise ValueError(f'Unrecognized transaction_type: {txn!r}')
    root_tag, type_code_tag, type_code, subtype = TXN_TYPE_MAP[txn]
    # Standard invoices go through Clearance (real-time, before the buyer
    # ever sees it); Simplified ones go through Reporting (within 24 hours
    # after issuance) -- confirmed against ZATCA's own published XML
    # Implementation Standard.
    submission_type = 'clearance' if subtype == 'standard' else 'reporting'

    if root_tag in ('CreditNote', 'DebitNote') and not referenced_uuid:
        raise ValueError('A Credit/Debit Note must reference a prior invoice with its own zatca_uuid')

    nsmap = {None: ROOT_NS[root_tag], 'cac': CAC, 'cbc': CBC, 'ext': EXT}
    root = etree.Element('{%s}%s' % (ROOT_NS[root_tag], root_tag), nsmap=nsmap)

    # Extensions placeholder -- populated by sign_and_finalize().
    etree.SubElement(root, '{%s}UBLExtensions' % EXT)

    _sub(root, 'ProfileID', CBC, f'{submission_type}:1.0')
    _sub(root, 'ID', CBC, doc.doc_no or '')
    _sub(root, 'UUID', CBC, uuid_str)
    issue_date = doc.document_date or doc.posting_date or datetime.now(timezone.utc).date()
    _sub(root, 'IssueDate', CBC, issue_date.isoformat())
    _sub(root, 'IssueTime', CBC, datetime.now(timezone.utc).strftime('%H:%M:%S'))
    _sub(root, type_code_tag, CBC, type_code, name=INVOICE_NAME_SUBTYPE[subtype])
    _sub(root, 'DocumentCurrencyCode', CBC, 'SAR')
    _sub(root, 'TaxCurrencyCode', CBC, 'SAR')
    _sub(root, 'LineCountNumeric', CBC, str(len(items)))

    if referenced_uuid:
        billing_ref = _sub(root, 'BillingReference', CAC)
        inv_doc_ref = _sub(billing_ref, 'InvoiceDocumentReference', CAC)
        _sub(inv_doc_ref, 'ID', CBC, referenced_uuid)

    icv_adr = _sub(root, 'AdditionalDocumentReference', CAC)
    _sub(icv_adr, 'ID', CBC, 'ICV')
    _sub(icv_adr, 'UUID', CBC, str(icv))

    pih_adr = _sub(root, 'AdditionalDocumentReference', CAC)
    _sub(pih_adr, 'ID', CBC, 'PIH')
    pih_attach = _sub(pih_adr, 'Attachment', CAC)
    _sub(pih_attach, 'EmbeddedDocumentBinaryObject', CBC, pih, mimeCode='text/plain')

    # Placeholder -- set_qr_reference() fills this in once the invoice hash
    # and signature (the QR's tags 6-7) exist, after sign_and_finalize().
    # Deliberately excluded from what gets signed (see canonicalize()), so
    # leaving it empty here has no effect on the signature.
    qr_adr = _sub(root, 'AdditionalDocumentReference', CAC)
    _sub(qr_adr, 'ID', CBC, 'QR')
    qr_attach = _sub(qr_adr, 'Attachment', CAC)
    _sub(qr_attach, 'EmbeddedDocumentBinaryObject', CBC, '', mimeCode='text/plain')

    sig_ref = _sub(root, 'Signature', CAC)
    _sub(sig_ref, 'ID', CBC, 'urn:oasis:names:specification:ubl:signature:Invoice')
    _sub(sig_ref, 'SignatureMethod', CBC, 'urn:oasis:names:specification:ubl:dsig:enveloped:xades')

    root.append(_party_block('AccountingSupplierParty', 'supplier', owner, True))
    if buyer is not None:
        root.append(_party_block('AccountingCustomerParty', 'customer', buyer, False))
    else:
        # Simplified (B2C) invoices tolerate an anonymous buyer -- an empty
        # customer party is valid per ZATCA's Simplified Tax Invoice rules.
        root.append(_party_block('AccountingCustomerParty', 'customer', _EmptyParty(), False))

    delivery_date = getattr(doc, 'delivery_date', None) or getattr(doc, 'posting_date', None) or issue_date
    delivery = _sub(root, 'Delivery', CAC)
    _sub(delivery, 'ActualDeliveryDate', CBC, delivery_date.isoformat())

    payment_means = _sub(root, 'PaymentMeans', CAC)
    _sub(payment_means, 'PaymentMeansCode', CBC,
         _PAYMENT_MEANS_CODE.get((doc.payment_method or '').strip(), '30'))
    _sub(payment_means, 'InstructionNote', CBC, doc.payment_method or 'Credit')

    # Header-level totals come from the invoice's own already-saved,
    # already-reconciled columns -- see this function's own docstring.
    total_taxable = Decimal(str(doc.total_excl_vat or 0))
    total_tax = Decimal(str(doc.vat_amount or 0))
    total_incl = Decimal(str(doc.total_incl_vat or 0))
    total_discount = Decimal(str(getattr(doc, 'total_discount', 0) or 0))

    # One TaxSubtotal per distinct (category, percent, exemption) actually
    # present on the line items -- a single hardcoded "15% Standard" would
    # be wrong for an invoice with exempt/zero-rated/mixed-rate lines.
    tax_groups = {}
    for li in items:
        category, percent, exemption_reason = ubl_tax_category(li.tax_code, None)
        key = (category, percent, exemption_reason)
        g = tax_groups.setdefault(key, {'taxable': Decimal('0.00'), 'tax': Decimal('0.00')})
        g['taxable'] += Decimal(str(li.taxable or 0))
        g['tax'] += Decimal(str(li.tax_amount or 0))
    if not tax_groups:
        tax_groups[('S', 15.0, None)] = {'taxable': Decimal('0.00'), 'tax': Decimal('0.00')}
    # The document-level discount's own TaxCategory can only carry one
    # rate; for the (overwhelmingly common) single-rate invoice this is
    # exact, and for a genuinely mixed-rate invoice this is a deliberate,
    # documented simplification -- the discount is attributed to whichever
    # tax category appears first.
    primary_category, primary_percent, _primary_exempt = next(iter(tax_groups))

    allowance = _sub(root, 'AllowanceCharge', CAC)
    _sub(allowance, 'ChargeIndicator', CBC, 'false')
    _sub(allowance, 'AllowanceChargeReason', CBC, 'discount')
    _sub(allowance, 'Amount', CBC, f'{total_discount:.2f}', currencyID='SAR')
    disc_cat = _sub(allowance, 'TaxCategory', CAC)
    _sub(disc_cat, 'ID', CBC, primary_category, schemeID='UN/ECE 5305', schemeAgencyID='6')
    _sub(disc_cat, 'Percent', CBC, f'{primary_percent:.2f}')
    disc_scheme = _sub(disc_cat, 'TaxScheme', CAC)
    _sub(disc_scheme, 'ID', CBC, 'VAT', schemeID='UN/ECE 5153', schemeAgencyID='6')

    tax_total_summary = _sub(root, 'TaxTotal', CAC)
    _sub(tax_total_summary, 'TaxAmount', CBC, f'{total_tax:.2f}', currencyID='SAR')

    tax_total_detail = _sub(root, 'TaxTotal', CAC)
    _sub(tax_total_detail, 'TaxAmount', CBC, f'{total_tax:.2f}', currencyID='SAR')
    for (category, percent, exemption_reason), amounts in tax_groups.items():
        subtotal = _sub(tax_total_detail, 'TaxSubtotal', CAC)
        _sub(subtotal, 'TaxableAmount', CBC, f'{amounts["taxable"]:.2f}', currencyID='SAR')
        _sub(subtotal, 'TaxAmount', CBC, f'{amounts["tax"]:.2f}', currencyID='SAR')
        cat_el = _sub(subtotal, 'TaxCategory', CAC)
        _sub(cat_el, 'ID', CBC, category, schemeID='UN/ECE 5305', schemeAgencyID='6')
        _sub(cat_el, 'Percent', CBC, f'{percent:.2f}')
        if exemption_reason:
            _sub(cat_el, 'TaxExemptionReasonCode', CBC, exemption_reason)
        scheme_el = _sub(cat_el, 'TaxScheme', CAC)
        _sub(scheme_el, 'ID', CBC, 'VAT', schemeID='UN/ECE 5153', schemeAgencyID='6')

    monetary = _sub(root, 'LegalMonetaryTotal', CAC)
    _sub(monetary, 'LineExtensionAmount', CBC, f'{total_taxable:.2f}', currencyID='SAR')
    _sub(monetary, 'TaxExclusiveAmount', CBC, f'{total_taxable:.2f}', currencyID='SAR')
    _sub(monetary, 'TaxInclusiveAmount', CBC, f'{total_incl:.2f}', currencyID='SAR')
    _sub(monetary, 'AllowanceTotalAmount', CBC, f'{total_discount:.2f}', currencyID='SAR')
    _sub(monetary, 'PrepaidAmount', CBC, '0.00', currencyID='SAR')
    _sub(monetary, 'PayableAmount', CBC, f'{total_incl:.2f}', currencyID='SAR')

    for idx, li in enumerate(items, start=1):
        root.append(_line_item_block(idx, li))

    return root, submission_type


# UNTDID 4461 payment means codes for this app's payment_method values;
# defaults to 30 (credit transfer) for anything unrecognized.
_PAYMENT_MEANS_CODE = {
    'Credit': '30', 'Cash': '10', 'Bank Transfer': '30', 'Cheque': '20', 'Card': '48',
}


def set_qr_reference(xml_root, qr_base64):
    """Fill in the QR AdditionalDocumentReference placeholder
    build_invoice_xml() left empty, now that the invoice hash/signature
    (the QR's tags 6-7) are known -- must be called after
    sign_and_finalize(), with the SAME qr_base64 used for the printed QR
    (see phase2_qr_extra_tags()), so the embedded XML QR and the printed
    one are always identical."""
    for adr in xml_root.findall('{%s}AdditionalDocumentReference' % CAC):
        id_el = adr.find('{%s}ID' % CBC)
        if id_el is not None and id_el.text == 'QR':
            embed = adr.find('.//{%s}EmbeddedDocumentBinaryObject' % CBC)
            embed.text = qr_base64
            return
    raise ValueError('QR AdditionalDocumentReference placeholder not found -- '
                      'was this document built with build_invoice_xml()?')


class _EmptyParty:
    """Placeholder customer for a Simplified (B2C) invoice with no real
    buyer record -- every attribute _party_block() reads resolves to ''."""
    def __getattr__(self, name):
        return ''


# ─────────────────────────────────────────────────────────────────
# Canonicalization + signing
# ─────────────────────────────────────────────────────────────────

def canonicalize(xml_root):
    """Returns the canonical bytes of the invoice's business-data view --
    ext:UBLExtensions, cac:Signature, and the QR AdditionalDocumentReference
    all stripped -- that get hashed and signed. Matches the exact three
    exclusions a real signed ZATCA invoice's own XPath transforms declare
    (`not(//ancestor-or-self::ext:UBLExtensions)`, `...cac:Signature`,
    `...cac:AdditionalDocumentReference[cbc:ID='QR']`); the ICV and PIH
    AdditionalDocumentReferences are NOT excluded and remain part of what's
    signed.

    Uses plain (non-exclusive) C14N. The reference's own
    CanonicalizationMethod Algorithm is `xml-c14n11` (Canonical XML 1.1),
    which lxml doesn't implement as a distinct method -- for a document
    with no xml:base/xml:id attributes (true of every invoice this engine
    generates), non-exclusive C14N 1.0 produces byte-identical output to
    C14N 1.1, so this is a safe practical stand-in, not a guess at a
    different algorithm. NOT INDEPENDENTLY VERIFIED against ZATCA's own
    compliance-check tool; isolated here as a single named function
    specifically so it can be swapped without touching any other code if
    that ever needs correcting.
    """
    working = etree.fromstring(etree.tostring(xml_root))
    for ext in working.findall('{%s}UBLExtensions' % EXT):
        working.remove(ext)
    for sig in working.findall('{%s}Signature' % CAC):
        working.remove(sig)
    for adr in working.findall('{%s}AdditionalDocumentReference' % CAC):
        id_el = adr.find('{%s}ID' % CBC)
        if id_el is not None and id_el.text == 'QR':
            working.remove(adr)
    return etree.tostring(working, method='c14n', exclusive=False, with_comments=False)


# XML-DSig / XAdES namespaces -- not part of the base UBL document, only
# used inside the signature extension sign_and_finalize() builds.
_NS_DS = 'http://www.w3.org/2000/09/xmldsig#'
_NS_XADES = 'http://uri.etsi.org/01903/v1.3.2#'
_NS_SIG = 'urn:oasis:names:specification:ubl:schema:xsd:CommonSignatureComponents-2'
_NS_SAC = 'urn:oasis:names:specification:ubl:schema:xsd:SignatureAggregateComponents-2'
_NS_SBC = 'urn:oasis:names:specification:ubl:schema:xsd:SignatureBasicComponents-2'

_DN_OID_SHORT_NAMES = {
    NameOID.COMMON_NAME: 'CN', NameOID.ORGANIZATIONAL_UNIT_NAME: 'OU',
    NameOID.ORGANIZATION_NAME: 'O', NameOID.LOCALITY_NAME: 'L',
    NameOID.STATE_OR_PROVINCE_NAME: 'ST', NameOID.COUNTRY_NAME: 'C',
    NameOID.DOMAIN_COMPONENT: 'DC',
}


def _x509_name_oneline(name):
    """Renders an x509.Name the way a real signed ZATCA invoice sample
    shows it (most-specific attribute first, joined by ", ") rather than
    cryptography's own rfc4514_string() (reversed order, no spaces, LDAP
    convention). Best-effort cosmetic match -- ZATCA's actual string-
    comparison tolerance for this field is not independently verified."""
    parts = []
    for attr in name:
        short = _DN_OID_SHORT_NAMES.get(attr.oid) or attr.oid.dotted_string
        parts.append(f'{short}={attr.value}')
    return ', '.join(parts)


def _hex_then_b64(raw_bytes):
    """SHA-256 of `raw_bytes`, hex-encoded, THEN base64-encoded. Confirmed
    against a real signed ZATCA invoice sample: unlike ordinary XMLDSig/
    XAdES (which base64-encodes the raw digest bytes directly), ZATCA's
    own ecosystem base64-encodes the digest's HEX STRING representation
    instead for the XAdES CertDigest and SignedProperties reference digest
    -- but NOT for the main invoice data digest, which is the standard
    raw-bytes encoding (see compute_invoice_hash())."""
    digest_hex = hashlib.sha256(raw_bytes).hexdigest()
    return base64.b64encode(digest_hex.encode('ascii')).decode('ascii')


def sign_and_finalize(xml_root, private_key, cert_token):
    """Computes the invoice hash, ECDSA-signs it, and embeds a full
    XAdES-enveloped ds:Signature into the previously-empty
    ext:UBLExtensions block -- reconstructed from a real signed-and-
    accepted ZATCA invoice sample's element shape, order, namespaces, and
    algorithm URIs, replacing an earlier made-up, non-standard
    ext:ZatcaSignatureInfo shape that would not have validated.
    `cert_token` is the raw certificate value on file (single- or double-
    base64-encoded, or PEM -- see load_certificate()), not necessarily a
    clean PEM string.

    NOT INDEPENDENTLY VERIFIED against ZATCA's own compliance-check tool
    for the exact canonicalization of the xades:SignedProperties fragment
    (built as a standalone tree and canonicalized with Exclusive C14N, the
    common convention for a self-contained XAdES fragment referenced by
    URI -- ZATCA's own tool doesn't declare this explicitly anywhere
    visible in a signed sample, since a Reference without its own
    ds:Transforms child gives no direct evidence either way). Everything
    else here -- structure, algorithm URIs, and the hex-then-base64 digest
    quirk -- is a direct, verified match against a real accepted sample.
    Returns (finalized_root, invoice_hash_b64, signature_b64).
    """
    cert = load_certificate(cert_token)
    if cert is None:
        raise ValueError('The signing certificate on file could not be parsed.')
    cert_der = cert.public_bytes(encoding=serialization.Encoding.DER)
    cert_b64 = base64.b64encode(cert_der).decode('ascii')

    invoice_canonical = canonicalize(xml_root)
    invoice_hash = compute_invoice_hash(invoice_canonical)

    signature = private_key.sign(
        hashlib.sha256(invoice_canonical).digest(),
        ec.ECDSA(asym_utils.Prehashed(hashes.SHA256())),
    )
    signature_b64 = base64.b64encode(signature).decode('ascii')

    signing_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    issuer_name = _x509_name_oneline(cert.issuer)
    serial_number = str(cert.serial_number)
    cert_digest_b64 = _hex_then_b64(cert_der)

    # xades:SignedProperties is built standalone first so it can be
    # canonicalized on its own, independent of where it ends up nested.
    signed_props = etree.Element('{%s}SignedProperties' % _NS_XADES,
                                  nsmap={'xades': _NS_XADES, 'ds': _NS_DS})
    signed_props.set('Id', 'xadesSignedProperties')
    sig_props = _sub(signed_props, 'SignedSignatureProperties', _NS_XADES)
    _sub(sig_props, 'SigningTime', _NS_XADES, signing_time)
    signing_cert = _sub(sig_props, 'SigningCertificate', _NS_XADES)
    cert_el = _sub(signing_cert, 'Cert', _NS_XADES)
    cert_digest_el = _sub(cert_el, 'CertDigest', _NS_XADES)
    dm_cert = _sub(cert_digest_el, 'DigestMethod', _NS_DS)
    dm_cert.set('Algorithm', 'http://www.w3.org/2001/04/xmlenc#sha256')
    _sub(cert_digest_el, 'DigestValue', _NS_DS, cert_digest_b64)
    issuer_serial = _sub(cert_el, 'IssuerSerial', _NS_XADES)
    _sub(issuer_serial, 'X509IssuerName', _NS_DS, issuer_name)
    _sub(issuer_serial, 'X509SerialNumber', _NS_DS, serial_number)

    signed_props_canonical = etree.tostring(signed_props, method='c14n', exclusive=True, with_comments=False)
    signed_props_digest_b64 = _hex_then_b64(signed_props_canonical)

    ext_container = xml_root.find('{%s}UBLExtensions' % EXT)
    ext_container.clear()
    extension = _sub(ext_container, 'UBLExtension', EXT)
    _sub(extension, 'ExtensionURI', EXT, 'urn:oasis:names:specification:ubl:dsig:enveloped:xades')
    content = _sub(extension, 'ExtensionContent', EXT)

    ubl_doc_sigs = etree.SubElement(content, '{%s}UBLDocumentSignatures' % _NS_SIG,
                                     nsmap={'sig': _NS_SIG, 'sac': _NS_SAC, 'sbc': _NS_SBC})
    sig_info = _sub(ubl_doc_sigs, 'SignatureInformation', _NS_SAC)
    _sub(sig_info, 'ID', CBC, 'urn:oasis:names:specification:ubl:signature:1')
    _sub(sig_info, 'ReferencedSignatureID', _NS_SBC, 'urn:oasis:names:specification:ubl:signature:Invoice')

    ds_sig = etree.SubElement(sig_info, '{%s}Signature' % _NS_DS, nsmap={'ds': _NS_DS})
    ds_sig.set('Id', 'signature')
    signed_info = etree.SubElement(ds_sig, '{%s}SignedInfo' % _NS_DS)
    c14n_method = etree.SubElement(signed_info, '{%s}CanonicalizationMethod' % _NS_DS)
    c14n_method.set('Algorithm', 'http://www.w3.org/2006/12/xml-c14n11')
    sig_method = etree.SubElement(signed_info, '{%s}SignatureMethod' % _NS_DS)
    sig_method.set('Algorithm', 'http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256')

    ref1 = etree.SubElement(signed_info, '{%s}Reference' % _NS_DS)
    ref1.set('Id', 'invoiceSignedData')
    ref1.set('URI', '')
    transforms = etree.SubElement(ref1, '{%s}Transforms' % _NS_DS)
    for xpath_expr in (
        'not(//ancestor-or-self::ext:UBLExtensions)',
        'not(//ancestor-or-self::cac:Signature)',
        "not(//ancestor-or-self::cac:AdditionalDocumentReference[cbc:ID='QR'])",
    ):
        t = etree.SubElement(transforms, '{%s}Transform' % _NS_DS)
        t.set('Algorithm', 'http://www.w3.org/TR/1999/REC-xpath-19991116')
        xp = etree.SubElement(t, '{%s}XPath' % _NS_DS, nsmap={'ext': EXT, 'cac': CAC, 'cbc': CBC})
        xp.text = xpath_expr
    t_c14n = etree.SubElement(transforms, '{%s}Transform' % _NS_DS)
    t_c14n.set('Algorithm', 'http://www.w3.org/2006/12/xml-c14n11')
    dm1 = etree.SubElement(ref1, '{%s}DigestMethod' % _NS_DS)
    dm1.set('Algorithm', 'http://www.w3.org/2001/04/xmlenc#sha256')
    _sub(ref1, 'DigestValue', _NS_DS, invoice_hash)

    ref2 = etree.SubElement(signed_info, '{%s}Reference' % _NS_DS)
    ref2.set('Type', 'http://www.w3.org/2000/09/xmldsig#SignatureProperties')
    ref2.set('URI', '#xadesSignedProperties')
    dm2 = etree.SubElement(ref2, '{%s}DigestMethod' % _NS_DS)
    dm2.set('Algorithm', 'http://www.w3.org/2001/04/xmlenc#sha256')
    _sub(ref2, 'DigestValue', _NS_DS, signed_props_digest_b64)

    _sub(ds_sig, 'SignatureValue', _NS_DS, signature_b64)

    key_info = _sub(ds_sig, 'KeyInfo', _NS_DS)
    x509_data = _sub(key_info, 'X509Data', _NS_DS)
    _sub(x509_data, 'X509Certificate', _NS_DS, cert_b64)

    obj = _sub(ds_sig, 'Object', _NS_DS)
    qualifying = etree.SubElement(obj, '{%s}QualifyingProperties' % _NS_XADES, nsmap={'xades': _NS_XADES})
    qualifying.set('Target', 'signature')
    qualifying.append(signed_props)

    return xml_root, invoice_hash, signature_b64


def serialize(xml_root, pretty=False):
    """Never call with pretty=True on the tree that was/will be hashed --
    pretty-printing changes the byte stream. Use a re-parsed copy for
    human display only."""
    return etree.tostring(xml_root, xml_declaration=True, encoding='UTF-8', pretty_print=pretty)


# ─────────────────────────────────────────────────────────────────
# QR (Phase 2: 9 tags)
# ─────────────────────────────────────────────────────────────────

def phase2_qr_extra_tags(invoice_hash_b64, signature_b64, public_key_b64, cert_ca_signature_b64):
    """Tags 6-9, in order, as RAW DECODED BYTES (not base64 text) -- the
    TLV encoder in database/routes/purchase.py base64-decodes each of
    these before writing them, mirroring tags 1-5's plain-text encoding.
    A common real bug is double-base64-wrapping these; every value here is
    decoded exactly once before being handed to the TLV builder."""
    return [
        (6, base64.b64decode(invoice_hash_b64)),
        (7, base64.b64decode(signature_b64)),
        (8, base64.b64decode(public_key_b64)),
        (9, base64.b64decode(cert_ca_signature_b64) if cert_ca_signature_b64 else b''),
    ]


# ─────────────────────────────────────────────────────────────────
# Live ZATCA API calls -- written to spec, gated by ZATCA_LIVE_CALLS_ENABLED
# at the call site (see database/routes/zatca.py / sales.py). NOT verified
# against a real ZATCA endpoint.
# ─────────────────────────────────────────────────────────────────

ENV_BASE_URLS = {
    # NOTE (unverified): re-check these against ZATCA's current developer
    # portal before any live call -- base URLs have changed before.
    'sandbox':    'https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal',
    'simulation': 'https://gw-fatoora.zatca.gov.sa/e-invoicing/simulation',
    'production': 'https://gw-fatoora.zatca.gov.sa/e-invoicing/core',
}


def request_compliance_csid(environment, csr_pem, otp):
    import requests
    url = ENV_BASE_URLS.get(environment, ENV_BASE_URLS['sandbox']) + '/compliance'
    # ZATCA's "csr" field is base64 of the CSR's raw DER bytes, NOT base64
    # of the PEM text -- a PEM already contains its own inner base64 body
    # wrapped in BEGIN/END markers, so base64-encoding the whole PEM string
    # produces a value that decodes back to PEM text (not binary DER) on
    # ZATCA's side, which a DER parser rejects outright. This -- not the
    # CSR's actual field content, which has been verified byte-for-byte
    # against a real issued ZATCA certificate -- is the likeliest reason
    # every CSR this engine built kept coming back "Invalid-CSR" no matter
    # how its Subject/SAN fields changed: the wrapping was broken, so
    # ZATCA may never have been able to parse the CSR at all.
    csr_der = x509.load_pem_x509_csr(csr_pem.encode('utf-8')).public_bytes(serialization.Encoding.DER)
    # ZATCA's own sample requests for this endpoint always send Accept and
    # Content-Type explicitly alongside OTP/Accept-Version -- a bare "400
    # Invalid Request" with no field-level detail at all (unlike ZATCA's
    # usual specific error bodies, e.g. for a malformed CSR) looks like a
    # gateway-level rejection before the request ever reaches ZATCA's own
    # validation, which missing/implicit headers can cause even though
    # `requests` already sets Content-Type on its own for `json=`.
    resp = requests.post(url, json={'csr': base64.b64encode(csr_der).decode('ascii')},
                          headers={'OTP': otp, 'Accept-Version': 'V2',
                                   'Accept': 'application/json',
                                   'Content-Type': 'application/json'}, timeout=30)
    if not resp.ok:
        # ZATCA's error responses carry a specific, actionable body (e.g.
        # which field/validation failed) -- raise_for_status() alone
        # discards that, leaving only an opaque "400 Bad Request" with
        # nothing to diagnose from.
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise RuntimeError(f'ZATCA returned {resp.status_code}: {detail}')
    return resp.json()


def _auth_headers(binary_token, secret):
    token = base64.b64encode(f'{binary_token}:{secret}'.encode('utf-8')).decode('ascii')
    return {'Authorization': f'Basic {token}', 'Accept-Version': 'V2', 'Content-Type': 'application/json'}


def submit_clearance(environment, xml_bytes, uuid_str, binary_token, secret):
    import requests
    url = ENV_BASE_URLS.get(environment, ENV_BASE_URLS['sandbox']) + '/invoices/clearance/single'
    payload = {
        'invoiceHash': compute_invoice_hash(xml_bytes),
        'uuid': uuid_str,
        'invoice': base64.b64encode(xml_bytes).decode('ascii'),
    }
    resp = requests.post(url, json=payload, headers=_auth_headers(binary_token, secret), timeout=30)
    return resp.status_code, resp.json() if resp.content else {}


def submit_reporting(environment, xml_bytes, uuid_str, binary_token, secret):
    import requests
    url = ENV_BASE_URLS.get(environment, ENV_BASE_URLS['sandbox']) + '/invoices/reporting/single'
    payload = {
        'invoiceHash': compute_invoice_hash(xml_bytes),
        'uuid': uuid_str,
        'invoice': base64.b64encode(xml_bytes).decode('ascii'),
    }
    resp = requests.post(url, json=payload, headers=_auth_headers(binary_token, secret), timeout=30)
    return resp.status_code, resp.json() if resp.content else {}
