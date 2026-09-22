"""企微 program_error 通知折叠（2026-09-21）：全部用假 HTTP 桩，不打真企微。

时钟可注入：测试靠 monkeypatch notify_mod._now 推进时间，不用 sleep。
"""
import threading
from unittest.mock import Mock, patch

import pytest

import mailbots_next.core.notify as notify_mod
from mailbots_next.core.notify import (
    WeComNotifier,
    get_notifier,
    reset_notifier,
    get_counters,
)

ADMIN = "maoxiaoyang@cqtransit.com"


@pytest.fixture(autouse=True)
def _collapse_env(monkeypatch):
    """每条用例：窗口=60（默认）、时钟可控、state 干净、桩可计数。"""
    monkeypatch.delenv("NOTIFY_COLLAPSE_WINDOW_SEC", raising=False)
    monkeypatch.setattr(notify_mod, "NOTIFY_COLLAPSE_WINDOW_SEC",
                        notify_mod._DEFAULT_COLLAPSE_WINDOW)
    t = [1000.0]
    monkeypatch.setattr(notify_mod, "_now", lambda: t[0])
    reset_notifier()
    n = get_notifier()
    n._notify_by_name = Mock(return_value=(True, "wxwork"))
    yield t
    reset_notifier()


def _notifier():
    return get_notifier()


def _err(n, detail, error_id):
    from mailbots_next.config import OPS_OWNER_EMAIL
    return n.send_program_error(OPS_OWNER_EMAIL, error_id, detail)


# 1. 首条立即发
def test_first_error_sends_immediately():
    n = _notifier()
    assert _err(n, "SMTP timeout at relay", "e1") is True
    assert n._notify_by_name.call_count == 1


# 2. 窗口内同类 10 条：只发 1 次；_log.info 记 10 次（首条 window start + 9 条 collapsed）
def test_window_collapses_to_first_only():
    n = _notifier()
    with patch.object(notify_mod._log, "info") as mock_info:
        for i in range(10):
            assert _err(n, "SMTP timeout at relay", f"e{i}") is True
    assert n._notify_by_name.call_count == 1
    assert mock_info.call_count == 10


# 3. 窗口过期后再来一条：补发 1 条汇总 + 当前 1 条
def test_expired_window_flushes_summary_and_current(_collapse_env):
    t = _collapse_env
    n = _notifier()
    for i in range(10):
        _err(n, "SMTP timeout at relay", f"e{i}")
    assert n._notify_by_name.call_count == 1
    n._notify_by_name.reset_mock()
    t[0] += 61.0
    assert _err(n, "SMTP timeout at relay", "e10") is True
    assert n._notify_by_name.call_count == 2


# 4. 汇总正文：含计数、折叠数，原正文仍在，且无时间断言（防迟到失真）
def test_summary_body_keeps_original_and_counts(_collapse_env):
    t = _collapse_env
    n = _notifier()
    for i in range(10):
        _err(n, "SMTP timeout at relay", f"e{i}")
    t[0] += 61.0
    _err(n, "SMTP timeout at relay", "e10")
    bodies = [c.args[1] for c in n._notify_by_name.call_args_list]
    assert len(bodies) == 3
    summary = bodies[1]
    assert "同一批同类错误共发生 10 次" in summary
    assert "已折叠 9 条" in summary
    assert "最近" not in summary
    assert "SMTP timeout" in summary


# 4b. 过期汇总能被任何一条通知触发（修正 1 的锁）：forwarded 到来同样补发汇总
def test_expired_window_flushed_by_any_notify_type(_collapse_env):
    from mailbots_next.config import OPS_OWNER_EMAIL
    t = _collapse_env
    n = _notifier()
    for i in range(10):
        _err(n, "SMTP timeout at relay", f"e{i}")
    assert n._notify_by_name.call_count == 1
    t[0] += 61.0
    # 单收件人 forwarded（非折叠类型）：send_forwarded 默认发 2 收件人（=2 次请求），
    # 这里用单收件人直调 notify() 走同一闸门，保持"首条 + forwarded + 汇总 = 3"的算术。
    assert n.notify("forwarded", [ADMIN], "forwarded detail") is True
    # forwarded 那条照发 + 补发 1 条汇总 ⇒ 共 3 次请求
    assert n._notify_by_name.call_count == 3
    bodies = [c.args[1] for c in n._notify_by_name.call_args_list]
    summaries = [b for b in bodies if "同一批同类错误" in b]
    assert len(summaries) == 1
    assert "最近" not in summaries[0]
    assert "同一批同类错误共发生 10 次" in summaries[0]


# 5. 不同 detail 不合并
def test_different_details_not_merged():
    n = _notifier()
    _err(n, "no such table: foo", "e1")
    _err(n, "SMTP timeout at relay", "e2")
    assert n._notify_by_name.call_count == 2


# 6. error_id 不同、detail 相同 ⇒ 仍合并（专锁归一化）
def test_different_error_ids_still_collapse():
    # error_id 含不同字母（模拟 log.error() 的随机 uuid）：仅数字归一化救不了，
    # 必须把 [error_id=…] 整段去掉才会合并。
    n = _notifier()
    for ch in "abcdefghij":
        _err(n, "SMTP timeout at relay", f"id-{ch}-xyz")
    assert n._notify_by_name.call_count == 1


# 7. 非折叠类型一次都不折叠
def test_non_collapsible_types_never_collapse():
    from mailbots_next.config import OPS_OWNER_EMAIL
    n = _notifier()
    for _ in range(3):
        n.send_alarm(ADMIN, OPS_OWNER_EMAIL, "same alarm")
        n.send_pending(ADMIN, OPS_OWNER_EMAIL, "same pending")
        n.send_forwarded(ADMIN, OPS_OWNER_EMAIL, "same forwarded")
        n.send_digest(OPS_OWNER_EMAIL, {"forwarded": 1})
    # alarm/pending/forwarded 各 3 收件人×... 每类 3 次 notify，每次 2 收件人 → 6 调用；digest 3 次 × 1 收件人 → 3
    assert n._notify_by_name.call_count == 6 + 6 + 6 + 3


# 8. 关闭折叠：NOTIFY_COLLAPSE_WINDOW_SEC=0 ⇒ 10 条发 10 次
def test_collapse_disabled_sends_all(monkeypatch):
    monkeypatch.setattr(notify_mod, "NOTIFY_COLLAPSE_WINDOW_SEC", 0)
    n = _notifier()
    for i in range(10):
        _err(n, "SMTP timeout at relay", f"e{i}")
    assert n._notify_by_name.call_count == 10


# 9. 计数不受影响：折叠不增 notify_failed；真失败照常 +1
def test_collapse_does_not_touch_failure_counter():
    from mailbots_next.config import OPS_OWNER_EMAIL
    n = _notifier()
    before = get_counters().get("notify_failed", 0)
    for i in range(10):
        _err(n, "SMTP timeout at relay", f"e{i}")
    assert get_counters().get("notify_failed", 0) == before
    n._notify_by_name = Mock(return_value=(False, "wxwork"))
    # 新指纹 + 失败桩 ⇒ 真发送失败 ⇒ +1（收件人 1 个 ⇒ +1）
    _err(n, "brand new failure detail xyz", "efail")
    assert get_counters().get("notify_failed", 0) == before + 1


# 10. 多线程：10 线程并发同一指纹 ⇒ 总请求数 = 1
def test_concurrent_same_fingerprint_sends_once():
    n = _notifier()
    barrier = threading.Barrier(10)

    def worker(i):
        barrier.wait()
        _err(n, "SMTP timeout at relay", f"e{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert n._notify_by_name.call_count == 1


# 11. state 上界：600 个不同指纹 ⇒ 条目数 ==500，不抛异常
def test_collapse_state_bounded():
    # detail 必须用字母区分：只差数字会被归一化（数字→#）吞成同一个指纹，
    # state 恒为 1，断言永远成立、锁失效。将来别把字母"简化"回数字。
    def _unique_kind(i):
        a = chr(ord("a") + i % 26)
        b = chr(ord("a") + (i // 26) % 26)
        c = chr(ord("a") + (i // 26 // 26) % 26)
        return f"kind-{a}{b}{c}"

    from mailbots_next.config import OPS_OWNER_EMAIL
    n = _notifier()
    for i in range(600):
        n.send_program_error(OPS_OWNER_EMAIL, f"e{i}", _unique_kind(i))
    assert len(notify_mod._collapse_state) == 500
