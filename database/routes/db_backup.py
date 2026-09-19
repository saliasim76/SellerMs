"""Database Backup / Restore -- Super Admin.

Dumps the entire MySQL database (schema + data) into a single JSON or
.sql file that can later be uploaded to fully restore it. Pure
Python/pymysql -- no dependency on the mysqldump/mysql CLI tools being
installed on the host, which they are not in this environment.

Restore is destructive (drops and recreates every table), so it is
gated behind super_admin_required -- not the broader is_admin() check
every tenant's own Admin account also satisfies -- and always takes an
automatic safety backup of the current database, on disk, before
touching anything.
"""
import os
import re
import json
import decimal
import datetime
from functools import wraps

import pymysql
from pymysql.constants import CLIENT
from flask import (Blueprint, render_template, request, jsonify, session,
                    send_file, current_app, abort)
from flask_login import login_required, current_user

from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

db_backup_bp = Blueprint('db_backup', __name__)


def _t(en, ar):
    return ar if session.get('lang') == 'ar' else en


def super_admin_required(f):
    """Whole-database backup/restore is destructive (restore drops and
    recreates every table), so it is gated to a Super Admin specifically --
    not the broader is_admin() check every tenant's own Admin account also
    satisfies. effectively_super_admin (not the is_super_admin flag alone)
    so a tenant's own local SuperAdmin can use this too -- safe because
    _target_db_name() below always resolves to THAT tenant's own database
    when inside a tenant session, never the platform's."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not getattr(current_user, 'effectively_super_admin', False):
            return jsonify({'ok': False, 'error': _t('Access denied. Super Admin privileges required.',
                                                       'الوصول مرفوض. يلزم صلاحيات المدير الأعلى.')}), 403
        return f(*args, **kwargs)
    return decorated


def _target_db_name():
    """The database this request's backup/restore should act on: the
    active tenant's own database when a Super Admin is inside a tenant
    session (session['tenant_db'], same value TenantAwareSession routes
    every other query by), else the platform's own default database.
    Every connection and every display/filename below goes through this
    instead of the bare DB_NAME so a Super Admin backing up/restoring
    while "inside" SaaS Customer X always acts on X's own database, never
    silently falls back to the platform's shared one."""
    return session.get('tenant_db') or DB_NAME


def _conn_settings():
    """(host, port, user, password) for the database this request acts on: the
    settings saved on the customer when inside a tenant session, else the
    platform's own."""
    tenant = session.get('tenant_db')
    if tenant:
        from database.tenant_provisioning import tenant_connection_params
        return tenant_connection_params(tenant)
    return DB_HOST, DB_PORT, DB_USER, DB_PASSWORD


def _raw_conn():
    host, port, user, password = _conn_settings()
    return pymysql.connect(host=host, port=int(port), user=user,
                            password=password, database=_target_db_name(),
                            charset='utf8mb4', cursorclass=pymysql.cursors.Cursor)


# MySQL 8.0's utf8mb4_0900_* collation family (its default since 8.0,
# e.g. utf8mb4_0900_ai_ci) doesn't exist on MariaDB or pre-8.0 MySQL --
# both common on shared/cPanel hosting -- so a raw `SHOW CREATE TABLE`
# captured from a MySQL 8 server (this app's own local dev database)
# fails to import there with "Unknown collation". Every backup this
# module produces is normalized to utf8mb4_general_ci instead, which
# every MySQL version and MariaDB understands, so a downloaded .sql or
# .json file is portable to whatever database engine/version the host
# actually runs.
_MYSQL8_COLLATION_RE = re.compile(r'utf8mb4_0900_\w+')


def _portable_create_sql(create_sql):
    return _MYSQL8_COLLATION_RE.sub('utf8mb4_general_ci', create_sql)


def _raw_conn_multi():
    """Same connection as _raw_conn(), with CLIENT_MULTI_STATEMENTS enabled
    so a whole .sql script (many ;-separated statements) can be sent to
    MySQL in one execute() call -- the server itself then parses statement
    boundaries, which correctly handles a semicolon that appears inside a
    quoted string value. A naive Python-side str.split(';') would corrupt
    exactly that case."""
    host, port, user, password = _conn_settings()
    return pymysql.connect(host=host, port=int(port), user=user,
                            password=password, database=_target_db_name(),
                            charset='utf8mb4', cursorclass=pymysql.cursors.Cursor,
                            client_flag=CLIENT.MULTI_STATEMENTS)


def _kill_other_connections(conn):
    """Restore does DROP/CREATE TABLE on every table, which needs an
    exclusive metadata lock. Any other open connection to this database --
    including ones idling with an uncommitted transaction, e.g. from the
    app's own SQLAlchemy pool -- can block that lock indefinitely. Since
    restore already intends to replace the entire database, it is safe to
    clear the way by terminating every other session first.

    The CURRENT request's own Flask-SQLAlchemy connection (used behind
    the scenes by @login_required/current_user on every request, on a
    MySQL thread_id that has nothing to do with the raw `conn` passed
    in here) is closed first rather than left to be killed alongside
    everything else -- killing it out from under Flask crashes this
    same request's own teardown handler once the view function returns,
    and since that happens AFTER the response has started streaming
    back, the browser sees a dropped connection ("Failed to fetch")
    even though the server logs the request as a normal 200. Closing it
    cleanly here instead just returns it to the pool; pool_pre_ping
    (config.py) transparently reconnects next time it's needed."""
    from models import db
    db.session.close()

    cur = conn.cursor()
    my_id = conn.thread_id()
    cur.execute("SELECT id FROM information_schema.processlist WHERE db=%s AND id<>%s", (_target_db_name(), my_id))
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
            f"-- SellerMS full database backup ({_target_db_name()})",
            f"-- Generated: {datetime.datetime.utcnow().isoformat()}Z",
            "SET NAMES utf8mb4;",
            "SET FOREIGN_KEY_CHECKS=0;",
            "",
        ]

        # Every DROP runs before ANY CREATE. If a table with a foreign
        # key (e.g. journal_entry_detail -> level_five) were instead
        # dropped-and-recreated immediately, back to back per table,
        # re-importing into a database that still has a stale, not-yet-
        # recreated version of the table it references (e.g. from an
        # earlier partial/failed import) makes MySQL compare the new
        # column against that stale one and reject the FK as
        # "incompatible" (error 3780) -- even with FOREIGN_KEY_CHECKS=0,
        # MySQL still validates column compatibility against a table
        # that already exists. Dropping every table first means the
        # referenced table genuinely doesn't exist yet when its
        # dependent's FK is declared, so that check is deferred
        # entirely, exactly as `mysqldump` itself relies on.
        for t in tables:
            lines.append(f"DROP TABLE IF EXISTS `{t}`;")
        lines.append("")

        for t in tables:
            cur.execute(f"SHOW CREATE TABLE `{t}`")
            create_sql = _portable_create_sql(cur.fetchone()[1])
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
            'app': 'SellerMS', 'db_name': _target_db_name(),
            'created_at': datetime.datetime.utcnow().isoformat(),
            'tables': {},
        }
        for t in tables:
            cur.execute(f"SHOW CREATE TABLE `{t}`")
            create_sql = _portable_create_sql(cur.fetchone()[1])
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

        # Every DROP runs before ANY CREATE -- see _dump_database_sql()'s
        # matching comment for why interleaving drop+create per table
        # can make MySQL reject a foreign key as "incompatible" (error
        # 3780) against a not-yet-recreated table it references, even
        # with FOREIGN_KEY_CHECKS off.
        for table in payload['tables']:
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        conn.commit()

        for table, info in payload['tables'].items():
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


def _restore_database_sql(sql_text):
    """Same destructive whole-database rebuild as _restore_database(),
    fed a plain .sql script instead of the JSON format -- either this
    app's own .sql backup (db_backup_download_sql() above) or a standard
    dump from an external tool (e.g. `mysqldump`, phpMyAdmin's Export),
    since both are just DROP/CREATE/INSERT statements MySQL itself can
    run directly. Uses PyMySQL's multi-statement execution (see
    _raw_conn_multi()) rather than splitting on ';' in Python."""
    conn = _raw_conn_multi()
    try:
        cur = conn.cursor()
        _kill_other_connections(conn)
        cur.execute("SET SESSION lock_wait_timeout = 30")
        cur.execute(sql_text)
        # Every statement after the first only actually runs as this
        # drains through its result -- without this loop, everything
        # past the first statement in the script would silently never
        # execute.
        while cur.nextset():
            pass
        conn.commit()
    finally:
        conn.close()


# ── Page ────────────────────────────────────────────────────────
@db_backup_bp.route('/admin/db-backup')
@login_required
def db_backup_page():
    if not getattr(current_user, 'effectively_super_admin', False):
        abort(403)
    return render_template('admin/db_backup.html', target_db_name=_target_db_name())


# ── Backup: stream a single JSON file back to the browser ───────
@db_backup_bp.route('/admin/db-backup/download', methods=['POST'])
@login_required
@super_admin_required
def db_backup_download():
    try:
        import io
        data = _dump_database()
        buf = io.BytesIO(json.dumps(data, cls=_JSONEnc).encode('utf-8'))
        buf.seek(0)
        fname = f"{_target_db_name()}_backup_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        return send_file(buf, mimetype='application/json',
                          as_attachment=True, download_name=fname)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Backup: stream a single .sql dump back to the browser ───────
@db_backup_bp.route('/admin/db-backup/download-sql', methods=['POST'])
@login_required
@super_admin_required
def db_backup_download_sql():
    try:
        import io
        sql_text = _dump_database_sql()
        buf = io.BytesIO(sql_text.encode('utf-8'))
        buf.seek(0)
        fname = f"{_target_db_name()}_backup_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.sql"
        return send_file(buf, mimetype='application/sql',
                          as_attachment=True, download_name=fname)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Restore: upload a backup file and rebuild the database from it ──
@db_backup_bp.route('/admin/db-backup/restore', methods=['POST'])
@login_required
@super_admin_required
def db_backup_restore():
    f = request.files.get('backup_file')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': _t('No file selected.', 'لم يتم اختيار ملف.')}), 400

    raw = f.stream.read()
    name_lower = f.filename.lower()
    payload = None
    sql_text = None

    if name_lower.endswith('.sql'):
        sql_text = raw.decode('utf-8', errors='replace')
    elif name_lower.endswith('.json'):
        try:
            payload = json.loads(raw)
        except Exception:
            return jsonify({'ok': False, 'error': _t('Invalid backup file -- could not parse.',
                                                       'ملف النسخ الاحتياطي غير صالح — تعذر التحليل.')}), 400
    else:
        # No recognizable extension -- guess by content: valid JSON never
        # coincidentally looks like a SQL script and vice versa, so try
        # JSON first and fall back to treating it as plain .sql.
        try:
            payload = json.loads(raw)
        except Exception:
            sql_text = raw.decode('utf-8', errors='replace')

    if payload is not None and (not isinstance(payload, dict) or 'tables' not in payload):
        return jsonify({'ok': False, 'error': _t('Invalid backup file format.',
                                                   'صيغة ملف النسخ الاحتياطي غير صحيحة.')}), 400

    # Cross-tenant safety net: a backup downloaded while "inside" one SaaS
    # customer's session is a completely normal file to have lying around,
    # but restoring it while inside a DIFFERENT customer's session would
    # silently overwrite that other customer's live database with this
    # one's data. Block it unless explicitly confirmed. JSON backups record
    # their source db_name directly; .sql backups only have it in the
    # leading comment this app itself writes (see _dump_database_sql()),
    # so a backup from any other source (mysqldump, phpMyAdmin) has no
    # recorded name and is allowed through uncompared.
    backup_db_name = None
    if payload is not None:
        backup_db_name = payload.get('db_name')
    elif sql_text is not None:
        m = re.search(r'^-- SellerMS full database backup \(([^)]+)\)', sql_text, re.MULTILINE)
        if m:
            backup_db_name = m.group(1)
    target_db_name = _target_db_name()
    if (backup_db_name and backup_db_name != target_db_name
            and request.form.get('confirm_mismatch') != 'true'):
        return jsonify({
            'ok': False, 'db_mismatch': True,
            'backup_db': backup_db_name, 'target_db': target_db_name,
            'error': _t(
                f'This backup was taken from database "{backup_db_name}", but you are '
                f'about to restore into "{target_db_name}". Confirm if this is intentional.',
                f'تم أخذ هذه النسخة الاحتياطية من قاعدة البيانات "{backup_db_name}"، '
                f'لكنك على وشك الاستعادة إلى "{target_db_name}". أكّد إذا كان هذا مقصوداً.'),
        }), 409

    # Safety net: back up the CURRENT database to disk before overwriting it.
    try:
        safety = _dump_database()
        safety_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'db_safety_backups')
        os.makedirs(safety_dir, exist_ok=True)
        safety_name = f"pre_restore_{target_db_name}_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        with open(os.path.join(safety_dir, safety_name), 'w', encoding='utf-8') as fh:
            json.dump(safety, fh, cls=_JSONEnc)
    except Exception as e:
        return jsonify({'ok': False, 'error': _t(
            'Could not create a safety backup of the current database -- restore aborted before any changes were made: ',
            'تعذر إنشاء نسخة أمان لقاعدة البيانات الحالية — تم إلغاء الاستعادة قبل إجراء أي تغييرات: ') + str(e)}), 500

    try:
        if sql_text is not None:
            _restore_database_sql(sql_text)
        else:
            _restore_database(payload)
        return jsonify({'ok': True, 'safety_backup': safety_name})
    except Exception as e:
        return jsonify({'ok': False, 'error': _t(
            'Restore failed partway through. A safety backup of the pre-restore database was saved as: ',
            'فشلت الاستعادة في منتصف الطريق. تم حفظ نسخة أمان لقاعدة البيانات السابقة باسم: ') + safety_name
            + ' -- ' + str(e)}), 500
