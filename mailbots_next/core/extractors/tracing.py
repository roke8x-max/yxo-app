import re
from email.message import Message
from typing import List, Dict, Any, Optional

from mailbots_next.config import CONTAINER_RE
from mailbots_next.core.extract import BaseExtractor, ExtractedRow, parse_email


class TracingExtractor(BaseExtractor):
    def extract(self, msg: Message, parsed: Dict[str, Any]) -> List[ExtractedRow]:
        subject = parsed["subject"] or ""
        attachments = parsed["attachments"]

        train_no = self._extract_train_no(subject)
        if not train_no:
            return []

        rows = []
        xls_att = None
        for att in attachments:
            if att["filename"].lower().endswith((".xls", ".xlsx")):
                xls_att = att
                break

        if xls_att:
            parsed_rows = self._parse_tracing_xls(xls_att["payload"])
            for i, r in enumerate(parsed_rows):
                box = r.get("container_no", "")
                row = ExtractedRow(
                    row_idx=i,
                    email_type="tracing",
                    raw_data={**parsed, "tracing_row": r, "train_no": train_no},
                    customer_code=None,
                    customer_code_num=None,
                    container_no=box,
                    train_no=train_no,
                )
                rows.append(row)
        else:
            row = ExtractedRow(
                row_idx=0,
                email_type="tracing",
                raw_data={**parsed, "train_no": train_no},
                customer_code=None,
                customer_code_num=None,
                container_no=None,
                train_no=train_no,
            )
            rows.append(row)

        return rows

    def _extract_train_no(self, subject: str) -> Optional[str]:
        match = re.search(r'train\s+(\d+)', subject, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _parse_tracing_xls(self, raw_bytes: bytes) -> List[Dict[str, str]]:
        rows = []
        try:
            from mailbots.core.tracing_xls import parse_tracing_xls
            for r in parse_tracing_xls(raw_bytes):
                box = r.get("container_no", "")
                if box and CONTAINER_RE.match(box.upper()):
                    rows.append({"container_no": box.upper()})
        except Exception as e:
            from mailbots_next.core.log import get_logger
            get_logger(__name__).error(
                f"Tracing XLS parse failed: {e}", error_id="TRACE_PARSE_ERR"
            )
            return []
        return rows