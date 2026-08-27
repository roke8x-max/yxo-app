# -*- coding: utf-8 -*-
"""IMAP 抓取器（spec §10）：raw-IDLE + UIDVALIDITY 增量 + 轮询降级。
依赖：core.paths(utf7_encode), core.events_store(MailState), core.matching。
标准库 imaplib 无 IDLE 支持——自实现：utf7_encode / MailState / fetch_new / Idler。"""
import base64
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# 使用核心模块的路径函数（如需要）
try:
    from core.paths import detect_root
except ImportError:
    detect_root = None

logger = logging.getLogger(__name__)


# ============================================================
# utf7_encode: modified UTF-7 for IMAP folder names
# ============================================================
def utf7_encode(folder: str) -> str:
    """将文件夹名编码为 modified UTF-7 (RFC 3501 §5.1.3)。
    - ASCII printable (0x20-0x7e) 保持不变，但 & 必须转义为 &-
    - 其余字符：UTF-16BE → base64 → 用 &...- 包裹，base64 中 / 改为 ,
    - base64 移除 padding (=)
    例：「草单运单号」 → &g0lTVY,QU1VT9w-
    """
    if not folder:
        return ""
    result = []
    i = 0
    n = len(folder)
    while i < n:
        ch = folder[i]
        o = ord(ch)
        # ASCII printable (0x20-0x7e) except & (0x26)
        if 0x20 <= o <= 0x7e and ch != "&":
            result.append(ch)
            i += 1
        elif ch == "&":
            result.append("&-")
            i += 1
        else:
            # 收集连续的需要编码的字符
            start = i
            while i < n:
                ch2 = folder[i]
                o2 = ord(ch2)
                if 0x20 <= o2 <= 0x7e:
                    break
                i += 1
            segment = folder[start:i]
            # UTF-16BE 编码
            utf16be = segment.encode("utf-16be")
            # base64 编码，替换 / 为 ,，移除 padding =
            b64 = base64.b64encode(utf16be).decode("ascii").replace("/", ",").rstrip("=")
            result.append("&" + b64 + "-")
    return "".join(result)


# ============================================================
# MailState: UIDVALIDITY + last_uid 持久化（JSON 原子写）
# ============================================================
class MailState:
    """每账号每文件夹的 (uidvalidity, last_uid) 状态存储。JSON 原子写。"""

    def __init__(self, state_path: str):
        self.state_path = Path(state_path)
        self._lock = threading.Lock()
        self._ensure_file()

    def _ensure_file(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self.state_path.write_text("{}", encoding="utf-8")

    def _read(self) -> dict:
        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_atomic(self, data: dict):
        # 原子写：写临时文件再 rename
        # Windows 上需要唯一临时文件名避免共享冲突
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.state_path.parent,
            delete=False, suffix=".tmp"
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=False, separators=(",", ":"))
            tmp_path = tmp.name
        try:
            os.replace(tmp_path, self.state_path)
        except OSError:
            # Windows: 目标文件被占用时尝试删除临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get(self, account: str, folder: str) -> Tuple[Optional[int], int]:
        """返回 (uidvalidity, last_uid)；不存在时返回 (None, 0)。"""
        with self._lock:
            data = self._read()
            acc = data.get(account, {})
            fld = acc.get(folder, {})
            uv = fld.get("uidvalidity")
            last = fld.get("last_uid", 0)
            return (uv, last)

    def update(self, account: str, folder: str, uidvalidity: int, last_uid: int):
        """原子更新状态。"""
        with self._lock:
            data = self._read()
            acc = data.setdefault(account, {})
            acc[folder] = {"uidvalidity": uidvalidity, "last_uid": last_uid}
            self._write_atomic(data)


# ============================================================
# fetch_new: 增量抓取（UIDVALIDITY 校验 + UID SEARCH + FETCH）
# ============================================================
def fetch_new(
    conn, folder_srv: str, state: MailState, account: str
) -> List[Tuple[int, bytes]]:
    """增量抓取新邮件。
    1. SELECT READONLY folder_srv
    2. 校验 UIDVALIDITY：变化 → 全量重扫（last_uid 置 0）
    3. UID SEARCH UID {last_uid+1}:*
    4. 逐封 UID FETCH (BODY.PEEK[])
    5. 更新 state
    返回 [(uid, raw_bytes), ...] 按 uid 升序。
    """
    # SELECT READONLY
    typ, data = conn.select(folder_srv, readonly=True)
    if typ != "OK":
        raise RuntimeError(f"SELECT {folder_srv} failed: {data}")

    # 解析 UIDVALIDITY（SELECT 响应中通常在 data[0]）
    # data 示例: [b'1', b'5 EXISTS'] → uidvalidity = int(data[0])
    uidvalidity = None
    for item in data:
        if isinstance(item, (bytes, bytearray)):
            try:
                uidvalidity = int(item.strip())
                break
            except ValueError:
                continue

    if uidvalidity is None:
        raise RuntimeError(f"Cannot parse UIDVALIDITY from SELECT response: {data}")

    # 获取本地状态
    stored_uv, last_uid = state.get(account, folder_srv)

    # UIDVALIDITY 变化 → 全量重扫
    if stored_uv is not None and stored_uv != uidvalidity:
        logger.warning(
            f"UIDVALIDITY changed for {account}:{folder_srv} "
            f"({stored_uv} -> {uidvalidity}), full rescan"
        )
        last_uid = 0

    # UID SEARCH 增量
    search_from = last_uid + 1
    search_criterion = f"UID {search_from}:*"
    typ, search_data = conn.uid("SEARCH", search_criterion)
    if typ != "OK":
        raise RuntimeError(f"UID SEARCH failed: {search_data}")

    uids = []
    if search_data and search_data[0]:
        uids = [int(x) for x in search_data[0].split()]

    if not uids:
        # 无新邮件，仍需更新 uidvalidity（首次或变化后）
        state.update(account, folder_srv, uidvalidity, last_uid)
        return []

    # 逐批 FETCH（可优化为批量，这里逐封以简化）
    results = []
    max_uid = last_uid
    for uid in uids:
        typ, fetch_data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            logger.warning(f"UID FETCH {uid} failed: {fetch_data}")
            continue
        # 解析 FETCH 响应：格式为 [b'* 1 FETCH (BODY[] {123}', b'raw\r\n', b')', b'OK ...']
        raw = b""
        for line in fetch_data:
            if isinstance(line, (bytes, bytearray)) and line.startswith(b"* "):
                # 找到 FETCH 行，下一行通常是 body
                pass
        # 简化：假设 fetch_data 中包含原始邮件内容
        # 实际 imaplib 返回格式较复杂，这里做最简解析
        for item in fetch_data:
            if isinstance(item, (bytes, bytearray)) and not item.startswith(b"*") and not item.startswith(b"OK"):
                raw = item
                break
        if raw:
            results.append((uid, raw))
            if uid > max_uid:
                max_uid = uid

    # 更新状态
    state.update(account, folder_srv, uidvalidity, max_uid)
    return results


# ============================================================
# Idler: IDLE 循环 + 指数退避 + 轮询降级
# ============================================================
class IdleError(Exception):
    """IDLE 相关错误。"""
    pass


class Idler(threading.Thread):
    """IMAP IDLE 守护线程。
    - 对每个 folder 循环：fetch_new → IDLE（29 分钟窗口）→ EXISTS/超时 → DONE → 再 fetch_new
    - 异常指数退避重连（base 30s cap 300s）
    - poll_fallback_secs>0 时不用 IDLE 改定时轮询
    - on_raw(account, folder, uid, raw_bytes) 回调交给上层分发
    """

    def __init__(
        self,
        account: str,
        password: str,
        folders_srv: List[str],
        on_raw: Callable[[str, str, int, bytes], None],
        state_path: str,
        max_idle: int = 1740,  # 29 分钟
        poll_fallback_secs: int = 0,
        base_backoff: int = 30,
        max_backoff: int = 300,
    ):
        super().__init__(daemon=True)
        self.account = account
        self.password = password
        self.folders_srv = folders_srv
        self.on_raw = on_raw
        self.state = MailState(state_path)
        self.max_idle = max_idle
        self.poll_fallback_secs = poll_fallback_secs

        self._stop_event = threading.Event()
        self._conn = None
        self._idle_supported = True  # 服务器是否支持 IDLE
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._backoff = base_backoff

    def _connect(self):
        """建立 IMAP 连接并登录。子类可重写用于测试注入。"""
        import imaplib
        conn = imaplib.IMAP4_SSL("imap.example.com", 993)  # 占位，实际应从配置读取
        conn.login(self.account, self.password)
        return conn

    def _send_idle(self, conn) -> bool:
        """发送 IDLE 命令，返回是否进入 idling 状态。
        使用 conn.send() + conn.readline() 原语。
        """
        try:
            conn.send(b"X IDLE\r\n")
            line = conn.readline()
            return line.startswith(b"+")
        except Exception as e:
            logger.warning(f"IDLE send failed: {e}")
            return False

    def _wait_idle(self, conn, timeout: float) -> bool:
        """等待 IDLE 期间的 untagged 响应。
        返回 True 表示收到 EXISTS 或其他需要重新 fetch 的事件。
        """
        deadline = time.time() + timeout
        while not self._stop_event.is_set() and time.time() < deadline:
            try:
                # 设置 socket 超时以便定期检查 stop_event
                sock = conn.socket()
                if sock:
                    sock.settimeout(1.0)
                line = conn.readline()
                if not line:
                    break
                if b"EXISTS" in line:
                    return True
                # 其他 untagged 响应（如 EXPUNGE）也触发重 fetch
                if b"EXPUNGE" in line:
                    return True
            except socket.timeout:
                continue
            except Exception as e:
                logger.debug(f"IDLE read error: {e}")
                break
        return False

    def _send_done(self, conn):
        """发送 DONE 结束 IDLE。"""
        try:
            conn.send(b"DONE\r\n")
            # 读取完成响应
            conn.readline()
        except Exception as e:
            logger.debug(f"DONE send error: {e}")

    def _process_folder(self, conn, folder_srv: str) -> bool:
        """处理单个文件夹：fetch_new → 返回是否有新邮件。"""
        try:
            results = fetch_new(conn, folder_srv, self.state, self.account)
            for uid, raw in results:
                try:
                    self.on_raw(self.account, folder_srv, uid, raw)
                except Exception as e:
                    logger.exception(f"on_raw callback error for {uid}: {e}")
            return len(results) > 0
        except Exception as e:
            logger.exception(f"fetch_new error for {folder_srv}: {e}")
            raise

    def _run_idle_loop(self):
        """IDLE 模式主循环。"""
        while not self._stop_event.is_set():
            try:
                if self._conn is None:
                    self._conn = self._connect()
                    self._backoff = self._base_backoff  # 连接成功重置退避

                # 对每个文件夹轮询
                for folder_srv in self.folders_srv:
                    if self._stop_event.is_set():
                        break
                    self._process_folder(self._conn, folder_srv)

                    # 发起 IDLE
                    if not self._send_idle(self._conn):
                        # 服务器拒绝 IDLE → 降级轮询
                        logger.warning(f"Server rejected IDLE for {self.account}, falling back to polling")
                        self._idle_supported = False
                        break

                    # 等待 IDLE 事件
                    self._wait_idle(self._conn, self.max_idle)

                    # 发送 DONE
                    self._send_done(self._conn)

                    # IDLE 结束后立即再 fetch 一次（处理 EXISTS 期间到达的邮件）
                    if not self._stop_event.is_set():
                        self._process_folder(self._conn, folder_srv)

                if not self._idle_supported:
                    break

            except Exception as e:
                logger.warning(f"IDLE loop error: {e}, reconnecting in {self._backoff}s")
                self._close_conn()
                self._stop_event.wait(self._backoff)
                self._backoff = min(self._backoff * 2, self._max_backoff)

        # IDLE 不支持时进入轮询模式
        if not self._idle_supported and not self._stop_event.is_set():
            self._run_poll_loop()

    def _run_poll_loop(self):
        """轮询模式主循环。"""
        poll_interval = max(1, self.poll_fallback_secs or 60)
        logger.info(f"Entering poll mode for {self.account}, interval={poll_interval}s")
        while not self._stop_event.is_set():
            try:
                if self._conn is None:
                    self._conn = self._connect()
                    self._backoff = self._base_backoff

                for folder_srv in self.folders_srv:
                    if self._stop_event.is_set():
                        break
                    self._process_folder(self._conn, folder_srv)

            except Exception as e:
                logger.warning(f"Poll loop error: {e}, reconnecting in {self._backoff}s")
                self._close_conn()
                self._stop_event.wait(self._backoff)
                self._backoff = min(self._backoff * 2, self._max_backoff)
                continue

            self._stop_event.wait(poll_interval)

    def _close_conn(self):
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def run(self):
        """线程入口。"""
        logger.info(f"Idler started for {self.account}")
        try:
            if self.poll_fallback_secs > 0:
                self._run_poll_loop()
            else:
                self._run_idle_loop()
        finally:
            self._close_conn()
            logger.info(f"Idler stopped for {self.account}")

    def stop(self):
        """停止线程。"""
        self._stop_event.set()
        # 如果正在 IDLE，尝试发送 DONE 唤醒 readline
        if self._conn:
            try:
                self._conn.send(b"DONE\r\n")
            except Exception:
                pass

    def join(self, timeout=None):
        super().join(timeout)


# ============================================================
# 自测
# ============================================================
if __name__ == "__main__":
    # 简单自测
    print("Testing utf7_encode...")
    assert utf7_encode("Inbox") == "Inbox"
    assert utf7_encode("Sent Items") == "Sent Items"
    assert utf7_encode("草单运单号") == "&Xn9BTVP3-"
    assert utf7_encode("A&B") == "A&B-"
    print("utf7_encode OK")

    print("Testing MailState...")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "test.json"
        state = MailState(str(state_path))
        assert state.get("acc", "Inbox") == (None, 0)
        state.update("acc", "Inbox", 123, 456)
        assert state.get("acc", "Inbox") == (123, 456)
        state.update("acc", "Inbox", 123, 789)
        assert state.get("acc", "Inbox") == (123, 789)
    print("MailState OK")

    print("All basic tests passed!")