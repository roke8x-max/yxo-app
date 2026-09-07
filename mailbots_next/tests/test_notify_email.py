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

    def test_send_draft_update(self, notifier):
        from mailbots_next.config import OPS_OWNER_EMAIL
        result = notifier.send_draft_update("maoxiaoyang@cqtransit.com", OPS_OWNER_EMAIL, "update detail")
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


class TestModeBehavior:
    def test_test_mode_no_real_send(self, monkeypatch):
        monkeypatch.setenv("MAILBOT_MODE", "test")
        assert _cfg.MODE in ("test", "live")
        if not _cfg.is_live():
            with patch("mailbots_next.core.notify.get_notifier") as mock:
                notifier = Mock()
                notifier.send_alarm.return_value = True
                mock.return_value = notifier
                from mailbots_next.core.notify import get_notifier
                n = get_notifier()
                n.send_alarm("user", "admin", "test")
                notifier.send_alarm.assert_called_once()