# -*- coding: utf-8 -*-
"""企微通知最小通道（spec §3 notify.py 的 Plan-B 子集；
digest/聚合在刀7 于本模块内扩展）。"""
_channel = None
try:
    from core import paths
    _root = paths.detect_root()
    import sys
    _cs = _root + "\\WeComBot\\cs_bot"
    if _cs not in sys.path:
        sys.path.insert(0, _cs)
    from cs_bot.wecom_api import notify_by_name as _real_channel  # noqa
    _channel = _real_channel
except Exception:
    _channel = None


def set_channel(fn):
    global _channel
    _channel = fn


def notify_realtime(real_name, text):
    if not real_name or _channel is None:
        return False
    try:
        ok, _ch = _channel(real_name, text)
        return bool(ok)
    except Exception:
        return False


def notify_alarm(names, reason, text):
    if not reason:
        raise ValueError("alarm 必须携带 reason（spec §8 杜绝不知为何而报警）")
    for n in names or []:
        notify_realtime(n, "[alarm:{}]\n{}".format(reason, text))
