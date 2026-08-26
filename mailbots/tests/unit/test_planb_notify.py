# -*- coding: utf-8 -*-
"""notify：realtime/alarm 最小通道；set_channel 注入假通道，绝不触达真实企微。"""
import pytest
from core import notify


@pytest.fixture(autouse=True)
def _restore_channel():
    """set_channel 改写模块级 _channel，逐用例保存/还原防跨用例污染。"""
    old = notify._channel
    yield
    notify._channel = old


def test_realtime_forwards_text():
    seen = {}

    def fake(name, text):
        seen["call"] = (name, text)
        return True, "cs_bot"

    notify.set_channel(fake)
    assert notify.notify_realtime("张三", "运单已启动") is True
    assert seen["call"] == ("张三", "运单已启动")


def test_realtime_none_name_never_calls_channel():
    calls = []

    def fake(name, text):
        calls.append((name, text))
        return True, "cs_bot"

    notify.set_channel(fake)
    assert notify.notify_realtime(None, "x") is False
    assert notify.notify_realtime("", "x") is False
    assert calls == []


def test_realtime_swallows_channel_exception():
    def broken(name, text):
        raise RuntimeError("wecom down")

    notify.set_channel(broken)
    assert notify.notify_realtime("李四", "hi") is False


def test_alarm_empty_reason_raises():
    notify.set_channel(lambda n, t: (True, "cs_bot"))
    with pytest.raises(ValueError):
        notify.notify_alarm(["张三"], "", "正文")
    with pytest.raises(ValueError):
        notify.notify_alarm(["张三"], None, "正文")


def test_alarm_multi_names_each_notified():
    got = []

    def fake(name, text):
        got.append((name, text))
        return True, "cs_bot"

    notify.set_channel(fake)
    notify.notify_alarm(["张三", "李四"], "smtp_failed", "部分拒收")
    assert got == [("张三", "[alarm:smtp_failed]\n部分拒收"),
                   ("李四", "[alarm:smtp_failed]\n部分拒收")]
