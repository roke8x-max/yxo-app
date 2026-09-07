"""tracing_xls 解析器的单测：正常/空/畸形 + CSV 文本回退路径。"""
import pytest
from mailbots_next.core.extractors.tracing_xls import parse_tracing_xls


def test_csv_text_fallback_returns_container():
    raw = "箱号,目的站,班列号\nCICU1234567,莫斯科,WB123".encode("utf-8")
    rows = parse_tracing_xls(raw)
    assert len(rows) == 1
    assert rows[0]["container_no"] == "CICU1234567"
    assert rows[0]["source"] == "text"


def test_empty_bytes_returns_empty():
    assert parse_tracing_xls(b"") == []


def test_invalid_bytes_returns_empty():
    assert parse_tracing_xls(b"invalid data") == []


def test_normal_xls_parses_container():
    xlwt = pytest.importorskip("xlwt")
    import io
    
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    # Use English headers to avoid xlrd/xlwt Unicode encoding issues
    # Need at least 3 rows (header + 2 data rows) for xlrd parser
    ws.write(0, 0, "container_no")
    ws.write(0, 1, "destination")
    ws.write(0, 2, "status")
    ws.write(1, 0, "CICU1234567")
    ws.write(1, 1, "Moscow")
    ws.write(1, 2, "in_transit")
    ws.write(2, 0, "HLXU8152547")
    ws.write(2, 1, "Beijing")
    ws.write(2, 2, "arrived")
    buf = io.BytesIO()
    wb.save(buf)
    xls_bytes = buf.getvalue()
    
    rows = parse_tracing_xls(xls_bytes)
    assert len(rows) >= 1
    assert any(r["container_no"] == "CICU1234567" for r in rows)