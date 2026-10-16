#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR-A：sync_departure（按班列批量改发班时间）必须排除退舱行。

任务书 PR-A 节验收 1-4。依据 docs/mainsite/specs/退舱记录处理约定.md §3/§4：
退舱记录不得作为批量操作的落点；人工手动删除仍允许（"人能删，系统不替人删"）。

夹具样板照抄 tests/auth_gate_test.py：返回值是元组 (c, appmod)；
写请求必须带 X-CSRF-Token（全局 before_request 校验）。
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

ADMIN = "t_admin"
PW = {"ADMIN": "TestAa123456"}
TRAIN = "WB9999"
NORMAL_DEP = "2026-08-01"
CANCEL_DEP = "2026-08-02"


def _seed_two_rows(db_path):
    """同一班列 WB9999 造两条：正常行（2026-08-01）+ 退舱行（2026-08-02）。"""
    conn = sqlite3.connect(db_path)
    cols = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        f'seq INTEGER, {cols}, updated_at TEXT, updated_by TEXT, order_idx REAL, '
        f'is_deleted INTEGER DEFAULT 0, deleted_at TEXT, deleted_by TEXT)')
    conn.execute("DELETE FROM records")
    for status, dep in (("正常", NORMAL_DEP), ("退舱", CANCEL_DEP)):
        vals = ["" for _ in config.ALL_FIELDS]
        vals[config.ALL_FIELDS.index("班列号")] = TRAIN
        vals[config.ALL_FIELDS.index("发班时间")] = dep
        vals[config.ALL_FIELDS.index("状态")] = status
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
    auth_db = str(tmp_path / "auth.db")
    yxo_db = str(tmp_path / "yxo.db")
    monkeypatch.setattr(config, "AUTH_DB_PATH", auth_db)
    monkeypatch.setattr(config, "AUTH_ENABLED", True, raising=False)
    import app as appmod  # 延迟导入：gate 每次请求都动态读 config.AUTH_DB_PATH
    monkeypatch.setattr(appmod, "DB", yxo_db)
    _seed_two_rows(yxo_db)
    from auth import schema, service
    schema.init_db(strict=False)
    service.create_user(ADMIN, PW["ADMIN"], role="admin", scope_type="all")
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    return c, appmod


def _csrf(c):
    r = c.get("/api/csrf")
    assert r.status_code == 200
    return r.get_json()["csrf_token"]


def _login(c):
    r = c.post("/api/login",
               data=json.dumps({"username": ADMIN, "password": PW["ADMIN"]}),
               content_type="application/json",
               headers={"X-CSRF-Token": _csrf(c)})
    assert r.status_code == 200, r.get_data(as_text=True)
    return _csrf(c)  # 登录后换新 token


def _rows(appmod):
    conn = sqlite3.connect(appmod.DB)
    rows = conn.execute(
        'SELECT id, "发班时间", "状态", COALESCE(is_deleted,0) FROM records ORDER BY id'
    ).fetchall()
    conn.close()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _insert_row(appmod, dep, status=""):
    conn = sqlite3.connect(appmod.DB)
    cols = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    vals = ["" for _ in config.ALL_FIELDS]
    vals[config.ALL_FIELDS.index("班列号")] = TRAIN
    vals[config.ALL_FIELDS.index("发班时间")] = dep
    vals[config.ALL_FIELDS.index("状态")] = status
    ph = ", ".join(["?"] * len(vals))
    cur = conn.execute(
        f"INSERT INTO records (seq, order_idx, {cols}) VALUES (?, ?, {ph})",
        [1, 1.0] + vals)
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def test_sync_departure_excludes_cancel_row(client):
    """验收 1-3：改正常行发班时间 → 退舱行不得被联动，synced==0。"""
    c, appmod = client
    tok = _login(c)
    rows = _rows(appmod)
    normal_id = next(i for i, v in rows.items() if v[1] == "正常")
    cancel_id = next(i for i, v in rows.items() if v[1] == "退舱")
    r = c.patch(f"/api/row/{normal_id}",
                json={"field": "发班时间", "value": "2026-08-15"},
                headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    assert r.get_json()["synced"] == 0  # 退舱行被排除，无人被联动
    got = _rows(appmod)
    assert got[normal_id][0] == "2026-08-15"
    assert got[cancel_id][0] == CANCEL_DEP, "退舱行发班时间不得被批量联动改写"
    assert got[cancel_id][1] == "退舱"


def test_cancel_row_still_soft_deletable_manually(client):
    """验收 4：人工手动单条软删退舱行仍允许（人能删，系统不替人删）。"""
    c, appmod = client
    tok = _login(c)
    cancel_id = next(i for i, v in _rows(appmod).items() if v[1] == "退舱")
    r = c.delete(f"/api/row/{cancel_id}", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    assert _rows(appmod)[cancel_id][2] == 1


def test_normal_same_year_row_still_synced(client):
    """回归：同班列同原年份的正常行仍照旧联动，不能为了排除退舱误伤正常行。"""
    c, appmod = client
    tok = _login(c)
    same_year_id = _insert_row(appmod, "2026-08-05")            # 同年 → 联动
    other_year_id = _insert_row(appmod, "2027-08-03")           # 跨年 → 年份锁定，不联动
    normal_id = next(i for i, v in _rows(appmod).items() if v[1] == "正常" and v[0] == NORMAL_DEP)
    r = c.patch(f"/api/row/{normal_id}",
                json={"field": "发班时间", "value": "2026-08-15"},
                headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    assert r.get_json()["synced"] == 1  # 只有同年那条正常行被联动
    got = _rows(appmod)
    assert got[same_year_id][0] == "2026-08-15"
    assert got[other_year_id][0] == "2027-08-03", "跨年同号不受影响（old_year 锁定语义不变）"


def test_batch_cells_fallback_excludes_cancel_row(client):
    """影响面回归：api_cells（sendBeacon 兜底批量保存）复用 sync_departure，同样不得碰退舱行。"""
    c, appmod = client
    tok = _login(c)
    rows = _rows(appmod)
    normal_id = next(i for i, v in rows.items() if v[1] == "正常")
    cancel_id = next(i for i, v in rows.items() if v[1] == "退舱")
    r = c.post("/api/cells",
               json={"edits": [{"id": normal_id, "field": "发班时间", "value": "2026-08-20"}]},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    got = _rows(appmod)
    assert got[normal_id][0] == "2026-08-20"
    assert got[cancel_id][0] == CANCEL_DEP


def test_frontend_sync_departure_local_excludes_cancel():
    """前端本地同步（syncDepartureLocal）也要短路退舱行，否则屏幕值与服务端不一致。

    断言必须落在 syncDepartureLocal 函数体内 —— app.js 其他函数（如数据过滤）
    已有同形态判据，全局搜子串会假通过。
    """
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()
    m = re.search(r"function syncDepartureLocal\(.*?\n\}", src, re.S)
    assert m, "未找到 syncDepartureLocal 函数（前端被重构过？）"
    assert 'String(r["状态"] || "").trim() === "退舱"' in m.group(0)
