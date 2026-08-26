# -*- coding: utf-8 -*-
"""启动自检 + 凭据/端点网关（spec §3 paths.py）。
裁定：yxo.db 自检为『只读可查 records』而非写测——写测会在业务高峰制造锁竞争。"""
import os


class StartupError(RuntimeError):
    pass


_CANDIDATE_ROOTS = [r"D:\YXO_DATA", r"\\10.0.199.184\yxo_data"]
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # mailbots/


def detect_root(candidates=None):
    """返回首个含 WeComBot\\config.py 的根目录；显式传 candidates 供测试注入。"""
    for cand in (candidates or _CANDIDATE_ROOTS):
        if os.path.isfile(os.path.join(cand, "WeComBot", "config.py")):
            return cand
    raise StartupError(
        "未找到运行根目录（候选 {} 均无 WeComBot\\config.py）".format(_CANDIDATE_ROOTS))


def yxo_db_path():
    return os.path.join(detect_root(), "yxo_app", "data", "yxo.db")


def events_db_path():
    return os.path.join(_HERE, "data", "events.db")


def eml_repo_dir():
    return os.path.join(_HERE, "data", "eml_repo")


def load_accounts():
    """ACCOUNTS 网关：WeComBot config 优先（与现役机器人同源），回退 config_local。
    两者皆缺 → 空 dict（调用方据此跳过对应账号并告警，绝不抛出凭据内容）。"""
    root = None
    try:
        root = detect_root()
    except StartupError:
        root = None
    if root:
        try:
            import sys
            if root + "\\WeComBot" not in sys.path:
                sys.path.insert(0, root + "\\WeComBot")
            from config import ACCOUNTS  # type: ignore
            return dict(ACCOUNTS)
        except Exception:
            pass
    try:
        sys_path = os.path.dirname(os.path.abspath(__file__))
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from config_local import ACCOUNTS  # type: ignore
        return dict(ACCOUNTS)
    except Exception:
        return {}


def smtp_endpoint():
    root = detect_root()
    import sys
    if root + "\\WeComBot" not in sys.path:
        sys.path.insert(0, root + "\\WeComBot")
    from config import SMTP_SERVER, SMTP_PORT  # type: ignore
    return str(SMTP_SERVER), int(SMTP_PORT)


def ensure_startup_checks():
    db = yxo_db_path()
    if not os.path.isfile(db):
        raise StartupError("yxo.db 不存在: {}".format(db))
    import sqlite3
    conn = sqlite3.connect("file:{}/?mode=ro".format(db.replace("\\", "/")), uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        conn.close()
    if not n:
        raise StartupError("records 表为空，拒绝启动（疑似连错库）")
    os.makedirs(eml_repo_dir(), exist_ok=True)
