# -*- coding: utf-8 -*-
"""企微通知最小通道（spec §3 notify.py 的 Plan-B 子集；
digest/聚合在刀7 于本模块内扩展）。

通道懒初始化：模块导入零副作用（不做 detect_root/UNC 探测）；
首次真正发通知时才尝试导入 cs_bot.wecom_api.notify_by_name，
结果缓存——失败（含 StartupError）同样缓存为 None，绝不重复探测。"""
import threading
import time
from datetime import datetime

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


# ============================================================
# 刀 7: 通知日报 - 告警聚合 + C1 通知 + Digest 日报
# ============================================================

# 告警聚合：5分钟窗口内同 reason 仅发一次
_alarm_aggregation = {}  # {reason: (first_time, count, names, text)}
_alarm_aggregation_lock = threading.Lock()


def _flush_alarm_aggregation():
    """刷新告警聚合缓存"""
    global _alarm_aggregation
    with _alarm_aggregation_lock:
        for reason, (first_time, count, names, text) in list(_alarm_aggregation.items()):
            if time.time() - first_time >= 300:  # 5分钟窗口
                # 发送聚合告警
                if names:
                    text_with_count = "[alarm:{}] (已聚合 {} 次)\n{}".format(
                        reason, count, text)
                    for name in names:
                        notify_realtime(name, text_with_count)
                del _alarm_aggregation[reason]


def notify_alarm(names, reason, text):
    """
    发送告警（支持聚合）。
    同一 reason 在 5 分钟内只发一次，聚合计数。
    """
    if not reason:
        raise ValueError("alarm 必须携带 reason（spec §8 杜绝不知为何而报警）")

    now = time.time()
    with _alarm_aggregation_lock:
        if reason in _alarm_aggregation:
            first_time, count, agg_names, _ = _alarm_aggregation[reason]
            # 合并 names
            for n in names:
                if n not in agg_names:
                    agg_names.append(n)
            _alarm_aggregation[reason] = (first_time, count + 1, agg_names, text)
        else:
            _alarm_aggregation[reason] = (now, 1, list(names), text)
            # 启动定时器刷新
            threading.Timer(300, _flush_alarm_aggregation).start()

        # 立即发送第一次（count == 1 表示首次）
        if _alarm_aggregation[reason][1] == 1:
            for n in names:
                notify_realtime(n, "[alarm:{}]\n{}".format(reason, text))


def notify_c1(real_name, text):
    """
    C1 类内部反馈通知（仅实时，不聚合）。
    """
    notify_realtime(real_name, "[C1反馈]\n{}".format(text))


# ============================================================
# Digest 日报
# ============================================================

_digest_buffer = []  # [(timestamp, level, text)]
_digest_lock = threading.Lock()
_digest_timer = None


def _flush_digest():
    """发送每日汇总"""
    global _digest_buffer, _digest_timer
    with _digest_lock:
        if not _digest_buffer:
            return
        lines = []
        for ts, level, text in _digest_buffer:
            lines.append("[{}][{}] {}".format(
                datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
                level, text))
        digest_text = "📋 每日汇总\n" + "\n".join(lines)

        # 发送给管理员
        notify_realtime("毛骁洋", digest_text)
        notify_realtime("杨雅雯", digest_text)

        _digest_buffer.clear()

    # 重置定时器
    global _digest_timer
    _digest_timer = threading.Timer(86400, _flush_digest)  # 24小时
    _digest_timer.daemon = True
    _digest_timer.start()


def digest_add(level, text):
    """
    添加一条记录到每日汇总。
    level: INFO/WARNING/ERROR 等级
    """
    with _digest_lock:
        _digest_buffer.append((time.time(), level, text))
        if _digest_timer is None:
            _digest_timer = threading.Timer(86400, _flush_digest)  # 24小时
            _digest_timer.daemon = True
            _digest_timer.start()