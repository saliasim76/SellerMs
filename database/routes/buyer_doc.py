import os, uuid, mimetypes
from datetime import datetime, date as _date
from functools import wraps

from flask import (
    Blueprint, render_template, redirect, url_for, flash,
    request, current_app, send_from_directory, jsonify, session,
)
from flask_login import login_required, current_user
from models import db, ActivityLog, User, BuyerMaster, BuyerDocument

buyer_doc_bp = Blueprint('buyer_doc', __name__)

# ── MIME configuration ───────────────────────────────────────────────
mimetypes.add_type('application/msword', '.doc')
mimetypes.add_type('application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx')
mimetypes.add_type('application/vnd.ms-excel', '.xls')
mimetypes.add_type('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx')
mimetypes.add_type('application/vnd.ms-powerpoint', '.ppt')
mimetypes.add_type('application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx')

ALLOWED_MIMETYPES = {
    'application/pdf',
    'image/jpeg','image/png','image/gif','image/bmp','image/webp','image/tiff',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'text/plain','text/csv',
    'application/rtf',
    'application/zip','application/x-rar-compressed','application/x-7z-compressed',
}
ALLOWED_EXTENSIONS = {
    'jpg','jpeg','png','gif','bmp','webp','tiff','tif',
    'pdf','doc','docx','xls','xlsx','ppt','pptx',
    'txt','csv','rtf','zip','rar','7z',
}
INLINE_MIMETYPES = {
    'application/pdf',
    'image/jpeg','image/png','image/gif','image/bmp','image/webp','image/tiff',
    'text/plain','text/csv',
}

# ── Helpers ──────────────────────────────────────────────────────────
def _t(en, ar): return ar if session.get('lang') == 'ar' else en

def _admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash(_t('Access denied.','الوصول مرفوض.'),'danger')
            return redirect(url_for('lookups.list_buyers'))
        return f(*args, **kwargs)
    return decorated

def _allowed_file(filename):
    if not filename or '.' not in filename: return False
    ext  = filename.rsplit('.', 1)[1].lower()
    mime, _ = mimetypes.guess_type(filename)
    if mime and mime in ALLOWED_MIMETYPES: return True
    if ext in ALLOWED_EXTENSIONS: return True
    return False

def _allowed_file_size(file_storage, max_size_mb=16):
    file_storage.seek(0, 2); size = file_storage.tell(); file_storage.seek(0)
    return (size <= max_size_mb * 1024 * 1024), size

def _save_file(file, buyer_id):
    ext = file.filename.rsplit('.', 1)[1].lower()
    unique_name = f'{uuid.uuid4().hex}.{ext}'
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'buyer', str(buyer_id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, unique_name)
    file.save(path)
    return os.path.join('buyer', str(buyer_id), unique_name)

def _log(action, target_id, detail=''):
    try:
        db.session.add(ActivityLog(
            user_id=current_user.id, action=action,
            target='buyer_document', target_id=target_id,
            detail=detail, ip_address=request.remote_addr,
        ))
    except Exception: pass

def _doc_to_dict(doc):
    uploader = User.query.get(doc.uploaded_by) if doc.uploaded_by else None
    buyer    = BuyerMaster.query.get(doc.buyer_id) if doc.buyer_id else None
    return {
        'id':               doc.id,
        'buyer_id':         doc.buyer_id or '',
        'buyer_name':       buyer.buyer_name_en if buyer else '',
        'buyer_code':       buyer.buyer_code if buyer else '',
        'document_type':    doc.document_type or '',
        'document_name':    doc.document_name or '',
        'issue_date':       str(getattr(doc, 'issue_date', '') or ''),
        'expiry_date':      str(doc.expiry_date or '') if doc.expiry_date else '',
        'uploaded_at':      doc.uploaded_at.strftime('%Y-%m-%d %H:%M') if doc.uploaded_at else '',
        'uploaded_by':      doc.uploaded_by,
        'uploaded_by_name': uploader.username if uploader else '',
        'file_path':        doc.file_path or '',
        'file_size_kb':     round((doc.file_size or 0) / 1024, 1),
    }

def _get_mime_type(file_path):
    mime, _ = mimetypes.guess_type(file_path)
    return mime or 'application/octet-stream'

def _can_display_inline(mime_type): return mime_type in INLINE_MIMETYPES

def _resolve_path(doc):
    rel = (doc.file_path or '').replace('\\', '/')
    parts = rel.split('/')
    fname = parts[-1]
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], *parts[:-1])
    return folder, fname, os.path.join(folder, fname)

# ── 1. UPLOAD ────────────────────────────────────────────────────────
@buyer_doc_bp.route('/buyers/<int:buyer_id>/documents/upload', methods=['POST'])
@login_required
def upload_document(buyer_id):
    is_ajax = bool(request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                   or 'application/json' in request.headers.get('Accept', '')
                   or request.headers.get('X-CSRFToken'))

    # Verify buyer exists
    BuyerMaster.query.get_or_404(buyer_id)
    file = request.files.get('file')

    def _fail(msg):
        if is_ajax: return jsonify({'ok': False, 'error': msg})
        flash(msg, 'danger')
        return redirect(url_for('lookups.list_buyers'))

    if not file or not file.filename:
        return _fail(_t('No file selected.', 'لم يتم اختيار ملف'))
    if not _allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'unknown'
        return _fail(_t(f'File type not allowed. Got: .{ext}', f'نوع الملف غير مسموح به. المستلم: .{ext}'))
    size_ok, file_size = _allowed_file_size(file, 16)
    if not size_ok:
        size_mb = file_size / (1024 * 1024)
        return _fail(_t(f'File too large ({size_mb:.1f} MB). Max 16 MB.', f'الملف كبير جداً ({size_mb:.1f} ميغابايت).'))

    path = _save_file(file, buyer_id)
    doc  = BuyerDocument(
        buyer_id      = buyer_id,
        document_type = request.form.get('document_type', 'Other'),
        document_name = request.form.get('document_name', file.filename),
        file_path     = path,
        file_size     = os.path.getsize(os.path.join(current_app.config['UPLOAD_FOLDER'], path)),
        uploaded_by   = current_user.id,
        uploaded_at   = datetime.utcnow(),
    )
    raw_issue  = request.form.get('issue_date', '')
    raw_expiry = request.form.get('expiry_date', '')
    if raw_issue:
        try: doc.issue_date = _date.fromisoformat(raw_issue)
        except ValueError: pass
    if raw_expiry:
        try: doc.expiry_date = _date.fromisoformat(raw_expiry)
        except ValueError: pass
    db.session.add(doc); db.session.commit()
    _log('UPLOAD', doc.id, f'Uploaded "{doc.document_name}"')
    if is_ajax: return jsonify({'ok': True, 'doc': _doc_to_dict(doc)})
    flash(_t('Document uploaded successfully.', 'تم رفع المستند بنجاح'), 'success')
    return redirect(url_for('lookups.list_buyers'))

# ── 2. VIEW ──────────────────────────────────────────────────────────
@buyer_doc_bp.route('/buyers/documents/<int:did>/view')
@login_required
def view_document(did):
    doc = BuyerDocument.query.get_or_404(did)
    folder, fname, fpath = _resolve_path(doc)
    if not os.path.exists(fpath):
        from flask import abort; abort(404)
    mime = _get_mime_type(fpath)
    return send_from_directory(folder, fname, as_attachment=False, mimetype=mime)

# ── 3. DOWNLOAD ──────────────────────────────────────────────────────
@buyer_doc_bp.route('/buyers/documents/<int:did>/download')
@login_required
def download_document(did):
    doc = BuyerDocument.query.get_or_404(did)
    folder, fname, fpath = _resolve_path(doc)
    if not os.path.exists(fpath):
        from flask import abort; abort(404)
    mime = _get_mime_type(fpath)
    return send_from_directory(folder, fname, as_attachment=True,
                               download_name=doc.document_name or fname, mimetype=mime)

# ── 4. DELETE ────────────────────────────────────────────────────────
@buyer_doc_bp.route('/buyer-documents/<int:did>/delete', methods=['POST'])
@login_required
@_admin_required
def delete_document(did):
    doc = BuyerDocument.query.get_or_404(did)
    _, _, fpath = _resolve_path(doc)
    if os.path.exists(fpath): os.remove(fpath)
    _log('DELETE', did, f'Deleted "{doc.document_name}"')
    db.session.delete(doc); db.session.commit()
    return jsonify({'ok': True})

# ── 5. LIST JSON per buyer ───────────────────────────────────────────
@buyer_doc_bp.route('/buyers/<int:buyer_id>/documents/json')
@login_required
def list_buyer_documents(buyer_id):
    docs = BuyerDocument.query.filter_by(buyer_id=buyer_id).order_by(BuyerDocument.id).all()
    return jsonify([_doc_to_dict(d) for d in docs])

# ── 6. SINGLE JSON ───────────────────────────────────────────────────
@buyer_doc_bp.route('/buyer-documents/<int:did>/json')
@login_required
def buyer_document_json(did):
    doc = BuyerDocument.query.get_or_404(did)
    return jsonify(_doc_to_dict(doc))

# ── 7. EDIT METADATA ─────────────────────────────────────────────────
@buyer_doc_bp.route('/buyer-documents/<int:did>/edit', methods=['POST'])
@login_required
@_admin_required
def edit_buyer_document(did):
    doc  = BuyerDocument.query.get_or_404(did)
    data = request.get_json() or {}
    doc.document_type = data.get('document_type', doc.document_type) or doc.document_type
    doc.document_name = data.get('document_name', doc.document_name) or doc.document_name
    raw_issue  = data.get('issue_date', '')
    raw_expiry = data.get('expiry_date', '')
    if raw_issue:
        try: doc.issue_date = _date.fromisoformat(raw_issue)
        except ValueError: pass
    try: doc.expiry_date = _date.fromisoformat(raw_expiry) if raw_expiry else None
    except ValueError: pass
    db.session.commit()
    _log('EDIT', did, f'Edited "{doc.document_name}"')
    return jsonify({'ok': True, 'doc': _doc_to_dict(doc)})

# ── 8. STANDALONE GRID PAGE ──────────────────────────────────────────
@buyer_doc_bp.route('/buyer-documents')
@login_required
def list_buyer_documents_page():
    return render_template('buyers/buyer_doc.html')

# ── 9. AG-GRID DATA FEED ─────────────────────────────────────────────
@buyer_doc_bp.route('/buyer-documents/data')
@login_required
def buyer_documents_data():
    docs = BuyerDocument.query.order_by(BuyerDocument.id.desc()).all()
    return jsonify([_doc_to_dict(d) for d in docs])