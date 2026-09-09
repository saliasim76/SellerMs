"""Database Backup / Restore -- Administration.

Dumps the entire MySQL database (schema + data) into a single JSON file
that can later be uploaded to fully restore it. Pure Python/pymysql --
no dependency on the mysqldump/mysql CLI tools being installed on the
host, which they are not in this environment.

Restore is destructive (drops and recreates every table), so it is
gated behind admin_required and always takes an automatic safety
backup of the current database, on disk, before touching anything.
"""
import os
import json
import decimal
import datetime
from functools import wraps

import pymysql
from flask import (Blueprint, render_template, request, jsonify, session,
                    send_file, current_app, abort)
from flask_login import login_required, current_user

from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

db_backup_bp = Blueprint('db_backup', __name__)


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            return jsonify({'ok': False, 'error': _t('Access denied. Admin privileges required.',
                                                       'الوصول مرفوض. يلزم صلاحيات المدير.')}), 403
        return f(*args, **kwargs)
    return decorated


def _raw_conn():
    return pymysql.connect(host=DB_HOST, port=int(DB_PORT), user=DB_USER,
                            password=DB_PASSWORD, database=DB_NAME,
                            charset='utf8mb4', cursorclass=pymysql.cursors.Cursor)


def _kill_other_connections(conn):
    """Restore does DROP/CREATE TABLE on every table, which needs an
    exclusive metadata lock. Any other open connection to this database --
    including ones idling with an uncommitted transaction, e.g. from the
    app's own SQLAlchemy pool -- can block that lock indefinitely. Since
    restore already intends to replace the entire database, it is safe to
    clear the way by terminating every other session first."""
    cur = conn.cursor()
    my_id = conn.thread_id()
    cur.execute("SELECT id FROM information_schema.processlist WHERE db=%s AND id<>%s", (DB_NAME, my_id))
    for (pid,) in cur.fetchall():
        try:
            cur.execute(f"KILL {int(pid)}")
        except Exception:
            pass


class _JSONEnc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (datetime.date, datetime.datetime)):
            return o.isoformat()
        if isinstance(o, decimal.Decimal):
            return str(o)
        if isinstance(o, (bytes, bytearray)):
            return {'__bytes__': o.hex()}
        return super().default(o)


def _decode_value(v):
    if isinstance(v, dict) and '__bytes__' in v:
        return bytes.fromhex(v['__bytes__'])
    return v


def _sql_literal(conn, value):
    """A single value as a MySQL SQL literal for the .sql dump below.
    Bytes/bytearray use MySQL's hex-literal syntax (X'..') rather than
    pymysql's own raw-byte string escaping (Connection.escape), which can
    embed non-UTF-8 bytes that break once the dump is written out as a
    UTF-8 text file -- this app has no BLOB columns today (uploaded files
    are stored on disk, only their paths live in the DB), but hex-literal
    is the universally-safe form regardless. Every other type (str, int,
    float, Decimal, date/datetime, bool, None) is already handled
    correctly by pymysql's own escaping."""
    if value is None:
        return 'NULL'
    if isinstance(value, (bytes, bytearray)):
        return "X'" + value.hex() + "'"
    return conn.escape(value)


def _dump_database_sql():
    """Schema (SHOW CREATE TABLE) + full row data for every table, as one
    plain .sql script (DROP/CREATE/INSERT statements) -- a standard MySQL
    dump any MySQL client can run directly (phpMyAdmin's Import tab,
    `mysql -u ... -p dbname < file.sql`, cPanel's own Import), unlike the
    JSON backup below which only this app's own Restore button can read
    back."""
    conn = _raw_conn()
    try:
        cur = conn.cursor()
        cur.execute("SHOW TABLES")
        tables = [r[0] for r in cur.fetchall()]

        lines = [
            f"-- SellerMS full database backup ({DB_NAME})",
            f"-- Generated: {datetime.datetime.utcnow().isoformat()}Z",
            "SET NAMES utf8mb4;",
            "SET FOREIGN_KEY_CHECKS=0;",
            "",
        ]
        for t in tables:
            cur.execute(f"SHOW CREATE TABLE `{t}`")
            create_sql = cur.fetchone()[1]
            lines.append(f"DROP TABLE IF EXISTS `{t}`;")
            lines.append(create_sql + ";")
            lines.append("")

            cur.execute(f"SELECT * FROM `{t}`")
            cols = [d[0] for d in cur.description]
            col_list = ','.join(f'`{c}`' for c in cols)
            rows = cur.fetchall()
            if rows:
                # Batch multiple rows per INSERT (faster restore) while
                # keeping any single statement from growing unbounded.
                batch_size = 500
                for i in range(0, len(rows), batch_size):
                    batch = rows[i:i + batch_size]
                    values_sql = ',\n'.join(
                        '(' + ','.join(_sql_literal(conn, v) for v in row) + ')'
                        for row in batch
                    )
                    lines.append(f"INSERT INTO `{t}` ({col_list}) VALUES\n{values_sql};")
                lines.append("")

        lines.append("SET FOREIGN_KEY_CHECKS=1;")
        return '\n'.join(lines)
    finally:
        conn.close()


def _dump_database():
    """Schema (SHOW CREATE TABLE) + full row data for every table."""
    conn = _raw_conn()
    try:
        cur = conn.cursor()
        cur.execute("SHOW TABLES")
        tables = [r[0] for r in cur.fetchall()]
        out = {
            'app': 'SellerMS', 'db_name': DB_NAME,
            'created_at': datetime.datetime.utcnow().isoformat(),
            'tables': {},
        }
        for t in tables:
            cur.execute(f"SHOW CREATE TABLE `{t}`")
            create_sql = cur.fetchone()[1]
            cur.execute(f"SELECT * FROM `{t}`")
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
            out['tables'][t] = {'create_sql': create_sql, 'columns': cols, 'rows': rows}
        return out
    finally:
        conn.close()


def _restore_database(payload):
    """Drop + recreate every table from the backup, then reload its rows.
    FK checks are disabled for the whole operation so table order and
    forward-referencing foreign keys never block the rebuild."""
    conn = _raw_conn()
    try:
        cur = conn.cursor()
        # Every other connection to this database -- including the app's own
        # pooled ones sitting idle mid-transaction -- can hold a metadata
        # lock that blocks the DROP/CREATE TABLE calls below indefinitely.
        # Restore is about to replace the whole database anyway, so clear
        # the way first rather than risk hanging forever on a stale lock.
        _kill_other_connections(conn)
        # Defense in depth: if a new connection still manages to grab a
        # conflicting lock in the brief window after the kill above, fail
        # fast with a clear error instead of hanging again.
        cur.execute("SET SESSION lock_wait_timeout = 30")
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        conn.commit()

        for table, info in payload['tables'].items():
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
            cur.execute(info['create_sql'])
            conn.commit()

        for table, info in payload['tables'].items():
            rows = info['rows']
            if not rows:
                continue
            cols = info['columns']
            col_list = ','.join(f'`{c}`' for c in cols)
            placeholders = ','.join(['%s'] * len(cols))
            sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"
            decoded = [[_decode_value(v) for v in row] for row in rows]
            cur.executemany(sql, decoded)
            conn.commit()

        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    finally:
        conn.close()


# ── Page ────────────────────────────────────────────────────────
@db_backup_bp.route('/admin/db-backup')
@login_required
def db_backup_page():
    if not current_user.is_admin():
        abort(403)
    return render_template('admin/db_backup.html')


# ── Backup: stream a single JSON file back to the browser ───────
@db_backup_bp.route('/admin/db-backup/download', methods=['POST'])
@login_required
@admin_required
def db_backup_download():
    try:
        import io
        data = _dump_database()
        buf = io.BytesIO(json.dumps(data, cls=_JSONEnc).encode('utf-8'))
        buf.seek(0)
        fname = f"sellerms_backup_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        return send_file(buf, mimetype='application/json',
                          as_attachment=True, download_name=fname)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Backup: stream a single .sql dump back to the browser ───────
@db_backup_bp.route('/admin/db-backup/download-sql', methods=['POST'])
@login_required
@admin_required
def db_backup_download_sql():
    try:
        import io
        sql_text = _dump_database_sql()
        buf = io.BytesIO(sql_text.encode('utf-8'))
        buf.seek(0)
        fname = f"sellerms_backup_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.sql"
        return send_file(buf, mimetype='application/sql',
                          as_attachment=True, download_name=fname)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Restore: upload a backup file and rebuild the database from it ──
@db_backup_bp.route('/admin/db-backup/restore', methods=['POST'])
@login_required
@admin_required
def db_backup_restore():
    f = request.files.get('backup_file')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': _t('No file selected.', 'لم يتم اختيار ملف.')}), 400
    try:
        payload = json.load(f.stream)
    except Exception:
        return jsonify({'ok': False, 'error': _t('Invalid backup file -- could not parse.',
                                                   'ملف النسخ الاحتياطي غير صالح — تعذر التحليل.')}), 400
    if not isinstance(payload, dict) or 'tables' not in payload:
        return jsonify({'ok': False, 'error': _t('Invalid backup file format.',
                                                   'صيغة ملف النسخ الاحتياطي غير صحيحة.')}), 400

    # Safety net: back up the CURRENT database to disk before overwriting it.
    try:
        safety = _dump_database()
        safety_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'db_safety_backups')
        os.makedirs(safety_dir, exist_ok=True)
        safety_name = f"pre_restore_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        with open(os.path.join(safety_dir, safety_name), 'w', encoding='utf-8') as fh:
            json.dump(safety, fh, cls=_JSONEnc)
    except Exception as e:
        return jsonify({'ok': False, 'error': _t(
            'Could not create a safety backup of the current database -- restore aborted before any changes were made: ',
            'تعذر إنشاء نسخة أمان لقاعدة البيانات الحالية — تم إلغاء الاستعادة قبل إجراء أي تغييرات: ') + str(e)}), 500

    try:
        _restore_database(payload)
        return jsonify({'ok': True, 'safety_backup': safety_name})
    except Exception as e:
        return jsonify({'ok': False, 'error': _t(
            'Restore failed partway through. A safety backup of the pre-restore database was saved as: ',
            'فشلت الاستعادة في منتصف الطريق. تم حفظ نسخة أمان لقاعدة البيانات السابقة باسم: ') + safety_name
            + ' -- ' + str(e)}), 500
