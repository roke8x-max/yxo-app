#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR-C：Excel 台账导入已停用 —— 入口确实没了 + "删干净"护栏。

任务书 PR-C 节验收 2-3。tests/ 下无 conftest.py，client fixture 照抄
tests/auth_gate_test.py（返回值是元组 (c, appmod)；写请求带 X-CSRF-Token）。
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

ADMIN = "t_admin"
PW = "TestAa123456"


def _seed_records(db_path):
    conn = sqlite3.connect(db_path)
    cols = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        f'seq INTEGER, {cols}, updated_at TEXT, updated_by TEXT, order_idx REAL, '
        f'is_deleted INTEGER DEFAULT 0, deleted_at TEXT, deleted_by TEXT)')
    conn.execute("DELETE FROM records")
    conn.execute("CREATE TABLE IF NOT EXISTS user_state (user TEXT, key TEXT, value TEXT, "
                 "PRIMARY KEY (user, key))")
    conn.execute("CREATE TABLE IF NOT EXISTS meta_kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    auth_db = str(tmp_path / "auth.db")
    yxo_db = str(tmp_path / "yxo.db")
    monkeypatch.setattr(config, "AUTH_DB_PATH", auth_db)
    monkeypatch.setattr(config, "AUTH_ENABLED", True, raising=False)
    import app as appmod  # 延迟导入：gate 每次请求都动态读 config.AUTH_DB_PATH
    monkeypatch.setattr(appmod, "DB", yxo_db)
    _seed_records(yxo_db)
    from auth import schema, service
    schema.init_db(strict=False)
    service.create_user(ADMIN, PW, role="admin", scope_type="all")
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    return c, appmod


def _csrf(c):
    r = c.get("/api/csrf")
    assert r.status_code == 200
    return r.get_json()["csrf_token"]


def _login(c):
    r = c.post("/api/login",
               data=json.dumps({"username": ADMIN, "password": PW}),
               content_type="application/json",
               headers={"X-CSRF-Token": _csrf(c)})
    assert r.status_code == 200, r.get_data(as_text=True)
    return _csrf(c)  # 登录后换新 token


def test_import_routes_are_gone(client):
    """两路由已停用：直接 POST 应 404（不是 403、不是 500）；且未在门禁登记."""
    c, appmod = client
    tok = _login(c)
    assert c.post("/api/import", headers={"X-CSRF-Token": tok}).status_code == 404
    assert c.post("/api/import_upload", headers={"X-CSRF-Token": tok}).status_code == 404
    from auth import rbac
    assert ("POST", "/api/import") not in rbac.ROUTE_PERMISSIONS
    assert ("POST", "/api/import_upload") not in rbac.ROUTE_PERMISSIONS


def test_data_import_permission_fully_removed():
    """'删干净'的守卫：权限与角色项都不该再存在（不靠 grep 兜）。"""
    from auth import rbac
    assert "data:import" not in rbac.PERMISSIONS
    assert "data:import" not in rbac.ROLE_PERMS["admin"]
