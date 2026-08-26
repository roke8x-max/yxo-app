# -*- coding: utf-8 -*-
"""草单转发构造纯函数（自 Draft_Forward_Robot.py 原样移植）：
CATEGORY_LABEL(:163-166)、build_forward(:527-570)、decode_any(:221-230)；
test_subject 提取自旧发送处 :767 的测试主题格式。
唯一改动：不 import 旧机器人任何东西；B 类「主题前缀仅加 CATEGORY_LABEL」
自旧调用方 :772 内聚进 build_forward（回传 subject 仍为原始主题，语义不变）。"""
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header

CATEGORY_LABEL = {
    "A": "【转草单】", "B": "【草单更新】", "C1": "【反馈问题】",
    "C2": "确认回复(不转发)", "WAY_A": "运单号确认", "WAY_B": "驳回告警",
}


def decode_any(raw):
    if not raw:
        return ""
    out = ""
    for part, cs in decode_header(raw):
        if isinstance(part, bytes):
            out += part.decode(cs or "utf-8", errors="replace")
        else:
            out += str(part)
    return out


# ---------- 转发 ----------
def build_forward(raw_bytes, category, extra_note=None):
    orig = email.message_from_bytes(raw_bytes)
    subject = decode_any(orig.get("Subject", ""))
    out = MIMEMultipart("mixed")
    html_body, plain_body = None, None
    att_parts = []
    for part in orig.walk():
        if part.is_multipart():
            continue
        ct = part.get_content_type()
        disp = str(part.get("Content-Disposition", "")).lower()
        fn = part.get_filename()
        if "attachment" in disp or fn:
            att_parts.append((part, decode_any(fn) if fn else "attachment.bin"))
        elif ct == "text/html" and html_body is None:
            p = part.get_payload(decode=True)
            if p:
                html_body = p.decode(part.get_content_charset() or "utf-8", errors="replace")
        elif ct == "text/plain" and plain_body is None:
            p = part.get_payload(decode=True)
            if p:
                plain_body = p.decode(part.get_content_charset() or "utf-8", errors="replace")
    note = ""
    if category == "B":
        note = "【提示】此为更新版草单，此前版本作废，请以本邮件附件为准。"
    if extra_note:
        note = (note + "\n" + extra_note).strip()
    if html_body:
        if note:
            html_body = f'<p style="color:#c00;font-weight:bold">{note}</p>' + html_body
        out.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        text = ((note + "\n\n") if note else "") + (plain_body or "")
        out.attach(MIMEText(text, "plain", "utf-8"))
    for part, fn in att_parts:
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        np = MIMEBase(part.get_content_maintype(), part.get_content_subtype())
        np.set_payload(payload)
        encoders.encode_base64(np)
        np.add_header("Content-Disposition", "attachment", filename=("utf-8", "", fn))
        out.attach(np)
    # 主题前缀仅 B 类加 CATEGORY_LABEL（自旧发送处 :772 内聚; LIVE 门控时由处理器调用）
    if category == "B":
        out["Subject"] = f"{CATEGORY_LABEL.get(category, '')}{subject}"
    return out, subject


def test_subject(orig_subject, orig_to, label):
    """TEST 模式外发主题（LIVE 门控时由处理器调用; 格式同旧机器人 :767）。"""
    return f"[测试·{label}→原收件人:{orig_to}] {orig_subject}"
