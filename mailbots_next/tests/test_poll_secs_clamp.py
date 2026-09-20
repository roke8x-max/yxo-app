# -*- coding: utf-8 -*-
"""INGEST_POLL_SEC / --poll-secs 上限夹取（D 组补丁）：两条入口合流之后夹到 1500。"""
import sys
from unittest.mock import Mock, patch

import pytest

import mailbots_next.serve as serve_mod
import mailbots_next.config as config_mod


def test_clamp_over_max_warns_once():
    """用例 1：3600 ⇒ 1500 + WARN 一次（消息含 3600 与 1500）。"""
    with patch.object(serve_mod, "_log") as mock_log:
        assert serve_mod._clamp_poll_secs(3600, "t") == 1500
    assert mock_log.warning.call_count == 1
    msg = mock_log.warning.call_args.args[0]
    assert "3600" in msg and "1500" in msg


def test_clamp_boundary():
    """用例 2：1500 原样（不触发）；1501 ⇒ 1500 + WARN。"""
    with patch.object(serve_mod, "_log") as mock_log:
        assert serve_mod._clamp_poll_secs(1500, "t") == 1500
        assert serve_mod._clamp_poll_secs(1501, "t") == 1500
    assert mock_log.warning.call_count == 1


def test_clamp_normal_no_warn():
    """用例 3：30 / 60 原样返回、不产生 WARN（防误报）。"""
    with patch.object(serve_mod, "_log") as mock_log:
        assert serve_mod._clamp_poll_secs(30, "t") == 30
        assert serve_mod._clamp_poll_secs(60, "t") == 60
    mock_log.warning.assert_not_called()


def _run_main(argv, default_poll_secs):
    """调 serve.main() 走到 start_pollers/poll_once，捕获真实传入的 poll_secs。"""
    got = {}
    import pytest as _pt

    with _pt.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", argv)
        mp.setattr(config_mod, "INGEST_POLL_SEC", default_poll_secs)
        mp.setattr(serve_mod, "snapshot", Mock(return_value=Mock()))
        mp.setattr(serve_mod, "MailProcessor", Mock(return_value=Mock()))
        # main() 内是局部导入，必须按定义位置 patch，别让它真读库
        mp.setattr("mailbots_next.core.store.check_company_recipients_configured",
                   Mock(return_value=True))
        mp.setattr(serve_mod, "start_pollers",
                   Mock(side_effect=lambda proc, poll_secs=0: got.update(poll_secs=poll_secs)))
        mp.setattr(serve_mod, "poll_once",
                   Mock(side_effect=lambda proc, poll_secs=0: got.update(poll_secs=poll_secs)))
        mp.setattr(serve_mod, "start_inbound_server", Mock())
        mp.setattr(serve_mod, "stop_pollers", Mock())
        mp.setattr(serve_mod, "stop_inbound_server", Mock())
        serve_mod._shutdown_event.set()
        try:
            serve_mod.main()
        finally:
            serve_mod._shutdown_event.clear()
    return got


def test_env_entry_clamped():
    """用例 4：INGEST_POLL_SEC=3600 经 main() ⇒ start_pollers 收到 1500。"""
    got = _run_main(["serve"], 3600)
    assert got["poll_secs"] == 1500


def test_cli_entry_clamped():
    """用例 5：--poll-secs 3600 ⇒ start_pollers 收到 1500（本任务核心目的）。"""
    got = _run_main(["serve", "--poll-secs", "3600"], 30)
    assert got["poll_secs"] == 1500


def test_default_and_once_obey_clamp():
    """用例 6：未指定用默认值 30；--once 分支同样受夹取。"""
    got = _run_main(["serve"], 30)
    assert got["poll_secs"] == 30
    got_once = _run_main(["serve", "--once", "--poll-secs", "3600"], 30)
    assert got_once["poll_secs"] == 1500
