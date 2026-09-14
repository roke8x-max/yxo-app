import email
import email.policy
import smtplib
import io
import hashlib
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from email.utils import parseaddr
from typing import List, Optional, Tuple, Dict, Any

from bs4 import BeautifulSoup

from ..config import SMTP_SERVER, SMTP_PORT, settings
from .. import config
from .log import get_logger
from .store import write_dsk_timestamp, write_tracing_log, write_tracing_snapshot, write_forward_log

_log = get_logger(__name__)


def decode_header_safe(raw: str) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="replace")
        else:
            out += str(part)
    return out


def make_forward_id(message_id: str, row_key: str) -> str:
    """生成 VERP 安全的 forward_id：纯 hex + 短横线，可安全进入邮件地址本地名。
    message_id/row_key 的业务关联另存 forward_log，故令牌本身无需携带明文。"""
    digest = hashlib.sha256(f"{message_id}|{row_key}".encode("utf-8")).hexdigest()[:16]
    return f"{digest}-{uuid.uuid4().hex[:8]}"


def _bounce_addr(forward_id: str) -> str:
    """返回 envelope MAIL FROM。
    VERP 开启：bounce+<forward_id>@<BOUNCE_ADDRESS 域名>（forward_id 为纯 hex，RFC 合规）。
    VERP 关闭：直接返回 BOUNCE_ADDRESS（关联仅靠 X-YXO-Forward-Id 头）。"""
    if settings.BOUNCE_USE_VERP:
        domain = settings.BOUNCE_ADDRESS.partition("@")[2]
        return f"bounce+{forward_id}@{domain}"
    return settings.BOUNCE_ADDRESS


def _envelope_from(forward_id: str, sender_email: str) -> str:
    """按 BOUNCE_ENVELOPE_MODE 返回 envelope MAIL FROM。
    sender（默认）：本封发信账号，NDR 落回同事自己收件箱；
    fixed：BOUNCE_ADDRESS；verp：bounce+<fid>@<BOUNCE_ADDRESS 域名>。"""
    mode = (settings.BOUNCE_ENVELOPE_MODE or "sender").lower()
    if mode == "verp":
        return _bounce_addr(forward_id)
    if mode == "fixed":
        return settings.BOUNCE_ADDRESS
    return sender_email


def build_forward_message(
    original_msg: email.message.Message,
    to_list: List[str],
    cc_list: List[str],
    sender_email: str,
    subject_prefix: str = "",
    html_override: Optional[str] = None,
    attachments_override: Optional[List[Dict]] = None,
    forward_id: Optional[str] = None,
) -> email.message.Message:
    msg = MIMEMultipart("mixed")
    msg["From"] = sender_email
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    original_subject = decode_header_safe(original_msg.get("Subject", ""))
    msg["Subject"] = f"{subject_prefix}{original_subject}"

    if forward_id:
        msg["X-YXO-Forward-Id"] = forward_id

    if html_override is not None:
        msg.attach(MIMEText(html_override, "html", "utf-8"))
    else:
        for part in original_msg.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", "")).lower()
            if "attachment" not in disposition and content_type in ("text/html", "text/plain"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        text = payload.decode(charset, errors="replace")
                    except Exception:
                        text = payload.decode("utf-8", errors="replace")
                    if content_type == "text/html":
                        msg.attach(MIMEText(text, "html", "utf-8"))
                    else:
                        msg.attach(MIMEText(text, "plain", "utf-8"))
                    break

    if attachments_override:
        for att in attachments_override:
            payload = att.get("payload")
            filename = att.get("filename", "attachment.bin")
            content_type = att.get("content_type", "application/octet-stream")
            if not payload:
                continue
            maintype, subtype = content_type.split("/", 1) if "/" in content_type else ("application", "octet-stream")
            new_part = MIMEBase(maintype, subtype)
            new_part.set_payload(payload)
            encoders.encode_base64(new_part)
            new_part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
            msg.attach(new_part)
    else:
        for part in original_msg.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get("Content-Disposition", "")).lower()
            filename = part.get_filename()
            if "attachment" in disposition and filename:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                new_part = MIMEBase(part.get_content_maintype(), part.get_content_subtype())
                new_part.set_payload(payload)
                encoders.encode_base64(new_part)
                new_part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
                msg.attach(new_part)

    return msg


def send_smtp(
    msg: email.message.Message,
    sender_email: str,
    sender_password: str,
    to_list: List[str],
    cc_list: List[str] = None,
    mail_from: Optional[str] = None,
) -> dict:
    cc_list = cc_list or []
    all_recipients = list(set(to_list + cc_list))
    msg["From"] = sender_email
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.login(sender_email, sender_password)
            env_from = mail_from or sender_email
            refused = server.sendmail(env_from, all_recipients, msg.as_string())
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        return {"success": True, "refused": {}}
    except Exception as e:
        _log.error(f"SMTP send failed: {e}")
        return {"success": False, "error": str(e)}


def _record_forward(forward_id, message_id, row, send_ctx, to_list, cc_list,
                    original_raw, sent_by=""):
    """成功转发后写 forward_log（仅 is_live 落库）。元数据取自 send_ctx['_meta']；
    sent_by 为本封 SMTP 发信账号（NDR 次键关联用）。row_key 优先取 serve 侧
    注入的 send_ctx['_row_key']（单一来源），缺失才用 serve_row_key(row) 兜底。"""
    from mailbots_next.core.rowkey import serve_row_key
    if not config.is_live():
        return
    meta = (send_ctx or {}).get("_meta", {}) or {}
    row_key = (send_ctx or {}).get("_row_key") or serve_row_key(row)
    write_forward_log(
        forward_id, message_id, row_key,
        meta.get("account", ""), meta.get("folder", ""),
        int(meta.get("uid", 0) or 0),
        meta.get("subject", ""), meta.get("sender", ""), meta.get("date_hdr", ""),
        original_raw, to_list, cc_list, sent_by,
    )


def split_waybill_by_company(
    original_msg: email.message.Message,
    rows: List[Dict],
    company_routes: Dict[str, Tuple[List[str], List[str]]],
) -> List[Dict]:
    groups = {}
    for r in rows:
        code = r.get("客户编码", "")
        box = r.get("箱号", "")
        company = r.get("company", "")
        if not company:
            continue
        key = (tuple(company_routes.get(company, ([], []))[0]), tuple(company_routes.get(company, ([], []))[1]))
        if key not in groups:
            groups[key] = {"to": company_routes[company][0], "cc": company_routes[company][1], "company": company, "rows": []}
        groups[key]["rows"].append(r)

    result = []
    for g in groups.values():
        if not g["rows"]:
            continue
        msg = MIMEMultipart("mixed")
        original_subject = decode_header_safe(original_msg.get("Subject", ""))
        msg["Subject"] = original_subject
        lines = ["本邮件仅包含贵公司相关的运单号信息：", "", "客户编码 | 箱号 | 运单号", "--- | --- | ---"]
        for r in g["rows"]:
            lines.append(f"{r.get('客户编码') or '-'} | {r.get('箱号') or '-'} | {r.get('运单号') or '-'}")
        msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))

        for part in original_msg.walk():
            if part.is_multipart():
                continue
            fn = part.get_filename()
            if fn and fn.lower().endswith(".xls"):
                payload = part.get_payload(decode=True)
                if payload:
                    new_bytes, new_name = rewrite_xls_filtered(payload, fn, g["rows"])
                    if new_bytes:
                        np = MIMEBase("application", "octet-stream")
                        np.set_payload(new_bytes)
                        encoders.encode_base64(np)
                        np.add_header("Content-Disposition", "attachment", filename=("utf-8", "", new_name))
                        msg.attach(np)
                    else:
                        np = MIMEBase(part.get_content_maintype(), part.get_content_subtype())
                        np.set_payload(payload)
                        encoders.encode_base64(np)
                        np.add_header("Content-Disposition", "attachment", filename=("utf-8", "", fn))
                        msg.attach(np)
                break

        result.append({
            "msg": msg,
            "to": g["to"],
            "cc": g["cc"],
            "company": g["company"],
            "rows": g["rows"],
        })
    return result


def _col_index(headers: List[str], candidates: List[str]) -> int:
    for cand in candidates:
        for i, h in enumerate(headers):
            if h and cand.lower() in h.lower():
                return i
    return -1


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return str(v)
    return str(v).strip()


def rewrite_xls_filtered(raw_bytes: bytes, name: str, keep_rows: List[Dict]) -> Tuple[Optional[bytes], Optional[str]]:
    if not keep_rows:
        return None, None
    try:
        import xlrd
        book = xlrd.open_workbook(file_contents=raw_bytes)
        sh = book.sheet_by_index(0)
        headers = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
        idx_code = _col_index(headers, ["客户编码", "客户代码", "code"])
        idx_box = _col_index(headers, ["箱号", "箱", "container", "box"])
        idx_rwb = _col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])

        keep_set = set()
        for r in keep_rows:
            keep_set.add((
                str(r.get("客户编码", "")).strip().upper(),
                str(r.get("箱号", "")).strip().upper(),
                str(r.get("运单号", "")).strip().upper(),
            ))

        kept = []
        for r in range(1, sh.nrows):
            row_data = [sh.cell_value(r, c) for c in range(sh.ncols)]
            key = (
                _cell_str(row_data[idx_code]) if idx_code >= 0 else "",
                _cell_str(row_data[idx_box]) if idx_box >= 0 else "",
                _cell_str(row_data[idx_rwb]) if idx_rwb >= 0 else "",
            )
            if key in keep_set:
                kept.append(row_data)

        if not kept:
            return None, None

        import xlwt
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
    except Exception:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            ws = wb.active
            data = list(ws.iter_rows(values_only=True))
            if not data:
                return None, None
            headers = [str(h or "").strip() for h in data[0]]
            idx_code = _col_index(headers, ["客户编码", "客户代码", "code"])
            idx_box = _col_index(headers, ["箱号", "箱", "container", "box"])
            idx_rwb = _col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])

            keep_set = set()
            for r in keep_rows:
                keep_set.add((
                    str(r.get("客户编码", "")).strip().upper(),
                    str(r.get("箱号", "")).strip().upper(),
                    str(r.get("运单号", "")).strip().upper(),
                ))

            kept = []
            for row in data[1:]:
                key = (
                    str(row[idx_code]).strip().upper() if idx_code >= 0 and idx_code < len(row) and row[idx_code] else "",
                    str(row[idx_box]).strip().upper() if idx_box >= 0 and idx_box < len(row) and row[idx_box] else "",
                    str(row[idx_rwb]).strip().upper() if idx_rwb >= 0 and idx_rwb < len(row) and row[idx_rwb] else "",
                )
                if key in keep_set:
                    kept.append(row)

            if not kept:
                return None, None

            new_wb = openpyxl.Workbook()
            new_ws = new_wb.active
            new_ws.append([_cell_str(h) for h in headers])
            for row in kept:
                new_ws.append([_cell_str(c) for c in row])
            buf = io.BytesIO()
            new_wb.save(buf)
            return buf.getvalue(), name
        except Exception:
            return None, None


def split_dsk_by_box(
    original_msg: email.message.Message,
    html_body: str,
    attachments: List[Dict],
    container_rows: List[Tuple[str, str]],
    target_box: str,
) -> Tuple[Optional[str], List[Dict]]:
    modified_html = None
    if html_body:
        try:
            soup = BeautifulSoup(html_body, "html.parser")
            for table in soup.find_all("table"):
                for tr in table.find_all("tr"):
                    cells = tr.find_all(["td", "th"])
                    if len(cells) >= 2:
                        cell1 = cells[0].get_text(strip=True)
                        if cell1 != target_box:
                            tr.decompose()
            modified_html = str(soup)
        except Exception:
            modified_html = html_body

    matched_atts = []
    for att in attachments:
        filename = att["filename"] or ""
        if target_box.upper() in filename.upper():
            matched_atts.append(att)
    return modified_html, matched_atts


def build_dsk_subject(original_subject: str, box_no: str, customer_code: str) -> str:
    return f"【{box_no}】{original_subject} - {customer_code or '未知编码'}"


def _box_customer_code(records, box_no: str) -> str:
    """Reverse lookup: box number -> 客户编码 from active master rows."""
    for r in records or []:
        if (r.get("箱号") or "") == box_no:
            return r.get("客户编码") or ""
    return ""


def _forward_split(decision, row, routing, original_msg, sender_email: str,
                   sender_password: str, message_id: str, send_ctx: dict, original_raw: bytes) -> Tuple[bool, str]:
    """Per-company/per-box fan-out for multi-target mails (same contract as before)."""
    email_type = str(send_ctx.get("email_type", ""))
    key = send_ctx.get("keys", {}).get(row.row_idx)
    if key is None or row.row_idx != send_ctx.get("designated", {}).get(key):
        _log.info(f"Group-covered, skip duplicate send | msg_id={message_id[:50]} | row={row.row_idx}")
        return True, "group-covered"

    sub_fid = make_forward_id(message_id, str(key))   # 子 forward_id（贯穿 头/envelope/log）

    try:
        if email_type in ("waybill", "draft"):
            company = key
            parts = split_waybill_by_company(
                original_msg, send_ctx["group_rows"][key], send_ctx["company_routes"])
            part = next((p for p in parts if p.get("company") == company), None)
            if part is None:
                return False, f"No split part for company {company}"
            part["msg"]["X-YXO-Forward-Id"] = sub_fid                # 写头（split 不走 build_forward_message）
            result = send_smtp(part["msg"], sender_email, sender_password,
                               part["to"], part["cc"],
                               mail_from=_envelope_from(sub_fid, sender_email))  # envelope 用 sub_fid
            if not result["success"]:
                return False, f"SMTP failed: {result.get('error')}"
            try:
                _record_forward(sub_fid, message_id, row, send_ctx,
                                part["to"], part["cc"], original_raw, sender_email)
            except Exception as e:
                _log.error(f"Post-send bookkeeping failed (mail already sent) | msg={message_id}: {e}")
                from mailbots_next.core.notify import increment_counter
                increment_counter("post_send_bookkeeping_failed")
            return True, "forwarded"
        elif email_type in ("dsk", "atb"):
            company, box = key
            cropped, matched = split_dsk_by_box(
                original_msg, send_ctx["html_body"], send_ctx["attachments"],
                send_ctx["container_rows"], box)
            unrelated = [a for a in send_ctx["attachments"]
                         if not any(b and b.upper() in (a.get("filename") or "").upper()
                                    for b in send_ctx["all_boxes"])]
            to_list, cc_list = send_ctx["company_routes"][company]
            if email_type == "dsk":
                code = _box_customer_code(send_ctx.get("records"), box)
                subject = build_dsk_subject(
                    decode_header_safe(original_msg.get("Subject", "")), box, code)
            else:
                subject = decode_header_safe(original_msg.get("Subject", ""))
            msg = build_forward_message(original_msg, to_list, cc_list, sender_email,
                                        html_override=cropped,
                                        attachments_override=matched + unrelated,
                                        forward_id=sub_fid)          # 写 X-YXO-Forward-Id 头
            msg.replace_header("Subject", subject)
            result = send_smtp(msg, sender_email, sender_password, to_list, cc_list,
                               mail_from=_envelope_from(sub_fid, sender_email))  # envelope 用 sub_fid
            if not result["success"]:
                return False, f"SMTP failed: {result.get('error')}"
            try:
                if row.container_no:
                    write_dsk_timestamp(row.container_no, email_type.upper(),
                                        datetime.now().strftime("%m/%d %H:%M"))
                _record_forward(sub_fid, message_id, row, send_ctx,
                                to_list, cc_list, original_raw, sender_email)
            except Exception as e:
                _log.error(f"Post-send bookkeeping failed (mail already sent) | msg={message_id}: {e}")
                from mailbots_next.core.notify import increment_counter
                increment_counter("post_send_bookkeeping_failed")
            return True, "forwarded"
        else:
            return False, f"Unsupported split type: {email_type}"
    except Exception as e:
        _log.error(f"Split forward failed for {message_id}: {e}")
        return False, str(e)


def execute_action(
    decision,
    row,
    routing,
    original_raw: bytes,
    sender_email: str,
    sender_password: str,
    message_id: str,
    send_ctx=None,
) -> Tuple[bool, str]:
    if decision.action == "skip":
        return True, "skipped"

    if decision.action == "alarm":
        return True, "alarm_logged"

    if decision.action == "pending":
        return True, "pending_queued"

    if decision.action != "forward":
        return False, f"Unknown action: {decision.action}"

    # Safety gate: test mode never sends externally nor writes business state.
    if not config.is_live():
        _log.info(f"TEST MODE: skip actual send/write for {message_id}")
        return (True, "test-mode-skipped")

    original_msg = email.message_from_bytes(original_raw, policy=email.policy.default)

    try:
        if routing.success:
            to_list = routing.to_list
            cc_list = routing.cc_list
        else:
            return False, "No valid recipients"

        # Multi-target mail: per-company/per-box split sends (single-target
        # mails keep the legacy whole-mail path below).
        if send_ctx and send_ctx.get("multi"):
            return _forward_split(decision, row, routing, original_msg,
                                  sender_email, sender_password,
                                  message_id, send_ctx, original_raw)

        # 单目标路径：同一 forward_id 贯穿 头 / envelope(VERP) / forward_log 三处。
        forward_id = make_forward_id(message_id, str(row.row_idx))
        msg = build_forward_message(original_msg, to_list, cc_list, sender_email,
                                    forward_id=forward_id)
        result = send_smtp(msg, sender_email, sender_password, to_list, cc_list,
                           mail_from=_envelope_from(forward_id, sender_email))
        if not result["success"]:
            return False, f"SMTP failed: {result.get('error')}"
        # 邮件已发出：此后任何失败都不得翻转结论，否则重放会导致重复发信
        try:
            _record_forward(forward_id, message_id, row, send_ctx,
                            to_list, cc_list, original_raw, sender_email)
            if row.email_type in ("dsk", "atb") and row.container_no:
                write_dsk_timestamp(row.container_no, row.email_type.upper(),
                                    datetime.now().strftime("%m/%d %H:%M"))
        except Exception as e:
            _log.error(f"Post-send bookkeeping failed (mail already sent) | msg={message_id}: {e}")
            from mailbots_next.core.notify import increment_counter
            increment_counter("post_send_bookkeeping_failed")
        return True, "forwarded"
    except Exception as e:
        _log.error(f"Forward failed for {message_id}: {e}")
        return False, str(e)