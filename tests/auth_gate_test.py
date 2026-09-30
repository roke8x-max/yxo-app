#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""权限 RBAC 门禁 IDOR 测试矩阵（spec §14）。

落码：OpenCode｜验收：芙蕾雅。对应 spec：
docs/superpowers/specs/2026-09-21-permission-rbac-module-design.md

命名刻意用 `*_test.py`：仓库 .gitignore 屏蔽了 `test_*.py`，
`*_test.py` 仍被 pytest 默认发现且不会被忽略。
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

ADMIN = "t_admin"
MGR = "t_mgr"
VIS = "t_visitor"
NONEU = "t_none"
PW = {
    ADMIN: "TestAa123456",
    MGR: "TestBb123456",
    VIS: "TestCc123456",
    NONEU: "TestDd123456",
}


def _seed_records(db_path):
    conn = sqlite3.connect(db_path)
    cols = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        f'seq INTEGER, {cols}, updated_at TEXT, updated_by TEXT, order_idx REAL, '
        f'is_deleted INTEGER DEFAULT 0, deleted_at TEXT, deleted_by TEXT)')
    conn.execute("DELETE FROM records")
    for comp in ("太平洋、港九港铁", "保时达", "沙坪坝、中欧木业"):
        vals = ["" for _ in config.ALL_FIELDS]
        vals[config.ALL_FIELDS.index("客户编码")] = "T" + comp[:2]
        vals[config.ALL_FIELDS.index("开票子公司名称")] = comp
        ph = ", ".join(["?"] * len(vals))
        conn.execute(f"INSERT INTO records (seq, order_idx, {cols}) VALUES (?, ?, {ph})",
                     [1, 1.0] + vals)
    conn.execute("CREATE TABLE IF NOT EXISTS user_state (user TEXT, key TEXT, value TEXT, "
                 "PRIMARY KEY (user, key))")
    conn.execute("CREATE TABLE IF NOT EXISTS meta_kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # A 工单：既有用例假设门禁生效，fixture 显式开 AUTH_ENABLED=1（不断言不动）。
    return _make_client(tmp_path, monkeypatch, auth_enabled=True)


@pytest.fixture()
def dclient(tmp_path, monkeypatch):
    # A 工单：disabled 模式（AUTH_ENABLED=0）客户端，门禁只观察不拒绝。
    return _make_client(tmp_path, monkeypatch, auth_enabled=False)


def _make_client(tmp_path, monkeypatch, auth_enabled):
    auth_db = str(tmp_path / "auth.db")
    yxo_db = str(tmp_path / "yxo.db")
    monkeypatch.setattr(config, "AUTH_DB_PATH", auth_db)
    # raising=False：A 落码前 config 尚无该属性（RED 期），落码后按值覆盖
    monkeypatch.setattr(config, "AUTH_ENABLED", auth_enabled, raising=False)
    import app as appmod  # 延迟导入：gate 每次请求都动态读 config.AUTH_DB_PATH
    monkeypatch.setattr(appmod, "DB", yxo_db)
    _seed_records(yxo_db)
    from auth import schema, service
    schema.init_db()
    service.create_user(ADMIN, PW[ADMIN], role="admin", scope_type="all")
    service.create_user(MGR, PW[MGR], role="manager", scope_type="all")
    service.create_user(VIS, PW[VIS], role="visitor",
                        scope_type="companies", companies=["太平洋、港九港铁", "保时达"])
    service.create_user(NONEU, PW[NONEU], role="visitor", scope_type="none")
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    return c, appmod


def _csrf(c):
    r = c.get("/api/csrf")
    assert r.status_code == 200
    return r.get_json()["csrf_token"]


def _login(c, username, password):
    tok = _csrf(c)
    r = c.post("/api/login",
               data=json.dumps({"username": username, "password": password}),
               content_type="application/json",
               headers={"X-CSRF-Token": tok})
    return r


def _authed(c, username):
    r = _login(c, username, PW[username])
    assert r.status_code == 200, r.get_data(as_text=True)
    return _csrf(c)


# ---------------- §14 矩阵 ----------------

def test_unauth_rows_401(client):
    c, _ = client
    r = c.get("/api/rows")
    assert r.status_code == 401


def test_spoofed_user_query_param_ignored(client):
    c, _ = client
    tok = _authed(c, MGR)
    # 旧式 ?user=毛骁洋 直调：必须被忽略，以 session 为准
    r = c.get("/api/rows?user=毛骁洋")
    assert r.status_code == 200
    r2 = c.post("/api/row", json={},
                headers={"X-CSRF-Token": tok})
    assert r2.status_code in (200, 403)  # manager 有写权限应 200，且绝不看 user 参数


def test_no_session_delete_401(client):
    c, _ = client
    r = c.delete("/api/row/1")
    assert r.status_code in (401, 403)


def test_non_admin_admin_api_forbidden(client):
    c, _ = client
    _authed(c, VIS)
    r = c.get("/api/admin/status")
    assert r.status_code == 403
    r2 = c.get("/admin")
    assert r2.status_code == 200  # 页面 HTML 公开空壳


def test_manager_has_admin_view(client):
    # spec §3.1/§7.2：manager 持有 admin:view，可看管理页数据接口
    c, _ = client
    _authed(c, MGR)
    r = c.get("/api/admin/status")
    assert r.status_code == 200


def test_lockout_after_5_fails(client):
    c, _ = client
    for _ in range(5):
        r = _login(c, MGR, "wrong-password-1")
        assert r.status_code in (401, 429)
    r = _login(c, MGR, PW[MGR])
    assert r.status_code == 429  # 锁定 15 分钟，正确口令也拒绝


def test_logout_then_401(client):
    c, _ = client
    tok = _authed(c, MGR)
    r = c.post("/api/logout", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    r2 = c.get("/api/rows")
    assert r2.status_code == 401


def test_disabled_user_401(client):
    c, _ = client
    _authed(c, MGR)
    from auth import dao
    dao.set_disabled(MGR, True)
    r = c.get("/api/rows")
    assert r.status_code == 401


def test_scope_none_returns_empty(client):
    c, _ = client
    _authed(c, NONEU)
    r = c.get("/api/rows")
    assert r.status_code == 200
    assert r.get_json() == []  # 绝不全量


def test_visitor_companies_filtered(client):
    c, _ = client
    _authed(c, VIS)
    r = c.get("/api/rows")
    assert r.status_code == 200
    rows = r.get_json()
    got = {row["开票子公司名称"] for row in rows}
    assert got == {"太平洋、港九港铁", "保时达"}


def test_visitor_post_forbidden(client):
    c, _ = client
    tok = _authed(c, VIS)
    r = c.post("/api/row", json={}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 403


def test_unregistered_route_403(client, monkeypatch):
    # 默认拒绝安全网：登记表无 → 403。临时摘掉已注册的 public 路由，
    # 模拟「url_map 有、表无」的漏登，验证门禁端到端返回 403。
    from auth import rbac
    c, _ = client
    pruned = set(rbac.ROUTE_PERMISSIONS_SET) - {("GET", "/api/version")}
    monkeypatch.setattr(rbac, "ROUTE_PERMISSIONS_SET", pruned)
    import auth as auth_pkg
    monkeypatch.setattr(auth_pkg, "ROUTE_PERMISSIONS_SET", pruned)
    r = c.get("/api/version")
    assert r.status_code == 403


def test_updated_by_ignores_client_user(client):
    c, appmod = client
    tok = _authed(c, MGR)
    conn = sqlite3.connect(appmod.DB)
    rid = conn.execute("SELECT id FROM records LIMIT 1").fetchone()[0]
    conn.close()
    r = c.patch(f"/api/row/{rid}",
                json={"field": "备注", "value": "x", "user": "毛骁洋"},
                headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    conn = sqlite3.connect(appmod.DB)
    by = conn.execute("SELECT updated_by FROM records WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    assert by == MGR


def test_change_password_flow(client):
    c, _ = client
    tok = _authed(c, MGR)
    r = c.post("/api/change_password",
               json={"old_password": PW[MGR], "new_password": "short"},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 400  # 弱口令拒绝
    r = c.post("/api/change_password",
               json={"old_password": PW[MGR], "new_password": "NewStrongPass99"},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    c2_r = _login(c, MGR, "NewStrongPass99")
    assert c2_r.status_code == 200


def test_csrf_required_on_post(client):
    c, _ = client
    _authed(c, MGR)
    r = c.post("/api/row", json={})  # 无 CSRF 头
    assert r.status_code == 403


def test_route_table_covers_url_map():
    from auth import rbac
    import app as appmod
    missing = []
    for rule in appmod.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for m in (rule.methods or set()) - {"HEAD", "OPTIONS"}:
            if (m, rule.rule) not in rbac.ROUTE_PERMISSIONS_SET:
                missing.append(f"{m} {rule.rule}")
    assert missing == []


# ---------------- B 工单：/api/stamp 加固自测 ----------------

FAKE_STAMP = "test-stamp-token-123"


def _stamp_post(c, appmod, token, extra_headers=None, remote_addr=None):
    headers = {"X-Stamp-Token": token} if token is not None else {}
    if extra_headers:
        headers.update(extra_headers)
    kw = {"json": {"box_no": "NO_SUCH_BOX", "field": "dsk", "value": "v"}}
    if remote_addr is not None:
        kw["environ_overrides"] = {"REMOTE_ADDR": remote_addr}
    return c.post("/api/stamp", headers=headers, **kw)


def test_stamp_localhost_ok(client, monkeypatch):
    c, appmod = client
    monkeypatch.setattr(appmod, "STAMP_TOKEN", FAKE_STAMP)
    r = _stamp_post(c, appmod, FAKE_STAMP)
    assert r.status_code == 200  # 箱号不存在 → found=False 但 200，证明 token+白名单通过
    assert r.get_json()["found"] is False


def test_stamp_wrong_token_403(client, monkeypatch):
    c, appmod = client
    monkeypatch.setattr(appmod, "STAMP_TOKEN", FAKE_STAMP)
    r = _stamp_post(c, appmod, "wrong-token")
    assert r.status_code == 403


def test_stamp_non_localhost_403(client, monkeypatch):
    c, appmod = client
    monkeypatch.setattr(appmod, "STAMP_TOKEN", FAKE_STAMP)
    r = _stamp_post(c, appmod, FAKE_STAMP, remote_addr="8.8.8.8")
    assert r.status_code == 403  # 白名单生效


def test_stamp_proxy_header_403(client, monkeypatch):
    c, appmod = client
    monkeypatch.setattr(appmod, "STAMP_TOKEN", FAKE_STAMP)
    r = _stamp_post(c, appmod, FAKE_STAMP,
                    extra_headers={"X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 403  # 拒代理头生效
    r2 = _stamp_post(c, appmod, FAKE_STAMP,
                     extra_headers={"X-Real-IP": "1.2.3.4"})
    assert r2.status_code == 403


def test_stamp_none_token_no_crash(client, monkeypatch):
    # 测试环境缺 token：not STAMP_TOKEN 短路返 403，绝不抛 TypeError
    c, appmod = client
    monkeypatch.setattr(appmod, "STAMP_TOKEN", None)
    r = _stamp_post(c, appmod, FAKE_STAMP)
    assert r.status_code == 403


def test_is_production_false_under_pytest(client):
    from auth.common import is_production
    import app as appmod
    # fixture 已置 TESTING=True；且 PYTEST_CURRENT_TEST 环境变量存在 → 非生产
    assert is_production(appmod.app) is False


def test_init_auth_refuses_without_token(client, monkeypatch):
    # 生产（debug/testing 全 False 且无 PYTEST_CURRENT_TEST）缺 token → 启动即 raise
    import config
    from auth import init_auth
    from flask import Flask
    monkeypatch.setattr(config, "STAMP_TOKEN", None)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    prod = Flask(__name__)
    prod.debug = False
    prod.testing = False
    with pytest.raises(RuntimeError, match="YXSTAMP_TOKEN"):
        init_auth(prod)


# ---------------- A 工单：AUTH_ENABLED 灰度开关 + system 兜底 + dry-run ----------------

def test_system_identity_shape():
    # 与 _load_identity 同形状 5 字段；权限必须全量（§1.3 修正），scope 全量
    from auth import _system_identity
    from auth.rbac import PERMISSIONS
    from data.records_dao import resolve_scope
    ident = _system_identity()
    assert ident.username == "system"
    assert ident.role == "system"
    assert sorted(ident.permissions) == sorted(PERMISSIONS.keys())
    assert ident.scope_type == "all"
    assert ident.companies == []
    assert resolve_scope(ident) == ("all", [])  # companies=[] 不挡全量


def test_disabled_row_200(dclient):
    # POST /api/row（record:write）：disabled 直接 200，无需登录/CSRF
    c, _ = dclient
    r = c.post("/api/row", json={})
    assert r.status_code == 200


def test_disabled_admin_status_200(dclient):
    # GET /api/admin/status（admin:view，过 admin_api 内联 ident.permissions 判定）
    c, _ = dclient
    r = c.get("/api/admin/status")
    assert r.status_code == 200


def test_disabled_tuoshu_manifest_not_blocked(dclient):
    # 内联判定点：403 只能来自视图层拒绝以外的因由——此处断言门禁/内联不挡（401/403 绝不出现；
    # 400/500 来自视图参数校验或引擎，同样证明已穿过权限层）
    c, _ = dclient
    r = c.post("/api/tuoshu/generate", json={})
    assert r.status_code not in (401, 403)
    r2 = c.post("/api/manifest/apply", json={})
    assert r2.status_code not in (401, 403)


def test_disabled_delete_marks_system(dclient):
    # 13 处裸 g.identity.username 不崩，且 updated_by 署 system（灰度通知口径）
    import sqlite3
    c, appmod = dclient
    conn = sqlite3.connect(appmod.DB)
    rid = conn.execute("SELECT id FROM records LIMIT 1").fetchone()[0]
    conn.close()
    r = c.delete(f"/api/row/{rid}")
    assert r.status_code == 200
    conn = sqlite3.connect(appmod.DB)
    by = conn.execute("SELECT deleted_by FROM records WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    assert by == "system"


def test_dry_run_log_fields(dclient, caplog):
    # 每行 5 字段齐；matched 与 ROUTE_PERMISSIONS 一致；path 为实际路径（非 rule 原串）
    import logging
    c, _ = dclient
    caplog.set_level(logging.INFO, logger="auth")
    r = c.patch("/api/row/1", json={"field": "备注", "value": "x"})
    assert r.status_code == 200
    recs = [rec for rec in caplog.records
            if rec.name == "auth" and "AUTH_DRY_RUN" in rec.getMessage()]
    assert recs, "disabled 期非噪声请求必须记 AUTH_DRY_RUN"
    msg = recs[-1].getMessage()
    assert "path=/api/row/1" in msg      # 实际路径，不是 /api/row/<int:rid>
    assert "method=PATCH" in msg
    assert "ip=" in msg
    assert "matched=record:write" in msg  # 与 ROUTE_PERMISSIONS 登记一致
    assert "has_session=False" in msg


def test_dry_run_skips_noise(dclient, caplog):
    # 噪声（404 无 url_rule）不记 dry-run
    import logging
    c, _ = dclient
    caplog.set_level(logging.INFO, logger="auth")
    caplog.clear()
    c.get("/no/such/route/xyz")
    assert not [rec for rec in caplog.records
                if rec.name == "auth" and "AUTH_DRY_RUN" in rec.getMessage()]


def test_enabled_gate_still_enforced(client, monkeypatch):
    # AUTH_ENABLED=1：门禁全效；stamp 仍按 B 逻辑（与开关无关，拒体为 JSON）
    c, appmod = client
    r = c.get("/api/rows")
    assert r.status_code == 401
    monkeypatch.setattr(appmod, "STAMP_TOKEN", FAKE_STAMP)
    r2 = _stamp_post(c, appmod, "wrong-token")
    assert r2.status_code == 403
    body = r2.get_json()
    assert body["ok"] is False  # D3：拒体 JSON，不再是 HTML 错误页
