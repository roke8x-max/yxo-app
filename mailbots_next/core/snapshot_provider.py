"""任务 33 / 需求3：共享的版本化 RecordsSnapshotProvider。

为什么需要它：serve.py 原来只有一个 MailProcessor（构造时 load_records 一次），
但 sweeper / repool / 手动转发各自 `MailProcessor(snapshot())` 重新读库 ⇒ 同一时刻
存在新旧两份 records，判定同一封邮件时会「一失败一成功」。本模块把 records 收敛成
唯一一份、带版本号：

* `current()` 是纯读：任何线程拿到的都是同一个 (version, records) 局部快照，
  **绝不 reload**（2000+ 行 × 12 线程无条件 reload 性能不可接受）。
* 只有主循环调用 `refresh_if_due()`（默认 60 秒，硬上限 300 秒）与显式事件
  `force_refresh()`（舱单导入提交 / repool requested / 手动转发前）会读库。
* 每次刷新版本 +1，判定 / 告警 / 转发日志打印同一个 version 便于反证。

唯一允许调用 `load_records()` 的位置就是本模块的 `_reload_locked()`。
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.settings import SNAPSHOT_REFRESH_SEC, SWEEP_INTERVAL_SEC
from .store import load_records


class RecordsSnapshotProvider:
    """线程安全的版本化快照；构造时立即读一次，之后按需刷新。"""

    def __init__(
        self,
        refresh_sec: Optional[int] = None,
        *,
        loader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        raw = SNAPSHOT_REFRESH_SEC if refresh_sec is None else int(refresh_sec)
        # 硬上限 = SWEEP_INTERVAL_SEC(300)，下限 1 秒（0/负数会让每轮都 reload）。
        self.refresh_sec = max(1, min(int(raw), int(SWEEP_INTERVAL_SEC)))
        self._loader = loader or load_records
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._version = 0
        self._records: List[Dict[str, Any]] = []
        self._last_refresh = float("-inf")
        self._reload_locked(self._clock())

    # ---------------------------------------------------------- 只读入口
    def current(self) -> Tuple[int, List[Dict[str, Any]]]:
        """返回 (version, records)，无 I/O、无等待刷新 —— poller 线程只走这里。"""
        with self._lock:
            return self._version, self._records

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def records(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._records

    # ---------------------------------------------------------- 刷新入口
    def refresh_if_due(self, now: Optional[float] = None) -> bool:
        """主循环每轮调用；只有到间隔才真正读库。返回是否刷新。"""
        now = self._clock() if now is None else float(now)
        with self._lock:
            if now - self._last_refresh < self.refresh_sec:
                return False
            self._reload_locked(now)
            return True

    def force_refresh(self) -> int:
        """显式事件触发（舱单导入提交 / repool requested / 手动转发前）。返回新版本。"""
        with self._lock:
            self._reload_locked(self._clock())
            return self._version

    # ---------------------------------------------------------- 内部
    def _reload_locked(self, now: Optional[float] = None) -> None:
        records = self._loader()
        self._records = list(records or [])
        self._version += 1
        self._last_refresh = self._clock() if now is None else float(now)
