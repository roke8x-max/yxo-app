#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫描 4 个 IMAP 账户的全部文件夹，统计：
  1) 缺失 Message-ID 的邮件（及其标题）
  2) 重复 Message-ID 的邮件（同一 ID 出现 >1 次，及其位置/标题）

用途：为"去重是否纯以 Message-ID 为准"这一决策提供真实数据。
只读操作：所有文件夹以 readonly=True 选中，不修改任何邮件标记/状态。

运行环境要求：
  - Python 3
  - 同目录（或 sys.path）存在 config_local.py，提供 ACCOUNTS = {邮箱: 密码}
  - 网络可达 imap.qiye.aliyun.com:993

用法：
  python scan_msgid.py
"""
import imaplib
import email
import sys
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from config_local import ACCOUNTS  # {email_addr: password}
except Exception as e:
    print("无法导入 ACCOUNTS（需要 config_local.py 提供 IMAP 密码）：%s" % e)
    sys.exit(1)

IMAP_SERVER = "imap.qiye.aliyun.com"
IMAP_PORT = 993


def _decode_folder_name(raw: str) -> str:
    """从 IMAP LIST 响应里取出文件夹名（最后一个被引号包裹的字段）。"""
    raw = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    # LIST 形如: (\HasNoChildren) "/" "INBOX"
    # 或带空格: (\HasNoChildren) "/" "My Folder"
    parts = raw.split('"')
    # 引号成对出现，文件夹名是最后一个偶数索引段
    quoted = [p for i, p in enumerate(parts) if i % 2 == 1]
    return quoted[-1] if quoted else ""


def get_msgid_and_subject(conn, uid: bytes):
    typ, data = conn.fetch(uid, "(BODY.PEEK[HEADER])")
    if typ != "OK" or not data:
        return None, None
    raw = data[0][1]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    msg = email.message_from_string(raw)
    mid = (msg.get("Message-ID") or "").strip()
    subj = (msg.get("Subject") or "").strip()
    return mid, subj


def scan_account(addr: str, pwd: str):
    rows = []  # (folder, uid, mid, subj)
    conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=120)
    conn.login(addr, pwd)
    typ, listing = conn.list()
    if typ == "OK" and listing:
        for f in listing:
            name = _decode_folder_name(f)
            if not name:
                continue
            try:
                conn.select(name, readonly=True)
            except Exception as e:
                print("    [!] 文件夹 %r 选中失败: %s" % (name, e), flush=True)
                continue
            try:
                typ2, data = conn.search(None, "ALL")
            except Exception:
                conn.close()
                continue
            if typ2 != "OK" or not data or not data[0]:
                conn.close()
                continue
            uids = data[0].split()
            for uid in uids:
                mid, subj = get_msgid_and_subject(conn, uid)
                rows.append((name, uid.decode(), mid, subj))
            conn.close()
    conn.logout()
    return rows


def main():
    all_msgs = []  # (addr, folder, uid, mid, subj)
    for addr, pwd in ACCOUNTS.items():
        print("[*] 扫描账户 %s ..." % addr, flush=True)
        try:
            rows = scan_account(addr, pwd)
            for folder, uid, mid, subj in rows:
                all_msgs.append((addr, folder, uid, mid, subj))
            print("    本账户扫描到 %d 封" % len(rows), flush=True)
        except Exception as e:
            print("  [!] 账户 %s 扫描失败: %s" % (addr, e), flush=True)

    total = len(all_msgs)
    missing = [m for m in all_msgs if not m[3]]
    by_mid = defaultdict(list)
    for m in all_msgs:
        if m[3]:
            by_mid[m[3]].append(m)
    dupes = {mid: ms for mid, ms in by_mid.items() if len(ms) > 1}

    print("\n==================== 扫描报告 ====================")
    print("总邮件数: %d" % total)
    print("缺失 Message-ID: %d 封" % len(missing))
    print("重复 Message-ID: %d 个 ID，涉及 %d 封邮件"
          % (len(dupes), sum(len(v) for v in dupes.values())))

    print("\n--- 缺失 Message-ID 的邮件（标题） ---")
    if not missing:
        print("  （无）")
    for addr, folder, uid, mid, subj in missing:
        print("  [%s / %s] %r" % (addr, folder, subj))

    print("\n--- 重复 Message-ID（ID -> 出现位置/标题） ---")
    if not dupes:
        print("  （无）")
    for mid, ms in dupes.items():
        print("  ID=%s" % mid)
        for addr, folder, uid, m2, subj in ms:
            print("    [%s / %s] %r" % (addr, folder, subj))


if __name__ == "__main__":
    main()
