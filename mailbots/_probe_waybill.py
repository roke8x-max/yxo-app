# -*- coding: utf-8 -*-
"""只读探查：4 邮箱「运单号」文件夹有什么邮件、分类、匹配到哪个公司/同事。
不发信、不入队、不写台账、不标已读。"""
import sys, os, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Waybill_Robot as w

records = w.load_records()
print("yxo.db 记录:", len(records))

all_items = []
for addr in w.MAILBOXES:
    pwd = w.ACCOUNTS.get(addr)
    if not pwd:
        print("  无密码跳过", addr); continue
    all_items.extend(w.scan_mailbox(addr, pwd, w.WAYBILL_IMAP_FOLDER))

seen, items = set(), []
for it in all_items:
    if it["message_id"] and it["message_id"] not in seen:
        seen.add(it["message_id"]); items.append(it)
print(f"\n跨邮箱去重后 {len(items)} 封\n" + "=" * 70)

senders = collections.Counter(i["sender"] for i in items)
print("发件人分布:", dict(senders))

stat = collections.Counter()
detail = []
for it in items:
    cat = w.classify(it["subject"], it["sender"], it["atts"], it["body"])
    stat[cat or "忽略"] += 1
    if not cat:
        continue
    if cat == "A":
        xb = w.get_attachment_bytes(it, w.XLS_RE)
        rows = w.parse_waybill_xls(xb) if xb else []
        code = rows[0]["客户编码"] if rows else ""
        box = rows[0]["箱号"] if rows else ""
    else:
        rows, box = [], ""
        code = w.extract_code_from_body(it["body"])
    num = w.CODE_NUM_RE.match(code).group(1) if (code and w.CODE_NUM_RE.match(code)) else ""
    level, rec = w.match_record(records, code, num, box)
    company = rec["company"] if rec else ""
    owner = w.company_to_name(company) if company else None
    detail.append({"cat": cat, "subject": it["subject"][:48], "date": it["date"],
                   "code": code, "box": box, "rows": len(rows), "company": company,
                   "owner": owner or "(未匹配)", "level": level or "none",
                   "mailbox": it["mailbox"], "mid": it["message_id"]})

print("分类统计:", dict(stat))
print("=" * 70)
by_owner = collections.defaultdict(lambda: collections.defaultdict(list))
for d in detail:
    by_owner[d["owner"]][d["cat"]].append(d)

for owner in sorted(by_owner):
    print(f"\n【{owner}】")
    for cat in ("A", "B"):
        lst = by_owner[owner][cat]
        print(f"  {cat} 类 {len(lst)} 封")
        for d in lst[:4]:
            print(f"    - {d['date']} | 公司={d['company'] or '-'} | 客编={d['code'] or '-'} "
                  f"| 箱={d['box'] or '-'} | 行数={d['rows']} | 匹配={d['level']}")
            print(f"      主题: {d['subject']}")
