# -*- coding: utf-8 -*-
"""robot_base.py 单元测试"""
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, Mock
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from mailbots.core.robot_base import BaseMailBot, MailEvent


class TestBaseMailBot:
    """BaseMailBot 基础功能测试"""

    def test_mail_event_creation(self):
        """测试 MailEvent 创建"""
        from mailbots.core.robot_base import MailEvent
        ev = MailEvent(
            account="test@cqtransit.com",
            folder="草单运单号",
            message_id="<test@msg>",
            uid=123,
            subject="测试草单",
            sender="test@cqtransit.com",
            date_hdr="Thu, 26 Aug 2026 10:00:00 +0800",
            raw_bytes=b"raw",
            eml_path="/tmp/test.eml"
        )
        assert ev.account == "test@cqtransit.com"
        assert ev.folder == "草单运单号"
        assert ev.message_id == "<test@msg>"
        assert ev.uid == 123
        assert ev.subject == "测试草单"
        assert ev.sender == "test@cqtransit.com"
        assert ev.eml_path == "/tmp/test.eml"

    def test_mail_event_sender_domain(self):
        """测试 sender_domain 属性"""
        from mailbots.core.robot_base import MailEvent
        ev = MailEvent(
            account="test@cqtransit.com",
            folder="草单运单号",
            message_id="<test@msg>",
            uid=123,
            subject="测试草单",
            sender="test@cqtransit.com",
            date_hdr="Thu, 26 Aug 2026 10:00:00 +0800",
            raw_bytes=b"raw",
        )
        assert ev.sender_domain == "cqtransit.com"

    def test_base_mailbot_abstract(self):
        """测试 BaseMailBot 抽象基类"""
        from mailbots.core.robot_base import BaseMailBot
        # BaseMailBot 是抽象类，不能直接实例化
        from mailbots.core.robot_base import BaseMailBot
        import inspect
        assert inspect.isabstract(BaseMailBot)
        assert hasattr(BaseMailBot, "run")
        assert hasattr(BaseMailBot, "_scan_mailbox")
        assert hasattr(BaseMailBot, "_process_email")
        assert hasattr(BaseMailBot, "_should_process")
        assert hasattr(BaseMailBot, "_forward_email")


class TestRobotBaseUtils:
    """robot_base 工具函数测试"""

    def test_decode_header_safe(self):
        from mailbots.core.robot_base import decode_header_safe
        assert decode_header_safe("") == ""
        assert decode_header_safe("测试主题") == "测试主题"
        # 测试编码头
        from email.header import Header
        encoded = Header("测试主题", "utf-8").encode()
        assert decode_header_safe(encoded) == "测试主题"

    def test_decode_any(self):
        from mailbots.core.robot_base import decode_any
        assert decode_any("") == ""
        assert decode_any("测试") == "测试"
        # 测试 bytes 解码
        from email.header import Header
        encoded = Header("测试", "utf-8").encode()
        assert decode_any(encoded) == "测试"

    def test_decode_any_str(self):
        from mailbots.core.robot_base import decode_any
        assert decode_any("") == ""
        assert decode_any("测试") == "测试"

    def test_attachment_names(self):
        from mailbots.core.robot_base import attachment_names
        import email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.application import MIMEApplication

        msg = email.mime.multipart.MIMEMultipart()
        msg["Subject"] = "test"
        msg["From"] = "test@test.com"
        msg["Message-ID"] = "<test@msg>"
        
        # 添加附件
        from email.mime.application import MIMEApplication
        part = MIMEApplication(b"test content")
        part.add_header("Content-Disposition", "attachment", filename="test.pdf")
        msg = email.mime.multipart.MIMEMultipart()
        msg.attach(part)
        
        names = attachment_names(msg)
        assert "test.pdf" in names

    def test_extract_body_text(self):
        from mailbots.core.robot_base import extract_body_text
        import email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = email.mime.multipart.MIMEMultipart()
        msg["Subject"] = "test"
        msg.attach(MIMEText("plain text body", "plain", "utf-8"))
        msg.attach(MIMEText("<html><body>html body</body></html>", "html", "utf-8"))

        plain, html = extract_body_text(msg)
        assert "plain text body" in plain
        assert "html body" in html

    def test_decode_any(self):
        from mailbots.core.robot_base import decode_any
        assert decode_any("") == ""
        assert decode_any("测试") == "测试"
        # 测试 bytes
        from email.header import Header
        encoded = Header("测试", "utf-8").encode()
        assert decode_any(encoded) == "测试"

    def test_decode_any_str(self):
        from mailbots.core.robot_base import decode_any
        assert decode_any("") == ""
        assert decode_any("测试") == "测试"

    def test_decode_any_bytes(self):
        from mailbots.core.robot_base import decode_any
        from email.header import Header
        encoded = Header("测试", "utf-8").encode()
        assert decode_any(encoded) == "测试"


class TestRobotBaseIntegration:
    """robot_base 集成测试（需要 mock）"""

    @pytest.fixture
    def mock_ctx(self):
        from types import SimpleNamespace
        ctx = SimpleNamespace()
        ctx.live = False
        ctx.account = "test@cqtransit.com"
        ctx.accounts = {"test@cqtransit.com": "pwd"}
        ctx.accounts_dict = {"test@cqtransit.com": "pwd"}
        ctx.smtp = ("smtp.test", 465)
        ctx.ADMIN_NAME = "毛骁洋"
        ctx.ADMIN_MAILBOX = "maoxiaoyang@cqtransit.com"
        return SimpleNamespace(
            live=False,
            account="test@cqtransit.com",
            accounts={"test@cqtransit.com": "pwd"},
            accounts_dict={"test@cqtransit.com": "pwd"},
            smtp=("smtp.test", 465),
            ADMIN_NAME="毛骁洋",
            ADMIN_MAILBOX="maoxiaoyang@cqtransit.com",
        )

    def test_base_mailbot_init(self, mock_ctx):
        from mailbots.core.robot_base import BaseMailBot
        # BaseMailBot 是抽象类，不能直接实例化
        from mailbots.core.robot_base import BaseMailBot
        import inspect
        assert inspect.isabstract(BaseMailBot)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])