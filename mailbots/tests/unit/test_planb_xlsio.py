# -*- coding: utf-8 -*-
"""xlsio 用真实夹具做端到端解析/重写验证。"""
import email
import os
from email import policy
from processors import xlsio

FIX = os.path.join(os.path.dirname(__file__), "..", "testset", "test_fixtures")


def _att_bytes(eml_name, needle=".xls"):
    with open(os.path.join(FIX, "waybill", eml_name), "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    for part in msg.walk():
        fn = part.get_filename() or ""
        if needle in fn.lower():
            return part.get_payload(decode=True), fn
    return None, None


def test_parse_w06_rows():
    raw, name = _att_bytes("W06_multirow_mixed.eml")
    assert raw, "夹具应含 xls 附件"
    rows = xlsio.parse_waybill_xls(raw)
    assert len(rows) >= 2
    assert set(rows[0]) == {"客户编码", "箱号", "运单号"}


def test_rewrite_keeps_only_requested(tmp_path):
    raw, name = _att_bytes("W06_multirow_mixed.eml")
    rows = xlsio.parse_waybill_xls(raw)
    keep = rows[:1]
    out, out_name = xlsio.rewrite_xls_filtered(raw, name, keep)
    assert out and out_name
    back = xlsio.parse_waybill_xls(out)
    assert len(back) == 1
    assert back[0]["客户编码"] == keep[0]["客户编码"]
