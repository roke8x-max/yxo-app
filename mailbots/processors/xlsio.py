# -*- coding: utf-8 -*-
"""运单号 .xls/.xlsx 纯函数（自 Waybill_Robot.py:397-611 原样移植）：
parse_waybill_xls / _xls_all_rows / rewrite_xls_filtered 及其私有助手
_col_index/_cell_str；CODE_RE 常量一并复制（Waybill_Robot.py:144）。
唯一改动：不依赖旧机器人全局状态——log() 改为模块内 stdlib logging。
get_attachment_bytes 留在旧处，本模块只收原始字节。"""
import logging
import re

CODE_RE = re.compile(r'CQWLJT[0-9A-Za-z\-]+')

_log = logging.getLogger(__name__)


def log(msg):
    # 旧机器人的 log 写 LOG_DIR 全局文件；纯函数模块改为 stdlib logging
    _log.warning(msg)


# ---------- .xls 解析（运单号提取） ----------
def _col_index(headers, candidates):
    for cand in candidates:
        for i, h in enumerate(headers):
            if h and cand.lower() in h.lower():
                return i
    return -1


def _cell_str(v):
    if v is None:
        return ""
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return str(v)
    return str(v).strip()


def parse_waybill_xls(raw_bytes):
    """解析运单号 .xls 附件, 返回 [{客户编码, 箱号, 运单号}] 列表。
    优先 xlrd(.xls) / openpyxl(.xlsx); 都不存在则退化为文本提取(仅客户编码)。"""
    rows = []
    # 1) xlrd 处理真正的 .xls (BIFF)
    try:
        import io, xlrd
        book = xlrd.open_workbook(file_contents=raw_bytes)
        sh = book.sheet_by_index(0)
        headers = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
        idx_code = _col_index(headers, ["客户编码", "客户代码", "code"])
        idx_box = _col_index(headers, ["箱号", "箱", "container", "box"])
        idx_rwb = _col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])
        for r in range(1, sh.nrows):
            code = _cell_str(sh.cell_value(r, idx_code)) if idx_code >= 0 else ""
            box = _cell_str(sh.cell_value(r, idx_box)) if idx_box >= 0 else ""
            rwb = _cell_str(sh.cell_value(r, idx_rwb)) if idx_rwb >= 0 else ""
            if not (code or box or rwb):
                continue
            rows.append({"客户编码": code, "箱号": box, "运单号": rwb})
        if rows:
            return rows
    except Exception as e:
        log(f"  ⚠ xlrd 解析失败: {e}")
    # 2) openpyxl 处理 .xlsx
    try:
        import io, openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        ws = wb.active
        data = list(ws.iter_rows(values_only=True))
        if data:
            headers = [str(h or "").strip() for h in data[0]]
            idx_code = _col_index(headers, ["客户编码", "客户代码", "code"])
            idx_box = _col_index(headers, ["箱号", "箱", "container", "box"])
            idx_rwb = _col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])
            for r in data[1:]:
                code = str(r[idx_code]).strip() if idx_code >= 0 and idx_code < len(r) and r[idx_code] is not None else ""
                box = str(r[idx_box]).strip() if idx_box >= 0 and idx_box < len(r) and r[idx_box] is not None else ""
                rwb = str(r[idx_rwb]).strip() if idx_rwb >= 0 and idx_rwb < len(r) and r[idx_rwb] is not None else ""
                if not (code or box or rwb):
                    continue
                rows.append({"客户编码": code, "箱号": box, "运单号": rwb})
            return rows
    except Exception as e:
        log(f"  ⚠ openpyxl 解析失败: {e}")
    # 3) 文本退化：从字节里抽可打印 ASCII 行, 找 CQWLJT... 客户编码
    try:
        text = raw_bytes.decode("latin-1", "ignore")
        for line in text.splitlines():
            line = line.strip()
            mcode = CODE_RE.search(line)
            if mcode:
                rows.append({"客户编码": mcode.group(0).split("-")[0], "箱号": "", "运单号": ""})
        if rows:
            log("  ⚠ 无 xls 解析库, 使用文本退化提取(仅客户编码)")
    except Exception:
        pass
    return rows


# ---------- 完全拆分转发（2026-08-14 修复） ----------
def _xls_all_rows(raw_bytes, name):
    """返回 (headers, rows) 或 (None, None)。rows=list[list](全列), 兼容 .xlsx/.xls。"""
    lower = (name or "").lower()
    if lower.endswith(".xlsx"):
        try:
            import io, openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            data = list(wb.active.iter_rows(values_only=True))
            wb.close()
            if data:
                return [str(h or "").strip() for h in data[0]], [[c for c in row] for row in data[1:]]
        except Exception as e:
            log(f"  ⚠ openpyxl 读取失败: {e}")
    else:
        try:
            import io, xlrd
            book = xlrd.open_workbook(file_contents=raw_bytes)
            sh = book.sheet_by_index(0)
            headers = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
            rows = [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(1, sh.nrows)]
            return headers, rows
        except Exception as e:
            log(f"  ⚠ xlrd 读取失败: {e}")
    return None, None


def rewrite_xls_filtered(raw_bytes, name, keep_rows):
    """保留原始表头与所有列, 仅保留 keep_rows 命中的行; 返回 (new_bytes, new_name) 或 (None, None)。
    keep_rows: list[{客户编码,箱号,运单号}]。.xlsx 原样重写; .xls 优先 xlwt, 缺失则转 .xlsx 并告警。"""
    if not keep_rows:
        return None, None
    headers, all_rows = _xls_all_rows(raw_bytes, name)
    if headers is None:
        return None, None
    idx_code = _col_index(headers, ["客户编码", "客户代码", "code"])
    idx_box = _col_index(headers, ["箱号", "箱", "container", "box"])
    idx_rwb = _col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])

    def _idx_ok(i, row):
        return i >= 0 and i < len(row) and row[i] is not None

    def _key(row):
        def _v(i):
            return str(row[i]).strip().upper() if _idx_ok(i, row) else ""
        return (_v(idx_code), _v(idx_box), _v(idx_rwb))

    keep_set = set()
    for r in keep_rows:
        keep_set.add((str(r.get("客户编码", "")).strip().upper(),
                      str(r.get("箱号", "")).strip().upper(),
                      str(r.get("运单号", "")).strip().upper()))
    kept = [row for row in all_rows if _key(row) in keep_set]
    if not kept:
        return None, None
    lower = (name or "").lower()
    if lower.endswith(".xlsx"):
        try:
            import io, openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append([_cell_str(h) for h in headers])
            for row in kept:
                ws.append([_cell_str(c) for c in row])
            buf = io.BytesIO()
            wb.save(buf)
            wb.close()
            return buf.getvalue(), name
        except Exception as e:
            log(f"  ⚠ openpyxl 写失败: {e}")
    else:
        try:
            import io, xlwt
            wb = xlwt.Workbook()
            ws = wb.add_sheet("Sheet1")
            for c, h in enumerate(headers):
                ws.write(0, c, _cell_str(h))
            for ri, row in enumerate(kept, start=1):
                for c, v in enumerate(row):
                    ws.write(ri, c, _cell_str(v))
            buf = io.BytesIO()
            wb.save(buf)
            return buf.getvalue(), name
        except Exception as e:
            log(f"  ⚠ xlwt 写失败(缺 xlwt? 转 .xlsx): {e}")
            try:
                import io, openpyxl
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.append([_cell_str(h) for h in headers])
                for row in kept:
                    ws.append([_cell_str(c) for c in row])
                buf = io.BytesIO()
                wb.save(buf)
                wb.close()
                base = name[:-4] if (name or "").lower().endswith(".xls") else name
                return buf.getvalue(), base + ".xlsx"
            except Exception as e2:
                log(f"  ⚠ 回退写也失败: {e2}")
    return None, None
