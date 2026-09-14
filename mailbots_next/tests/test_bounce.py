"""NDR / 退信监控测试。"""
import pytest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.nonmultipart import MIMENonMultipart
from email import encoders
from unittest.mock import patch, MagicMock

from mailbots_next.core.act import make_forward_id, _bounce_addr, build_forward_message
from mailbots_next.core.bounce import parse_ndr, poll_bounces
from mailbots_next.core.store import write_forward_log, get_forward_log, mark_forward_bounced
from mailbots_next.core.notify import get_notifier
from mailbots_next.config import settings


def test_make_forward_id_format():
    """forward_id 格式：纯 hex + 短横线，可安全进入邮件地址本地名。"""
    fid = make_forward_id("msg-1", "0")
    assert fid is not None
    assert len(fid) == 25  # 16 + 1 + 8
    parts = fid.split("-")
    assert len(parts) == 2
    assert len(parts[0]) == 16
    assert len(parts[1]) == 8
    # 纯 hex
    assert all(c in "0123456789abcdef" for c in parts[0])
    assert all(c in "0123456789abcdef" for c in parts[1])
    # 正则
    import re
    assert re.match(r"^[0-9a-f]{16}-[0-9a-f]{8}$", fid)


def test_bounce_addr_verp_enabled(monkeypatch):
    """VERP 开启时 envelope from = bounce+<fid>@domain。"""
    monkeypatch.setattr(settings, "BOUNCE_USE_VERP", True)
    monkeypatch.setattr(settings, "BOUNCE_ADDRESS", "mailbots-bounce@cqtransit.com")
    fid = "a1b2c3d4e5f6a7b8-c0ffee12"
    addr = _bounce_addr(fid)
    assert addr == f"bounce+{fid}@cqtransit.com"


def test_bounce_addr_verp_disabled(monkeypatch):
    """VERP 关闭时直接返回 BOUNCE_ADDRESS。"""
    monkeypatch.setattr(settings, "BOUNCE_USE_VERP", False)
    monkeypatch.setattr(settings, "BOUNCE_ADDRESS", "mailbots-bounce@cqtransit.com")
    fid = "a1b2c3d4e5f6a7b8-c0ffee12"
    addr = _bounce_addr(fid)
    assert addr == "mailbots-bounce@cqtransit.com"


def test_build_forward_message_has_x_header():
    """build_forward_message 写入 X-YXO-Forward-Id 头。"""
    from email.mime.text import MIMEText
    original = MIMEText("test body")
    original["Subject"] = "Test Subject"
    original["From"] = "sender@example.com"

    msg = build_forward_message(original, ["to@example.com"], [], "sender@example.com",
                                forward_id="a1b2c3d4e5f6a7b8-c0ffee12")
    assert msg["X-YXO-Forward-Id"] == "a1b2c3d4e5f6a7b8-c0ffee12"


def test_send_smtp_uses_mail_from(monkeypatch):
    """send_smtp 使用 mail_from 作为 envelope from。"""
    from email.mime.text import MIMEText
    import smtplib

    mock_server = MagicMock()
    mock_server.sendmail.return_value = {}
    mock_context = MagicMock()
    mock_context.__enter__.return_value = mock_server
    mock_context.__exit__.return_value = False
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *a, **kw: mock_context)

    msg = MIMEText("test")
    msg["From"] = "sender@example.com"
    msg["To"] = "to@example.com"

    from mailbots_next.core.act import send_smtp
    result = send_smtp(msg, "sender@example.com", "password", ["to@example.com"],
                       mail_from="bounce+test@cqtransit.com")

    assert result["success"] is True
    # 检查 sendmail 的第一个参数是 mail_from
    args, _ = mock_server.sendmail.call_args
    assert args[0] == "bounce+test@cqtransit.com"


def test_parse_ndr_verps():
    """parse_ndr 从 VERP To 头析出 forward_id。"""
    # 使用简单的 NDR 格式：将 delivery status 放在主体中
    # 这种格式能被 email parser 正确解析
    raw = b"""Content-Type: multipart/mixed; boundary="boundary123"
To: bounce+a1b2c3d4e5f6a7b8-c0ffee12@cqtransit.com
From: MAILER-DAEMON@example.com
Subject: Delivery Status Notification (Failure)

--boundary123
Content-Type: text/plain

Action: failed
Diagnostic-Code: smtp; 550 5.1.1 User unknown
Final-Recipient: rfc822; bad@example.com

--boundary123
Content-Type: message/rfc822
X-YXO-Forward-Id: a1b2c3d4e5f6a7b8-c0ffee12

Returned message body
--boundary123--
"""
    ndr = parse_ndr(raw)

    assert ndr["forward_id"] == "a1b2c3d4e5f6a7b8-c0ffee12"
    assert ndr["action"] == "failed"
    assert "550" in ndr["diagnostic"]
    assert "bad@example.com" in ndr["failed_recipient"]


def test_parse_ndr_fallback_to_header():
    """VERP 缺失时，兜底用 X-YXO-Forward-Id 头。"""
    raw = b"""Content-Type: multipart/report; report-type=delivery-status; boundary="boundary123"
To: postmaster@example.com
From: MAILER-DAEMON@example.com
Subject: Delivery Status Notification (Failure)

--boundary123
Content-Type: message/delivery-status

Action: failed
Diagnostic-Code: smtp; 550 5.1.1 User unknown
Final-Recipient: rfc822; bad@example.com

--boundary123
Content-Type: message/rfc822
X-YXO-Forward-Id: f1e2d3c4b5a69876-12345678

Returned message body
--boundary123--
"""
    ndr = parse_ndr(raw)

    assert ndr["forward_id"] == "f1e2d3c4b5a69876-12345678"


@patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL")
def test_poll_bounces_skipped_when_disabled(mock_imap, monkeypatch):
    """BOUNCE_MONITOR_ENABLED=False 时直接返回 skipped。"""
    monkeypatch.setattr(settings, "BOUNCE_MONITOR_ENABLED", False)
    result = poll_bounces()
    assert result == {"skipped": True}
    mock_imap.assert_not_called()


def test_poll_bounces_requeues_on_ndr(monkeypatch):
    """NDR 到达时，重新入队 error_queue 并告警。"""
    from mailbots_next.config import settings as cfg
    from unittest.mock import MagicMock, patch
    
    # Setup settings —— 必须经 monkeypatch，保证用例结束自动还原
    monkeypatch.setattr(cfg, "BOUNCE_MONITOR_ENABLED", True)
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_USER", "test@cqtransit.com")
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_PASSWORD", "password")
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_SERVER", "imap.example.com")
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_PORT", 993)
    monkeypatch.setattr(cfg, "BOUNCE_FOLDER", "INBOX")
    monkeypatch.setattr(cfg, "OPS_OWNER_EMAIL", "ops@example.com")

    raw = b"""Content-Type: multipart/mixed; boundary="boundary123"
To: bounce+a1b2c3d4e5f6a7b8-c0ffee12@cqtransit.com
From: MAILER-DAEMON@example.com
Subject: Delivery Status Notification (Failure)

--boundary123
Content-Type: text/plain

Action: failed
Diagnostic-Code: smtp; 550 5.1.1
Final-Recipient: rfc822; bad@example.com

--boundary123
Content-Type: message/rfc822
X-YXO-Forward-Id: a1b2c3d4e5f6a7b8-c0ffee12

Returned message body
--boundary123--
"""

    mock_get_log = MagicMock()
    mock_notifier = MagicMock()
    mock_add_error = MagicMock()
    mock_mark = MagicMock()
    mock_imap = MagicMock()

    with patch("mailbots_next.core.bounce.get_forward_log", mock_get_log), \
         patch("mailbots_next.core.bounce.get_notifier", mock_notifier), \
         patch("mailbots_next.core.bounce.dedup.add_error", mock_add_error), \
         patch("mailbots_next.core.bounce.mark_forward_bounced", mock_mark), \
         patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL", mock_imap):
        
        mock_conn = MagicMock()
        mock_imap.return_value = mock_conn
        mock_conn.search.return_value = ("OK", [b"1"])
        
        raw = b"""Content-Type: multipart/mixed; boundary="boundary123"
To: bounce+a1b2c3d4e5f6a7b8-c0ffee12@cqtransit.com
From: MAILER-DAEMON@example.com
Subject: Delivery Status Notification (Failure)

--boundary123
Content-Type: text/plain

Action: failed
Diagnostic-Code: smtp; 550 5.1.1
Final-Recipient: rfc822; bad@example.com

--boundary123
Content-Type: message/rfc822
X-YXO-Forward-Id: a1b2c3d4e5f6a7b8-c0ffee12

Returned message body
--boundary123--
"""
        mock_conn.fetch.return_value = [None, [(None, raw)]]

        mock_get_log.return_value = {
            "forward_id": "a1b2c3d4e5f6a7b8-c0ffee12",
            "message_id": "msg-123",
            "row_key": "0",
            "account": "sender@test.com",
            "folder": "INBOX",
            "uid": 1,
            "subject": "Test",
            "sender": "from@example.com",
            "date_hdr": "Mon, 1 Jan 2024 00:00:00 +0000",
            "raw_hex": b"dummy".hex(),
            "bounced": 0,
        }

        mock_notifier_instance = MagicMock()
        mock_notifier.return_value = mock_notifier_instance

        from mailbots_next.core.bounce import poll_bounces
        result = poll_bounces()

    assert result["processed"] == 1
    assert result["requeued"] == 1
    mock_add_error.assert_called_once()
    mock_mark.assert_called_once_with("a1b2c3d4e5f6a7b8-c0ffee12")
    mock_notifier.return_value.send_program_error.assert_called()


def test_forward_log_write_read_mark(monkeypatch):
    """forward_log 写入/读取/标记 bounced 完整流程。"""
    # Need to be in live mode for write_forward_log to work
    monkeypatch.setenv("MAILBOT_MODE", "live")
    # Re-import to pick up new env
    import importlib
    import mailbots_next.config as cfg_mod
    importlib.reload(cfg_mod)
    from mailbots_next.config import settings as reloaded_settings
    import mailbots_next.core.store as store_mod
    importlib.reload(store_mod)
    from mailbots_next.core.store import write_forward_log as wfl, get_forward_log as gfl, mark_forward_bounced as mfb

    fid = "a1b2c3d4e5f6a7b8-c0ffee12"
    wfl(fid, "msg-1", "0", "acc@test.com", "INBOX", 1,
          "Subject", "from@test.com", "Date", b"rawbytes",
          ["to@test.com"], ["cc@test.com"])
    rec = gfl(fid)
    assert rec is not None
    assert rec["forward_id"] == fid
    assert rec["bounced"] == 0

    mfb(fid)
    rec = gfl(fid)
    assert rec["bounced"] == 1


def test_poll_bounces_dedupe_already_bounced(monkeypatch):
    """同一 NDR 重投（bounced=1）不再重复入队。"""
    monkeypatch.setattr(settings, "BOUNCE_MONITOR_ENABLED", True)
    monkeypatch.setattr(settings, "BOUNCE_IMAP_USER", "test@cqtransit.com")
    monkeypatch.setattr(settings, "BOUNCE_IMAP_PASSWORD", "password")
    monkeypatch.setattr(settings, "BOUNCE_IMAP_SERVER", "imap.example.com")
    monkeypatch.setattr(settings, "BOUNCE_IMAP_PORT", 993)
    monkeypatch.setattr(settings, "BOUNCE_FOLDER", "INBOX")

    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL") as mock_imap:
        mock_conn = MagicMock()
        mock_imap.return_value = mock_conn
        mock_conn.search.return_value = ("OK", [b"1"])
        raw = b"""Content-Type: multipart/mixed; boundary="boundary123"
To: bounce+a1b2c3d4e5f6a7b8-c0ffee12@cqtransit.com
From: MAILER-DAEMON@example.com
Subject: Delivery Status Notification (Failure)

--boundary123
Content-Type: text/plain

Action: failed
Diagnostic-Code: smtp; 550 5.1.1
Final-Recipient: rfc822; bad@example.com

--boundary123
Content-Type: message/rfc822
X-YXO-Forward-Id: a1b2c3d4e5f6a7b8-c0ffee12

Returned message body
--boundary123--
"""
        mock_conn.fetch.return_value = [None, [(None, raw)]]

        # bounced=1
        with patch("mailbots_next.core.bounce.get_forward_log") as mock_get_log:
            mock_get_log.return_value = {
                "forward_id": "a1b2c3d4e5f6a7b8-c0ffee12",
                "bounced": 1,
            }
            with patch("mailbots_next.core.bounce.dedup.add_error") as mock_add:
                with patch("mailbots_next.core.bounce.mark_forward_bounced"):
                    with patch("mailbots_next.core.bounce.get_notifier"):
                        result = poll_bounces()
        assert result["requeued"] == 0  # 不再重复入队