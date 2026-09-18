import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from ..config import YXO_DB_PATH, BOT_CONFIG_DB_PATH, RESPONSIBLE_COMPANY_COL, COMPANY_ALIAS
from .. import config
from .log import get_logger

_log = get_logger(__name__)
_yxo_lock = threading.Lock()
_bot_config_lock = threading.Lock()


def _is_unc_path(path) -> bool:
    s = str(path)
    return s.startswith("\\\\") or s.startswith("//")


def get_yxo_connection(readonly: bool = True) -> sqlite3.Connection:
    uri = f"file:{YXO_DB_PATH}?mode={'ro' if readonly else 'rw'}"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not readonly:
        if _is_unc_path(YXO_DB_PATH):
            _log.warning(
                f"YXO_DB_PATH is UNC ({YXO_DB_PATH}) — WAL 不兼容网络盘，"
                f"建议改用本地路径 D:\\YXO_DATA\\yxo_app\\data\\yxo.db；"
                f"本次回退为 DELETE 模式，写库可能失败。"
            )
        else:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception as e:
                _log.warning(f"WAL pragma failed, fallback to DELETE: {e}")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_bot_config_connection() -> sqlite3.Connection:
    Path(BOT_CONFIG_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(BOT_CONFIG_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_bot_config_db():
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    to_addrs TEXT DEFAULT '[]',
                    cc_addrs TEXT DEFAULT '[]',
                    extra TEXT DEFAULT '{}',
                    updated_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(bot, scope, key)
                );
                CREATE INDEX IF NOT EXISTS idx_bot_config_lookup ON bot_config(bot, scope, key);
                """
            )
            conn.commit()
        finally:
            conn.close()


def load_records(active_only: bool = True) -> List[Dict[str, Any]]:
    with _yxo_lock:
        conn = get_yxo_connection(readonly=True)
        try:
            where = ""
            if active_only:
                where = "WHERE 状态 <> '退舱' AND COALESCE(is_deleted, 0) = 0"
            rows = conn.execute(
                f"""
                SELECT id, 客户编码, 箱号, 班列号, 目的站, 开票子公司名称, 本地货源公司, 状态, is_deleted, dsk, ATB
                FROM records
                {where}
                """
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                company = d.get(RESPONSIBLE_COMPANY_COL, "")
                d["company"] = COMPANY_ALIAS.get(company, company)
                result.append(d)
            return result
        finally:
            conn.close()


def get_responsible_person(company: str) -> Optional[str]:
    if not company:
        return None
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            cursor = conn.execute(
                "SELECT extra FROM bot_config WHERE scope='owner' AND bot='all' AND key=?",
                (company,),
            )
            row = cursor.fetchone()
            if row and row["extra"]:
                import json
                extra = json.loads(row["extra"])
                return extra.get("email")
            return None
        finally:
            conn.close()


def get_recipients(company: str, scope: str = "company") -> Tuple[List[str], List[str]]:
    if not company:
        return [], []
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            import json
            cursor = conn.execute(
                "SELECT to_addrs, cc_addrs FROM bot_config WHERE bot='all' AND scope=? AND key=?",
                (scope, company),
            )
            row = cursor.fetchone()
            if row:
                return json.loads(row["to_addrs"] or "[]"), json.loads(row["cc_addrs"] or "[]")
            return [], []
        finally:
            conn.close()


def get_train_companies(train_no: str) -> List[str]:
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            import json
            cursor = conn.execute(
                "SELECT extra FROM bot_config WHERE bot='tracking' AND scope='train' AND key=?",
                (train_no,),
            )
            row = cursor.fetchone()
            if row and row["extra"]:
                extra = json.loads(row["extra"])
                return extra.get("companies", [])
            return []
        finally:
            conn.close()


def get_company_default_recipients(company: str) -> Tuple[List[str], List[str]]:
    return get_recipients(company, "company")


def get_owner_email(company: str) -> Optional[str]:
    return get_responsible_person(company)


def write_dsk_timestamp(box_no: str, field: str, timestamp: str) -> bool:
    # Defense in depth: business-DB writes only happen live, even if a
    # caller forgets the act-layer gate.
    if not config.is_live():
        return False
    with _yxo_lock:
        conn = get_yxo_connection(readonly=False)
        try:
            cursor = conn.execute(
                f"UPDATE records SET {field}=? WHERE 箱号=? AND 状态 <> '退舱' AND COALESCE(is_deleted, 0) = 0",
                (timestamp, box_no),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def write_tracing_log(
    log_id: str,
    train_no: str,
    company: str,
    mail_msg_id: str,
    forward_detail: str,
    log_date: str,
    train_key: str,
) -> bool:
    if not config.is_live():
        return False
    with _yxo_lock:
        conn = get_yxo_connection(readonly=False)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO tracing_log
                   (log_id, train_no, company, mail_msg_id, forward_detail, log_date, train_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (log_id, train_no, company, mail_msg_id, forward_detail, log_date, train_key),
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()


def write_tracing_snapshot(
    train_key: str,
    box_no: Optional[str],
    node: Optional[str],
    status: Optional[str],
    event_time: str,
    source: str,
) -> bool:
    if not config.is_live():
        return False
    with _yxo_lock:
        conn = get_yxo_connection(readonly=False)
        try:
            conn.execute(
                """INSERT INTO tracing_snapshot(train_key, box_no, node, status, event_time, source)
                   SELECT ?,?,?,?,?,?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM tracing_snapshot
                       WHERE train_key=? AND event_time=? AND source=?)""",
                (train_key, box_no, node, status, event_time, source, train_key, event_time, source),
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()


def seed_owner_mapping():
    mapping = {
        "太平洋": "maoxiaoyang@cqtransit.com",
        "港九港铁": "maoxiaoyang@cqtransit.com",
        "东盟": "yangyawen@cqtransit.com",
        "同程配": "yangyawen@cqtransit.com",
        "中欧木业": "hanwenhao@cqtransit.com",
        "沙坪坝": "hanwenhao@cqtransit.com",
        "保时达": "fengqian@cqtransit.com",
        "联运": "fengqian@cqtransit.com",
    }
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            import json
            for company, email in mapping.items():
                conn.execute(
                    """INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra, updated_at)
                       VALUES ('all', 'owner', ?, '[]', '[]', ?, datetime('now'))""",
                    (company, json.dumps({"email": email})),
                )
            conn.commit()
        finally:
            conn.close()


init_bot_config_db()
seed_owner_mapping()


def init_forward_log():
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS forward_log (
                    forward_id TEXT PRIMARY KEY,
                    message_id TEXT, row_key TEXT,
                    account TEXT, folder TEXT, uid INTEGER,
                    subject TEXT, sender TEXT, date_hdr TEXT,
                    raw_hex TEXT, to_list TEXT, cc_list TEXT,
                    sent_by TEXT DEFAULT '',
                    sent_at TEXT, bounced INTEGER DEFAULT 0)"""
            )
            conn.commit()
            try:
                conn.execute("ALTER TABLE forward_log ADD COLUMN sent_by TEXT DEFAULT ''")
                conn.commit()
            except Exception:
                pass  # 列已存在（新库建表时自带）
        finally:
            conn.close()


def write_forward_log(forward_id, message_id, row_key, account, folder, uid,
                      subject, sender, date_hdr, raw_bytes, to_list, cc_list,
                      sent_by=""):
    import json
    if not config.is_live():
        return
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO forward_log "
                "(forward_id, message_id, row_key, account, folder, uid, "
                "subject, sender, date_hdr, raw_hex, to_list, cc_list, sent_by, "
                "sent_at, bounced) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),0)",
                (forward_id, message_id, row_key, account, folder, uid, subject, sender,
                 date_hdr, raw_bytes.hex() if raw_bytes else "",
                 json.dumps(to_list, ensure_ascii=False),
                 json.dumps(cc_list, ensure_ascii=False), sent_by))
            conn.commit()
        finally:
            conn.close()


def get_forward_log(forward_id):
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            row = conn.execute("SELECT * FROM forward_log WHERE forward_id=?", (forward_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def mark_forward_bounced(forward_id):
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute("UPDATE forward_log SET bounced=1 WHERE forward_id=?", (forward_id,))
            conn.commit()
        finally:
            conn.close()


def init_bounce_handled():
    """本地 handled 索引（D1）：(account, uid) 主键，替代 Seen 做幂等。"""
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS bounce_handled (
                    account TEXT NOT NULL, uid INTEGER NOT NULL,
                    message_id TEXT, forward_id TEXT, outcome TEXT,
                    handled_at TEXT NOT NULL,
                    PRIMARY KEY (account, uid))"""
            )
            conn.commit()
        finally:
            conn.close()


def _with_handled_table(fn, *args):
    """bounce_handled 缺表时自动建表后重试一次（旧库/裸库容错）。"""
    import sqlite3
    try:
        return fn(*args)
    except sqlite3.OperationalError:
        init_bounce_handled()
        return fn(*args)


def _is_bounce_handled(account, uid):
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM bounce_handled WHERE account=? AND uid=?",
                (account, uid)).fetchone()
            return row is not None
        finally:
            conn.close()


def is_bounce_handled(account, uid):
    return _with_handled_table(_is_bounce_handled, account, uid)


def _mark_bounce_handled(account, uid, message_id="", forward_id="", outcome=""):
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bounce_handled "
                "(account, uid, message_id, forward_id, outcome, handled_at) "
                "VALUES (?,?,?,?,?,datetime('now'))",
                (account, uid, message_id, forward_id, outcome))
            conn.commit()
        finally:
            conn.close()


def mark_bounce_handled(account, uid, message_id="", forward_id="", outcome=""):
    _with_handled_table(_mark_bounce_handled, account, uid,
                        message_id, forward_id, outcome)


def purge_old_bounce_handled(days=90):
    """清理 NDR 到达窗口之外的 handled 记录（默认 90 天，随 poll 每轮执行）。"""
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            cur = conn.execute(
                "DELETE FROM bounce_handled WHERE handled_at < datetime('now', ?)",
                ("-%d days" % int(days),))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()


def _forward_log_candidates(recipient, sent_by=None):
    """返回所有 to/cc 含 recipient、未 bounced 的 forward_log 行（rowid 倒序=最新在前）。
    供 NDR 次键关联：调用方据条数判定唯一性。"""
    import json
    out = []
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM forward_log WHERE bounced=0 ORDER BY rowid DESC"
            ).fetchall()
        finally:
            conn.close()
    for r in rows:
        d = dict(r)
        if sent_by and (d.get("sent_by") or "") != sent_by:
            continue
        hit = False
        for col in ("to_list", "cc_list"):
            try:
                addrs = json.loads(d.get(col) or "[]")
            except Exception:
                addrs = []
            if recipient in addrs:
                hit = True
                break
        if hit:
            out.append(d)
    return out


def find_forward_log_by_recipient(recipient, sent_by=None):
    """次键关联：在 to/cc 里找含 recipient 且未 bounced 的最新一行。"""
    cands = _forward_log_candidates(recipient, sent_by)
    return cands[0] if cands else None


init_forward_log()
init_bounce_handled()