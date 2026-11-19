# -*- coding: utf-8 -*-
"""任务 22-E：NONE 自动补发一次（T23–T34，判据 25–32）。

三道闸：等满 1800s / 每封最多一次（claim_resend 占位）/ 每轮最多 5 封。
硬边界：不写新 forward_log 行、不改 check_result、用原 forward_id、排除运踪。
用临时库 + patch send_smtp，不连真邮箱。
"""
import json
import sqlite3
from unittest.mock import patch

import pytest

from mailbots_next import config
from mailbots_next.core import send_check as sc
from mailbots_next.core.notify import get_counters
from mailbots_next.core.store import (
    get_bot_config_connection,
    get_forward_log,
    init_bot_config_db,
    init_forward_log,
    list_resend_candidates,
    claim_resend,
)

ACCT = "maoxiaoyang@cqtransit.com"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    import mailbots_next.config as cfg_mod
    import mailbots_next.core.store as store_mod
    import mailbots_next.core.notify as notify_mod

    target = tmp_path / "bot_config.db"
    for mod in (cfg_mod, store_mod):
        if hasattr(mod, "BOT_CONFIG_DB_PATH"):
            monkeypatch.setattr(mod, "BOT_CONFIG_DB_PATH", target)
    monkeypatch.setattr(notify_mod, "DAILY_COUNTERS_PATH", tmp_path / "counters.json")
    init_bot_config_db()
    init_forward_log()
    return tmp_path


def _add(fid, *, robot="ATB", result="NONE", resent="", bounced=0,
         age="-3600 seconds", to=("c@x.com",), cc=(), raw=b"Subject: s\r\n\r\nbody"):
    conn = get_bot_config_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO forward_log (forward_id, message_id, row_key, "
            " account, folder, uid, subject, sender, date_hdr, raw_hex, to_list, cc_list, "
            " sent_by, company, checked_at, check_result, sent_at, bounced, robot, resent_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now', ?),?,?,?)",
            (fid, "msg-" + fid, "0", "a@x.com", "INBOX", 1, "s", "f@x.com", "",
             (raw.hex() if raw else ""), json.dumps(list(to)),
             json.dumps(list(cc)), ACCT, "", "2026-11-18 00:00:00", result,
             age, bounced, robot, resent),
        )
        conn.commit()
    finally:
        conn.close()


def _resent(fid):
    return get_forward_log(fid)["resent_at"]


# ---------------------------------------------------------------- 纯 DB（T23–T27, T32–T34）


def test_T23_no_arg_candidates_uses_config():
    """T23：无参调用 list_resend_candidates() 含合格行（验证默认值 None → 读 config）。"""
    _add("f23")
    assert "f23" in {r["forward_id"] for r in list_resend_candidates()}


def test_T24_wait_window():
    """T24：sent_at 只早 5 分钟不进候选。"""
    _add("f24", age="-300 seconds")
    assert "f24" not in {r["forward_id"] for r in list_resend_candidates()}


def test_T25_already_resent_excluded():
    _add("f25", resent="2026-11-18 01:00:00")
    assert "f25" not in {r["forward_id"] for r in list_resend_candidates()}


def test_T26_bounced_excluded():
    _add("f26", bounced=1)
    assert "f26" not in {r["forward_id"] for r in list_resend_candidates()}


def test_T27_claim_once():
    """T27：claim_resend 连调两次只有第一次 True；resent_at 只写一次。"""
    _add("f27")
    assert claim_resend("f27") is True
    first = _resent("f27")
    assert claim_resend("f27") is False
    assert _resent("f27") == first


def test_T32_tracing_none_excluded():
    """T32：运踪 NONE 不进候选（历史 14 条保护）。"""
    _add("f32", robot=config.ROBOT_LABELS[config.EmailType.TRACING])
    assert "f32" not in {r["forward_id"] for r in list_resend_candidates()}


def test_T33_empty_robot_legacy_included():
    """T33：robot='' 老行照常进。"""
    _add("f33", robot="")
    assert "f33" in {r["forward_id"] for r in list_resend_candidates()}


def test_T34_limit_zero_sends_nothing(db, monkeypatch):
    """T34：resend_once(limit=0) 一封都不补。"""
    _add("f34a"); _add("f34b"); _add("f34c")
    with patch("mailbots_next.core.act.send_smtp") as mock_send:
        stats = sc.resend_once(limit=0)
    assert stats["resent"] == 0
    assert mock_send.call_count == 0


# ---------------------------------------------------------------- 端到端（T28–T31）


def test_T28_end_to_end_success_keeps_invariants(db):
    """T28：补发用原 fid / check_result 仍 NONE / forward_log 行数不变 / 原 to,cc。"""
    _add("f28", to=("c@x.com",), cc=("cc@x.com",))
    conn = get_bot_config_connection()
    before = conn.execute("SELECT COUNT(*) FROM forward_log").fetchone()[0]
    conn.close()
    with patch("mailbots_next.core.act.send_smtp",
               return_value={"success": True, "refused": {}}) as mock_send:
        stats = sc.resend_once()
    assert stats["resent"] == 1
    assert mock_send.call_count == 1
    msg = mock_send.call_args[0][0]
    assert msg["X-YXO-Forward-Id"] == "f28"
    assert mock_send.call_args[0][3] == ["c@x.com"]
    assert mock_send.call_args[0][4] == ["cc@x.com"]
    assert _resent("f28") != ""
    assert get_forward_log("f28")["check_result"] == "NONE"
    conn = get_bot_config_connection()
    after = conn.execute("SELECT COUNT(*) FROM forward_log").fetchone()[0]
    conn.close()
    assert after == before
    assert get_counters().get("send_check_resent") == 1


def test_T29_failure_no_retry():
    """T29：发送失败 ⇒ failed+1 / resent_at 仍非空 / 不重试 / 计数+record_issue。"""
    _add("f29")
    with patch("mailbots_next.core.act.send_smtp",
               return_value={"success": False, "error": "550 fail"}) as mock_send:
        stats = sc.resend_once()
    assert stats["failed"] == 1
    assert _resent("f29") != ""
    assert get_counters().get("send_check_resend_failed") == 1
    # 再跑一轮不再重试
    mock_send.reset_mock()
    with patch("mailbots_next.core.act.send_smtp",
               return_value={"success": True, "refused": {}}) as mock_send2:
        stats2 = sc.resend_once()
    assert stats2["resent"] == 0
    assert mock_send2.call_count == 0


def test_T30_disabled_returns_enabled_false(monkeypatch):
    """T30：SEND_CHECK_RESEND_ENABLED=0 ⇒ 返回 {"enabled":False}，不查库不发信。"""
    monkeypatch.setattr(sc.settings, "SEND_CHECK_RESEND_ENABLED", False)
    _add("f30")
    with patch.object(sc, "list_resend_candidates") as mock_list, \
            patch("mailbots_next.core.act.send_smtp") as mock_send:
        res = sc.resend_once()
    assert res == {"enabled": False}
    mock_list.assert_not_called()
    assert mock_send.call_count == 0


def test_T31_round_cap():
    """T31：8 行符合 ⇒ 一轮只补 5 封。"""
    for i in range(8):
        _add("f31-%d" % i)
    with patch("mailbots_next.core.act.send_smtp",
               return_value={"success": True, "refused": {}}) as mock_send:
        stats = sc.resend_once()
    assert stats["resent"] == 5
    assert mock_send.call_count == 5
