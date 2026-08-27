# -*- coding: utf-8 -*-
"""core 数据类（spec §3 models.py）：全量类型注解，frozen 保证跨处理器传递不被改写。"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MatchResult:
    """classify_match 的输出。tier 见 spec §4 八档；reason 为机器可读明细。"""
    tier: str
    reason: str | None
    record: dict | None
    candidates: tuple[dict, ...] = ()


@dataclass(frozen=True)
class MailEvent:
    """一封待处理邮件的元数据（raw 已落盘，处理器不回 IMAP 取件）。"""
    account: str
    folder: str
    message_id: str
    uid: str
    subject: str
    sender: str
    date_hdr: str
    eml_path: str = ""


@dataclass(frozen=True)
class RouteTarget:
    """一家负责公司的路由结果：外部联系人 To/Cc（来自 bot_config scope='company'）。"""
    company: str
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    def __post_init__(self):
        object.__setattr__(self, "to", tuple(self.to))
        object.__setattr__(self, "cc", tuple(self.cc))


@dataclass(frozen=True)
class ProcessResult:
    """处理器一次处理的结果。action 见 Global Constraints 枚举。"""
    event: MailEvent
    action: str
    tier: str | None = None
    route: tuple = ()
    detail: str = ""
    def __post_init__(self):
        object.__setattr__(self, "route", tuple(self.route))


@runtime_checkable
class Processor(Protocol):
    def can_handle(self, event: MailEvent) -> bool: ...
    def process(self, event: MailEvent) -> ProcessResult: ...
