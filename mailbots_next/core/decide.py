from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import EmailType, CODE_NUM_RE
from .log import get_logger

_log = get_logger(__name__)


def _split_code(code: str) -> Tuple[str, str]:
    """Split 客户编码 into (stem_digits, dest_suffix).

    stem = digits after CQWLJT (the 序号); suffix = part after '-' (到站后缀),
    '' when the code has no '-'. Codes not matching CQWLJT<digits> fall back
    to (code, '') so they only match themselves.
    """
    if not code:
        return "", ""
    m = CODE_NUM_RE.match(code)
    stem = m.group(1) if m else code
    suffix = code.rsplit("-", 1)[1] if "-" in code else ""
    return stem, suffix


@dataclass
class Decision:
    tier: str
    action: str
    reason: str
    notify_type: Optional[str] = None


def decide_draft(row, routing, records) -> Decision:
    if row.draft_category in ("C2", "W", "OTHER"):
        return Decision("MANUAL", "skip", f"Non-auto category: {row.draft_category}")

    code = row.customer_code or ""
    box = row.container_no or ""

    if code:
        # T1: exact customer-code hit (recipients resolved)
        if routing.success and any(r.get("客户编码") == code for r in records):
            return Decision("T1", "forward", "Exact customer code match")

        # Stem analysis on the active subset (records are pre-filtered by the
        # scope rule: 状态<>'退舱' AND is_deleted=0)
        stem, suffix = _split_code(code)
        stem_rows = [
            r for r in records
            if stem and _split_code(r.get("客户编码", ""))[0] == stem
        ]
        if not stem_rows:
            return Decision("T4", "alarm", "Customer code stem not in active records")

        # T2: same stem + same box, different destination suffix
        if box:
            same_box = [r for r in stem_rows if (r.get("箱号") or "") == box]
            if same_box and any(
                _split_code(r.get("客户编码", ""))[1] != suffix for r in same_box
            ):
                return Decision("T2", "pending", "Same stem and box, different destination suffix")

        # T7: defensive guard — one stem spans >=2 active suffixes
        suffixes = {_split_code(r.get("客户编码", ""))[1] for r in stem_rows}
        if len(suffixes) >= 2:
            return Decision("T7", "pending", "Stem spans multiple active suffixes")

        # T3: stem hit but box mismatch/empty
        return Decision("T3", "pending", "Stem hit but box mismatch or empty")

    # Codeless branch: never enters T1-T3
    if not box:
        return Decision("T0", "alarm", "No customer code and no box")
    box_matches = [r for r in records if (r.get("箱号") or "") == box]
    if len(box_matches) == 0:
        return Decision("T0", "alarm", "No customer code and box has no hit")
    if len(box_matches) == 1:
        return Decision("T5", "alarm", "No customer code but unique box match")
    return Decision("T6", "pending", "Box number reused by multiple records")


def decide_waybill(row, routing, records) -> Decision:
    # Waybill shares the draft eight-tier engine (客编+箱号 keys).
    return decide_draft(row, routing, records)


def decide_dsk(row, routing, records) -> Decision:
    # Reused box (route_dsk marks box_reuse) alarms instead of forwarding
    # to the first company it happens to hit.
    if getattr(routing, "route_source", "") == "box_reuse":
        return Decision("T6", "alarm", "Box number reused by multiple records (>=2)")

    if routing.success:
        return Decision("T1", "forward", "Unique box match")

    if row.container_no:
        box_matches = [r for r in records if r["箱号"] == row.container_no]
        if len(box_matches) >= 2:
            return Decision("T6", "alarm", "Box number reused by multiple records (>=2)")

    return Decision("T0", "alarm", "Box not found in active records")


def decide_atb(row, routing, records) -> Decision:
    return decide_dsk(row, routing, records)


def decide_tracing(row, routing, records) -> Decision:
    if isinstance(routing, list):
        if routing:
            return Decision("TRACE_HIT", "forward", f"Matched {len(routing)} companies via train")
        else:
            return Decision("TRACE_MISS", "skip", "No company matched for this train")
    return Decision("TRACE_MISS", "skip", "Invalid routing result")


def decide(email_type, row, routing, records) -> Decision:
    # Accept both EmailType enum and string
    if isinstance(email_type, str):
        try:
            email_type = EmailType(email_type)
        except ValueError:
            return Decision("UNKNOWN", "skip", f"Unknown email type: {email_type}")
    
    if email_type == EmailType.DRAFT:
        return decide_draft(row, routing, records)
    elif email_type == EmailType.WAYBILL:
        return decide_waybill(row, routing, records)
    elif email_type == EmailType.DSK:
        return decide_dsk(row, routing, records)
    elif email_type == EmailType.ATB:
        return decide_atb(row, routing, records)
    elif email_type == EmailType.TRACING:
        return decide_tracing(row, routing, records)
    else:
        return Decision("UNKNOWN", "skip", f"Unknown email type: {email_type}")