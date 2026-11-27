"""任务 33：告警延后 / 诊断日志 / 共享快照的验收测试（逐文件运行）。

只使用 conftest 的临时 SQLite 库；不发真实邮件、不碰生产数据。
"""

import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mailbots_next import serve
from mailbots_next.config import EmailType, OPS_OWNER_EMAIL
from mailbots_next.config.settings import ALARM_FOLD_SEC, ALARM_GRACE_SEC, SNAPSHOT_REFRESH_SEC
from mailbots_next.core import dedup
from mailbots_next.core.snapshot_provider import RecordsSnapshotProvider
from mailbots_next.core.store import get_bot_config_connection

MID = "alarm-timing-test-msg"
RK = "row-1|box-1"


def _row(code=None, box="CICU1000001", train="WB1"):
    return SimpleNamespace(
        row_idx=0, customer_code=code, container_no=box, train_no=train,
        draft_category=None, waybill_rejected=False, raw_data={},
    )


def _route(success=False, company="", source="no_route"):
    return SimpleNamespace(
        success=success, company=company, responsible_person="",
        to_list=[], cc_list=[], route_source=source,
    )


def _write_forward(mid=MID, row_key=RK, company="", bounced=0):
    conn = get_bot_config_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO forward_log "
            "(forward_id, message_id, row_key, company, sent_at, bounced) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?)",
            (f"fl-{mid}-{row_key}", mid, row_key, company, bounced),
        )
        conn.commit()
    finally:
        conn.close()


def _age_pending(mid=MID, row_key=RK, seconds=200):
    conn = dedup.get_connection()
    try:
        conn.execute(
            "UPDATE alarm_pending SET first_failed_at=datetime('now', ?) "
            "WHERE msg_id=? AND row_key=?",
            (f"-{int(seconds)} seconds", mid, row_key),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def clean_alarm_state():
    conn = dedup.get_connection()
    try:
        for table in ("alarm_pending", "error_queue", "dedup", "alarm_state"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()
    bc = get_bot_config_connection()
    try:
        bc.execute("DELETE FROM forward_log")
        bc.commit()
    finally:
        bc.close()
    yield


def _processor(provider=None):
    return serve.MailProcessor(serve.snapshot(), provider=provider)


def _first_failure(proc, code=None, mid=MID, row_key=RK):
    """完整走 _process_row：claim → route(NO_ROUTE) → settle(unresolved)。"""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(serve, "try_claim", lambda *a, **k: True)
        mp.setattr(serve, "route_row", lambda *a, **k: _route())
        return proc._process_row(
            _row(code=code), row_key, EmailType.DSK, mid, b"raw",
            "acct", "folder", 1, "sender@example.com", "subject box", "",
        )


# ---------------------------------------------------------------- 需求1
def test_default_grace_is_180():
    assert ALARM_GRACE_SEC == 180
    assert SNAPSHOT_REFRESH_SEC == 60
    assert serve.ALARM_FLUSH_SEC == 60


def test_first_failure_keeps_evidence_but_defers_alarm():
    proc = _processor()
    alarms = []
    mp = pytest.MonkeyPatch()
    mp.setattr(proc, "_alarm_keyed", lambda *a, **k: alarms.append(a) or True)
    counters = []
    mp.setattr(serve, "increment_counter", lambda name: counters.append(name))
    try:
        outcome = _first_failure(proc)
    finally:
        mp.undo()
    assert outcome == "queued"
    assert alarms == []                                   # 首次失败零企微
    assert "routing_failed" in counters                   # 计数照常
    assert dedup.get_status(MID, RK) == dedup.STATUS_UNRESOLVED  # 进池照常
    assert dedup.is_error_unresolved(MID, RK)             # error_queue 照常
    state = dedup.get_alarm(MID, RK)
    assert state is not None and state["status"] == "pending"


def test_pending_not_sent_before_grace_even_at_retry_cap():
    dedup.upsert_pending_alarm(MID, RK, error_id="e1", detail="箱CICU1000001",
                               recipients=[OPS_OWNER_EMAIL])
    dedup.add_error(MID, RK, "routing", "NO_ROUTE", "x", "e1")
    conn = dedup.get_connection()
    try:
        conn.execute("UPDATE error_queue SET attempt_count=3 WHERE message_id=?", (MID,))
        conn.commit()
    finally:
        conn.close()
    proc = _processor()
    proc.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    stats = serve.flush_due_alarms(proc)
    assert stats["scanned"] == 0 and stats["sent"] == 0
    assert proc.notifier._notify_by_name.call_count == 0
    assert dedup.get_alarm(MID, RK)["status"] == "pending"


def test_due_alarm_fires_once_with_required_fields(caplog):
    dedup.upsert_pending_alarm(
        MID, RK, error_id="e1",
        detail="路由失败 箱CICU1000001 班列WB1 主题箱号邮件 发件人sender@example.com 客编-",
        recipients=[OPS_OWNER_EMAIL],
    )
    dedup.add_error(MID, RK, "routing", "NO_ROUTE", "x", "e1")
    _age_pending(seconds=200)
    proc = _processor()
    proc.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    with caplog.at_level(logging.INFO, logger="mailbots_next.serve"):
        stats = serve.flush_due_alarms(proc)
    assert stats["sent"] == 1
    assert dedup.get_alarm(MID, RK)["status"] == "sent"
    assert proc.notifier._notify_by_name.call_count == 1
    content = proc.notifier._notify_by_name.call_args[0][1]
    for field in ("箱CICU1000001", "班列WB1", "主题", "发件人", "客编"):
        assert field in content
    assert "alarm_due_fired" in caplog.text


def test_recheck_success_cancels_due_before_send():
    dedup.upsert_pending_alarm(MID, RK, error_id="e1", detail="箱CICU1000001",
                               recipients=[OPS_OWNER_EMAIL])
    dedup.add_error(MID, RK, "routing", "NO_ROUTE", "x", "e1")
    _age_pending(seconds=200)
    _write_forward()  # 到期前刚刚落账成功
    proc = _processor()
    proc.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    stats = serve.flush_due_alarms(proc)
    assert stats["cancelled"] == 1 and stats["sent"] == 0
    assert proc.notifier._notify_by_name.call_count == 0
    assert dedup.get_alarm(MID, RK)["status"] == "cancelled"


def test_success_on_pending_cancels_immediately():
    proc = _processor()
    _first_failure(proc)
    assert proc._cancel_alarm_on_forward(MID, RK, "forwarded", "") is False  # 没落账不撤
    assert dedup.get_alarm(MID, RK)["status"] == "pending"
    _write_forward()
    assert proc._cancel_alarm_on_forward(MID, RK, "forwarded", "") is True
    assert dedup.get_alarm(MID, RK)["status"] == "cancelled"
    assert serve.flush_due_alarms(proc)["scanned"] == 0


def test_first_failed_at_not_renewed_by_recheck_failure():
    proc = _processor()
    _first_failure(proc)
    conn = dedup.get_connection()
    try:
        conn.execute("UPDATE alarm_pending SET first_failed_at=? WHERE msg_id=? AND row_key=?",
                     ("2020-01-01 00:00:00", MID, RK))
        conn.commit()
    finally:
        conn.close()
    _first_failure(proc)  # 重判仍失败
    assert dedup.get_alarm(MID, RK)["first_failed_at"] == "2020-01-01 00:00:00"


def test_restart_recovery_and_no_duplicate():
    dedup.upsert_pending_alarm(MID, RK, error_id="e1", detail="箱CICU1000001",
                               recipients=[OPS_OWNER_EMAIL])
    dedup.add_error(MID, RK, "routing", "NO_ROUTE", "x", "e1")
    _age_pending(seconds=200)
    p1 = _processor()
    p1.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    assert serve.flush_due_alarms(p1)["sent"] == 1
    p2 = _processor()  # 模拟重启：新实例、同一 dedup.db
    p2.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    assert serve.flush_due_alarms(p2)["sent"] == 0
    assert p2.notifier._notify_by_name.call_count == 0
    assert dedup.get_alarm(MID, RK)["status"] == "sent"


def test_concurrent_upsert_creates_one_pending_and_one_alarm():
    def upsert(_):
        dedup.upsert_pending_alarm(MID, RK, error_id="e1", detail="箱CICU1000001",
                                   recipients=[OPS_OWNER_EMAIL])

    threads = [threading.Thread(target=upsert, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = dedup.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM alarm_pending").fetchone()[0] == 1
    finally:
        conn.close()
    dedup.add_error(MID, RK, "routing", "NO_ROUTE", "x", "e1")
    _age_pending(seconds=200)
    proc = _processor()
    proc.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    stats = serve.flush_due_alarms(proc)
    assert stats["scanned"] == 1 and stats["sent"] == 1
    assert proc.notifier._notify_by_name.call_count == 1


def test_due_state_is_cancelled_if_success_wins_race():
    dedup.upsert_pending_alarm(MID, RK, error_id="e1", detail="箱CICU1000001",
                               recipients=[OPS_OWNER_EMAIL])
    dedup.add_error(MID, RK, "routing", "NO_ROUTE", "x", "e1")
    _age_pending(seconds=200)
    # 模拟 CAS 抢到 due 之后、发送之前成功落账
    conn = dedup.get_connection()
    try:
        conn.execute("UPDATE alarm_pending SET status='due' WHERE msg_id=?", (MID,))
        conn.commit()
    finally:
        conn.close()
    _write_forward()
    proc = _processor()
    proc.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    stats = serve.flush_due_alarms(proc)
    assert stats["cancelled"] == 1 and stats["sent"] == 0
    assert dedup.get_alarm(MID, RK)["status"] == "cancelled"


def test_24h_fold_still_applies_to_due_alarm():
    dedup.bump_alarm(f"alarm|{MID}|{RK}", ALARM_FOLD_SEC)
    dedup.upsert_pending_alarm(MID, RK, error_id="e1", detail="箱CICU1000001",
                               recipients=[OPS_OWNER_EMAIL])
    dedup.add_error(MID, RK, "routing", "NO_ROUTE", "x", "e1")
    _age_pending(seconds=200)
    proc = _processor()
    proc.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    stats = serve.flush_due_alarms(proc)
    assert stats["suppressed"] == 1 and stats["sent"] == 0
    assert dedup.get_alarm(MID, RK)["status"] == "suppressed"
    assert proc.notifier._notify_by_name.call_count == 0


def test_stable_row_key_without_customer_code():
    proc = _processor()
    _first_failure(proc, code=None)
    state = dedup.get_alarm(MID, RK)
    assert state is not None and state["row_key"] == RK
    # 重判后客编有值：键不变，撤销仍然命中
    _write_forward()
    assert proc._cancel_alarm_on_forward(MID, RK, "forwarded", "") is True
    assert dedup.get_alarm(MID, RK)["status"] == "cancelled"


# ------------------------------------------------ 验收11：24h 折叠计数 / 重挂
def test_fold_count_accumulates_on_repeat_failure_without_notify():
    """T-FOLD-1：同 key 连续两次 no_route ⇒ 计数 2，宽限期内仍零通知。

    反证：删掉 serve.py 的 bump_alarm 调用（v1）⇒ alarm_state 无行，本例红。
    """
    # 预置窗口内首条已计数（基线同场景 count=1），两次失败之间不清空：
    # 第一次 inserted 不重复计数，第二次 repeat_same_cycle 必须 +1 ⇒ 2。
    conn = dedup.get_connection()
    try:
        conn.execute(
            "INSERT INTO alarm_state (key, window_start, count, last_sent_at) "
            "VALUES (?, datetime('now'), 1, datetime('now'))",
            (f"alarm|{MID}|{RK}",),
        )
        conn.commit()
    finally:
        conn.close()
    proc = _processor()
    proc.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    _first_failure(proc)
    _first_failure(proc)
    conn = dedup.get_connection()
    try:
        row = conn.execute(
            "SELECT count FROM alarm_state WHERE key=?", (f"alarm|{MID}|{RK}",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["count"] == 2
    assert proc.notifier._notify_by_name.call_count == 0
    assert dedup.get_alarm(MID, RK)["status"] == "pending"


def test_new_cycle_reattaches_pending_after_fold_window_expired():
    """T-FOLD-2：sent + 折叠窗口过期后再失败 ⇒ 重挂 pending 并重置起算时间。

    反证：不重挂（v1）⇒ 状态仍 sent、notifier 0 次，本例红。
    """
    key = f"alarm|{MID}|{RK}"
    dedup.upsert_pending_alarm(MID, RK, error_id="e1", detail="old",
                               recipients=[OPS_OWNER_EMAIL])
    conn = dedup.get_connection()
    try:
        conn.execute(
            "UPDATE alarm_pending SET status='sent', "
            "first_failed_at='2020-01-01 00:00:00' WHERE msg_id=? AND row_key=?",
            (MID, RK),
        )
        conn.execute(
            "INSERT INTO alarm_state (key, window_start, count, last_sent_at) "
            "VALUES (?, '2020-01-01 00:00:00', 1, '2020-01-01 00:00:00')",
            (key,),
        )
        conn.commit()
    finally:
        conn.close()
    proc = _processor()
    proc.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    _first_failure(proc)
    state = dedup.get_alarm(MID, RK)
    assert state["status"] == "pending"
    assert state["first_failed_at"] != "2020-01-01 00:00:00"
    assert proc.notifier._notify_by_name.call_count == 0
    _age_pending(seconds=200)
    stats = serve.flush_due_alarms(proc)
    assert stats["sent"] == 1
    assert proc.notifier._notify_by_name.call_count == 1
    conn = dedup.get_connection()
    try:
        count = conn.execute(
            "SELECT count FROM alarm_state WHERE key=?", (key,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1                            # 新窗口，不接旧计数


def test_fold_count_accumulates_after_cancelled_reattach():
    """可选：cancelled 重挂属于 repeat_same_cycle，计数继续 +1。"""
    proc = _processor()
    proc.notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    _first_failure(proc)
    _write_forward()
    assert proc._cancel_alarm_on_forward(MID, RK, "forwarded", "") is True
    dedup.bump_alarm(f"alarm|{MID}|{RK}", ALARM_FOLD_SEC)   # 模拟此前已发过 1 次
    _first_failure(proc)
    conn = dedup.get_connection()
    try:
        count = conn.execute(
            "SELECT count FROM alarm_state WHERE key=?", (f"alarm|{MID}|{RK}",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2
    assert proc.notifier._notify_by_name.call_count == 0
    assert dedup.get_alarm(MID, RK)["status"] == "pending"


# ---------------------------------------------------------------- 需求2
def test_routing_failed_diagnostic_line_is_single_and_complete(caplog):
    proc = _processor()
    with caplog.at_level(logging.WARNING, logger="mailbots_next.serve"):
        proc._log_routing_failed_diag(MID, _row(code=None, box=None, train=None),
                                      _route(), "acct@example.com")
    lines = [r.message for r in caplog.records if "routing_failed msg_id=" in r.message]
    assert len(lines) == 1
    line = lines[0]
    for field in ("msg_id", "customer_code", "container_no", "train_no",
                  "records_n", "processor_id", "snapshot_version", "thread",
                  "account", "route_source"):
        assert f"{field}=" in line
    assert "customer_code=None" in line
    assert "container_no=None" in line
    assert "train_no=None" in line
    assert "route_source=no_route" in line


def test_diagnostic_uses_snapshot_version_not_only_processor_id(caplog):
    calls = []

    def loader():
        calls.append(1)
        return [{"客户编码": "C1"}]

    provider = RecordsSnapshotProvider(60, loader=loader)
    proc = _processor(provider)
    with caplog.at_level(logging.WARNING, logger="mailbots_next.serve"):
        proc._begin_snapshot()
        proc._log_routing_failed_diag("v1", _row(), _route(), "acct")
        provider.force_refresh()
        proc._begin_snapshot()
        proc._log_routing_failed_diag("v2", _row(), _route(), "acct")
    versions = [line.split("snapshot_version=")[1].split()[0]
                for line in (r.message for r in caplog.records)
                if "routing_failed msg_id=" in line]
    assert versions == ["1", "2"]
    assert len(calls) == 2  # 构造一次 + force_refresh 一次


# ---------------------------------------------------------------- 需求3
def test_provider_current_never_reloads_and_clamps_to_300():
    calls = []

    def loader():
        calls.append(1)
        return [{"客户编码": "C1"}]

    provider = RecordsSnapshotProvider(1000, loader=loader, clock=lambda: 0.0)
    assert provider.refresh_sec == 300          # 硬上限 = SWEEP_INTERVAL_SEC
    before = len(calls)
    for _ in range(12):
        for _ in range(100):
            provider.current()
    assert len(calls) == before                  # 2000+ 行 × 12 线程零 reload
    assert provider.refresh_if_due(now=299) is False
    assert len(calls) == before
    assert provider.refresh_if_due(now=300) is True
    assert len(calls) == before + 1
    assert provider.version == 2


def test_all_decision_entries_share_provider_version():
    calls = []

    def loader():
        calls.append(1)
        return [{"客户编码": "C1", "箱号": "CICU1000001"}]

    provider = RecordsSnapshotProvider(60, loader=loader)
    proc = _processor(provider)
    assert proc._provider is provider
    v1, recs1 = proc._begin_snapshot()
    assert v1 == 1 and recs1 == [{"客户编码": "C1", "箱号": "CICU1000001"}]
    # 12 个线程各取一次，版本不变、loader 不再调用
    versions = []
    lock = threading.Lock()

    def grab():
        v, _ = proc._begin_snapshot()
        with lock:
            versions.append(v)

    threads = [threading.Thread(target=grab) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert versions == [1] * 12
    assert len(calls) == 1
    provider.force_refresh()
    assert proc._begin_snapshot()[0] == 2
