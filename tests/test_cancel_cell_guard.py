#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""退舱行「单格编辑」守卫（2026-10-19 拍板）。

规则：退舱行单格只允许改「备注」「状态」；批量只允许改「备注」(状态/其他字段跳过)。
夹具照抄 tests/test_sync_departure_cancel.py：返回 (c, appmod)，写请求带 X-CSRF-Token。
"""
import json
import os
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

ADMIN = "t_admin"
PW = {"ADMIN": "TestAa123456"}
TRAIN = "WB9999"
DELAY_TRAIN = "WB8888"
NORMAL_DEP = "2026-08-01"
CANCEL_DEP = "2026-08-02"
DELAY_DEP = "2026-08-03"


def _seed_three_rows(db_path):
    """同一班列 WB9999：正常 + 退舱；另一班列：延期（边界，非退舱）。"""
    conn = sqlite3.connect(db_path)
    cols = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        f'seq INTEGER, {cols}, updated_at TEXT, updated_by TEXT, order_idx REAL, '
        f'is_deleted INTEGER DEFAULT 0, deleted_at TEXT, deleted_by TEXT)')
    conn.execute("DELETE FROM records")
    seed = [
        ("正常", NORMAL_DEP, TRAIN, "BOX-NORMAL", "CUST-NORMAL", "莫斯科", "备注正常"),
        ("退舱", CANCEL_DEP, TRAIN, "BOX-CANCEL", "CUST-CANCEL", "沃尔西诺", "备注退舱"),
        ("延期", DELAY_DEP, DELAY_TRAIN, "BOX-DELAY", "CUST-DELAY", "电煤", "备注延期"),
    ]
    for status, dep, train, box_no, cust, dest, remark in seed:
        vals = ["" for _ in config.ALL_FIELDS]
        vals[config.ALL_FIELDS.index("班列号")] = train
        vals[config.ALL_FIELDS.index("发班时间")] = dep
        vals[config.ALL_FIELDS.index("状态")] = status
        vals[config.ALL_FIELDS.index("箱号")] = box_no
        vals[config.ALL_FIELDS.index("客户编码")] = cust
        vals[config.ALL_FIELDS.index("目的站")] = dest
        vals[config.ALL_FIELDS.index("备注")] = remark
        vals[config.ALL_FIELDS.index("单箱价格")] = "8888"
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
    _seed_three_rows(yxo_db)
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
    return _csrf(c)


def _ids(appmod):
    conn = sqlite3.connect(appmod.DB)
    rows = conn.execute('SELECT id, "状态" FROM records ORDER BY id').fetchall()
    conn.close()
    return {status: rid for rid, status in rows}


def _val(appmod, rid, field):
    conn = sqlite3.connect(appmod.DB)
    r = conn.execute(f'SELECT "{field}" FROM records WHERE id=?', (rid,)).fetchone()
    conn.close()
    return r[0] if r else None


def _patch(c, tok, rid, field, value):
    return c.patch(f"/api/row/{rid}", json={"field": field, "value": value},
                   headers={"X-CSRF-Token": tok})


def _cells(c, tok, rid, field, value):
    return c.post("/api/cells",
                  json={"edits": [{"id": rid, "field": field, "value": value}]},
                  headers={"X-CSRF-Token": tok})


def test_patch_cancel_remark_allowed(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = _patch(c, tok, rid, "备注", "退舱备注已改")
    assert r.status_code == 200
    assert _val(appmod, rid, "备注") == "退舱备注已改"


def test_patch_cancel_status_allowed(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = _patch(c, tok, rid, "状态", "正常")
    assert r.status_code == 200
    assert _val(appmod, rid, "状态") == "正常"


def test_patch_cancel_box_rejected(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = _patch(c, tok, rid, "箱号", "BOX-HACK")
    assert r.status_code == 403
    assert _val(appmod, rid, "箱号") == "BOX-CANCEL"


def test_patch_cancel_customer_code_rejected(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = _patch(c, tok, rid, "客户编码", "CUST-HACK")
    assert r.status_code == 403
    assert _val(appmod, rid, "客户编码") == "CUST-CANCEL"


def test_patch_cancel_departure_rejected_without_sync(client):
    c, appmod = client
    tok = _login(c)
    ids = _ids(appmod)
    cancel_id, normal_id = ids["退舱"], ids["正常"]
    r = _patch(c, tok, cancel_id, "发班时间", "2026-09-01")
    assert r.status_code == 403
    assert _val(appmod, cancel_id, "发班时间") == CANCEL_DEP
    assert _val(appmod, normal_id, "发班时间") == NORMAL_DEP, "同班列正常行不得被联动"


def test_patch_cancel_destination_rejected(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = _patch(c, tok, rid, "目的站", "莫斯科")
    assert r.status_code == 403
    assert _val(appmod, rid, "目的站") == "沃尔西诺"


def test_cells_cancel_box_skipped(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = _cells(c, tok, rid, "箱号", "BOX-HACK")
    assert r.status_code == 200
    j = r.get_json()
    assert j["saved"] == 0
    assert j["cancelled_skipped"] == 1
    assert _val(appmod, rid, "箱号") == "BOX-CANCEL"


def test_cells_cancel_remark_allowed(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = _cells(c, tok, rid, "备注", "批量备注已改")
    assert r.status_code == 200
    j = r.get_json()
    assert j["saved"] == 1
    assert j["cancelled_skipped"] == 0
    assert _val(appmod, rid, "备注") == "批量备注已改"


def test_cells_cancel_status_skipped(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = _cells(c, tok, rid, "状态", "正常")
    assert r.status_code == 200
    j = r.get_json()
    assert j["saved"] == 0
    assert j["cancelled_skipped"] == 1
    assert _val(appmod, rid, "状态") == "退舱"


def test_patch_normal_row_unaffected(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["正常"]
    r = _patch(c, tok, rid, "客户编码", "CUST-NORMAL-2")
    assert r.status_code == 200
    assert _val(appmod, rid, "客户编码") == "CUST-NORMAL-2"


def test_patch_normal_departure_still_syncs_but_not_cancel(client):
    c, appmod = client
    tok = _login(c)
    ids = _ids(appmod)
    normal_id, cancel_id = ids["正常"], ids["退舱"]
    r = _patch(c, tok, normal_id, "发班时间", "2026-08-15")
    assert r.status_code == 200
    assert r.get_json()["synced"] == 0, "同班列只有退舱行，必须被排除"
    assert _val(appmod, normal_id, "发班时间") == "2026-08-15"
    assert _val(appmod, cancel_id, "发班时间") == CANCEL_DEP


def test_delete_cancel_row_still_allowed(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = c.delete(f"/api/row/{rid}", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    conn = sqlite3.connect(appmod.DB)
    deleted = conn.execute("SELECT is_deleted FROM records WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    assert deleted == 1


def test_soft_deleted_cancel_still_rejected(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    conn = sqlite3.connect(appmod.DB)
    conn.execute("UPDATE records SET is_deleted=1 WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    r = _patch(c, tok, rid, "箱号", "BOX-HACK")
    assert r.status_code == 403
    assert _val(appmod, rid, "箱号") == "BOX-CANCEL"


def test_delay_row_is_not_locked(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["延期"]
    r = _patch(c, tok, rid, "箱号", "BOX-DELAY-2")
    assert r.status_code == 200
    assert _val(appmod, rid, "箱号") == "BOX-DELAY-2"


def test_price_cancel_row_rejected(client):
    c, appmod = client
    tok = _login(c)
    rid = _ids(appmod)["退舱"]
    r = c.post(f"/api/price/{rid}", headers={"X-CSRF-Token": tok})
    assert r.status_code == 403
    assert _val(appmod, rid, "单箱价格") == "8888"


def _function_body(src, name):
    m = re.search(r"function " + name + r"\(.*?\n\}", src, re.S)
    assert m, f"未找到 {name} 函数（前端被重构过？）"
    return m.group(0)


def test_frontend_cell_guards_present():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()
    assert "isCancelLocked" in _function_body(src, "openCellEditor")
    assert "isCancelLocked" in _function_body(src, "saveCell")
    assert "isCancelBatchLocked" in _function_body(src, "applyEdits")
    assert "isCancelBatchLocked" in _function_body(src, "doGridPaste")
    assert "isCancelBatchLocked" in _function_body(src, "applyBulkSet")


def test_frontend_paste_to_row_guard_present():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()
    assert "isCancelLocked" in _function_body(src, "pasteToRow")


def test_frontend_grid_paste_skip_message_present():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()
    assert "已跳过" in _function_body(src, "doGridPaste")


def test_frontend_asset_version_bumped():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "templates", "index.html"), encoding="utf-8").read()
    assert "app.js?v=20260805copy" not in html
    # 2026-10-26：任务 02 bump 到 20261026pw（旧值进「不能出现」名单，判据只收紧不放松）
    assert "app.js?v=20261023chpw" not in html
    assert "app.js?v=20261026pw" in html
