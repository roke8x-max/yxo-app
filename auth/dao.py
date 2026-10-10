#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auth.db 读写（spec §4 dao.py）。只读 config.AUTH_DB_PATH（动态读，测试可 monkeypatch）。"""
import json
import sqlite3
from datetime import datetime


def connect():
    import config
    import os
    os.makedirs(os.path.dirname(os.path.abspath(config.AUTH_DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(config.AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------- users ----------------

def get_user(username):
    conn = connect()
    try:
        return conn.execute("SELECT * FROM auth_users WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()


def set_password(username, pw_hash):
    conn = connect()
    try:
        conn.execute("UPDATE auth_users SET pw_hash=? WHERE username=?", (pw_hash, username))
        conn.commit()
    finally:
        conn.close()


def set_disabled(username, disabled):
    conn = connect()
    try:
        conn.execute("UPDATE auth_users SET disabled=? WHERE username=?",
                     (1 if disabled else 0, username))
        conn.commit()
    finally:
        conn.close()
    if disabled:
        # 双保险：清掉该用户所有 session（spec §5.2）
        from auth import service
        service.destroy_user_sessions(username)


# ---------------- sessions ----------------

def save_session(sid, user_id, username, expires_at, remember):
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO auth_sessions(session_id, user_id, username, created_at,"
            " expires_at, remember) VALUES(?,?,?,?,?,?)",
            (sid, user_id, username, datetime.now().isoformat(timespec="seconds"),
             expires_at, 1 if remember else 0))
        conn.commit()
    finally:
        conn.close()


def load_session(sid):
    conn = connect()
    try:
        return conn.execute("SELECT * FROM auth_sessions WHERE session_id=?", (sid,)).fetchone()
    finally:
        conn.close()


def touch_session(sid, expires_at):
    conn = connect()
    try:
        conn.execute("UPDATE auth_sessions SET expires_at=? WHERE session_id=?", (expires_at, sid))
        conn.commit()
    finally:
        conn.close()


def delete_session(sid):
    conn = connect()
    try:
        conn.execute("DELETE FROM auth_sessions WHERE session_id=?", (sid,))
        conn.commit()
    finally:
        conn.close()


def delete_user_sessions(username):
    conn = connect()
    try:
        conn.execute("DELETE FROM auth_sessions WHERE username=?", (username,))
        conn.commit()
    finally:
        conn.close()


# ---------------- login attempts ----------------

def get_attempt(username, ip):
    conn = connect()
    try:
        return conn.execute("SELECT * FROM auth_login_attempts WHERE username=? AND ip=?",
                            (username, ip or "")).fetchone()
    finally:
        conn.close()


def save_attempt(username, ip, fails, locked_until):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO auth_login_attempts(username, ip, fails, locked_until, last_fail)"
            " VALUES(?,?,?,?,?) ON CONFLICT(username, ip) DO UPDATE SET fails=excluded.fails,"
            " locked_until=excluded.locked_until, last_fail=excluded.last_fail",
            (username, ip or "", fails, locked_until,
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()


def clear_attempt(username, ip):
    conn = connect()
    try:
        conn.execute("DELETE FROM auth_login_attempts WHERE username=? AND ip=?",
                     (username, ip or ""))
        conn.commit()
    finally:
        conn.close()


# ---------------- audit ----------------

def write_audit(ts, username, ip, req_id, event, target, detail):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO auth_audit(ts, username, ip, req_id, event, target, detail)"
            " VALUES(?,?,?,?,?,?,?)",
            (ts, username or "", ip or "", req_id or "", event, target or "",
             (detail or "")[:2000]))
        conn.commit()
    finally:
        conn.close()


def list_shadow_users():
    """影子期「我是」下拉候选（G10-B）：未禁用 + scope 全量的内部账号名（按 id 排序）。
    数据驱动，不硬编码名单；游客（scope=companies）与禁用账号不在内。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT username FROM auth_users WHERE disabled=0 AND scope_type='all'"
            " ORDER BY id").fetchall()
        return [r["username"] for r in rows]
    finally:
        conn.close()


def user_companies_scope(username):
    """取某用户的 scope（供 records_dao 用）。返回 (scope_type, [companies])。"""
    row = get_user(username)
    if not row:
        return "none", []
    try:
        companies = json.loads(row["scope_companies"] or "[]")
    except Exception:
        companies = []
    return (row["scope_type"] or "none"), (companies if isinstance(companies, list) else [])
