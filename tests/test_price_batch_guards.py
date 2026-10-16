#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR-E：价格批量端点补守卫（R3/R4 转正修）。

任务书 3bis 节。退舱记录不得作为批量操作的落点（AGENTS.md 硬规则 12；
退舱记录处理约定.md §3/§4）；/api/price/batch 往软删行写单价还会盖
updated_by/updated_at，污染"空壳重复"判据（约定 §5）。

夹具样板照抄 tests/auth_gate_test.py：返回值是元组 (c, appmod)；
写请求必须带 X-CSRF-Token。两个端点测试函数各自独立夹具（全新 tmp 目录），
互不污染。跳过 = 保留原单价，不置空（P-c）。
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
PRICE = 7000.0  # 1000.0(USD) * 7.0


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


def _insert(db_path, price="", status="", deleted=0, by=""):
    """四类行统一经此造：同月/同规则命中，保证被端点扫到。"""
    conn = sqlite3.connect(db_path)
    cols = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    vals = ["" for _ in config.ALL_FIELDS]
    vals[config.ALL_FIELDS.index("口岸")] = "山口"
    vals[config.ALL_FIELDS.index("目的站")] = "莫斯科"
    vals[config.ALL_FIELDS.index("箱属")] = "SOC"
    vals[config.ALL_FIELDS.index("发班时间")] = "2026-08-10"
    vals[config.ALL_FIELDS.index("单箱价格")] = price
    vals[config.ALL_FIELDS.index("状态")] = status
    ph = ", ".join(["?"] * len(vals))
    cur = conn.execute(
        f"INSERT INTO records (seq, order_idx, {cols}, is_deleted, updated_by) "
        f"VALUES (?, ?, {ph}, ?, ?)", [1, 1.0] + vals + [deleted, by])
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def _write_cfg(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"prices": {MONTH: {"山口|莫斯科": {"SOC": 1000.0, "currency": "USD"}}},
                   "exrates": {MONTH: {"USD": 7.0}}}, f, ensure_ascii=False)


def _get(db_path, rid):
    conn = sqlite3.connect(db_path)
    r = conn.execute('SELECT "单箱价格", "状态", COALESCE(is_deleted,0), '
                     'COALESCE(updated_by,"") FROM records WHERE id=?', (rid,)).fetchone()
    conn.close()
    return {"price": r[0], "by": r[3]}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    auth_db = str(tmp_path / "auth.db")
    yxo_db = str(tmp_path / "yxo.db")
    monkeypatch.setattr(config, "AUTH_DB_PATH", auth_db)
    monkeypatch.setattr(config, "AUTH_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "PRICE_CONFIG", str(tmp_path / "price_config.json"))
    monkeypatch.setattr(admin_api, "PRICE_CONFIG", str(tmp_path / "price_config.json"))
    monkeypatch.setattr(admin_api, "PRICE_BACKUP_DIR", str(tmp_path / "price_backups"))
    monkeypatch.setattr(admin_api, "PRICE_PENDING", str(tmp_path / "price_import_pending.json"))
    monkeypatch.setattr(admin_api, "YXO_DB", yxo_db)
    import app as appmod  # 延迟导入：gate 每次请求都动态读 config.AUTH_DB_PATH
    monkeypatch.setattr(appmod, "DB", yxo_db)
    _seed_records(yxo_db)
    _write_cfg(str(tmp_path / "price_config.json"))
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


def test_batch_skips_cancel_and_deleted(client):
    """R3：退舱/软删/重叠行跳过且计数互斥；正常空价行照旧补；已有价行不动（只补空价）。"""
    c, appmod = client
    tok = _login(c)
    a = _insert(appmod.DB)                                  # 正常空价 → priced
    b = _insert(appmod.DB, price="0", status="退舱")         # 退舱（"0"视同空价，会被改写→须守）
    cc = _insert(appmod.DB, deleted=1)                      # 已软删
    d = _insert(appmod.DB, status="退舱", deleted=1)        # 重叠：退舱+软删
    e = _insert(appmod.DB, price="5555", by="韩文豪")       # 已有手填价 → 只补空价，不动
    r = c.post("/api/price/batch", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    for f in ("total", "priced", "cancelled_skipped", "deleted_skipped"):
        assert f in body, f"响应缺字段：{f}"
    assert body["total"] == 5, "total = SELECT 取回行数（全表，含软删）"
    assert body["priced"] == 1, "只有正常空价行被写入（既有 priced 语义）"
    assert body["cancelled_skipped"] == 2, "退舱行 + 重叠行"
    assert body["deleted_skipped"] == 1, "纯软删行"
    assert body["priced"] + body["cancelled_skipped"] + body["deleted_skipped"] \
        <= body["total"]
    assert _get(appmod.DB, a)["price"] == PRICE
    got_b = _get(appmod.DB, b)
    assert got_b["price"] == "0" and got_b["by"] == "", "退舱行一字不变（含署名不被污染）"
    got_c = _get(appmod.DB, cc)
    assert (got_c["price"] or "") == "" and got_c["by"] == "", "软删行不动"
    got_d = _get(appmod.DB, d)
    assert (got_d["price"] or "") == "" and got_d["by"] == "", "重叠行绝不进 priced"
    assert _get(appmod.DB, e)["price"] == "5555", "只补空价口径不变"


def test_recalc_skips_cancel(client):
    """R4：退舱跳过（仍计 scanned）+ priced == updated + filled + deleted_skipped 恒 0."""
    c, appmod = client
    tok = _login(c)
    a = _insert(appmod.DB)                                  # 正常空价 → filled
    b = _insert(appmod.DB, status="退舱")                   # 退舱空价 → 会被补算→须守
    h = _insert(appmod.DB, price="9999", by="韩文豪")       # 手填价 → 不动（既有行为）
    _insert(appmod.DB, deleted=1)                           # 软删：WHERE 已过滤，取不回
    _insert(appmod.DB, status="退舱", deleted=1)             # 重叠：同样取不回
    r = c.post("/api/admin/price/recalc", json={"month": MONTH},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    for f in ("total", "priced", "cancelled_skipped", "deleted_skipped"):
        assert f in body, f"响应缺字段：{f}"
    for f in ("scanned", "updated", "filled", "failed"):
        assert f in body, f"既有字段必须保留：{f}"
    assert body["total"] == 3, "total = 取回行数（软删已过滤）"
    assert body["scanned"] == 3, "scanned 语义不变：退舱行仍计入月份命中"
    assert body["priced"] == body["updated"] + body["filled"] == 1
    assert body["cancelled_skipped"] == 1
    assert body["deleted_skipped"] == 0, "恒 0 是预期值，不是失败"
    assert body["priced"] + body["cancelled_skipped"] + body["deleted_skipped"] \
        <= body["total"]
    assert _get(appmod.DB, a)["price"] == PRICE
    got_b = _get(appmod.DB, b)
    assert (got_b["price"] or "") == "" and got_b["by"] == "", "退舱行保留原单价"
    assert _get(appmod.DB, h)["price"] == "9999", "手填价格不动"
