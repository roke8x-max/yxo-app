# -*- coding: utf-8 -*-
"""notify：realtime/alarm 最小通道；set_channel 注入假通道，绝不触达真实企微。"""
import pytest
from core import notify


@pytest.fixture(autouse=True)
def _restore_channel():
    """set_channel/_get_channel 改写模块级 _channel/_resolved，
    逐用例保存/还原防跨用例污染。"""
    old_ch, old_resolved = notify._channel, notify._resolved
    yield
    notify._channel, notify._resolved = old_ch, old_resolved


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


def test_lazy_resolution_swallows_startup_error(monkeypatch):
    # 懒初始化契约：首次发送时才探测通道；detect_root 抛 StartupError
    # 被吞掉、notify_realtime 返 False 不外抛；失败结果同样缓存（只探测一次）。
    # 注：先 _reset_channel_cache() 清内部缓存，暴露懒解析路径。
    from core import paths
    probes = []

    def boom():
        probes.append(1)
        raise paths.StartupError("x")

    monkeypatch.setattr(paths, "detect_root", boom)
    notify._reset_channel_cache()
    assert notify.notify_realtime("某人", "文本") is False
    assert notify.notify_realtime("某人", "文本") is False
    assert len(probes) == 1
