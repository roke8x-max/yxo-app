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
    schema.init_db(strict=False)
    service.create_user(ADMIN, PW[ADMIN], role="admin", scope_type="all")
    service.create_user(MGR, PW[MGR], role="hanwenhao", scope_type="all")
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
    # 确认版 2026-10-06 §2：admin:view 仅 admin 持有
    c, _ = client
    _authed(c, ADMIN)
    r = c.get("/api/admin/status")
    assert r.status_code == 200


def test_hanwenhao_no_admin_view(client):
    c, _ = client
    _authed(c, MGR)
    r = c.get("/api/admin/status")
    assert r.status_code == 403


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


# ---------------- A 工单/G10-A：AUTH_ENABLED 灰度开关 + 影子身份 ----------------

def test_shadow_identity_shape():
    # 与 _load_identity 同形状 5 字段；username 取 ?user= 自报（空即空，不兜底）；
    # 权限必须全量，scope 全量（影子期不拦截）。
    import app as appmod
    from auth import _shadow_identity
    from auth.rbac import PERMISSIONS
    from data.records_dao import resolve_scope
    with appmod.app.test_request_context("/?user=韩文豪"):
        ident = _shadow_identity()
        assert ident.username == "韩文豪"
        assert sorted(ident.permissions) == sorted(PERMISSIONS.keys())
        assert ident.scope_type == "all"
        assert ident.companies == []
        assert resolve_scope(ident) == ("all", [])
    with appmod.app.test_request_context("/"):
        assert _shadow_identity().username == ""  # 空 ?user= 不兜底，尤其禁止 or "system"


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


def test_disabled_delete_marks_shadow_user(dclient):
    # G10-A：影子期署名取 ?user= 自报（13 处裸 g.identity.username 不崩）；
    # 不带 ?user= 则署空字符串（不兜底 system）。
    import sqlite3
    c, appmod = dclient
    conn = sqlite3.connect(appmod.DB)
    ids = [r[0] for r in conn.execute("SELECT id FROM records LIMIT 2").fetchall()]
    conn.close()
    r = c.delete(f"/api/row/{ids[0]}?user=韩文豪")
    assert r.status_code == 200
    conn = sqlite3.connect(appmod.DB)
    by = conn.execute("SELECT deleted_by FROM records WHERE id=?", (ids[0],)).fetchone()[0]
    conn.close()
    assert by == "韩文豪"
    r2 = c.delete(f"/api/row/{ids[1]}")
    assert r2.status_code == 200
    conn = sqlite3.connect(appmod.DB)
    by2 = conn.execute("SELECT deleted_by FROM records WHERE id=?", (ids[1],)).fetchone()[0]
    conn.close()
    assert by2 == ""


def test_dry_run_log_fields(dclient, caplog):
    # 每行 5 字段齐；matched 与 ROUTE_PERMISSIONS 一致；path 为实际路径（非 rule 原串）
    import logging
    c, _ = dclient
    caplog.set_level(logging.INFO, logger="app")  # D4：dry-run 走 current_app.logger（名 "app"）
    r = c.patch("/api/row/1", json={"field": "备注", "value": "x"})
    assert r.status_code == 200
    recs = [rec for rec in caplog.records
            if rec.name == "app" and "AUTH_DRY_RUN" in rec.getMessage()]
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
    caplog.set_level(logging.INFO, logger="app")
    caplog.clear()
    c.get("/no/such/route/xyz")
    assert not [rec for rec in caplog.records
                if rec.name == "app" and "AUTH_DRY_RUN" in rec.getMessage()]


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


def test_dry_run_log_lands_in_app_log(dclient):
    from app import app as _app
    import os
    from logging.handlers import RotatingFileHandler
    # 从 handler 取真实日志路径（不要自己拼目录约定，避免与 _LOG_DIR 不一致而对不上）
    log_path = next(h.baseFilename for h in _app.logger.handlers
                    if isinstance(h, RotatingFileHandler))
    # —— 防「假通过」：日志是追加写入，若文件里已有旧的 AUTH_DRY_RUN（调试跑过 / A 工单测试写过），
    # 仅看 "AUTH_DRY_RUN" in content 会误判通过。先记 size_before，本次请求后必须增长。
    size_before = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    # 触发一次非噪声请求（disabled 态 _auth_gate 注入 system 并调 _dry_run_log）
    dclient[0].get("/api/rows")  # 任意已知端点均可；GET 也会触发 _dry_run_log
    # flush 必须针对 app.logger 上的 handler —— 不要用 root.handlers！
    # RotatingFileHandler 挂在 app.logger 上（app.py:67-84），root 没有这个 handler，
    # logging.getLogger().manager.root.handlers 是空列表，flush 它毫无作用。
    # （FileHandler 默认同步落盘，flush 是双保险；若不想 flush 也可直接删掉本段。）
    for h in _app.logger.handlers:
        h.flush()
    size_after = os.path.getsize(log_path)
    assert size_after > size_before, "日志文件未增长，AUTH_DRY_RUN 未真写入（D4 修复未生效？）"
    # 读文件断言内容（errors="ignore" 兜底：handler 已指定 encoding="utf-8"(app.py:73)，
    # 但加 ignore 防 Windows 编码读取异常导致解码崩溃）
    content = open(log_path, encoding="utf-8", errors="ignore").read()
    assert "AUTH_DRY_RUN" in content, "disabled 期观察日志未落到 logs/app.log"


# ---------------- G1：strict 缺口令拒绝启动 ----------------

def _clean_pw_env():
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("YXO_AUTH_PASSWORD_"):
            del env[k]
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def test_g1_strict_missing_pw_subprocess(tmp_path):
    # 子进程无 PYTEST_CURRENT_TEST、无 app 上下文 → enforce=True；
    # 清空全部口令 env 后 init_db(strict=True) 须非 0 退出且 stderr 含明确错误。
    import subprocess
    probe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_strict_probe.py")
    env = _clean_pw_env()
    env["YXO_AUTH_DB"] = str(tmp_path / "auth.db")
    r = subprocess.run([sys.executable, probe], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "缺少必需口令环境变量" in (r.stderr + r.stdout)


def test_g1_strict_all_pw_ok_same_process(tmp_path, monkeypatch):
    # 同进程设齐 5 env 后 init_db(strict=True) 成功（PYTEST_CURRENT_TEST 下降级亦成功）。
    monkeypatch.setattr(config, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    for key in ("MAOXIAOYANG", "FENGQIAN", "YANGYAWEN", "HANWENHAO", "YOUKE"):
        monkeypatch.setenv("YXO_AUTH_PASSWORD_" + key, "TestStrict123-" + key)
    from auth import schema
    schema.init_db(strict=True)


def test_g1_init_auth_no_crash_under_pytest(client):
    # pytest 运行期 init_auth(app) 不因口令 env 未设而崩（降级生效）。
    c, appmod = client
    r = c.get("/api/version")
    assert r.status_code == 200


# ---------------- G10-C：visitor_demo 已移除 ----------------

def test_g10_visitor_demo_removed(tmp_path, monkeypatch):
    # 洋拍板直接删：全新库 init 后 visitor_demo 行不存在；SEED_USERS 无此 key
    monkeypatch.setattr(config, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    from auth import schema
    assert "visitor_demo" not in schema.SEED_USERS
    schema.init_db(strict=False)
    conn = sqlite3.connect(str(tmp_path / "auth.db"))
    row = conn.execute("SELECT id FROM auth_users WHERE username='visitor_demo'").fetchone()
    conn.close()
    assert row is None


# ---------------- G7：角色重划 ----------------

def test_g7_manager_gone_and_new_perms():
    from auth import rbac
    assert "manager" not in rbac.ROLES
    assert "manager" not in rbac.ROLE_PERMS
    for p in ("data:import", "options:manage", "trash:purge", "train:manage"):
        assert p in rbac.PERMISSIONS
    assert set(rbac.ROLE_PERMS) == {"admin", "hanwenhao", "yangyawen", "fengqian", "visitor"}
    assert "user:manage" in rbac.ROLE_PERMS["admin"]
    for r in ("hanwenhao", "yangyawen", "fengqian", "visitor"):
        assert "user:manage" not in rbac.ROLE_PERMS[r]
    for p in ("manifest:apply", "price:manage", "config:manage", "data:import"):
        assert p in rbac.ROLE_PERMS["admin"]
        assert p not in rbac.ROLE_PERMS["fengqian"]
        assert p not in rbac.ROLE_PERMS["visitor"]
    assert "tuoshu:generate" not in rbac.ROLE_PERMS["fengqian"]
    assert "price:manage" not in rbac.ROLE_PERMS["fengqian"]


def test_g7_route_perm_split():
    from auth import rbac
    assert rbac.ROUTE_PERMISSIONS[("POST", "/api/import")] == "data:import"
    assert rbac.ROUTE_PERMISSIONS[("POST", "/api/import_upload")] == "data:import"
    assert rbac.ROUTE_PERMISSIONS[("POST", "/api/field_options")] == "options:manage"
    assert rbac.ROUTE_PERMISSIONS[("DELETE", "/api/trash/<int:rid>")] == "trash:purge"
    assert rbac.ROUTE_PERMISSIONS[("POST", "/api/trash/<int:rid>/restore")] == "record:write"
    assert rbac.ROUTE_PERMISSIONS[("POST", "/api/train_status")] == "train:manage"


def test_g7_seed_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    from auth import schema
    schema.init_db(strict=False)
    conn = sqlite3.connect(str(tmp_path / "auth.db"))
    got = {r[0]: r[1] for r in
           conn.execute("SELECT username, role FROM auth_users").fetchall()}
    conn.close()
    assert got["冯茜"] == "fengqian"
    assert got["杨雅雯"] == "yangyawen"
    assert got["韩文豪"] == "hanwenhao"
    assert got["毛骁洋"] == "admin"


def test_g7_trash_purge_admin_only(client):
    # hanwenhao 有 record:write（可恢复）但无 trash:purge（彻底删除 403）；admin 可删。
    c, appmod = client
    tok = _authed(c, MGR)
    conn = sqlite3.connect(appmod.DB)
    rid = conn.execute("SELECT id FROM records LIMIT 1").fetchone()[0]
    conn.close()
    r = c.post(f"/api/trash/{rid}/restore", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    r2 = c.delete(f"/api/trash/{rid}", headers={"X-CSRF-Token": tok})
    assert r2.status_code == 403
    _authed(c, ADMIN)
    tok2 = _csrf(c)
    r3 = c.delete(f"/api/trash/{rid}", headers={"X-CSRF-Token": tok2})
    assert r3.status_code == 200


# ---------------- G8：收敛散落名单 ----------------

def test_g8_no_scattered_lists():
    import config as cfg
    assert not hasattr(cfg, "USERS")
    assert not hasattr(cfg, "TUOSHU_ADMINS")
    import admin_api
    assert not hasattr(admin_api, "ADMIN_USER")
    assert not hasattr(admin_api, "LIMITED_ADMINS")
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "templates", "tuoshu.html"),
               encoding="utf-8").read()
    assert "TUOSHU_ADMINS" not in src


# ---------------- G10-A：影子期署名 = 自报身份 ----------------

def test_g10_shadow_patch_marks_real_name(dclient):
    # 影子期 PATCH 一行 → updated_by = ?user= 真实姓名（非 system）
    import sqlite3
    c, appmod = dclient
    conn = sqlite3.connect(appmod.DB)
    rid = conn.execute("SELECT id FROM records LIMIT 1").fetchone()[0]
    conn.close()
    r = c.patch(f"/api/row/{rid}?user=杨雅雯",
                json={"field": "备注", "value": "g10"})
    assert r.status_code == 200
    conn = sqlite3.connect(appmod.DB)
    by = conn.execute("SELECT updated_by FROM records WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    assert by == "杨雅雯"


def test_g10_shadow_state_isolated_per_user(dclient):
    # 四人各 GET /api/state 读到各自行，互不覆盖
    c, _ = dclient
    r = c.post("/api/state?user=毛骁洋", json={"state": {"month": "2026-01"}})
    assert r.status_code == 200
    r = c.post("/api/state?user=冯茜", json={"state": {"month": "2026-02"}})
    assert r.status_code == 200
    got_mao = c.get("/api/state?user=毛骁洋").get_json()["state"]
    got_feng = c.get("/api/state?user=冯茜").get_json()["state"]
    assert got_mao.get("month") == "2026-01"
    assert got_feng.get("month") == "2026-02"


# ---------------- G10-B：auth_meta 双模式 ----------------

def test_g10_auth_meta_shadow_mode(dclient):
    c, _ = dclient
    m = c.get("/api/auth_meta?user=韩文豪").get_json()
    assert m["mode"] == "shadow"
    assert m["username"] == "韩文豪"
    # fixture 另建 t_* 测试账号（同为全量 scope），此处断言 4 位同事在列即可
    assert {"毛骁洋", "冯茜", "杨雅雯", "韩文豪"} <= set(m["users"])
    assert "游客" not in m["users"]


def test_g10_auth_meta_auth_mode(client):
    c, _ = client
    _authed(c, ADMIN)
    m = c.get("/api/auth_meta").get_json()
    assert m["mode"] == "auth"
    assert m["username"] == ADMIN


def test_g10_frontend_dual_mode_static():
    # 四页引入公共判断 + 身份下拉；CSRF 与 401 弹窗只在 auth 模式生效
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shared = open(os.path.join(root, "static", "auth_mode.js"), encoding="utf-8").read()
    assert "function applyAuthMode" in shared
    assert "function withShadowUser" in shared
    assert "function fillWhoSel" in shared
    for page in ("templates/index.html", "templates/admin.html",
                 "templates/tuoshu.html", "templates/manifest.html"):
        src = open(os.path.join(root, page), encoding="utf-8").read()
        assert 'id="whoSel"' in src, page
    appjs = open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()
    assert "AUTH_MODE === \"auth\"" in appjs
    for page in ("templates/admin.html", "templates/tuoshu.html", "templates/manifest.html"):
        src = open(os.path.join(root, page), encoding="utf-8").read()
        assert "AUTH_MODE === \"auth\"" in src, page
        assert "auth_mode.js" in src, page


# ---------------- G10-C：游客 8 独立公司 ----------------

def test_g10_youke_companies(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    from auth import schema
    schema.init_db(strict=False)
    import json as _json
    conn = sqlite3.connect(str(tmp_path / "auth.db"))
    row = conn.execute("SELECT scope_companies FROM auth_users WHERE username='游客'").fetchone()
    conn.close()
    assert _json.loads(row[0]) == ["太平洋", "港九港铁", "保时达", "同程配",
                                   "东盟", "沙坪坝", "中欧木业", "联运"]


def test_g10_youke_rows_match_all_companies(tmp_path, monkeypatch):
    # 游客登录后 /api/rows 覆盖全部有公司归属的行（独立取值精确匹配）
    monkeypatch.setattr(config, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(config, "AUTH_ENABLED", True, raising=False)
    import app as appmod
    yxo_db = str(tmp_path / "yxo.db")
    monkeypatch.setattr(appmod, "DB", yxo_db)
    conn = sqlite3.connect(yxo_db)
    cols = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        f'seq INTEGER, {cols}, updated_at TEXT, updated_by TEXT, order_idx REAL, '
        f'is_deleted INTEGER DEFAULT 0, deleted_at TEXT, deleted_by TEXT)')
    for comp in ("同程配", "保时达", "港九港铁", "中欧木业", "沙坪坝", "太平洋", "东盟", "联运"):
        vals = ["" for _ in config.ALL_FIELDS]
        vals[config.ALL_FIELDS.index("客户编码")] = "T" + comp[:1]
        vals[config.ALL_FIELDS.index("开票子公司名称")] = comp
        ph = ", ".join(["?"] * len(vals))
        conn.execute(f"INSERT INTO records (seq, order_idx, {cols}) VALUES (?, ?, {ph})",
                     [1, 1.0] + vals)
    conn.commit()
    conn.close()
    from auth import schema, service
    schema.init_db(strict=False)
    service.create_user("t_youke", "TestEe123456", role="visitor",
                        scope_type="companies",
                        companies=["太平洋", "港九港铁", "保时达", "同程配",
                                   "东盟", "沙坪坝", "中欧木业", "联运"])
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    tok = _csrf(c)
    r = c.post("/api/login",
               data=json.dumps({"username": "t_youke", "password": "TestEe123456"}),
               content_type="application/json",
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    rows = c.get("/api/rows").get_json()
    assert {row["开票子公司名称"] for row in rows} == {"同程配", "保时达", "港九港铁",
                                                      "中欧木业", "沙坪坝", "太平洋",
                                                      "东盟", "联运"}
