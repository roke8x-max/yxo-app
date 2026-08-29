# -*- coding: utf-8 -*-
"""MailBot 通用基类（spec 刀 6：短进程打补丁，统一核心层）"""
import os
import sys
import imaplib
import email
import email.utils
import smtplib
import logging
import threading
import time
import re
import json
import hashlib
from abc import ABC, abstractmethod
from typing import Callable, List, Optional, Tuple, Dict, Any
from dataclasses import dataclass
from types import SimpleNamespace
from datetime import datetime

# 核心层依赖
from core import paths
from core import identity
from core import notify
from core import sending
from core import events_store
from core import dedup
from core import matching
from core import routing

# 邮件处理
import email
import email.utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header

logger = logging.getLogger(__name__)

# ============================================================
# 通用工具函数
# ============================================================

def decode_header_safe(raw: str) -> str:
    """安全解码邮件头，兼容多种编码"""
    if not raw:
        return ""
    try:
        parts = []
        for part, enc in decode_header(raw):
            if isinstance(part, bytes):
                parts.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(str(part))
        return "".join(parts)
    except Exception:
        return ""

def decode_any(raw: str) -> str:
    """解码任意字符串，兼容多种编码"""
    if not raw:
        return ""
    try:
        parts = []
        for part, enc in decode_header(raw):
            if isinstance(part, bytes):
                parts.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(str(part))
        return "".join(parts)
    except Exception:
        return ""

def extract_body_text(msg, limit=2000) -> Tuple[str, str]:
    """提取邮件正文，返回 (plain_body, html_body)"""
    plain_body = None
    html_body = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload:
                cs = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(cs, errors="replace")
                except Exception:
                    text = payload.decode("utf-8", errors="replace")
                if ctype == "text/plain":
                    plain_body = text[:limit]
                else:
                    html_body = text[:limit]
    return plain_body, html_body


def attachment_names(msg) -> List[str]:
    """提取附件文件名列表"""
    names = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn:
            names.append(decode_any(fn))
    return names


# ============================================================
# 邮件事件数据类
# ============================================================

@dataclass
class MailEvent:
    """邮件事件数据结构"""
    account: str
    folder: str
    message_id: str
    uid: int
    subject: str
    sender: str
    date_hdr: str
    raw_bytes: bytes
    eml_path: str = ""

    @property
    def sender_domain(self) -> str:
        """提取发件人域名"""
        if "@" in self.sender:
            return self.sender.split("@")[-1].lower()
        return ""


# ============================================================
# 基础机器人抽象类
# ============================================================

class BaseMailBot(ABC):
    """MailBot 通用基类，封装 IMAP 连接、去重、路由、发送等通用逻辑"""
    
    # 子类必须定义的类属性
    BOT_NAME: str = "base"
    IMAP_SERVER: str = "imap.qiye.aliyun.com"
    IMAP_PORT: int = 993
    IMAP_FOLDER: str = "INBOX"
    SENDER_FILTER: str = ""
    BOT_LOG_DIR: str = ""
    
    # 子类可覆盖的配置
    TEST_MODE = False
    FORCE_ALL = False
    TEST_TO = "3841559246@qq.com"
    
    def __init__(self, ctx: SimpleNamespace):
        """初始化机器人上下文"""
        self.ctx = ctx
        self.account = ctx.account
        self.password = ctx.accounts.get(ctx.account, "")
        self.folder = self.IMAP_FOLDER
        
        # 从上下文获取依赖
        self.live = getattr(ctx, 'live', False)
        self.accounts = getattr(ctx, 'accounts', {})
        self.accounts_dict = getattr(ctx, 'accounts_dict', {})
        self.smtp = getattr(ctx, 'smtp', ("smtp.qiye.aliyun.com", 465))
        self.smtp_host, self.smtp_port = self.smtp
        
        # 核心依赖
        self.dedup_source = getattr(self, 'DEDUP_SOURCE', f"{self.BOT_NAME}_log")
        self.dedup = dedup
        
        # 解析器
        from core import matching
        self.matching = matching
        from core import routing
        self.routing = routing
        from core import identity
        self.identity = identity
        
        # 通知
        self.notify = notify
        self.ctx_live = getattr(ctx, 'live', False)
        
        # 发送函数
        self._send = None
        self._add_pending = None
        self._alarm = None
        self._notify = None
        self._release = None
        
        # 统计
        self.total_forwarded = 0
        self.total_skipped = 0

    @property
    def live(self) -> bool:
        return self.ctx_live

    @property
    def sender_email(self) -> str:
        return self.account

    @property
    def sender_password(self) -> str:
        return self.ctx.accounts.get(self.account, "")

    def setup_logger(self):
        """设置日志"""
        os.makedirs(self.BOT_LOG_DIR, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = os.path.join(self.BOT_LOG_DIR, f"{self.BOT_NAME.lower()}_{datetime.now().strftime('%Y-%m-%d')}.log")
        self._log_file = log_path
        
    def log(self, msg: str):
        """记录日志到文件和控制台"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        print(line)

    def _load_accounts(self):
        """加载账号配置"""
        from core import paths
        return paths.load_accounts()

    def _connect_imap(self):
        """建立 IMAP 连接"""
        conn = imaplib.IMAP4_SSL(self.IMAP_SERVER, self.IMAP_PORT, timeout=60)
        conn.login(self.account, self.password)
        return conn

    @property
    def account(self) -> str:
        return self.ctx.account

    @property
    def password(self) -> str:
        return self.ctx.accounts.get(self.account, "")

    @property
    def folder(self) -> str:
        return getattr(self, 'IMAP_FOLDER', 'INBOX')

    # ============================================================
    # 核心流程模板方法
    # ============================================================

    def run(self) -> int:
        """主运行入口"""
        self.setup_logger()
        self.log("=" * 60)
        self.log(f"{self.BOT_NAME} 邮件自动转发机器人 启动 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
        self.log("=" * 60)

        # 1. 检查开关
        try:
            from bot_config import load_bot_config
            _cfg = load_bot_config(self.BOT_NAME.lower())
            if not _cfg.get("live", True):
                self.log(f"⏸ {self.BOT_NAME} 机器人已停用(live=false)，本轮回退。")
                return 0
        except Exception as e:
            self.log(f"  ⚠ 读取开关配置失败(按运行处理): {e}")

        # 加载账号
        self.accounts = self._load_accounts()
        self.account = getattr(self, 'account', list(self.accounts.keys())[0])
        self.password = self.accounts.get(self.account, "")

        # 加载路由配置
        self._load_routing()

        # 连接去重库
        self._init_dedup()

        total_forwarded = self._run_scan()
        
        self.log("-" * 40)
        self.log(f"{self.BOT_NAME} 转发完成: 成功 {self.total_forwarded} 封 | 跳过 {self.total_skipped} 封")
        self.log("=" * 60)
        return self.total_forwarded

    def _load_routing(self):
        """加载路由配置（子类实现）"""
        pass

    def _init_dedup(self):
        """初始化去重存储"""
        if not self.TEST_MODE:
            from core import dedup
            try:
                from core import events_store
                # 确保表存在
            except:
                pass

    def _run_scan(self) -> int:
        """执行扫描循环"""
        self.total_forwarded = 0
        self.total_skipped = 0
        
        for email_addr, password in self.accounts.items():
            self.log("-" * 40)
            self.log(f"扫描账户: {email_addr}")
            
            try:
                conn = self._connect_imap()
                if not conn:
                    continue
                    
                forwarded, skipped = self._scan_mailbox(conn)
                self.total_forwarded += forwarded
                self.total_skipped += skipped
                
                conn.close()
                conn.logout()
            except Exception as e:
                self.log(f"  ❌ 账户 {email_addr} 扫描异常: {e}")
                
        return self.total_forwarded

    def _connect_imap(self):
        """建立 IMAP 连接"""
        try:
            conn = imaplib.IMAP4_SSL(self.IMAP_SERVER, self.IMAP_PORT, timeout=60)
            conn.login(self.account, self.password)
            return conn
        except Exception as e:
            self.log(f"  ❌ IMAP 登录失败: {e}")
            return None

    @abstractmethod
    def _scan_mailbox(self, conn) -> Tuple[int, int]:
        """扫描邮箱，返回 (转发数, 跳过数)"""
        pass

    def _process_email(self, conn, uid, msg, account: str, password: str) -> str:
        """处理单封邮件，返回动作结果"""
        # 解析邮件
        _, msg_data = conn.uid('FETCH', str(uid), '(RFC822)')
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        
        # 去重检查
        msg_id = msg.get("Message-ID", "").strip()
        if not msg_id:
            msg_id = f"SYN::{self.account}|{self.folder}|{uid}"
            
        if not self.dedup.try_claim(self.ctx_events, self.dedup_source, msg_id):
            self.log(f"  ⏭ 重复邮件 uid={uid} msg_id={msg_id[:30]}")
            self._mark_seen(conn, uid)
            return "duplicate"
        
        # 解析邮件
        msg_obj = email.message_from_bytes(raw)
        subject = decode_header_safe(msg.get("Subject", ""))
        sender = email.utils.parseaddr(msg.get("From", ""))[1]
        
        # 判断是否处理
        if not self._should_process(sender):
            return "filtered"
            
        # 子类实现具体处理逻辑
        result = self._process_message(msg, account)
        
        # 标记已读
        try:
            conn.uid('STORE', str(uid), '+FLAGS', '\\Seen')
        except Exception:
            pass
            
        return "processed"

    def _should_process(self, sender: str) -> bool:
        """判断是否处理该发件人"""
        if self.SENDER_FILTER:
            return sender.lower() == self.SENDER_FILTER.lower()
        return True

    def _mark_seen(self, conn, uid):
        """标记已读"""
        try:
            conn.uid('STORE', str(uid), '+FLAGS', '\\Seen')
        except Exception:
            pass

    def _mark_seen_and_commit(self, conn, uid, msg_id):
        """标记已读并记录去重"""
        try:
            conn.uid('STORE', str(uid), '+FLAGS', '\\Seen')
        except Exception:
            pass
        from core import dedup
        self.dedup.mark(self.ctx_events, self.dedup_source, msg_id)

    @abstractmethod
    def _process_message(self, msg, account: str) -> str:
        """处理邮件消息（子类实现）"""
        pass

    def _forward_email(self, raw_bytes, to_list, cc_list, sender_email, sender_pwd, subject_prefix=""):
        """转发邮件"""
        from core import sending
        msg = email.message_from_bytes(raw_bytes)
        
        # 修改主题
        if subject_prefix:
            msg = email.message_from_bytes(raw_bytes)
            msg['Subject'] = f"{msg['Subject']}"
            
        msg['From'] = sender_email
        msg['To'] = ", ".join(to_list)
        if cc_list:
            msg['Cc'] = ", ".join(cc_list)
            
        from core.sending import send_smtp
        res = send_smtp(msg, sender_email, sender_pwd, to_list, cc_list)
        return res


# ============================================================
# 邮件解析工具
# ============================================================

def parse_email(msg) -> Tuple[str, str, List[Tuple], List]:
    """解析邮件结构，返回 (html_body, plain_body, attachments, inline_parts)"""
    html_body = None
    plain_body = None
    attachments = []
    inline_parts = []
    
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = str(part.get('Content-Disposition', '')).lower()
        filename = part.get_filename()
        
        if 'attachment' in (part.get('Content-Disposition', '') or '').lower() or filename:
            attachments.append((part, filename))
        elif part.get_content_type() == 'text/html':
            payload = part.get_payload(decode=True)
            if payload:
                cs = part.get_content_charset() or 'utf-8'
                html_body = payload.decode(cs, errors='replace')
        elif part.get_content_type() == 'text/plain':
            payload = part.get_payload(decode=True)
            if payload:
                cs = part.get_content_charset() or 'utf-8'
                plain_body = payload.decode(cs, errors='replace')
    
    return html_body, plain_body, [], []


# ============================================================
# 导出
# ============================================================

__all__ = [
    'BaseMailBot',
    'MailEvent',
    'decode_header_safe',
    'decode_any',
    'attachment_names',
    'extract_body_text',
    'parse_email',
]

if __name__ == "__main__":
    print("BaseMailBot module loaded")