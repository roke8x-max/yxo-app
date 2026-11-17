# -*- coding: utf-8 -*-
"""任务 21 · 负责人映射单一真相源（`store.seed_owner_mapping`）。

覆盖判据：
  1  正常读取 —— 从 WeComBot `config.py` 的 USER_COMPANIES / USER_EMAILS 反向生成
     `{公司关键词: 邮箱}` 并写入 `bot_config`（bot='all', scope='owner'）
  2  单一真相源 —— 改 config 里的邮箱，表跟着变（不是只读内置 fallback 字典）
  3  路径不存在 → 回退 fallback，不抛异常，日志有 WARNING，且为 2026-11-17 口径
  4  `config.py` import 期抛 RuntimeError（同目录缺 secrets.json）→ 同样回退、不炸

隔离：库路径全部指临时目录（conftest 已 `setdefault`），**绝不碰生产 bot_config.db**。
时序：`store.py` 模块级会无条件 seed 一次；本文件因此都是「改常量 → 显式重调
`seed_owner_mapping()` → 再查表」，不复用 import 期那次的结果。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import mailbots_next.core.store as store_mod


# 2026-11-17 业务口径（毛骁洋指令）：中欧木业 → 杨雅雯、沙坪坝 → 冯茜、韩文豪名下清零。
OWNERS_1117 = {
    "太平洋": "maoxiaoyang@cqtransit.com",
    "港九港铁": "maoxiaoyang@cqtransit.com",
    "东盟": "yangyawen@cqtransit.com",
    "同程配": "yangyawen@cqtransit.com",
    "中欧木业": "yangyawen@cqtransit.com",
    "沙坪坝": "fengqian@cqtransit.com",
    "保时达": "fengqian@cqtransit.com",
    "联运": "fengqian@cqtransit.com",
}

# 仿生产 WeComBot/config.py 的开头：import 期必读同目录 secrets.json，缺失即 RuntimeError。
_RAISING_CONFIG = '''\
import json as _json
import os as _os

_SECRETS_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "secrets.json")
try:
    with open(_SECRETS_PATH, "r", encoding="utf-8") as _f:
        _SECRETS = _json.load(_f)
except FileNotFoundError:
    raise RuntimeError(
        "缺少凭证文件 %s。请参照 secrets.example.json 创建并填入真实密钥。" % _SECRETS_PATH)

USER_COMPANIES = {}
USER_EMAILS = {}
'''


class _LogRecorder:
    """替身 logger：EmailLogger 的 `propagate=False`，caplog 收不到，只能换掉 `store._log`。"""

    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, msg, **kwargs):
        self.warnings.append(str(msg))

    def error(self, msg, **kwargs):        # 回退绝不是 error：服务照常起
        self.errors.append(str(msg))

    def info(self, msg, **kwargs):
        pass

    def debug(self, msg, **kwargs):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    """临时 bot_config 库 + 捕获 WARNING 的 logger 替身。

    注意：`seed_owner_mapping()` 只写不建表（生产靠 `store.py` 模块级
    `init_bot_config_db()` 先行），所以换了库路径后要自己建一次表。
    """
    db = tmp_path / "bot_config.db"
    monkeypatch.setattr(store_mod, "BOT_CONFIG_DB_PATH", db)
    rec = _LogRecorder()
    monkeypatch.setattr(store_mod, "_log", rec)
    store_mod.init_bot_config_db()
    return {"db": db, "log": rec, "tmp": tmp_path}


def _write_config(directory: Path, user_companies, user_emails, source=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.py"
    if source is not None:
        path.write_text(source, encoding="utf-8")
    else:
        path.write_text(
            "USER_COMPANIES = %r\nUSER_EMAILS = %r\n" % (user_companies, user_emails),
            encoding="utf-8",
        )
    return path


def _owner_rows(db: Path):
    """读 bot_config 里 scope='owner' 的行 → {公司: 邮箱}。"""
    conn = sqlite3.connect(db)
    try:
        return {
            key: json.loads(extra or "{}").get("email")
            for key, extra in conn.execute(
                "SELECT key, extra FROM bot_config WHERE bot='all' AND scope='owner'"
            )
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- 判据 1 / 2：正常读取

def test_seed_reads_wecombot_config(env, monkeypatch):
    """判据 1：8 行 = WeComBot config.py 的口径（不是内置字典）。"""
    path = _write_config(env["tmp"] / "wecombot", {
        "毛骁洋": ["太平洋", "港九港铁"],
        "杨雅雯": ["同程配", "东盟", "中欧木业"],
        "冯茜": ["保时达", "联运", "沙坪坝"],
        "韩文豪": [],
    }, {
        "毛骁洋": "maoxiaoyang@cqtransit.com",
        "杨雅雯": "yangyawen@cqtransit.com",
        "冯茜": "fengqian@cqtransit.com",
        "韩文豪": "hanwenhao@cqtransit.com",
    })
    monkeypatch.setattr(store_mod, "WECOMBOT_CONFIG_PATH", path)

    store_mod.seed_owner_mapping()

    rows = _owner_rows(env["db"])
    assert len(rows) == 8
    assert rows == OWNERS_1117
    assert "hanwenhao@cqtransit.com" not in rows.values()   # 韩文豪名下无 owner 行
    assert env["log"].warnings == []                        # 走的是真相源，不该有回退告警


def test_seed_follows_config_not_fallback(env, monkeypatch):
    """判据 2：只改 config 的邮箱 → 表跟着变（证明运行时构建，不只读 fallback）。"""
    path = _write_config(env["tmp"] / "wecombot", {
        "毛骁洋": ["太平洋", "港九港铁"],
        "杨雅雯": ["同程配", "东盟", "中欧木业"],
        "冯茜": ["保时达", "联运", "沙坪坝"],
        "韩文豪": [],
    }, {
        "毛骁洋": "maoxiaoyang@cqtransit.com",
        "杨雅雯": "yangyawen@cqtransit.com",
        "冯茜": "SENTINEL-lianyun@example.test",    # ← 哨兵值
        "韩文豪": "hanwenhao@cqtransit.com",
    })
    monkeypatch.setattr(store_mod, "WECOMBOT_CONFIG_PATH", path)

    store_mod.seed_owner_mapping()

    rows = _owner_rows(env["db"])
    assert rows["联运"] == "SENTINEL-lianyun@example.test"
    assert rows["沙坪坝"] == "SENTINEL-lianyun@example.test"   # 同一人被改，同行一起变
    assert rows["中欧木业"] == "yangyawen@cqtransit.com"       # 未改的保持


def test_seed_uses_module_constant_not_reprobe(env, monkeypatch):
    """硬契约：seed 只读模块常量 `WECOMBOT_CONFIG_PATH`，改常量即生效。"""
    assert isinstance(store_mod.WECOMBOT_CONFIG_PATH, Path)
    path = _write_config(env["tmp"] / "wecombot", {"测试员": ["测试公司"]},
                         {"测试员": "tester@example.test"})
    monkeypatch.setattr(store_mod, "WECOMBOT_CONFIG_PATH", path)

    store_mod.seed_owner_mapping()

    assert _owner_rows(env["db"]) == {"测试公司": "tester@example.test"}


# ---------------------------------------------------------------- 判据 3 / 4：回退

def test_seed_falls_back_when_config_missing(env, monkeypatch):
    """判据 3：路径不存在 → 不抛异常、按 11-17 口径 fallback 写 8 行、日志 WARNING。"""
    monkeypatch.setattr(store_mod, "WECOMBOT_CONFIG_PATH",
                        env["tmp"] / "no_such_dir" / "config.py")

    store_mod.seed_owner_mapping()          # ← 绝不抛

    rows = _owner_rows(env["db"])
    assert len(rows) == 8
    assert rows == OWNERS_1117              # fallback 已是 2026-11-17 口径，不是旧人
    assert rows["中欧木业"] == "yangyawen@cqtransit.com"
    assert rows["沙坪坝"] == "fengqian@cqtransit.com"
    assert env["log"].errors == []
    assert len(env["log"].warnings) == 1
    msg = env["log"].warnings[0]
    assert "config" in msg and "回退" in msg, msg


def test_seed_falls_back_on_runtime_error(env, monkeypatch):
    """判据 4：config.py 存在但同目录缺 secrets.json ⇒ import 期 RuntimeError 必须被吞。

    这是任务书点名的高危场景：不加 try/except，服务直接 import 不起来。
    """
    path = _write_config(env["tmp"] / "wecombot_broken", None, None,
                         source=_RAISING_CONFIG)

    # 先证明这份桩确实会抛（不是测了个假场景）
    import importlib.util
    spec = importlib.util.spec_from_file_location("_probe_raising_config", str(path))
    probe = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError):
        spec.loader.exec_module(probe)

    monkeypatch.setattr(store_mod, "WECOMBOT_CONFIG_PATH", path)
    store_mod.seed_owner_mapping()          # ← 绝不抛

    assert _owner_rows(env["db"]) == OWNERS_1117
    assert env["log"].errors == []
    assert len(env["log"].warnings) == 1
    assert "RuntimeError" in env["log"].warnings[0]


def test_seed_falls_back_on_missing_email_key(env, monkeypatch):
    """键缺失（有人公司但 USER_EMAILS 没他）→ 同样回退，不是写半张表。"""
    path = _write_config(env["tmp"] / "wecombot_noemail",
                         {"冯茜": ["联运"]}, {"毛骁洋": "maoxiaoyang@cqtransit.com"})
    monkeypatch.setattr(store_mod, "WECOMBOT_CONFIG_PATH", path)

    store_mod.seed_owner_mapping()

    assert _owner_rows(env["db"]) == OWNERS_1117
    assert len(env["log"].warnings) == 1
