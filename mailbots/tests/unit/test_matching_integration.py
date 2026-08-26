# -*- coding: utf-8 -*-
"""集成抽查：真实库快照上重放关键拍板结论。"""
import sqlite3
import os
from core import matching

DB = os.path.join(os.path.dirname(__file__), "..", "data", "yxo_test.db")


def _idx_from_db():
    conn = sqlite3.connect("file:{}?mode=ro".format(DB.replace("\\", "/")), uri=True)
    conn.row_factory = sqlite3.Row
    rows = [{"code": r["客户编码"], "box": r["箱号"],
             "company": r["开票子公司名称"], "status": r["状态"],
             "deleted": r["is_deleted"] or 0}
            for r in conn.execute("SELECT 客户编码,箱号,开票子公司名称,状态,is_deleted FROM records")]
    conn.close()
    return matching.build_index(rows)


def test_yxo_test_db_vxn_cancelled_t4():
    idx = _idx_from_db()
    # 真实库中该码退舱且同序号无 active 记录 → T4(cancelled_only)，仍报警
    r = matching.classify_match("CQWLJT260708001-VXN", "", idx)
    assert (r.tier, r.reason) == ("T4", "cancelled_only")


def test_yxo_test_db_seq_sibling_active_t3():
    # CQWLJT260718008-VXN 虽退舱，但同序号存在 active 的 -SPB：
    # 按 spec §4 判定顺序 T2/T7/T3 先于全量兜底 → 正确落 T3(box_mismatch)
    idx = _idx_from_db()
    r = matching.classify_match("CQWLJT260718008-VXN", "", idx)
    assert (r.tier, r.reason) == ("T3", "box_mismatch")


def test_yxo_test_db_reused_boxes_t6():
    idx = _idx_from_db()
    for b in ("FWRU0192384", "PONU8175063"):
        assert matching.classify_match("", b, idx).tier == "T6"
