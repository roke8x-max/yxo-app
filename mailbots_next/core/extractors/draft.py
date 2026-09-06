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
    DRAFT_CATEGORIES,
)
from mailbots_next.core.extract import BaseExtractor, ExtractedRow, parse_email


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
            category = "B" if code_num and code_num in self._get_draft_nums() else "A"
        elif "运单号" in subject:
            category = "W"
        else:
            domain = sender.split("@")[-1].lower() if "@" in sender else ""
            category = "C1" if domain == YXO_DOMAIN else "C2"

        if category in ("C2", "W", "OTHER") or category not in DRAFT_CATEGORIES:
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

    def _get_draft_nums(self) -> set:
        from mailbots_next.config import DRAFT_NUMS_DB_PATH
        import sqlite3
        nums = set()
        try:
            conn = sqlite3.connect(DRAFT_NUMS_DB_PATH)
            for r in conn.execute("SELECT code_num FROM forwarded_drafts"):
                if r[0]:
                    nums.add(r[0])
            conn.close()
        except Exception:
            pass
        return nums