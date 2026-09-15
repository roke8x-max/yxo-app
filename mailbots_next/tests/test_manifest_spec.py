# -*- coding: utf-8 -*-
"""修正A（spec_舱单导入_修正A_审计日志漏记目的站.md）回归测试。

(a) suffix_change 审计完整 + n_alert 按日志条数计（验收 14 机器可验）。
(b) revert_batch / revert_item 遇历史合体 field 不崩溃、不制造假回退。
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import manifest_engine as me


@pytest.fixture()
def conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER, order_idx REAL,
        "客户编码" TEXT, "箱号" TEXT, "封号" TEXT, "箱属" TEXT, "发班时间" TEXT,
        "台账月份" TEXT, "班列号" TEXT, "口岸" TEXT, "目的站" TEXT,
        "开票子公司名称" TEXT, "班列类型" TEXT, "状态" TEXT,
        is_deleted INTEGER DEFAULT 0, updated_by TEXT, updated_at TEXT)""")
    c.execute("""CREATE TABLE update_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, batch_type TEXT,
        record_id INTEGER, "客户编码" TEXT, "箱号" TEXT, field TEXT, old_value TEXT,
        new_value TEXT, action TEXT, source_file TEXT, operator TEXT,
        reverted INTEGER DEFAULT 0, reverted_at TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE import_batch (
        batch_id TEXT PRIMARY KEY, batch_type TEXT, source_files TEXT, snapshot TEXT,
        n_update INTEGER DEFAULT 0, n_insert INTEGER DEFAULT 0, n_alert INTEGER DEFAULT 0,
        operator TEXT, reverted INTEGER DEFAULT 0, created_at TEXT)""")
    c.commit()
    yield c
    c.close()
    os.unlink(path)


def seed(conn, **kw):
    cols = ",".join(f'"{k}"' for k in kw)
    ph = ",".join("?" * len(kw))
    cur = conn.execute(f"INSERT INTO records ({cols}) VALUES ({ph})", list(kw.values()))
    conn.commit()
    return cur.lastrowid


def apply_suffix(conn, rid, new_code, station, monkeypatch, tmp_path):
    monkeypatch.setattr(me, "BACKUP_DIR", str(tmp_path))
    diff = {"updates": [], "imports": [],
            "alerts_applied": [{"record_id": rid, "new_code": new_code, "目的站": station}]}
    bid = me.apply_diff(conn, diff, "毛骁洋", ["t.xlsx"])
    conn.commit()
    return bid


# ---------- (a) 审计完整 + 计数 ----------

def test_suffix_change_logs_both_columns(conn, tmp_path, monkeypatch):
    """两列都变 → 两条单列日志，新旧值齐全，n_alert +2。"""
    rid = seed(conn, **{"客户编码": "A001-DMZ", "箱号": "AB1", "班列号": "WB6",
                        "目的站": "电煤"})
    bid = apply_suffix(conn, rid, "A001-VXN", "沃尔西诺", monkeypatch, tmp_path)
    rows = conn.execute(
        "SELECT field,old_value,new_value FROM update_log WHERE batch_id=? ORDER BY id",
        (bid,)).fetchall()
    assert [(r["field"], r["old_value"], r["new_value"]) for r in rows] == [
        ("客户编码", "A001-DMZ", "A001-VXN"),
        ("目的站", "电煤", "沃尔西诺"),
    ]
    n = conn.execute("SELECT n_alert FROM import_batch WHERE batch_id=?",
                     (bid,)).fetchone()["n_alert"]
    assert n == 2


def test_suffix_change_single_column(conn, tmp_path, monkeypatch):
    """只变目的站 → 仅 1 条 field='目的站' 日志，n_alert +1。"""
    rid = seed(conn, **{"客户编码": "A002-DMZ", "箱号": "AB2", "班列号": "WB6",
                        "目的站": "电煤"})
    bid = apply_suffix(conn, rid, "A002-DMZ", "沃尔西诺", monkeypatch, tmp_path)
    rows = conn.execute(
        "SELECT field,old_value,new_value FROM update_log WHERE batch_id=?",
        (bid,)).fetchall()
    assert [(r["field"], r["old_value"], r["new_value"]) for r in rows] == [
        ("目的站", "电煤", "沃尔西诺"),
    ]
    n = conn.execute("SELECT n_alert FROM import_batch WHERE batch_id=?",
                     (bid,)).fetchone()["n_alert"]
    assert n == 1


def test_suffix_change_noop_writes_no_log(conn, tmp_path, monkeypatch):
    """两列都没真变 → 不写日志行，n_alert +0。"""
    rid = seed(conn, **{"客户编码": "A003-DMZ", "箱号": "AB3", "班列号": "WB6",
                        "目的站": "电煤"})
    bid = apply_suffix(conn, rid, "A003-DMZ", "电煤", monkeypatch, tmp_path)
    rows = conn.execute(
        "SELECT COUNT(*) c FROM update_log WHERE batch_id=?", (bid,)).fetchone()["c"]
    assert rows == 0
    n = conn.execute("SELECT n_alert FROM import_batch WHERE batch_id=?",
                     (bid,)).fetchone()["n_alert"]
    assert n == 0


# ---------- (b) revert 不崩溃 ----------

def _legacy_log(conn, bid="OLD-1", rid=1):
    conn.execute(
        "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,field,"
        "old_value,new_value,action,source_file,operator,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (bid, "update", rid, "L001-DMZ", "", "客户编码/目的站",
         "", "L001-VXN", "改", "old.xlsx", "毛骁洋", "2026-09-01 00:00:00"))
    conn.commit()


def test_revert_batch_skips_legacy_combined_field(conn):
    """历史合体 field 日志 → revert_batch 不抛 SQLITE_ERROR，该行 reverted 保持 0，其余行正常回退。"""
    rid = seed(conn, **{"客户编码": "B001-DMZ", "箱号": "BB1", "班列号": "WB6",
                        "封号": "S-OLD"})
    _legacy_log(conn, bid="OLD-1", rid=rid)
    conn.execute(
        "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,field,"
        "old_value,new_value,action,source_file,operator,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("OLD-1", "update", rid, "B001-DMZ", "BB1", "封号",
         "S-OLD", "S-NEW", "改", "old.xlsx", "毛骁洋", "2026-09-01 00:00:00"))
    conn.commit()
    me.revert_batch(conn, "OLD-1")  # 不崩溃是核心断言
    conn.commit()
    legacy = conn.execute(
        "SELECT reverted FROM update_log WHERE batch_id=? AND field=?",
        ("OLD-1", "客户编码/目的站")).fetchone()
    assert legacy["reverted"] == 0  # 跳过且不制造假回退
    seal = conn.execute(
        "SELECT reverted FROM update_log WHERE batch_id=? AND field=?",
        ("OLD-1", "封号")).fetchone()
    assert seal["reverted"] == 1  # 正常行照常回退标记


def test_revert_item_legacy_combined_field(conn):
    """历史合体 field 日志 → revert_item 返回 True，不标记 reverted，不抛错。"""
    rid = seed(conn, **{"客户编码": "B002-DMZ", "箱号": "BB2", "班列号": "WB6"})
    _legacy_log(conn, bid="OLD-2", rid=rid)
    log_id = conn.execute(
        "SELECT id FROM update_log WHERE batch_id=?", ("OLD-2",)).fetchone()["id"]
    assert me.revert_item(conn, log_id) is True
    conn.commit()
    assert conn.execute(
        "SELECT reverted FROM update_log WHERE id=?", (log_id,)).fetchone()["reverted"] == 0


def test_revert_batch_new_single_column_logs(conn, tmp_path, monkeypatch):
    """新批次两条单列日志 → revert_batch 不崩；按白名单不回写、reverted 保持 0；records 保持新值。"""
    rid = seed(conn, **{"客户编码": "C001-DMZ", "箱号": "CB1", "班列号": "WB6",
                        "目的站": "电煤"})
    bid = apply_suffix(conn, rid, "C001-VXN", "沃尔西诺", monkeypatch, tmp_path)
    me.revert_batch(conn, bid)
    conn.commit()
    rows = conn.execute(
        "SELECT field,reverted FROM update_log WHERE batch_id=?", (bid,)).fetchall()
    assert sorted((r["field"], r["reverted"]) for r in rows) == [
        ("客户编码", 0), ("目的站", 0),
    ]
    r = conn.execute('SELECT "客户编码","目的站" FROM records WHERE id=?',
                     (rid,)).fetchone()
    assert (r["客户编码"], r["目的站"]) == ("C001-VXN", "沃尔西诺")
