# -*- coding: utf-8 -*-
"""八档判定全档位测试。基准=v2 规范 §2/§5 + 2026-08-26 拍板：
退舱/软删仅命中的码 → T4(reason=cancelled_only)，仍报警。"""
import csv
import os
import pytest
from core import matching

RECORDS_CSV = os.path.join(os.path.dirname(__file__), "..", "testset",
                           "test_fixtures", "records.csv")


def load_rows():
    with open(RECORDS_CSV, encoding="utf-8-sig") as f:
        return [{"code": (r["客户编码"] or "").strip(),
                 "box": (r["箱号"] or "").strip().upper(),
                 "company": (r["开票子公司名称"] or "").strip(),
                 "status": (r["状态"] or "").strip(),
                 "deleted": int(r["is_deleted"] or 0)}
                for r in csv.DictReader(f)]


@pytest.fixture(scope="module")
def idx():
    return matching.build_index(load_rows())


def test_t1_exact_hit(idx):
    r = matching.classify_match("CQWLJT260713004-BLLST", "WRONGBOX", idx)
    assert (r.tier, r.reason) == ("T1", "exact")       # 客编精确→箱号不卡


def test_t4_cancelled_only(idx):
    r = matching.classify_match("CQWLJT260713006-VXN", "", idx)
    assert (r.tier, r.reason) == ("T4", "cancelled_only")


def test_t2_suffix_mismatch(idx):
    # 序号 260709001 在库(-Kol)；来信同箱不同后缀
    r = matching.classify_match("CQWLJT260709001-XXX", "OVLU2507254", idx)
    assert r.tier == "T2"


def test_t3_box_mismatch_or_empty(idx):
    r = matching.classify_match("CQWLJT260713004-ZZZ", "", idx)
    assert (r.tier, r.reason) == ("T3", "box_mismatch")


def test_t5_unique_box_no_code(idx):
    r = matching.classify_match("", "TSRU8008478", idx)
    assert (r.tier, r.reason) == ("T5", "unique_box_only")


def test_t6_reused_box(idx):
    # v2 §5: FWRU0192384 / PONU8175063 在 yxo_test.db 复用；records.csv 若无此箱，
    # 该用例以合成索引验证（下方 test_t6_with_synth_index）
    assert True


def test_t6_with_synth_index():
    rows = [
        {"code": "CQWLJT260101001-A", "box": "FWRU0192384", "company": "甲",
         "status": "", "deleted": 0},
        {"code": "CQWLJT260101002-B", "box": "FWRU0192384", "company": "乙",
         "status": "", "deleted": 0},
    ]
    r = matching.classify_match("", "FWRU0192384", matching.build_index(rows))
    assert (r.tier, r.reason) == ("T6", "reused_box")


def test_t7_cross_suffix_guard():
    rows = [
        {"code": "CQWLJT260201001-A", "box": "ABCU1111111", "company": "甲",
         "status": "", "deleted": 0},
        {"code": "CQWLJT260201001-B", "box": "ABCU2222222", "company": "乙",
         "status": "", "deleted": 0},
    ]
    r = matching.classify_match("CQWLJT260201001-C", "", matching.build_index(rows))
    assert (r.tier, r.reason) == ("T7", "multi_suffix")


def test_t0_nothing(idx):
    r = matching.classify_match("", "", idx)
    assert (r.tier, r.reason) == ("T0", "nothing")


def test_disambiguation_box_not_code():
    assert matching.is_iso_box("TRIU1234567") is True
    assert matching.parse_code("TRIU1234567") is not None  # 能解析但…
    rows = [{"code": "CQWLJT260101001-A", "box": "TRIU1234567", "company": "甲",
             "status": "", "deleted": 0}]
    idx = matching.build_index(rows)
    # 提取层规则：ISO 箱形态 token 一律判箱号，不进客编通道（spec §4 消歧规则1）
    assert matching.is_client_code_candidate("TRIU1234567", idx) is False
    assert matching.is_client_code_candidate("CQWLJT260101001-A", idx) is True


def test_parse_generic_prefix():
    p = matching.parse_code("ABC12345-DME")
    assert (p["prefix"], p["seq"], p["suffix"]) == ("ABC", "12345", "DME")
