# -*- coding: utf-8 -*-
"""SMTP 发送语义（spec §3 sending.py）：
成功定义 = smtplib.sendmail() 返回空 dict。部分拒收不抛异常，必须检查返回值。"""
import smtplib
from dataclasses import dataclass
from core import paths


@dataclass(frozen=True)
class SendResult:
    ok: bool
    delivered: tuple = ()
    refused: tuple = ()
    error: str = ""


def send_smtp(msg, sender_email, sender_pwd, to_list, cc_list=()):
    tos = [a for a in list(to_list) + list(cc_list) if a]
    if not tos:
        return SendResult(ok=False, error="no_recipients")
    host, port = paths.smtp_endpoint()
    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(sender_email, sender_pwd)
            refused = s.sendmail(sender_email, tos, msg.as_string())
    except Exception as e:
        return SendResult(ok=False, error=repr(e))
    delivered = tuple(t for t in tos if t not in refused)
    return SendResult(ok=not refused, delivered=delivered,
                      refused=tuple(refused.keys()))
