import re
import io
from email.message import Message
from typing import List, Dict, Any

from mailbots_next.config import CODE_RE, CODE_NUM_RE, CONTAINER_RE, WAYBILL_REJECT_KEYWORD
from mailbots_next.core.extract import BaseExtractor, ExtractedRow, parse_email


XLS_SUFFIXES = (".xls", ".xlsx", ".xlsm")


class WaybillExtractor(BaseExtractor):
    def extract(self, msg: Message, parsed: Dict[str, Any]) -> List[ExtractedRow]:
        subject = parsed["subject"] or ""
        body = parsed["plain_body"] or parsed["html_body"] or ""
        attachments = parsed["attachments"]

        # 单证审核驳回（waybill_rejected）：无附件、编码在正文。必须在附件分流之前。
        if WAYBILL_REJECT_KEYWORD in subject:
            code = None
            m = CODE_RE.search(body)
            if m:
                code = m.group(0).split("-")[0]
            code_num = None
            if code:
                nm = CODE_NUM_RE.match(code)
                if nm:
                    code_num = nm.group(1)
            return [ExtractedRow(
                row_idx=0,
                email_type="waybill",
                raw_data=parsed,
                customer_code=code,
                customer_code_num=code_num,
                container_no=None,
                waybill_rejected=True,
            )]

        rows = []
        xls_att = None
        for att in attachments:
            if att["filename"].lower().endswith(XLS_SUFFIXES):
                xls_att = att
                break

        if xls_att:
            parsed_rows = self._parse_waybill_xls(xls_att["payload"], xls_att["filename"])
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

    def _parse_waybill_xls(self, raw_bytes: bytes, filename: str = "") -> List[Dict[str, str]]:
        """Parse waybill xls/xlsx attachment with three-way fallback:
        1. .xlsx/.xlsm -> openpyxl
        2. .xls -> xlrd
        3. Retry with openpyxl (filename may not match content)
        4. Text/CSV fallback
        """
        rows = []
        ext = filename.lower() if filename else ""

        # Try openpyxl first for xlsx/xlsm
        if ext.endswith((".xlsx", ".xlsm")):
            rows = self._read_xlsx(raw_bytes)
            if rows:
                return rows

        # Try xlrd for xls
        if ext.endswith(".xls"):
            rows = self._read_xls(raw_bytes)
            if rows:
                return rows

        # Fallback: try openpyxl (in case .xls is actually xlsx)
        rows = self._read_xlsx(raw_bytes)
        if rows:
            return rows

        # Fallback: try xlrd (in case .xlsx is actually xls)
        rows = self._read_xls(raw_bytes)
        if rows:
            return rows

        # Final fallback: text/CSV parsing
        return self._read_text(raw_bytes)

    def _read_xlsx(self, raw_bytes: bytes) -> List[Dict[str, str]]:
        """Parse .xlsx/.xlsm using openpyxl."""
        try:
            import openpyxl
            book = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            sh = book.worksheets[0]
            headers = [str(sh.cell(row=1, column=c + 1).value or "").strip() for c in range(sh.max_column)]
            idx_code = self._col_index(headers, ["客户编码", "客户代码", "code"])
            idx_box = self._col_index(headers, ["箱号", "箱", "container", "box"])
            idx_rwb = self._col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])
            rows = []
            for r in range(2, sh.max_row + 1):
                code = self._cell_str(sh.cell(row=r, column=idx_code + 1).value) if idx_code >= 0 else ""
                box = self._cell_str(sh.cell(row=r, column=idx_box + 1).value) if idx_box >= 0 else ""
                rwb = self._cell_str(sh.cell(row=r, column=idx_rwb + 1).value) if idx_rwb >= 0 else ""
                if not (code or box or rwb):
                    continue
                rows.append({"客户编码": code, "箱号": box, "运单号": rwb})
            return rows
        except Exception as e:
            return []

    def _read_xls(self, raw_bytes: bytes) -> List[Dict[str, str]]:
        """Parse .xls using xlrd."""
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=raw_bytes)
            sh = book.sheet_by_index(0)
            headers = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
            idx_code = self._col_index(headers, ["客户编码", "客户代码", "code"])
            idx_box = self._col_index(headers, ["箱号", "箱", "container", "box"])
            idx_rwb = self._col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])
            rows = []
            for r in range(1, sh.nrows):
                code = self._cell_str(sh.cell_value(r, idx_code)) if idx_code >= 0 else ""
                box = self._cell_str(sh.cell_value(r, idx_box)) if idx_box >= 0 else ""
                rwb = self._cell_str(sh.cell_value(r, idx_rwb)) if idx_rwb >= 0 else ""
                if not (code or box or rwb):
                    continue
                rows.append({"客户编码": code, "箱号": box, "运单号": rwb})
            return rows
        except Exception as e:
            return []

    def _read_text(self, raw_bytes: bytes) -> List[Dict[str, str]]:
        """Parse as text/CSV (UTF-8 then gbk)."""
        for encoding in ("utf-8", "gbk"):
            try:
                text = raw_bytes.decode(encoding, errors="strict")
            except UnicodeDecodeError:
                continue
            import csv
            reader = csv.reader(io.StringIO(text))
            data = list(reader)
            if len(data) < 2:
                continue
            headers = [h.strip() for h in data[0]]
            idx_code = self._col_index(headers, ["客户编码", "客户代码", "code"])
            idx_box = self._col_index(headers, ["箱号", "箱", "container", "box"])
            idx_rwb = self._col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])
            rows = []
            for row in data[1:]:
                code = row[idx_code] if idx_code >= 0 and idx_code < len(row) else ""
                box = row[idx_box] if idx_box >= 0 and idx_box < len(row) else ""
                rwb = row[idx_rwb] if idx_rwb >= 0 and idx_rwb < len(row) else ""
                if not (code or box or rwb):
                    continue
                rows.append({"客户编码": code.strip(), "箱号": box.strip(), "运单号": rwb.strip()})
            return rows
        return []

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