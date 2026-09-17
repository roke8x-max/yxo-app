"""转发层修复回归锁（spec 2026-09-17 v2.2：运单号正文表格 + 附件后缀口径 + 重写回退命名）。

只测转发层（act.py 的正文/附件/重写），不碰收信层。
"""
import email
import html as html_mod
import io
import os
import re
import unicodedata
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

import mailbots_next.core.act as act_mod
from mailbots_next.core.act import rewrite_xls_filtered, split_waybill_by_company


# ---------- local helpers ----------

def _disp(s):
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _make_orig(filename, payload, subject="运单号下发"):
    orig = MIMEMultipart()
    orig["Subject"] = subject
    orig.attach(MIMEText("body", "plain"))
    att = MIMEBase("application", "octet-stream")
    att.set_payload(payload)
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", "attachment", filename=filename)
    orig.attach(att)
    return email.message_from_bytes(orig.as_bytes())


def _make_orig_multi(files, subject="运单号下发"):
    orig = MIMEMultipart()
    orig["Subject"] = subject
    orig.attach(MIMEText("body", "plain"))
    for filename, payload in files:
        att = MIMEBase("application", "octet-stream")
        att.set_payload(payload)
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename=filename)
        orig.attach(att)
    return email.message_from_bytes(orig.as_bytes())


def _split_one(filename, payload, rows, routes=None):
    msg = _make_orig(filename, payload)
    routes = routes or {"C1": (["a@t.com"], [])}
    parts = split_waybill_by_company(msg, rows, routes)
    assert len(parts) == 1
    return parts[0]["msg"]


def _rows_c1():
    return [{"客户编码": "CQWLJT260914003-XLYJN", "箱号": "FWRU0243394",
             "运单号": "38273225", "company": "C1"}]


def _alternative(msg):
    for part in msg.get_payload() or []:
        if part.get_content_type() == "multipart/alternative":
            return part
    return None


def _plain_html(msg):
    alt = _alternative(msg)
    assert alt is not None
    subs = alt.get_payload() or []
    out = {}
    for p in subs:
        ct = p.get_content_type()
        raw = p.get_payload(decode=True) or b""
        out[ct] = raw.decode(p.get_content_charset() or "utf-8", errors="replace")
    return alt, subs, out


def _attachments(msg):
    atts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            atts.append((part.get_filename(), part.get_payload(decode=True)))
    return atts


def _xlsx_bytes(rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["客户编码", "箱号", "运单号"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xls_bytes(rows):
    import xlwt
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    for c, h in enumerate(["客户编码", "箱号", "运单号"]):
        ws.write(0, c, h)
    for ri, r in enumerate(rows, start=1):
        for c, v in enumerate(r):
            ws.write(ri, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _keep(rows):
    return [{"客户编码": c, "箱号": b, "运单号": w} for c, b, w in rows]


DATA2 = [["CQWLJT260810001-SPB", "CICU1000001", "RWB1"],
         ["CQWLJT260810002-VXN", "CICU1000002", "RWB2"]]


# ---------- 4.1 正文结构（F1） ----------

def test_01_top_is_mixed_with_alternative():
    out = _split_one("x.xls", _xls_bytes(DATA2), [
        {"客户编码": "CQWLJT260810001-SPB", "箱号": "CICU1000001",
         "运单号": "RWB1", "company": "C1"}])
    assert out.get_content_type() == "multipart/mixed"
    assert _alternative(out) is not None


def test_02_alternative_has_plain_then_html():
    out = _split_one("x.xls", _xls_bytes(DATA2), _rows_c1())
    alt, subs, _ = _plain_html(out)
    assert [p.get_content_type() for p in subs] == ["text/plain", "text/html"]


def test_03_plain_has_no_markdown_separator():
    out = _split_one("x.xls", _xls_bytes(DATA2), _rows_c1())
    _, _, bodies = _plain_html(out)
    assert "--- | ---" not in bodies["text/plain"]


def test_04_plain_has_no_pipe():
    out = _split_one("x.xls", _xls_bytes(DATA2), _rows_c1())
    _, _, bodies = _plain_html(out)
    assert bodies["text/plain"].count("|") == 0


def test_05_html_has_real_table():
    out = _split_one("x.xls", _xls_bytes(DATA2), _rows_c1())
    _, _, bodies = _plain_html(out)
    h = bodies["text/html"]
    assert "<table" in h and "<tr" in h and "<th" in h
    assert h.count("<th") >= 3


def test_06_html_styles_inline_only():
    out = _split_one("x.xls", _xls_bytes(DATA2), _rows_c1())
    _, _, bodies = _plain_html(out)
    h = bodies["text/html"]
    assert "<style" not in h and "class=" not in h


def test_07_html_row_data_correct():
    rows = [
        {"客户编码": "CODE1", "箱号": "BOX1", "运单号": "WB1", "company": "C1"},
        {"客户编码": "CODE2", "箱号": "BOX2", "运单号": "WB2", "company": "C1"},
    ]
    out = _split_one("x.xls", _xls_bytes([["CODE1", "BOX1", "WB1"],
                                          ["CODE2", "BOX2", "WB2"]]), rows)
    _, _, bodies = _plain_html(out)
    tbody = bodies["text/html"].split("<tbody>")[1].split("</tbody>")[0]
    trs = re.findall(r"<tr>(.*?)</tr>", tbody, re.S)
    assert len(trs) == 2
    for tr, r in zip(trs, rows):
        tds = [html_mod.unescape(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        assert tds == [r["客户编码"], r["箱号"], r["运单号"]]


def test_08_html_escapes_special_chars():
    rows = [{"客户编码": "C1", "箱号": "A&<B>", "运单号": "W1", "company": "C1"}]
    out = _split_one("x.xls", _xls_bytes([["C1", "A&<B>", "W1"]]), rows)
    _, _, bodies = _plain_html(out)
    assert "A&amp;&lt;B&gt;" in bodies["text/html"]
    assert "A&<B>" not in bodies["text/html"]


def test_09_empty_waybill_shows_dash():
    rows = [{"客户编码": "C1", "箱号": "BOX1", "运单号": "", "company": "C1"}]
    out = _split_one("x.xls", _xls_bytes([["C1", "BOX1", ""]]), rows)
    _, _, bodies = _plain_html(out)
    assert ">-<" in bodies["text/html"]
    assert "-" in bodies["text/plain"]


def test_10_plain_uses_table_wide_column_widths():
    # 中文放在末列：char-index 断言要求前序列纯 ASCII（中文字符数与显示宽度不一致）。
    rows = [
        {"客户编码": "ABC123", "箱号": "BOX1", "运单号": "1", "company": "C1"},
        {"客户编码": "LONGCODE12345678", "箱号": "FWRU0243394",
         "运单号": "38273225", "company": "C1"},
        {"客户编码": "MIDCODE9988", "箱号": "TCLU999", "运单号": "重庆物流99", "company": "C1"},
    ]
    out = _split_one("x.xls", _xls_bytes([
        ["ABC123", "BOX1", "1"],
        ["LONGCODE12345678", "FWRU0243394", "38273225"],
        ["MIDCODE9988", "TCLU999", "重庆物流99"]]), rows)
    _, _, bodies = _plain_html(out)
    table_lines = bodies["text/plain"].split("本邮件仅包含贵公司相关的运单号信息：\n\n")[1].split("\n")
    assert len(table_lines) == 4  # 表头 + 3 数据行
    data_lines = table_lines[1:]
    idx = [ln.index(r["箱号"]) for ln, r in zip(data_lines, rows)]
    assert idx[0] == idx[1] == idx[2]
    widths = {_disp(ln) for ln in table_lines}
    assert len(widths) == 1


# ---------- 4.2 附件后缀（F2） ----------

def test_11_all_three_suffixes_attached():
    cases = [("x.xls", _xls_bytes(DATA2)),
             ("x.xlsx", _xlsx_bytes(DATA2)),
             ("x.xlsm", _xlsx_bytes(DATA2))]
    for fn, payload in cases:
        out = _split_one(fn, payload, [
            {"客户编码": "CQWLJT260810001-SPB", "箱号": "CICU1000001",
             "运单号": "RWB1", "company": "C1"}])
        assert len(_attachments(out)) == 1, fn


def test_12_shared_suffix_constant():
    assert hasattr(act_mod, "XLS_SUFFIXES")
    assert act_mod.XLS_SUFFIXES == (".xls", ".xlsx", ".xlsm")
    import inspect
    src = inspect.getsource(act_mod)
    assert "XLS_SUFFIXES" in src
    assert 'endswith(".xls")' not in src


def test_13_only_first_matching_attachment_kept():
    msg = _make_orig_multi([("a.xls", _xls_bytes(DATA2)), ("b.xls", _xls_bytes(DATA2))])
    routes = {"C1": (["a@t.com"], [])}
    parts = split_waybill_by_company(msg, [
        {"客户编码": "CQWLJT260810001-SPB", "箱号": "CICU1000001",
         "运单号": "RWB1", "company": "C1"}], routes)
    assert len(_attachments(parts[0]["msg"])) == 1


# ---------- 4.3 重写命名（F3） ----------

def test_14_xls_normal_path_keeps_name_and_biff():
    new_bytes, new_name = rewrite_xls_filtered(
        _xls_bytes(DATA2), "report.xls", _keep([DATA2[0]]))
    assert new_name == "report.xls"
    assert new_bytes[:4] == b"\xd0\xcf\x11\xe0"


def test_15_xlsx_keeps_name_exactly():
    new_bytes, new_name = rewrite_xls_filtered(
        _xlsx_bytes(DATA2), "x.xlsx", _keep([DATA2[0]]))
    base, _ = os.path.splitext("x.xlsx")
    assert new_name == base + ".xlsx" == "x.xlsx"
    assert new_bytes[:2] == b"PK"


def test_16_double_suffix_name_unchanged():
    new_bytes, new_name = rewrite_xls_filtered(
        _xlsx_bytes(DATA2), "x.xlsx.xlsx", _keep([DATA2[0]]))
    base, _ = os.path.splitext("x.xlsx.xlsx")
    assert new_name == base + ".xlsx" == "x.xlsx.xlsx"


def test_17_xlsm_renamed_to_xlsx_zip():
    new_bytes, new_name = rewrite_xls_filtered(
        _xlsx_bytes(DATA2), "x.xlsm", _keep([DATA2[0]]))
    base, _ = os.path.splitext("x.xlsm")
    assert new_name == base + ".xlsx" == "x.xlsx"
    assert new_bytes[:2] == b"PK"


def test_18_fallback_mislabeled_xls_renamed():
    new_bytes, new_name = rewrite_xls_filtered(
        _xlsx_bytes(DATA2), "x.xls", _keep([DATA2[0]]))
    base, _ = os.path.splitext("x.xls")
    assert new_name == base + ".xlsx" == "x.xlsx"
    assert new_bytes[:2] == b"PK"


def test_19_empty_keep_and_failure_return_none_none():
    assert rewrite_xls_filtered(_xlsx_bytes(DATA2), "x.xlsx", []) == (None, None)
    assert rewrite_xls_filtered(b"\x00\x01\x02not excel", "x.xls",
                                _keep([DATA2[0]])) == (None, None)


def test_20_rewrite_failure_falls_back_to_original():
    out = _split_one("bad.xls", b"\x00\x01\x02not excel", _rows_c1())
    atts = _attachments(out)
    assert len(atts) == 1
    assert atts[0][0] == "bad.xls"
    assert atts[0][1] == b"\x00\x01\x02not excel"


# ---------- F4 深色模式兼容 ----------

def test_21_html_has_no_color():
    out = _split_one("x.xls", _xls_bytes(DATA2), _rows_c1())
    _, _, bodies = _plain_html(out)
    assert bodies["text/html"].count("color:") == 0


def test_22_html_has_no_background():
    out = _split_one("x.xls", _xls_bytes(DATA2), _rows_c1())
    _, _, bodies = _plain_html(out)
    assert bodies["text/html"].count("background") == 0


def test_23_table_structure_and_borders_intact():
    out = _split_one("x.xls", _xls_bytes(DATA2), _rows_c1())
    _, _, bodies = _plain_html(out)
    h = bodies["text/html"]
    assert "<table" in h and "<th" in h and "<td" in h
    assert h.count("border: 1px solid") == 6  # 3×th + 3×td（单行数据）


def test_24_no_regression_inline_style_and_nowrap():
    out = _split_one("x.xls", _xls_bytes(DATA2), _rows_c1())
    _, _, bodies = _plain_html(out)
    h = bodies["text/html"]
    assert "<style" not in h and "class=" not in h
    assert h.count("white-space: nowrap") == 6  # 3×th + 3×td（单行数据）


def test_25_plain_part_unaffected():
    rows = [
        {"客户编码": "ABC123", "箱号": "BOX1", "运单号": "1", "company": "C1"},
        {"客户编码": "LONGCODE12345678", "箱号": "FWRU0243394",
         "运单号": "38273225", "company": "C1"},
        {"客户编码": "MIDCODE9988", "箱号": "TCLU999", "运单号": "重庆物流99", "company": "C1"},
    ]
    out = _split_one("x.xls", _xls_bytes([
        ["ABC123", "BOX1", "1"],
        ["LONGCODE12345678", "FWRU0243394", "38273225"],
        ["MIDCODE9988", "TCLU999", "重庆物流99"]]), rows)
    _, _, bodies = _plain_html(out)
    plain = bodies["text/plain"]
    assert "|" not in plain
    assert "--- | ---" not in plain
    table_lines = plain.split("本邮件仅包含贵公司相关的运单号信息：\n\n")[1].split("\n")
    assert len(table_lines) == 4
    idx = [ln.index(r["箱号"]) for ln, r in zip(table_lines[1:], rows)]
    assert idx[0] == idx[1] == idx[2]
    assert len({_disp(ln) for ln in table_lines}) == 1
