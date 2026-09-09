import os
import uuid
import mimetypes
from datetime import datetime, date as _date
from functools import wraps

from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    flash,
    request,
    current_app,
    send_from_directory,
    jsonify,
    session,
    abort,
    send_file,
)
from flask_login import login_required, current_user

from models import db, ActivityLog, User, SupplierMaster, SupplierDocument

supplier_doc_bp = Blueprint('supplier_doc', __name__)

mimetypes.add_type('application/msword', '.doc')
mimetypes.add_type('application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx')
mimetypes.add_type('application/vnd.ms-excel', '.xls')
mimetypes.add_type('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx')
mimetypes.add_type('application/vnd.ms-powerpoint', '.ppt')
mimetypes.add_type('application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx')

ALLOWED_MIMETYPES = {
    'application/pdf',
    'image/jpeg', 'image/png', 'image/gif', 'image/bmp', 'image/webp', 'image/tiff',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'text/plain', 'text/csv',
    'application/rtf',
    'application/zip', 'application/x-rar-compressed', 'application/x-7z-compressed',
}

ALLOWED_EXTENSIONS = {
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tiff', 'tif',
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'txt', 'csv', 'rtf', 'zip', 'rar', '7z',
}

INLINE_MIMETYPES = {
    'application/pdf',
    'image/jpeg', 'image/png', 'image/gif', 'image/bmp', 'image/webp', 'image/tiff',
    'text/plain', 'text/csv',
}

DOC_TYPE_CATEGORY = {
    'CR': 'registration',
    'VAT': 'registration',
    'Chamber': 'registration',
    'Zakat': 'registration',
    'Contract': 'legal',
    'Agreement': 'legal',
    'NDA': 'legal',
    'Invoice': 'financial',
    'Bank Statement': 'financial',
    'Insurance': 'financial',
    'Specification': 'technical',
    'Certificate': 'technical',
    'Other': 'other',
}


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def _admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash(_t('Access denied. Admin privileges required.', 'الوصول مرفوض. يلزم صلاحيات المدير.'), 'danger')
            return redirect(url_for('purchase.supplier_list'))
        return f(*args, **kwargs)
    return decorated


def _allowed_file(filename):
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    mime_type, _ = mimetypes.guess_type(filename)
    if mime_type and mime_type in ALLOWED_MIMETYPES:
        return True
    if ext in ALLOWED_EXTENSIONS:
        return True
    generic_mimes = {'application/octet-stream', 'application/x-msdownload', 'binary/octet-stream'}
    return mime_type in generic_mimes and ext in ALLOWED_EXTENSIONS


def _allowed_file_size(file_storage, max_size_mb=16):
    file_storage.seek(0, 2)
    size = file_storage.tell()
    file_storage.seek(0)
    return (size <= max_size_mb * 1024 * 1024), size


def _save_file(file_storage, supplier_id):
    """Save file and return the relative path."""
    # Get file extension
    ext = file_storage.filename.rsplit('.', 1)[1].lower() if '.' in file_storage.filename else 'bin'
    
    # Generate unique filename
    unique_name = f'{uuid.uuid4().hex}.{ext}'
    
    # Create supplier-specific folder
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'supplier_documents', str(supplier_id))
    os.makedirs(folder, exist_ok=True)
    
    # Save file
    full_path = os.path.join(folder, unique_name)
    file_storage.save(full_path)
    
    # Return relative path (for database storage)
    relative_path = f'supplier_documents/{supplier_id}/{unique_name}'
    
    current_app.logger.info(f"File saved: {full_path}")
    current_app.logger.info(f"Relative path: {relative_path}")
    
    return relative_path


def _log(action, target_id, detail=''):
    try:
        db.session.add(ActivityLog(
            user_id=current_user.id,
            action=action,
            target='supplier_document',
            target_id=target_id,
            detail=detail,
            ip_address=request.remote_addr,
        ))
    except Exception:
        pass


def _doc_to_dict(doc):
    uploader = User.query.get(doc.uploaded_by) if doc.uploaded_by else None
    supplier = SupplierMaster.query.get(doc.supplier_id) if doc.supplier_id else None

    file_path = doc.file_path or ''
    file_ext = file_path.rsplit('.', 1)[-1].lower() if '.' in file_path else ''

    doc_type = doc.document_type or 'Other'
    category = DOC_TYPE_CATEGORY.get(doc_type, 'other')
    if hasattr(doc, 'document_category') and doc.document_category:
        category = doc.document_category

    return {
        'id': doc.id,
        'supplier_id': doc.supplier_id or '',
        'supplier_name': supplier.supplier_name_en if supplier else '',
        'supplier_code': supplier.supplier_code if supplier else '',
        'document_type': doc_type,
        'document_category': category,
        'file_extension': file_ext,
        'document_name': doc.document_name or '',
        'issue_date': str(doc.issue_date) if doc.issue_date else '',
        'expiry_date': str(doc.expiry_date) if doc.expiry_date else '',
        'uploaded_at': doc.uploaded_at.strftime('%Y-%m-%d %H:%M') if doc.uploaded_at else '',
        'uploaded_by': doc.uploaded_by,
        'uploaded_by_name': uploader.username if uploader else '',
        'file_path': file_path,
        'file_size': doc.file_size or 0,
        'file_size_kb': round((doc.file_size or 0) / 1024, 1),
    }


def _get_mime_type(file_path):
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type or 'application/octet-stream'


def _can_display_inline(mime_type):
    return mime_type in INLINE_MIMETYPES


def _download_name(doc, stored_filename):
    """
    Build a safe download filename.

    ``document_name`` is user-entered and usually has no file extension, which
    makes the downloaded file unopenable. Always ensure the name carries the
    real extension taken from the stored file.
    """
    stored_ext = os.path.splitext(stored_filename)[1]          # e.g. '.pdf'
    name = (doc.document_name or '').strip()
    if not name:
        return stored_filename
    # Strip any path separators a user may have typed.
    name = os.path.basename(name.replace('\\', '/'))
    if not name:
        return stored_filename
    # If the name already ends with the correct extension, keep it as-is.
    if stored_ext and name.lower().endswith(stored_ext.lower()):
        return name
    return f'{name}{stored_ext}'


def _get_full_file_path(doc):
    """
    Get the full file system path for a document.
    Handles both relative paths and legacy flat filenames.
    """
    rel_path = (doc.file_path or '').replace('\\', '/')
    upload_root = current_app.config['UPLOAD_FOLDER']
    
    # If the path already contains 'supplier_documents', it's a relative path
    if rel_path.startswith('supplier_documents/'):
        full_path = os.path.join(upload_root, rel_path)
    else:
        # Legacy: flat filename in upload root
        full_path = os.path.join(upload_root, rel_path)
    
    current_app.logger.info(f"Resolved file path: {full_path}")
    return full_path


# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

@supplier_doc_bp.route('/supplier-documents')
@login_required
def list_supplier_documents_page():
    return render_template('supplier/supplier_doc.html')


@supplier_doc_bp.route('/supplier-documents/suppliers-list')
@login_required
def suppliers_list():
    suppliers = SupplierMaster.query.order_by(SupplierMaster.supplier_name_en.asc()).all()
    data = [
        {
            'id': v.id,
            'name': v.supplier_name_en or v.supplier_name_ar or '',
            'supplier_code': v.supplier_code or '',
        }
        for v in suppliers
    ]
    return jsonify(data)


@supplier_doc_bp.route('/supplier-documents/data')
@login_required
def supplier_documents_data():
    try:
        documents = SupplierDocument.query.order_by(SupplierDocument.uploaded_at.desc()).all()
        return jsonify([_doc_to_dict(doc) for doc in documents])
    except Exception as e:
        current_app.logger.error(f'supplier_documents_data error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@supplier_doc_bp.route('/supplier-documents/upload', methods=['POST'])
@login_required
def upload_documents():
    try:
        current_app.logger.info(f"Files in request: {request.files}")
        current_app.logger.info(f"Form data: {request.form}")
        
        supplier_id = request.form.get('supplier_id', '').strip()
        if not supplier_id:
            return jsonify({'ok': False, 'error': 'Supplier is required'})

        supplier = SupplierMaster.query.get(int(supplier_id))
        if not supplier:
            return jsonify({'ok': False, 'error': 'Supplier not found'})

        # Get the file - support both 'files' and 'file' field names
        file_storage = None
        if 'files' in request.files:
            file_storage = request.files['files']
        elif 'file' in request.files:
            file_storage = request.files['file']
        
        if not file_storage or not file_storage.filename:
            return jsonify({'ok': False, 'error': 'No file selected'})

        if not _allowed_file(file_storage.filename):
            return jsonify({'ok': False, 'error': 'File type not allowed'})

        ok, size = _allowed_file_size(file_storage)
        if not ok:
            size_mb = size / (1024 * 1024)
            return jsonify({'ok': False, 'error': f'File too large ({size_mb:.1f} MB). Max 16 MB.'})

        # Save the file
        relative_path = _save_file(file_storage, supplier.id)

        # Get document metadata
        doc_type = request.form.get('document_type', 'Other')
        doc_name = request.form.get('document_name', '').strip()
        if not doc_name:
            doc_name = file_storage.filename

        expiry_str = request.form.get('expiry_date', '')
        category = DOC_TYPE_CATEGORY.get(doc_type, 'other')

        # Create document record
        doc = SupplierDocument(
            supplier_id=supplier.id,
            document_type=doc_type or 'Other',
            document_name=doc_name,
            issue_date=(
                _date.fromisoformat(request.form.get('issue_date', ''))
                if request.form.get('issue_date') else None
            ),
            expiry_date=(
                _date.fromisoformat(expiry_str)
                if expiry_str else None
            ),
            file_path=relative_path,
            file_size=size,
            uploaded_by=current_user.id,
            uploaded_at=datetime.utcnow(),
        )

        if hasattr(doc, 'document_category'):
            doc.document_category = category

        db.session.add(doc)
        db.session.commit()
        
        _log('UPLOAD', doc.id, f'Uploaded "{doc.document_name}" for supplier {supplier.supplier_code}')

        return jsonify({
            'ok': True,
            'message': 'File uploaded successfully',
            'document_id': doc.id,
        })

    except Exception as e:
        current_app.logger.error(f'upload_documents error: {e}', exc_info=True)
        db.session.rollback()
        return jsonify({'ok': False, 'error': f'Server error: {str(e)}'}), 500


@supplier_doc_bp.route('/suppliers/documents/<int:doc_id>/view')
@login_required
def view_document(doc_id):
    """View document inline if possible."""
    try:
        doc = SupplierDocument.query.get_or_404(doc_id)
        full_path = _get_full_file_path(doc)
        
        current_app.logger.info(f"Viewing document: {full_path}")
        
        if not os.path.exists(full_path):
            current_app.logger.error(f"File not found: {full_path}")
            abort(404, "File not found on server")
        
        # Get the directory and filename
        directory = os.path.dirname(full_path)
        filename = os.path.basename(full_path)
        
        # Get mime type
        mime_type = _get_mime_type(full_path)
        
        # If it's a PDF or image, display inline, otherwise download
        if _can_display_inline(mime_type):
            return send_from_directory(
                directory, 
                filename, 
                as_attachment=False, 
                mimetype=mime_type
            )
        else:
            # For other files, download
            return send_from_directory(
                directory, 
                filename, 
                as_attachment=True,
                download_name=_download_name(doc, filename),
                mimetype=mime_type
            )
            
    except Exception as e:
        current_app.logger.error(f'view_document error: {e}', exc_info=True)
        abort(500, f"Error viewing document: {str(e)}")


@supplier_doc_bp.route('/suppliers/documents/<int:doc_id>/download')
@login_required
def download_document(doc_id):
    """Force download the document."""
    try:
        doc = SupplierDocument.query.get_or_404(doc_id)
        full_path = _get_full_file_path(doc)
        
        current_app.logger.info(f"Downloading document: {full_path}")
        
        if not os.path.exists(full_path):
            current_app.logger.error(f"File not found: {full_path}")
            abort(404, "File not found on server")
        
        # Get the directory and filename
        directory = os.path.dirname(full_path)
        filename = os.path.basename(full_path)
        
        # Get mime type
        mime_type = _get_mime_type(full_path)
        
        return send_from_directory(
            directory, 
            filename, 
            as_attachment=True,
            download_name=_download_name(doc, filename),
            mimetype=mime_type
        )
        
    except Exception as e:
        current_app.logger.error(f'download_document error: {e}', exc_info=True)
        abort(500, f"Error downloading document: {str(e)}")


@supplier_doc_bp.route('/supplier-documents/<int:doc_id>/json')
@login_required
def supplier_document_json(doc_id):
    doc = SupplierDocument.query.get_or_404(doc_id)
    return jsonify(_doc_to_dict(doc))


@supplier_doc_bp.route('/supplier-documents/<int:doc_id>/edit', methods=['POST'])
@login_required
@_admin_required
def edit_supplier_document(doc_id):
    doc  = SupplierDocument.query.get_or_404(doc_id)
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
    _log('EDIT', doc_id, f'Edited "{doc.document_name}"')
    return jsonify({'ok': True, 'doc': _doc_to_dict(doc)})


@supplier_doc_bp.route('/supplier-documents/<int:doc_id>/delete', methods=['POST'])
@login_required
@_admin_required
def delete_document(doc_id):
    try:
        doc = SupplierDocument.query.get_or_404(doc_id)
        full_path = _get_full_file_path(doc)
        doc_name = doc.document_name or os.path.basename(full_path)
        
        # Delete the file if it exists
        if os.path.exists(full_path):
            os.remove(full_path)
            current_app.logger.info(f"Deleted file: {full_path}")
        
        _log('DELETE', doc.id, f'Deleted "{doc_name}"')
        db.session.delete(doc)
        db.session.commit()
        
        return jsonify({'ok': True})
        
    except Exception as e:
        current_app.logger.error(f'delete_document error: {e}', exc_info=True)
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500