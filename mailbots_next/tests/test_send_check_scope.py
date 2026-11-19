# -*- coding: utf-8 -*-
"""任务 22-B：自检把「运踪」排除出候选（T8–T13，判据 13–16）。

T8 必须先证伪：基线代码上 get_pending_checks 会把 robot='运踪' 的行选进候选。
"""
import sqlite3

import pytest

from mailbots_next import config
from mailbots_next.core.store import (
    get_bot_config_connection,
    get_pending_checks,
    init_bot_config_db,
    init_forward_log,
)


@pytest.fixture(autouse=True)
def clean_forward_log():
    init_bot_config_db()
    init_forward_log()
    conn = get_bot_config_connection()
    try:
        conn.execute("DELETE FROM forward_log")
        conn.commit()
    finally:
        conn.close()
    yield


def _add(fid, robot, sent_at="-600 seconds", checked="", bounced=0):
    conn = get_bot_config_connection()
    try:
        conn.execute(
            "INSERT INTO forward_log (forward_id, message_id, row_key, account, "
            " folder, uid, subject, sender, date_hdr, raw_hex, to_list, cc_list, "
            " sent_by, company, checked_at, check_result, sent_at, bounced, robot) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now', ?),?,?)",
            (fid, "msg-" + fid, "0", "a@x.com", "INBOX", 1, "s", "f@x.com", "",
             "", '["c@x.com"]', "[]", "a@x.com", "", checked, "", sent_at,
             bounced, robot),
        )
        conn.commit()
    finally:
        conn.close()


def _fids(rows):
    return {r["forward_id"] for r in rows}


def test_tracing_excluded_from_pending_checks():
    """T8：robot='运踪'、checked_at=''、bounced=0、早于 120 秒 ⇒ 不在候选。"""
    _add("trace-1", config.ROBOT_LABELS[config.EmailType.TRACING])
    assert "trace-1" not in _fids(get_pending_checks(120, 200))


def test_non_tracing_robots_included():
    """T9：草单/运单号/DSK/ATB 四行（同条件）都含。论证 T8 是 robot 挡的。"""
    for i, robot in enumerate(["草单", "运单号", "DSK", "ATB"]):
        _add("nontrace-%d" % i, robot)
    got = _fids(get_pending_checks(120, 200))
    assert {"nontrace-0", "nontrace-1", "nontrace-2", "nontrace-3"} <= got


def test_empty_robot_legacy_row_included():
    """T10：robot='' 的老行仍含（不能被误挡）。"""
    _add("legacy-empty", "")
    assert "legacy-empty" in _fids(get_pending_checks(120, 200))


def test_tracing_within_delay_window_excluded():
    """T11：运踪但 sent_at 在延迟窗口内 ⇒ 不含（时间条件对运踪同样生效）。"""
    _add("trace-fresh", config.ROBOT_LABELS[config.EmailType.TRACING],
         sent_at="-5 seconds")
    assert "trace-fresh" not in _fids(get_pending_checks(120, 200))


def test_tracing_bounced_excluded():
    """T12：运踪但 bounced=1 ⇒ 不含。"""
    _add("trace-bounced", config.ROBOT_LABELS[config.EmailType.TRACING],
         bounced=1)
    assert "trace-bounced" not in _fids(get_pending_checks(120, 200))


def test_tracing_label_constant():
    """T13：标签常量 == '运踪'（防将来改标签导致本任务静默失效）。"""
    assert config.ROBOT_LABELS[config.EmailType.TRACING] == "\u8fd0\u8e2a"
