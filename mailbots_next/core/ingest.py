import email
import email.policy
import imaplib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from ..config import (
    IMAP_SERVER,
    IMAP_PORT,
    DEFAULT_ACCOUNTS,
    IDLE_GROUPS,
    MARK_SEEN_FLUSH_SEC,
    MARK_SEEN_BATCH_CAP,
    get_accounts,
)
from .. import config
from .log import get_logger
from .notify import increment_counter
from .notify import increment_counter

_log = get_logger(__name__)


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
    single STORE with all uids ("1,2,3"). Login frequency drops to at most
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
                typ, _ = conn.store(seq, "+FLAGS", "\\Seen")
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


class Idler:
    def __init__(
        self,
        account: str,
        password: str,
        folders: List[Tuple[str, str]],
        on_raw: Callable[[str, str, str, str, int, str, str, str, bytes], None],
        state_path: str,
        max_idle: int = 1740,
        poll_fallback_secs: int = 0,
    ):
        self.account = account
        self.password = password
        self.folders = folders
        self.on_raw = on_raw
        self.state_path = state_path
        self.max_idle = max_idle
        self.poll_fallback_secs = poll_fallback_secs
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

    def _process_new_messages(self, conn: imaplib.IMAP4_SSL, folder_name: str, folder_utf7: str):
        res, data = conn.search(None, "UNSEEN")
        if res != "OK":
            return
        for uid_bytes in data[0].split():
            if self._stop.is_set():
                break
            try:
                uid = int(uid_bytes)
                _, msg_data = conn.fetch(uid_bytes, "(RFC822)")
                raw_bytes = msg_data[0][1]
                msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
                message_id = msg.get("Message-ID", "").strip()
                if not message_id:
                    import hashlib
                    message_id = f"SYN::{hashlib.sha256(f'{self.account}|{folder_name}|{uid}'.encode()).hexdigest()[:32]}"
                subject = msg.get("Subject", "")
                sender = msg.get("From", "")
                date_hdr = msg.get("Date", "")
                self.on_raw(self.account, folder_name, folder_utf7, message_id, uid, subject, sender, date_hdr, raw_bytes)
            except Exception as e:
                _log.error(f"[{self.account}] Failed to process uid={uid_bytes}: {e}")

    def _idle_loop(self):
        while not self._stop.is_set():
            try:
                conn = self._connect()
                for folder_name, folder_utf7 in self.folders:
                    if self._stop.is_set():
                        break
                    self._select_folder(conn, folder_utf7)
                    _log.log_imap(self.account, folder_name, "idle_start")
                    if self.poll_fallback_secs > 0:
                        while not self._stop.is_set():
                            self._process_new_messages(conn, folder_name, folder_utf7)
                            time.sleep(self.poll_fallback_secs)
                    else:
                        self._idle_once(conn, folder_name, folder_utf7)
                conn.close()
                conn.logout()
            except Exception as e:
                _log.error(f"[{self.account}] IDLE error: {e}")
                time.sleep(10)

    def _idle_once(self, conn: imaplib.IMAP4_SSL, folder_name: str, folder_utf7: str):
        """One IDLE wait cycle on an already-selected folder: idle, wait,
        always close the IDLE state with idle_done(), then process news."""
        conn.idle()
        try:
            conn.idle_check(timeout=self.max_idle)
        except Exception:
            pass
        finally:
            try:
                conn.idle_done()
            except Exception:
                pass
        self._process_new_messages(conn, folder_name, folder_utf7)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._idle_loop, name=f"Idler-{self.account}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)


class IngestManager:
    def __init__(self):
        self.idlers: List[Idler] = []
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
                idler = Idler(
                    account=account,
                    password=password,
                    folders=folders,
                    on_raw=self._on_raw,
                    state_path="",
                    poll_fallback_secs=0,
                )
                self.idlers.append(idler)
                idler.start()
        _log.info(f"Started {len(self.idlers)} IDLE connections")

    def stop(self):
        for idler in self.idlers:
            idler.stop()
        _log.info("All IDLE connections stopped")