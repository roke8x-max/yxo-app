# -*- coding: utf-8 -*-
"""tracing_xls.py 单元测试"""
import pytest
import os
import sys
import tempfile
import io

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from mailbots.core.tracing_xls import (
    parse_tracing_xls_attachment,
    _parse_with_xlrd,
    _parse_with_openpyxl,
    _parse_as_text,
    _find_header_row,
    _find_header_row_openpyxl,
    _map_columns,
    _map_columns_openpyxl,
    _has_required_columns,
    _parse_row,
    _parse_row_openpyxl,
    _get_cell,
    _get_cell_openpyxl,
    _parse_row_openpyxl as _parse_row_openpyxl_func,
    _parse_row_text,
    _get_text_cell,
    parse_tracing_xls_attachment,
)


class TestTracingXLSParser:
    """运踪 .xls 解析器测试"""

    def test_parse_tracing_xls_attachment_empty(self):
        """测试空字节"""
        from mailbots.core.tracing_xls import parse_tracing_xls_attachment
        result = parse_tracing_xls_attachment(b"")
        assert result == []

    def test_parse_tracing_xls_attachment_invalid(self):
        """测试无效数据"""
        from mailbots.core.tracing_xls import parse_tracing_xls_attachment
        result = parse_tracing_xls_attachment(b"invalid data")
        assert result == []


class TestTracingXLSHelpers:
    """辅助函数测试"""

    def test_has_required_columns(self):
        from mailbots.core.tracing_xls import _has_required_columns
        assert _has_required_columns({"container_no": 0}) is True
        assert _has_required_columns({"container_no_alt": 0}) is True
        assert _has_required_columns({"other": 0}) is False

    def test_map_columns(self):
        from mailbots.core.tracing_xls import _map_columns
        # 创建 mock sheet
        class MockSheet:
            def __init__(self):
                self.ncols = 5
                self.values = ["箱号", "箱型", "中方车号", "箱号", "目的站"]
            def cell_value(self, row, col):
                return self.values[col]
            def __init__(self):
                self.ncols = 5
                self.values = ["箱号", "箱型", "中方车号", "箱号", "目的站"]
        
        sheet = MockSheet()
        col_map = _map_columns(sheet, 0)
        assert "container_no" in col_map

    def test_has_required_columns(self):
        from mailbots.core.tracing_xls import _has_required_columns
        assert _has_required_columns({"container_no": 0}) is True
        assert _has_required_columns({"container_no_alt": 0}) is True
        assert _has_required_columns({"other": 0}) is False


class TestParseTracingXLS:
    """parse_tracing_xls_attachment 集成测试"""

    def test_parse_empty(self):
        from mailbots.core.tracing_xls import parse_tracing_xls_attachment
        result = parse_tracing_xls_attachment(b"")
        assert result == []

    def test_parse_invalid(self):
        from mailbots.core.tracing_xls import parse_tracing_xls_attachment
        result = parse_tracing_xls_attachment(b"invalid data")
        assert result == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])