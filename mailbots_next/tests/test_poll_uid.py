"""收信通路修复 spec 验收测试：轮询 + UID 水位线（Step 4.1/4.2/4.7，Step 2/3）。

4.1 真链路冒烟必须用真 TLS + 最小 IMAP 协议，禁止 patch imaplib.IMAP4_SSL。
本文件只在 4.1 中用真实 socket+TLS 服务器；其余单元测试可用 Mock conn。
"""
import email
import json
import socket
import ssl
import threading
from email.message import EmailMessage
from unittest.mock import Mock, patch

import pytest

from mailbots_next.config import BOT_CONFIG_DB_PATH, DEDUP_DB_PATH, DRAFT_NUMS_DB_PATH, DAILY_COUNTERS_PATH
from mailbots_next.core.dedup import init_db, add_error, get_pending_errors, has_pending_message
from mailbots_next.core.store import init_bot_config_db, init_forward_log, seed_owner_mapping


@pytest.fixture(autouse=True)
def _dbs(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILBOT_MODE", "test")
    for p in (BOT_CONFIG_DB_PATH, DEDUP_DB_PATH, DRAFT_NUMS_DB_PATH, DAILY_COUNTERS_PATH):
        try:
            if p.exists():
                p.unlink()
        except PermissionError:
            pass
    init_db()
    init_bot_config_db()
    init_forward_log()
    seed_owner_mapping()
    try:
        yield
    finally:
        from mailbots_next.config.provider import invalidate_cache
        invalidate_cache()


def _raw(msgid="<m1@x>", subject="草单 CQWLJT260810001", sender="youlia@yxologistics.com", date="Tue, 16 Sep 2026 10:00:00 +0000"):
    m = EmailMessage()
    m["Message-ID"] = msgid
    m["Subject"] = subject
    m["From"] = sender
    m["Date"] = date
    m.set_content("hello")
    raw = m.as_bytes()
    # IMAP literal 要求 CRLF；as_bytes 用 LF，统一转成 CRLF
    return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _mock_conn(uids=(1, 2), validity=2001, mails=None, forward_since=""):
    """Mock IMAP conn：select + uid(SEARCH/FETCH/STORE) + untagged_responses。"""
    mails = mails or {}
    conn = Mock()
    conn.select.return_value = ("OK", [b"1"])
    conn.untagged_responses = {"UIDVALIDITY": [str(validity).encode()]}
    seen_search = []
    seen_fetch = []

    def _uid(cmd, *args):
        if cmd == "SEARCH":
            criteria = " ".join(str(a) for a in args if a)
            seen_search.append(criteria)
            assert "UNSEEN" not in criteria.upper(), f"search must not contain UNSEEN: {criteria}"
            if "UID" in criteria and ":" in criteria:
                import re
                mt = re.search(r"UID (\d+):\*", criteria)
                start = int(mt.group(1)) if mt else 1
                found = [u for u in uids if u >= start]
            else:
                found = list(uids)
            return ("OK", [" ".join(str(u) for u in found).encode()] if found else [b""])
        if cmd == "FETCH":
            uid = int(args[0])
            item = str(args[1]) if len(args) > 1 else ""
            seen_fetch.append(item)
            raw = mails.get(uid, _raw(f"<m{uid}@x>"))
            return ("OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}})".encode(), raw)])
        if cmd == "STORE":
            return ("OK", [b"done"])
        raise AssertionError(cmd)
    conn.uid.side_effect = _uid
    conn.close.return_value = ("OK", [])
    conn.logout.return_value = ("OK", [])
    conn._seen_search = seen_search
    conn._seen_fetch = seen_fetch
    return conn


def _poller(tmp_path, conn_uids=(1,), mails=None, validity=2001, forward_since="", on_raw=None):
    from mailbots_next.core.ingest import Poller
    state = str(tmp_path / "imap_state.json")
    return Poller(account="a@x", password="p", folders=[("运单草单", "F1")],
                  on_raw=on_raw or Mock(return_value=True),
                  state_path=state, poll_secs=30, forward_since=forward_since)


# ---------------- 4.1 真链路冒烟：真 TLS + 最小 IMAP ----------------

class _FakeIMAPServer:
    """最小 IMAP over TLS：LOGIN/EXAMINE/UID SEARCH/UID FETCH/UID STORE/LOGOUT。"""

    def __init__(self, mails, uidvalidity=2001):
        import trustme
        self.mails = dict(mails)
        self.uidvalidity = uidvalidity
        self.seen_commands = []
        self.stored = []
        ca = trustme.CA()
        cert = ca.issue_cert("127.0.0.1")
        self._tmpdir = __import__("tempfile").mkdtemp()
        import pathlib
        self._cert = str(pathlib.Path(self._tmpdir) / "c.pem")
        self._key = str(pathlib.Path(self._tmpdir) / "k.pem")
        cert.cert_chain_pems[0].write_to_path(self._cert)
        # trustme 私钥写入
        with open(self._key, "wb") as f:
            f.write(cert.private_key_pem.bytes())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self._cert, self._key)
        self._ctx = ctx
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except Exception:
            pass
        self._sock.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except Exception:
                return
            try:
                conn = self._ctx.wrap_socket(raw, server_side=True)
            except Exception:
                raw.close()
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        f = conn.makefile("rwb")
        def _send(b):
            f.write(b + b"\r\n")
            f.flush()
        _send(b"* OK FakeIMAP ready")
        while True:
            line = f.readline()
            if not line:
                break
            try:
                text = line.decode("utf-8", "replace").strip()
            except Exception:
                break
            if not text:
                continue
            self.seen_commands.append(text)
            parts = text.split()
            tag = parts[0]
            cmd = parts[1].upper() if len(parts) > 1 else ""
            arg = " ".join(parts[2:]) if len(parts) > 2 else ""
            if cmd == "LOGIN":
                _send(f"{tag} OK LOGIN completed".encode())
            elif cmd in ("SELECT", "EXAMINE"):
                _send(f"* {len(self.mails)} EXISTS".encode())
                _send(f"* OK [UIDVALIDITY {self.uidvalidity}] done".encode())
                _send(f"{tag} OK {cmd} completed".encode())
            elif cmd == "UID" and arg.upper().startswith("SEARCH"):
                crit = arg[6:].strip()
                import re
                m = re.search(r"UID (\d+):\*", crit)
                if m:
                    start = int(m.group(1))
                    found = [u for u in sorted(self.mails) if u >= start]
                else:
                    found = sorted(self.mails)
                _send(("* SEARCH " + " ".join(str(u) for u in found)).encode())
                _send(f"{tag} OK SEARCH completed".encode())
            elif cmd == "UID" and arg.upper().startswith("FETCH"):
                import re
                m = re.search(r"FETCH\s+(\d+)", arg, re.I)
                uid = int(m.group(1)) if m else 0
                raw = self.mails.get(uid, b"")
                head = f"* 1 FETCH (UID {uid} BODY[] {{{len(raw)}}}".encode()
                f.write(head + b"\r\n")
                f.write(raw)
                f.write(b")\r\n")
                f.flush()
                _send(f"{tag} OK FETCH completed".encode())
            elif cmd == "UID" and arg.upper().startswith("STORE"):
                self.stored.append(arg)
                _send(b"* 1 FETCH (FLAGS (\\Seen) UID 1)")
                _send(f"{tag} OK STORE completed".encode())
            elif cmd in ("CLOSE", "LOGOUT"):
                _send(b"* BYE bye")
                _send(f"{tag} OK done".encode())
                break
            elif cmd == "CAPABILITY":
                _send(b"* CAPABILITY IMAP4rev1")
                _send(f"{tag} OK done".encode())
            else:
                _send(f"{tag} OK done".encode())
        try:
            f.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def test_true_link_smoke_tls_proves_callback(tmp_path):
    """4.1：真 TLS + 真 imaplib 客户端，断言 on_raw_calls >= 1，且全程无 UNSEEN。"""
    import imaplib
    import mailbots_next.core.ingest as ingest_mod
    mails = {1: _raw("<smoke1@x>")}
    srv = _FakeIMAPServer(mails).start()
    try:
        old_server, old_port = ingest_mod.IMAP_SERVER, ingest_mod.IMAP_PORT
        ingest_mod.IMAP_SERVER, ingest_mod.IMAP_PORT = "127.0.0.1", srv.port
        try:
            from mailbots_next.core.ingest import Poller
            calls = []

            def _on_raw(acct, folder, fu, mid, uid, subj, sender, date, raw):
                calls.append((mid, uid))
                return True

            p = Poller(account="a@x", password="p", folders=[("运单草单", "F1")],
                       on_raw=_on_raw, state_path=str(tmp_path / "imap_state.json"),
                       poll_secs=30)
            # 真客户端、真 TLS：禁止 patch imaplib.IMAP4_SSL
            conn = imaplib.IMAP4_SSL("127.0.0.1", srv.port, timeout=10)
            conn.login("a@x", "p")
            p._process_new_messages(conn, "运单草单", "F1")
            try:
                conn.logout()
            except Exception:
                pass
        finally:
            ingest_mod.IMAP_SERVER, ingest_mod.IMAP_PORT = old_server, old_port
    finally:
        srv.stop()
    assert len(calls) >= 1, "必须证明真有邮件被回调"
    assert calls[0][0] == "<smoke1@x>" and calls[0][1] == 1
    joined = "\n".join(srv.seen_commands).upper()
    assert "UNSEEN" not in joined
    assert "BODY.PEEK" in joined or "FETCH" in joined


def test_seen_mail_still_fetched_no_unseen(tmp_path):
    """4.7⑤：已标 Seen 的邮件仍被取到（搜索条件不含 UNSEEN）。"""
    raw = _raw("<seen1@x>")
    conn = _mock_conn(uids=(5,), mails={5: raw})
    p = _poller(tmp_path, on_raw=Mock(return_value=True))
    p._process_new_messages(conn, "运单草单", "F1")
    assert p.on_raw.call_count == 1
    assert all("UNSEEN" not in s.upper() for s in conn._seen_search)
    assert any("PEEK" in f.upper() or "BODY" in f.upper() for f in conn._seen_fetch)


def test_uid_fetch_failure_leaves_trace_and_holds_watermark(tmp_path):
    """整改项二：FETCH 非 OK / 空 payload 时 WARN（含 account/folder/uid）+ uid_fetch_empty 计数，且水位不推进。"""
    import json
    from mailbots_next.core.notify import get_counters
    # 情形 A：FETCH 返回非 OK
    conn_a = _mock_conn(uids=(1,), mails={1: _raw()})
    def _uid_fail(cmd, *args):
        if cmd == "SEARCH":
            return ("OK", [b"1"])
        if cmd == "FETCH":
            return ("NO", [b"cannot fetch"])
        return ("OK", [b""])
    conn_a.uid.side_effect = _uid_fail
    p = _poller(tmp_path, on_raw=Mock(return_value=True))
    with patch("mailbots_next.core.ingest._log") as mock_log:
        p._process_new_messages(conn_a, "运单草单", "F1")
    assert p.on_raw.call_count == 0
    warns = [str(c) for c in mock_log.warning.call_args_list]
    assert any("a@x" in w and "运单草单" in w and "1" in w for w in warns), warns
    assert get_counters().get("uid_fetch_empty", 0) == 1
    st = json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8")) if (tmp_path / "imap_state.json").exists() else {}
    assert st.get("a@x|运单草单", {}).get("last_uid", 0) == 0
    # 情形 B：FETCH OK 但空 payload
    conn_b = _mock_conn(uids=(2,), mails={})
    def _uid_empty(cmd, *args):
        if cmd == "SEARCH":
            return ("OK", [b"2"])
        if cmd == "FETCH":
            return ("OK", [b""])
        return ("OK", [b""])
    conn_b.uid.side_effect = _uid_empty
    with patch("mailbots_next.core.ingest._log") as mock_log2:
        p._process_new_messages(conn_b, "运单草单", "F1")
    assert p.on_raw.call_count == 0
    assert get_counters().get("uid_fetch_empty", 0) == 2
    st2 = json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8")) if (tmp_path / "imap_state.json").exists() else {}
    assert st2.get("a@x|运单草单", {}).get("last_uid", 0) == 0


def test_first_run_then_only_new(tmp_path):
    """4.7①：首次取全量、第二次只取新增（水位推进）。"""
    m1, m2 = _raw("<n1@x>"), _raw("<n2@x>")
    conn1 = _mock_conn(uids=(1, 2), mails={1: m1, 2: m2})
    p = _poller(tmp_path, on_raw=Mock(return_value=True))
    p._process_new_messages(conn1, "运单草单", "F1")
    assert p.on_raw.call_count == 2
    import json
    saved = json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8"))
    assert saved["a@x|运单草单"]["last_uid"] == 2
    # 第二轮：只有 UID 3 新增
    m3 = _raw("<n3@x>")
    conn2 = _mock_conn(uids=(3,), mails={3: m3}, validity=2001)
    # 注意 _mock_conn 的 SEARCH 按 start 过滤；直接复用同一 state
    from mailbots_next.core.ingest import Poller as _Poller
    p2 = _Poller(account="a@x", password="p", folders=[("运单草单", "F1")],
                 on_raw=p.on_raw, state_path=str(tmp_path / "imap_state.json"), poll_secs=30)
    p2._process_new_messages(conn2, "运单草单", "F1")
    assert p2.on_raw.call_count == 3
    assert "UID 3:*" in conn2._seen_search[0]


def test_uidvalidity_change_rescans_with_since(tmp_path):
    """4.7②：UIDVALIDITY 变化 → 全量重扫，且起点受 FORWARD_SINCE 约束（非 UID=1）。"""
    p = _poller(tmp_path, forward_since="2026-09-01", on_raw=Mock(return_value=True))
    conn1 = _mock_conn(uids=(1,), mails={1: _raw()}, validity=111)
    p._process_new_messages(conn1, "运单草单", "F1")
    assert any("SINCE" in s.upper() for s in conn1._seen_search)
    conn2 = _mock_conn(uids=(9,), mails={9: _raw("<r9@x>")}, validity=222)
    p._process_new_messages(conn2, "运单草单", "F1")
    crit = conn2._seen_search[0].upper()
    assert "SINCE" in crit and "08-SEP-2026" not in crit  # 起点是 FORWARD_SINCE，非 UID=1 裸扫
    import json
    saved = json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8"))
    assert saved["a@x|运单草单"]["uidvalidity"] == 222


def test_watermark_held_on_false_and_fatal(tmp_path):
    """4.7③：水位不推进的两种情形。(a)返回 False；(b)claim 后抛异常且入队失败。"""
    import json
    # (a) 返回 False → 不推进
    p = _poller(tmp_path, on_raw=Mock(return_value=False))
    conn = _mock_conn(uids=(1,), mails={1: _raw()})
    with patch("mailbots_next.core.notify.get_notifier"):
        p._process_new_messages(conn, "运单草单", "F1")
    assert not (tmp_path / "imap_state.json").exists() or \
        json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8")).get("a@x|运单草单", {}).get("last_uid", 0) == 0

    # (b) 致命异常 + 兜底入队失败 → 不推进（禁止把 claim 成功当推进条件）
    from mailbots_next.core import ingest as ingest_mod
    p2 = _poller(tmp_path, on_raw=Mock(side_effect=RuntimeError("fatal")))
    conn2 = _mock_conn(uids=(2,), mails={2: _raw("<f2@x>")})
    with patch.object(ingest_mod, "_load_state", return_value={}), \
         patch("mailbots_next.core.dedup.has_pending_message", return_value=False), \
         patch("mailbots_next.core.dedup.add_error", side_effect=RuntimeError("db down")), \
         patch("mailbots_next.core.notify.get_notifier"):
        p2._process_new_messages(conn2, "运单草单", "F1")
    st = json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8")) if (tmp_path / "imap_state.json").exists() else {}
    assert st.get("a@x|运单草单", {}).get("last_uid", 0) == 0


def test_forward_since_gate_and_historical_skip(tmp_path):
    """4.7④：首次运行受 FORWARD_SINCE 门禁 + historical_skip 推进水位防重扫。"""
    old = _raw("<old@x>", date="Tue, 01 Sep 2026 10:00:00 +0000")
    conn = _mock_conn(uids=(1,), mails={1: old})
    p = _poller(tmp_path, forward_since="2026-09-10", on_raw=Mock(return_value=True))
    p._process_new_messages(conn, "运单草单", "F1")
    assert p.on_raw.call_count == 0  # 门禁前历史邮件不回调
    import json
    saved = json.loads((tmp_path / "imap_state.json").read_text(encoding="utf-8"))
    assert saved["a@x|运单草单"]["last_uid"] == 1
    assert any("SINCE" in s.upper() for s in conn._seen_search)


def test_mark_seen_uses_uid_store(tmp_path, monkeypatch):
    """4.7⑥：mark-seen 走 UID STORE。"""
    monkeypatch.setenv("MAILBOT_MODE", "live")
    from mailbots_next.core.ingest import MarkSeenBatcher
    b = MarkSeenBatcher(flush_sec=9999)
    assert b.enqueue("maoxiaoyang@cqtransit.com", "运单草单", 11) is True
    with patch("mailbots_next.core.ingest.imaplib.IMAP4_SSL") as mock_cls, \
         patch("mailbots_next.config.secrets.get_accounts", return_value={"maoxiaoyang@cqtransit.com": "pw"}):
        conn = Mock()
        conn.login.return_value = None
        conn.select.return_value = ("OK", [])
        conn.uid.return_value = ("OK", [])
        conn.logout.return_value = None
        mock_cls.return_value = conn
        stats = b.flush()
    assert stats["failed"] == 0
    conn.uid.assert_called_once_with("STORE", "11", "+FLAGS", "(\\Seen)")
    assert conn.store.call_count == 0 if hasattr(conn, "store") else True


def test_process_email_contract_four_scenes():
    """4.7⑦：process_email 契约四场景 + 无重复入队 + 推进必有持久化事实。"""
    from mailbots_next.serve import MailProcessor
    from mailbots_next.config import snapshot
    from mailbots_next.core.extract import ExtractedRow
    from mailbots_next.config import EmailType
    proc = MailProcessor(snapshot())
    proc.records = [{"id": 1, "客户编码": "CQWLJT260810001", "箱号": "CICU1000001",
                     "company": "太平洋", "状态": "正常", "is_deleted": 0}]
    proc.accounts = {"t@t.com": "pw"}
    proc.notifier = Mock()
    rows = [ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                         customer_code="CQWLJT260810001", container_no="CICU1000001")]
    # 场景1：全成功 → True（mock 类型识别+路由，避免未分类/无路由干扰）
    from mailbots_next.core.routing import RoutingResult as _RR
    _ok_route = _RR("太平洋", "t@t.com", ["a@x"], [], "full_match", True)
    with patch.object(proc, "_identify_type", return_value="draft"), \
         patch("mailbots_next.serve.extract_email", return_value=rows), \
         patch("mailbots_next.serve.route_row", return_value=_ok_route), \
         patch("mailbots_next.serve.execute_action", return_value=(True, "forwarded")), \
         patch("mailbots_next.serve.get_sender_by_email", return_value=("t@t.com", "pw")):
        assert proc.process_email("t@t.com", "草单", "c-ok-1", 1, "s", "x@y", "", b"raw") is True
    assert [e for e in get_pending_errors(100) if e["message_id"] == "c-ok-1"] == []
    # 场景2：部分行失败内部已入队 → 仍 True
    with patch.object(proc, "_identify_type", return_value="draft"), \
         patch("mailbots_next.serve.extract_email", return_value=rows), \
         patch("mailbots_next.serve.route_row", return_value=_ok_route), \
         patch("mailbots_next.serve.execute_action", side_effect=RuntimeError("boom")):
        assert proc.process_email("t@t.com", "草单", "c-part-1", 2, "s", "x@y", "", b"raw") is True
    assert len([e for e in get_pending_errors(100) if e["message_id"] == "c-part-1"]) == 1
    # 场景3：返回 False（mock）→ 外层不推进（由 Poller 测，此处只验布尔可区分）
    assert proc.process_email.__annotations__.get("return") in (bool, "bool")
    # 场景4：致命异常（identify 抛）→ 内部入队 + True，无重复入队
    with patch.object(proc, "_identify_type", side_effect=RuntimeError("fatal-id")):
        assert proc.process_email("t@t.com", "草单", "c-fatal-1", 3, "s", "x@y", "", b"raw") is True
        assert proc.process_email("t@t.com", "草单", "c-fatal-1", 3, "s", "x@y", "", b"raw") is True
    pend = [e for e in get_pending_errors(100) if e["message_id"] == "c-fatal-1"]
    assert len(pend) == 1  # 幂等，无重复入队


def test_identify_exception_second_entry_no_duplicate_enqueue():
    """整改项三：同 message_id 第二次进 IDENTIFY_EXCEPTION 不重复入队（与 UNCLASSIFIED 对称）。"""
    from mailbots_next.serve import MailProcessor
    from mailbots_next.config import snapshot
    proc = MailProcessor(snapshot())
    proc.records = []
    proc.accounts = {"t@t.com": "pw"}
    proc.notifier = Mock()
    with patch.object(proc, "_identify_type", side_effect=RuntimeError("fatal-id")):
        assert proc.process_email("t@t.com", "草单", "dup-id-1", 1, "s", "x@y", "", b"raw") is True
    pend1 = [e for e in get_pending_errors(100) if e["message_id"] == "dup-id-1"]
    assert len(pend1) == 1
    with patch.object(proc, "_identify_type", side_effect=RuntimeError("fatal-id")), \
         patch("mailbots_next.serve.add_error") as mock_add:
        assert proc.process_email("t@t.com", "草单", "dup-id-1", 1, "s", "x@y", "", b"raw") is True
        mock_add.assert_not_called()
    pend2 = [e for e in get_pending_errors(100) if e["message_id"] == "dup-id-1"]
    assert len(pend2) == 1


def test_tracing_full_path_single_multi_miss():
    """4.3：经 process_email → _process_row → 运踪分支 → decide_tracing（禁止直调）。"""
    from mailbots_next.serve import MailProcessor
    from mailbots_next.config import snapshot
    from mailbots_next.core.extract import ExtractedRow
    from mailbots_next.core.routing import RoutingResult
    proc = MailProcessor(snapshot())
    proc.records = []
    proc.accounts = {"t@t.com": "pw"}
    proc.notifier = Mock()
    trow = [ExtractedRow(row_idx=0, email_type="tracing", train_no="WB123")]
    # 单公司 → forward
    single = RoutingResult("太平洋", "t@t.com", ["a@x"], [], "train_match", True)
    with patch("mailbots_next.serve.extract_email", return_value=trow), \
         patch("mailbots_next.serve.route_row", return_value=single), \
         patch("mailbots_next.serve.execute_action", return_value=(True, "forwarded")) as mock_act:
        assert proc.process_email("t@t.com", "Tracing", "tr-1", 1, "s", "tracing-system@yxologistics.com", "", b"r") is True
        assert mock_act.call_count == 1
    # 多公司 → N 次 forward
    multi = [RoutingResult("太平洋", "t@t.com", ["a@x"], [], "train_match", True),
             RoutingResult("东盟", "t@t.com", ["b@x"], [], "train_match", True)]
    with patch("mailbots_next.serve.extract_email", return_value=trow), \
         patch("mailbots_next.serve.route_row", return_value=multi), \
         patch("mailbots_next.serve.execute_action", return_value=(True, "forwarded")) as mock_act2:
        assert proc.process_email("t@t.com", "Tracing", "tr-2", 2, "s", "tracing-system@yxologistics.com", "", b"r") is True
        assert mock_act2.call_count == 2
    # 无命中 → skip（不进 error_queue）
    miss = RoutingResult("", None, [], [], "no_company_match", False)
    with patch("mailbots_next.serve.extract_email", return_value=trow), \
         patch("mailbots_next.serve.route_row", return_value=miss), \
         patch("mailbots_next.serve.execute_action") as mock_act3:
        assert proc.process_email("t@t.com", "Tracing", "tr-3", 3, "s", "tracing-system@yxologistics.com", "", b"r") is True
        mock_act3.assert_not_called()
    assert [e for e in get_pending_errors(100) if e["message_id"] == "tr-3"] == []


def test_data_dir_autocreate(tmp_path, monkeypatch):
    """4.4：data 目录删除后 TEST 模式启动不崩、自动建目录。"""
    import mailbots_next.config.settings as st
    import mailbots_next.core.dedup as dedup_mod
    import mailbots_next.core.store as store_mod
    d = tmp_path / "nodata"
    monkeypatch.setattr(st, "DEDUP_DB_PATH", d / "dedup.db")
    monkeypatch.setattr(dedup_mod, "DEDUP_DB_PATH", d / "dedup.db")
    monkeypatch.setattr(st, "BOT_CONFIG_DB_PATH", d / "bot_config.db")
    monkeypatch.setattr(store_mod, "BOT_CONFIG_DB_PATH", d / "bot_config.db")
    assert not d.exists()
    dedup_mod.init_db()
    store_mod.init_bot_config_db()
    assert (d / "dedup.db").exists() and (d / "bot_config.db").exists()
    # state 文件同样自动建父目录
    from mailbots_next.core.ingest import Poller
    p = Poller(account="a@x", password="p", folders=[("F", "F")],
               on_raw=Mock(return_value=True),
               state_path=str(d / "sub" / "imap_state.json"), poll_secs=30)
    p._advance_watermark("F", 1, 5)
    assert (d / "sub" / "imap_state.json").exists()


def test_poll_secs_failsafe_and_topology_count():
    """Step 1.2/1.4/1.0：poll_secs fail-safe + 恒 12 条。"""
    from mailbots_next.core.ingest import Poller
    import mailbots_next.serve as serve_mod
    assert Poller(account="a", password="p", folders=[], on_raw=Mock(),
                  state_path=":memory:", poll_secs=0).poll_secs == 30
    assert Poller(account="a", password="p", folders=[], on_raw=Mock(),
                  state_path=":memory:", poll_secs=-5).poll_secs == 30
    proc = Mock()
    proc.accounts = {a: "p" for a in
                     ["maoxiaoyang@cqtransit.com", "yangyawen@cqtransit.com",
                      "fengqian@cqtransit.com", "hanwenhao@cqtransit.com"]}
    with patch.object(serve_mod, "Poller") as mock_p:
        serve_mod.start_pollers(proc, poll_secs=0)
        try:
            assert mock_p.call_count == 12
        finally:
            serve_mod.stop_pollers()
