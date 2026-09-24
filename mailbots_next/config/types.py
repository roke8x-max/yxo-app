import re
from enum import Enum
from dataclasses import dataclass
from typing import List, Dict, Any


class EmailType(str, Enum):
    DRAFT = "draft"
    WAYBILL = "waybill"
    TRACING = "tracing"
    DSK = "dsk"
    ATB = "atb"


EXTRACTION_KEYS: Dict[EmailType, List[str]] = {
    EmailType.DRAFT: ["customer_code", "container_no"],
    EmailType.WAYBILL: ["customer_code", "container_no"],
    EmailType.TRACING: ["train_no", "container_no"],
    EmailType.DSK: ["container_no"],
    EmailType.ATB: ["container_no"],
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