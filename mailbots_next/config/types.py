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

DRAFT_CATEGORIES = ["A", "B", "C1", "C2", "OTHER"]

ENC_PDF_RE = re.compile(r'^[A-Z]{4}\d{7}-\d{6}-\d{6}已加密\.pdf$', re.I)
BOX_PDF_RE = re.compile(r'^[A-Z]{4}\d{7}\.pdf$', re.I)
UPDATE_KEYWORDS = ("更新草单", "草单更新", "更新的草单", "请查收更新")
YXO_DOMAIN = "yxologistics.com"