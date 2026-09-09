"""
seller_doc.py
─────────────────────────────────────────────────────────────────────
Blueprint: seller_doc_bp
Prefix:    (none — URLs are defined explicitly on each route)

All routes related to OwnerDocument:
  Upload, View inline, Download, Delete, List JSON per owner,
  Standalone AG-Grid page, AG-Grid data feed, Edit metadata.

FILE PATH in your project:
  seller_ms/
  └── seller_doc.py          ← this file
       (same level as sellers.py, models.py, app.py)

Register in app.py (or __init__.py):
  from seller_doc import seller_doc_bp
  app.register_blueprint(seller_doc_bp)
─────────────────────────────────────────────────────────────────────
"""

import os
import uuid
import mimetypes
from datetime import datetime, date as _date
from functools import wraps

from flask import (
    Blueprint, render_template, redirect, url_for, flash,
    request, current_app, send_from_directory, jsonify, session,
    Response,
)
from flask_login import login_required, current_user

from models import db, Owner, OwnerDocument, ActivityLog, User

seller_doc_bp = Blueprint('seller_doc', __name__)


# ─────────────────────────────────────────────────────────────────────
# MIME TYPE AND EXTENSION CONFIGURATION
# ─────────────────────────────────────────────────────────────────────

# Ensure proper MIME type detection for Office documents
mimetypes.add_type('application/msword', '.doc')
mimetypes.add_type('application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx')
mimetypes.add_type('application/vnd.ms-excel', '.xls')
mimetypes.add_type('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx')
mimetypes.add_type('application/vnd.ms-powerpoint', '.ppt')
mimetypes.add_type('application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx')

# Allowed MIME types for upload
ALLOWED_MIMETYPES = {
    # PDF
    'application/pdf',
    
    # Images
    'image/jpeg',
    'image/png',
    'image/gif',
    'image/bmp',
    'image/webp',
    'image/tiff',
    
    # Word Documents
    'application/msword',  # .doc
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',  # .docx
    
    # Excel Documents
    'application/vnd.ms-excel',  # .xls
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',  # .xlsx
    
    # PowerPoint
    'application/vnd.ms-powerpoint',  # .ppt
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',  # .pptx
    
    # Text files
    'text/plain',
    'text/csv',
    
    # Rich Text
    'application/rtf',
    
    # Archives
    'application/zip',
    'application/x-rar-compressed',
    'application/x-7z-compressed',
}

# Allowed extensions (fallback when MIME type is unknown)
ALLOWED_EXTENSIONS = {
    # Images
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tiff', 'tif',
    # Documents
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    # Text
    'txt', 'csv', 'rtf',
    # Archives
    'zip', 'rar', '7z',
}

# MIME types that can be displayed inline in browser
INLINE_MIMETYPES = {
    'application/pdf',
    'image/jpeg',
    'image/png',
    'image/gif',
    'image/bmp',
    'image/webp',
    'image/tiff',
    'text/plain',
    'text/csv',
}


# ─────────────────────────────────────────────────────────────────────
# PRIVATE HELPERS
# ─────────────────────────────────────────────────────────────────────

def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def _admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash(_t('Access denied. Admin privileges required.',
                     'الوصول مرفوض. يلزم صلاحيات المدير.'), 'danger')
            return redirect(url_for('sellers.list_owners'))
        return f(*args, **kwargs)
    return decorated


def _allowed_file(filename):
    """
    Check if the file is allowed based on:
    1. MIME type (most reliable)
    2. File extension (fallback)
    3. Common binary MIME types with known extensions
    """
    if not filename or '.' not in filename:
        return False
    
    # Get file extension
    ext = filename.rsplit('.', 1)[1].lower()
    
    # Get MIME type from filename
    mime_type, _ = mimetypes.guess_type(filename)
    
    print(f"DEBUG _allowed_file: filename='{filename}', ext='{ext}', mime_type='{mime_type}'")
    
    # Check if MIME type is in allowed list
    if mime_type and mime_type in ALLOWED_MIMETYPES:
        print(f"DEBUG: MIME type '{mime_type}' is allowed")
        return True
    
    # If MIME type is unknown, check extension
    if ext in ALLOWED_EXTENSIONS:
        print(f"DEBUG: Extension '{ext}' is allowed (MIME fallback)")
        return True
    
    # Handle generic binary MIME types (application/octet-stream, etc.)
    # If the extension is known, allow it
    generic_mimes = {
        'application/octet-stream',
        'application/x-msdownload',
        'binary/octet-stream',
    }
    if mime_type in generic_mimes and ext in ALLOWED_EXTENSIONS:
        print(f"DEBUG: Generic MIME '{mime_type}' with known extension '{ext}' - allowed")
        return True
    
    print(f"DEBUG: File type NOT allowed - ext='{ext}', mime='{mime_type}'")
    return False


def _allowed_file_size(file_storage, max_size_mb=16):
    """
    Check if file size is within allowed limit.
    Returns (is_allowed: bool, size_in_bytes: int)
    """
    max_size_bytes = max_size_mb * 1024 * 1024
    
    # Get file size by seeking
    file_storage.seek(0, 2)  # Seek to end
    size = file_storage.tell()
    file_storage.seek(0)  # Seek back to beginning
    
    return (size <= max_size_bytes), size


def _save_file(file, owner_id):
    ext         = file.filename.rsplit('.', 1)[1].lower()
    unique_name = f'{uuid.uuid4().hex}.{ext}'
    folder      = os.path.join(current_app.config['UPLOAD_FOLDER'], str(owner_id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, unique_name)
    file.save(path)
    return os.path.join(str(owner_id), unique_name)


def _log(action, target_id, detail=''):
    try:
        db.session.add(ActivityLog(
            user_id    = current_user.id,
            action     = action,
            target     = 'owner_document',
            target_id  = target_id,
            detail     = detail,
            ip_address = request.remote_addr,
        ))
    except Exception:
        pass


def _doc_to_dict(doc):
    """Serialize a OwnerDocument row to a plain dict (JSON-safe)."""
    uploader = User.query.get(doc.uploaded_by) if doc.uploaded_by else None
    owner   = Owner.query.get(doc.owner_id) if doc.owner_id else None
    return {
        'id':               doc.id,
        'owner_id':        doc.owner_id or '',
        'owner_name':      owner.name         if owner   else '',
        'owner_code':      owner.owner_code  if owner   else '',
        'document_type':    doc.document_type   or '',
        'document_name':    doc.document_name   or '',
        'issue_date':       str(getattr(doc, 'issue_date', '') or ''),
        'expiry_date':      str(doc.expiry_date or '') if doc.expiry_date else '',
        'uploaded_at':      doc.uploaded_at.strftime('%Y-%m-%d %H:%M') if doc.uploaded_at else '',
        'uploaded_by':      doc.uploaded_by,
        'uploaded_by_name': uploader.username   if uploader else '',
        'file_path':        doc.file_path       or '',
        'file_size_kb':     round((doc.file_size or 0) / 1024, 1),
    }


def _get_mime_type(file_path):
    """
    Get the MIME type of a file based on its extension.
    Returns 'application/octet-stream' if unknown.
    """
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type or 'application/octet-stream'


def _can_display_inline(mime_type):
    """
    Check if a MIME type can be displayed inline in the browser.
    PDFs and images can be shown inline, Office documents should download.
    """
    return mime_type in INLINE_MIMETYPES


# ─────────────────────────────────────────────────────────────────────
# 1. UPLOAD  POST /owners/<id>/documents/upload
#    Dual-mode: AJAX → JSON   |   plain form POST → flash + redirect
# ─────────────────────────────────────────────────────────────────────
@seller_doc_bp.route('/owners/<int:owner_id>/documents/upload', methods=['POST'])
@login_required
def upload_document(owner_id):
    is_ajax = bool(
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in request.headers.get('Accept', '')
        or request.headers.get('X-CSRFToken')
    )

    Owner.query.get_or_404(owner_id)
    file = request.files.get('file')

    def _fail(msg):
        if is_ajax:
            return jsonify({'ok': False, 'error': msg})
        flash(msg, 'danger')
        return redirect(url_for('sellers.view_owner', id=owner_id))

    if not file or not file.filename:
        return _fail(_t('No file selected.', 'لم يتم اختيار ملف'))
    
    # Debug logging
    print(f"DEBUG upload: filename='{file.filename}', content_type='{file.content_type}'")
    
    # Check file type
    if not _allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'unknown'
        msg = _t(
            f'File type not allowed. Got: .{ext}',
            f'نوع الملف غير مسموح به. المستلم: .{ext}'
        )
        return _fail(msg)
    
    # Check file size (16 MB limit)
    size_ok, file_size = _allowed_file_size(file, max_size_mb=16)
    if not size_ok:
        size_mb = file_size / (1024 * 1024)
        msg = _t(
            f'File too large ({size_mb:.1f} MB). Maximum size is 16 MB.',
            f'الملف كبير جداً ({size_mb:.1f} ميغابايت). الحد الأقصى هو 16 ميغابايت.'
        )
        return _fail(msg)

    path = _save_file(file, owner_id)
    doc  = OwnerDocument(
        owner_id     = owner_id,
        document_type = request.form.get('document_type', 'Other'),
        document_name = request.form.get('document_name', file.filename),
        file_path     = path,
        file_size     = os.path.getsize(
            os.path.join(current_app.config['UPLOAD_FOLDER'], path)),
        uploaded_by   = current_user.id,
        uploaded_at   = datetime.utcnow(),
    )

    raw_issue  = request.form.get('issue_date',  '')
    raw_expiry = request.form.get('expiry_date', '')
    if hasattr(doc, 'issue_date') and raw_issue:
        try:   doc.issue_date = _date.fromisoformat(raw_issue)
        except ValueError: pass
    if raw_expiry:
        try:   doc.expiry_date = _date.fromisoformat(raw_expiry)
        except ValueError: pass

    db.session.add(doc)
    db.session.commit()
    _log('UPLOAD', doc.id, f'Uploaded "{doc.document_name}"')

    if is_ajax:
        return jsonify({'ok': True, 'doc': _doc_to_dict(doc)})
    flash(_t('Document uploaded successfully.', 'تم رفع المستند بنجاح'), 'success')
    return redirect(url_for('sellers.view_owner', id=owner_id))


# ─────────────────────────────────────────────────────────────────────
# 2. VIEW INLINE  GET /owners/documents/<did>/view
#    Displays file in browser if possible (PDF, images, text)
#    Forces download for Office documents (Excel, Word, PowerPoint)
# ─────────────────────────────────────────────────────────────────────
@seller_doc_bp.route('/owners/documents/<int:did>/view')
@login_required
def view_document(did):
    """
    View a document inline in the browser.
    - PDFs, images, and text files: displayed inline
    - Office documents (Excel, Word, PPT): downloaded (browsers can't display them inline)
    """
    doc    = OwnerDocument.query.get_or_404(did)
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], str(doc.owner_id))
    fname  = doc.file_path.split(os.sep)[-1]
    mime   = _get_mime_type(doc.file_path)
    
    print(f"DEBUG view_document: file='{fname}', mime='{mime}', inline={_can_display_inline(mime)}")
    
    # For files that can be displayed inline (PDF, images, text)
    if _can_display_inline(mime):
        return send_from_directory(
            folder, fname,
            as_attachment=False,
            mimetype=mime
        )
    
    # For Office documents and other files, force download
    # because browsers can't display them inline properly
    return send_from_directory(
        folder, fname,
        as_attachment=True,
        download_name=doc.document_name or fname,
        mimetype=mime
    )


# ─────────────────────────────────────────────────────────────────────
# 3. DOWNLOAD  GET /owners/documents/<did>/download
#              GET /documents/<did>/download  (legacy alias)
#    Always forces download regardless of file type
# ─────────────────────────────────────────────────────────────────────
@seller_doc_bp.route('/owners/documents/<int:did>/download')
@login_required
def download_document(did):
    """Always download the file, never display inline."""
    doc    = OwnerDocument.query.get_or_404(did)
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], str(doc.owner_id))
    fname  = doc.file_path.split(os.sep)[-1]
    mime   = _get_mime_type(doc.file_path)
    
    return send_from_directory(
        folder, fname,
        as_attachment=True,
        download_name=doc.document_name or fname,
        mimetype=mime
    )


@seller_doc_bp.route('/documents/<int:did>/download')
@login_required
def download_document_legacy(did):
    """Legacy URL kept so existing links don't break."""
    return download_document(did)


# ─────────────────────────────────────────────────────────────────────
# 4. DELETE  POST /documents/<did>/delete
# ─────────────────────────────────────────────────────────────────────
@seller_doc_bp.route('/documents/<int:did>/delete', methods=['POST'])
@login_required
@_admin_required
def delete_document(did):
    doc   = OwnerDocument.query.get_or_404(did)
    fpath = os.path.join(current_app.config['UPLOAD_FOLDER'], doc.file_path)
    if os.path.exists(fpath):
        os.remove(fpath)
    _log('DELETE', did, f'Deleted "{doc.document_name}"')
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────────────────────────────────────────────────────
# 5. LIST JSON per owner  GET /owners/<id>/documents/json
#    Used by the owner edit modal to reload the documents tab
# ─────────────────────────────────────────────────────────────────────
@seller_doc_bp.route('/owners/<int:owner_id>/documents/json')
@login_required
def list_owner_documents(owner_id):
    docs = (OwnerDocument.query
            .filter_by(owner_id=owner_id)
            .order_by(OwnerDocument.id)
            .all())
    return jsonify([_doc_to_dict(d) for d in docs])


# ─────────────────────────────────────────────────────────────────────
# 6. SINGLE JSON  GET /owner-documents/<did>/json
#    Used by the edit modal on the standalone grid page
# ─────────────────────────────────────────────────────────────────────
@seller_doc_bp.route('/owner-documents/<int:did>/json')
@login_required
def owner_document_json(did):
    doc = OwnerDocument.query.get_or_404(did)
    return jsonify(_doc_to_dict(doc))


# ─────────────────────────────────────────────────────────────────────
# 7. EDIT METADATA  POST /owner-documents/<did>/edit
#    Updates document_type, document_name, issue_date, expiry_date.
#    File replacement is NOT done here (upload a new doc instead).
# ─────────────────────────────────────────────────────────────────────
@seller_doc_bp.route('/owner-documents/<int:did>/edit', methods=['POST'])
@login_required
@_admin_required
def edit_owner_document(did):
    doc  = OwnerDocument.query.get_or_404(did)
    data = request.get_json() or {}

    doc.document_type = data.get('document_type', doc.document_type) or doc.document_type
    doc.document_name = data.get('document_name', doc.document_name) or doc.document_name

    raw_issue  = data.get('issue_date',  '')
    raw_expiry = data.get('expiry_date', '')

    if hasattr(doc, 'issue_date'):
        try:
            doc.issue_date = _date.fromisoformat(raw_issue) if raw_issue \
                             else getattr(doc, 'issue_date', None)
        except ValueError:
            pass

    try:
        doc.expiry_date = _date.fromisoformat(raw_expiry) if raw_expiry else None
    except ValueError:
        pass

    db.session.commit()
    _log('EDIT', did, f'Edited "{doc.document_name}"')
    return jsonify({'ok': True, 'doc': _doc_to_dict(doc)})


# ─────────────────────────────────────────────────────────────────────
# 8. STANDALONE GRID PAGE  GET /owner-documents
# ─────────────────────────────────────────────────────────────────────
@seller_doc_bp.route('/owner-documents')
@login_required
def list_owner_documents_page():
    """Renders the full standalone AG-Grid page for all owner documents."""
    return render_template('owners/owner_doc.html')


# ─────────────────────────────────────────────────────────────────────
# 9. AG-GRID DATA FEED  GET /owner-documents/data
#    Returns every OwnerDocument row enriched with owner + uploader info
# ─────────────────────────────────────────────────────────────────────
@seller_doc_bp.route('/owner-documents/data')
@login_required
def owner_documents_data():
    docs = OwnerDocument.query.order_by(OwnerDocument.id.desc()).all()
    return jsonify([_doc_to_dict(d) for d in docs])