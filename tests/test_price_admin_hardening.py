#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR-B：价格维护加固（B①–B⑤）。

任务书 PR-B 节验收 1–7。夹具样板照抄 tests/auth_gate_test.py：
返回值是元组 (c, appmod)；写请求必须带 X-CSRF-Token。

范围红线：R3（app.py:922-945 查询语义）与 R4（admin_api.py:1109-1111 查询守卫）
只登记不修 —— 本文件断言绝不触碰这两处行为。
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import admin_api  # noqa: E402

ADMIN = "t_admin"
PW = "TestAa123456"
MONTH = "2026-08"


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


def _insert_price_row(db_path, port="山口", dest="莫斯科", box="COC",
                      depart="2026-08-10", price=""):
    conn = sqlite3.connect(db_path)
    cols = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    vals = ["" for _ in config.ALL_FIELDS]
    vals[config.ALL_FIELDS.index("口岸")] = port
    vals[config.ALL_FIELDS.index("目的站")] = dest
    vals[config.ALL_FIELDS.index("箱属")] = box
    vals[config.ALL_FIELDS.index("发班时间")] = depart
    vals[config.ALL_FIELDS.index("单箱价格")] = price
    ph = ", ".join(["?"] * len(vals))
    cur = conn.execute(f"INSERT INTO records (seq, order_idx, {cols}) VALUES (?, ?, {ph})",
                       [1, 1.0] + vals)
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """AUTH_ENABLED=1 的登录客户端；价格三件路径全部指向临时目录."""
    auth_db = str(tmp_path / "auth.db")
    yxo_db = str(tmp_path / "yxo.db")
    price_cfg = str(tmp_path / "price_config.json")
    backups = str(tmp_path / "price_backups")
    pending = str(tmp_path / "price_import_pending.json")
    monkeypatch.setattr(config, "AUTH_DB_PATH", auth_db)
    monkeypatch.setattr(config, "AUTH_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "PRICE_CONFIG", price_cfg)
    monkeypatch.setattr(admin_api, "PRICE_CONFIG", price_cfg)
    monkeypatch.setattr(admin_api, "PRICE_BACKUP_DIR", backups)
    monkeypatch.setattr(admin_api, "PRICE_PENDING", pending)
    monkeypatch.setattr(admin_api, "YXO_DB", yxo_db)
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


def _write_cfg(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)


def _soc_only_cfg():
    return {"prices": {MONTH: {"山口|莫斯科": {"SOC": 1000.0, "currency": "USD"}}},
            "exrates": {MONTH: {"USD": 7.0}}}


def _audit_rows(auth_db):
    conn = sqlite3.connect(auth_db)
    rows = conn.execute("SELECT event, username, target, detail FROM auth_audit").fetchall()
    conn.close()
    return rows


# ---------------- B① 读价失败 → 报错（两处实现同步） ----------------

def test_read_fail_get_price_errors_not_empty_table(client, tmp_path, monkeypatch):
    """验收1：PRICE_CONFIG 指向不存在路径 → GET 明确"读取失败"（不是 200 空表）；保存被拒."""
    c, appmod = client
    tok = _login(c)
    missing = str(tmp_path / "no_such_dir" / "price_config.json")
    monkeypatch.setattr(admin_api, "PRICE_CONFIG", missing)  # 模拟文件丢失
    r = c.get("/api/admin/price")
    assert r.status_code == 500
    assert "读取失败" in r.get_json()["msg"]
    r2 = c.post("/api/admin/price/entry",
                json={"month": MONTH, "port": "山口", "dest": "莫斯科",
                      "SOC": "1000", "COC": "", "currency": "USD"},
                headers={"X-CSRF-Token": tok})
    assert r2.status_code == 500
    assert r2.get_json()["ok"] is False  # 保存被拒，未落盘
    assert not os.path.exists(missing)


def test_app_price_endpoints_fail_closed_on_read_error(client, tmp_path, monkeypatch):
    """验收1（app 侧）：/api/price/<id> 与 /api/price/batch 读价失败 → 报错且不写库."""
    c, appmod = client
    tok = _login(c)
    rid = _insert_price_row(appmod.DB)
    monkeypatch.setattr(config, "PRICE_CONFIG",
                        str(tmp_path / "no_such_dir" / "price_config.json"))
    r = c.post(f"/api/price/{rid}", headers={"X-CSRF-Token": tok})
    assert r.status_code == 500
    assert "读取失败" in r.get_json()["msg"]
    r2 = c.post("/api/price/batch", headers={"X-CSRF-Token": tok})
    assert r2.status_code == 500
    assert r2.get_json()["ok"] is False
    conn = sqlite3.connect(appmod.DB)
    got = conn.execute('SELECT "单箱价格", updated_by FROM records WHERE id=?',
                       (rid,)).fetchone()
    conn.close()
    assert (got[0] or "") == "" and (got[1] or "") == "", "读价失败时绝不能落库"


# ---------------- B② COC 无价不回落 SOC（两处同步） ----------------

def test_coc_without_price_stays_blank(client, tmp_path):
    """验收2：只有 SOC 价、无 COC 价的规则 → COC 箱算价留空，不回落 SOC."""
    c, appmod = client
    tok = _login(c)
    _write_cfg(admin_api.PRICE_CONFIG, _soc_only_cfg())
    rid = _insert_price_row(appmod.DB)
    r = c.post(f"/api/price/{rid}", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is False
    assert "无匹配价格" in body["msg"]
    conn = sqlite3.connect(appmod.DB)
    got = conn.execute('SELECT "单箱价格" FROM records WHERE id=?', (rid,)).fetchone()[0]
    conn.close()
    assert (got or "") == "", "COC 无价必须留空，不得用 SOC 价顶"


def test_compute_price_local_coc_no_fallback():
    """B② 第二实现：admin_api._compute_price_local 同样不回落."""
    got = admin_api._compute_price_local("山口", "莫斯科", "COC", MONTH, _soc_only_cfg())
    assert got is None
    assert admin_api._compute_price_local("山口", "莫斯科", "SOC", MONTH,
                                          _soc_only_cfg()) == 7000.0


# ---------------- B③ 提示分开 ----------------

def test_error_messages_distinct(client, tmp_path, monkeypatch):
    """验收3：读失败提示 vs 真无匹配价提示必须明显不同；COC 无价带 reason."""
    c, appmod = client
    tok = _login(c)
    rid = _insert_price_row(appmod.DB)
    _write_cfg(admin_api.PRICE_CONFIG, _soc_only_cfg())
    r_nomatch = c.post(f"/api/price/{rid}", headers={"X-CSRF-Token": tok})
    msg_nomatch = r_nomatch.get_json()["msg"]
    assert "无匹配价格" in msg_nomatch
    assert r_nomatch.get_json().get("reason", "") != "", "COC 无价应带 reason 区分"
    assert "COC" in r_nomatch.get_json()["reason"]
    monkeypatch.setattr(config, "PRICE_CONFIG",
                        str(tmp_path / "no_such_dir" / "price_config.json"))
    r_fail = c.post(f"/api/price/{rid}", headers={"X-CSRF-Token": tok})
    msg_fail = r_fail.get_json()["msg"]
    assert "读取失败" in msg_fail
    assert msg_fail != msg_nomatch, "两句提示必须明显不同"


# ---------------- B④ 备份保留上限 + 从备份恢复 ----------------

def test_prune_backups_keep_30(tmp_path):
    """验收4：31 份备份 → 触发清理 → 只剩 30 份（直接调纯函数）."""
    d = tmp_path / "backups"
    d.mkdir()
    for i in range(1, 32):
        (d / f"price_config_20260101_0000{i:02d}.json").write_text("{}", encoding="utf-8")
    (d / "other.txt").write_text("x", encoding="utf-8")
    removed = admin_api._prune_price_backups(keep=30, backup_dir=str(d))
    assert removed == 1
    names = sorted(p.name for p in d.iterdir())
    assert len([n for n in names if n.startswith("price_config_")]) == 30
    assert "price_config_20260101_000001.json" not in names, "应删最旧的一份"
    assert "other.txt" in names, "非备份文件不得动"


def test_restore_roundtrip_and_audit(client):
    """验收5：从备份恢复 → 内容回到备份时点；恢复前后各留一份备份；有"恢复价格"审计."""
    c, appmod = client
    tok = _login(c)
    v1 = {"prices": {MONTH: {"山口|莫斯科": {"SOC": 111.0, "currency": "USD"}}},
          "exrates": {}}
    v2 = {"prices": {MONTH: {"山口|莫斯科": {"SOC": 222.0, "currency": "USD"}}},
          "exrates": {}}
    os.makedirs(admin_api.PRICE_BACKUP_DIR, exist_ok=True)
    bak = "price_config_20260101_000001.json"
    _write_cfg(os.path.join(admin_api.PRICE_BACKUP_DIR, bak), v1)
    _write_cfg(admin_api.PRICE_CONFIG, v2)
    auth_db = config.AUTH_DB_PATH
    before = len(_audit_rows(auth_db))
    r = c.post("/api/admin/price/restore", json={"file": bak},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True
    with open(admin_api.PRICE_CONFIG, encoding="utf-8") as f:
        assert json.load(f) == v1, "内容应回到备份时点"
    rest = [p.name for p in
            __import__("pathlib").Path(admin_api.PRICE_BACKUP_DIR).iterdir()
            if p.name.startswith("price_config_")]
    assert len(rest) >= 2, "恢复前后各留一份备份"
    audits = _audit_rows(auth_db)[before:]
    assert any(ev == "恢复价格" and f"file={bak}" in (dt or "")
               for ev, _, _, dt in audits), "恢复动作必须写审计"


def test_restore_rejects_bad_filename(client):
    """路径穿越 / 非白名单文件名 → 拒绝且不写盘."""
    c, appmod = client
    tok = _login(c)
    _write_cfg(admin_api.PRICE_CONFIG, _soc_only_cfg())
    for bad, code in (("../evil.json", 400), ("evil.json", 400),
                      ("price_config_20260101_000099.json", 404)):
        r = c.post("/api/admin/price/restore", json={"file": bad},
                   headers={"X-CSRF-Token": tok})
        assert r.status_code == code, bad
    with open(admin_api.PRICE_CONFIG, encoding="utf-8") as f:
        assert json.load(f) == _soc_only_cfg(), "非法恢复绝不能动正式配置"


def test_restore_works_when_config_corrupt(client):
    """补丁①：当前配置损坏（非法 JSON）时恢复仍可用 → 200 且内容回滚为备份."""
    c, appmod = client
    tok = _login(c)
    bak = "price_config_20260101_000001.json"
    good = {"prices": {MONTH: {"山口|莫斯科": {"SOC": 111.0, "currency": "USD"}}},
            "exrates": {}}
    os.makedirs(admin_api.PRICE_BACKUP_DIR, exist_ok=True)
    _write_cfg(os.path.join(admin_api.PRICE_BACKUP_DIR, bak), good)
    with open(admin_api.PRICE_CONFIG, "w", encoding="utf-8") as f:
        f.write("{not valid json!!!")
    r = c.post("/api/admin/price/restore", json={"file": bak},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True
    with open(admin_api.PRICE_CONFIG, encoding="utf-8") as f:
        assert json.load(f) == good


def test_restore_works_when_config_missing(client):
    """补丁①：当前配置缺失时恢复仍可用 → 200 且内容回滚为备份（与损坏场景隔离：独立函数+全新 tmp 目录）."""
    c, appmod = client
    tok = _login(c)
    bak = "price_config_20260101_000001.json"
    good = {"prices": {MONTH: {"山口|莫斯科": {"SOC": 111.0, "currency": "USD"}}},
            "exrates": {}}
    os.makedirs(admin_api.PRICE_BACKUP_DIR, exist_ok=True)
    _write_cfg(os.path.join(admin_api.PRICE_BACKUP_DIR, bak), good)
    assert not os.path.exists(admin_api.PRICE_CONFIG)
    r = c.post("/api/admin/price/restore", json={"file": bak},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True
    with open(admin_api.PRICE_CONFIG, encoding="utf-8") as f:
        assert json.load(f) == good


def test_backups_list_endpoint(client):
    """GET /api/admin/price/backups → 倒序 items[{file,mtime,size}]."""
    c, appmod = client
    tok = _login(c)
    os.makedirs(admin_api.PRICE_BACKUP_DIR, exist_ok=True)
    for n in ("price_config_20260101_000001.json", "price_config_20260102_000002.json"):
        with open(os.path.join(admin_api.PRICE_BACKUP_DIR, n), "w",
                               encoding="utf-8") as f:
            f.write("{}")
    r = c.get("/api/admin/price/backups")
    assert r.status_code == 200
    items = r.get_json()["items"]
    assert [it["file"] for it in items] == ["price_config_20260102_000002.json",
                                            "price_config_20260101_000001.json"]
    assert set(items[0]) >= {"file", "mtime", "size"}


def test_frontend_backup_restore_ui_present():
    """B④ UI：价格表卡片底部备份下拉 + 恢复按钮（二次 confirm）+ 成功后 loadPrice 刷新."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "templates", "admin.html"), encoding="utf-8").read()
    assert "function prLoadBackups" in src
    assert "function prRestore" in src
    assert 'id="prBackup"' in src
    assert "confirm(" in src


# ---------------- B⑤ 价格端点补审计 ----------------

def test_price_write_audit_trail(client):
    """验收6：改一次价格 → auth_audit 能查到谁·何时·旧值→新值（5 类事件全覆盖）."""
    c, appmod = client
    tok = _login(c)
    _write_cfg(admin_api.PRICE_CONFIG, {"prices": {}, "exrates": {}})
    auth_db = config.AUTH_DB_PATH
    before = len(_audit_rows(auth_db))
    assert c.post("/api/admin/price/exrate",
                  json={"month": MONTH, "currency": "USD", "rate": 7.0},
                  headers={"X-CSRF-Token": tok}).get_json()["ok"] is True
    assert c.post("/api/admin/price/exrate",
                  json={"month": MONTH, "currency": "USD", "rate": 8.0},
                  headers={"X-CSRF-Token": tok}).get_json()["ok"] is True
    assert c.post("/api/admin/price/entry",
                  json={"month": MONTH, "port": "山口", "dest": "莫斯科",
                        "SOC": "1000", "COC": "", "currency": "USD"},
                  headers={"X-CSRF-Token": tok}).get_json()["ok"] is True
    assert c.post("/api/admin/price/entry",
                  json={"month": MONTH, "port": "山口", "dest": "莫斯科",
                        "SOC": "2000", "COC": "", "currency": "USD"},
                  headers={"X-CSRF-Token": tok}).get_json()["ok"] is True
    assert c.post("/api/admin/price/entry/delete",
                  json={"month": MONTH, "port": "山口", "dest": "莫斯科"},
                  headers={"X-CSRF-Token": tok}).get_json()["ok"] is True
    _write_cfg(admin_api.PRICE_PENDING, {"prices": {MONTH: {"山口|莫斯科": {
        "SOC": 1000.0, "currency": "USD"}}}, "exrates": {}})
    assert c.post("/api/admin/price/import/confirm", json={},
                  headers={"X-CSRF-Token": tok}).get_json()["ok"] is True
    _insert_price_row(appmod.DB, price="")  # 空单价 → recalc 补算
    assert c.post("/api/admin/price/recalc", json={"month": MONTH},
                  headers={"X-CSRF-Token": tok}).get_json()["ok"] is True
    audits = _audit_rows(auth_db)[before:]
    by_event = {}
    for ev, user, target, dt in audits:
        by_event.setdefault(ev, []).append((user, target, dt or ""))
    for ev in ("改价·汇率", "改价·条目", "删价·条目", "导入价格", "重算价格"):
        assert ev in by_event, f"缺审计事件：{ev}"
    assert all(u == ADMIN for ev in by_event.values() for u, _, _ in ev), "署名应为操作人"
    assert "7.0" in by_event["改价·汇率"][0][2], "汇率审计应记新值"
    assert "山口|莫斯科" in by_event["改价·条目"][0][2]
    # 补丁②加严：第二次改汇率/改条目的审计必须同时含旧值与新值（旧值→新值）
    rate_dt = by_event["改价·汇率"][-1][2]
    assert "→" in rate_dt and "7.0" in rate_dt and "8.0" in rate_dt, "汇率审计须记旧值→新值"
    entry_dt = by_event["改价·条目"][-1][2]
    assert "→" in entry_dt and "1000.0" in entry_dt and "2000.0" in entry_dt, \
        "条目审计须记旧 dict→新 dict"
    assert MONTH in by_event["导入价格"][0][2]
    assert "priced=" in by_event["重算价格"][0][2]
