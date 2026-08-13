# -*- coding: utf-8 -*-
"""
公网出口 IP 监控：每 5 分钟检查一次，IP 变化时立即发邮件告警，
提醒去企业微信后台更新「企业可信 IP」。
邮件通道走 SMTP，不受企微可信 IP 限制，任何时候都能送达。
"""
import os
import sys
import json
import time
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\YXO_DATA\MailBots")
from common_io import atomic_write_json
from config import SMTP_SERVER, SMTP_PORT, ACCOUNTS, BOT_LOG_DIR

import requests

ALERT_TO = "maoxiaoyang@cqtransit.com"
SENDER = "maoxiaoyang@cqtransit.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "last_ip.json")
CHECK_INTERVAL = 300  # 5 分钟

IP_SOURCES = [
    ("http://members.3322.org/dyndns/getip", "text"),
    ("https://myip.ipip.net/ip", "text"),
    ("https://api.ipify.org", "text"),
]


def log(text):
    os.makedirs(BOT_LOG_DIR, exist_ok=True)
    path = os.path.join(BOT_LOG_DIR, f"ipwatch_{datetime.now().strftime('%Y-%m-%d')}.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {text}\n")


def get_public_ip():
    for url, _ in IP_SOURCES:
        try:
            r = requests.get(url, timeout=8)
            ip = r.text.strip()
            # 简单校验
            parts = ip.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                return ip
        except Exception:
            continue
    return None


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ip": None, "history": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    atomic_write_json(STATE_FILE, state)


def send_alert(old_ip, new_ip, history):
    hist_lines = "\n".join(
        f"  {h['time']}  {h['ip']}" for h in history[-15:]
    )
    body = f"""服务器公网出口 IP 发生变化！

旧 IP: {old_ip}
新 IP: {new_ip}
时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

企微机器人主动回复将失效（错误码 60020），请尽快处理：

1. 打开 企业微信管理后台 → 应用管理 → 自动化助手
2. 找到「企业可信 IP」→ 编辑
3. 追加新 IP（用分号分隔）: {new_ip}

近期 IP 历史（可参考保留常用的几个）:
{hist_lines}

—— 本邮件由 ip_watchdog 自动发送
"""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(f"【企微机器人】出口IP变更: {new_ip}，需更新可信IP", "utf-8")
    msg["From"] = formataddr(("IP监控", SENDER))
    msg["To"] = ALERT_TO
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
            s.login(SENDER, ACCOUNTS[SENDER])
            s.sendmail(SENDER, [ALERT_TO], msg.as_string())
        log(f"alert mail sent: {old_ip} -> {new_ip}")
        return True
    except Exception as e:
        log(f"alert mail FAILED: {e}")
        return False


def main():
    # 单实例锁：绑定本地端口 5099，重复启动直接退出（供 bat 用 netstat 判断）
    import socket
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 5099))
        lock.listen(1)
    except OSError:
        return  # 已有实例在跑
    log("ip_watchdog started")
    while True:
        try:
            ip = get_public_ip()
            if ip:
                state = load_state()
                known = set(state.get("known", []))
                if state["ip"] != ip:
                    old = state["ip"]
                    state["history"].append(
                        {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "ip": ip})
                    state["history"] = state["history"][-50:]
                    state["ip"] = ip
                    is_new = ip not in known
                    known.add(ip)
                    state["known"] = sorted(known)
                    save_state(state)
                    log(f"IP changed: {old} -> {ip} (new={is_new})")
                    # 双线路会在已知 IP 间来回跳，只有出现全新 IP 才告警
                    if old and is_new:
                        send_alert(old, ip, state["history"])
            else:
                log("get_public_ip failed on all sources")
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
