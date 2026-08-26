# -*- coding: utf-8 -*-
"""企微通知最小通道（spec §3 notify.py 的 Plan-B 子集；
digest/聚合在刀7 于本模块内扩展）。

通道懒初始化：模块导入零副作用（不做 detect_root/UNC 探测）；
首次真正发通知时才尝试导入 cs_bot.wecom_api.notify_by_name，
结果缓存——失败（含 StartupError）同样缓存为 None，绝不重复探测。"""
_channel = None
_resolved = False  # 是否已完成一次通道解析（成功或失败均算）


def _get_channel():
    """首次调用探测并缓存通道；后续直接返回缓存值。"""
    global _channel, _resolved
    if _resolved:
        return _channel
    _resolved = True
    try:
        from core import paths
        root = paths.detect_root()
        import sys
        _cs = root + "\\WeComBot\\cs_bot"
        if _cs not in sys.path:
            sys.path.insert(0, _cs)
        from cs_bot.wecom_api import notify_by_name as real_channel  # type: ignore
        _channel = real_channel
    except Exception:
        _channel = None
    return _channel


def set_channel(fn):
    """测试注入强制覆盖：注入即视为已解析，懒加载不再改写注入值。"""
    global _channel, _resolved
    _channel = fn
    _resolved = True


def _reset_channel_cache():
    """测试用：清空通道缓存，恢复未解析态。"""
    global _channel, _resolved
    _channel = None
    _resolved = False


def notify_realtime(real_name, text):
    ch = _get_channel()
    if not real_name or ch is None:
        return False
    try:
        ok, _ch_tag = ch(real_name, text)
        return bool(ok)
    except Exception:
        return False


def notify_alarm(names, reason, text):
    if not reason:
        raise ValueError("alarm 必须携带 reason（spec §8 杜绝不知为何而报警）")
    for n in names or []:
        notify_realtime(n, "[alarm:{}]\n{}".format(reason, text))
