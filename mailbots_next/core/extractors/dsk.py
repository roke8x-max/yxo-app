import re
from email.message import Message
from typing import List, Dict, Any, Optional, Tuple

from bs4 import BeautifulSoup

from mailbots_next.config import CONTAINER_RE, COMPANY_ALIAS
from mailbots_next.core.extract import BaseExtractor, ExtractedRow, parse_email


class DSKExtractor(BaseExtractor):
    def extract(self, msg: Message, parsed: Dict[str, Any]) -> List[ExtractedRow]:
        subject = parsed["subject"] or ""
        sender = parsed["sender"] or ""
        html_body = parsed["html_body"]
        attachments = parsed["attachments"]

        container_rows = self._extract_container_table(html_body)
        box_nos = self._extract_box_from_attachments(attachments)
        box_nos.update(self._extract_box_from_subject(subject))

        if not box_nos:
            return []

        sender_type = self._identify_sender(sender)
        if sender_type == "kasa" and html_body:
            container_rows = self._extract_container_table(html_body)
            if container_rows:
                box_nos = {row[0] for row in container_rows}

        rows = []
        for i, box_no in enumerate(sorted(box_nos)):
            row = ExtractedRow(
                row_idx=i,
                email_type="dsk",
                raw_data={**parsed, "sender_type": sender_type, "container_rows": container_rows},
                customer_code=None,
                customer_code_num=None,
                container_no=box_no,
            )
            rows.append(row)

        return rows

    def _identify_sender(self, sender: str) -> str:
        sender_lower = sender.lower()
        if "kasa@rtsb.de" in sender_lower:
            return "kasa"
        elif "reex.mala@deutschebahn.com" in sender_lower:
            return "deutschebahn"
        return "unknown"

    def _extract_container_table(self, html_body: Optional[str]) -> List[Tuple[str, str]]:
        if not html_body:
            return []
        rows = []
        container_re = re.compile(r'[A-Z]{4}\d{7}')
        try:
            soup = BeautifulSoup(html_body, "html.parser")
            for table in soup.find_all("table"):
                for tr in table.find_all("tr"):
                    cells = tr.find_all(["td", "th"])
                    if len(cells) >= 2:
                        cell1 = cells[0].get_text(strip=True)
                        cell2 = cells[1].get_text(strip=True)
                        m = container_re.search(cell1)
                        if m:
                            rows.append((m.group(0).upper(), cell2))
        except Exception:
            pass
        return rows

    def _extract_box_from_attachments(self, attachments: List[Dict]) -> set:
        box_nos = set()
        container_re = re.compile(r'[A-Z]{4}\d{7}')
        for att in attachments:
            filename = att["filename"] or ""
            matches = container_re.findall(filename.upper())
            for m in matches:
                box_nos.add(m)
        return box_nos

    def _extract_box_from_subject(self, subject: str) -> set:
        box_nos = set()
        container_re = re.compile(r'[A-Z]{4}\d{7}')
        matches = container_re.findall(subject.upper() + " ")
        for m in matches:
            box_nos.add(m)
        return box_nos