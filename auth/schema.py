#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auth.db 建表 + 种子（spec §12）。

单一 auth.db，独立于 yxo.db。表：
  auth_users / auth_roles / auth_permissions / auth_role_permissions /
  auth_sessions / auth_audit / auth_login_attempts

初始口令来源（按优先级）：
  1. 环境变量 YXO_AUTH_PASSWORD_<KEY>（KEY 见 SEED_USERS）
  2. 缺失 → 开发占位口令 "yxo-dev-<key>" + WARNING（生产必须设 env）
绝不硬编码真实口令、不写 config 默认值。
"""
import json
import os
import sqlite3
from datetime import datetime

import config

# username → (role, scope_type, companies, env_key, disabled)
SEED_USERS = {
    "毛骁洋": ("admin", "all", [], "MAOXIAOYANG", False),
    "冯茜": ("fengqian", "all", [], "FENGQIAN", False),
    "杨雅雯": ("yangyawen", "all", [], "YANGYAWEN", False),
    "韩文豪": ("hanwenhao", "all", [], "HANWENHAO", False),
    # 真实外部只读方：集团下属数科公司同事。visitor 角色纯只读
    # (record:read + record:export)，无写无管理权限。口令经 YXO_AUTH_PASSWORD_YOUKE
    # 设置；不设则 init_db 用开发占位 + WARNING（生产必须设真实口令）。
    # companies 必须与 yxo.db「开票子公司名称」实际取值逐一精确对应（独立字符串，
    # 禁止顿号连写），2026-10-10 生产库查得 8 家全量（G10-C）。
    "游客": ("visitor", "companies",
             ["太平洋", "港九港铁", "保时达", "同程配", "东盟", "沙坪坝", "中欧木业", "联运"], "YOUKE", False),
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auth_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'none',
    scope_companies TEXT NOT NULL DEFAULT '[]',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_roles (
    role TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS auth_permissions (
    perm TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS auth_role_permissions (
    role TEXT NOT NULL, perm TEXT NOT NULL,
    PRIMARY KEY (role, perm)
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    remember INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    req_id TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS auth_login_attempts (
    username TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    fails INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT NOT NULL DEFAULT '',
    last_fail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (username, ip)
);
"""


def connect():
    os.makedirs(os.path.dirname(os.path.abspath(config.AUTH_DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(config.AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_password(env_key, dev_key):
    pw = os.environ.get("YXO_AUTH_PASSWORD_" + env_key)
    if pw:
        return pw, "env"
    print(f"[auth] WARNING: YXO_AUTH_PASSWORD_{env_key} 未设置，使用开发占位口令（生产必须设环境变量）")
    return "yxo-dev-" + dev_key, "dev-placeholder"


def _test_or_debug():
    try:
        from flask import current_app
        return bool(current_app.testing or current_app.debug)
    except Exception:
        return False


def init_db(strict: bool = False):
    """建表 + 角色/权限种子 + 用户种子（幂等：用户已存在则跳过）。

    strict=True 时遍历 SEED_USERS 中未标记 disabled 的账号，
    任一 YXO_AUTH_PASSWORD_<ENVKEY> 缺失即 raise RuntimeError。
    pytest 运行期（PYTEST_CURRENT_TEST）与 Flask testing/debug 自动降级为不严格。
    """
    from auth import rbac, service
    enforce = strict and not (os.environ.get("PYTEST_CURRENT_TEST") or _test_or_debug())
    if enforce:
        for _username, (_role, _scope, _companies, _env_key, *rest) in SEED_USERS.items():
            _disabled = rest[0] if rest else False
            if _disabled:
                continue
            if not os.environ.get("YXO_AUTH_PASSWORD_" + _env_key):
                raise RuntimeError(f"缺少必需口令环境变量 YXO_AUTH_PASSWORD_{_env_key}")
    conn = connect()
    conn.executescript(SCHEMA_SQL)
    for role, desc in rbac.ROLES.items():
        conn.execute("INSERT OR IGNORE INTO auth_roles(role, description) VALUES(?,?)",
                     (role, desc))
    for perm, desc in rbac.PERMISSIONS.items():
        conn.execute("INSERT OR IGNORE INTO auth_permissions(perm, description) VALUES(?,?)",
                     (perm, desc))
    for role, perms in rbac.ROLE_PERMS.items():
        for p in rbac.expand_perms(perms):
            conn.execute("INSERT OR IGNORE INTO auth_role_permissions(role, perm) VALUES(?,?)",
                         (role, p))
    for username, (role, scope, companies, env_key, *rest) in SEED_USERS.items():
        row = conn.execute("SELECT id FROM auth_users WHERE username=?", (username,)).fetchone()
        if row:
            continue
        pw, _src = _seed_password(env_key, env_key.lower())
        disabled = 1 if (rest[0] if rest else False) else 0
        conn.execute(
            "INSERT INTO auth_users(username, pw_hash, role, scope_type, scope_companies, disabled, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (username, service.hash_password(pw), role, scope,
             json.dumps(companies, ensure_ascii=False), disabled,
             datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
