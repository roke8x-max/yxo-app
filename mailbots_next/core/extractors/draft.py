import re
from email.message import Message
from typing import List, Dict, Any, Optional

from mailbots_next.config import (
    ENC_PDF_RE,
    BOX_PDF_RE,
    UPDATE_KEYWORDS,
    CODE_RE,
    CODE_NUM_RE,
    CONTAINER_RE,
    YXO_DOMAIN,
    DraftCategory,
    NON_AUTO_DRAFT_CATEGORIES,
)
from mailbots_next.core.extract import BaseExtractor, ExtractedRow, parse_email
from mailbots_next.core.log import get_logger

_log = get_logger(__name__)


class DraftExtractor(BaseExtractor):
    def extract(self, msg: Message, parsed: Dict[str, Any]) -> List[ExtractedRow]:
        subject = parsed["subject"] or ""
        sender = parsed["sender"] or ""
        body = parsed["plain_body"] or parsed["html_body"] or ""
        attachments = parsed["attachments"]
        att_names = [a["filename"] for a in attachments]

        code, code_num, box = self._parse_ids(subject, att_names)
        is_draft, draft_att_name = self._is_draft_attachment(att_names, body)

        if is_draft:
            # 台账已移除（2026-09-21）；恒 A = 永不误标；重新启用需按 README 技术债 X4 的三项前置单独立项。
            category = DraftCategory.NEW
        else:
            domain = sender.split("@")[-1].lower() if "@" in sender else ""
            category = DraftCategory.UPSTREAM_FEEDBACK if domain == YXO_DOMAIN else DraftCategory.EXTERNAL_REPLY

        if category in NON_AUTO_DRAFT_CATEGORIES:
            _log = __import__("mailbots_next.core.log", fromlist=["get_logger"]).get_logger(__name__)
            _log.log_manual_category(msg.get("Message-ID", "")[:50], category, f"Non-auto category: {category}")

        row = ExtractedRow(
            row_idx=0,
            email_type="draft",
            raw_data=parsed,
            customer_code=code,
            customer_code_num=code_num,
            container_no=box,
            draft_category=category,
            draft_code_num=code_num,
            is_draft_attachment=is_draft,
            draft_attachment_name=draft_att_name,
        )
        return [row]

    def _parse_ids(self, subject: str, att_names: List[str]) -> tuple:
        code = None
        box = None
        codes = CODE_RE.findall(subject)
        if codes:
            code = codes[0]
        # Use regex with word boundaries to avoid matching inside customer codes
        # Container numbers are typically standalone tokens
        container_re = re.compile(r'\b[A-Z]{4}\d{7}\b')
        boxes = container_re.findall(subject + " ")
        if boxes:
            box = boxes[0]
        if not box:
            for fn in att_names:
                m = container_re.search(fn)
                if m:
                    box = m.group(0)
                    break
        num = None
        if code:
            m = CODE_NUM_RE.match(code)
            if m:
                num = m.group(1)
        return code, num, box

    def _is_draft_attachment(self, att_names: List[str], body: str) -> tuple:
        for fn in att_names:
            if ENC_PDF_RE.match(fn.strip()):
                return True, fn
        for fn in att_names:
            if BOX_PDF_RE.match(fn.strip()) and any(k in body for k in UPDATE_KEYWORDS):
                return True, fn
        return False, None

