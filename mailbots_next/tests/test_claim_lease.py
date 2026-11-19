# -*- coding: utf-8 -*-
"""任务 22-A1 / A3：dedup 认领互斥（lease + 单条原子 UPSERT）+ 在途件不重判。

对应 05_ACCEPTANCE 判据 5–11 的 T1–T7。

🔴 本文件必须能在**基线代码**上被 pytest 收集到：
顶部只 import 基线就有的名字；is_inflight（T6）/_repool_item（T7）一律
函数体内延迟 import —— 基线没有它们，顶格 import 会让整个文件 ImportError，
T1 一行都跑不到、拿不到"基线 RED 输出"。T7 读 stats 用 .get("inflight")。
"""
import sqlite3
import threading
from unittest.mock import Mock, patch

import pytest

# 基线就有的名字（顶格）
from mailbots_next.core.dedup import (
    DEDUP_DB_PATH,
    STATUS_UNRESOLVED,
    get_connection,
    get_status,
    init_db,
    join_key,
    set_status,
    try_claim,
)


@pytest.fixture(autouse=True)
def clean_dedup():
    """每个用例一份干净的 dedup 表（只用 conftest 指向的临时库）。"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM dedup")
        conn.execute("DELETE FROM error_queue")
        conn.execute("DELETE FROM alarm_state")
        conn.commit()
    finally:
        conn.close()
    yield


# ---------------------------------------------------------------- T1–T6 (A1)


def test_concurrent_claim_exactly_once():
    """T1：4 线程 Barrier 对齐，同时认领同一 (msg,row_key) ⇒ 恰好 1 个 True。

    基线：results.count(True) 通常 4（每个线程都 SELECT 到 unresolved 就返回 True），
    或有线程抛 IntegrityError —— 两者都算 RED，别为"凑出 4"改并发节奏。
    """
    results, errors = [], []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        try:
            results.append(try_claim("c-msg", "0:BOX"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 4, f"有线程抛异常: {errors}"
    assert results.count(True) == 1


def test_lease_blocks_then_expires_allows_reclaim():
    """T2：认领后立刻再认领 → False；租约改成过去 → True。"""
    assert try_claim("c-lease", "0:BOX") is True
    assert try_claim("c-lease", "0:BOX") is False
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE dedup SET lease_until = datetime('now', '-1 day') "
            "WHERE message_id = ?", (join_key("c-lease", "0:BOX"),))
        conn.commit()
    finally:
        conn.close()
    assert try_claim("c-lease", "0:BOX") is True


def test_sent_and_closed_not_reclaimable():
    """T3：set_status(sent) / set_status(closed) 后认领 → 都 False。"""
    for st in ("sent", "closed"):
        mid = "c3-" + st
        assert try_claim(mid, "0:BOX") is True
        assert set_status(mid, "0:BOX", st) is True
        assert try_claim(mid, "0:BOX") is False


def test_set_status_clears_lease():
    """T4：set_status 之后该行 lease_until 必须为空串（三种状态都验）。"""
    for st in ("sent", "closed", STATUS_UNRESOLVED):
        mid = "c4-" + st
        assert try_claim(mid, "0:BOX") is True
        set_status(mid, "0:BOX", st)
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT lease_until FROM dedup WHERE message_id = ?",
                (join_key(mid, "0:BOX"),)).fetchone()
        finally:
            conn.close()
        assert (row["lease_until"] or "") == ""


def test_legacy_three_column_table_migrates_to_twelve():
    """T5：3 列老表 → init_db() 两次 → 12 列 + 老行默认值 + 老 unresolved 可认领。"""
    conn = get_connection()
    try:
        conn.executescript(
            "DROP TABLE IF EXISTS dedup;"
            "CREATE TABLE dedup ("
            "    message_id TEXT PRIMARY KEY, claimed_at TEXT NOT NULL, claimed_by TEXT);"
        )
        conn.execute(
            "INSERT INTO dedup VALUES ('old-1', datetime('now'), 'legacy')")
        conn.commit()
    finally:
        conn.close()

    init_db()
    init_db()

    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(dedup)")]
        assert len(cols) == 12 and "lease_until" in cols
        row = conn.execute(
            "SELECT status, lease_until FROM dedup WHERE message_id='old-1'"
        ).fetchone()
        assert row["status"] == "sent" and (row["lease_until"] or "") == ""

        # ④ 空 row_key 时 join_key 返回裸 mid（否则 UPSERT 插新键 = 静默假绿）
        assert join_key("old-1", "") == "old-1"
        conn.execute("UPDATE dedup SET status='unresolved' WHERE message_id='old-1'")
        conn.commit()
    finally:
        conn.close()

    assert try_claim("old-1", "") is True
    conn = get_connection()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM dedup WHERE message_id LIKE 'old-1%'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_is_inflight_lifecycle():
    """T6：认领后 True / 过期后 False / sent 后 False / 键不存在 False。"""
    from mailbots_next.core.dedup import is_inflight  # 延迟 import（基线没有）

    assert is_inflight("c6-none", "0:BOX") is False
    assert try_claim("c6", "0:BOX") is True
    assert is_inflight("c6", "0:BOX") is True
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE dedup SET lease_until = datetime('now', '-1 day') "
            "WHERE message_id = ?", (join_key("c6", "0:BOX"),))
        conn.commit()
    finally:
        conn.close()
    assert is_inflight("c6", "0:BOX") is False
    assert try_claim("c6", "0:BOX") is True
    set_status("c6", "0:BOX", "sent")
    assert is_inflight("c6", "0:BOX") is False


# ---------------------------------------------------------------- T7 (A3)


def test_repool_item_respects_inflight():
    """T7：在途件不被误关；租约过期后允许正常重判。"""
    import mailbots_next.serve as serve_mod

    assert try_claim("c7-msg", "0:BOX") is True
    item = {"message_id": "c7-msg", "row_key": "0:BOX",
            "account": "a@x.com", "folder": "INBOX", "uid": 1,
            "subject": "s", "sender": "f@x.com", "date_hdr": ""}
    proc = Mock()
    proc._get_last_outcome.return_value = {}

    # 在途：这道闸必须在 _pool_alarm / 取原文之前 return
    stats = {}
    with patch.object(serve_mod, "_pool_replay_payload",
                      return_value=(b"raw", "s", "f", "d", "a", "INBOX", 1)), \
            patch.object(serve_mod, "_pool_alarm") as alarm:
        ret = serve_mod._repool_item(proc, item, stats)
    assert ret == STATUS_UNRESOLVED
    assert stats.get("inflight") == 1
    assert stats.get("unresolved", 0) == 0
    assert stats.get("closed", 0) == 0
    alarm.assert_not_called()

    # 租约过期后允许正常重判
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE dedup SET lease_until = datetime('now', '-1 day') "
            "WHERE message_id = ?", (join_key("c7-msg", "0:BOX"),))
        conn.commit()
    finally:
        conn.close()
    stats2 = {}
    with patch.object(serve_mod, "_pool_replay_payload",
                      return_value=(b"raw", "s", "f", "d", "a", "INBOX", 1)), \
            patch.object(serve_mod, "_pool_alarm"), \
            patch.object(serve_mod, "get_status", return_value=STATUS_UNRESOLVED):
        serve_mod._repool_item(proc, item, stats2)
    assert stats2.get("inflight", 0) == 0
    assert stats2.get("unresolved", 0) == 1
