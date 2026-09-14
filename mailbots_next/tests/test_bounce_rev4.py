"""NDR rev4 unit tests: four-mailbox polling + subkey assoc + strict is_ndr.

New behavior only; test_bounce.py is left untouched.
All settings mutations go through monkeypatch (no global leakage).
This file is pure ASCII on purpose (CJK via \\u escapes) to avoid
editor-encoding corruption of literals the tests depend on.
"""
import pytest
from unittest.mock import Mock, patch

from mailbots_next.core.bounce import parse_ndr, is_ndr, poll_bounces
from mailbots_next.core.store import (
    init_bot_config_db,
    init_forward_log,
    init_bounce_handled,
    seed_owner_mapping,
    write_forward_log,
    get_forward_log,
)
from mailbots_next.core.dedup import init_db, get_pending_errors

FID = "a1b2c3d4e5f6a7b8-c0ffee12"
ACCT = "maoxiaoyang@cqtransit.com"

# CJK literals (escaped): keep source ASCII.
C_CODE = "\u5ba2\u6237\u7f16\u7801"          # customer code
C_BOX = "\u7bb1\u53f7"                       # container no
C_TRAIN = "\u73ed\u5217\u53f7"                # train no
C_DEST = "\u76ee\u7684\u7ad9"                # destination
C_COMPANY = "\u5f00\u7968\u5b50\u516c\u53f8\u540d\u79f0"  # responsible company
C_SRC = "\u672c\u5730\u8d27\u6e90\u516c\u53f8"  # local source company
C_STATUS = "\u72b6\u6001"                    # status


@pytest.fixture(autouse=True)
def _rev4_dbs(monkeypatch, tmp_path):
    """Per-test DB isolation: rebind path constants onto tmp_path per module."""
    import sqlite3
    import mailbots_next.config as cfg_mod
    import mailbots_next.core.store as store_mod
    import mailbots_next.core.dedup as dedup_mod
    import mailbots_next.core.notify as notify_mod

    for name, fname in (("YXO_DB_PATH", "yxo.db"),
                        ("BOT_CONFIG_DB_PATH", "bot_config.db"),
                        ("DEDUP_DB_PATH", "dedup.db"),
                        ("DRAFT_NUMS_DB_PATH", "draft_nums.db")):
        target = tmp_path / fname
        for mod in (cfg_mod, store_mod, dedup_mod):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, target)
    monkeypatch.setattr(
        notify_mod, "DAILY_COUNTERS_PATH", tmp_path / "daily_counters.json")

    conn = sqlite3.connect(tmp_path / "yxo.db")
    try:
        conn.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY, "
            + C_CODE + " TEXT, " + C_BOX + " TEXT, "
            + C_TRAIN + " TEXT, " + C_DEST + " TEXT, "
            + C_COMPANY + " TEXT, " + C_SRC + " TEXT, "
            + C_STATUS + " TEXT, is_deleted INTEGER, dsk TEXT, ATB TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    init_db()
    init_bot_config_db()
    init_forward_log()
    init_bounce_handled()
    seed_owner_mapping()
    yield


def _seed_log(fid=FID, to=("cust-a@test.com",), sent_by=ACCT,
              message_id="rev4-msg-1"):
    """Write one forward_log row directly (live gate opened via env)."""
    import mailbots_next.config as cfg_mod
    assert cfg_mod.is_live()
    write_forward_log(
        fid, message_id, "0",
        ACCT, "inbox-folder", 7,
        "subj", "up@example.com", "Mon, 01 Sep 2026 00:00:00 +0000",
        b"rev4-raw-bytes", list(to), [], sent_by,
    )


def _ndr_raw(to_hdr, body_lines, fid_header=None):
    """Build a multipart NDR; fid_header=None means no X-YXO header."""
    hdr = "X-YXO-Forward-Id: %s\r\n" % fid_header if fid_header else ""
    return (
        "Content-Type: multipart/mixed; boundary=\"b123\"\r\n"
        "To: %s\r\n"
        "From: MAILER-DAEMON@example.com\r\n"
        "Subject: Delivery Status Notification (Failure)\r\n"
        "\r\n"
        "--b123\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "%s\r\n"
        "--b123\r\n"
        "Content-Type: message/rfc822\r\n"
        "%s"
        "\r\n"
        "Returned message body\r\n"
        "--b123--\r\n"
        % (to_hdr, "\r\n".join(body_lines), hdr)
    ).encode("utf-8")


def _make_imap(mails_by_acct):
    """Per-account fake IMAP; mails_by_acct: {acct: [raw, ...]}."""
    class _FakeIMAP:
        def __init__(self, *a, **k):
            self.acct = None

        def login(self, u, p):
            self.acct = u
            return "OK", []

        def select(self, f):
            return "OK", []

        def search(self, *a):
            mails = mails_by_acct.get(self.acct, [])
            if not mails:
                return "OK", [b""]
            return "OK", [b" ".join(str(i + 1).encode() for i in range(len(mails)))]

        def fetch(self, num, item):
            if item == "(UID)":
                uid = 1000 + int(num)
                return None, [(f"{int(num)} (UID {uid})".encode(), b"")]
            mails = mails_by_acct.get(self.acct, [])
            return None, [(None, mails[int(num) - 1])]

        def store(self, *a):
            return "OK", []

        def logout(self):
            return "OK", []
    return _FakeIMAP


def _poll_env(monkeypatch, **kw):
    from mailbots_next.config import settings as cfg
    monkeypatch.setattr(cfg, "BOUNCE_MONITOR_ENABLED", True)
    monkeypatch.setattr(cfg, "BOUNCE_ENVELOPE_MODE", "sender")
    monkeypatch.setattr(cfg, "BOUNCE_POLL_ACCOUNTS", kw.get("poll", ""))
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_SERVER", "imap.test")
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_PORT", 993)
    monkeypatch.setattr(cfg, "BOUNCE_FOLDER", "INBOX")
    monkeypatch.setattr(cfg, "OPS_OWNER_EMAIL", "maoxiaoyang@cqtransit.com")


# ---------- parse_ndr forms ----------

def test_parse_multipart_report_delivery_status():
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: failed",
                    "Diagnostic-Code: smtp; 550 5.1.1 User unknown",
                    "Final-Recipient: rfc822; bad@example.com"],
                   fid_header=FID)
    ndr = parse_ndr(raw)
    assert ndr["forward_id"] == FID
    assert ndr["action"] == "failed"
    assert "550" in ndr["diagnostic"]
    assert "bad@example.com" in ndr["failed_recipient"]


def test_parse_rfc822_headers_body_carries_fid():
    """Pit-1 regression: fid only in text body (MTA returns headers as text)."""
    raw = (
        "Content-Type: multipart/mixed; boundary=\"b123\"\r\n"
        "To: postmaster@example.com\r\n"
        "From: MAILER-DAEMON@example.com\r\n"
        "\r\n"
        "--b123\r\n"
        "Content-Type: text/rfc822-headers\r\n"
        "\r\n"
        "Received: from mx.example.com\r\n"
        "X-YXO-Forward-Id: %s\r\n"
        "Subject: original subject\r\n"
        "--b123\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "Action: failed\r\n"
        "Diagnostic-Code: smtp; 550 5.1.1\r\n"
        "Final-Recipient: rfc822; bad@example.com\r\n"
        "--b123--\r\n" % FID
    ).encode("utf-8")
    ndr = parse_ndr(raw)
    assert ndr["forward_id"] == FID
    assert ndr["action"] == "failed"


def test_parse_no_fid_returns_none():
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: failed", "Diagnostic-Code: smtp; 550",
                    "Final-Recipient: rfc822; bad@example.com"],
                   fid_header=None)
    ndr = parse_ndr(raw)
    assert ndr["forward_id"] is None
    assert ndr["action"] == "failed"


def test_parse_fid_in_body_not_header():
    """fid appears in body text, not in any part header."""
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: failed",
                    "X-YXO-Forward-Id: %s" % FID,
                    "Diagnostic-Code: smtp; 550"],
                   fid_header=None)
    assert parse_ndr(raw)["forward_id"] == FID


# ---------- is_ndr ----------

def test_is_ndr_true_for_real_ndr():
    from email.parser import BytesParser
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: failed",
                    "Diagnostic-Code: smtp; 550 5.1.1",
                    "Final-Recipient: rfc822; bad@example.com"],
                   fid_header=FID)
    assert is_ndr(BytesParser().parsebytes(raw)) is True


def test_is_ndr_false_for_normal_mail():
    from email.parser import BytesParser
    from email.mime.text import MIMEText
    msg = MIMEText("Hello, please see attached.", "plain", "utf-8")
    msg["Subject"] = "daily hello"
    msg["From"] = "customer@example.com"
    assert is_ndr(BytesParser().parsebytes(msg.as_bytes())) is False


def test_is_ndr_multipart_report_detected():
    from email.parser import BytesParser
    raw = (
        "Content-Type: multipart/report; report-type=delivery-status; boundary=\"b1\"\r\n"
        "To: postmaster@example.com\r\n"
        "\r\n"
        "--b1\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "This is a delivery report\r\n"
        "--b1--\r\n"
    ).encode("utf-8")
    assert is_ndr(BytesParser().parsebytes(raw)) is True


def test_delayed_action_not_treated_as_failure(monkeypatch):
    """Action: delayed notices are ignored, never requeued."""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    _poll_env(monkeypatch, poll=ACCT)
    _seed_log()
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: delayed",
                    "Diagnostic-Code: smtp; 421 4.4.7 deferred",
                    "Final-Recipient: rfc822; cust-a@test.com"],
                   fid_header=FID)
    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL",
               _make_imap({ACCT: [raw]})), \
         patch("mailbots_next.core.bounce.get_notifier") as mock_n, \
         patch("mailbots_next.config.secrets.get_accounts",
               return_value={ACCT: "pw"}):
        stats = poll_bounces()
    assert stats["processed"] == 1
    assert stats["requeued"] == 0
    assert get_pending_errors(100) == []
    mock_n.return_value.send_program_error.assert_not_called()


# ---------- multi-account poll ----------

def test_poll_multi_account_only_ndr_account_requeues(monkeypatch):
    """4-account poll: only the NDR account requeues; normal mail untouched."""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    _poll_env(monkeypatch,
              poll="maoxiaoyang@cqtransit.com,yangyawen@cqtransit.com")
    _seed_log()
    from email.mime.text import MIMEText
    normal = MIMEText("Daily hello.", "plain", "utf-8")
    normal["Subject"] = "hello"
    normal["From"] = "friend@example.com"
    ndr = _ndr_raw("postmaster@example.com",
                   ["Action: failed",
                    "Diagnostic-Code: smtp; 550 5.1.1",
                    "Final-Recipient: rfc822; cust-a@test.com"],
                   fid_header=FID)
    mails = {"maoxiaoyang@cqtransit.com": [ndr],
             "yangyawen@cqtransit.com": [normal.as_bytes()]}
    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL", _make_imap(mails)), \
         patch("mailbots_next.core.bounce.get_notifier") as mock_n, \
         patch("mailbots_next.config.secrets.get_accounts",
               return_value={"maoxiaoyang@cqtransit.com": "pw",
                             "yangyawen@cqtransit.com": "pw"}):
        stats = poll_bounces()
    assert stats["requeued"] == 1
    assert stats["skipped_non_ndr"] == 1
    assert stats["processed"] == 1
    pend = get_pending_errors(100)
    assert len(pend) == 1 and pend[0]["error_type"] == "BOUNCE"
    mock_n.return_value.send_program_error.assert_called_once()


# ---------- subkey association ----------

def test_subkey_unique_hit_requeues(monkeypatch):
    """Missing fid + unique failed_recipient/sent_by hit -> requeued."""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    _poll_env(monkeypatch, poll=ACCT)
    _seed_log()
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: failed",
                    "Diagnostic-Code: smtp; 550 5.1.1",
                    "Final-Recipient: rfc822; cust-a@test.com"],
                   fid_header=None)
    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL",
               _make_imap({ACCT: [raw]})), \
         patch("mailbots_next.core.bounce.get_notifier"), \
         patch("mailbots_next.config.secrets.get_accounts",
               return_value={ACCT: "pw"}):
        stats = poll_bounces()
    assert stats["requeued"] == 1
    pend = get_pending_errors(100)
    assert len(pend) == 1
    assert pend[0]["raw_hex"] != ""
    assert get_forward_log(FID)["bounced"] == 1


def test_subkey_ambiguous_no_requeue(monkeypatch):
    """Non-unique subkey -> unknown alert, no requeue (never guess)."""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    _poll_env(monkeypatch, poll=ACCT)
    _seed_log(fid="1111111111111111-aaaaaaaa", to=("cust-a@test.com",),
              message_id="rev4-amb-1")
    _seed_log(fid="2222222222222222-bbbbbbbb", to=("cust-a@test.com",),
              message_id="rev4-amb-2")
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: failed",
                    "Diagnostic-Code: smtp; 550",
                    "Final-Recipient: rfc822; cust-a@test.com"],
                   fid_header=None)
    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL",
               _make_imap({ACCT: [raw]})), \
         patch("mailbots_next.core.bounce.get_notifier") as mock_n, \
         patch("mailbots_next.config.secrets.get_accounts",
               return_value={ACCT: "pw"}):
        stats = poll_bounces()
    assert stats["requeued"] == 0
    assert stats["unknown"] == 1
    assert get_pending_errors(100) == []
    mock_n.return_value.send_program_error.assert_called_once()


# ---------- idempotency ----------

def test_is_ndr_action_only_is_false():
    """判据 (c) 收紧：只有 Action 行、没有 Final-Recipient/Diagnostic-Code
    （如会议纪要 'Action: 请跟进'）→ 不是 NDR。"""
    from email.parser import BytesParser
    from email.mime.text import MIMEText
    msg = MIMEText("Action: follow up tomorrow", "plain", "utf-8")
    msg["Subject"] = "meeting notes"
    msg["From"] = "colleague@example.com"
    assert is_ndr(BytesParser().parsebytes(msg.as_bytes())) is False


def test_prescreen_fuzzy_headers_still_fetched(monkeypatch):
    """预筛 fail-open：连 From 都没有的模糊头部 → 仍取全文并判定入队。"""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    _poll_env(monkeypatch, poll=ACCT)
    _seed_log()
    raw = (
        "Content-Type: text/plain\r\n"
        "\r\n"
        "Action: failed\r\n"
        "Diagnostic-Code: smtp; 550 5.1.1\r\n"
        "Final-Recipient: rfc822; cust-a@test.com\r\n"
        "X-YXO-Forward-Id: %s\r\n" % FID
    ).encode("utf-8")
    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL",
               _make_imap({ACCT: [raw]})), \
         patch("mailbots_next.core.bounce.get_notifier"), \
         patch("mailbots_next.config.secrets.get_accounts",
               return_value={ACCT: "pw"}):
        stats = poll_bounces()
    assert stats["requeued"] == 1
    assert len(get_pending_errors(100)) == 1


def test_poll_never_writes_seen_and_uses_peek(monkeypatch):
    """轮询不碰已读标志：store 从未被调用；取信只用 BODY.PEEK。"""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    _poll_env(monkeypatch, poll=ACCT)
    _seed_log()
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: failed",
                    "Diagnostic-Code: smtp; 550",
                    "Final-Recipient: rfc822; cust-a@test.com"],
                   fid_header=FID)
    calls = {"fetch": [], "store": []}

    class _SpyIMAP:
        def __init__(self, *a, **k):
            pass

        def login(self, u, p):
            return "OK", []

        def select(self, f):
            return "OK", []

        def search(self, *a):
            return "OK", [b"1"]

        def fetch(self, num, item):
            calls["fetch"].append(item)
            return None, [(None, raw)]

        def store(self, *a):
            calls["store"].append(a)
            return "OK", []

        def logout(self):
            return "OK", []

    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL", _SpyIMAP), \
         patch("mailbots_next.core.bounce.get_notifier"), \
         patch("mailbots_next.config.secrets.get_accounts",
               return_value={ACCT: "pw"}):
        stats = poll_bounces()
    assert stats["requeued"] == 1
    assert calls["store"] == []
    assert calls["fetch"], "expected at least one fetch"
    for item in calls["fetch"]:
        assert "RFC822" not in item, item
        assert "BODY[]" not in item.replace("BODY.PEEK[]", ""), item
        assert item == "(UID)" or "BODY.PEEK" in item, item


def test_non_ndr_no_store_no_alert(monkeypatch):
    """正常客户邮件：不 store、不告警、计 skipped_non_ndr。"""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    _poll_env(monkeypatch, poll=ACCT)
    from email.mime.text import MIMEText
    normal = MIMEText("Daily hello.", "plain", "utf-8")
    normal["Subject"] = "hello"
    normal["From"] = "friend@example.com"
    calls = {"store": []}

    class _SpyIMAP:
        def __init__(self, *a, **k):
            pass

        def login(self, u, p):
            return "OK", []

        def select(self, f):
            return "OK", []

        def search(self, *a):
            return "OK", [b"1"]

        def fetch(self, num, item):
            return None, [(None, normal.as_bytes())]

        def store(self, *a):
            calls["store"].append(a)
            return "OK", []

        def logout(self):
            return "OK", []

    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL", _SpyIMAP), \
         patch("mailbots_next.core.bounce.get_notifier") as mock_n, \
         patch("mailbots_next.config.secrets.get_accounts",
               return_value={ACCT: "pw"}):
        stats = poll_bounces()
    assert stats["skipped_non_ndr"] == 1
    assert stats["processed"] == 0 and stats["requeued"] == 0
    assert calls["store"] == []
    mock_n.return_value.send_program_error.assert_not_called()


def test_handled_index_second_poll_no_realarm(monkeypatch):
    """handled 索引幂等：unknown 的 NDR 第二轮不再重复告警。"""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    _poll_env(monkeypatch, poll=ACCT)
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: failed",
                    "Diagnostic-Code: smtp; 550",
                    "Final-Recipient: rfc822; nobody@test.com"],
                   fid_header=None)

    def _run_once():
        with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL",
                   _make_imap({ACCT: [raw]})), \
             patch("mailbots_next.core.bounce.get_notifier") as mock_n, \
             patch("mailbots_next.config.secrets.get_accounts",
                   return_value={ACCT: "pw"}):
            return poll_bounces(), mock_n.return_value.send_program_error.call_count

    stats1, alerts1 = _run_once()
    assert stats1["unknown"] == 1 and alerts1 == 1
    stats2, alerts2 = _run_once()
    assert stats2["unknown"] == 0 and stats2["deduped"] == 1 and alerts2 == 0
    assert get_pending_errors(100) == []


def test_idempotent_second_poll_no_dup(monkeypatch):
    """Same NDR on two consecutive polls -> requeued exactly once."""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    _poll_env(monkeypatch, poll=ACCT)
    _seed_log()
    raw = _ndr_raw("postmaster@example.com",
                   ["Action: failed",
                    "Diagnostic-Code: smtp; 550",
                    "Final-Recipient: rfc822; cust-a@test.com"],
                   fid_header=FID)

    def _run_once():
        with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL",
                   _make_imap({ACCT: [raw]})), \
             patch("mailbots_next.core.bounce.get_notifier"), \
             patch("mailbots_next.config.secrets.get_accounts",
                   return_value={ACCT: "pw"}):
            return poll_bounces()

    assert _run_once()["requeued"] == 1
    assert _run_once()["requeued"] == 0
    assert len(get_pending_errors(100)) == 1
