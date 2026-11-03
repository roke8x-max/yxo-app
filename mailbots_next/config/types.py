import re
from enum import Enum, StrEnum
from dataclasses import dataclass
from typing import List, Dict, Any


class EmailType(str, Enum):
    DRAFT = "draft"
    WAYBILL = "waybill"
    TRACING = "tracing"
    DSK = "dsk"
    ATB = "atb"
    RELEASE = "release"
    RELEASE_CANCEL = "release_cancel"
    RETBOX = "retbox"


class DraftCategory(StrEnum):
    """草单分类。⚠️ 值一律保持历史字符串（A/B/C1/C2/OTHER），不要改值 ——
    老系统台账 (draft_forward_ledger.processed_mails.category 271 行) 与
    pending_queue.category (11 行) 里存的就是这些字符串，改值会造成跨系统对照困难。"""
    NEW = "A"                     # 新草单（原 A）
    UPDATE = "B"                  # ⚠️ 历史值：新系统不再产生（恒判 A），保留仅为识别老库历史数据
    UPSTREAM_FEEDBACK = "C1"      # 上游问题反馈（原 C1）
    EXTERNAL_REPLY = "C2"         # 外部回复/确认（原 C2）
    OTHER = "OTHER"               # ⚠️ 历史值：新系统不再产生


EXTRACTION_KEYS: Dict[EmailType, List[str]] = {
    EmailType.DRAFT: ["customer_code", "container_no"],
    EmailType.WAYBILL: ["customer_code", "container_no"],
    EmailType.TRACING: ["train_no", "container_no"],
    EmailType.DSK: ["container_no"],
    EmailType.ATB: ["container_no"],
    EmailType.RELEASE: ["customer_code", "release_no", "code_no"],
    EmailType.RELEASE_CANCEL: ["customer_code", "release_no", "code_no"],
    EmailType.RETBOX: ["customer_code", "container_no", "train_no"],
}

TYPE_ROUTES = [
    {
        "type": EmailType.WAYBILL,
        "enabled": True,
        "priority": 10,
        "match": {
            "sender_whitelist": ["docwbfb@yxologistics.com"],
        },
    },
    {
        "type": EmailType.ATB,
        "enabled": True,
        "priority": 20,
        "match": {
            "sender_whitelist": ["atb@yxologistics.com"],
        },
    },
    {
        "type": EmailType.DSK,
        "enabled": True,
        "priority": 30,
        "match": {
            "sender_whitelist": ["kasa@rtsb.de", "reex.mala@deutschebahn.com"],
        },
    },
    {
        "type": EmailType.TRACING,
        "enabled": True,
        "priority": 40,
        "match": {
            "sender_whitelist": ["tracing-system@yxologistics.com"],
        },
    },
    {
        "type": EmailType.DRAFT,
        "enabled": True,
        "priority": 50,
        "match": {
            "attachment_pattern": r"已加密|箱号",
            "sender_exclude": [
                "docwbfb@yxologistics.com",
                "atb@yxologistics.com",
                "kasa@rtsb.de",
                "reex.mala@deutschebahn.com",
                "tracing-system@yxologistics.com",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# 放箱令 / 取消放箱令 / 还箱令 —— 第一阶段只做识别与提取，规则**停用**合入。
# `enabled: False` 时 _identify_type() 直接跳过，现有 5 类行为完全不变。
# 必须插到列表最前面：_identify_type() 是按列表顺序遍历、命中即停，
# 而「取消放箱令」的主题同样含「放箱」，排在 release 之后会被它先吃掉。
# priority 运行时无人读取，但 tests/test_core.py 会断言整体升序，故必须带 5/6/7。
# ---------------------------------------------------------------------------
_TYPE_ROUTES_NEW = [
    {
        "type": EmailType.RELEASE_CANCEL,
        "enabled": False,
        "priority": 5,
        "match": {
            "sender_whitelist": ["shidong@yxologistics.com"],
            "subject_pattern": r"取消放箱令",
        },
    },
    {
        "type": EmailType.RELEASE,
        "enabled": False,
        "priority": 6,
        "match": {
            "sender_whitelist": ["shidong@yxologistics.com"],
            "subject_pattern": r"放箱",
            "subject_exclude": r"取消",
        },
    },
    {
        "type": EmailType.RETBOX,
        "enabled": False,
        "priority": 7,
        "match": {
            "sender_whitelist": ["luohua@yxologistics.com", "735274962@qq.com"],
            "subject_pattern": r"还箱令",
        },
    },
]

TYPE_ROUTES = _TYPE_ROUTES_NEW + [
    r for r in TYPE_ROUTES
    if r["type"] not in (EmailType.RELEASE, EmailType.RELEASE_CANCEL, EmailType.RETBOX)
]

# 需要"留人工"的草单分类：不自动转发、不进错误队列、不标已读。
# 2026-09-23 收敛：原全量分类表（五个历史字符串，注意 W 从来不在其中）已删
# （删掉死值后零消费者，且原名会让人误以为它是"所有分类"）；
# 原三处各写一遍的留人工元组（draft.py／decide.py／serve.py）收敛到这里。
NON_AUTO_DRAFT_CATEGORIES = ("C2",)

ENC_PDF_RE = re.compile(r'^[A-Z]{4}\d{7}-\d{6}-\d{6}已加密\.pdf$', re.I)
BOX_PDF_RE = re.compile(r'^[A-Z]{4}\d{7}\.pdf$', re.I)
UPDATE_KEYWORDS = ("更新草单", "草单更新", "更新的草单", "请查收更新")
WAYBILL_REJECT_KEYWORD = "单证审核驳回"
YXO_DOMAIN = "yxologistics.com"