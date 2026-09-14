"""F1-F4 regression locks for rev4.2.

All settings mutations via monkeypatch; all new tests in this file.
"""
import pytest
from email.mime.text import MIMEText
from unittest.mock import Mock, patch

from mailbots_next.core.bounce import (
    parse_ndr,
    is_ndr,
    poll_bounces,
    _looks_like_ndr_headers,
    _candidate_seqnos,
)
from mailbots_next.core.store import (
    init_bot_config_db,
    init_forward_log,
    get_bot_config_connection,
    seed_owner_mapping,
    write_forward_log,
)
from mailbots_next.core.dedup import init_db, get_pending_errors

FID = "a1b2c3d4e5f6a7b8-c0ffee12"
ACCT = "maoxiaoyang@cqtransit.com"


@pytest.fixture(autouse=True)
def _rev42_dbs(monkeypatch, tmp_path):
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
    monkeypatch.setattr(notify_mod, "DAILY_COUNTERS_PATH", tmp_path / "daily_counters.json")
    conn = sqlite3.connect(tmp_path / "yxo.db")
    try:
        conn.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY, ke TEXT, bx TEXT, tr TEXT, dst TEXT, co TEXT, src TEXT, st TEXT, is_deleted INTEGER, dsk TEXT, ATB TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    init_db()
    init_bot_config_db()
    init_forward_log()
    seed_owner_mapping()
    # reset bounce throttle / purge globals between tests
    import mailbots_next.core.bounce as bounce_mod
    bounce_mod._last_purge_date = None  # type: ignore
    yield


def _seed_log(fid=FID, to=("cust-a@test.com",), sent_by=ACCT, message_id="rev42-msg-1"):
    import mailbots_next.config as cfg_mod
    assert cfg_mod.is_live()
    write_forward_log(
        fid, message_id, "0",
        ACCT, "inbox-folder", 7,
        "subj", "up@example.com", "Mon, 01 Sep 2026 00:00:00 +0000",
        b"rev42-raw", list(to), [], sent_by,
    )


def _ndr_raw(to_hdr, body_lines, fid_header=None, top_is_mixed=True):
    """top_is_mixed=True => top Content-Type is multipart/mixed with nested delivery-status;
    this is the shape HEADER search misses (only top header matched)."""
    hdr = "X-YXO-Forward-Id: %s\r\n" % fid_header if fid_header else ""
    top_ct = "multipart/mixed" if top_is_mixed else "multipart/report; report-type=delivery-status"
    return (
        'Content-Type: %s; boundary="b123"\r\n'
        "To: %s\r\n"
        "From: MAILER-DAEMON@example.com\r\n"
        "\r\n"
        "--b123\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "%s\r\n"
        "--b123\r\n"
        "Content-Type: message/rfc822\r\n"
        "%s\r\n"
        "Returned message body\r\n"
        "--b123--\r\n" % (top_ct, to_hdr, "\r\n".join(body_lines), hdr)
    ).encode("utf-8")


def _make_imap(mails_by_acct):
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
            return "OK", [b" ".join(str(i+1).encode() for i in range(len(mails)))]
        def fetch(self, num, item):
            if item == "(UID)":
                uid = 1000 + int(num)
                return None, [(f"{int(num)} (UID {uid})".encode(), b"")]
            mails = mails_by_acct.get(self.acct, [])
            return None, [(None, mails[int(num)-1])]
        def store(self, *a):
            return "OK", []
        def logout(self):
            return "OK", []
    return _FakeIMAP


# ---------- F1 regression lock ----------

def test_f1_mixed_top_still_requeues(monkeypatch):
    """Top multipart/mixed with nested delivery-status (HEADER search misses it)
    must still be found and requeued (rev4 used full UNSEEN, F1 fix restores it)."""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    from mailbots_next.config import settings as cfg
    monkeypatch.setattr(cfg, "BOUNCE_MONITOR_ENABLED", True)
    monkeypatch.setattr(cfg, "BOUNCE_ENVELOPE_MODE", "sender")
    monkeypatch.setattr(cfg, "BOUNCE_POLL_ACCOUNTS", ACCT)
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_SERVER", "imap.test")
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_PORT", 993)
    monkeypatch.setattr(cfg, "BOUNCE_FOLDER", "INBOX")
    _seed_log()
    # Fake IMAP that would have made old HEADER search return empty:
    # search with HEADER arg -> [], search with UNSEEN -> [ndr].
    # With fixed _candidate_seqnos (UNSEEN only), the NDR is found.
    calls = []
    class _SpyIMAP:
        def __init__(self, *a, **k):
            self.acct = None
        def login(self, u, p):
            self.acct = u
            return "OK", []
        def select(self, f):
            return "OK", []
        def search(self, *a):
            calls.append(a)
            if len(a) >= 2 and "HEADER" in str(a[1]):
                return "OK", [b""]  # old path would have stopped here with []
            return "OK", [b"1"]
        def fetch(self, num, item):
            if item == "(UID)":
                return None, [(f"{int(num)} (UID {1000+int(num)})".encode(), b"")]
            if "BODY.PEEK[HEADER]" in item:
                return None, [(None, b"Content-Type: text/plain\r\nFrom: MAILER-DAEMON@example.com\r\nSubject: Undeliverable\r\n")]
            raw = _ndr_raw("postmaster@example.com",
                           ["Action: failed", "Diagnostic-Code: smtp; 550",
                            "Final-Recipient: rfc822; cust-a@test.com"],
                           fid_header=FID, top_is_mixed=True)
            return None, [(None, raw)]
        def store(self, *a):
            return "OK", []
        def logout(self):
            return "OK", []
    # _candidate_seqnos path: we call poll_bounces which uses _candidate_seqnos.
    # The above Spy ensures HEADER search (if still present) would have returned [].
    # Fixed code never calls it, so search called once with UNSEEN and succeeds.
    with patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL", _SpyIMAP), \
         patch("mailbots_next.core.bounce.get_notifier"), \
         patch("mailbots_next.config.secrets.get_accounts", return_value={ACCT: "pw"}):
        # Directly test _candidate_seqnos not calling HEADER search:
        from mailbots_next.core.bounce import _candidate_seqnos
        c = _SpyIMAP()
        c.search = lambda *a, **k: (calls.append(a), ("OK", [b"1"]))[1]
        seqs = _candidate_seqnos(c)
        assert seqs == [b"1"]
        assert not any("HEADER" in str(a) for a in calls), calls
        # And poll path still requeues this mixed-top NDR
        stats = poll_bounces()
    assert stats["requeued"] == 1
    assert get_pending_errors(100)


def test_f1_candidate_seqnos_only_unseen(monkeypatch):
    """_candidate_seqnos must call search exactly once with UNSEEN."""
    calls = []
    class _C:
        def search(self, *a, **k):
            calls.append(a)
            return "OK", [b"1 2"]
    seqs = _candidate_seqnos(_C())
    assert seqs == [b"1", b"2"]
    assert len(calls) == 1
    assert calls[0] == (None, "UNSEEN")


# ---------- F2 ----------

def test_f2_chinese_subject_prescreen():
    assert _looks_like_ndr_headers("Subject: \u9000\u4fe1\u901a\u77e5") is True
    assert _looks_like_ndr_headers("Subject: \u65e0\u6cd5\u6295\u9012 - test") is True

def test_f2_normal_mail_not_prescreened():
    hdr = "From: alice@example.com\r\nSubject: Weekly report\r\nContent-Type: text/plain\r\n"
    assert _looks_like_ndr_headers(hdr) is False
    # fail-open: no From at all -> still fetch
    assert _looks_like_ndr_headers("Content-Type: text/plain\r\n\r\n") is True


# ---------- G2 throttle + G3 inf ----------

def test_g2_bounce_poll_due_pure():
    """_bounce_poll_due pure function: boundary equal -> True, one short -> False."""
    from mailbots_next.serve import _bounce_poll_due
    from mailbots_next.config import BOUNCE_POLL_SEC
    t = 10000.0
    assert _bounce_poll_due(t, t - BOUNCE_POLL_SEC) is True
    assert _bounce_poll_due(t, t - BOUNCE_POLL_SEC + 1) is False
    # boundary equal
    assert _bounce_poll_due(t, t - BOUNCE_POLL_SEC) is True


def test_g2_run_sweeper_two_rounds_throttled(monkeypatch):
    """Drive real run_sweeper 2 rounds: second poll within BOUNCE_POLL_SEC is skipped."""
    import mailbots_next.serve as serve_mod
    from unittest.mock import patch

    monkeypatch.setattr(serve_mod, "BOUNCE_MONITOR_ENABLED", True)
    monkeypatch.setattr(serve_mod, "BOUNCE_POLL_SEC", 9999)
    serve_mod._last_bounce_poll_ts = float("-inf")

    # Make time.sleep drive the loop: 2nd sleep sets shutdown so loop exits after 2 iterations.
    sleeps = {"n": 0}

    def fake_sleep(sec):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            serve_mod._shutdown_event.set()

    with patch("mailbots_next.serve.time.sleep", side_effect=fake_sleep), \
         patch("mailbots_next.serve.poll_bounces") as mock_poll, \
         patch("mailbots_next.serve.sweep_once", return_value={}):
        serve_mod._shutdown_event.clear()
        serve_mod._last_bounce_poll_ts = float("-inf")
        serve_mod.run_sweeper()
        assert mock_poll.call_count == 1
    serve_mod._shutdown_event.clear()
    serve_mod._last_bounce_poll_ts = float("-inf")


def test_g4_known_limitation_neutral_from_no_marker_is_skipped():
    """【已知限制 · 登记不修】形态为「无 Content-Type 标记 + 中性 From +
    无关键词 Subject」的真 NDR 会被预筛跳过 → 漏判。
    这是为性能有意接受的取舍（彻底覆盖需全量下载全文）。
    本用例不是待修 bug，而是把该边界显式记录；若将来预筛改动使本用例失败，
    必须先重新评估该取舍。"""
    hdr = ("From: notify@example-mta.com\r\n"
           "Subject: Message 12345 could not be delivered\r\n"
           "Content-Type: multipart/mixed; boundary=\"x\"\r\n")
    assert _looks_like_ndr_headers(hdr) is False   # 记录现状（非期望修复）


def test_g3_last_bounce_poll_ts_inf():
    """G3: initial value is -inf so first tick always polls even with small uptime."""
    import pathlib
    src = pathlib.Path("mailbots_next/serve.py").read_text(encoding="utf-8")
    assert 'float("-inf")' in src
    from mailbots_next.serve import _bounce_poll_due
    # -inf -> any now should be due
    assert _bounce_poll_due(100.0, float("-inf")) is True
    assert _bounce_poll_due(0.5, float("-inf")) is True
