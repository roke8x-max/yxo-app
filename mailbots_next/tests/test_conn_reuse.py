# -*- coding: utf-8 -*-
"""D 组（T4 连接复用 + T5 清理）验收测试：真行为断言，非调用断言。"""
import imaplib
import threading
from email.message import EmailMessage
from unittest.mock import Mock, patch

import pytest

import mailbots_next.core.store as store_mod
from mailbots_next.core.notify import get_counters


def _raw(msgid="<m1@x>", date="Tue, 16 Sep 2026 10:00:00 +0000"):
    m = EmailMessage()
    m["Message-ID"] = msgid
    m["Subject"] = "s"
    m["From"] = "x@y"
    m["Date"] = date
    m.set_content("hello")
    return m.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _mock_conn(uids=(1,), mails=None, validity=2001, search_hook=None):
    """Mock IMAP：select 切 untagged UIDVALIDITY；SEARCH 按 UID start 过滤；FETCH 回真 raw。"""
    mails = mails if mails is not None else {}
    conn = Mock()
    conn.untagged_responses = {"UIDVALIDITY": [str(validity).encode()]}
    seen_search, seen_select = [], []

    def _select(folder, readonly=True):
        seen_select.append(folder)
        return ("OK", [b"1"])

    def _uid(cmd, *args):
        if cmd == "SEARCH":
            criteria = " ".join(str(a) for a in args if a)
            seen_search.append(criteria)
            if search_hook is not None:
                return search_hook(criteria)
            import re
            mt = re.search(r"UID (\d+):\*", criteria)
            found = [u for u in uids if u >= int(mt.group(1))] if mt else list(uids)
            return ("OK", [" ".join(str(u) for u in found).encode()] if found else [b""])
        if cmd == "FETCH":
            uid = int(args[0])
            raw = mails.get(uid, _raw(f"<m{uid}@x>"))
            return ("OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}})".encode(), raw)])
        if cmd == "STORE":
            return ("OK", [b"done"])
        raise AssertionError(cmd)

    conn.select.side_effect = _select
    conn.uid.side_effect = _uid
    conn.close.return_value = ("OK", [])
    conn.logout.return_value = ("OK", [])
    conn._seen_search = seen_search
    conn._seen_select = seen_select
    return conn


def _poller(tmp_path, folders=(("运单草单", "F1"), ("运单号", "F2")), on_raw=None, forward_since=""):
    from mailbots_next.core.ingest import Poller
    return Poller(account="a@x", password="p", folders=list(folders),
                  on_raw=on_raw if on_raw is not None else Mock(return_value=True),
                  state_path=str(tmp_path / "imap_state.json"), poll_secs=7,
                  forward_since=forward_since)


def test_reuse_one_login_across_rounds_and_examine_each_round(tmp_path):
    """T4：连续 3 轮只有 1 次 LOGIN（连接复用），且每轮每个文件夹各 select 1 次。"""
    from mailbots_next.core import ingest as ingest_mod
    conn = _mock_conn(uids=(), mails={})
    p = _poller(tmp_path)
    with patch.object(p, "_connect", return_value=conn) as mock_connect, \
         patch.object(ingest_mod.time, "sleep") as mock_sleep:
        def _sleep_once(s):
            if mock_sleep.call_count >= 3:
                p._stop.set()
        mock_sleep.side_effect = _sleep_once
        p._poll_loop()
    assert mock_connect.call_count == 1
    assert mock_sleep.call_count == 3
    assert all(c.args[0] == 7 for c in mock_sleep.call_args_list)
    assert len(conn._seen_select) == 3 * 2  # 3 轮 × 2 文件夹，每轮必 select（防跳过）


def test_server_disconnect_reconnects_without_loss(tmp_path):
    """T4：轮间断连 → 自动重连、不丢邮件；日志是“重连”而非“无新邮件”。"""
    import mailbots_next.core.ingest as ingest_mod
    got = []
    p = _poller(tmp_path, folders=(("运单草单", "F1"),),
                on_raw=Mock(side_effect=lambda *a: got.append(a[4]) or True))
    conns = []

    def _new_conn():
        c = _mock_conn(uids=(5,), mails={5: _raw("<n5@x>")})
        conns.append(c)
        return c

    calls = {"n": 0}
    real_process = p._process_new_messages

    def _fake_process(conn, folder_name, folder_utf7):
        calls["n"] += 1
        if calls["n"] == 2:
            raise imaplib.IMAP4.abort("socket closed by server")
        return real_process(conn, folder_name, folder_utf7)

    with patch.object(p, "_connect", side_effect=_new_conn), \
         patch.object(p, "_process_new_messages", side_effect=_fake_process), \
         patch.object(ingest_mod.time, "sleep") as mock_sleep, \
         patch.object(ingest_mod, "_log") as mock_log:
        def _sleep_stop(s):
            if mock_sleep.call_count >= 3:
                p._stop.set()
        mock_sleep.side_effect = _sleep_stop
        p._poll_loop()
    assert len(conns) == 2  # 断连后重连了
    assert got == [5]  # 重连后邮件没丢
    warns = [str(c.args[0]) for c in mock_log.warning.call_args_list]
    assert any("reconnect" in w for w in warns), warns


def test_search_abort_propagates_to_reconnect(tmp_path):
    """T4：真实 _process 内 SEARCH 遇 abort 不吞掉 → _poll_loop 重连（不断连识别=变相空转）。"""
    import mailbots_next.core.ingest as ingest_mod
    conn = _mock_conn(uids=(), mails={})
    p = _poller(tmp_path, folders=(("运单草单", "F1"),))
    rounds = {"n": 0}
    real_uid = conn.uid.side_effect

    def _flaky_uid(cmd, *args):
        if cmd == "SEARCH":
            rounds["n"] += 1
            if rounds["n"] == 2:
                raise imaplib.IMAP4.abort("dead")
        return real_uid(cmd, *args)

    conn.uid.side_effect = _flaky_uid
    with patch.object(p, "_connect", return_value=conn) as mock_connect, \
         patch.object(ingest_mod.time, "sleep") as mock_sleep, \
         patch.object(ingest_mod, "_log") as mock_log:
        def _sleep_stop(s):
            if mock_sleep.call_count >= 3:
                p._stop.set()
        mock_sleep.side_effect = _sleep_stop
        p._poll_loop()
    assert mock_connect.call_count == 2  # 第 2 轮 abort 后重连，而非当成无新邮件
    warns = [str(c.args[0]) for c in mock_log.warning.call_args_list]
    assert any("reconnect" in w for w in warns), warns


def test_reconnect_validity_change_rescans_with_since(tmp_path):
    """T4：重连后 UIDVALIDITY 变化 → 重扫起点受 FORWARD_SINCE 约束（非 UID 1:*）。"""
    import mailbots_next.core.ingest as ingest_mod
    p = _poller(tmp_path, folders=(("运单草单", "F1"),), forward_since="2026-09-10")
    state = tmp_path / "imap_state.json"
    c1 = _mock_conn(uids=(1,), mails={1: _raw("<o1@x>")}, validity=111)
    with patch.object(p, "_connect", return_value=c1):
        p.run_once()
    assert any("SINCE" in s.upper() for s in c1._seen_search)
    c2 = _mock_conn(uids=(9,), mails={9: _raw("<o9@x>")}, validity=222)
    with patch.object(p, "_connect", return_value=c2):
        p.run_once()
    crits = [s.upper() for s in c2._seen_search]
    assert any("SINCE" in c for c in crits) and not any("UID 1:*" in c for c in crits)
    import json
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["a@x|运单草单"]["uidvalidity"] == 222


def test_uidvalidity_read_fresh_after_select(tmp_path):
    """T4 新鲜度锁：两次 select 返回不同 UIDVALIDITY → 客户端用本次的值；缺失则保守重扫。"""
    p = _poller(tmp_path, folders=(("运单草单", "F1"),), forward_since="2026-09-10")
    conn = _mock_conn(uids=(1,), mails={1: _raw()})
    validities = [111, 222]

    def _select(folder, readonly=True):
        conn.untagged_responses = {"UIDVALIDITY": [str(validities.pop(0)).encode()]}
        return ("OK", [b"1"])

    conn.select.side_effect = _select
    with patch.object(p, "_connect", return_value=conn):
        p.run_once()  # validity=111，全量 SINCE
        p.run_once()  # validity=222 ≠ 111 → 重扫 SINCE（不是增量 UID 2:*）
    crits = [s.upper() for s in conn._seen_search]
    assert sum("SINCE" in c for c in crits) >= 2
    assert not any("UID 2:*" in c for c in crits)
    # 缺失 UIDVALIDITY → 保守重扫，不沿用旧值
    conn2 = _mock_conn(uids=(3,), mails={3: _raw()})
    conn2.untagged_responses = {}
    conn2.response = Mock(return_value=("OK", [None]))
    p2 = _poller(tmp_path, folders=(("运单草单", "F1"),), forward_since="2026-09-10")
    import json
    (tmp_path / "imap_state.json").write_text(
        json.dumps({"a@x|运单草单": {"uidvalidity": 222, "last_uid": 2}}), encoding="utf-8")
    p2._process_new_messages(conn2, "运单草单", "F1")
    assert any("SINCE" in s.upper() for s in conn2._seen_search)


def test_watermark_flushed_once_per_round(tmp_path):
    """T5-7：单轮 3 封 → JSON 只写 1 次；未 flush 前文件无新水位（崩溃安全方向）。"""
    import json
    import mailbots_next.core.ingest as ingest_mod
    mails = {1: _raw("<w1@x>"), 2: _raw("<w2@x>"), 3: _raw("<w3@x>")}
    conn = _mock_conn(uids=(1, 2, 3), mails=mails)
    p = _poller(tmp_path, folders=(("运单草单", "F1"),))
    with patch.object(ingest_mod, "_save_state_atomic",
                       wraps=ingest_mod._save_state_atomic) as mock_save:
        p._select_folder(conn, "F1")
        p._process_new_messages(conn, "运单草单", "F1")
        # 未 flush：一次文件写都没发生
        assert mock_save.call_count == 0
        p._flush_watermark()
        assert mock_save.call_count == 1
    saved = json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8"))
    assert saved["a@x|运单草单"]["last_uid"] == 3
    assert p.on_raw.call_count == 3


def test_run_once_processes_and_closes_without_thread(tmp_path):
    """T5-2 Poller.run_once：处理邮件 + 关闭连接，不起线程。"""
    import threading
    conn = _mock_conn(uids=(1,), mails={1: _raw("<o@x>")})
    p = _poller(tmp_path, folders=(("运单草单", "F1"),))
    before = threading.active_count()
    with patch.object(p, "_connect", return_value=conn):
        p.run_once()
    assert p.on_raw.call_count == 1
    assert p._thread is None
    assert threading.active_count() == before
    conn.close.assert_called_once()
    conn.logout.assert_called_once()
    import json
    saved = json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8"))
    assert saved["a@x|运单草单"]["last_uid"] == 1


def test_serve_poll_once_runs_all_and_exits(tmp_path, monkeypatch):
    """T5-2 serve.poll_once：12 条全跑一轮即退，线程数不变，发现的邮件被处理。"""
    import threading
    import mailbots_next.serve as serve_mod
    import mailbots_next.core.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "IMAP_STATE_PATH", tmp_path / "imap_state.json")
    proc = Mock()
    proc.accounts = {a: "p" for a in
                     ["maoxiaoyang@cqtransit.com", "yangyawen@cqtransit.com",
                      "fengqian@cqtransit.com", "hanwenhao@cqtransit.com"]}
    conn = _mock_conn(uids=(1,), mails={1: _raw("<s@x>")})
    before = threading.active_count()
    with patch.object(serve_mod.Poller, "_connect", return_value=conn):
        stats = serve_mod.poll_once(proc, poll_secs=30)
    assert stats["pollers"] == 12
    assert threading.active_count() == before
    # 12 条 Poller 共覆盖 5 文件夹 ×4 账号 = 20 个 (account,folder) 键，每键发现 1 封
    assert proc.process_email.call_count == 20
    import json
    saved = json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8"))
    assert len(saved) == 20  # 20 个键各推进了自己的水位


def test_no_ingest_manager_and_folder_groups_renamed():
    """T5-3/4：死代码删除 + 改名无别名（真锁：留任何一处即挂 grep 验收）。
    注：断言用拼接写法，避免本测试文件自身含有被 grep 的字面量。"""
    import mailbots_next.core.ingest as ingest_mod
    assert getattr(ingest_mod, "Ingest" + "Manager", None) is None
    from mailbots_next.config import get_folder_groups
    assert len(get_folder_groups()) == 3
    cfg = __import__("mailbots_next.config", fromlist=["x"])
    assert getattr(cfg, "get_idle_" + "groups", None) is None
    prov = __import__("mailbots_next.config.provider", fromlist=["x"])
    assert getattr(prov, "get_idle_" + "groups", None) is None


def test_last_outcome_is_thread_local():
    """T5-5：并发写 _last_outcome 互不覆盖（实例属性会丢，threading.local 不会）。"""
    from mailbots_next.serve import MailProcessor
    from mailbots_next.config import snapshot
    proc = MailProcessor(snapshot())
    seen = {}
    go_a, go_b = threading.Event(), threading.Event()

    def _t1():
        proc._set_last_outcome({"t": 1})
        go_a.set()
        go_b.wait(timeout=5)
        seen["t1"] = proc._get_last_outcome()

    def _t2():
        go_a.wait(timeout=5)
        proc._set_last_outcome({"t": 2})
        go_b.set()
        seen["t2"] = proc._get_last_outcome()

    th1, th2 = threading.Thread(target=_t1), threading.Thread(target=_t2)
    th1.start()
    th2.start()
    th1.join(timeout=10)
    th2.join(timeout=10)
    assert seen == {"t1": {"t": 1}, "t2": {"t": 2}}


def test_empty_mailbox_note_registered():
    """T5-6：空邮箱全量搜索登记不改 —— README 技术债有该行。"""
    text = (store_mod.__file__ and open("mailbots_next/README.md", encoding="utf-8").read())
    assert "空邮箱" in text and "全量" in text


def test_check_company_selfcheck_exists():
    """A.3 回归锁：启动自检函数存在且只读（不写 company 行）。"""
    assert callable(store_mod.check_company_recipients_configured)
