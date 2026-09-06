#!/usr/bin/env python3
import argparse
import email
import email.policy
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import List, Dict, Any, Optional
import json

from mailbots_next import config
from mailbots_next.config import (
    DEFAULT_ACCOUNTS,
    IDLE_GROUPS,
    IDLE_PER_FOLDER,
    MAX_IDLE_SEC,
    INBOUND_PORT,
    INBOUND_SHARED_SECRET,
    OPS_OWNER_EMAIL,
    EmailType,
    get_accounts,
    is_type_enabled,
    get_idle_groups,
    snapshot,
    ConfigSnapshot,
)
from mailbots_next.core import (
    Idler,
    extract_email,
    route_row,
    decide,
    execute_action,
    get_notifier,
    try_claim,
    add_error,
    has_pending_error,
    load_records,
    get_responsible_person,
    get_recipients,
    get_sender_by_email,
    increment_counter,
    flush_counters,
    has_pending_error,
    has_pending_message,
    mark_seen,
    enqueue_mark_seen,
    EmailLogger,
    get_connection,
)

_log = EmailLogger("mailbots_next.serve")

_shutdown_event = threading.Event()
_idlers: List[Idler] = []
_inbound_server: Optional[HTTPServer] = None


class MailProcessor:
    def __init__(self, config: ConfigSnapshot):
        self.config = config
        self.records = load_records()
        self.accounts = get_accounts()
        self.notifier = get_notifier()

    def process_email(self, account: str, folder: str, message_id: str, uid: int, subject: str, sender: str, date_hdr: str, raw_bytes: bytes):
        _log.log_email_received(message_id, folder, sender, subject)

        if not message_id:
            import hashlib
            message_id = f"SYN::{hashlib.sha256(f'{account}|{folder}|{uid}'.encode()).hexdigest()[:32]}"

        email_type = self._identify_type(sender, subject, raw_bytes)
        if not email_type:
            _log.log_unclassified_email(message_id, folder, sender, subject)
            error_id = _log.error(f"Unclassified email: folder={folder}, from={sender}, subject={subject[:100]}")
            self.notifier.send_config_missing(OPS_OWNER_EMAIL, error_id, f"Unclassified email: {subject[:100]}")
            return

        if not is_type_enabled(email_type):
            _log.log_type_skipped(message_id, email_type, "Type disabled via config")
            return

        _log.log_type_identified(message_id, email_type, f"sender={sender}, subject={subject[:50]}")

        rows = extract_email(email_type, raw_bytes)
        if not rows:
            _log.warning(f"No rows extracted for {message_id} type={email_type}")
            return

        send_ctx = self._build_send_ctx(email_type, rows, raw_bytes)

        send_ctx = self._build_send_ctx(email_type, rows, raw_bytes)

        has_manual = False
        outcome = {"forwarded": 0, "alarm": 0, "pending": 0, "queued": 0,
                   "manual": 0, "duplicate": 0, "skipped": 0}
        for row in rows:
            row_key = f"{row.row_idx}:{row.container_no or row.customer_code or 'no_key'}"
            try:
                row_outcome = self._process_row(row, row_key, email_type, message_id, raw_bytes,
                                                account, folder, uid, sender, subject, date_hdr,
                                                send_ctx)
                if row_outcome == "manual":
                    has_manual = True
                outcome[row_outcome] = outcome.get(row_outcome, 0) + 1
            except Exception as e:
                # Pure last-resort guard: one row's unexpected failure must
                # never abort the remaining rows of the same email.
                error_id = _log.error(
                    f"Row failed unexpectedly: row={row.row_idx} "
                    f"error={type(e).__name__}: {e}",
                    error_id=None,
                )
                if not has_pending_error(message_id, row_key):
                    add_error(message_id, row_key, "row", "ROW_EXCEPTION",
                              f"{type(e).__name__}: {e}", error_id,
                              account=account, folder=folder, uid=uid,
                              subject=subject, sender=sender, date_hdr=date_hdr,
                              raw_bytes=raw_bytes)
                increment_counter("row_exception")
                outcome["queued"] += 1
                continue

        # End-of-mail mark-seen: only when live, with a real uid, no manual
        # rows, and no unsettled error-queue entries for this message.
        # Mark-seen is a UX aid only — forward-first ordering is guaranteed
        # because this runs after the whole row loop above.
        if config.is_live() and uid and not has_manual \
                and not has_pending_message(message_id):
            mark_seen(account, folder, uid)
        return outcome

    def _build_send_ctx(self, email_type, rows, raw_bytes):
        """Pre-resolve per-row companies for multi-target split sending.

        Returns None for single-target mails (legacy whole-mail path, zero
        behavior change) and for tracing (per-company full copies are already
        correct). Otherwise a dict with group keys (company, or
        (company, box) for dsk/atb), the designated sending row per key, the
        row dicts, per-company recipients, and the parsed mail parts.
        """
        from mailbots_next.core.extract import parse_email
        from mailbots_next.core.extractors.dsk import DSKExtractor
        import email as email_mod

        if email_type == EmailType.TRACING or len(rows) <= 1:
            return None
        per_box = (email_type == EmailType.DSK or email_type == EmailType.ATB)
        keys, companies_seen = {}, []
        for row in rows:
            routing = route_row(email_type, row, self.records)
            company = routing.company if getattr(routing, "success", False) else None
            if not company:
                continue
            box = (row.container_no or "") if per_box else ""
            key = (company, box) if per_box else company
            keys[row.row_idx] = key
            if company not in companies_seen:
                companies_seen.append(company)
        uniq_keys = []
        for idx in sorted(keys):
            if keys[idx] not in uniq_keys:
                uniq_keys.append(keys[idx])
        if len(uniq_keys) <= 1:
            return None
        designated = {}
        for idx in sorted(keys):
            designated.setdefault(keys[idx], idx)
        group_rows = {k: [] for k in uniq_keys}
        for row in rows:
            key = keys.get(row.row_idx)
            if key is None:
                continue
            wb_row = (row.raw_data.get("waybill_row") or {}) if isinstance(row.raw_data, dict) else {}
            group_rows[key].append({
                "客户编码": row.customer_code or "",
                "箱号": row.container_no or "",
                "运单号": wb_row.get("运单号", ""),
                "company": key[0] if per_box else key,
            })
        company_routes = {c: get_recipients(c) for c in companies_seen}
        ctx = {
            "email_type": str(email_type),
            "multi": True,
            "keys": keys,
            "designated": designated,
            "group_rows": group_rows,
            "company_routes": company_routes,
            "records": self.records,
            "all_boxes": set(),
            "html_body": None,
            "attachments": [],
            "container_rows": [],
        }
        if per_box:
            msg = email_mod.message_from_bytes(raw_bytes, policy=email_mod.policy.default)
            parsed = parse_email(msg)
            ctx["html_body"] = parsed.get("html_body")
            ctx["attachments"] = parsed.get("attachments", [])
            ctx["container_rows"] = DSKExtractor()._extract_container_table(ctx["html_body"])
            ctx["all_boxes"] = {r.container_no for r in rows if r.container_no}
        return ctx

    def _process_row(self, row, row_key, email_type, message_id, raw_bytes,
                     account, folder, uid, sender, subject, date_hdr, send_ctx=None):
        """Process one extracted row: claim, manual-skip, route, act.

        Returns the row outcome: "duplicate" | "manual" | "forwarded" |
        "alarm" | "pending" | "queued" | "skipped". "queued" means this row
        produced (or kept) an error-queue entry and still needs attention."""
        full_msg_id = f"{message_id}|{row_key}"

        if not try_claim(full_msg_id):
            _log.log_dedup_claim(full_msg_id, False, row_key)
            return "duplicate"

        _log.log_dedup_claim(full_msg_id, True, row_key)

        # Manual categories leave it to humans: skip before routing so
        # they never enter the error queue (留人工，不标已读).
        if getattr(row, "draft_category", None) in ("C2", "W", "OTHER"):
            _log.info(
                f"Manual skip | msg_id={message_id[:50]} | row={row.row_idx} | "
                f"category={row.draft_category} | left unread for humans"
            )
            return "manual"

        routing = route_row(email_type, row, self.records)
        outcomes = []
        if isinstance(routing, list):
            for r in routing:
                outcomes.append(self._process_routing_result(r, row, email_type, message_id, row_key, raw_bytes, account, folder, uid, sender, subject, date_hdr, send_ctx))
        else:
            outcomes.append(self._process_routing_result(routing, row, email_type, message_id, row_key, raw_bytes, account, folder, uid, sender, subject, date_hdr, send_ctx))
        # A multi-company fan-out row is only as good as its worst outcome.
        for terminal in ("queued", "forwarded", "alarm", "pending", "skipped"):
            if terminal in outcomes:
                return terminal
        return "queued"

    def _identify_type(self, sender: str, subject: str, raw_bytes: bytes) -> Optional[str]:
        from mailbots_next.config import TYPE_ROUTES, ENC_PDF_RE, BOX_PDF_RE, UPDATE_KEYWORDS
        import re

        sender_lower = sender.lower() if sender else ""

        for route in TYPE_ROUTES:
            match = route["match"]
            if "sender_whitelist" in match:
                for wl in match["sender_whitelist"]:
                    if wl.lower() in sender_lower:
                        return route["type"].value
            if "attachment_pattern" in match:
                msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
                att_names = []
                for part in msg.walk():
                    if part.is_multipart():
                        continue
                    fn = part.get_filename()
                    if fn:
                        att_names.append(fn)
                pattern = match["attachment_pattern"]
                for fn in att_names:
                    if re.search(pattern, fn, re.I):
                        excluded = False
                        for ex in match.get("sender_exclude", []):
                            if ex.lower() in sender_lower:
                                excluded = True
                                break
                        if not excluded:
                            return route["type"].value
        return None

    def _process_routing_result(self, routing, row, email_type, message_id, row_key, raw_bytes, account, folder, uid, sender, subject, date_hdr, send_ctx=None):
        from mailbots_next.core import Decision

        if not routing.success:
            # Tracing zero-hit is a normal business branch (type recognized,
            # just no company owns these boxes): INFO only, no forward, no
            # alarm, no error-queue entry.
            if email_type == EmailType.TRACING and routing.route_source == "no_company_match":
                _log.info(
                    f"Tracing zero hit | msg_id={message_id[:50]} | row={row.row_idx} | "
                    f"train={getattr(row, 'train_no', '')} box={getattr(row, 'container_no', '')} | "
                    "no responsible company, skipped without alert"
                )
                increment_counter("tracing_zero_hit")
                return "skipped"
            _log.log_routing_failed(message_id, row.row_idx, routing.route_source)
            error_id = _log.error(f"Routing failed: {routing.route_source}", error_id=None)
            add_error(message_id, row_key, "routing", "NO_ROUTE", routing.route_source, error_id,
                      account=account, folder=folder, uid=uid,
                      subject=subject, sender=sender, date_hdr=date_hdr,
                      raw_bytes=raw_bytes)
            increment_counter("routing_failed")
            # E2 config-missing: company resolved but no recipients configured —
            # log + alert the ops owner instead of silently skipping.
            if routing.company:
                self.notifier.send_config_missing(
                    OPS_OWNER_EMAIL,
                    error_id,
                    f"E2 missing recipients for company {routing.company} "
                    f"(type={email_type} source={routing.route_source} subject={subject[:100]})",
                )
            return "queued"

        _log.log_routing(message_id, row.row_idx, routing.company, routing.responsible_person or "", routing.to_list, routing.cc_list)

        decision = decide(email_type, row, routing, self.records)
        _log.log_decide(message_id, row.row_idx, decision.tier, decision.action, decision.reason)

        sender_info = None
        if routing.responsible_person:
            sender_info = get_sender_by_email(routing.responsible_person)
        if not sender_info:
            sender_info = (account, self.accounts.get(account, ""))

        sender_email, sender_password = sender_info

        # E2 missing-credentials: no SMTP password for the sender account —
        # never attempt the login, alert the ops owner instead.
        if not sender_password:
            eid = _log.error(
                f"Missing SMTP credentials for sender {sender_email or account}",
                error_id=None,
            )
            add_error(message_id, row_key, "act", "MISSING_CREDENTIALS",
                      f"No SMTP password for {sender_email or account}", eid,
                      account=account, folder=folder, uid=uid,
                      subject=subject, sender=sender, date_hdr=date_hdr,
                      raw_bytes=raw_bytes)
            increment_counter("missing_credentials")
            self.notifier.send_config_missing(
                OPS_OWNER_EMAIL,
                eid,
                f"E2 missing SMTP credentials for {sender_email or account} "
                f"(type={email_type} subject={subject[:100]})",
            )
            return "queued"

        success, detail = execute_action(decision, row, routing, raw_bytes, sender_email, sender_password, message_id, send_ctx)

        if success:
            _log.log_action(message_id, row.row_idx, decision.action, detail)
            increment_counter(f"action_{decision.action}")

            # Group-covered rows ride on their company's group mail: no
            # extra notify, no extra send.
            if detail == "forwarded" and decision.action == "forward":
                if email_type == "draft" and row.draft_category == "B":
                    self.notifier.send_draft_update(
                        routing.responsible_person or "",
                        OPS_OWNER_EMAIL,
                        f"Draft update forwarded: {subject[:100]}"
                    )
                else:
                    self.notifier.send_forwarded(
                        routing.responsible_person or "",
                        OPS_OWNER_EMAIL,
                        f"Email forwarded: {subject[:100]}"
                    )
            elif decision.action == "alarm":
                self.notifier.send_alarm(
                    routing.responsible_person or "",
                    OPS_OWNER_EMAIL,
                    f"Alarm: {decision.reason} | {subject[:100]}"
                )
            elif decision.action == "pending":
                self.notifier.send_pending(
                    routing.responsible_person or "",
                    OPS_OWNER_EMAIL,
                    f"Pending: {decision.reason} | {subject[:100]}"
                )
            # NB: outcome vocabulary uses "forwarded" (past) while
            # Decision.action uses "forward" — map explicitly.
            if detail == "group-covered":
                return "forwarded"
            return {"forward": "forwarded", "alarm": "alarm",
                    "pending": "pending"}.get(decision.action, "skipped")
        else:
            error_id = _log.error(f"Action failed: {detail}", error_id=None)
            _log.log_action_failed(message_id, row.row_idx, decision.action, detail, error_id)
            add_error(message_id, row_key, "act", "ACTION_FAILED", detail, error_id,
                      account=account, folder=folder, uid=uid,
                      subject=subject, sender=sender, date_hdr=date_hdr,
                      raw_bytes=raw_bytes)
            increment_counter("action_failed")
            return "queued"


def signal_handler(signum, frame):
    _log.info(f"Received signal {signum}, shutting down...")
    _shutdown_event.set()


def start_idle_processors(processor: MailProcessor):
    """Start IDLE listeners. Default topology is one Idler per (folder x
    account) so no folder ever blocks behind another folder's 29-minute
    idle_check; set IDLE_PER_FOLDER=0 to fall back to one Idler per group."""
    global _idlers
    folder_jobs = []
    if IDLE_PER_FOLDER:
        for group_config in IDLE_GROUPS.values():
            for folder, folder_utf7 in zip(group_config["folders"], group_config["folder_utf7"]):
                folder_jobs.append([(folder, folder_utf7)])
    else:
        for group_config in IDLE_GROUPS.values():
            folder_jobs.append(list(zip(group_config["folders"], group_config["folder_utf7"])))
    for folders in folder_jobs:
        for account in DEFAULT_ACCOUNTS:
            password = processor.accounts.get(account)
            if not password:
                _log.warning(f"No password for account {account}, skipping")
                continue

            def callback(acct, folder, folder_utf7, message_id, uid, subject, sender, date_hdr, raw_bytes):
                processor.process_email(acct, folder, message_id, uid, subject, sender, date_hdr, raw_bytes)

            idler = Idler(
                account=account,
                password=password,
                folders=folders,
                on_raw=callback,
                state_path="",
                max_idle=MAX_IDLE_SEC,
                poll_fallback_secs=0,
            )
            _idlers.append(idler)
            idler.start()

    _log.info(f"Started {len(_idlers)} IDLE connections")


def stop_idle_processors():
    global _idlers
    for idler in _idlers:
        idler.stop()
    _idlers.clear()
    _log.info("All IDLE connections stopped")


class InboundHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self):
        if self.path != "/internal/forward":
            self._respond(404, b"Not Found")
            return

        if INBOUND_SHARED_SECRET:
            auth = self.headers.get("X-Forward-Secret", "")
            if auth != INBOUND_SHARED_SECRET:
                self._respond(401, b"Unauthorized")
                return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, b"Invalid JSON")
            return

        error_id = data.get("error_id")
        if not error_id or not isinstance(error_id, int):
            self._respond(400, b"Missing or invalid error_id")
            return

        from mailbots_next.core import claim_manual_forward, reopen_error, get_connection

        # Direct lookup by error_id instead of O(n) iteration.
        # Materialize the Row to a plain dict BEFORE closing: Row field
        # access after conn.close() raises ProgrammingError on some
        # sqlite builds, which would turn every manual forward into a 500.
        conn = get_connection()
        cursor = conn.execute(
            "SELECT * FROM error_queue WHERE id = ?", (error_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            self._respond(404, b"Error record not found or already processed")
            return
        target = dict(row)

        if not claim_manual_forward(error_id):
            self._respond(404, b"Already being processed")
            return

        try:
            processor = MailProcessor(snapshot())
            raw_bytes = bytes.fromhex(target["raw_hex"]) if target["raw_hex"] else b""
            if not raw_bytes:
                raise ValueError("No raw email data stored")

            processor.process_email(
                target["account"] or "",
                target["folder"] or "",
                target["message_id"],
                target["uid"] or 0,
                target["subject"] or "",
                target["sender"] or "",
                target["date"] or "",
                raw_bytes
            )
            self._respond(200, b"Forwarded successfully")
        except Exception as e:
            _log.error(f"Inbound forward failed for error_id={error_id}: {e}")
            reopen_error(error_id)
            self._respond(500, f"Forward failed: {e}".encode())


def start_inbound_server():
    global _inbound_server
    _inbound_server = HTTPServer(("127.0.0.1", INBOUND_PORT), InboundHandler)
    thread = threading.Thread(target=_inbound_server.serve_forever, daemon=True)
    thread.start()
    _log.info(f"Inbound HTTP server started on 127.0.0.1:{INBOUND_PORT}")


def stop_inbound_server():
    global _inbound_server
    if _inbound_server:
        _inbound_server.shutdown()
        _inbound_server = None
        _log.info("Inbound HTTP server stopped")


def sweep_once(processor=None) -> dict:
    """Single sweeper pass over the error queue.

    Each pending entry with a replay payload is really reprocessed through
    the full pipeline. Dedup conflict is resolved by releasing the original
    claim first ((a)): the replay re-claims synchronously in this same
    thread, so a concurrent IDLE pass can only make our claim fail — i.e. a
    wasted cycle, never a double forward. Dedup stays the single source of
    truth; no bypass flag exists anywhere.
    Success closes the entry (+mark-seen enqueue when no sibling rows are
    still pending); failure increments attempts, escalating to manual + one
    E1 at the cap. Entries without payload can't replay: WARN + counter.
    Returns {"retried": n, "escalated": n}; retried counts real replays.
    """
    from mailbots_next.core.dedup import (
        get_pending_errors, get_manual_errors, increment_attempt,
        mark_error_manual, mark_error_resolved, is_error_notified,
        set_error_notified, release_claim,
    )
    from mailbots_next.config import SWEEP_MAX_RETRY

    if processor is None:
        processor = MailProcessor(snapshot())

    stats = {"retried": 0, "escalated": 0}
    for e in get_pending_errors(100):
        raw_hex = e.get("raw_hex") or ""
        account = e.get("account") or ""
        if not raw_hex or not account:
            _log.warning(
                f"Sweep retry skipped, no replay payload | id={e['id']} "
                f"msg_id={e['message_id'][:50]}"
            )
            increment_counter("retry_no_payload")
            increment_attempt(e["id"])
            if e["attempt_count"] >= SWEEP_MAX_RETRY:
                mark_error_manual(e["id"])
                if not is_error_notified(e["id"]):
                    get_notifier().send_program_error(OPS_OWNER_EMAIL, e["error_id"], f"Max retries reached for {e['message_id']}")
                    set_error_notified(e["id"])
                stats["escalated"] += 1
            else:
                stats["retried"] += 1
            continue

        row_key = e.get("row_key") or ""
        release_claim(f"{e['message_id']}|{row_key}" if row_key else e["message_id"])
        try:
            outcome = processor.process_email(
                account, e.get("folder") or "", e["message_id"],
                e.get("uid") or 0, e.get("subject") or "",
                e.get("sender") or "", e.get("date") or "",
                bytes.fromhex(raw_hex),
            ) or {}
        except Exception as exc:
            _log.error(f"Sweep replay crashed: id={e['id']} error={type(exc).__name__}: {exc}")
            outcome = {"queued": 1}
        stats["retried"] += 1
        if outcome.get("queued", 0) > 0:
            increment_attempt(e["id"])
            if e["attempt_count"] >= SWEEP_MAX_RETRY:
                mark_error_manual(e["id"])
                if not is_error_notified(e["id"]):
                    get_notifier().send_program_error(OPS_OWNER_EMAIL, e["error_id"], f"Max retries reached for {e['message_id']}")
                    set_error_notified(e["id"])
                stats["escalated"] += 1
            continue
        if outcome.get("forwarded", 0) + outcome.get("alarm", 0) \
                + outcome.get("pending", 0) + outcome.get("manual", 0) \
                + outcome.get("skipped", 0) == 0:
            # Nothing was actually reprocessed (e.g. duplicate-claimed by a
            # concurrent pass): leave pending without burning an attempt.
            continue
        mark_error_resolved(e["id"])
        if (e.get("uid") or 0) and not has_pending_message(e["message_id"]):
            enqueue_mark_seen(account, e.get("folder") or "", e["uid"])

    for e in get_manual_errors(100):
        # Manual entries were already E1-alerted at escalation time; the daily
        # digest (not the 5-minute sweeper) carries the ongoing reminder.
        if not is_error_notified(e["id"]):
            get_notifier().send_program_error(OPS_OWNER_EMAIL, e["error_id"], f"Manual intervention needed: {e['message_id']} ({e['stage']})")
            set_error_notified(e["id"])
            stats["escalated"] += 1
    return stats


def run_sweeper():
    from mailbots_next.config import SWEEP_INTERVAL_SEC

    while not _shutdown_event.is_set():
        time.sleep(SWEEP_INTERVAL_SEC)
        if _shutdown_event.is_set():
            break

        sweep_once()


def flush_and_notify() -> bool:
    """Flush the daily counters and send the digest. Returns True iff a
    digest was sent (non-empty counters), False when there was nothing."""
    counts = flush_counters()
    if counts:
        get_notifier().send_digest(OPS_OWNER_EMAIL, counts)
        return True
    return False


def _next_digest_time(now: datetime) -> datetime:
    """Next 09:00 digest run at or after `now`. Plain date arithmetic rolls
    over month ends correctly (replace(day=+1) would explode on 1/31)."""
    from mailbots_next.config import DIGEST_HOUR
    next_run = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run = next_run + timedelta(days=1)
    return next_run


def run_digest():
    while not _shutdown_event.is_set():
        now = datetime.now()
        next_run = _next_digest_time(now)
        wait_secs = (next_run - now).total_seconds()
        if wait_secs > 86400:
            wait_secs = 86400

        for _ in range(int(wait_secs)):
            if _shutdown_event.is_set():
                break
            time.sleep(1)

        if _shutdown_event.is_set():
            break

        flush_and_notify()


def main():
    # Argparse first: MAILBOT_MODE must be set before any runtime is_live()
    # check below can observe it (import-time MODE constants stay frozen).
    parser = argparse.ArgumentParser(description="MailBot Next - Unified Email Pipeline")
    parser.add_argument("--live", action="store_true", help="Run in LIVE mode (default: test)")
    parser.add_argument("--poll-secs", type=int, default=0, help="Polling fallback interval (seconds)")
    parser.add_argument("--once", action="store_true", help="Single scan and exit")
    parser.add_argument("--errors", choices=["list", "retry", "resolve"], help="Error queue management")
    parser.add_argument("--error-id", type=int, help="Error ID for retry/resolve")
    args = parser.parse_args()

    os.environ["MAILBOT_MODE"] = "live" if args.live else "test"

    mode = os.environ.get("MAILBOT_MODE", "test").lower()
    if mode not in ("test", "live"):
        print("ERROR: MAILBOT_MODE must be 'test' or 'live'")
        sys.exit(1)

    _log.info(f"Starting MailBot Next in {mode.upper()} mode")

    if args.errors:
        from mailbots_next.core.dedup import get_pending_errors, get_manual_errors, mark_error_resolved
        if args.errors == "list":
            pending = get_pending_errors(100)
            manual = get_manual_errors(100)
            print(f"Pending: {len(pending)}, Manual: {len(manual)}")
            for e in pending + manual:
                print(f"  #{e['id']} [{e['status']}] {e['message_id'][:30]} {e['stage']} attempts={e['attempt_count']}")
        elif args.errors == "retry" and args.error_id:
            mark_error_resolved(args.error_id)
            print(f"Marked #{args.error_id} as resolved (retry)")
        elif args.errors == "resolve" and args.error_id:
            mark_error_resolved(args.error_id)
            print(f"Marked #{args.error_id} as resolved")
        return

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    config = snapshot()
    processor = MailProcessor(config)

    from mailbots_next.core.ingest import get_batcher
    get_batcher().start()

    start_idle_processors(processor)
    start_inbound_server()

    sweeper_thread = threading.Thread(target=run_sweeper, daemon=True)
    sweeper_thread.start()

    digest_thread = threading.Thread(target=run_digest, daemon=True)
    digest_thread.start()

    try:
        while not _shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    _log.info("Shutting down...")
    stop_idle_processors()
    stop_inbound_server()
    from mailbots_next.core.ingest import get_batcher
    get_batcher().stop()
    _log.info("Shutdown complete")


if __name__ == "__main__":
    main()