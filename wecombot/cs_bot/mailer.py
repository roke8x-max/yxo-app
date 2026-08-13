# -*- coding: utf-8 -*-
"""
邮件草稿与发送：SMTP(smtp.qiye.aliyun.com:465)，发件人按操作人。
"""
import os
import sys
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.header import Header
from email.utils import formataddr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SMTP_SERVER, SMTP_PORT, ACCOUNTS, USER_EMAILS, DEFAULT_TO, DEFAULT_CC, sender_for_company

DEFAULT_BODY_TMPL = "您好，\n附件为 {train} 班列 {code} / {box} 随车资料，请查收。"


def build_draft(rec, operator, attachments):
    """生成邮件草稿 dict。发件人按记录所属公司的负责同事（与邮件转发一致）。"""
    train = rec.get("班列号") or ""
    code = rec.get("客户编码") or ""
    box = rec.get("箱号") or ""
    subject = "_".join(x for x in (train, code, box) if x) + "_随车资料"
    ref = f"{code} / {box}" if box else code
    body = f"您好，\n附件为 {train} 班列 {ref} 随车资料，请查收。"
    # 发件人：优先用箱号所属公司的负责同事邮箱，否则退回操作人
    sen = sender_for_company(rec.get("开票子公司名称"))
    sender = sen[0] if (sen and sen[1]) else USER_EMAILS.get(operator, list(ACCOUNTS.keys())[0])
    return {
        "code": code,
        "rec_id": rec.get("id"),
        "subject": subject,
        "body": body,
        "to": [DEFAULT_TO],
        "cc": [DEFAULT_CC],
        "sender": sender,
        "operator": operator,
        "attachments": list(attachments),   # 本地文件路径列表
    }


def preview(draft):
    """草稿预览文本。"""
    lines = [
        "📧 邮件草稿预览",
        f"发件：{draft['sender']}",
        f"收件：{', '.join(draft['to'])}",
        f"抄送：{', '.join(draft['cc']) if draft['cc'] else '—'}",
        f"主题：{draft['subject']}",
        "正文：",
        draft["body"],
        f"附件（{len(draft['attachments'])} 个）：",
    ]
    if draft["attachments"]:
        for i, p in enumerate(draft["attachments"], 1):
            lines.append(f"  {i}. {os.path.basename(p)}")
    else:
        lines.append("  （无）")
    lines.append("")
    lines.append("可用指令：更新正文 xx / 更新收件 xx / 更新抄送 xx / 删除附件 N / "
                 "直接发文件=新增附件 / 查看文件夹 / 确认发送 / 取消")
    return "\n".join(lines)


def send(draft):
    """发送草稿。返回 (ok, err_msg)。"""
    sender = draft["sender"]
    password = ACCOUNTS.get(sender)
    if not password:
        return False, f"未配置发件账号 {sender} 的密码"

    msg = MIMEMultipart()
    msg["From"] = formataddr((str(Header(sender.split("@")[0], "utf-8")), sender))
    msg["To"] = ", ".join(draft["to"])
    if draft["cc"]:
        msg["Cc"] = ", ".join(draft["cc"])
    msg["Subject"] = Header(draft["subject"], "utf-8")
    msg.attach(MIMEText(draft["body"], "plain", "utf-8"))

    for path in draft["attachments"]:
        if not os.path.isfile(path):
            return False, f"附件不存在：{os.path.basename(path)}"
        with open(path, "rb") as f:
            part = MIMEApplication(f.read())
        part.add_header("Content-Disposition", "attachment",
                        filename=Header(os.path.basename(path), "utf-8").encode())
        msg.attach(part)

    recipients = draft["to"] + draft["cc"]
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        return True, ""
    except Exception as e:
        return False, str(e)
