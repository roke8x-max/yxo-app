import re
from email.message import Message
from typing import List, Dict, Any

from mailbots_next.config import CODE_RE, CODE_NUM_RE, CONTAINER_RE
from mailbots_next.core.extract import BaseExtractor, ExtractedRow, parse_email


class WaybillExtractor(BaseExtractor):
    def extract(self, msg: Message, parsed: Dict[str, Any]) -> List[ExtractedRow]:
        subject = parsed["subject"] or ""
        body = parsed["plain_body"] or parsed["html_body"] or ""
        attachments = parsed["attachments"]

        rows = []
        xls_att = None
        for att in attachments:
            if att["filename"].lower().endswith(".xls"):
                xls_att = att
                break

        if xls_att:
            parsed_rows = self._parse_waybill_xls(xls_att["payload"])
            for i, r in enumerate(parsed_rows):
                code = r.get("客户编码", "")
                box = r.get("箱号", "")
                code_num = None
                if code:
                    m = CODE_NUM_RE.match(code)
                    if m:
                        code_num = m.group(1)
                row = ExtractedRow(
                    row_idx=i,
                    email_type="waybill",
                    raw_data={**parsed, "waybill_row": r},
                    customer_code=code,
                    customer_code_num=code_num,
                    container_no=box,
                )
                rows.append(row)
        else:
            code = None
            codes = CODE_RE.findall(subject)
            if codes:
                code = codes[0]
            box = None
            for fn in [a["filename"] for a in attachments]:
                m = CONTAINER_RE.search(fn + " ")
                if m:
                    box = m.group(0)
                    break
            code_num = None
            if code:
                m = CODE_NUM_RE.match(code)
                if m:
                    code_num = m.group(1)
            row = ExtractedRow(
                row_idx=0,
                email_type="waybill",
                raw_data=parsed,
                customer_code=code,
                customer_code_num=code_num,
                container_no=box,
            )
            rows.append(row)

        return rows

    def _parse_waybill_xls(self, raw_bytes: bytes) -> List[Dict[str, str]]:
        rows = []
        try:
            import io, xlrd
            book = xlrd.open_workbook(file_contents=raw_bytes)
            sh = book.sheet_by_index(0)
            headers = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
            idx_code = self._col_index(headers, ["客户编码", "客户代码", "code"])
            idx_box = self._col_index(headers, ["箱号", "箱", "container", "box"])
            idx_rwb = self._col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])
            for r in range(1, sh.nrows):
                code = self._cell_str(sh.cell_value(r, idx_code)) if idx_code >= 0 else ""
                box = self._cell_str(sh.cell_value(r, idx_box)) if idx_box >= 0 else ""
                rwb = self._cell_str(sh.cell_value(r, idx_rwb)) if idx_rwb >= 0 else ""
                if not (code or box or rwb):
                    continue
                rows.append({"客户编码": code, "箱号": box, "运单号": rwb})
        except Exception:
            pass
        return rows

    def _col_index(self, headers: List[str], candidates: List[str]) -> int:
        for cand in candidates:
            for i, h in enumerate(headers):
                if h and cand.lower() in h.lower():
                    return i
        return -1

    def _cell_str(self, v) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            if v == int(v):
                return str(int(v))
            return str(v)
        return str(v).strip()