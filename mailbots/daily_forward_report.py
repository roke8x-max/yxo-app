# -*- coding: utf-8 -*-
"""
每日转发汇总日报（2026-07-31 毛骁洋定规则）

规则:
  DSK / ATB / 草单 / 运单号 统一 ——
    · 转发了就企微通知（实时，各机器人内部完成）
    · 每天早上 8:00 给对应同事邮箱发一份汇总邮件备查，说明「昨天」的转发情况

行为:
  · 按同事分组，只给「昨天确实有转发」的同事发（0 条不打扰，与毛骁洋"有转发再发"的一贯要求一致）
  · 毛骁洋额外收一份全局总表（只要全公司昨天有任何转发）
  · 测试记录(test=1)默认不计入

用法:
  python daily_forward_report.py              # 汇总昨天，正常发送
  python daily_forward_report.py --day 2026-07-30
  python daily_forward_report.py --dry        # 只打印不发送
  python daily_forward_report.py --to-admin-only   # 全部只发毛骁洋（灰度/试运行用）
"""
import os
import sys
import json
import argparse
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "WeComBot"))

import forward_log  # noqa: E402
from config import ACCOUNTS, USER_EMAILS  # noqa: E402

SMTP_SERVER = "smtp.qiye.aliyun.com"
SMTP_PORT = 465
ADMIN_NAME = "毛骁洋"
ADMIN_MAILBOX = USER_EMAILS.get(ADMIN_NAME, "maoxiaoyang@cqtransit.com")
ROBOT_ORDER = ["DSK", "ATB", "草单", "运单号"]


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}")


def smtp_send(subject, body, to_list):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = ADMIN_MAILBOX
    msg["To"] = ", ".join(to_list)
    pwd = ACCOUNTS.get(ADMIN_MAILBOX)
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as s:
        s.login(ADMIN_MAILBOX, pwd)
        s.sendmail(ADMIN_MAILBOX, to_list, msg.as_string())


def _group_by_robot(rows):
    g = {}
    for r in rows:
        g.setdefault(r["robot"] or "?", []).append(r)
    return g


def build_person_body(owner, rows, day):
    by_robot = _group_by_robot(rows)
    lines = [f"{owner} 你好，",
             f"以下是 {day} （昨天）系统自动转发给你的邮件汇总，供你核对备查。", "",
             f"合计 {len(rows)} 封：" + "、".join(
                 f"{k} {len(v)} 封" for k, v in by_robot.items()), ""]
    for robot in ROBOT_ORDER:
        rs = by_robot.get(robot)
        if not rs:
            continue
        lines.append("=" * 46)
        lines.append(f"【{robot}】{len(rs)} 封")
        for r in rs:
            t = (r["ts"] or "")[11:16]
            seg = [f"  {t}"]
            if r["box"]:
                seg.append(f"箱号 {r['box']}")
            if r["code"]:
                seg.append(f"客编 {r['code']}")
            if r["company"]:
                seg.append(r["company"])
            lines.append("  ".join(seg))
            if r["subject"]:
                lines.append(f"        主题：{r['subject'][:70]}")
            if r["to_list"]:
                lines.append(f"        收件：{r['to_list'][:80]}")
            if r["note"]:
                lines.append(f"        备注：{r['note']}")
    lines += ["", "=" * 46,
              "本邮件由订舱助手每天 08:00 自动发送，仅作备查，无需回复。",
              "如发现漏转/错转，请在企微或微信里告诉订舱助手。"]
    return "\n".join(lines)


def build_admin_body(grouped, day, total, by_robot):
    lines = [f"{day} （昨天）全公司自动转发汇总", "",
             f"合计 {total} 封：" + "、".join(f"{k} {v} 封" for k, v in by_robot.items()), "",
             "按同事分组：", ""]
    for owner in sorted(grouped, key=lambda o: -len(grouped[o])):
        rows = grouped[owner]
        rb = _group_by_robot(rows)
        lines.append(f"  · {owner}：{len(rows)} 封（"
                     + "、".join(f"{k}{len(v)}" for k, v in rb.items()) + "）")
    lines += ["", "=" * 46, "明细：", ""]
    for owner in sorted(grouped, key=lambda o: -len(grouped[o])):
        lines.append(f"【{owner}】")
        for r in grouped[owner]:
            t = (r["ts"] or "")[11:16]
            lines.append(f"  {t}  [{r['robot']}] "
                         f"{r['box'] or r['code'] or '-'}  {r['company'] or ''}  "
                         f"{(r['subject'] or '')[:50]}")
        lines.append("")
    # 待回填时间戳队列自检（2026-07-31 芙蕾雅：DSK/ATB 时间戳漏写时落 pending_stamps.json）
    try:
        _pf = os.path.join(HERE, "pending_stamps.json")
        _pn = 0
        if os.path.exists(_pf):
            with open(_pf, "r", encoding="utf-8") as _f:
                _pn = len(json.load(_f))
        if _pn:
            lines.append(f"⚠ 待回填时间戳队列仍有 {_pn} 条未消（DSK/ATB 时间戳未写回飞书总表），"
                         f"请核对飞书总表对应箱号，或重启机器人后下一轮自动补写。")
    except Exception:
        pass

    lines.append("本邮件由订舱助手每天 08:00 自动发送。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="汇总哪一天 YYYY-MM-DD，默认昨天")
    ap.add_argument("--dry", action="store_true", help="只打印不发送")
    ap.add_argument("--to-admin-only", action="store_true",
                    help="全部只发毛骁洋（灰度/试运行）")
    ap.add_argument("--include-test", action="store_true", help="把测试记录也算进来")
    args = ap.parse_args()

    day = args.day or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    grouped = forward_log.fetch_day(day, include_test=args.include_test)
    total, by_robot = forward_log.stats_day(day, include_test=args.include_test)

    log(f"汇总日期 {day} | 共 {total} 条 | {by_robot or '无'}")
    if not total:
        log("昨天没有任何转发，按规则不发送任何邮件。")
        return

    # 1) 逐个同事
    for owner, rows in grouped.items():
        addr = USER_EMAILS.get(owner)
        if not addr:
            log(f"  ⏭ {owner} 无邮箱映射，其 {len(rows)} 条并入毛骁洋总表")
            continue
        target = ADMIN_MAILBOX if args.to_admin_only else addr
        subject = f"[转发日报] {day} 你共收到 {len(rows)} 封自动转发邮件"
        if args.to_admin_only:
            subject = f"[代收·{owner}] " + subject
        body = build_person_body(owner, rows, day)
        if args.dry:
            log(f"  （演练）→ {owner} <{target}> {len(rows)} 条")
            continue
        try:
            smtp_send(subject, body, [target])
            log(f"  ✅ {owner} → {target}（{len(rows)} 条）")
        except Exception as e:
            log(f"  ❌ {owner} 发送失败: {e}")

    # 2) 毛骁洋全局总表
    subject = f"[转发日报·总表] {day} 全公司共转发 {total} 封"
    body = build_admin_body(grouped, day, total, by_robot)
    if args.dry:
        log(f"  （演练）→ 总表 <{ADMIN_MAILBOX}>")
        print("\n" + "-" * 60 + "\n" + body[:1500])
        return
    try:
        smtp_send(subject, body, [ADMIN_MAILBOX])
        log(f"  ✅ 总表 → {ADMIN_MAILBOX}")
    except Exception as e:
        log(f"  ❌ 总表发送失败: {e}")


if __name__ == "__main__":
    main()
