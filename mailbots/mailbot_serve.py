# -*- coding: utf-8 -*-
"""mailbot_serve.py —— IMAP IDLE 长驻邮件转发服务（计划 B 刀 11）

单进程常驻，监听 IMAP IDLE 实时接收邮件，按 Processor 分发处理：
- DraftProcessor 处理草单（A/B/C1/C2/W 分类、转发/待办/报警/记录）
- WaybillProcessor 处理运单号（WAY_A 行级拆分、WAY_B 忽略、留底表）

双模式并行：
- FOLDERS_DEFAULT: ["草单运单号"] (IMAP IDLE)
- FOLDERS_TRANSITION: ["运单号", "运单草单"] (过渡期并行监听，Merged后可关闭)
- env MERGED_FOLDER_ONLY=1 时剔除旧文件夹

运行模式：
- --live  (默认 False): TEST 干跑，只打印不发送/不标已读
- --poll-secs N: 轮询降级间隔(秒)，>0 时禁用 IDLE 改用定时轮询
- --once: 单轮扫描即退出，供冒烟测试
"""
# -*- coding: utf-8 -*-
import argparse
import email
import email.policy
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace
from typing import Callable, List, Optional, Tuple

from core import events_store
from core import identity
from core import matching
from core import notify
from core import paths
from core import routing
from core import sending
from core.imap_fetcher import Idler, utf7_encode, MailState, fetch_new
from core.routing import RecordIndexProvider
from processors.draft import DraftProcessor
from processors.waybill import WaybillProcessor

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
_log = logging.getLogger(__name__)

# IMAP 文件夹名（UTF-7 编码）
FOLDERS_DEFAULT = [("草单运单号", utf7_encode("草单运单号"))]

# 过渡期双模式：旧文件夹同步监听，邮箱规则切换完成后可用 MERGED_FOLDER_ONLY=1 关闭
FOLDERS_TRANSITION = [
    ("&j9BTVVP3-", "运单号"),        # "运单号" 的 UTF-7 编码
    ("&j9BTVYNJU1U-", "运单草单"),    # "运单草单" 的 UTF-7 编码
]

ADMIN_NAME = "毛骁洋"
ADMIN_MAILBOX = "maoxiaoyang@cqtransit.com"


def decode_any(raw: bytes) -> str:
    """解码邮件头，兼容多种编码"""
    if not raw:
        return ""
    try:
        from email.header import decode_header
        parts = []
        for part, enc in email.header.decode_header(raw):
            if isinstance(part, bytes):
                out = part.decode(enc or "utf-8", errors="replace")
            else:
                out = str(part)
            out = out.strip()
        return out
    except Exception:
        return ""


class MailEvent:
    """邮件事件，封装 IMAP 解析后的关键字段"""
    __slots__ = (
        "account", "folder", "message_id", "uid", "subject",
        "sender", "date_hdr", "raw_bytes", "eml_path"
    )

    def __init__(
        self,
        account: str,
        folder: str,
        message_id: str,
        uid: int,
        subject: str,
        sender: str,
        date_hdr: str,
        raw_bytes: bytes,
        eml_path: str = ""
    ):
        self.account = account
        self.folder = folder
        self.message_id = message_id
        self.uid = uid
        self.subject = subject
        self.sender = sender
        self.date_hdr = date_hdr
        self.raw_bytes = raw_bytes
        self.eml_path = eml_path


class MailbotServe:
    """MailBot 服务主类：组装依赖、启动 IDLE 监听、分发邮件到 Processor"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.idlers: List[Idler] = []
        self._stop_event = threading.Event()
        self._idlers_lock = threading.Lock()

    @staticmethod
    def build_context(live: bool = False) -> SimpleNamespace:
        """组装运行上下文：连接、配置、Processor 注册表等"""
        # 1. 启动自检
        paths.ensure_startup_checks()

        # 2. 连接与 Schema
        events_db = paths.events_db_path()
        conn_events = events_store.connect(events_db)
        events_store.ensure_schema(conn_events)

        # yxo.db 只读连接（路由查询）
        yxo_db_path = __import__("core.paths", fromlist=["yxo_db_path"]).yxo_db_path()
        yxo_ro_conn = __import__("sqlite3").connect(
            f"file:{yxo_db_path}?mode=ro", uri=True
        )
        yxo_ro_conn.row_factory = __import__("sqlite3").Row

        # 3. 账号与 SMTP
        accounts = __import__("core.paths", fromlist=["load_accounts"]).load_accounts()
        smtp_host, smtp_port = __import__("core.paths", fromlist=["smtp_endpoint"]).smtp_endpoint()

        # 4. 索引提供器（缓存失效机制）
        yxo_db_path_full = __import__("core.paths", fromlist=["yxo_db_path"]).yxo_db_path()
        idx_provider = RecordIndexProvider(yxo_db_path_full, ttl_seconds=300, ro=True)

        # 4.1 匹配索引（初始构建）
        idx = matching.build_index(
            r for r in __import__("sqlite3").connect(
                f"file:{__import__('core.paths', fromlist=['yxo_db_path']).yxo_db_path()}?mode=ro",
                uri=True
            ).execute(
                "SELECT 客户编码, 箱号, 开票子公司名称, 状态, is_deleted FROM records"
            )
        )

        # 5. 账号配置
        accounts_dict = __import__("core.paths", fromlist=["load_accounts"]).load_accounts()

        # 6. ADMIN
        from core.identity import real_name_of
        ADMIN_NAME = "毛骁洋"
        ADMIN_MAILBOX = "maoxiaoyang@cqtransit.com"

        # 7. 解析器注册表
        from processors.draft import DraftProcessor
        from processors.waybill import WaybillProcessor

        draft_processor = DraftProcessor(None)  # 延迟注入 ctx
        waybill_processor = WaybillProcessor(None)

        # 7.1 注入 ctx（循环引用在 process 时再填充）
        # 这里先存类，process 时动态注入 ctx

        # 路由器
        from core.routing import resolve_recipients, load_company_routes, load_train_overrides, norm_train_no

        def resolve(code: str, box: str, train_id: str = "") -> Tuple[List, Optional[str]]:
            from core.routing import resolve_recipients
            return routing.resolve_recipients(
                __import__("sqlite3").connect(
                    f"file:{__import__('core.paths', fromlist=['yxo_db_path']).yxo_db_path()}?mode=ro",
                    uri=True
                ),
                None, "draft", code=code, box=box, train_id=train_id
            )

        return SimpleNamespace(
            live=False,  # 默认 TEST 模式，外部再覆盖
            accounts={},
            smtp_host="",
            smtp_port=0,
            conn_events=None,
            yxo_ro_conn=None,
            idx_provider=None,
            idx=None,
            accounts_dict={},
            smtp=("smtp.qiye.aliyun.com", 465),
            ADMIN_NAME="毛骁洋",
            ADMIN_MAILBOX="maoxiaoyang@cqtransit.com",
            resolve=resolve,
            send=None,      # 由外部注入
            add_pending=None,
            alarm=None,
            notify=None,
            release=None,
        )

    def _inject_processor_ctx(self, live: bool):
        """向 Processor 注入运行时 ctx"""
        # 邮件发送函数
        def send_mail(msg, sender_email, sender_pwd, to_list, cc_list=None):
            from core import sending
            return sending.send_smtp(
                msg, self.ctx.accounts.get(self.ctx.sender_email, ""),
                self.ctx.accounts.get(self.ctx.sender_email, ""),
                to_list, cc_list
            )

        def add_pending(info, raw, test, simulated):
            if hasattr(self.ctx, 'add_pending') and self.ctx.add_pending:
                return self.ctx.add_pending(info, raw, test, simulated)
            # fallback
            from core import events_store
            events_store.seen_seq_add(self.ctx.conn_events, "", "")
            return 1

        def alarm(names, reason, text):
            if hasattr(self.ctx, 'alarm') and self.ctx.alarm:
                self.ctx.alarm(names, reason, text)

        def notify(name, text):
            if hasattr(self.ctx, 'notify') and self.ctx.notify:
                self.ctx.notify(name, text)

        # 组装 ctx
        self.ctx.live = live
        self.ctx.send = self._send_mail
        self.ctx.add_pending = add_pending
        self.ctx.alarm = alarm
        self.ctx.notify = notify
        self.ctx.release = None  # 可选

    def _send_mail(self, msg, sender_email, sender_pwd, to_list, cc_list=None):
        from core import sending
        return sending.send_smtp(msg, sender_email, "", to_list, cc_list)

    def _on_raw(self, account: str, folder: str, uid: int, raw_bytes: bytes):
        """IMAP FETCH 回调：解析邮件 -> 去重 -> 分发到 Processor"""
        # 1. 解析邮件
        try:
            msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        except Exception as e:
            _log.warning(f"[{self.ctx.account}] 邮件解析失败 uid={uid}: {e}")
            return

        message_id = msg.get("Message-ID", "").strip()
        if not message_id:
            # 合成合成键
            import hashlib
            message_id = f"SYN::{hashlib.sha256(f'{self.ctx.account}|{folder}|{uid}|'.encode()).hexdigest()[:32]}"

        # 2. 去重：claim
        from core import dedup
        synthetic_key = f"SYN::{self.ctx.account}|{folder}|{uid}"
        if not dedup.try_claim(self.ctx.conn_events, message_id, synthetic=False):
            if not dedup.try_claim(self.ctx.conn_events, f"SYN::{self.ctx.account}|{folder}|{uid}", synthetic=True):
                _log.info(f"[{self.ctx.account}] 重复邮件 uid={uid} msg_id={message_id[:20]}")
                return

        # 3. 解析邮件基础字段
        subject = msg.get("Subject", "")
        sender = msg.get("From", "")
        date_hdr = msg.get("Date", "")

        # 3.1 解码主题/发件人
        from email.header import decode_header
        def decode_header_safe(hdr):
            if not hdr:
                return ""
            parts = []
            for part, enc in email.header.decode_header(hdr):
                if isinstance(part, bytes):
                    out = part.decode(enc or "utf-8", errors="replace")
                else:
                    out = str(part)
                return " ".join(out)

        subject = decode_header(msg.get("Subject", ""))[0][0] if msg.get("Subject") else ""
        if isinstance(subject, bytes):
            subject = subject.decode("utf-8", errors="replace")
        sender = msg.get("From", "")
        date_hdr = msg.get("Date", "")

        # 4. 构造 MailEvent
        event = MailEvent(
            account=self.ctx.account,
            folder=folder,
            message_id=message_id,
            uid=uid,
            subject=subject,
            sender=sender,
            date_hdr=date_hdr,
            raw_bytes=raw_bytes,
        )

        # 5. 去重 claim
        from core import dedup
        if not dedup.try_claim(self.ctx.conn_events, message_id, synthetic=False):
            _log.info(f"[{self.ctx.account}] 重复邮件 msg_id={message_id[:30]}")
            return

        # 5. 分发到 Processor
        try:
            # 构造 MailEvent
            from mailbots.mailbot_serve import MailEvent
            event = MailEvent(
                account=self.ctx.account,
                folder=folder,
                message_id=message_id,
                uid=uid,
                subject=subject,
                sender=sender,
                date_hdr=msg.get("Date", ""),
                raw_bytes=raw_bytes,
            )

            # 分发：按顺序尝试 Processor
            from processors.draft import DraftProcessor
            from processors.waybill import WaybillProcessor

            # 临时构造 ctx（简化：直接使用 self.ctx）
            processors = [self.draft_processor, self.waybill_processor]
            for proc in self.processors:
                if proc.can_handle(event):
                    result = proc.process(event)
                    if result.action in ("forward", "pending", "alarm"):
                        # 成功处理，保持 claim
                        pass
                    else:
                        # skip/ignored/exception -> release
                        from core import dedup
                        dedup.release(self.ctx.conn_events, message_id)
                    return

        except Exception as e:
            import traceback
            traceback.print_exc()
            # 异常时 release
            from core import dedup
            dedup.release(self.ctx.conn_events, message_id)

    def start(self, live: bool = False, poll_secs: int = 0, once: bool = False):
        """启动服务"""
        self.ctx.live = live
        self._inject_processor_ctx(live)

        # 选择文件夹
        folders = self._get_folders()

        # 创建 Idler
        from core.imap_fetcher import Idler
        account = self.ctx.account
        password = self.ctx.accounts_dict.get(self.ctx.account, "")
        if not password:
            raise RuntimeError(f"账号 {self.ctx.account} 无密码配置")

        state_path = os.path.join(paths.eml_repo_dir(), "mail_state.json")
        self.idler = Idler(
            account=self.ctx.account,
            password=password,
            folders_srv=[f[0] for f in folders],  # 服务器端文件夹名
            on_raw=self._on_raw,
            state_path=os.path.join(paths.eml_repo_dir(), "mail_state.json"),
            max_idle=1740,
            poll_fallback_secs=poll_secs,
        )
        self.idler = idler
        idler.start()

        # 如果 --once，单轮扫描后退出
        if once:
            self._run_once()
        else:
            # 阻塞主线程，等待信号
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                self.stop()

    def _get_folders(self):
        """获取监听的文件夹列表"""
        folders = FOLDERS_DEFAULT[:]
        if not os.environ.get("MERGED_FOLDER_ONLY"):
            folders.extend(FOLDERS_TRANSITION)
        return folders

    def stop(self):
        """停止服务"""
        if hasattr(self, 'idler') and self.idler:
            self.idler.stop()
            self.idler.join(timeout=10)
        _log.info("MailbotServe stopped")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="MailBot Serve - IMAP IDLE 长驻邮件转发服务")
    parser.add_argument("--live", action="store_true", help="LIVE 模式：真实发送/标已读（默认 TEST 干跑）")
    parser.add_argument("--poll-secs", type=int, default=0, help="轮询降级间隔(秒)，>0 时禁用 IDLE 改用定时轮询")
    parser.add_argument("--once", action="store_true", help="单轮扫描退出，供冒烟测试")
    parser.add_argument("--account", default="maoxiaoyang@cqtransit.com", help="IMAP 账号")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # 1. 构建上下文
    live = args.live
    poll_secs = args.poll_secs
    once = args.once

    # 初始化
    serve = MailbotServe()
    serve.ctx.account = args.account
    serve.ctx.accounts = paths.load_accounts()
    serve.ctx.smtp_host, serve.ctx.smtp_port = paths.smtp_endpoint()
    serve.ctx.accounts_dict = paths.load_accounts()

    # 启动
    serve.start(live=live, poll_secs=args.poll_secs, once=args.once)


def build_context(live: bool = False):
    """组装运行上下文（供测试和 main 使用）"""
    from types import SimpleNamespace
    import sqlite3

    # 1. 启动自检
    paths.ensure_startup_checks()

    # 2. 连接与 Schema
    events_db = paths.events_db_path()
    conn_events = events_store.connect(events_db)
    events_store.ensure_schema(conn_events)

    # yxo.db 只读连接（路由查询）
    yxo_db_path = paths.yxo_db_path()
    yxo_ro_conn = sqlite3.connect(
        f"file:{yxo_db_path}?mode=ro", uri=True
    )
    yxo_ro_conn.row_factory = sqlite3.Row

    # 3. 账号与 SMTP
    accounts = paths.load_accounts()
    smtp_host, smtp_port = paths.smtp_endpoint()

    # 4. 索引提供器（缓存失效机制）
    yxo_db_path_full = paths.yxo_db_path()
    idx_provider = RecordIndexProvider(yxo_db_path_full, ttl_seconds=300, ro=True)

    # 4.1 匹配索引（初始构建）
    conn = sqlite3.connect(
        f"file:{paths.yxo_db_path()}?mode=ro", uri=True
    )
    idx = matching.build_index(
        r for r in conn.execute(
            "SELECT 客户编码, 箱号, 开票子公司名称, 状态, is_deleted FROM records"
        )
    )

    # 5. 账号配置
    accounts_dict = paths.load_accounts()

    # 6. ADMIN
    ADMIN_NAME = "毛骁洋"
    ADMIN_MAILBOX = "maoxiaoyang@cqtransit.com"

    # 6.1 解析器注册表
    draft_processor = DraftProcessor(None)  # 延迟注入 ctx
    waybill_processor = WaybillProcessor(None)

    # 6.2 路由器
    from core.routing import resolve_recipients, load_company_routes, load_train_overrides, norm_train_no

    def resolve(code: str, box: str, train_id: str = "") -> Tuple[List, Optional[str]]:
        from core.routing import resolve_recipients
        return routing.resolve_recipients(
            sqlite3.connect(
                f"file:{paths.yxo_db_path()}?mode=ro",
                uri=True
            ),
            None, "draft", code=code, box=box, train_id=train_id
        )

    return SimpleNamespace(
        live=False,
        account="",
        accounts={},
        smtp_host="",
        smtp_port=0,
        conn_events=None,
        yxo_ro_conn=None,
        idx_provider=None,
        idx=None,
        accounts_dict={},
        smtp=("smtp.qiye.aliyun.com", 465),
        ADMIN_NAME="毛骁洋",
        ADMIN_MAILBOX="maoxiaoyang@cqtransit.com",
        resolve=resolve,
        send=None,      # 由外部注入
        add_pending=None,
        alarm=None,
        notify=None,
        release=None,
    )


if __name__ == "__main__":
    main(sys.argv[1:])