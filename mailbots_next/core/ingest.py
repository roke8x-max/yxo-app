import email
import email.policy
import imaplib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import (
    IMAP_SERVER,
    IMAP_PORT,
    DEFAULT_ACCOUNTS,
    IDLE_GROUPS,
    MARK_SEEN_FLUSH_SEC,
    MARK_SEEN_BATCH_CAP,
    FORWARD_SINCE,
    INGEST_POLL_SEC,
    IMAP_STATE_PATH,
    get_accounts,
)
from .. import config
from .log import get_logger
from .notify import increment_counter

_log = get_logger(__name__)


_IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_since(date_iso: str) -> str:
    """'2026-09-08' -> '08-Sep-2026' (IMAP SEARCH SINCE requires DD-MMM-YYYY)."""
    y, m, d = date_iso.split("-")
    return f"{int(d):02d}-{_IMAP_MONTHS[int(m) - 1]}-{y}"


def _server_folder(folder: str) -> str:
    """Resolve a display folder name to its IMAP server (modified-UTF7) form
    via IDLE_GROUPS; unknown names pass through untouched."""
    for group in IDLE_GROUPS.values():
        names = group.get("folders", [])
        encoded = group.get("folder_utf7", [])
        if folder in names:
            return encoded[names.index(folder)]
    return folder


class MarkSeenBatcher:
    """Batch mark-seen (scheme B): aggregate (account, folder, uid) marks and
    flush them every MARK_SEEN_FLUSH_SEC (or when a bucket hits the cap).

    One short IMAP connection per (account, folder) bucket per flush, a
    single UID STORE with all uids ("1,2,3"). Login frequency drops to at most
    one connection per account per flush window, and no extra long-lived
    connections are added. Failures retry once, then WARN + counter; never
    raises, never touches error_queue.
    """

    def __init__(self, flush_sec=MARK_SEEN_FLUSH_SEC, batch_cap=MARK_SEEN_BATCH_CAP):
        self._buckets = {}
        self._lock = threading.Lock()
        self._flush_sec = flush_sec
        self._cap = batch_cap
        self._stop = threading.Event()
        self._thread = None

    def enqueue(self, account: str, folder: str, uid: int) -> bool:
        if not config.is_live() or not uid:
            return False
        with self._lock:
            bucket = self._buckets.setdefault((account, folder), [])
            if uid not in bucket:
                bucket.append(uid)
            full = len(bucket) >= self._cap
            early = list(bucket) if full else []
            if full:
                del self._buckets[(account, folder)]
        if full:
            # Dropped on failure by design (see _flush_bucket): a lost Seen
            # flag only affects unread display, never business correctness.
            self._flush_bucket(account, folder, early)
        return True

    def _take_all(self):
        with self._lock:
            taken = self._buckets
            self._buckets = {}
            return taken

    def _flush_bucket(self, account: str, folder: str, uids) -> bool:
        uids = sorted(set(uids))
        if not uids:
            return True
        password = get_accounts().get(account, "")
        if not password:
            _log.warning(
                f"Mark-seen batch skipped, no credentials | folder={folder} uids={len(uids)}"
            )
            increment_counter("mark_seen_failed")
            return False
        server_folder = _server_folder(folder)
        seq = ",".join(str(u) for u in uids)
        last_error = None
        for _ in range(2):
            conn = None
            try:
                conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=60)
                conn.login(account, password)
                typ, _ = conn.select(server_folder)
                if typ != "OK":
                    raise RuntimeError(f"Cannot select folder {server_folder}")
                # 真 UID 语义：序号会随 expunge 漂移，必须用 UID STORE。
                typ, _ = conn.uid("STORE", seq, "+FLAGS", "(\\Seen)")
                if typ != "OK":
                    raise RuntimeError(f"store \\Seen rejected for uids={seq}")
                _log.log_imap(account, folder, "mark_seen_batch", f"uids={seq}")
                return True
            except Exception as e:
                last_error = e
            finally:
                if conn is not None:
                    try:
                        conn.logout()
                    except Exception:
                        pass
        _log.warning(
            f"Mark-seen batch failed after retry | folder={folder} uids={seq} | error={last_error}"
        )
        increment_counter("mark_seen_failed")
        return False

    def flush(self) -> dict:
        """Flush all pending buckets now. Returns {"buckets","uids","failed"}."""
        taken = self._take_all()
        stats = {"buckets": 0, "uids": 0, "failed": 0}
        for (account, folder), uids in taken.items():
            stats["buckets"] += 1
            stats["uids"] += len(uids)
            if not self._flush_bucket(account, folder, uids):
                stats["failed"] += 1
        return stats

    def _run(self):
        while not self._stop.wait(self._flush_sec):
            try:
                self.flush()
            except Exception as e:
                _log.error(f"Mark-seen batcher error: {e}")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="MarkSeenBatcher", daemon=True)
        self._thread.start()
        _log.info("Mark-seen batcher started")

    def stop(self):
        self._stop.set()
        stats = self.flush()
        if self._thread:
            self._thread.join(timeout=10)
        _log.info(f"Mark-seen batcher stopped, flushed leftovers: {stats}")
        return stats


_batcher = None
_batcher_lock = threading.Lock()


def get_batcher() -> MarkSeenBatcher:
    global _batcher
    with _batcher_lock:
        if _batcher is None:
            _batcher = MarkSeenBatcher()
        return _batcher


def enqueue_mark_seen(account: str, folder: str, uid: int) -> bool:
    """Queue one mail for batch marking. Non-blocking; MODE/uid guards kept."""
    return get_batcher().enqueue(account, folder, uid)


def flush_mark_seen() -> dict:
    """Flush all pending mark-seen batches now (shutdown path, tests)."""
    return get_batcher().flush()


def mark_seen(account: str, folder: str, uid: int) -> bool:
    """Enqueue one mail for batch marking (scheme B). Live-only UX aid —
    dedup stays the source of truth, never the Seen flag.

    Forward-success paths call this *after* sending, never before.
    """
    if not config.is_live():
        _log.info(f"Mark-seen skipped (test mode) | folder={folder} uid={uid}")
        return False
    if not uid:
        return False
    return enqueue_mark_seen(account, folder, uid)


@dataclass
class MailEvent:
    account: str
    folder: str
    folder_utf7: str
    message_id: str
    uid: int
    subject: str
    sender: str
    date_hdr: str
    raw_bytes: bytes
    email_type: str = ""


# ---- UID 水位线状态（本地 JSON，原子写） ----
_state_lock = threading.Lock()


def _resolve_state_path(state_path: str = "") -> Path:
    if state_path:
        return Path(state_path)
    return Path(IMAP_STATE_PATH)


def _state_key(account: str, folder: str) -> str:
    return f"{account}|{folder}"


def _load_state(state_path: str = "") -> Dict[str, Any]:
    p = _resolve_state_path(state_path)
    try:
        if not p.exists():
            return {}
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        _log.warning(f"IMAP state load failed, rescan constrained | path={p}: {e}")
        return {}


def _save_state_atomic(state_path: str, data: Dict[str, Any]) -> None:
    p = _resolve_state_path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _get_uidvalidity(conn: imaplib.IMAP4_SSL) -> Optional[int]:
    """从 untagged_responses 取 UIDVALIDITY（select 返回值不含它）。

    实测：conn.untagged_responses['UIDVALIDITY'] == [b'1']（bytes 列表）。
    取不到时返回 None，调用方按“已变化”保守重扫（仍受 FORWARD_SINCE 约束）。
    """
    try:
        vals = (conn.untagged_responses or {}).get("UIDVALIDITY")
        if vals:
            raw = vals[0]
            if isinstance(raw, bytes):
                return int(raw.decode("utf-8", "replace").strip().split()[0])
            return int(str(raw).strip().split()[0])
    except Exception:
        pass
    try:
        typ, vals = conn.response("UIDVALIDITY")
        if vals:
            raw = vals[0]
            if isinstance(raw, bytes):
                return int(raw.decode("utf-8", "replace").strip().split()[0])
            return int(str(raw).strip().split()[0])
    except Exception:
        pass
    return None


def _parse_uid_search(data) -> List[int]:
    out: List[int] = []
    if not data:
        return out
    for chunk in data:
        if not chunk:
            continue
        if isinstance(chunk, bytes):
            parts = chunk.split()
        elif isinstance(chunk, (list, tuple)):
            for sub in chunk:
                if isinstance(sub, bytes):
                    out.extend(int(x) for x in sub.split() if x.isdigit())
            continue
        else:
            continue
        for x in parts:
            try:
                out.append(int(x))
            except Exception:
                continue
    return sorted(set(out))


def _extract_raw_from_fetch(msg_data) -> Optional[bytes]:
    if not msg_data:
        return None
    for item in msg_data:
        if isinstance(item, tuple) and len(item) >= 2:
            payload = item[1]
            if isinstance(payload, (bytes, bytearray)) and payload:
                return bytes(payload)
        elif isinstance(item, bytes) and len(item) > 0:
            # 某些服务器直接回字节块（无 tuple 包装）时兜底
            continue
    return None


class Poller:
    """短周期轮询收信器：每轮遍历组内所有文件夹，用 UID 水位线发现新邮件。

    唯一收信机制（IDLE 已彻底删除）。重连结构保留：
    外层 while not stop + try/except + _connect() + close/logout +
    异常后 sleep(10) 重连。
    """

    def __init__(
        self,
        account: str,
        password: str,
        folders: List[Tuple[str, str]],
        on_raw: Callable[..., Any],
        state_path: str = "",
        poll_secs: int = 30,
        forward_since: str = "",
    ):
        self.account = account
        self.password = password
        self.folders = folders
        self.on_raw = on_raw
        self.state_path = state_path
        if poll_secs is None or poll_secs <= 0:
            _log.warning(f"poll_secs={poll_secs} invalid, fallback to 30")
            poll_secs = 30
        self.poll_secs = max(1, int(poll_secs))
        self.forward_since = forward_since
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        self._current_folder: Optional[str] = None

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=60)
        conn.login(self.account, self.password)
        return conn

    def _select_folder(self, conn: imaplib.IMAP4_SSL, folder_utf7: str):
        res, _ = conn.select(folder_utf7, readonly=True)
        if res != "OK":
            raise RuntimeError(f"Cannot select folder {folder_utf7}")
        self._current_folder = folder_utf7

    def _advance_watermark(self, folder_name: str, uidvalidity: int, uid: int) -> None:
        with _state_lock:
            data = _load_state(self.state_path)
            data[_state_key(self.account, folder_name)] = {
                "uidvalidity": uidvalidity,
                "last_uid": int(uid),
            }
            _save_state_atomic(self.state_path, data)

    def _process_new_messages(self, conn: imaplib.IMAP4_SSL, folder_name: str, folder_utf7: str):
        self._select_folder(conn, folder_utf7)
        validity = _get_uidvalidity(conn)
        if validity is None:
            _log.warning(
                f"UIDVALIDITY missing, conservative rescan | account={self.account} folder={folder_name}"
            )

        with _state_lock:
            saved = _load_state(self.state_path).get(_state_key(self.account, folder_name), {})
        saved_validity = saved.get("uidvalidity")
        last_uid = int(saved.get("last_uid") or 0)
        need_rescan = (
            validity is None
            or saved_validity is None
            or (validity is not None and int(saved_validity) != int(validity))
            or last_uid <= 0
        )

        # 取 UID 列表：水位增量或 FORWARD_SINCE 约束的全量
        try:
            if need_rescan:
                if self.forward_since:
                    typ, data = conn.uid("SEARCH", None, f"SINCE {imap_since(self.forward_since)}")
                else:
                    typ, data = conn.uid("SEARCH", None, "UID 1:*")
                if typ != "OK":
                    return
                uids = _parse_uid_search(data)
                # 重扫同样受 FORWARD_SINCE 门禁：起点不是 UID=1，而是 SINCE 结果的最小 UID
            else:
                typ, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
                if typ != "OK":
                    return
                uids = _parse_uid_search(data)
        except Exception as e:
            _log.error(f"[{self.account}] UID SEARCH failed: {e}")
            return

        if not uids:
            # 无新邮件时仍要把（可能变化的）validity 落盘，避免每轮重扫
            if validity is not None and need_rescan:
                self._advance_watermark(folder_name, validity, last_uid)
            return

        # FORWARD_SINCE 客户端双重校验 + historical_skip（语义不动）
        since_dt = None
        if self.forward_since:
            try:
                from datetime import datetime, timezone
                since_dt = datetime.strptime(self.forward_since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                _log.warning(f"Invalid FORWARD_SINCE format: {self.forward_since}, skipping client-side filter")
                since_dt = None

        for uid in uids:
            if self._stop.is_set():
                break
            try:
                typ, msg_data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                if typ != "OK":
                    _log.warning(f"UID FETCH rejected | account={self.account} folder={folder_name} uid={uid} typ={typ}")
                    increment_counter("uid_fetch_empty")
                    continue
                raw_bytes = _extract_raw_from_fetch(msg_data)
                if not raw_bytes:
                    _log.warning(f"UID FETCH empty payload | account={self.account} folder={folder_name} uid={uid}")
                    increment_counter("uid_fetch_empty")
                    continue
                msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
                message_id = msg.get("Message-ID", "").strip()
                if not message_id:
                    import hashlib
                    message_id = f"SYN::{hashlib.sha256(f'{self.account}|{folder_name}|{uid}'.encode()).hexdigest()[:32]}"
                subject = msg.get("Subject", "")
                sender = msg.get("From", "")
                date_hdr = msg.get("Date", "")

                if since_dt:
                    from email.utils import parsedate_to_datetime
                    try:
                        msg_date = parsedate_to_datetime(date_hdr)
                        if msg_date and msg_date < since_dt:
                            increment_counter("historical_skip")
                            _log.info(f"Historical skip | uid={uid} date={date_hdr} since={self.forward_since}")
                            # 历史门禁之前的邮件视为“已处理终态”，推进水位避免每轮重扫
                            if validity is not None:
                                self._advance_watermark(folder_name, validity, uid)
                            continue
                    except Exception:
                        _log.warning(f"Could not parse Date header, allowing through | uid={uid} date={date_hdr}")

                try:
                    ok = self.on_raw(self.account, folder_name, folder_utf7, message_id, uid, subject, sender, date_hdr, raw_bytes)
                except Exception as e:
                    # 致命异常专用兜底：正常路径（含部分行已入队）不经过这里。
                    # 兜底入队须幂等检查，否则与内部已入队的行重复。
                    _log.error(f"[{self.account}] Poll process fatal | uid={uid}: {type(e).__name__}: {e}")
                    increment_counter("poll_process_fatal")
                    try:
                        from .dedup import add_error, has_pending_message
                        from .notify import get_notifier
                        from ..config import OPS_OWNER_EMAIL
                        enqueued = False
                        if has_pending_message(message_id):
                            enqueued = True
                        else:
                            eid = _log.error(f"Poll fallback enqueue | uid={uid}", error_id=None)
                            add_error(message_id, "", "ingest", "INGEST_EXCEPTION",
                                      f"{type(e).__name__}: {e}", eid,
                                      account=self.account, folder=folder_name, uid=uid,
                                      subject=subject, sender=sender, date_hdr=date_hdr,
                                      raw_bytes=raw_bytes)
                            enqueued = True
                        try:
                            get_notifier().send_program_error(OPS_OWNER_EMAIL, "poll-fatal", f"Poll fatal, fallback enqueued: uid={uid} {type(e).__name__}")
                        except Exception:
                            pass
                        if enqueued and validity is not None:
                            self._advance_watermark(folder_name, validity, uid)
                    except Exception as ee:
                        _log.error(f"[{self.account}] Poll fallback enqueue failed | uid={uid}: {ee}")
                    continue

                if ok:
                    if validity is not None:
                        self._advance_watermark(folder_name, validity, uid)
                else:
                    # 正常返回但未纳入持久化（理论上不应出现）：不推进 + ERROR + 告警
                    _log.error(f"[{self.account}] Poll process not persisted, watermark held | uid={uid}")
                    increment_counter("poll_not_persisted")
                    try:
                        from .notify import get_notifier
                        from ..config import OPS_OWNER_EMAIL
                        get_notifier().send_program_error(OPS_OWNER_EMAIL, "poll-not-persisted", f"process returned False, watermark held: uid={uid}")
                    except Exception:
                        pass
            except Exception as e:
                _log.error(f"[{self.account}] Failed to process uid={uid}: {e}")

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                conn = self._connect()
                try:
                    for folder_name, folder_utf7 in self.folders:
                        if self._stop.is_set():
                            break
                        self._select_folder(conn, folder_utf7)
                        _log.log_imap(self.account, folder_name, "poll_start")
                        self._process_new_messages(conn, folder_name, folder_utf7)
                    if not self._stop.is_set():
                        time.sleep(self.poll_secs)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    try:
                        conn.logout()
                    except Exception:
                        pass
            except Exception as e:
                _log.error(f"[{self.account}] poll error: {e}")
                time.sleep(10)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name=f"Poller-{self.account}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)


# 改名说明：旧类名已彻底删除，此处不保留任何别名 —— 名字承诺不存在的东西正是本次事故根因。


def fetch_raw_by_uid(account: str, folder: str, uid: int) -> Optional[bytes]:
    """按 UID 从 IMAP 重取原文（大邮件重放：error_queue/forward_log 不存全文时兜底）。
    任何异常 → WARN + None。"""
    try:
        password = get_accounts().get(account, "")
        if not password:
            _log.warning(f"Refetch skipped, no credentials | account={account}")
            return None
        conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=60)
        try:
            conn.login(account, password)
            typ, _ = conn.select(_server_folder(folder), readonly=True)
            if typ != "OK":
                raise RuntimeError(f"Cannot select folder {folder}")
            _, msg_data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            return _extract_raw_from_fetch(msg_data)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as e:
        _log.warning(f"Refetch failed | account={account} folder={folder} uid={uid}: {e}")
        return None


class IngestManager:
    """死代码（保留仅防旧引用）：实际入口为 serve.start_pollers。"""

    def __init__(self):
        self.pollers: List[Poller] = []
        self._accounts = get_accounts()
        self._enabled_types = set()

    def set_enabled_types(self, types: List[str]):
        self._enabled_types = set(types)

    def _on_raw(self, account: str, folder: str, folder_utf7: str, message_id: str, uid: int, subject: str, sender: str, date_hdr: str, raw_bytes: bytes):
        _log.log_email_received(message_id, folder, sender, subject)

    def start(self):
        for group_name, group_config in IDLE_GROUPS.items():
            folders = list(zip(group_config["folders"], group_config["folder_utf7"]))
            for account in DEFAULT_ACCOUNTS:
                password = self._accounts.get(account)
                if not password:
                    _log.warning(f"No password for account {account}, skipping")
                    continue
                poller = Poller(
                    account=account,
                    password=password,
                    folders=folders,
                    on_raw=self._on_raw,
                    state_path="",
                    poll_secs=INGEST_POLL_SEC,
                    forward_since=FORWARD_SINCE,
                )
                self.pollers.append(poller)
                poller.start()
        _log.info(f"Started {len(self.pollers)} ingest pollers (poll {INGEST_POLL_SEC}s)")

    def stop(self):
        for poller in self.pollers:
            poller.stop()
        _log.info("All ingest pollers stopped")
