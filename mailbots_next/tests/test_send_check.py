"""任务 08「发送后自检」单测：classify / 两路取证 / 失败语义 / 告警收件人 / 落库。

全部用注入的桩或假 IMAP 连接，**任何测试都不连真邮箱**。
本文件风格与 test_bounce_rev4.py 一致：每模块 fixture 把 DB 路径改到 tmp_path。
中文断言文本用 \\u 转义（避免编辑器编码问题）。
"""
import json
import sqlite3

import pytest

from mailbots_next.core import send_check
from mailbots_next.core.send_check import (
    RESULT_AB,
    RESULT_A_ONLY,
    RESULT_B_ONLY,
    RESULT_NONE,
    RESULT_SKIP_ERR,
    classify,
    collect_qq,
    list_qq_folders,
    run_once,
)
from mailbots_next.core.notify import get_counters
from mailbots_next.core.store import (
    get_forward_log,
    get_pending_checks,
    init_bot_config_db,
    init_forward_log,
    mark_forward_bounced,
    seed_owner_mapping,
    write_forward_log,
)

ACCT_A = "maoxiaoyang@cqtransit.com"
ACCT_B = "yangyawen@cqtransit.com"
OPS = "maoxiaoyang@cqtransit.com"

QQ_ROOT = "&UXZO1mWHTvZZOQ-"


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def db(monkeypatch, tmp_path):
    """把三个库路径重绑到 tmp_path，并打开 live 闸门（write_forward_log 用）。"""
    import mailbots_next.config as cfg_mod
    import mailbots_next.core.dedup as dedup_mod
    import mailbots_next.core.notify as notify_mod
    import mailbots_next.core.store as store_mod

    for name, fname in (("YXO_DB_PATH", "yxo.db"),
                        ("BOT_CONFIG_DB_PATH", "bot_config.db"),
                        ("DEDUP_DB_PATH", "dedup.db")):
        target = tmp_path / fname
        for mod in (cfg_mod, store_mod, dedup_mod):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, target)
    monkeypatch.setattr(notify_mod, "DAILY_COUNTERS_PATH",
                        tmp_path / "daily_counters.json")
    monkeypatch.setenv("MAILBOT_MODE", "live")
    init_bot_config_db()
    init_forward_log()
    seed_owner_mapping()
    return tmp_path


def _add(fid, company="\u592a\u5e73\u6d0b", sent_by=ACCT_A, subject="subj",
         to=("cust@example.com",), cc=(), no_fid=False):
    """写一行 forward_log（live 闸门已由 fixture 打开）。"""
    write_forward_log(
        fid, "msg-" + fid, "0", sent_by, "INBOX", 1, subject,
        "from@example.com", "Mon, 01 Sep 2026 00:00:00 +0000", b"raw-bytes",
        list(to), list(cc), sent_by, company=company, no_fid=no_fid,
    )


def _cand(fid, company="\u592a\u5e73\u6d0b", sent_by=ACCT_A, subject="subj"):
    return {"forward_id": fid, "sent_by": sent_by, "company": company,
            "subject": subject,
            "to_list": json.dumps(["cust@example.com"]),
            "sent_at": "2026-10-30 00:00:00"}


def _set_sent_at(root, fid, modifier):
    """直接改 sent_at（write_forward_log 固定写 datetime('now')，造不出太新的行）。"""
    conn = sqlite3.connect(root / "bot_config.db")
    try:
        conn.execute("UPDATE forward_log SET sent_at=datetime('now', ?) "
                     "WHERE forward_id=?", (modifier, fid))
        conn.commit()
    finally:
        conn.close()


class _Stub:
    """记录调用次数的桩：probe 或 notifier 都能用。"""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args if args else kwargs)
        if self.exc is not None:
            raise self.exc
        return self.result


class _Recorder:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def send_send_check(self, recipients, detail, error_id=None):
        self.calls.append({"recipients": list(recipients), "detail": detail})
        return self.ok


class _ListOnly:
    """只有一个 list() 的假连接（专测文件夹枚举）。"""

    def __init__(self, names):
        self.names = list(names)

    def list(self):
        return "OK", ['(\\HasNoChildren) "/" "%s"' % n for n in self.names]


class _FakeIMAP:
    """假 IMAP4_SSL：可指定登录失败 / 单夹打开失败 / 单夹取数失败。"""

    def __init__(self, folders, mails, fail_select=(), fail_fetch=(),
                 login_exc=None):
        self.folders = list(folders)
        self.mails = dict(mails)
        self.fail_select = set(fail_select)
        self.fail_fetch = set(fail_fetch)
        self.login_exc = login_exc
        self.selected = []
        self.fetched = []

    def login(self, user, pwd):
        if self.login_exc:
            raise self.login_exc
        self.user = user
        return "OK", [b"LOGIN completed"]

    def list(self):
        return "OK", ['(\\HasNoChildren) "/" "%s"' % n for n in self.folders]

    def select(self, folder, readonly=True):
        if folder in self.fail_select:
            return "NO", [b"[CLIENTBUG] SELECT failed"]
        self.selected.append(folder)
        return "OK", [str(len(self.mails.get(folder, []))).encode()]

    def fetch(self, seqs, spec):
        folder = self.selected[-1]
        assert "BODY.PEEK[HEADER]" in spec          # 一律全头（P2）
        self.fetched.append((folder, seqs))
        if folder in self.fail_fetch:
            return "NO", [b"FETCH failed"]
        raws = self.mails.get(folder, [])
        wanted = [int(s) for s in seqs.split(",")]
        out = []
        for i in wanted:
            if 1 <= i <= len(raws):
                out.append((str(i).encode(), raws[i - 1]))
        return "OK", out

    def logout(self):
        return "BYE", [b""]


def _hdr(fid):
    return ("X-YXO-Forward-Id: %s\r\nSubject: s\r\n\r\n" % fid).encode()


@pytest.fixture
def fake_imap(monkeypatch):
    """把 send_check 的 IMAP4_SSL 换成可配置的假实现，并给上 QQ 凭据。"""
    state = {}

    def _install(**kwargs):
        fake = _FakeIMAP(**kwargs)
        state["fake"] = fake
        monkeypatch.setattr(send_check.imaplib, "IMAP4_SSL",
                            lambda *a, **k: fake)
        monkeypatch.setattr(send_check.settings, "QQ_VERIFY_USER", "qq@test.com")
        monkeypatch.setattr(send_check.settings, "QQ_VERIFY_PASSWORD", "qq-pwd")
        return fake

    return _install


# ---------------------------------------------------------------- 1-4 classify


def test_classify_ab():
    assert classify(True, True) == RESULT_AB


def test_classify_a_only():
    assert classify(True, False) == RESULT_A_ONLY


def test_classify_b_only():
    assert classify(False, True) == RESULT_B_ONLY


def test_classify_none():
    assert classify(False, False) == RESULT_NONE


# ---------------------------------------------------------------- 5 无候选


def test_no_candidates_no_imap_connection(db):
    sent = _Stub(set())
    qq = _Stub((set(), []))
    rec = _Recorder()

    res = run_once(sent_probe=sent, qq_probe=qq, notifier=rec)

    assert res["candidates"] == 0
    assert sent.calls == []          # 零调用：一个 IMAP 连接都不建
    assert qq.calls == []
    assert rec.calls == []


# ---------------------------------------------------------------- 6 延迟窗口


def test_pending_respects_delay_window(db):
    _add("fid-new")
    _set_sent_at(db, "fid-new", "-5 seconds")
    assert get_pending_checks(120, 200) == []

    _set_sent_at(db, "fid-new", "-600 seconds")
    rows = get_pending_checks(120, 200)
    assert [r["forward_id"] for r in rows] == ["fid-new"]


def test_run_once_does_not_refilter_injected_candidates(db):
    """入参即候选：即使 DB 里 sent_at 是「刚刚」，注入后照样核。"""
    _add("fid-now")
    rec = _Recorder()

    res = run_once(candidates=[_cand("fid-now")],
                   sent_probe=_Stub({"fid-now"}),
                   qq_probe=_Stub(({"fid-now"}, [])),
                   notifier=rec)

    assert res["checked"] == 1
    assert res["ab"] == 1
    assert get_forward_log("fid-now")["check_result"] == RESULT_AB


# ---------------------------------------------------------------- 7 路 A 单账号失败


def test_route_a_single_account_failure_defers_only_that_account(db):
    _add("fid-a", sent_by=ACCT_A)
    _add("fid-b", sent_by=ACCT_B)
    seen_since = []

    def sent_probe(account, password, since):
        seen_since.append(since)
        if account == ACCT_A:
            raise RuntimeError("login failed")
        return {"fid-b"}

    rec = _Recorder()
    res = run_once(candidates=[_cand("fid-a", sent_by=ACCT_A),
                               _cand("fid-b", sent_by=ACCT_B)],
                   sent_probe=sent_probe,
                   qq_probe=_Stub(({"fid-a", "fid-b"}, [])),
                   notifier=rec)

    assert res["failed_accounts"] == [ACCT_A]
    assert get_forward_log("fid-a")["checked_at"] == ""       # 留待下轮
    assert get_forward_log("fid-b")["check_result"] == RESULT_AB
    assert res["checked"] == 1
    assert len(seen_since) == 2
    assert all(len(s) == 11 and s[2] == "-" for s in seen_since)


def test_route_a_missing_password_defers_account(db, monkeypatch):
    _add("fid-x", sent_by="nobody@example.com")
    sent = _Stub({"fid-x"})

    res = run_once(candidates=[_cand("fid-x", sent_by="nobody@example.com")],
                   sent_probe=sent,
                   qq_probe=_Stub(({"fid-x"}, [])),
                   notifier=_Recorder())

    assert res["failed_accounts"] == ["nobody@example.com"]
    assert sent.calls == []                                    # 不拿空密码去 login
    assert get_forward_log("fid-x")["checked_at"] == ""


# ---------------------------------------------------------------- 8 路 B 失败语义


def test_route_b_login_failure_defers_whole_batch(db):
    _add("fid-1")
    _add("fid-2")
    rec = _Recorder()

    res = run_once(candidates=[_cand("fid-1"), _cand("fid-2")],
                   sent_probe=_Stub({"fid-1", "fid-2"}),
                   qq_probe=_Stub(exc=RuntimeError("login failed")),
                   notifier=rec)

    assert get_forward_log("fid-1")["checked_at"] == ""
    assert get_forward_log("fid-2")["checked_at"] == ""
    assert rec.calls == []                                     # 零告警
    assert res["checked"] == 0


def test_route_b_single_folder_failure_still_judges(db):
    _add("fid-1")
    rec = _Recorder()
    bad = QQ_ROOT + "/DSK"

    res = run_once(candidates=[_cand("fid-1")],
                   sent_probe=_Stub({"fid-1"}),
                   qq_probe=_Stub(({"fid-1"}, [bad])),
                   notifier=rec)

    assert res["failed_folders"] == [bad]
    assert get_forward_log("fid-1")["check_result"] == RESULT_AB
    assert rec.calls == []


def test_route_b_inbox_failure_defers_whole_batch(db, fake_imap):
    """INBOX 打开失败 ⇒ collect_qq 抛异常 ⇒ 整批搁置、零告警。"""
    _add("fid-1")
    fake_imap(folders=["INBOX", QQ_ROOT + "/DSK"],
              mails={"INBOX": [_hdr("fid-1")], QQ_ROOT + "/DSK": []},
              fail_select=["INBOX"])
    rec = _Recorder()

    res = run_once(candidates=[_cand("fid-1")],
                   sent_probe=_Stub({"fid-1"}),
                   qq_probe=collect_qq,
                   notifier=rec)

    assert get_forward_log("fid-1")["checked_at"] == ""
    assert rec.calls == []
    assert res.get("deferred") is True


def test_collect_qq_all_folders_failed_raises(fake_imap):
    """所有目标夹都失败（此例枚举里只有非 INBOX 夹）⇒ 视为路 B 失败。"""
    folder = QQ_ROOT + "/DSK"
    fake_imap(folders=[folder], mails={folder: [_hdr("fid-1")]},
              fail_fetch=[folder])
    with pytest.raises(RuntimeError):
        collect_qq(300)


def test_collect_qq_single_folder_failure_is_recorded(fake_imap):
    fake_imap(folders=["INBOX", QQ_ROOT + "/DSK"],
              mails={"INBOX": [_hdr("fid-1")], QQ_ROOT + "/DSK": [_hdr("fid-2")]},
              fail_fetch=[QQ_ROOT + "/DSK"])

    fids, failed = collect_qq(300)

    assert fids == {"fid-1"}
    assert failed == [QQ_ROOT + "/DSK"]


def test_collect_qq_login_failure_propagates(fake_imap):
    fake_imap(folders=["INBOX"], mails={},
              login_exc=RuntimeError("auth failed"))
    with pytest.raises(RuntimeError):
        collect_qq(300)


def test_qq_credentials_raise_when_empty(monkeypatch):
    monkeypatch.setattr(send_check.settings, "QQ_VERIFY_USER", "")
    monkeypatch.setattr(send_check.settings, "QQ_VERIFY_PASSWORD", "")
    import mailbots_next.config.secrets as secrets_mod
    monkeypatch.setattr(secrets_mod, "load_secrets", lambda: {})
    with pytest.raises(RuntimeError):
        send_check._qq_credentials()


# ---------------------------------------------------------------- 9-14 告警收件人


def test_a_only_alerts_ops_only(db):
    _add("fid-1", company="\u4e1c\u76df")          # 东盟 → yangyawen
    rec = _Recorder()

    res = run_once(candidates=[_cand("fid-1", company="\u4e1c\u76df")],
                   sent_probe=_Stub({"fid-1"}),
                   qq_probe=_Stub((set(), [])),
                   notifier=rec)

    assert res["a_only"] == 1
    assert len(rec.calls) == 1
    assert rec.calls[0]["recipients"] == [OPS]


def test_none_alerts_owner_and_ops_without_duplicates(db):
    _add("fid-1", company="\u4e1c\u76df")
    rec = _Recorder()

    run_once(candidates=[_cand("fid-1", company="\u4e1c\u76df")],
             sent_probe=_Stub(set()),
             qq_probe=_Stub((set(), [])),
             notifier=rec)

    assert len(rec.calls) == 1
    assert rec.calls[0]["recipients"] == sorted({OPS, "yangyawen@cqtransit.com"})


def test_none_owner_equals_ops_sends_once(db):
    """公司负责人恰好是洋 ⇒ 去重后只发一次。"""
    _add("fid-1", company="\u592a\u5e73\u6d0b")     # 太平洋 → maoxiaoyang
    rec = _Recorder()

    run_once(candidates=[_cand("fid-1", company="\u592a\u5e73\u6d0b")],
             sent_probe=_Stub(set()),
             qq_probe=_Stub((set(), [])),
             notifier=rec)

    assert rec.calls[0]["recipients"] == [OPS]


def test_b_only_alerts_ops_only(db):
    _add("fid-1", company="\u4e1c\u76df")
    rec = _Recorder()

    res = run_once(candidates=[_cand("fid-1", company="\u4e1c\u76df")],
                   sent_probe=_Stub(set()),
                   qq_probe=_Stub(({"fid-1"}, [])),
                   notifier=rec)

    assert res["b_only"] == 1
    assert rec.calls[0]["recipients"] == [OPS]


def test_a_only_same_company_merged_into_one_summary(db):
    _add("fid-1", company="\u4e1c\u76df")
    _add("fid-2", company="\u4e1c\u76df")
    rec = _Recorder()

    res = run_once(candidates=[_cand("fid-1", company="\u4e1c\u76df"),
                               _cand("fid-2", company="\u4e1c\u76df")],
                   sent_probe=_Stub({"fid-1", "fid-2"}),
                   qq_probe=_Stub((set(), [])),
                   notifier=rec)

    assert res["a_only"] == 2
    assert len(rec.calls) == 1                       # 合并成一条
    assert "2 \u5c01" in rec.calls[0]["detail"]      # 「2 封」
    assert "fid-1" in rec.calls[0]["detail"]
    assert rec.calls[0]["recipients"] == [OPS]


def test_a_only_different_companies_send_one_each(db):
    _add("fid-1", company="\u4e1c\u76df")
    _add("fid-2", company="\u592a\u5e73\u6d0b")
    rec = _Recorder()

    res = run_once(candidates=[_cand("fid-1", company="\u4e1c\u76df"),
                               _cand("fid-2", company="\u592a\u5e73\u6d0b")],
                   sent_probe=_Stub({"fid-1", "fid-2"}),
                   qq_probe=_Stub((set(), [])),
                   notifier=rec)

    assert res["a_only"] == 2
    assert len(rec.calls) == 2
    assert all(c["recipients"] == [OPS] for c in rec.calls)


def test_ab_writes_result_without_alert(db):
    _add("fid-1")
    rec = _Recorder()

    res = run_once(candidates=[_cand("fid-1")],
                   sent_probe=_Stub({"fid-1"}),
                   qq_probe=_Stub(({"fid-1"}, [])),
                   notifier=rec)

    assert res["ab"] == 1
    assert rec.calls == []
    row = get_forward_log("fid-1")
    assert row["check_result"] == RESULT_AB
    assert row["checked_at"] != ""


def test_none_alert_with_unknown_company_only_ops(db):
    """get_owner_email 返回 None 时不许把 None 传进 notify。"""
    _add("fid-1", company="\u4e0d\u5b58\u5728\u7684\u516c\u53f8")
    rec = _Recorder()

    run_once(candidates=[_cand("fid-1", company="\u4e0d\u5b58\u5728\u7684\u516c\u53f8")],
             sent_probe=_Stub(set()),
             qq_probe=_Stub((set(), [])),
             notifier=rec)

    assert rec.calls[0]["recipients"] == [OPS]


def test_alerted_counter_counts_sent_alerts(db):
    _add("fid-1", company="\u4e1c\u76df")
    _add("fid-2", company="\u592a\u5e73\u6d0b")
    rec = _Recorder()

    run_once(candidates=[_cand("fid-1", company="\u4e1c\u76df"),
                         _cand("fid-2", company="\u592a\u5e73\u6d0b")],
             sent_probe=_Stub(set()),
             qq_probe=_Stub((set(), [])),
             notifier=rec)

    assert get_counters().get("send_check_alerted") == 2


# ---------------------------------------------------------------- 15-18 去重 / 跳过


def test_two_runs_same_forward_id_no_duplicate(db):
    _add("fid-1")
    _set_sent_at(db, "fid-1", "-600 seconds")
    rec = _Recorder()

    first = run_once(sent_probe=_Stub({"fid-1"}),
                     qq_probe=_Stub(({"fid-1"}, [])),
                     notifier=rec)
    second = run_once(sent_probe=_Stub({"fid-1"}),
                      qq_probe=_Stub(({"fid-1"}, [])),
                      notifier=rec)

    assert first["checked"] == 1
    assert second["candidates"] == 0
    assert rec.calls == []


def test_checked_row_not_returned_by_pending(db):
    _add("fid-1")
    _set_sent_at(db, "fid-1", "-600 seconds")
    assert len(get_pending_checks(120, 200)) == 1

    run_once(sent_probe=_Stub({"fid-1"}),
             qq_probe=_Stub(({"fid-1"}, [])),
             notifier=_Recorder())

    assert get_pending_checks(120, 200) == []


def test_bounced_row_not_a_candidate(db):
    _add("fid-1")
    _set_sent_at(db, "fid-1", "-600 seconds")
    mark_forward_bounced("fid-1")

    assert get_pending_checks(120, 200) == []


def test_no_fid_row_is_skip_no_id_and_never_pending(db):
    _add("fid-nofid", no_fid=True)

    row = get_forward_log("fid-nofid")
    assert row["check_result"] == "SKIP_NO_ID"
    assert row["checked_at"] != ""
    assert get_pending_checks(0, 200) == []


# ---------------------------------------------------------------- 19-20b 枚举


def test_dynamic_enumeration_includes_new_folder():
    names = ["INBOX", QQ_ROOT + "/DSK", QQ_ROOT + "/\u65b0\u5efa\u5f52\u6863\u5939"]
    folders = list_qq_folders(_ListOnly(names))

    assert "INBOX" in folders
    assert QQ_ROOT + "/\u65b0\u5efa\u5f52\u6863\u5939" in folders


def test_excluded_folders_not_queried():
    names = ["INBOX", "Sent Messages", "Deleted Messages", "Drafts", "Junk"]
    folders = list_qq_folders(_ListOnly(names))

    assert folders == ["INBOX"]


def test_tracing_subfolder_skipped():
    names = ["INBOX", QQ_ROOT + "/DSK", QQ_ROOT + "/tracing"]
    folders = list_qq_folders(_ListOnly(names))

    assert QQ_ROOT + "/tracing" not in folders
    assert QQ_ROOT + "/DSK" in folders


def test_collect_qq_queries_only_enumerated_folders(fake_imap):
    fake = fake_imap(folders=["INBOX", QQ_ROOT + "/DSK", QQ_ROOT + "/tracing",
                              "Junk"],
                     mails={"INBOX": [_hdr("fid-1")],
                            QQ_ROOT + "/DSK": [_hdr("fid-2")],
                            QQ_ROOT + "/tracing": [_hdr("fid-3")]})

    fids, failed = collect_qq(300)

    assert fids == {"fid-1", "fid-2"}
    assert failed == []
    assert QQ_ROOT + "/tracing" not in fake.selected
    assert "Junk" not in fake.selected


# ---------------------------------------------------------------- 21-23 存储


def test_write_forward_log_roundtrip_all_columns(db):
    write_forward_log(
        "fid-rt", "msg-rt", "row-7", ACCT_A, "&j9BTVYNJU1U-", 42, "subj-rt",
        "sender-rt@example.com", "Tue, 02 Sep 2026 01:02:03 +0000", b"\x00\x01",
        ["to-rt@example.com"], ["cc-rt@example.com"], ACCT_B,
        company="\u4e1c\u76df",
    )
    row = get_forward_log("fid-rt")

    assert row["forward_id"] == "fid-rt"
    assert row["message_id"] == "msg-rt"
    assert row["row_key"] == "row-7"
    assert row["account"] == ACCT_A
    assert row["folder"] == "&j9BTVYNJU1U-"
    assert row["uid"] == 42
    assert row["subject"] == "subj-rt"
    assert row["sender"] == "sender-rt@example.com"
    assert row["raw_hex"] == "0001"
    assert json.loads(row["to_list"]) == ["to-rt@example.com"]
    assert json.loads(row["cc_list"]) == ["cc-rt@example.com"]
    assert row["sent_by"] == ACCT_B
    assert row["company"] == "\u4e1c\u76df"
    assert row["checked_at"] == ""
    assert row["bounced"] == 0


def test_init_forward_log_twice_is_idempotent(db):
    init_forward_log()
    init_forward_log()
    conn = sqlite3.connect(db / "bot_config.db")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(forward_log)")}
    finally:
        conn.close()
    assert {"company", "checked_at", "check_result"} <= cols


def test_missing_sender_marks_skip_err_and_counters(db):
    _add("fid-1")
    qq = _Stub((set(), []))

    res = run_once(candidates=[_cand("fid-1", sent_by="")],
                   sent_probe=_Stub(set()),
                   qq_probe=qq,
                   notifier=_Recorder())

    row = get_forward_log("fid-1")
    assert row["check_result"] == RESULT_SKIP_ERR
    counts = get_counters()
    assert counts.get("send_check_skip_no_sender") == 1
    assert counts.get("send_check_failed", 0) == 0     # 两个计数器不许串
    assert qq.calls == []                              # 无有效候选 ⇒ 不建连接
    assert res["skip_err"] == 1


# ---------------------------------------------------------------- 其它


def test_since_format_is_ddmonyyyy():
    from datetime import datetime, timezone
    s = send_check._since_ddmonyyyy(datetime(2026, 10, 1, 3, 4, 5, tzinfo=timezone.utc))
    assert s == "01-Oct-2026"
    s2 = send_check._since_ddmonyyyy(datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert s2 == "29-Oct-2026"


def test_record_forward_passes_company_and_no_fid(db):
    from mailbots_next.core.act import _record_forward

    class _Row:
        row_idx = 0

    ctx = {"_meta": {"account": ACCT_A, "folder": "INBOX", "uid": 9,
                     "subject": "s", "sender": "x@example.com",
                     "date_hdr": "Mon, 01 Sep 2026 00:00:00 +0000"},
           "_row_key": "row-1"}

    _record_forward("fid-p1", "msg-p1", _Row(), ctx, ["c@example.com"], [],
                    b"raw", ACCT_A, company="\u4e1c\u76df")
    _record_forward("fid-p2", "msg-p2", _Row(), ctx, ["c@example.com"], [],
                    b"raw", ACCT_A, company="\u4e1c\u76df", no_fid=True)

    assert get_forward_log("fid-p1")["company"] == "\u4e1c\u76df"
    assert get_forward_log("fid-p1")["check_result"] == ""
    assert get_forward_log("fid-p2")["check_result"] == "SKIP_NO_ID"


def test_send_send_check_method_and_digest_labels(db):
    from unittest.mock import Mock
    from mailbots_next.core.notify import get_notifier

    n = get_notifier()
    n.send_send_check([OPS], "detail-line")
    content = n._notify_by_name.call_args[0][1]
    assert "\u53d1\u9001\u81ea\u68c0" in content      # 发送自检

    n2 = get_notifier()
    n2._notify_by_name = Mock(return_value=(True, "wxwork"))
    n2.send_digest(OPS, {"send_check_alerted": 2,
                         "send_check_failed": 1,
                         "send_check_skip_no_id": 0})
    digest = n2._notify_by_name.call_args[0][1]
    assert "\u81ea\u68c0\u544a\u8b66\u6761\u6570" in digest      # 自检告警条数
    assert "\u81ea\u68c0\u6293\u53d6\u5931\u8d25" in digest      # 自检抓取失败
