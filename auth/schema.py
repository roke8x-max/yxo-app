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

# username → (role, scope_type, companies, env_key)
SEED_USERS = {
    "毛骁洋": ("admin", "all", [], "MAOXIAOYANG"),
    "冯茜": ("manager", "all", [], "FENGQIAN"),
    "杨雅雯": ("manager", "all", [], "YANGYAWEN"),
    "韩文豪": ("manager", "all", [], "HANWENHAO"),
    # 验收账号：scope 绑定 yxo.db 开票子公司名称实际取值（完整字符串，禁用简写）
    "visitor_demo": ("visitor", "companies", ["太平洋、港九港铁", "保时达"], "VISITOR_DEMO"),
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


def init_db():
    """建表 + 角色/权限种子 + 用户种子（幂等：用户已存在则跳过）。"""
    from auth import rbac, service
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
    for username, (role, scope, companies, env_key) in SEED_USERS.items():
        row = conn.execute("SELECT id FROM auth_users WHERE username=?", (username,)).fetchone()
        if row:
            continue
        pw, _src = _seed_password(env_key, env_key.lower())
        conn.execute(
            "INSERT INTO auth_users(username, pw_hash, role, scope_type, scope_companies, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (username, service.hash_password(pw), role, scope,
             json.dumps(companies, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
