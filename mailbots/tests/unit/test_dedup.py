# -*- coding: utf-8 -*-
import hashlib
from core import dedup, events_store as es


def _conn(tmp_path):
    conn = es.connect(str(tmp_path / "e.db"))
    es.ensure_schema(conn)
    return conn


def test_try_claim_atomic_exactly_once(tmp_path):
    conn = _conn(tmp_path)
    assert dedup.try_claim(conn, "<m1@x>") is True
    assert dedup.try_claim(conn, "<m1@x>") is False   # 第二个进程/第二轮抢不到


def test_release_allows_retry(tmp_path):
    conn = _conn(tmp_path)
    dedup.try_claim(conn, "<m1@x>")
    dedup.release(conn, "<m1@x>")                      # 转发失败→释放
    assert dedup.try_claim(conn, "<m1@x>") is True     # 下轮重试


def test_synthetic_key_scoped_and_flagged(tmp_path):
    k1 = dedup.synthetic_key("a@cqtransit.com", "草单运单号", "x@y",
                             "Thu, 21 Aug 2026 10:00:00 +0800", "草单")
    k2 = dedup.synthetic_key("b@cqtransit.com", "草单运单号", "x@y",
                             "Thu, 21 Aug 2026 10:00:00 +0800", "草单")
    assert k1 != k2                                    # 跨账号作用域隔离
    expect = "SYN::" + hashlib.sha256(
        "|".join(["a@cqtransit.com", "草单运单号", "x@y",
                  "Thu, 21 Aug 2026 10:00:00 +0800", "草单"]).encode("utf-8")).hexdigest()[:32]
    assert k1 == expect
    conn = _conn(tmp_path)
    assert dedup.try_claim(conn, k1, synthetic=True) is True
    row = conn.execute("SELECT synthetic FROM dedup_global WHERE key=?", (k1,)).fetchone()
    assert row["synthetic"] == 1


def test_reclaim_stale(tmp_path):
    conn = _conn(tmp_path)
    dedup.try_claim(conn, "<old@x>")
    conn.execute("UPDATE dedup_global SET claimed_at=datetime('now','-7 hours') "
                 "WHERE key='<old@x>'")
    conn.commit()
    assert dedup.reclaim_stale(conn, hours=6) == 1
    assert dedup.try_claim(conn, "<old@x>") is True
