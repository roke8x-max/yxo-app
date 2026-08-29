# -*- coding: utf-8 -*-
"""Tracing_Robot_IMAP.py 单元测试"""
import pytest
import os
import sys
from unittest.mock import MagicMock, patch, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from mailbots.Tracing_Robot_IMAP import (
    parse_tracing_xls_attachment,
    resolve_train_companies,
    load_tracing_routing,
    _extract_tracing,
    _msg_text,
    run_robot,
)


def create_test_conn():
    """创建测试用的内存数据库连接"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE records(客户编码 TEXT, 箱号 TEXT, 开票子公司名称 TEXT,
                           班列号 TEXT, 状态 TEXT, is_deleted INTEGER);
      CREATE TABLE bot_config(bot TEXT, scope TEXT, key TEXT,
                               to_addrs TEXT, cc_addrs TEXT, extra TEXT);
      CREATE TABLE meta_kv(key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE tracing_log (log_id TEXT, train_no TEXT, company TEXT,
                                mail_msg_id TEXT, forward_detail TEXT, log_date TEXT, train_key TEXT);
      CREATE TABLE tracing_snapshot (train_key TEXT, box_no TEXT, node TEXT, status TEXT,
                                     event_time TEXT, source TEXT);
    """)
    conn.execute("INSERT INTO records VALUES ('CQWLJT260713004-BLLST', 'TSRU8008478', '莫斯科子公司', 'WB794', '正常', 0)")
    conn.execute("INSERT INTO bot_config VALUES ('tracking', 'company', '港九港铁', '[]', '[]', NULL)")
    conn.commit()
    return conn


@pytest.fixture
def mock_conn_factory():
    """Mock 数据库连接工厂 - 每次调用返回新连接"""
    def _create_conn():
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
          CREATE TABLE records(客户编码 TEXT, 箱号 TEXT, 开票子公司名称 TEXT,
                               班列号 TEXT, 状态 TEXT, is_deleted INTEGER);
          CREATE TABLE bot_config(bot TEXT, scope TEXT, key TEXT,
                                   to_addrs TEXT, cc_addrs TEXT, extra TEXT);
          CREATE TABLE meta_kv(key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE tracing_log (log_id TEXT, train_no TEXT, company TEXT,
                                    mail_msg_id TEXT, forward_detail TEXT, log_date TEXT, train_key TEXT);
          CREATE TABLE tracing_snapshot (train_key TEXT, box_no TEXT, node TEXT, status TEXT,
                                         event_time TEXT, source TEXT);
        """)
        conn.execute("INSERT INTO records VALUES ('CQWLJT260713004-BLLST', 'TSRU8008478', '莫斯科子公司', 'WB794', '正常', 0)")
        conn.execute("INSERT INTO records VALUES ('TEST999', 'BOX999', '莫斯科子公司', 'WB999', '正常', 0)")
        conn.execute("INSERT INTO bot_config VALUES ('tracking', 'company', '港九港铁', '[]', '[]', NULL)")
        conn.execute("INSERT INTO bot_config VALUES ('tracking', 'train', '794', '[]', '[]', '{\"companies\": [\"中欧木业\", \"沙坪坝\", \"港九港铁\"]}')")
        conn.commit()
        return conn
    return _create_conn


@pytest.fixture
def mock_db(monkeypatch, mock_conn_factory):
    """Patch get_conn to return a fresh connection each call"""
    monkeypatch.setattr('mailbots.db_write.get_conn', mock_conn_factory)
    monkeypatch.setattr('mailbots.Tracing_Robot_IMAP.get_conn', mock_conn_factory)
    return mock_conn_factory


@pytest.fixture
def mock_fs():
    """Mock 飞书客户端"""
    fs = MagicMock()
    fs.get_all_records = MagicMock(return_value=[])
    return fs


class TestParseTracingXls:
    """parse_tracing_xls_attachment 测试"""

    def test_parse_tracing_xls_attachment_empty(self):
        result = parse_tracing_xls_attachment(b"")
        assert result == []

    def test_parse_tracing_xls_attachment_invalid(self):
        result = parse_tracing_xls_attachment(b"invalid")
        assert result == []


class TestResolveTrainCompanies:
    """resolve_train_companies 测试"""

    def test_resolve_train_companies_from_records(self, mock_db):
        # 使用不存在的班列号，测试回退到 records 表查询
        companies = resolve_train_companies("999", train_companies=None)
        assert "莫斯科子公司" in companies

    def test_resolve_train_companies_override(self, mock_db):
        _, train_companies = load_tracing_routing()
        companies = resolve_train_companies("794", train_companies)
        assert "中欧木业" in companies or "沙坪坝" in companies or "港九港铁" in companies

    def test_resolve_train_companies_empty_override(self, mock_db):
        companies = resolve_train_companies("999", train_companies={"999": []})
        assert companies == []

    def test_resolve_train_companies_fallback_to_records(self, mock_db):
        companies = resolve_train_companies("999", train_companies={})
        assert "莫斯科子公司" in companies


class TestLoadTracingRouting:
    """load_tracing_routing 测试"""

    def test_load_tracing_routing(self, mock_db):
        default_map, train_companies = load_tracing_routing()
        assert isinstance(default_map, dict)
        assert isinstance(train_companies, dict)
        assert len(default_map) > 0


class TestExtractTracing:
    """_extract_tracing 测试"""

    def test_extract_tracing(self):
        text = "箱号: TSRU8008478 到达 阿拉山口 状态: 到达"
        box_no, node, status = _extract_tracing(text)
        assert box_no == "TSRU8008478"
        assert node == "阿拉山口"
        assert status == "到达"

    def test_extract_tracing_no_match(self):
        text = "随机文本无关键字"
        box_no, node, status = _extract_tracing(text)
        assert box_no is None
        assert node is None
        assert status is None


class TestMsgText:
    """_msg_text 测试"""

    def test_msg_text(self):
        import email
        msg = email.message_from_string("Subject: test\n\nbody text")
        text = _msg_text(msg)
        assert "body text" in text


class TestRunRobot:
    """run_robot 测试"""

    def test_run_robot_not_live(self, mock_db):
        with patch("mailbots.Tracing_Robot_IMAP.load_bot_config") as mock_load, \
             patch("mailbots.Tracing_Robot_IMAP.load_tracing_routing") as mock_routing:
            mock_load.return_value = {"live": False}
            mock_routing.return_value = ({}, {})
            result = run_robot()
            assert result == 0

    def test_run_robot_exception(self, mock_db):
        with patch("mailbots.Tracing_Robot_IMAP.load_bot_config") as mock_load, \
             patch("mailbots.Tracing_Robot_IMAP.load_tracing_routing") as mock_routing:
            mock_load.side_effect = Exception("config error")
            mock_routing.return_value = ({}, {})
            result = run_robot()
            assert result == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])