# -*- coding: utf-8 -*-
"""只读：看被 classify 忽略的邮件到底长什么样，确认有无漏判。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Waybill_Robot as w

all_items = []
for addr in w.MAILBOXES:
    pwd = w.ACCOUNTS.get(addr)
    if pwd:
        all_items.extend(w.scan_mailbox(addr, pwd, w.WAYBILL_IMAP_FOLDER))
seen, items = set(), []
for it in all_items:
    if it["message_id"] and it["message_id"] not in seen:
        seen.add(it["message_id"]); items.append(it)

n = 0
for it in items:
    if w.classify(it["subject"], it["sender"], it["atts"], it["body"]):
        continue
    n += 1
    print("=" * 68)
    print(f"[{n}] 邮箱={it['mailbox']}  日期={it['date']}")
    print(f"  发件人: {it['sender']}")
    print(f"  主题  : {it['subject'][:100]}")
    print(f"  附件  : {it['atts']}")
    print(f"  正文  : {(it['body'] or '')[:260].strip()}")
print(f"\n共 {n} 封被忽略")
