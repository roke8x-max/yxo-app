"""NDR end-to-end: real forward -> forward_log -> real NDR requeue ->
real sweep replay (sender envelope mode).

Per companion spec section 1 (envelope in rev4 sender mode) and section 2
(plan A: IMAP refetch by UID). add_error is always real (never mocked);
all settings mutations go through monkeypatch. Pure ASCII source on purpose:
all CJK literals use backslash-u escapes.
"""
import json
import sqlite3
import pytest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from unittest.mock import Mock, MagicMock, patch

from mailbots_next.core.store import (
    init_bot_config_db,
    init_forward_log,
    init_bounce_handled,
    get_bot_config_connection,
    seed_owner_mapping,
    get_forward_log,
)
from mailbots_next.core.dedup import (
    init_db,
    add_error,
    get_pending_errors,
)
from mailbots_next.config import settings as _settings_mod

CODE = "CQWLJT260810001-SPB"
BOX = "CICU1000001"
# COMPANY is the 3-char company name, FOLDER the 4-char folder name,
# ENC the 3-char "encrypted" marker, OK the 2-char "normal" status,
# STATION the 3-char destination. Written as escapes to keep source ASCII.
COMPANY = "\u592a\u5e73\u6d0b"
FOLDER = "\u8fd0\u5355\u8349\u5355"
ENC = "\u5df2\u52a0\u5bc6"
OK = "\u6b63\u5e38"
STATION = "\u6d4b\u8bd5\u7ad9"
ACCT = "maoxiaoyang@cqtransit.com"
UID = 7

# yxo column names are inlined above as escapes.


@pytest.fixture(autouse=True)
def _e2e_dbs(monkeypatch, tmp_path):
    """Copy of test_p1_coverage._p1_dbs: wipe DBs, build tables, seed owner
    plus one company recipient row."""
    import mailbots_next.config as cfg_mod
    import mailbots_next.core.store as store_mod
    import mailbots_next.core.dedup as dedup_mod
    import mailbots_next.core.notify as notify_mod

    for name, fname in (("YXO_DB_PATH", "yxo.db"),
                        ("BOT_CONFIG_DB_PATH", "bot_config.db"),
                        ("DEDUP_DB_PATH", "dedup.db")):
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
            "\u5ba2\u6237\u7f16\u7801 TEXT, \u7bb1\u53f7 TEXT, \u73ed\u5217\u53f7 TEXT, \u76ee\u7684\u7ad9 TEXT, \u5f00\u7968\u5b50\u516c\u53f8\u540d\u79f0 TEXT, \u672c\u5730\u8d27\u6e90\u516c\u53f8 TEXT, "
            "\u72b6\u6001 TEXT, is_deleted INTEGER, dsk TEXT, ATB TEXT)"
        )
        conn.execute(
            "INSERT INTO records "
"(\u5ba2\u6237\u7f16\u7801, \u7bb1\u53f7, \u73ed\u5217\u53f7, \u76ee\u7684\u7ad9, \u5f00\u7968\u5b50\u516c\u53f8\u540d\u79f0, \u672c\u5730\u8d27\u6e90\u516c\u53f8, \u72b6\u6001, is_deleted, dsk, ATB)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (CODE, BOX, "WB1", STATION, COMPANY, "", OK, 0, "", ""),
        )
        conn.commit()
    finally:
        conn.close()
    init_db()
    init_bot_config_db()
    init_forward_log()
    init_bounce_handled()
    seed_owner_mapping()
    conn = get_bot_config_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra)
               VALUES (?, 'company', ?, ?, ?, '{}')""",
            ("all", COMPANY, json.dumps(["tp-a@test.com"]), json.dumps([])),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        yield
    finally:
        from mailbots_next.config.provider import invalidate_cache
        invalidate_cache()


def _draft_raw():
    # Attachment name carries the encrypted-draft marker (ENC) + box number.
    msg = MIMEMultipart()
    msg["Subject"] = "caodan " + CODE
    msg["From"] = "youlia@yxologistics.com"
    msg.attach(MIMEText("body", "plain"))
    att = MIMEBase("application", "pdf")
    att.set_payload(b"test")
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", "attachment",
                   filename="%s-260810-123456%s.pdf" % (BOX, ENC))
    msg.attach(att)
    return msg.as_bytes()


def _proc():
    from mailbots_next.serve import MailProcessor
    from mailbots_next.config import snapshot
    proc = MailProcessor(snapshot())
    proc.notifier = Mock()
    return proc


def _smtp_spy():
    server = Mock()
    server.login.return_value = None
    server.sendmail.return_value = {}
    ctx = MagicMock()
    ctx.__enter__.return_value = server
    cls = Mock(return_value=ctx)
    return cls, server


def _ndr_for(fid):
    # NOTE: delivery fields live in a plain text/plain part (not nested
    # message/delivery-status): Python's stdlib parser nests the latter with
    # an empty body, so hand-built delivery-status blocks lose their content.
    # The delivery-status shape itself is covered in test_bounce_rev4.py.
    return (
        "Content-Type: multipart/mixed; boundary=\"b1\"\r\n"
        "To: postmaster@example.com\r\n"
        "From: MAILER-DAEMON@example.com\r\n"
        "\r\n"
        "--b1\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "Action: failed\r\n"
        "Diagnostic-Code: smtp; 550 5.1.1 User unknown\r\n"
        "Final-Recipient: rfc822; tp-a@test.com\r\n"
        "--b1\r\n"
        "Content-Type: message/rfc822\r\n"
        "X-YXO-Forward-Id: %s\r\n"
        "\r\n"
        "Returned message body\r\n"
        "--b1--\r\n" % fid
    ).encode("utf-8")


def _imap_with(mails_by_acct):
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


def test_bounce_requeue_then_sweep_replays_end_to_end(monkeypatch):
    """Step1 real forward -> Step2 forward_log asserts -> Step3 real poll ->
    Step4 real requeue asserts -> Step5 real sweep replay (sender envelope)."""
    import re
    from mailbots_next.serve import sweep_once

    monkeypatch.setenv("MAILBOT_MODE", "live")
    monkeypatch.setattr(_settings_mod, "BOUNCE_ENVELOPE_MODE", "sender")
    proc = _proc()
    raw = _draft_raw()

    # Step 1: real forward path (only send_smtp stubbed)
    cls, server = _smtp_spy()
    with patch("smtplib.SMTP_SSL", cls):
        outcome = proc.process_email(ACCT, FOLDER, "e2e-msg-1", UID,
                                     "caodan " + CODE, "youlia@yxologistics.com",
                                     "", raw)
    assert server.sendmail.call_count == 1
    # sender envelope: envelope MAIL FROM must be the sending account itself
    env_from = server.sendmail.call_args.args[0]
    assert env_from == ACCT, env_from

    # Step 2: exactly one forward_log row, with full replay fields
    conn = get_bot_config_connection()
    try:
        rows = conn.execute("SELECT * FROM forward_log").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["raw_hex"] == raw.hex()
    assert row["account"] == ACCT and row["folder"] == FOLDER and row["uid"] == UID
    assert row["sent_by"] == ACCT
    assert row["row_key"] == "0:%s" % BOX
    assert re.match(r"^[0-9a-f]{16}-[0-9a-f]{8}$", row["forward_id"])
    fid = row["forward_id"]

    # Step 3: build NDR, run real poll_bounces
    monkeypatch.setattr(_settings_mod, "BOUNCE_MONITOR_ENABLED", True)
    monkeypatch.setattr(_settings_mod, "BOUNCE_POLL_ACCOUNTS", ACCT)
    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL",
               _imap_with({ACCT: [_ndr_for(fid)]})), \
         patch("mailbots_next.core.bounce.get_notifier") as mock_n, \
         patch("mailbots_next.config.secrets.get_accounts",
               return_value={ACCT: "pw"}):
        from mailbots_next.core.bounce import poll_bounces
        stats = poll_bounces()
    assert stats["processed"] == 1 and stats["requeued"] == 1
    mock_n.return_value.send_program_error.assert_called_once()

    # Step 4: genuinely requeued, payload intact
    pend = [e for e in get_pending_errors(100) if e["message_id"] == "e2e-msg-1"]
    assert len(pend) == 1
    assert pend[0]["error_type"] == "BOUNCE"
    assert pend[0]["raw_hex"] == raw.hex()
    assert pend[0]["account"] == ACCT and pend[0]["folder"] == FOLDER
    assert pend[0]["uid"] == UID
    assert get_forward_log(fid)["bounced"] == 1

    # Step 5: real sweep_once replay closes the loop
    cls2, server2 = _smtp_spy()
    with patch("smtplib.SMTP_SSL", cls2), \
         patch("mailbots_next.serve.enqueue_mark_seen") as mock_mark:
        stats = sweep_once(processor=proc)
    assert stats["retried"] >= 1
    assert [e for e in get_pending_errors(100)
            if e["message_id"] == "e2e-msg-1"] == []
    mock_mark.assert_called_once()
    assert mock_mark.call_args.args[:3] == (ACCT, FOLDER, UID)


def test_bounce_payloadless_never_replays(monkeypatch):
    """Negative: empty raw_hex with no account/uid -> no replay, attempt burned."""
    from mailbots_next.serve import sweep_once
    eid = add_error("e2e-nopayload-1", "0:X", "act", "BOUNCE",
                    "NDR bounce", "e2e-nopayload-e",
                    account="", folder="", uid=0, raw_bytes=b"")
    assert eid > 0
    cls, server = _smtp_spy()
    with patch("smtplib.SMTP_SSL", cls):
        stats = sweep_once()
    server.sendmail.assert_not_called()
    pend = [e for e in get_pending_errors(100)
            if e["message_id"] == "e2e-nopayload-1"]
    assert len(pend) == 1 and pend[0]["attempt_count"] == 1
    assert stats == {"retried": 1, "escalated": 0}


def test_sweep_refetch_by_uid_then_replays(monkeypatch):
    """Plan A: missing payload + account/uid present -> IMAP refetch -> replay."""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    from mailbots_next.serve import sweep_once
    proc = _proc()
    raw = _draft_raw()
    eid = add_error("e2e-refetch-1", "0:X", "act", "BOUNCE",
                    "NDR bounce", "e2e-refetch-e",
                    account=ACCT, folder=FOLDER, uid=UID,
                    subject="caodan " + CODE, sender="youlia@yxologistics.com",
                    date_hdr="", raw_bytes=b"")
    assert eid > 0
    cls, server = _smtp_spy()
    with patch("mailbots_next.core.ingest.fetch_raw_by_uid", return_value=raw) as mock_fetch, \
         patch("smtplib.SMTP_SSL", cls), \
         patch("mailbots_next.serve.enqueue_mark_seen"):
        stats = sweep_once(processor=proc)
    mock_fetch.assert_called_once_with(ACCT, FOLDER, UID)
    assert server.sendmail.call_count == 1
    assert [e for e in get_pending_errors(100)
            if e["message_id"] == "e2e-refetch-1"] == []
    assert stats["retried"] >= 1


def test_sweep_refetch_failure_burns_attempt(monkeypatch):
    """Plan A: refetch fails -> attempt burned, no send, no false success."""
    from mailbots_next.serve import sweep_once
    proc = _proc()
    eid = add_error("e2e-refetch-2", "0:X", "act", "BOUNCE",
                    "NDR bounce", "e2e-refetch-e2",
                    account=ACCT, folder=FOLDER, uid=UID, raw_bytes=b"")
    assert eid > 0
    cls, server = _smtp_spy()
    with patch("mailbots_next.core.ingest.fetch_raw_by_uid", return_value=None), \
         patch("smtplib.SMTP_SSL", cls):
        stats = sweep_once(processor=proc)
    server.sendmail.assert_not_called()
    pend = [e for e in get_pending_errors(100)
            if e["message_id"] == "e2e-refetch-2"]
    assert len(pend) == 1 and pend[0]["attempt_count"] == 1
    assert stats == {"retried": 1, "escalated": 0}
