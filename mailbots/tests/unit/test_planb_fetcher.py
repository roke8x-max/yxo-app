# -*- coding: utf-8 -*-
"""imap_fetcher 单测（spec §10）：utf7_encode / MailState / fetch_new / Idler。"""
import json
import os
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from core.imap_fetcher import (
    utf7_encode,
    MailState,
    fetch_new,
    Idler,
    IdleError,
)


# ============= Fake IMAP connection for testing =============

class FakeSock:
    """内存 socket：支持 send/recv 读行。"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def recv(self, size):
        if not self.responses:
            return b""
        return self.responses.pop(0)

    def makefile(self, *args, **kwargs):
        return self


class FakeConn:
    """最小 imaplib.IMAP4 模拟：select / uid / fetch / send / readline / socket。"""
    def __init__(self, folder_data, uidvalidity=1):
        self.folder_data = folder_data  # dict[uid] = raw_bytes
        self.uidvalidity = uidvalidity
        self.selected_folder = None
        self.sock = FakeSock([
            b"* OK IMAP ready\r\n",
            b"+ idling\r\n",  # IDLE 响应
        ])
        self._idle_done = False

    def select(self, folder, readonly=True):
        self.selected_folder = folder
        typ = "OK"
        data = [str(self.uidvalidity).encode(), b"1 EXISTS"]
        return typ, data

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            # args: "UID", "N:*"
            search_arg = args[0] if args else ""
            if search_arg.startswith("UID "):
                range_part = search_arg[4:]
                if ":" in range_part:
                    start_str = range_part.split(":")[0]
                    start = int(start_str)
                    uids = [uid for uid in self.folder_data.keys() if uid >= start]
                else:
                    uids = list(self.folder_data.keys())
                return "OK", [b" ".join(str(u).encode() for u in uids)]
        elif cmd == "FETCH":
            # args: "UID", "(BODY.PEEK[])"
            uids_str = args[0] if args else ""
            uids = [int(u) for u in uids_str.split(",")]
            lines = []
            for uid in uids:
                if uid in self.folder_data:
                    raw = self.folder_data[uid]
                    lines.append(f"* {uid} FETCH (BODY[] {{{len(raw)}}}\r\n".encode())
                    lines.append(raw + b"\r\n")
            lines.append(b"OK FETCH done\r\n")
            return "OK", lines
        return "OK", [b""]

    def send(self, data):
        self.sock.send(data)

    def readline(self):
        # 用于 IDLE 读取 untagged 行
        if self._idle_done:
            self._idle_done = False
            return b"* 2 EXISTS\r\n"
        return self.sock.recv(4096)

    def socket(self):
        return self.sock


# ============= Tests for utf7_encode =============

def test_utf7_encode_ascii_unchanged():
    assert utf7_encode("Inbox") == "Inbox"
    assert utf7_encode("Sent Items") == "Sent Items"


def test_utf7_encode_chinese_folder():
    # "草单运单号" → modified UTF-7 (RFC 3501)
    encoded = utf7_encode("草单运单号")
    # 正确的 modified UTF-7: UTF-16BE -> base64 -> replace '/' with ',' -> strip '=' -> wrap with &-
    assert encoded == "&g0lTVY,QU1VT9w-"
    # 圆周验证：手工解码 modified UTF-7
    import base64
    # 去掉 & 和 -
    b64_part = encoded[1:-1]
    # 替换 , 为 /
    std_b64 = b64_part.replace(",", "/")
    # 补齐 padding
    padding = 4 - len(std_b64) % 4
    if padding != 4:
        std_b64 += "=" * padding
    decoded_bytes = base64.b64decode(std_b64)
    decoded = decoded_bytes.decode("utf-16be")
    assert decoded == "草单运单号"


def test_utf7_encode_special_chars():
    # 包含 & 的需要转义为 &-
    encoded = utf7_encode("A&B")
    assert encoded == "A&-B"


# ============= Tests for MailState =============

def test_mailstate_get_update_atomic(tmp_path):
    state_path = tmp_path / "mailstate.json"
    state = MailState(str(state_path))

    # 初始为空
    assert state.get("acc@test", "Inbox") == (None, 0)

    # 更新
    state.update("acc@test", "Inbox", 123, 456)
    assert state.get("acc@test", "Inbox") == (123, 456)

    # 第二次更新
    state.update("acc@test", "Inbox", 123, 789)
    assert state.get("acc@test", "Inbox") == (123, 789)

    # 文件内容合法 JSON
    with open(state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["acc@test"]["Inbox"] == {"uidvalidity": 123, "last_uid": 789}

    # 原子写：并发更新不丢数据（模拟）
    def writer(acc, folder, uv, uid):
        s = MailState(str(state_path))
        s.update(acc, folder, uv, uid)

    threads = [threading.Thread(target=writer, args=("acc@test", "Inbox", 123, i)) for i in range(10, 20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = state.get("acc@test", "Inbox")
    assert final[0] == 123
    assert final[1] >= 10


def test_mailstate_uidvalidity_change_resets_last_uid(tmp_path):
    state_path = tmp_path / "mailstate.json"
    state = MailState(str(state_path))

    state.update("acc@test", "Inbox", 100, 50)
    # UIDVALIDITY 变化时，get 仍返回旧值，但 fetch_new 逻辑会检测并重置
    assert state.get("acc@test", "Inbox") == (100, 50)

    state.update("acc@test", "Inbox", 200, 0)  # 模拟 UIDVALIDITY 变化后全量重扫
    assert state.get("acc@test", "Inbox") == (200, 0)


# ============= Tests for fetch_new =============

def test_fetch_new_first_time_returns_all(tmp_path):
    state_path = tmp_path / "mailstate.json"
    state = MailState(str(state_path))

    folder_data = {
        1: b"Message-ID: <1@x>\r\n\r\nbody1",
        2: b"Message-ID: <2@x>\r\n\r\nbody2",
        3: b"Message-ID: <3@x>\r\n\r\nbody3",
    }
    conn = FakeConn(folder_data, uidvalidity=1)

    results = fetch_new(conn, "Inbox", state, "acc@test")
    assert len(results) == 3
    uids = [r[0] for r in results]
    assert uids == [1, 2, 3]
    assert state.get("acc@test", "Inbox") == (1, 3)


def test_fetch_new_second_time_returns_empty(tmp_path):
    state_path = tmp_path / "mailstate.json"
    state = MailState(str(state_path))

    folder_data = {1: b"raw1", 2: b"raw2"}
    conn = FakeConn(folder_data, uidvalidity=1)

    # 第一次
    fetch_new(conn, "Inbox", state, "acc@test")
    # 第二次无新邮件
    results = fetch_new(conn, "Inbox", state, "acc@test")
    assert results == []


def test_fetch_new_incremental_after_new_mail(tmp_path):
    state_path = tmp_path / "mailstate.json"
    state = MailState(str(state_path))

    folder_data = {1: b"raw1", 2: b"raw2"}
    conn = FakeConn(folder_data, uidvalidity=1)

    fetch_new(conn, "Inbox", state, "acc@test")  # 取前两封

    # 新邮件到达
    folder_data[3] = b"raw3"
    folder_data[4] = b"raw4"

    results = fetch_new(conn, "Inbox", state, "acc@test")
    assert len(results) == 2
    uids = [r[0] for r in results]
    assert uids == [3, 4]
    assert state.get("acc@test", "Inbox") == (1, 4)


def test_fetch_new_uidvalidity_change_triggers_full_rescan(tmp_path):
    state_path = tmp_path / "mailstate.json"
    state = MailState(str(state_path))

    folder_data = {1: b"raw1", 2: b"raw2"}
    conn = FakeConn(folder_data, uidvalidity=1)

    fetch_new(conn, "Inbox", state, "acc@test")  # UIDVALIDITY=1

    # UIDVALIDITY 变化（服务器重建文件夹）
    conn2 = FakeConn({1: b"new1", 2: b"new2", 3: b"new3"}, uidvalidity=2)
    results = fetch_new(conn2, "Inbox", state, "acc@test")

    # 应该全量重扫
    assert len(results) == 3
    uids = [r[0] for r in results]
    assert uids == [1, 2, 3]
    assert state.get("acc@test", "Inbox") == (2, 3)


# ============= Tests for Idler =============

def test_idler_fetch_idle_done_fetch_cycle(tmp_path):
    """驱动一轮：fetch_new → IDLE → EXISTS → DONE → fetch_new。"""
    state_path = tmp_path / "mailstate.json"
    state = MailState(str(state_path))

    folder_data = {1: b"raw1"}
    conn = FakeConn(folder_data, uidvalidity=1)

    calls = []

    def on_raw(account, folder, uid, raw):
        calls.append((account, folder, uid, raw))

    # 创建 Idler，使用极短的 max_idle 以便测试
    idler = Idler(
        account="acc@test",
        password="pass",
        folders_srv=["Inbox"],
        on_raw=on_raw,
        state_path=str(state_path),
        max_idle=1,  # 1秒即超时
        poll_fallback_secs=0,
    )

    # 替换连接创建
    with patch.object(idler, "_connect", return_value=conn):
        idler.start()
        time.sleep(0.5)  # 让线程跑一轮
        idler.stop()
        idler.join(timeout=2)

    # 验证回调被调用
    assert len(calls) >= 1
    assert calls[0][0] == "acc@test"
    assert calls[0][1] == "Inbox"
    assert calls[0][2] == 1


def test_idler_poll_fallback_mode(tmp_path):
    """poll_fallback_secs>0 时不使用 IDLE，改定时轮询。"""
    state_path = tmp_path / "mailstate.json"
    state = MailState(str(state_path))

    folder_data = {1: b"raw1"}
    conn = FakeConn(folder_data, uidvalidity=1)

    calls = []

    def on_raw(account, folder, uid, raw):
        calls.append((account, folder, uid, raw))

    idler = Idler(
        account="acc@test",
        password="pass",
        folders_srv=["Inbox"],
        on_raw=on_raw,
        state_path=str(state_path),
        max_idle=1740,
        poll_fallback_secs=1,  # 1秒轮询
    )

    with patch.object(idler, "_connect", return_value=conn):
        idler.start()
        time.sleep(1.5)
        idler.stop()
        idler.join(timeout=2)

    # 应该轮询了至少一次
    assert len(calls) >= 1


def test_idler_exponential_backoff_on_error(tmp_path):
    """异常时指数退避重连（base 30s cap 300s）——单测用短间隔验证逻辑。"""
    state_path = tmp_path / "mailstate.json"

    calls = []
    attempt = [0]

    def failing_connect():
        attempt[0] += 1
        if attempt[0] < 3:
            raise ConnectionError("simulated failure")
        folder_data = {1: b"raw1"}
        return FakeConn(folder_data, uidvalidity=1)

    def on_raw(account, folder, uid, raw):
        calls.append((account, folder, uid, raw))

    idler = Idler(
        account="acc@test",
        password="pass",
        folders_srv=["Inbox"],
        on_raw=on_raw,
        state_path=str(state_path),
        max_idle=1,
        poll_fallback_secs=0,
        base_backoff=0.1,
        max_backoff=0.5,
    )

    with patch.object(idler, "_connect", side_effect=failing_connect):
        idler.start()
        time.sleep(1)  # 等待重试（base_backoff=0.1s, 3次约0.3s）
        idler.stop()
        idler.join(timeout=3)

    # 应该重试并最终成功
    assert attempt[0] >= 3
    assert len(calls) >= 1


def test_idler_idle_rejected_fallback_to_poll(tmp_path):
    """服务器拒绝 IDLE（无 + 响应）→ 自动降级轮询并记日志。"""
    state_path = tmp_path / "mailstate.json"

    # 构造拒绝 IDLE 的连接
    class RejectIdleConn(FakeConn):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sock = FakeSock([
                b"* OK IMAP ready\r\n",
                b"- BAD IDLE not supported\r\n",  # 拒绝 IDLE
            ])

    folder_data = {1: b"raw1"}
    conn = RejectIdleConn(folder_data, uidvalidity=1)

    calls = []

    def on_raw(account, folder, uid, raw):
        calls.append((account, folder, uid, raw))

    idler = Idler(
        account="acc@test",
        password="pass",
        folders_srv=["Inbox"],
        on_raw=on_raw,
        state_path=str(state_path),
        max_idle=1,
        poll_fallback_secs=0,
    )

    with patch.object(idler, "_connect", return_value=conn):
        idler.start()
        time.sleep(1.5)
        idler.stop()
        idler.join(timeout=3)

    # 应该降级为轮询并仍能获取邮件
    assert len(calls) >= 1


def test_idler_stop_cleans_up(tmp_path):
    """stop() 后线程退出，不再回调。"""
    state_path = tmp_path / "mailstate.json"

    folder_data = {1: b"raw1"}
    conn = FakeConn(folder_data, uidvalidity=1)

    calls = []

    def on_raw(account, folder, uid, raw):
        calls.append((account, folder, uid, raw))

    idler = Idler(
        account="acc@test",
        password="pass",
        folders_srv=["Inbox"],
        on_raw=on_raw,
        state_path=str(state_path),
        max_idle=1,
        poll_fallback_secs=0,
    )

    with patch.object(idler, "_connect", return_value=conn):
        idler.start()
        time.sleep(0.3)
        idler.stop()
        idler.join(timeout=2)

    call_count_at_stop = len(calls)
    time.sleep(0.5)
    assert len(calls) == call_count_at_stop  # stop 后不再有新回调


if __name__ == "__main__":
    pytest.main([__file__, "-v"])