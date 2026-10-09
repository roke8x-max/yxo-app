#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G9 舱单页 CSRF + 登录入口 回归（taskbook §4A）。

1. 静态：templates/manifest.html 含三件套（loginModal 弹层 / api/csrf 取种 /
   X-CSRF-Token / showLoginModal / api() 401 分支），防回退。
2. 门禁链：AUTH_ENABLED=1 下 POST /api/manifest/apply 无 CSRF 头 → 403；
   带有效 token 但未登录 → 401（证明 CSRF 层通过、登录层拦截）；
   admin 登录后带 token → 不再是 401/403（已穿过两层门禁，业务层 400/500 亦算通过）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

ADMIN = "t_admin"
ADMIN_PW = "TestAa123456"


def _template():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "templates", "manifest.html")
    with open(p, encoding="utf-8") as f:
        return f.read()


def test_manifest_has_login_modal_and_csrf_seed():
    src = _template()
    assert 'id="loginModal"' in src
    assert "api/csrf" in src
    assert "X-CSRF-Token" in src
    assert "showLoginModal" in src
    assert "CSRF_TOKEN" in src
    assert "r.status === 401" in src


@pytest.fixture()
def mclient(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(config, "AUTH_ENABLED", True, raising=False)
    import app as appmod
    monkeypatch.setattr(appmod, "DB", str(tmp_path / "yxo.db"))
    from auth import schema, service
    schema.init_db(strict=False)
    service.create_user(ADMIN, ADMIN_PW, role="admin", scope_type="all")
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def _csrf(c):
    r = c.get("/api/csrf")
    assert r.status_code == 200
    return r.get_json()["csrf_token"]


def test_manifest_page_public(mclient):
    assert mclient.get("/manifest").status_code == 200


def test_manifest_apply_needs_csrf(mclient):
    r = mclient.post("/api/manifest/apply", json={})
    assert r.status_code == 403


def test_manifest_apply_token_but_no_login_401(mclient):
    tok = _csrf(mclient)
    r = mclient.post("/api/manifest/apply", json={},
                     headers={"X-CSRF-Token": tok})
    assert r.status_code == 401


def test_manifest_apply_admin_passes_gate(mclient):
    tok = _csrf(mclient)
    r = mclient.post("/api/login",
                     data=json.dumps({"username": ADMIN, "password": ADMIN_PW}),
                     content_type="application/json",
                     headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    tok2 = _csrf(mclient)
    r2 = mclient.post("/api/manifest/apply", json={},
                      headers={"X-CSRF-Token": tok2})
    # 穿过 CSRF + 权限两层即算通过；业务层 400/500（缺表/缺参数）同样证明门禁已放行
    assert r2.status_code not in (401, 403)
