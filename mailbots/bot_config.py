#!/usr/bin/env python
"""
机器人开关配置共享模块（2026-08-03 芙蕾雅）
供各 MailBots/*.py 读取「启用/停用」开关；与 yxo_app/admin_api.py 的 BOT_CONFIG_MAP
保持路径一致。schema: {"live": bool, "forward_since": str|null, "accounts": []}
向后兼容：文件缺失或 live 缺省 → 默认 True（不误停现有运行）。
"""
import os
import json
from common_io import atomic_write_json

# 与 yxo_app/admin_api.py 的 BOT_CONFIG_MAP 完全一致
BOT_CONFIG_PATHS = {
    "draft_forward": "draft_robot_config.json",
    "waybill": "waybill_robot_config.json",
    "atb": "atb_robot_config.json",
    "dsk": "dsk_robot_config.json",
    "tracing": "tracing_robot_config.json",
    "syncer": "syncer_config.json",
}

_DIR = os.path.dirname(os.path.abspath(__file__))


def config_path(name):
    fname = BOT_CONFIG_PATHS.get(name, f"{name}_robot_config.json")
    return os.path.join(_DIR, fname)


def load_bot_config(name, default_live=True):
    """读取机器人开关配置。文件缺失或 live 缺省 → 返回 default_live（向后兼容）。"""
    cfg = {"live": default_live, "forward_since": None, "accounts": []}
    try:
        with open(config_path(name), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if "live" in data:
                cfg["live"] = bool(data["live"])
            if data.get("forward_since") not in (None, ""):
                cfg["forward_since"] = data["forward_since"]
            if "accounts" in data:
                cfg["accounts"] = data.get("accounts") or []
    except Exception:
        pass
    return cfg


def save_bot_config(name, live, forward_since=None, accounts=None):
    path = config_path(name)
    cfg = {"live": bool(live), "forward_since": forward_since, "accounts": accounts or []}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 原子写：临时文件 + fsync + os.replace，目标被运行中机器人占用时自动降级原地写
    # （内置 Windows [WinError 5] 回退，等价于原 open(path,'w') 方案，小叽 2026-08-03 同款修复）
    atomic_write_json(path, cfg)
    return cfg
