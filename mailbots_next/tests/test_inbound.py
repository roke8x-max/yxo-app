import json
import threading
import time
from http.client import HTTPConnection
from unittest.mock import Mock, patch, MagicMock
import pytest

from mailbots_next.core.dedup import (
    init_db,
    add_error,
    claim_manual_forward,
    reopen_error,
    get_pending_errors,
    get_manual_errors,
    DEDUP_DB_PATH,
)
from mailbots_next.core.log import EmailLogger
from mailbots_next.config import (
    INBOUND_PORT,
    INBOUND_SHARED_SECRET,
    DRAFT_NUMS_DB_PATH,
)


@pytest.fixture(autouse=True)
def setup_db():
    for _p in (DEDUP_DB_PATH, DRAFT_NUMS_DB_PATH):
        if _p.exists():
            try:
                _p.unlink()
            except PermissionError:
                pass
    init_db()
    yield
    for _p in (DEDUP_DB_PATH, DRAFT_NUMS_DB_PATH):
        if _p.exists():
            try:
                _p.unlink()
            except PermissionError:
                pass


@pytest.fixture
def mock_notifier():
    with patch("mailbots_next.core.notify.get_notifier") as mock:
        notifier = Mock()
        notifier.send_alarm.return_value = True
        notifier.send_pending.return_value = True
        notifier.send_forwarded.return_value = True
        notifier.send_draft_update.return_value = True
        notifier.send_program_error.return_value = True
        notifier.send_config_missing.return_value = True
        mock.return_value = notifier
        yield notifier


@pytest.fixture
def mock_smtp():
    with patch("mailbots_next.core.act.send_smtp") as mock:
        mock.return_value = {"success": True, "refused": {}}
        yield mock


def _draft_raw(code, box):
    """Build a real, parseable draft email (subject code + enc-pdf attachment)."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    msg = MIMEMultipart()
    msg["Subject"] = f"草单 {code}"
    msg["From"] = "youlia@yxologistics.com"
    msg.attach(MIMEText("body", "plain"))
    att = MIMEBase("application", "pdf")
    att.set_payload(b"test")
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", "attachment",
                   filename=f"{box}-260810-123456已加密.pdf")
    msg.attach(att)
    return msg.as_bytes()


@pytest.fixture
def test_error_record():
    # End-to-end queue path: the REAL add_error writes the full replay
    # payload (raw/account/folder/uid/...). The mail matches conftest seed
    # row 1, so the real handler can route and forward it.
    raw = _draft_raw("CQWLJT260810001-SPB", "CICU1000001")
    return add_error(
        "test_msg_123", "0:CICU1000001", "act", "TEST",
        "Test error for inbound", "test_error_001",
        account="maoxiaoyang@cqtransit.com", folder="运单草单", uid=55,
        subject="草单 CQWLJT260810001-SPB",
        sender="youlia@yxologistics.com", date_hdr="2026-09-05 12:00:00",
        raw_bytes=raw,
    )


class TestInboundEndpoint:
    @pytest.fixture(autouse=True)
    def start_server(self, mock_notifier, mock_smtp, test_error_record):
        # NOTE: the HTTP server below serves the REAL InboundHandler from
        # serve.py (no subclass, no do_POST override). The handler builds a
        # real MailProcessor reading the temp seed DB + temp bot_config, so
        # recipients and owner mapping must be seeded here.
        from mailbots_next.serve import InboundHandler, HTTPServer
        from mailbots_next.core.store import (
            get_bot_config_connection, init_bot_config_db, seed_owner_mapping,
        )
        import json as json_module
        init_bot_config_db()
        seed_owner_mapping()
        conn = get_bot_config_connection()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra)
                   VALUES (?, 'company', ?, ?, ?, '{}')""",
                ("all", "太平洋", json_module.dumps(["test@example.com"]), json_module.dumps(["cc@example.com"])),
            )
            conn.commit()
        finally:
            conn.close()

        self.server = HTTPServer(("127.0.0.1", INBOUND_PORT), InboundHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.5)
        yield
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path, body, headers=None):
        conn = HTTPConnection("127.0.0.1", INBOUND_PORT, timeout=5)
        default_headers = {"Content-Type": "application/json"}
        if INBOUND_SHARED_SECRET:
            default_headers["X-Forward-Secret"] = INBOUND_SHARED_SECRET
        if headers:
            default_headers.update(headers)
        conn.request("POST", path, json.dumps(body), default_headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        return resp.status, data

    # 【场景 1】确认 N 成功：status manual→resolved 且 forward_email 被调用一次
    def test_confirm_success(self, test_error_record, mock_smtp, monkeypatch):
        monkeypatch.setenv("MAILBOT_MODE", "live")
        status, data = self._post("/internal/forward", {"error_id": test_error_record})
        assert status == 200
        assert data == "Forwarded successfully"
        mock_smtp.assert_called_once()

        # Also verify DB state
        from mailbots_next.core.dedup import get_connection
        conn = get_connection()
        cursor = conn.execute("SELECT status FROM error_queue WHERE id = ?", (test_error_record,))
        row = cursor.fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "resolved"

    # 【场景 2】并发防双发：两线程同时确认同一 error_id，只有一个转发成功，另一个返回「已被处理」
    def test_concurrent_prevent_double_send(self, test_error_record, mock_smtp, monkeypatch):
        monkeypatch.setenv("MAILBOT_MODE", "live")

        def do_request():
            conn = HTTPConnection("127.0.0.1", INBOUND_PORT, timeout=5)
            headers = {"Content-Type": "application/json"}
            if INBOUND_SHARED_SECRET:
                headers["X-Forward-Secret"] = INBOUND_SHARED_SECRET
            conn.request("POST", "/internal/forward", json.dumps({"error_id": test_error_record}), headers)
            resp = conn.getresponse()
            data = resp.read().decode()
            conn.close()
            return resp.status, data

        t1 = threading.Thread(target=do_request)
        t2 = threading.Thread(target=do_request)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Verify only one SMTP call happened (prevent double send)
        assert mock_smtp.call_count == 1, f"SMTP should be called exactly once, got {mock_smtp.call_count}"

        # Verify DB state is resolved
        from mailbots_next.core.dedup import get_connection
        conn = get_connection()
        cursor = conn.execute("SELECT status FROM error_queue WHERE id = ?", (test_error_record,))
        row = cursor.fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "resolved"

    # 【场景 3】无效 ID：确认不存在的 error_id → 返回 404，且不调用 SMTP
    def test_invalid_id_returns_404(self, mock_smtp):
        status, data = self._post("/internal/forward", {"error_id": 99999})
        assert status == 404
        mock_smtp.assert_not_called()

    # 【场景 4】鉴权拦截：不带 X-Forward-Secret 或密钥错误 → 返回 401，不调用 SMTP
    def test_auth_rejected_without_secret(self, test_error_record, mock_smtp):
        if not INBOUND_SHARED_SECRET:
            pytest.skip("INBOUND_SHARED_SECRET not set, skipping auth test")
        conn = HTTPConnection("127.0.0.1", INBOUND_PORT, timeout=5)
        conn.request("POST", "/internal/forward", json.dumps({"error_id": test_error_record}), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        assert resp.status == 401
        assert data == "Unauthorized"
        mock_smtp.assert_not_called()

    def test_auth_rejected_with_wrong_secret(self, test_error_record, mock_smtp):
        if not INBOUND_SHARED_SECRET:
            pytest.skip("INBOUND_SHARED_SECRET not set, skipping auth test")
        conn = HTTPConnection("127.0.0.1", INBOUND_PORT, timeout=5)
        conn.request("POST", "/internal/forward", json.dumps({"error_id": test_error_record}), {"Content-Type": "application/json", "X-Forward-Secret": "wrong_secret"})
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        assert resp.status == 401
        assert data == "Unauthorized"
        mock_smtp.assert_not_called()

    def test_inbound_replay_passes_uid_to_mark_seen(self, test_error_record, mock_smtp, monkeypatch):
        """队列里的真实 uid 必须一路传到标已读（live 下入队）. """
        monkeypatch.setenv("MAILBOT_MODE", "live")
        with patch("mailbots_next.serve.mark_seen") as mock_mark:
            status, data = self._post("/internal/forward", {"error_id": test_error_record})
        assert status == 200
        mock_mark.assert_called_once_with("maoxiaoyang@cqtransit.com", "运单草单", 55)


class TestErrorQueueStateMachine:
    def test_claim_manual_forward_atomic(self, test_error_record):
        result1 = claim_manual_forward(test_error_record)
        assert result1 is True

        result2 = claim_manual_forward(test_error_record)
        assert result2 is False

    def test_reopen_error(self, test_error_record):
        claim_manual_forward(test_error_record)
        result = reopen_error(test_error_record)
        assert result is True

        from mailbots_next.core.dedup import get_manual_errors
        manual = get_manual_errors(10)
        target = [e for e in manual if e["id"] == test_error_record]
        assert len(target) == 1
        assert target[0]["status"] == "manual"
        assert target[0]["attempt_count"] == 0

    def test_reopen_only_from_resolved(self, test_error_record):
        result = reopen_error(test_error_record)
        assert result is False