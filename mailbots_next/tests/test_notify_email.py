import json
import os
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest

from mailbots_next.core.notify import (
    increment_counter,
    flush_counters,
    get_counters,
    WeComNotifier,
    DAILY_COUNTERS_PATH,
)
import mailbots_next.config as _cfg


@pytest.fixture(autouse=True)
def setup_counters():
    if DAILY_COUNTERS_PATH.exists():
        DAILY_COUNTERS_PATH.unlink()
    yield
    if DAILY_COUNTERS_PATH.exists():
        DAILY_COUNTERS_PATH.unlink()


class TestDailyCounters:
    def test_increment_and_get(self):
        increment_counter("test_key", 5)
        increment_counter("test_key", 3)
        counters = get_counters()
        assert counters["test_key"] == 8

    def test_flush_resets_counters(self):
        increment_counter("flush_test", 10)
        counts = flush_counters()
        assert counts["flush_test"] == 10
        counters = get_counters()
        assert "flush_test" not in counters

    def test_multiple_keys(self):
        increment_counter("key1", 1)
        increment_counter("key2", 2)
        increment_counter("key3", 3)
        counters = get_counters()
        assert counters["key1"] == 1
        assert counters["key2"] == 2
        assert counters["key3"] == 3

    def test_persistence_across_calls(self):
        increment_counter("persist", 1)
        counters1 = get_counters()
        assert counters1["persist"] == 1

        import mailbots_next.core.notify as notify_module
        notify_module._load_counters.cache_clear() if hasattr(notify_module._load_counters, 'cache_clear') else None
        counters2 = get_counters()
        assert counters2["persist"] == 1


class TestWeComNotifier:
    @pytest.fixture
    def notifier(self):
        n = WeComNotifier()
        n._notify_by_name = Mock(return_value=(True, "wxwork"))
        return n

    def test_notify_with_mock(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.notify(
            "test", ["maoxiaoyang@cqtransit.com", "yangyawen@cqtransit.com"], "test content"
        )
        assert result is True
        assert notifier._notify_by_name.call_count == 2
        # Emails are translated to WeCom display names before sending.
        from mailbots_next.core.notify import WeComNotifier
        _M = WeComNotifier._WECOM_NAME_BY_EMAIL
        sent_names = [c.args[0] for c in notifier._notify_by_name.call_args_list]
        assert sent_names == [_M["maoxiaoyang@cqtransit.com"], _M["yangyawen@cqtransit.com"]]

    def test_notify_unknown_email_skipped(self, notifier):
        result = notifier.notify("test", ["ghost@example.com"], "test content")
        assert result is False
        notifier._notify_by_name.assert_not_called()

    def test_notify_mixed_known_unknown(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.notify(
            "test", ["maoxiaoyang@cqtransit.com", "ghost@example.com"], "test content"
        )
        assert result is True
        notifier._notify_by_name.assert_called_once()
        from mailbots_next.core.notify import WeComNotifier
        assert notifier._notify_by_name.call_args.args[0] == \
            WeComNotifier._WECOM_NAME_BY_EMAIL["maoxiaoyang@cqtransit.com"]

    def test_send_alarm(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.send_alarm("maoxiaoyang@cqtransit.com", OPS_OWNER_EMAIL, "alarm detail")
        assert result is True

    def test_send_pending(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.send_pending("maoxiaoyang@cqtransit.com", OPS_OWNER_EMAIL, "pending detail")
        assert result is True

    def test_send_forwarded(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.send_forwarded("maoxiaoyang@cqtransit.com", OPS_OWNER_EMAIL, "forwarded detail")
        assert result is True

    def test_send_program_error(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.send_program_error(OPS_OWNER_EMAIL, "err_123", "program error detail")
        assert result is True

    def test_send_config_missing(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.send_config_missing(OPS_OWNER_EMAIL, "err_123", "config missing detail")
        assert result is True

    def test_send_digest(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.send_digest(OPS_OWNER_EMAIL, {"forwarded": 5, "alarms": 2})
        assert result is True

    def test_digest_empty_no_notify(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.send_digest(OPS_OWNER_EMAIL, {})
        assert result is True
        notifier._notify_by_name.assert_not_called()


class TestWeComLoudFail:
    """B 组（T3a 绝不静默失败）：真行为断言，非调用断言。"""

    def _force_client_failure(self, monkeypatch):
        """New mechanism: no URL/token anywhere => from_secrets raises
        config-missing => notifier loud-fails (no legacy package import)."""
        import mailbots_next.config.secrets as secrets_mod
        import mailbots_next.core.notify as notify_mod
        monkeypatch.setattr(notify_mod, "_client_unavailable_logged", False)
        monkeypatch.delenv("WECOM_NOTIFY_URL", raising=False)
        monkeypatch.delenv("WECOM_NOTIFY_TOKEN", raising=False)
        monkeypatch.delenv("WECOM_KF_EMAILS", raising=False)
        monkeypatch.setattr(secrets_mod, "_SECRETS_CACHE", {})

    def test_client_failure_errors_counts_and_returns_false(self, monkeypatch):
        import mailbots_next.core.notify as notify_mod
        from mailbots_next.core.notify import WeComNotifier
        self._force_client_failure(monkeypatch)
        before = get_counters().get("notify_failed", 0)
        with patch.object(notify_mod._log, "error") as mock_err:
            n = WeComNotifier()
            assert n._notify_by_name is None
            ok = n.notify("alarm", ["maoxiaoyang@cqtransit.com"], "content-text")
        assert ok is False
        err_texts = [str(c.args[0]) for c in mock_err.call_args_list]
        assert any("alarm" in t for t in err_texts), err_texts
        assert any("ma***g@cqtransit.com" in t for t in err_texts), err_texts
        assert "content-text" not in "".join(err_texts)  # 不记正文全文
        assert get_counters().get("notify_failed", 0) == before + 1

    def test_mask_addrs_survives_logger_private_api_rename(self, monkeypatch):
        """脱敏已提升为 log 模块级公共函数：即使 EmailLogger 的私有方法被改名/删除，
        loud fail 路径的脱敏也**不能崩** —— 否则就是从"静默失败"变成"崩在告警自身"。
        真锁：改动前 notify 走 `_log._mask_recipients`，本用例必挂。"""
        from mailbots_next.core.log import EmailLogger
        from mailbots_next.core.notify import _mask_addrs
        monkeypatch.delattr(EmailLogger, "_mask_recipients", raising=False)
        monkeypatch.delattr(EmailLogger, "_mask_email", raising=False)
        assert _mask_addrs(["maoxiaoyang@cqtransit.com"]) == ["ma***g@cqtransit.com"]

    def test_client_failure_logs_startup_error_only_once(self, monkeypatch):
        import mailbots_next.core.notify as notify_mod
        from mailbots_next.core.notify import WeComNotifier
        self._force_client_failure(monkeypatch)
        with patch.object(notify_mod._log, "error") as mock_err:
            WeComNotifier()
            WeComNotifier()
        startup = [c for c in mock_err.call_args_list
                   if "WeCom client not available" in str(c.args[0])]
        assert len(startup) == 1

    def test_send_program_error_also_loud_fails(self, monkeypatch):
        import mailbots_next.core.notify as notify_mod
        from mailbots_next.core.notify import WeComNotifier
        from mailbots_next.config import OPS_OWNER_EMAIL
        self._force_client_failure(monkeypatch)
        before = get_counters().get("notify_failed", 0)
        with patch.object(notify_mod._log, "error"):
            n = WeComNotifier()
            assert n.send_program_error(OPS_OWNER_EMAIL, "e1", "detail") is False
        assert get_counters().get("notify_failed", 0) == before + 1

    def test_normal_path_does_not_increment_counter(self):
        from mailbots_next.core.notify import WeComNotifier
        before = get_counters().get("notify_failed", 0)
        n = WeComNotifier()
        n._notify_by_name = Mock(return_value=(True, "wxwork"))
        assert n.notify("alarm", ["maoxiaoyang@cqtransit.com"], "ok") is True
        assert get_counters().get("notify_failed", 0) == before


class TestModeBehavior:
    def test_test_mode_no_real_send(self, monkeypatch):
        monkeypatch.setenv("MAILBOT_MODE", "test")
        assert _cfg.MODE in ("test", "live")
        if not _cfg.is_live():
            from mailbots_next.core.notify import get_notifier, reset_notifier
            reset_notifier()
            with patch("mailbots_next.core.notify.get_notifier") as mock:
                notifier = Mock()
                notifier.send_alarm.return_value = True
                mock.return_value = notifier
                from mailbots_next.core.notify import get_notifier
                n = get_notifier()
                n.send_alarm("user", "admin", "test")
                notifier.send_alarm.assert_called_once()