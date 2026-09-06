import email
import email.policy
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from ..config import EmailType, EXTRACTION_KEYS
from .log import get_logger

_log = get_logger(__name__)


@dataclass
class ExtractedRow:
    row_idx: int
    email_type: EmailType
    raw_data: Dict[str, Any] = field(default_factory=dict)
    customer_code: Optional[str] = None
    customer_code_num: Optional[str] = None
    container_no: Optional[str] = None
    train_no: Optional[str] = None
    draft_category: Optional[str] = None
    draft_code_num: Optional[str] = None
    is_draft_attachment: bool = False
    draft_attachment_name: Optional[str] = None


class BaseExtractor:
    def extract(self, msg: email.message.Message, parsed: Dict[str, Any]) -> List[ExtractedRow]:
        raise NotImplementedError


def get_extractor(email_type) -> BaseExtractor:
    # Accept both EmailType enum and string
    if isinstance(email_type, str):
        try:
            email_type = EmailType(email_type)
        except ValueError:
            raise ValueError(f"No extractor for type {email_type}")
    
    if email_type == EmailType.DRAFT:
        from .extractors.draft import DraftExtractor
        return DraftExtractor()
    elif email_type == EmailType.WAYBILL:
        from .extractors.waybill import WaybillExtractor
        return WaybillExtractor()
    elif email_type == EmailType.TRACING:
        from .extractors.tracing import TracingExtractor
        return TracingExtractor()
    elif email_type == EmailType.DSK:
        from .extractors.dsk import DSKExtractor
        return DSKExtractor()
    elif email_type == EmailType.ATB:
        from .extractors.atb import ATBExtractor
        return ATBExtractor()
    else:
        raise ValueError(f"No extractor for type {email_type}")


def parse_email(msg: email.message.Message) -> Dict[str, Any]:
    result = {
        "subject": "",
        "sender": "",
        "date": "",
        "html_body": None,
        "plain_body": None,
        "attachments": [],
        "inline_parts": [],
    }
    result["subject"] = msg.get("Subject", "")
    result["sender"] = msg.get("From", "")
    result["date"] = msg.get("Date", "")

    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", "")).lower()
        filename = part.get_filename() or ""

        if "attachment" in disposition:
            payload = part.get_payload(decode=True)
            if payload:
                result["attachments"].append({
                    "payload": payload,
                    "filename": filename,
                    "content_type": content_type,
                })
        elif "inline" in disposition:
            result["inline_parts"].append(part)
        elif content_type == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                try:
                    result["html_body"] = payload.decode(charset, errors="replace")
                except Exception:
                    result["html_body"] = payload.decode("utf-8", errors="replace")
        elif content_type == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                try:
                    result["plain_body"] = payload.decode(charset, errors="replace")
                except Exception:
                    result["plain_body"] = payload.decode("utf-8", errors="replace")

    return result


def extract_email(email_type, raw_bytes: bytes) -> List[ExtractedRow]:
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    parsed = parse_email(msg)
    extractor = get_extractor(email_type)
    rows = extractor.extract(msg, parsed)
    
    # Handle both EmailType enum and string for logging
    type_str = email_type.value if hasattr(email_type, 'value') else email_type
    _log.log_extraction(
        msg.get("Message-ID", "")[:50],
        type_str,
        len(rows),
        EXTRACTION_KEYS.get(email_type if isinstance(email_type, EmailType) else EmailType(type_str), []),
    )
    return rows