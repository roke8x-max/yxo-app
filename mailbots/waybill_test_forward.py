# -*- coding: utf-8 -*-
"""
运单号机器人 · 定向 TEST 转发（2026-07-31 毛骁洋指定）

规则（严格遵守）:
  · 每个同事各取「一家对应单位」的邮件, A 类(运单号下发/正常) 与 B 类(单证审核驳回/待确认) 各一封
  · 转发目标只有 3841559246@qq.com（毛骁洋 QQ 邮箱）, 绝不发同事、绝不发原始收件人
  · 企微通知只发毛骁洋
  · 全程不写 yxo.db、不写运单号台账(ledger) —— 保证将来 LIVE 时这些邮件不会被当成"已处理"跳过
  · 入 pending 队列时一律 test=1, 这样即使回复「确认」, draft_pending.confirm() 也只转发到验证邮箱且不写库

用法:
  python waybill_test_forward.py --dry     # 只演练, 不发任何东西
  python waybill_test_forward.py           # 实际执行
  python waybill_test_forward.py --clear   # 清空待确认列表(测试收尾用)
"""
import sys, os, time, argparse, collections
from datetime import datetime
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import Waybill_Robot as w
import draft_pending

QQ = "3841559246@qq.com"


def pick_targets(items, records):
    """按同事分组, 每人挑 一家公司 的 A 类 1 封 + B 类 1 封。"""
    parsed = []
    for it in items:
        cat = w.classify(it["subject"], it["sender"], it["atts"], it["body"])
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
        parsed.append({"it": it, "cat": cat, "code": code, "box": box, "rows": rows,
                       "company": company, "owner": owner, "level": level or "none"})

    staff = ["毛骁洋", "杨雅雯", "冯茜", "韩文豪"]
    plan = []
    # --- A 类: 每人取一家公司最新的一封 ---
    for name in staff:
        mine = [p for p in parsed if p["cat"] == "A" and p["owner"] == name and p["company"]]
        if not mine:
            plan.append({"owner": name, "cat": "A", "miss": "该同事名下无 A 类真实邮件"})
            continue
        # 固定挑一家公司: 取该同事邮件最多的那家, 再取其中最新一封
        top_company = collections.Counter(p["company"] for p in mine).most_common(1)[0][0]
        same = sorted([p for p in mine if p["company"] == top_company],
                      key=lambda p: p["it"]["date"], reverse=True)
        plan.append(dict(same[0], owner=name, demo=False))

    # --- B 类: 优先本人真实邮件; 没有则借一封真实 B 类做链路演示(明确标注) ---
    used_mid = set()
    spare = [p for p in parsed if p["cat"] == "B"]
    for name in staff:
        mine = [p for p in spare if p["owner"] == name and p["it"]["message_id"] not in used_mid]
        if mine:
            pick = sorted(mine, key=lambda p: p["it"]["date"], reverse=True)[0]
            used_mid.add(pick["it"]["message_id"])
            plan.append(dict(pick, owner=name, demo=False))
            continue
        rest = [p for p in spare if p["it"]["message_id"] not in used_mid]
        if rest:
            pick = sorted(rest, key=lambda p: p["it"]["date"], reverse=True)[0]
            used_mid.add(pick["it"]["message_id"])
            plan.append(dict(pick, owner=name, demo=True,
                             demo_note=f"该同事名下暂无真实 B 类邮件，此封原客编 {pick['code'] or '-'} "
                                       f"未匹配到公司（真实归属：{pick['owner'] or '未匹配→兜底给毛骁洋'}），"
                                       f"仅用于演示 B 类链路。"))
        else:
            plan.append({"owner": name, "cat": "B", "miss": "无可用 B 类邮件"})
    return plan


def build_test_mail(p, notify_text):
    """把原件重新打包成测试转发邮件（保留正文与附件），顶部加测试说明。"""
    fwd, subj = w_build_forward(p["it"]["raw"])
    tag = "正常·A类运单号下发" if p["cat"] == "A" else "待确认·B类单证审核驳回"
    head = [
        "=" * 58,
        "【运单号机器人 · 测试转发，请勿转给同事】",
        f"本封在正式运行时应发给：{p['owner']}（{p['company'] or '未匹配公司'}）",
        f"情况类型：{tag}",
        f"客户编码：{p.get('code') or '-'}    匹配级别：{p.get('level')}",
        f"原始邮件日期：{p['it']['date']}    落在邮箱：{p['it']['mailbox']}",
    ]
    if p.get("demo"):
        head.append(f"⚠ 链路演示：{p.get('demo_note')}")
    if p["cat"] == "A" and p.get("rows"):
        head.append(f"解析出 {len(p['rows'])} 条箱号→运单号：")
        for r in p["rows"][:10]:
            head.append(f"    {r.get('箱号') or '-'} → {r.get('运单号') or '-'}")
    head += ["", "—— 同事将在企微/微信收到的通知原文 ——", notify_text,
             "=" * 58, "", "以下为原始邮件内容：", ""]
    fwd.attach(MIMEText("\n".join(head), "plain", "utf-8"))
    # 说明块放最前
    fwd.set_payload([fwd.get_payload()[-1]] + fwd.get_payload()[:-1])
    return fwd, subj


def w_build_forward(raw_bytes):
    """复用草单机器人的转发打包逻辑（正文+附件完整保留）。"""
    import Draft_Forward_Robot as dfr
    return dfr.build_forward(raw_bytes, category=None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只演练不发送")
    ap.add_argument("--clear", action="store_true", help="清空待确认列表")
    args = ap.parse_args()

    if args.clear:
        conn = draft_pending._conn()
        n = conn.execute("SELECT COUNT(*) FROM pending_queue").fetchone()[0]
        conn.execute("DELETE FROM pending_queue")
        conn.commit()
        conn.close()
        print(f"✅ 已清空待确认列表，共删除 {n} 条")
        return

    print("=" * 66)
    print(f"运单号机器人 · 定向测试转发 {'[演练]' if args.dry else '[执行]'} {datetime.now()}")
    print(f"转发目标: {QQ} （只发这一个地址）")
    print("=" * 66)

    records = w.load_records()
    all_items = []
    for addr in w.MAILBOXES:
        pwd = w.ACCOUNTS.get(addr)
        if pwd:
            all_items.extend(w.scan_mailbox(addr, pwd, w.WAYBILL_IMAP_FOLDER))
    seen, items = set(), []
    for it in all_items:
        if it["message_id"] and it["message_id"] not in seen:
            seen.add(it["message_id"]); items.append(it)
    print(f"去重后 {len(items)} 封\n")

    plan = pick_targets(items, records)
    admin_pwd = w.ACCOUNTS.get(w.ADMIN_MAILBOX)
    sent, results = 0, []

    for p in plan:
        if p.get("miss"):
            print(f"⏭ {p['owner']} {p['cat']} 类跳过：{p['miss']}")
            results.append(f"⏭ {p['owner']} / {p['cat']} 类 —— {p['miss']}")
            continue
        pinfo = {"category": "WAY_A" if p["cat"] == "A" else "WAY_B",
                 "code": p["code"], "box_count": len(p.get("rows") or []),
                 "rows": p.get("rows") or []}
        notify_text = w.build_waybill_notify(pinfo, test_mode=True)
        label = f"{p['owner']}·{p['company'] or '未匹配'}·{p['cat']}类"
        print(f"▶ {label} | 客编 {p.get('code') or '-'} | {p['it']['date']}"
              + ("  [链路演示]" if p.get("demo") else ""))
        if args.dry:
            results.append(f"（演练）{label}")
            continue
        try:
            fwd, subj = build_test_mail(p, notify_text)
            fwd["Subject"] = f"[运单号TEST·{label}] {subj}"
            w.smtp_send(fwd, w.ADMIN_MAILBOX, admin_pwd, [QQ])
            # 入待确认队列（test=1 → 即使回复"确认"也只通知、不写库）
            n = draft_pending.add_pending({
                "message_id": p["it"]["message_id"], "subject": subj,
                "sender": p["it"]["sender"], "date": p["it"]["date"],
                "category": "WAY_A" if p["cat"] == "A" else "WAY_B",
                "code": p["code"], "num": "", "box": p.get("box") or "",
                "company": p["company"], "owner": p["owner"],
                "reason": ("运单号下发, 待确认转发" if p["cat"] == "A" else "单证审核被拒, 需重报"),
                "candidates": [], "boxes_seen": [p["it"]["mailbox"]],
                "to": [], "cc": [],
            }, p["it"]["raw"], test=True)
            # 企微通知只发毛骁洋
            wx = (f"【运单号机器人测试】本条正式运行时发给 {p['owner']}"
                  f"（{p['company'] or '未匹配'}）\n"
                  + ("⚠ 链路演示，非该同事真实邮件\n" if p.get("demo") else "")
                  + f"待确认编号 #{n}\n" + "-" * 20 + "\n" + notify_text)
            ok, ch = w.wecom_notify_person(w.ADMIN_NAME, wx)
            sent += 1
            print(f"   ✅ 已转发 {QQ} | 待确认#{n} | 企微{'✅' if ok else '❌'}({ch or '-'})")
            results.append(f"✅ {label} → 邮件已发 / 待确认#{n} / 企微{'成功' if ok else '失败'}")
            time.sleep(1.2)
        except Exception as e:
            print(f"   ❌ 失败: {e}")
            results.append(f"❌ {label} —— {e}")

    if not args.dry:
        try:
            body = ["运单号机器人 · 定向测试转发汇总",
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"实际转发 {sent} 封，全部只发送至 {QQ}", "",
                    "明细:"] + ["  " + r for r in results] + [
                    "", "说明:",
                    "  · 本次全程未写 yxo.db，未写运单号台账，正式启用时这些邮件仍会被正常处理",
                    "  · 待确认单均标记 test=1：即使回复「确认」也只转发到本 QQ 邮箱且不写库",
                    "  · 测试结束请执行: python waybill_test_forward.py --clear 清空待确认列表"]
            m = MIMEText("\n".join(body), "plain", "utf-8")
            m["Subject"] = f"[运单号测试] 定向转发汇总 {sent} 封 {datetime.now().strftime('%H:%M')}"
            w.smtp_send(m, w.ADMIN_MAILBOX, admin_pwd, [QQ])
            print(f"\n📧 汇总邮件已发 {QQ}")
            ok, ch = w.wecom_notify_person(
                w.ADMIN_NAME,
                f"✅ 运单号机器人测试转发完成\n实际转发 {sent} 封，全部只发到你的QQ邮箱\n"
                f"待确认队列已生成对应条目（均为测试单，回复确认不会写库）\n"
                f"详情见 QQ 邮箱汇总邮件。")
            print(f"📲 企微汇总: {'✅' if ok else '❌'} ({ch or '-'})")
        except Exception as e:
            print(f"⚠ 汇总发送失败: {e}")

    print("=" * 66)
    print(f"完成，实际转发 {sent} 封")


if __name__ == "__main__":
    main()
