import re
from email.message import Message
from typing import List, Dict, Any, Optional

from mailbots_next.config import CONTAINER_RE, CODE_RE, CODE_NUM_RE
from mailbots_next.core.extract import BaseExtractor, ExtractedRow, parse_email


class ATBExtractor(BaseExtractor):
    def extract(self, msg: Message, parsed: Dict[str, Any]) -> List[ExtractedRow]:
        subject = parsed["subject"] or ""
        attachments = parsed["attachments"]

        box_from_subject = self._extract_box_from_subject(subject)
        box_from_attachment = self._extract_box_from_attachment(attachments)

        box_no = None
        if box_from_subject and box_from_attachment:
            if box_from_subject == box_from_attachment:
                box_no = box_from_subject
            else:
                box_no = box_from_subject
        elif box_from_subject:
            box_no = box_from_subject
        elif box_from_attachment:
            box_no = box_from_attachment

        if not box_no:
            return []

        code_num = None
        codes = CODE_RE.findall(subject)
        if codes:
            m = CODE_NUM_RE.match(codes[0])
            if m:
                code_num = m.group(1)

        row = ExtractedRow(
            row_idx=0,
            email_type="atb",
            raw_data={**parsed, "subject_box": box_from_subject, "att_box": box_from_attachment},
            customer_code=None,
            customer_code_num=code_num,
            container_no=box_no,
        )
        return [row]

    def _extract_box_from_subject(self, subject: str) -> Optional[str]:
        parts = subject.strip().split()
        if not parts:
            return None
        last_part = parts[-1]
        if CONTAINER_RE.match(last_part):
            return last_part.upper()
        return None

    def _extract_box_from_attachment(self, attachments: List[Dict]) -> Optional[str]:
        for att in attachments:
            filename = att["filename"] or ""
            match = re.match(r'^([A-Z]{4}\d{7})_ATB\.pdf$', filename, re.I)
            if match:
                return match.group(1).upper()
        return None