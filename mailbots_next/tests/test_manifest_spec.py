# -*- coding: utf-8 -*-
"""spec_舱单导入_专列放行与箱属封号确认.md 的验收测试（TDD）。

覆盖：改动一（专列阈值放行/弱提示/缺班列号）、改动二（箱属更新/字段冲突可确认/更新清单列）、
改动三（退舱剔除）。改动四为纯前端交互，由人工按 §5 (13)(14)(15) 验收。

【PR#13 修正A 回归用例（与本文件上面的功能批合并保留）】
修正A（spec_舱单导入_修正A_审计日志漏记目的站.md）回归测试。

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


def nrow(code, train="WB883", box="", seal="", owner="SOC", suffix_code=None,
         port="山口", dep="2026-09-01"):
    """构造一条归一化后的待导入行（normalize_row 输出形态）。"""
    raw_code = suffix_code or code
    return {"客户编码": raw_code, "core": me.code_core(raw_code),
            "suffix": me.code_suffix(raw_code), "箱号": box, "封号": seal,
            "箱属": owner, "口岸": port, "发班时间": dep, "班列号": train}


def codes(n, prefix="C"):
    return [f"{prefix}{i:04d}-DMZ" for i in range(n)]


# ---------- 改动三：退舱剔除 ----------

def test_t3_retired_code_treated_as_new(conn):
    """验收11：状态=退舱的客编视同不存在 → 走陌生客编放行进 imports，不报陌生客编。"""
    seed(conn, **{"客户编码": "RT001-DMZ", "箱号": "BOX1", "班列号": "WB1",
                  "状态": "退舱", "开票子公司名称": "太平洋"})
    diff = me.build_diff(conn, [nrow("RT001-DMZ", train="WB1", box="BOX2")])
    assert not [a for a in diff["alerts"] if a["type"] == "陌生客编"]
    assert sum(len(g["rows"]) for g in diff["imports"]) == 1


def test_t3_retired_not_counted_in_threshold(conn):
    """验收11：41 个客编中 5 个退舱 → 有效 36+1=37 ≤40 → 散舱 + 弱提示。"""
    for i, c in enumerate(codes(36, "K")):
        seed(conn, **{"客户编码": c, "箱号": f"B{i}", "班列号": "WB2", "状态": "正常"})
    for i, c in enumerate(codes(5, "R")):
        seed(conn, **{"客户编码": c, "箱号": f"RB{i}", "班列号": "WB2", "状态": "退舱"})
    diff = me.build_diff(conn, [nrow("NEW-DMZ", train="WB2", box="NB")])
    groups = {g["train_no"]: g for g in diff["imports"]}
    assert groups["WB2"]["班列类型"] == "散舱"
    assert any(a["type"] == "弱提示" for a in diff["alerts"])


def test_t3_null_status_still_matched(conn):
    """验收11：状态为 NULL 的行仍参与比对（命中库内 → 非新行）。"""
    seed(conn, **{"客户编码": "N001-DMZ", "箱号": "BOXN", "班列号": "WB3",
                  "状态": None, "开票子公司名称": "太平洋"})
    diff = me.build_diff(conn, [nrow("N001-DMZ", train="WB3", box="BOXN")])
    assert sum(len(g["rows"]) for g in diff["imports"]) == 0
    assert not [a for a in diff["alerts"] if a["type"] in ("陌生客编", "缺班列号")]


def test_t3_soft_deleted_ignored(conn):
    """软删行不参与比对（SQL 层过滤）。"""
    seed(conn, **{"客户编码": "D001-DMZ", "箱号": "BOXD", "班列号": "WB4",
                  "状态": "正常", "is_deleted": 1})
    diff = me.build_diff(conn, [nrow("D001-DMZ", train="WB4", box="BOXD2")])
    assert sum(len(g["rows"]) for g in diff["imports"]) == 1


# ---------- 改动一：专列放行 ----------

def test_t1_over_threshold_writes_dedicated(conn):
    """验收1：库内 2 + 本次 50 = 52 > 40 → 放行写专列，0 条阻断报警。"""
    seed(conn, **{"客户编码": "E1-DMZ", "箱号": "EB1", "班列号": "WB883",
                  "状态": "正常", "班列类型": "散舱"})
    seed(conn, **{"客户编码": "E2-DMZ", "箱号": "EB2", "班列号": "WB883",
                  "状态": "正常", "班列类型": "散舱"})
    rows = [nrow(c, train="WB883", box=f"NB{i}") for i, c in enumerate(codes(50, "X"))]
    diff = me.build_diff(conn, rows)
    assert not [a for a in diff["alerts"] if a["type"] == "陌生客编"]
    groups = {g["train_no"]: g for g in diff["imports"]}
    assert groups["WB883"]["班列类型"] == "专列"
    assert sum(len(g["rows"]) for g in diff["imports"]) == 50


def test_t1_under_threshold_writes_bulk_with_weak_hint(conn):
    """验收2：客编总数 ≤40 → 放行写散舱 + 黄色弱提示（不阻断）。"""
    diff = me.build_diff(conn, [nrow("S001-DMZ", train="WB9", box="SB1"),
                                nrow("S002-KOL", train="WB9", box="SB2")])
    groups = {g["train_no"]: g for g in diff["imports"]}
    assert groups["WB9"]["班列类型"] == "散舱"
    weak = [a for a in diff["alerts"] if a["type"] == "弱提示"]
    assert len(weak) == 2  # 按 班列号:code_core 聚合，一客编一条
    assert all("散舱" in a["说明"] for a in weak)
    assert all(a["key"] for a in weak)


def test_t1_weak_hint_aggregated_per_key(conn):
    """同一客编多箱/多后缀只出一条弱提示（§8.3 聚合去重）。"""
    diff = me.build_diff(conn, [nrow("M001-DMZ", train="WB9", box="B1"),
                                nrow("M001-KOL", train="WB9", box="B2")])
    weak = [a for a in diff["alerts"] if a["type"] == "弱提示"]
    assert len(weak) == 1
    assert weak[0]["key"] == "WB9:M001"


def test_t1_existing_bulk_kept(conn):
    """验收3：库内既有散舱原样保留（无针对它的更新项）。"""
    rid = seed(conn, **{"客户编码": "E1-DMZ", "箱号": "EB1", "班列号": "WB883",
                        "状态": "正常", "班列类型": "散舱"})
    rows = [nrow(c, train="WB883", box=f"NB{i}") for i, c in enumerate(codes(45, "X"))]
    diff = me.build_diff(conn, rows)
    assert all(u["record_id"] != rid for u in diff["updates"])


def test_t1_missing_train_visible_alert(conn):
    """验收4(R3b-1)：缺班列号新行 → 可见 缺班列号 alert，不静默消失；其余行正常导入。"""
    diff = me.build_diff(conn, [nrow("Z001-DMZ", train="", box="ZB1"),
                                nrow("Z002-DMZ", train="WB5", box="ZB2")])
    missing = [a for a in diff["alerts"] if a["type"] == "缺班列号"]
    assert len(missing) == 1
    assert sum(len(g["rows"]) for g in diff["imports"]) == 1


def test_t1_import_applies_train_type(conn, tmp_path, monkeypatch):
    """改动一写库：apply 按判定写 专列/散舱（废除写死专列）；留痕可查。"""
    monkeypatch.setattr(me, "BACKUP_DIR", str(tmp_path))
    diff = {"updates": [],
            "imports": [{"train_no": "WB7", "口岸": "山口", "发班时间": "2026-09-01",
                         "目的站建议": "", "班列类型": "散舱",
                         "rows": [{"客户编码": "W001-DMZ", "箱号": "WB1", "封号": "",
                                   "箱属": "SOC", "口岸": "山口", "发班时间": "2026-09-01"}]}],
            "alerts_applied": []}
    me.apply_diff(conn, diff, "毛骁洋", ["t.xlsx"])
    conn.commit()
    r = conn.execute('SELECT "班列类型" FROM records WHERE "客户编码"=?',
                     ("W001-DMZ",)).fetchone()
    assert r["班列类型"] == "散舱"


# ---------- 改动二：箱属 / 箱号封号 ----------

def test_t2_owner_change_goes_to_updates(conn):
    """验收5：箱属 SOC→COC 进更新清单，不报警。"""
    seed(conn, **{"客户编码": "O001-DMZ", "箱号": "OB1", "班列号": "WB6",
                  "箱属": "SOC", "开票子公司名称": "太平洋"})
    diff = me.build_diff(conn, [nrow("O001-DMZ", train="WB6", box="OB1", owner="COC")])
    assert not [a for a in diff["alerts"] if a["type"] == "字段冲突"]
    assert len(diff["updates"]) == 1
    ch = {c["field"]: c for c in diff["updates"][0]["changes"]}
    assert ch["箱属"]["old"] == "SOC" and ch["箱属"]["new"] == "COC"


def test_t2_owner_case_insensitive(conn):
    """验收5：coc/COC 不误判（两侧 norm_owner）。"""
    seed(conn, **{"客户编码": "O002-DMZ", "箱号": "OB2", "班列号": "WB6", "箱属": "COC",
                  "口岸": "山口", "发班时间": "2026-09-01"})
    diff = me.build_diff(conn, [nrow("O002-DMZ", train="WB6", box="OB2", owner="coc")])
    assert diff["updates"] == []
    assert diff["alerts"] == []


def test_t2_owner_backfill_goes_to_updates(conn):
    """验收10：箱属补录（库空→文件有）进更新。"""
    seed(conn, **{"客户编码": "O003-DMZ", "箱号": "OB3", "班列号": "WB6",
                  "箱属": "", "开票子公司名称": "太平洋"})
    diff = me.build_diff(conn, [nrow("O003-DMZ", train="WB6", box="OB3", owner="SOC")])
    assert len(diff["updates"]) == 1
    assert diff["updates"][0]["changes"][0]["action"] == "补"


def test_t2_box_conflict_is_confirmable(conn):
    """验收6：箱号冲突 → 类型=字段冲突，显式 field/new_value，前端可勾选。"""
    seed(conn, **{"客户编码": "B001-DMZ", "箱号": "OLD-BOX", "班列号": "WB6",
                  "封号": "S1", "箱属": "SOC"})
    diff = me.build_diff(conn, [nrow("B001-DMZ", train="WB6", box="NEW-BOX")])
    conflicts = [a for a in diff["alerts"] if a["type"] == "字段冲突"]
    assert len(conflicts) == 1
    assert conflicts[0]["field"] == "箱号"
    assert conflicts[0]["new_value"] == "NEW-BOX"


def test_t2_seal_conflict_is_confirmable(conn):
    """验收7：封号冲突同样可确认（field=封号）。"""
    seed(conn, **{"客户编码": "F001-DMZ", "箱号": "FB1", "班列号": "WB6", "封号": "OLD-S"})
    diff = me.build_diff(conn, [nrow("F001-DMZ", train="WB6", box="FB1", seal="NEW-S")])
    conflicts = [a for a in diff["alerts"] if a["type"] == "字段冲突"]
    assert len(conflicts) == 1
    assert conflicts[0]["field"] == "封号"
    assert conflicts[0]["new_value"] == "NEW-S"


def test_t2_box_and_seal_conflict_both_kept(conn):
    """验收10：同一记录箱号+封号同时冲突 → 两条 alert（键含 field 才不覆盖）。"""
    seed(conn, **{"客户编码": "BS-DMZ", "箱号": "OLD-B", "班列号": "WB6", "封号": "OLD-S"})
    diff = me.build_diff(conn, [nrow("BS-DMZ", train="WB6", box="NEW-B", seal="NEW-S")])
    conflicts = [a for a in diff["alerts"] if a["type"] == "字段冲突"]
    assert {a["field"] for a in conflicts} == {"箱号", "封号"}


def test_t2_updates_carry_train_and_company(conn):
    """验收9：更新清单每行带 班列号 + 负责公司。"""
    seed(conn, **{"客户编码": "C001-DMZ", "箱号": "", "班列号": "WB6",
                  "开票子公司名称": "太平洋"})
    diff = me.build_diff(conn, [nrow("C001-DMZ", train="WB6", box="CB1")])
    assert diff["updates"][0]["班列号"] == "WB6"
    assert diff["updates"][0]["负责公司"] == "太平洋"


def test_t2_field_fix_apply_overwrites_with_log(conn, tmp_path, monkeypatch):
    """验收6/7：字段冲突确认 → 文件值覆盖 + update_log 留痕。"""
    monkeypatch.setattr(me, "BACKUP_DIR", str(tmp_path))
    rid = seed(conn, **{"客户编码": "B002-DMZ", "箱号": "OLD-B2", "班列号": "WB6"})
    diff = {"updates": [], "imports": [],
            "alerts_applied": [{"source": "field_fix", "record_id": rid,
                                "field": "箱号", "new_value": "NEW-B2"}]}
    me.apply_diff(conn, diff, "毛骁洋", ["t.xlsx"])
    conn.commit()
    r = conn.execute('SELECT "箱号" FROM records WHERE id=?', (rid,)).fetchone()
    assert r["箱号"] == "NEW-B2"
    log = conn.execute("SELECT field,old_value,new_value FROM update_log").fetchone()
    assert (log["field"], log["old_value"], log["new_value"]) == ("箱号", "OLD-B2", "NEW-B2")


def test_t2_field_fix_rejects_non_whitelist(conn, tmp_path, monkeypatch):
    """§8.3：field_fix 通道白名单服务端强制 —— 非法 field 拒绝执行。"""
    monkeypatch.setattr(me, "BACKUP_DIR", str(tmp_path))
    rid = seed(conn, **{"客户编码": "B003-DMZ", "箱号": "B3", "班列号": "WB6"})
    diff = {"updates": [], "imports": [],
            "alerts_applied": [{"source": "field_fix", "record_id": rid,
                                "field": "客户编码", "new_value": "HACK"}]}
    with pytest.raises(ValueError):
        me.apply_diff(conn, diff, "毛骁洋", ["t.xlsx"])
    r = conn.execute('SELECT "客户编码" FROM records WHERE id=?', (rid,)).fetchone()
    assert r["客户编码"] == "B003-DMZ"

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
