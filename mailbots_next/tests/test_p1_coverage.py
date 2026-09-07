"""P1 coverage: G3 per-row independence, G6 type identification, G7 runtime
switches, G8 sweeper/digest, G9 credentials, G10 log masking.

Uses the temp-dir databases from conftest (never the real data/ tree).
"""
import json
import sqlite3
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from unittest.mock import Mock, patch

import pytest

from mailbots_next.config import (
    BOT_CONFIG_DB_PATH,
    DEDUP_DB_PATH,
    DRAFT_NUMS_DB_PATH,
    DAILY_COUNTERS_PATH,
    OPS_OWNER_EMAIL,
    EmailType,
    get_enabled_types,
)
from mailbots_next.core.dedup import (
    init_db,
    add_error,
    get_pending_errors,
    get_manual_errors,
)
from mailbots_next.core.store import (
    init_bot_config_db,
    get_bot_config_connection,
    seed_owner_mapping,
)
from mailbots_next.core.extract import ExtractedRow


_SEVEN_COMPANIES = {
    "太平洋": (["tp@test.com"], ["tp_cc@test.com"]),
    "港九港铁": (["gj@test.com"], []),
    "东盟": (["dm@test.com"], []),
    "同程配": (["tcp@test.com"], []),
    "中欧木业": (["zom@test.com"], []),
    "沙坪坝": (["spb@test.com"], []),
    "保时达": (["bsd@test.com"], []),
}


def _seed_recipients():
    conn = get_bot_config_connection()
    try:
        for company, (to, cc) in _SEVEN_COMPANIES.items():
            conn.execute(
                """INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra)
                   VALUES (?, 'company', ?, ?, ?, '{}')""",
                ("all", company, json.dumps(to), json.dumps(cc)),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _p1_dbs():
    for p in (BOT_CONFIG_DB_PATH, DEDUP_DB_PATH, DRAFT_NUMS_DB_PATH, DAILY_COUNTERS_PATH):
        if p.exists():
            try:
                p.unlink()
            except PermissionError:
                pass
    init_db()
    init_bot_config_db()
    seed_owner_mapping()
    _seed_recipients()
    try:
        yield
    finally:
        from mailbots_next.config.provider import invalidate_cache
        invalidate_cache()
        for p in (BOT_CONFIG_DB_PATH, DEDUP_DB_PATH, DRAFT_NUMS_DB_PATH, DAILY_COUNTERS_PATH):
            if p.exists():
                try:
                    p.unlink()
                except PermissionError:
                    pass


def _harness(records):
    """MailProcessor with synthetic records, empty accounts, mocked notifier."""
    from mailbots_next.serve import MailProcessor
    from mailbots_next.config import snapshot
    proc = MailProcessor(snapshot())
    proc.records = records
    proc.accounts = {"t@t.com": "pw-t"}
    proc.notifier = Mock()
    return proc


def _rec(code, box, company="太平洋"):
    return {"id": 1, "客户编码": code, "箱号": box, "company": company,
            "状态": "正常", "is_deleted": 0}


# ---------------------------------------------------------------- G3 按行独立
class TestPerRowIndependence:
    def test_multi_row_one_forward_one_alarm_one_pending(self, monkeypatch):
        """一封三行：T1 照常转发，不被报警行/待办行阻断；队列按行键隔离."""
        monkeypatch.setenv("MAILBOT_MODE", "live")
        proc = _harness([
            _rec("CQWLJT260810001", "CICU1000001"),
            _rec("CQWLJT260810002", "REUSEDBOX"),
            _rec("CQWLJT260810003", "REUSEDBOX", "东盟"),
        ])
        rows = [
            ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                         customer_code="CQWLJT260810001", container_no="CICU1000001"),
            ExtractedRow(row_idx=1, email_type=EmailType.DRAFT,
                         customer_code="CQWLJT299999999-ZZZ", container_no="NOBOX9999"),
            ExtractedRow(row_idx=2, email_type=EmailType.DRAFT,
                         customer_code=None, container_no="REUSEDBOX"),
        ]
        with patch("mailbots_next.serve.extract_email", return_value=rows), \
             patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            mock_smtp.return_value = {"success": True, "refused": {}}
            proc.process_email("t@t.com", "草单", "g3-multi-1", 1,
                               "subj", "docwbfb@yxologistics.com", "", b"raw")
        assert mock_smtp.call_count == 1
        queued = [e for e in get_pending_errors(100) if e["message_id"] == "g3-multi-1"]
        assert len(queued) == 1
        # row_key prefers container_no: row2's key is its (unknown) box.
        assert queued[0]["row_key"] == "1:NOBOX9999"
        proc.notifier.send_forwarded.assert_called_once()
        proc.notifier.send_pending.assert_called_once()
        proc.notifier.send_alarm.assert_not_called()

    def test_row_exception_isolated_continues_next_rows(self):
        """某行 execute_action 抛异常 → 该行进 error_queue，后续行继续处理."""
        proc = _harness([
            _rec("CQWLJT260810001", "CICU1000001"),
            _rec("CQWLJT260810002", "CICU1000002"),
            _rec("CQWLJT260810003", "CICU1000003"),
        ])
        rows = [
            ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                         customer_code="CQWLJT260810001", container_no="CICU1000001"),
            ExtractedRow(row_idx=1, email_type=EmailType.DRAFT,
                         customer_code="CQWLJT260810002", container_no="CICU1000002"),
            ExtractedRow(row_idx=2, email_type=EmailType.DRAFT,
                         customer_code="CQWLJT260810003", container_no="CICU1000003"),
        ]

        def _boom_or_ok(decision, row, *a, **k):
            if row.row_idx == 1:
                raise RuntimeError("boom")
            return (True, "forwarded")

        with patch("mailbots_next.serve.extract_email", return_value=rows), \
             patch("mailbots_next.serve.execute_action", side_effect=_boom_or_ok):
            proc.process_email("t@t.com", "草单", "g3-exc-1", 1,
                               "subj", "docwbfb@yxologistics.com", "", b"raw")
        queued = [e for e in get_pending_errors(100) if e["message_id"] == "g3-exc-1"]
        assert len(queued) == 1
        # row_key prefers container_no.
        assert queued[0]["row_key"] == "1:CICU1000002"
        assert queued[0]["stage"] == "row" and queued[0]["error_type"] == "ROW_EXCEPTION"
        # Rows 0 and 2 were still forwarded despite row 1 exploding.
        assert proc.notifier.send_forwarded.call_count == 2


# ------------------------------------------------------- G6 类型识别
def _tiny_raw(sender, subject, filenames=()):
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = sender
    msg.attach(MIMEText("body", "plain"))
    for fn in filenames:
        att = MIMEBase("application", "pdf")
        att.set_payload(b"x")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename=fn)
        msg.attach(att)
    return msg.as_bytes()


class TestTypeIdentification:
    def _proc(self):
        proc = _harness([])
        return proc

    def test_whitelist_per_type(self):
        proc = self._proc()
        cases = [
            ("docwbfb@yxologistics.com", "waybill"),
            ("atb@yxologistics.com", "atb"),
            ("kasa@rtsb.de", "dsk"),
            ("reex.mala@deutschebahn.com", "dsk"),
            ("tracing-system@yxologistics.com", "tracing"),
        ]
        for sender, want in cases:
            raw = _tiny_raw(sender, "subject")
            assert proc._identify_type(sender, "subject", raw) == want, sender

    def test_draft_attachment_and_sender_exclude(self):
        proc = self._proc()
        # Non-system sender + 箱号 attachment -> draft.
        raw = _tiny_raw("youlia@yxologistics.com", "s", ["清单箱号.pdf"])
        assert proc._identify_type("youlia@yxologistics.com", "s", raw) == "draft"
        # System sender with the same attachment is NOT draft (whitelist wins,
        # sender_exclude keeps system mail out of the draft pipeline).
        raw2 = _tiny_raw("atb@yxologistics.com", "s", ["清单箱号.pdf"])
        got = proc._identify_type("atb@yxologistics.com", "s", raw2)
        assert got == "atb" and got != "draft"

    def test_unclassified_goes_config_missing_only(self):
        proc = self._proc()
        raw = _tiny_raw("stranger@example.com", "hello")
        with patch("mailbots_next.serve.extract_email") as mock_extract, \
             patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            proc.process_email("t@t.com", "草单", "g6-unclassified-1", 1,
                               "hello", "stranger@example.com", "", raw)
        proc.notifier.send_config_missing.assert_called_once()
        assert proc.notifier.send_config_missing.call_args[0][0] == OPS_OWNER_EMAIL
        mock_extract.assert_not_called()
        mock_smtp.assert_not_called()
        assert [e for e in get_pending_errors(100)
                if e["message_id"] == "g6-unclassified-1"] == []

    def test_identification_ignores_folder(self):
        proc = self._proc()
        raw = _tiny_raw("docwbfb@yxologistics.com", "s")
        seen = []
        with patch("mailbots_next.serve.extract_email",
                   side_effect=lambda t, b: seen.append(t) or []):
            proc.process_email("t@t.com", "草单", "g6-f1", 1, "s",
                               "docwbfb@yxologistics.com", "", raw)
            proc.process_email("t@t.com", "运单号", "g6-f2", 2, "s",
                               "docwbfb@yxologistics.com", "", raw)
            proc.process_email("t@t.com", "INBOX", "g6-f3", 3, "s",
                               "docwbfb@yxologistics.com", "", raw)
        assert seen == ["waybill", "waybill", "waybill"]


# ------------------------------------------------------- G7 类型启停开关
def _set_runtime(bot, enabled):
    conn = get_bot_config_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra)"
            " VALUES (?, 'runtime', 'ENABLED', '[]', '[]', ?)",
            (bot, json.dumps({"enabled": enabled})),
        )
        conn.commit()
    finally:
        conn.close()
    from mailbots_next.config.provider import invalidate_cache
    invalidate_cache()


class TestRuntimeSwitches:
    def test_disabled_type_skipped_with_info(self):
        _set_runtime("dsk", False)
        try:
            assert get_enabled_types()["dsk"] is False
            proc = _harness([])
            raw = _tiny_raw("kasa@rtsb.de", "s")
            with patch("mailbots_next.serve._log") as mock_log, \
                 patch("mailbots_next.serve.extract_email") as mock_extract, \
                 patch("mailbots_next.core.act.send_smtp") as mock_smtp:
                proc.process_email("t@t.com", "DSK", "g7-dis-1", 1,
                                   "s", "kasa@rtsb.de", "", raw)
            mock_log.log_type_skipped.assert_called_once()
            mock_extract.assert_not_called()
            mock_smtp.assert_not_called()
            assert [e for e in get_pending_errors(100)
                    if e["message_id"] == "g7-dis-1"] == []
        finally:
            _set_runtime("dsk", True)

    def test_reenable_flows_again(self):
        _set_runtime("dsk", False)
        try:
            assert get_enabled_types()["dsk"] is False
        finally:
            _set_runtime("dsk", True)
        assert get_enabled_types()["dsk"] is True
        proc = _harness([])
        raw = _tiny_raw("kasa@rtsb.de", "s")
        with patch("mailbots_next.serve.extract_email", return_value=[]) as mock_extract:
            proc.process_email("t@t.com", "DSK", "g7-reen-1", 1,
                               "s", "kasa@rtsb.de", "", raw)
        mock_extract.assert_called_once()


# ------------------------------------------------------- G8 sweeper/汇总
def _seed_queue_error(mid, attempt):
    import mailbots_next.core.dedup as dedup_mod
    eid = add_error(mid, "0:X", "act", "T", "detail", f"eid-{mid}")
    conn = dedup_mod.get_connection()
    try:
        conn.execute("UPDATE error_queue SET attempt_count=? WHERE id=?", (attempt, eid))
        conn.commit()
    finally:
        conn.close()
    return eid


class TestSweeper:
    def test_retry_stays_pending(self):
        from mailbots_next.serve import sweep_once
        eid = _seed_queue_error("g8-retry-1", 1)
        with patch("mailbots_next.serve.get_notifier") as mock_get:
            mock_get.return_value = Mock()
            stats = sweep_once()
        assert stats == {"retried": 1, "escalated": 0}
        pend = [e for e in get_pending_errors(100) if e["message_id"] == "g8-retry-1"]
        assert len(pend) == 1 and pend[0]["attempt_count"] == 2
        mock_get.return_value.send_program_error.assert_not_called()

    def test_max_retry_escalates_with_single_E1(self):
        from mailbots_next.serve import sweep_once
        eid = _seed_queue_error("g8-max-1", 3)
        with patch("mailbots_next.serve.get_notifier") as mock_get:
            mock_get.return_value = Mock()
            stats = sweep_once()
        assert stats["escalated"] == 1
        assert [e for e in get_pending_errors(100)
                if e["message_id"] == "g8-max-1"] == []
        assert [e for e in get_manual_errors(100)
                if e["message_id"] == "g8-max-1"] != []
        mock_get.return_value.send_program_error.assert_called_once()
        args = mock_get.return_value.send_program_error.call_args[0]
        assert args[0] == OPS_OWNER_EMAIL

    def test_manual_not_renotified(self):
        from mailbots_next.serve import sweep_once
        eid = _seed_queue_error("g8-once-1", 3)
        with patch("mailbots_next.serve.get_notifier") as mock_get:
            mock_get.return_value = Mock()
            sweep_once()
            assert mock_get.return_value.send_program_error.call_count == 1
            sweep_once()
            assert mock_get.return_value.send_program_error.call_count == 1

    def test_reopen_allows_renotify(self):
        from mailbots_next.serve import sweep_once
        from mailbots_next.core.dedup import claim_manual_forward, reopen_error
        eid = _seed_queue_error("g8-reopen-1", 3)
        with patch("mailbots_next.serve.get_notifier") as mock_get:
            mock_get.return_value = Mock()
            sweep_once()
            assert mock_get.return_value.send_program_error.call_count == 1
            assert claim_manual_forward(eid) is True
            assert reopen_error(eid) is True
            sweep_once()
            assert mock_get.return_value.send_program_error.call_count == 2

    def test_flush_and_notify(self):
        from mailbots_next.serve import flush_and_notify
        from mailbots_next.core.notify import increment_counter, get_counters
        increment_counter("g8k", 2)
        with patch("mailbots_next.serve.get_notifier") as mock_get:
            mock_get.return_value = Mock()
            assert flush_and_notify() is True
        args = mock_get.return_value.send_digest.call_args[0]
        assert args[0] == OPS_OWNER_EMAIL and args[1] == {"g8k": 2}
        assert get_counters() == {}

    def test_flush_and_notify_empty(self):
        from mailbots_next.serve import flush_and_notify
        with patch("mailbots_next.serve.get_notifier") as mock_get:
            mock_get.return_value = Mock()
            assert flush_and_notify() is False
        mock_get.return_value.send_digest.assert_not_called()


# ------------------------------------------------------- G9 凭证
class TestCredentials:
    def test_fake_secrets_effective_and_real_untouched(self):
        import mailbots_next.config.secrets as sec_mod
        from mailbots_next.config import BASE_DIR
        sec_mod._SECRETS_CACHE = None
        try:
            accounts = sec_mod.get_accounts()
            assert accounts.get("maoxiaoyang@cqtransit.com") == "fake-pwd-mao"
            assert not (BASE_DIR / "secrets.json").exists()
        finally:
            sec_mod._SECRETS_CACHE = None

    def test_env_override_injection_point(self, tmp_path, monkeypatch):
        import mailbots_next.config.secrets as sec_mod
        other = tmp_path / "other-secrets.json"
        other.write_text(json.dumps({"ACCOUNTS": {"alt@example.com": "alt-pwd"}}),
                         encoding="utf-8")
        monkeypatch.setenv("MAILBOT_SECRETS_PATH", str(other))
        monkeypatch.setattr(sec_mod, "SECRETS_PATH", other)
        sec_mod._SECRETS_CACHE = None
        try:
            assert sec_mod.get_accounts().get("alt@example.com") == "alt-pwd"
        finally:
            sec_mod._SECRETS_CACHE = None

    def test_missing_password_goes_E2(self):
        proc = _harness([{
            "id": 1, "客户编码": "CQWLJT260810001", "箱号": "CICU1000001",
            "company": "太平洋", "状态": "正常", "is_deleted": 0,
        }])
        proc.accounts = {}  # no SMTP passwords anywhere
        raw = _tiny_raw("docwbfb@yxologistics.com",
                        "Waybill CQWLJT260810001 CICU1000001")
        # The responsible owner has no SMTP password configured: both the
        # owner lookup and the account fallback yield empty passwords.
        with patch("mailbots_next.serve.get_sender_by_email", return_value=None), \
             patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            proc.process_email("t@t.com", "运单号", "g9-nopwd-1", 1,
                               "Waybill CQWLJT260810001 CICU1000001",
                               "docwbfb@yxologistics.com", "", raw)
        proc.notifier.send_config_missing.assert_called_once()
        assert proc.notifier.send_config_missing.call_args[0][0] == OPS_OWNER_EMAIL
        mock_smtp.assert_not_called()
        queued = [e for e in get_pending_errors(100) if e["message_id"] == "g9-nopwd-1"]
        assert len(queued) == 1 and queued[0]["error_type"] == "MISSING_CREDENTIALS"


# ------------------------------------------------------- G10 日志脱敏
def _fresh_logger(name):
    import io as _io
    import logging as _logging
    from mailbots_next.core.log import EmailLogger
    lg = EmailLogger(name)
    lg.logger.handlers = []
    lg.logger.propagate = False
    buf = _io.StringIO()
    h = _logging.StreamHandler(buf)
    h.setLevel(_logging.DEBUG)
    lg.logger.addHandler(h)
    lg.logger.setLevel(_logging.DEBUG)
    return lg, buf


class TestLogMasking:
    def test_mask_email_rule(self):
        from mailbots_next.core.log import EmailLogger
        lg = EmailLogger("mask-rule-probe")
        assert lg._mask_email("maoxiaoyang@cqtransit.com") == "ma***g@cqtransit.com"
        assert lg._mask_email("ab@x.com") == "a*@x.com"
        assert lg._mask_email("no-at-sign") == "no-at-sign"
        assert "maoxiaoyang@cqtransit.com" not in lg._mask_email("maoxiaoyang@cqtransit.com")

    def test_log_lines_mask_all_emails(self):
        lg, buf = _fresh_logger("mask-lines-probe")
        lg.log_email_received("m1", "草单", "youlia@yxologistics.com",
                              "subject", email_type="draft")
        lg.log_routing("m1", 0, "太平洋", "maoxiaoyang@cqtransit.com",
                       ["client@corp.com"], ["cc@corp.com"])
        lg.log_notify("m1", "forwarded", ["client@corp.com"], True)
        out = buf.getvalue()
        for full in ("youlia@yxologistics.com", "maoxiaoyang@cqtransit.com",
                     "client@corp.com", "cc@corp.com"):
            assert full not in out, full
        assert "ma***g@cqtransit.com" in out

    def test_log_subject_truncated_no_body(self):
        lg, buf = _fresh_logger("mask-body-probe")
        long_subject = "S" * 150
        lg.log_email_received("m2", "草单", "a@b.com", long_subject, email_type="draft")
        out = buf.getvalue()
        assert "S" * 100 in out
        assert "S" * 101 not in out
        assert "运价8800USD提单号XYZ" not in out


def _mock_imap_conn(store_result=("OK", [])):
    conn = Mock()
    conn.login.return_value = None
    conn.select.return_value = ("OK", [])
    conn.store.return_value = store_result
    conn.logout.return_value = None
    return conn


def _live_ingest(monkeypatch):
    import mailbots_next.core.ingest as ingest_mod
    monkeypatch.setenv("MAILBOT_MODE", "live")
    return ingest_mod


class TestMarkSeen:
    """标已读批量方案 B：mark_seen 只入队；flush 执行单连接单 STORE."""

    def test_mark_seen_live_enqueues(self, monkeypatch):
        import mailbots_next.core.ingest as ingest_mod
        monkeypatch.setenv("MAILBOT_MODE", "live")
        with patch.object(ingest_mod, "get_batcher") as mock_get:
            mock_get.return_value.enqueue.return_value = True
            assert ingest_mod.mark_seen(
                "maoxiaoyang@cqtransit.com", "运单草单", 42) is True
        mock_get.return_value.enqueue.assert_called_once_with(
            "maoxiaoyang@cqtransit.com", "运单草单", 42)

    def test_mark_seen_test_mode_never_enqueues(self, monkeypatch):
        import mailbots_next.core.ingest as ingest_mod
        import mailbots_next.config as cfg
        monkeypatch.setenv("MAILBOT_MODE", "test")
        assert cfg.is_live() is False
        with patch.object(ingest_mod, "get_batcher") as mock_get, \
             patch("mailbots_next.core.ingest.imaplib.IMAP4_SSL") as mock_cls:
            assert ingest_mod.mark_seen(
                "maoxiaoyang@cqtransit.com", "运单草单", 45) is False
        mock_get.assert_not_called()
        mock_cls.assert_not_called()

    def test_mark_seen_no_uid_short_circuits(self, monkeypatch):
        import mailbots_next.core.ingest as ingest_mod
        monkeypatch.setenv("MAILBOT_MODE", "live")
        with patch.object(ingest_mod, "get_batcher") as mock_get:
            assert ingest_mod.mark_seen("maoxiaoyang@cqtransit.com", "运单草单", 0) is False
        mock_get.assert_not_called()

    def test_flush_aggregates_multi_uid_single_store(self, monkeypatch):
        from mailbots_next.core.ingest import MarkSeenBatcher
        _live_ingest(monkeypatch)
        batcher = MarkSeenBatcher(flush_sec=9999)
        assert batcher.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 7) is True
        assert batcher.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 8) is True
        assert batcher.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 9) is True
        with patch("mailbots_next.core.ingest.imaplib.IMAP4_SSL") as mock_cls:
            mock_cls.return_value = _mock_imap_conn()
            stats = batcher.flush()
        assert stats == {"buckets": 1, "uids": 3, "failed": 0}
        mock_cls.assert_called_once()
        conn = mock_cls.return_value
        conn.select.assert_called_once_with("&j9BTVYNJU1U-")
        conn.store.assert_called_once_with("7,8,9", "+FLAGS", "\\Seen")

    def test_flush_batches_per_account_folder(self, monkeypatch):
        from mailbots_next.core.ingest import MarkSeenBatcher
        _live_ingest(monkeypatch)
        batcher = MarkSeenBatcher(flush_sec=9999)
        batcher.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 1)
        batcher.enqueue("maoxiaoyang@cqtransit.com", "DSK", 2)
        batcher.enqueue("yangyawen@cqtransit.com", "运单草单", 3)
        with patch("mailbots_next.core.ingest.imaplib.IMAP4_SSL") as mock_cls:
            mock_cls.return_value = _mock_imap_conn()
            stats = batcher.flush()
        assert stats == {"buckets": 3, "uids": 3, "failed": 0}
        assert mock_cls.call_count == 3

    def test_flush_failure_retries_once_then_warns(self, monkeypatch):
        from mailbots_next.core.ingest import MarkSeenBatcher
        from mailbots_next.core.notify import get_counters
        _live_ingest(monkeypatch)
        batcher = MarkSeenBatcher(flush_sec=9999)
        batcher.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 44)
        with patch("mailbots_next.core.ingest.imaplib.IMAP4_SSL") as mock_cls, \
             patch("mailbots_next.core.log.EmailLogger.warning") as mock_warn:
            conn = _mock_imap_conn()
            conn.store.side_effect = Exception("down")
            mock_cls.return_value = conn
            stats = batcher.flush()
        assert stats["failed"] == 1
        assert conn.store.call_count == 2
        mock_warn.assert_called_once()
        assert get_counters().get("mark_seen_failed") == 1
        assert get_pending_errors(100) == []

    def test_batch_cap_flushes_early(self, monkeypatch):
        from mailbots_next.core.ingest import MarkSeenBatcher
        _live_ingest(monkeypatch)
        batcher = MarkSeenBatcher(flush_sec=9999, batch_cap=2)
        with patch("mailbots_next.core.ingest.imaplib.IMAP4_SSL") as mock_cls:
            mock_cls.return_value = _mock_imap_conn()
            batcher.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 1)
            assert mock_cls.call_count == 0
            batcher.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 2)
            assert mock_cls.call_count == 1
            conn = mock_cls.return_value
            conn.store.assert_called_once_with("1,2", "+FLAGS", "\\Seen")

    def test_stop_flushes_leftovers(self, monkeypatch):
        from mailbots_next.core.ingest import MarkSeenBatcher
        _live_ingest(monkeypatch)
        batcher = MarkSeenBatcher(flush_sec=9999)
        batcher.start()
        try:
            with patch("mailbots_next.core.ingest.imaplib.IMAP4_SSL") as mock_cls:
                mock_cls.return_value = _mock_imap_conn()
                batcher.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 5)
                batcher.stop()
            mock_cls.return_value.store.assert_called_once_with(
                "5", "+FLAGS", "\\Seen")
        finally:
            batcher.stop()

    def test_test_mode_never_enqueues(self, monkeypatch):
        from mailbots_next.core.ingest import MarkSeenBatcher
        import mailbots_next.config as cfg
        monkeypatch.setenv("MAILBOT_MODE", "test")
        assert cfg.is_live() is False
        batcher = MarkSeenBatcher(flush_sec=9999)
        assert batcher.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 6) is False
        with patch("mailbots_next.core.ingest.imaplib.IMAP4_SSL") as mock_cls:
            assert batcher.flush() == {"buckets": 0, "uids": 0, "failed": 0}
        mock_cls.assert_not_called()

    def test_manual_rows_exempt_from_mark_seen(self, monkeypatch):
        """C2/W/OTHER 留人工行 → 即使 live 也不标已读."""
        monkeypatch.setenv("MAILBOT_MODE", "live")
        proc = _harness([_rec("CQWLJT260810001", "CICU1000001")])
        rows = [
            ExtractedRow(row_idx=i, email_type=EmailType.DRAFT,
                         customer_code=None, container_no=None,
                         draft_category=cat)
            for i, cat in enumerate(("C2", "W", "OTHER"))
        ]
        with patch("mailbots_next.serve.extract_email", return_value=rows), \
             patch("mailbots_next.serve.mark_seen") as mock_mark, \
             patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            proc.process_email("t@t.com", "草单", "g10-manual-1", 7,
                               "subj", "docwbfb@yxologistics.com", "", b"raw")
        mock_mark.assert_not_called()
        mock_smtp.assert_not_called()
        assert [e for e in get_pending_errors(100)
                if e["message_id"] == "g10-manual-1"] == []

    def test_pending_error_blocks_mark_seen(self, monkeypatch):
        """多行邮件存在未决错误 → 已转发的行不受影响，但整封不标已读."""
        monkeypatch.setenv("MAILBOT_MODE", "live")
        proc = _harness([_rec("CQWLJT260810001", "CICU1000001")])
        rows = [
            ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                         customer_code="CQWLJT260810001", container_no="CICU1000001"),
            ExtractedRow(row_idx=1, email_type=EmailType.DRAFT,
                         customer_code="CQWLJT299999999-ZZZ", container_no="NOBOX9999"),
        ]
        with patch("mailbots_next.serve.extract_email", return_value=rows), \
             patch("mailbots_next.serve.mark_seen") as mock_mark, \
             patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            mock_smtp.return_value = {"success": True, "refused": {}}
            proc.process_email("t@t.com", "草单", "g10-pending-1", 9,
                               "subj", "docwbfb@yxologistics.com", "", b"raw")
        assert mock_smtp.call_count == 1
        mock_mark.assert_not_called()
        assert len([e for e in get_pending_errors(100)
                    if e["message_id"] == "g10-pending-1"]) == 1

    def test_full_success_marks_seen_once(self, monkeypatch):
        """无人工行、无未决错误 → 整封标已读一次（先转发后标）. """
        monkeypatch.setenv("MAILBOT_MODE", "live")
        proc = _harness([_rec("CQWLJT260810001", "CICU1000001")])
        rows = [ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                             customer_code="CQWLJT260810001",
                             container_no="CICU1000001")]
        with patch("mailbots_next.serve.extract_email", return_value=rows), \
             patch("mailbots_next.serve.mark_seen") as mock_mark, \
             patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            mock_smtp.return_value = {"success": True, "refused": {}}
            proc.process_email("t@t.com", "草单", "g10-ok-1", 11,
                               "subj", "docwbfb@yxologistics.com", "", b"raw")
        mock_smtp.assert_called_once()
        mock_mark.assert_called_once_with("t@t.com", "草单", 11)


class TestReplayPayload:
    """P0-1：error_queue 存全重放字段；旧库迁移补 uid 列."""

    def test_add_error_roundtrip_keeps_replay_fields(self):
        from mailbots_next.core import get_connection
        raw = b"From: docwbfb@yxologistics.com\r\nSubject: t CQWLJT260810001\r\n\r\nb"
        eid = add_error(
            "p01-mid-1", "0:CQWLJT260810001", "act", "ACTION_FAILED",
            "boom", "p01-eid-1",
            account="maoxiaoyang@cqtransit.com", folder="运单草单", uid=77,
            subject="t CQWLJT260810001", sender="docwbfb@yxologistics.com",
            date_hdr="2026-09-06 10:00:00", raw_bytes=raw,
        )
        assert eid > 0
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM error_queue WHERE id=?", (eid,)).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["raw_hex"] == raw.hex() and row["raw_hex"] != ""
        assert row["account"] == "maoxiaoyang@cqtransit.com"
        assert row["folder"] == "运单草单"
        assert row["uid"] == 77
        assert row["subject"] == "t CQWLJT260810001"
        assert row["sender"] == "docwbfb@yxologistics.com"
        assert row["date"] == "2026-09-06 10:00:00"

    def test_old_db_migrates_uid_column(self, tmp_path):
        import mailbots_next.core.dedup as dedup_mod
        old_db = tmp_path / "old_dedup.db"
        conn = sqlite3.connect(old_db)
        try:
            conn.execute(
                "CREATE TABLE error_queue (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " message_id TEXT NOT NULL, row_key TEXT, stage TEXT NOT NULL,"
                " error_type TEXT, error_detail TEXT, error_id TEXT,"
                " attempt_count INTEGER DEFAULT 0, last_attempt_at TEXT,"
                " status TEXT DEFAULT 'pending', created_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE dedup (message_id TEXT PRIMARY KEY,"
                " claimed_at TEXT NOT NULL, claimed_by TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
        old_path = dedup_mod.DEDUP_DB_PATH
        dedup_mod.DEDUP_DB_PATH = old_db
        try:
            dedup_mod.init_db()
            cols = [r[1] for r in sqlite3.connect(old_db).execute(
                "PRAGMA table_info(error_queue)").fetchall()]
        finally:
            dedup_mod.DEDUP_DB_PATH = old_path
        assert "uid" in cols and "extra" in cols

    def test_oversize_raw_not_stored_with_warn_counter(self):
        from mailbots_next.config import RAW_MAX_BYTES
        from mailbots_next.core import get_connection
        from mailbots_next.core.notify import get_counters
        big = b"x" * (RAW_MAX_BYTES + 1)
        with patch("mailbots_next.core.log.EmailLogger.warning") as mock_warn:
            eid = add_error("p01-big-1", "0:X", "act", "T", "d", "p01-big-e",
                            account="a@x", raw_bytes=big)
        assert eid > 0
        mock_warn.assert_called_once()
        assert get_counters().get("raw_too_large") == 1
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT raw_hex FROM error_queue WHERE id=?", (eid,)).fetchone()
        finally:
            conn.close()
        assert row["raw_hex"] == ""


def _draft_bytes(code, box):
    import email as email_mod
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    msg = MIMEMultipart()
    msg["Subject"] = f"运单草单 {code}"
    msg["From"] = "youlia@yxologistics.com"
    msg.attach(MIMEText("body", "plain"))
    att = MIMEBase("application", "pdf")
    att.set_payload(b"test")
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", "attachment",
                   filename=f"{box}-260810-123456已加密.pdf")
    msg.attach(att)
    return msg.as_bytes()


class TestSweepReplay:
    """P0-2：失败进队列带原文 → sweep_once 真重放 → 成功 resolved + 标已读入队."""

    def _real_record_case(self):
        from mailbots_next.core.store import load_records
        for r in load_records():
            code, box = r.get("客户编码") or "", r.get("箱号") or ""
            if code.startswith("CQWLJT") and box and r.get("company"):
                return r
        raise AssertionError("yxo_test.db has no usable CQWLJT record")

    def test_failed_forward_replays_to_success(self, monkeypatch):
        monkeypatch.setenv("MAILBOT_MODE", "live")
        from mailbots_next.serve import sweep_once
        rec = self._real_record_case()
        code, box, company = rec["客户编码"], rec["箱号"], rec["company"]
        raw = _draft_bytes(code, box)
        proc_accounts = {"maoxiaoyang@cqtransit.com": "fake-pw"}
        from mailbots_next.serve import MailProcessor
        from mailbots_next.config import snapshot
        proc = MailProcessor(snapshot())
        proc.accounts = proc_accounts
        proc.notifier = Mock()
        with patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            mock_smtp.return_value = {"success": False, "error": "boom"}
            proc.process_email("maoxiaoyang@cqtransit.com", "运单草单",
                               "p02-e2e-1", 77, f"运单草单 {code}",
                               "youlia@yxologistics.com", "", raw)
        assert mock_smtp.call_count == 1
        queued = [e for e in get_pending_errors(100)
                  if e["message_id"] == "p02-e2e-1"]
        assert len(queued) == 1
        assert queued[0]["raw_hex"] == raw.hex()
        assert queued[0]["uid"] == 77
        with patch("mailbots_next.core.act.send_smtp") as mock_smtp2, \
             patch("mailbots_next.serve.enqueue_mark_seen") as mock_mark:
            mock_smtp2.return_value = {"success": True, "refused": {}}
            stats = sweep_once()
        assert mock_smtp2.call_count == 1
        assert stats["retried"] == 1
        assert [e for e in get_pending_errors(100)
                if e["message_id"] == "p02-e2e-1"] == []
        mock_mark.assert_called_once_with(
            "maoxiaoyang@cqtransit.com", "运单草单", 77)

    def test_payloadless_entry_never_misjudged_success(self):
        from mailbots_next.serve import sweep_once
        eid = add_error("p02-nopayload-1", "0:X", "act", "T", "d", "p02-nopayload-e")
        assert eid > 0
        with patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            stats = sweep_once()
        mock_smtp.assert_not_called()
        pend = [e for e in get_pending_errors(100)
                if e["message_id"] == "p02-nopayload-1"]
        assert len(pend) == 1 and pend[0]["attempt_count"] == 1
        assert stats == {"retried": 1, "escalated": 0}


class TestIdleTopology:
    """P0-3：每 (文件夹 × 账号) 一条连接；idle_done 必调."""

    def _proc4(self):
        proc = Mock()
        proc.accounts = {
            "maoxiaoyang@cqtransit.com": "p1",
            "yangyawen@cqtransit.com": "p2",
            "fengqian@cqtransit.com": "p3",
            "hanwenhao@cqtransit.com": "p4",
        }
        return proc

    def test_per_folder_idler_count(self):
        import mailbots_next.serve as serve_mod
        proc = self._proc4()
        with patch.object(serve_mod, "IDLE_PER_FOLDER", True), \
             patch.object(serve_mod, "Idler") as mock_idler:
            serve_mod.start_idle_processors(proc)
            try:
                assert mock_idler.call_count == 5 * 4
                for call in mock_idler.call_args_list:
                    assert len(call.kwargs["folders"]) == 1
            finally:
                serve_mod.stop_idle_processors()

    def test_legacy_group_topology(self):
        import mailbots_next.serve as serve_mod
        proc = self._proc4()
        with patch.object(serve_mod, "IDLE_PER_FOLDER", False), \
             patch.object(serve_mod, "Idler") as mock_idler:
            serve_mod.start_idle_processors(proc)
            try:
                assert mock_idler.call_count == 3 * 4
            finally:
                serve_mod.stop_idle_processors()

    def test_idle_done_called(self):
        from mailbots_next.core.ingest import Idler
        conn = Mock()
        conn.select.return_value = ("OK", [])
        conn.search.return_value = ("OK", [b""])
        idler = Idler(account="a@x", password="p", folders=[("F", "F")],
                      on_raw=Mock(), state_path="")
        idler._idle_once(conn, "F", "F")
        conn.idle.assert_called_once()
        conn.idle_done.assert_called_once()

    def test_idle_done_called_on_check_error(self):
        from mailbots_next.core.ingest import Idler
        conn = Mock()
        conn.select.return_value = ("OK", [])
        conn.search.return_value = ("OK", [b""])
        conn.idle_check.side_effect = Exception("idle broken")
        idler = Idler(account="a@x", password="p", folders=[("F", "F")],
                      on_raw=Mock(), state_path="")
        idler._idle_once(conn, "F", "F")
        conn.idle_done.assert_called_once()
