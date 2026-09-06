import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any

from ..config import DEDUP_DB_PATH, DEDUP_RETENTION_DAYS
from .log import get_logger

_log = get_logger(__name__)
_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DEDUP_DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_connection() -> sqlite3.Connection:
    """Get a connection to the dedup database (for direct queries)."""
    return _connect()


def init_db():
    with _db_lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dedup (
                    message_id TEXT PRIMARY KEY,
                    claimed_at TEXT NOT NULL,
                    claimed_by TEXT
                );
                CREATE TABLE IF NOT EXISTS error_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    row_key TEXT,
                    stage TEXT NOT NULL,
                    error_type TEXT,
                    error_detail TEXT,
                    error_id TEXT,
                    attempt_count INTEGER DEFAULT 0,
                    last_attempt_at TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    raw_hex TEXT,
                    account TEXT,
                    folder TEXT,
                    subject TEXT,
                    sender TEXT,
                    date TEXT,
                    extra TEXT DEFAULT '{}',
                    uid INTEGER DEFAULT 0,
                    UNIQUE(message_id, row_key, stage)
                );
                CREATE INDEX IF NOT EXISTS idx_error_queue_status ON error_queue(status);
                CREATE INDEX IF NOT EXISTS idx_error_queue_message_id ON error_queue(message_id);
                """
            )
            # Migrations for DBs created before these columns existed.
            for _ddl in (
                "ALTER TABLE error_queue ADD COLUMN extra TEXT DEFAULT '{}'",
                "ALTER TABLE error_queue ADD COLUMN uid INTEGER DEFAULT 0",
            ):
                try:
                    conn.execute(_ddl)
                except sqlite3.OperationalError:
                    pass  # column already present
            conn.commit()
        finally:
            conn.close()


def try_claim(message_id: str, row_key: str = "") -> bool:
    if not message_id:
        return False
    full_key = f"{message_id}|{row_key}" if row_key else message_id
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO dedup (message_id, claimed_at, claimed_by) VALUES (?, datetime('now'), ?)",
                (full_key, threading.current_thread().name),
            )
            conn.commit()
            claimed = cursor.rowcount > 0
            _log.log_dedup_claim(message_id, claimed, row_key)
            return claimed
        finally:
            conn.close()


def release_claim(message_id: str, row_key: str = ""):
    if not message_id:
        return
    full_key = f"{message_id}|{row_key}" if row_key else message_id
    with _db_lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM dedup WHERE message_id = ?", (full_key,))
            conn.commit()
        finally:
            conn.close()


def is_claimed(message_id: str, row_key: str = "") -> bool:
    if not message_id:
        return False
    full_key = f"{message_id}|{row_key}" if row_key else message_id
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute("SELECT 1 FROM dedup WHERE message_id = ?", (full_key,))
            return cursor.fetchone() is not None
        finally:
            conn.close()


def claim_manual_forward(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET status='resolved' WHERE id=? AND status IN ('pending','manual')",
                (error_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def _read_extra(conn, error_id: int) -> dict:
    import json
    cursor = conn.execute("SELECT extra FROM error_queue WHERE id=?", (error_id,))
    row = cursor.fetchone()
    if not row or not row["extra"]:
        return {}
    try:
        data = json.loads(row["extra"])
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_error_notified(error_id: int) -> bool:
    """True iff an E1 alert was already sent for this error-queue entry
    (extra.notified_manual_at set)."""
    with _db_lock:
        conn = _connect()
        try:
            return bool(_read_extra(conn, error_id).get("notified_manual_at"))
        finally:
            conn.close()


def set_error_notified(error_id: int) -> bool:
    """Record that the E1 first-alert for this entry has been sent."""
    import json
    from datetime import datetime
    with _db_lock:
        conn = _connect()
        try:
            extra = _read_extra(conn, error_id)
            extra["notified_manual_at"] = datetime.now().isoformat(timespec="seconds")
            cursor = conn.execute(
                "UPDATE error_queue SET extra=? WHERE id=?",
                (json.dumps(extra, ensure_ascii=False), error_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def reopen_error(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET status='manual', attempt_count=0 WHERE id=? AND status='resolved'",
                (error_id,),
            )
            if cursor.rowcount:
                # Reopened entries may alert again on the next sweep.
                extra = _read_extra(conn, error_id)
                extra.pop("notified_manual_at", None)
                import json
                conn.execute(
                    "UPDATE error_queue SET extra=? WHERE id=?",
                    (json.dumps(extra, ensure_ascii=False), error_id),
                )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def add_error(
    message_id: str,
    row_key: str,
    stage: str,
    error_type: str,
    error_detail: str,
    error_id: str,
    account: str = "",
    folder: str = "",
    uid: int = 0,
    subject: str = "",
    sender: str = "",
    date_hdr: str = "",
    raw_bytes: bytes = b"",
) -> int:
    """Queue a failed row for retry, together with its replay payload.

    Extra replay fields are keyword-only with defaults so old 6-argument
    callers keep working. Raw mail over RAW_MAX_BYTES is NOT stored (a
    truncated payload could never replay correctly); that case logs WARN +
    counter instead of silently truncating.
    """
    from .notify import increment_counter
    from ..config import RAW_MAX_BYTES

    raw_hex = ""
    if raw_bytes:
        if len(raw_bytes) > RAW_MAX_BYTES:
            _log.warning(
                f"error_queue payload too large, not stored | msg_id={message_id[:50]} "
                f"| bytes={len(raw_bytes)} limit={RAW_MAX_BYTES}"
            )
            increment_counter("raw_too_large")
        else:
            raw_hex = raw_bytes.hex()
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO error_queue
                   (message_id, row_key, stage, error_type, error_detail, error_id, attempt_count, last_attempt_at, status, created_at,
                    raw_hex, account, folder, uid, subject, sender, date)
                   VALUES (?, ?, ?, ?, ?, ?, 0, datetime('now'), 'pending', datetime('now'),
                           ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, row_key, stage, error_type, error_detail, error_id,
                 raw_hex, account, folder, uid or 0, subject, sender, date_hdr),
            )
            conn.commit()
            if cursor.lastrowid:
                return cursor.lastrowid
            cursor = conn.execute(
                "SELECT id FROM error_queue WHERE message_id=? AND row_key=? AND stage=?",
                (message_id, row_key, stage),
            )
            row = cursor.fetchone()
            return row[0] if row else -1
        finally:
            conn.close()


def has_pending_error(message_id: str, row_key: str) -> bool:
    """True iff a pending error-queue entry already exists for this row.
    Used by the per-row guard so one row failure lands exactly one entry."""
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "SELECT 1 FROM error_queue WHERE message_id=? AND row_key=? AND status='pending'",
                (message_id, row_key),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


def has_pending_message(message_id: str) -> bool:
    """True iff any pending error-queue entry exists for this whole email.
    Used by the end-of-mail mark-seen rule: a mail with unsettled rows
    stays unread."""
    if not message_id:
        return False
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "SELECT 1 FROM error_queue WHERE message_id=? AND status='pending'",
                (message_id,),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


def get_pending_errors(limit: int = 100) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM error_queue WHERE status='pending' ORDER BY created_at LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()


def get_manual_errors(limit: int = 100) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM error_queue WHERE status='manual' ORDER BY created_at LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()


def increment_attempt(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET attempt_count = attempt_count + 1, last_attempt_at = datetime('now') WHERE id=?",
                (error_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def mark_error_resolved(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET status='resolved' WHERE id=?",
                (error_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def mark_error_manual(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET status='manual' WHERE id=?",
                (error_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def purge_old_dedup(days: int = DEDUP_RETENTION_DAYS) -> int:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "DELETE FROM dedup WHERE claimed_at < datetime('now', ?)", (f"-{days} days",)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


init_db()