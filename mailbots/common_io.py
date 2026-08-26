# -*- coding: utf-8 -*-
"""公共 IO / 规范化工具。所有 MailBots / yxo_app / WeComBot 共用，避免各进程各写一套。

来源：渝新欧系统体检报告_20260803
  - 板块 2.2：atomic_write_json / safe_read_json（原子写，消灭 open(path,'w') 截断竞态）
  - 板块 1.1：norm_train_no（班列号统一）
  - 板块 1.2：norm_date（日期统一）
  - 板块 3.3：to_local_naive（时间统一北京时间 naive）

关键：Windows 下运行中的机器人以读模式占用 config/cache 文件时，os.replace(MoveFileEx)
会因共享冲突抛 [WinError 5]/[WinError 32]，故 atomic_write_json 内置「降级原地写」回退
（小叽 2026-08-03 同款修复，bot_config.py / admin_api.py 已验证）。
"""
import json
import os
import re
import tempfile
import traceback as _traceback
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

CN = timezone(timedelta(hours=8))

# 常见日期格式 → 统一 YYYY-MM-DD
DATE_PATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%m/%d/%Y")


def atomic_write_json(path, data, indent=2):
    """原子写 JSON：先写同目录临时文件，fsync 后 os.replace 原子替换。
    读方永远看到完整的旧版或新版，不会读到半截 / 空文件。

    Windows 回退：若目标文件正被运行中进程以读模式占用，os.replace 会抛
    PermissionError / OSError（WinError 5/32），此时降级为直接 open(path,'w')
    原地写（CPython 以读模式打开的文件允许写共享，原地写可成功）。
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, path)
        except (PermissionError, OSError):
            # WinError 5/32：目标被占用，降级原地写
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
            try:
                os.remove(tmp)
            except OSError:
                pass
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def safe_read_json(path, default=None):
    """读侧兜底：用 utf-8-sig 兼容 BOM；解析失败把坏文件改 .corrupt 留证并返回默认，
    缺失 / 权限错误直接返回默认。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return default if default is not None else {}
    except (json.JSONDecodeError, ValueError):
        try:
            if os.path.exists(path):
                os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return default if default is not None else {}
    except OSError:
        return default if default is not None else {}


def norm_train_no(raw):
    """班列号规范化：统一为 WB + 纯数字。'491'/'wb491'/'WB 491' -> 'WB491'"""
    if not raw:
        return ""
    s = re.sub(r"[^0-9A-Za-z]", "", str(raw)).upper()
    m = re.match(r"^(?:WB)?(\d+)$", s)
    return f"WB{m.group(1)}" if m else s


def norm_date(raw):
    """任何常见格式 -> 'YYYY-MM-DD'，解析不了原样返回（便于事后排查）"""
    if not raw:
        return ""
    s = str(raw).strip()
    for p in DATE_PATS:
        try:
            return datetime.strptime(s, p).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def to_local_naive(raw):
    """把任何来源的时间统一成'北京时间的 naive datetime'（入库用）"""
    if isinstance(raw, str):
        dt = parsedate_to_datetime(raw)      # 解析邮件 Date 头
    else:
        dt = raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN)           # 无时区信息 → 假定为北京时间
    return dt.astimezone(CN).replace(tzinfo=None)


# ==================== 退避轮询（体检报告 2.1）====================
# 机器人是「计划任务每 15 分钟拉起、单次执行后退出」的短进程，
# 所以退避状态必须跨进程持久化到 JSON，否则每次启动都从最短间隔开始，退化成固定轮询。
# 守护循环带 max_runtime 守卫：确保进程在下次计划任务触发前自行退出，避免重复进程重叠扫描。

import time as _time

_BACKOFF_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backoff_state.json")


def load_backoff_state():
    return safe_read_json(_BACKOFF_STATE_FILE, {})


def save_backoff_state(state):
    atomic_write_json(_BACKOFF_STATE_FILE, state)


class Backoff:
    """空转时逐步拉长间隔，有命中立刻恢复到最短间隔。"""

    def __init__(self, base=60, cap=900, max_n=8):
        self.base, self.cap, self.max_n = base, cap, max_n
        self.n = 0

    def hit(self):
        """本轮有命中（处理到邮件）→ 重置到最短间隔。"""
        self.n = 0
        return self.base

    def miss(self):
        """本轮空转 → 指数退避。"""
        self.n = min(self.n + 1, self.max_n)
        return min(self.base * (2 ** self.n), self.cap)


# ==================== 错误日志（spec 刀1/D4：根治隐身崩溃）====================
ERROR_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error_logs")


def log_error(bot_name, text):
    """把异常/告警文本追加写入 error_logs/{bot}_error_YYYYMMDD.log（UTF-8）。
    目录首次自动创建；写日志自身失败只打控制台，绝不向上抛。"""
    try:
        os.makedirs(ERROR_LOG_DIR, exist_ok=True)
        fname = os.path.join(
            ERROR_LOG_DIR,
            "{}_error_{}.log".format(bot_name, datetime.now().strftime("%Y%m%d")),
        )
        with open(fname, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception as e:
        print("[{}] 写错误日志失败: {}".format(bot_name, e))


def run_once(bot_name, run_fn):
    """守护循环的一轮：网络类异常续跑；未知异常把完整 traceback 写入错误日志。
    返回本轮处理邮件数（run_fn 返回 None 视为 0）。"""
    try:
        return run_fn() or 0
    except (TimeoutError, OSError, ConnectionError) as e:
        log_error(bot_name, "网络类异常，续跑: {!r}".format(e))
        return 0
    except Exception:
        log_error(bot_name, "未知异常，本轮跳过:\n" + _traceback.format_exc())
        return 0


def daemon_loop(bot_name, run_fn, max_runtime=12 * 60, base=60, cap=900):
    """退避轮询守护循环。

    - run_fn() 返回本次处理的邮件数（>0 视为命中，重置间隔；0/None 视为空转，退避）
    - 跨进程持久化 backoff 指数 n（key=bot_name），下次启动延续
    - max_runtime 守卫：进程在下次计划任务(15min)触发前自行退出，避免重复进程重叠
    - run_fn 抛网络类异常时捕获续跑，不退出（替代原 __main__ 的 2 次重试）
    """
    state = load_backoff_state()
    bo = Backoff(base, cap)
    bo.n = min(int(state.get(bot_name, {}).get("n", 0)), bo.max_n)
    start = _time.time()
    while _time.time() - start < max_runtime:
        got = run_once(bot_name, run_fn)
        # 命中重置/空转退避全权交给 Backoff.hit()/miss()（含 n 自增），勿手动改 n
        sleep_t = bo.hit() if got else bo.miss()
        state[bot_name] = {"n": bo.n}
        save_backoff_state(state)
        if _time.time() - start + sleep_t > max_runtime:
            break
        _time.sleep(sleep_t)
    print(f"[{bot_name}] 守护循环到时退出（max_runtime={max_runtime}s，当前退避指数 n={bo.n}）")
