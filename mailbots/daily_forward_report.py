# -*- coding: utf-8 -*-
"""每日转发汇总 —— 现在**只有一封**（任务 16）。

历史（已废止）：原来是「每位同事一份个人版 + 毛骁洋一份全局总表」两批邮件，
每天早上 8:00 由计划任务拉起。

任务 16 起（洋已拍板，方向不再讨论）：
  · 个人版**停发** —— 洋因负责太平洋/港九港铁才会收到其中一封，那封正是"两封"的
    来源之一；它包含的信息本来就在总表「按同事分组 + 全部明细」里，停发不丢信息。
  · 全局总表是**唯一那一封**，但正文**从头重写**为五节结构
    （唯一真相源：任务包 `06_EMAIL-DESIGN.md`），并把原企微 09:00 digest、
    12:00 归档汇总、未分类/配置缺失逐封告警、待办汇总、程序错误全部并入。
  · 收件人只有洋；**每天 12:30**（等 12:00 归档核对跑完）；当日 0 转发 0 异常也发短报。

`build_admin_body()` / `build_person_body()` 已不再是邮件文本骨架：
`build_admin_body()` 保留仅作**取数/对照**（任务书 2.2：旧函数只作为取数来源），
**不要**把它接回发送路径。真正的正文生成在
`mailbots_next/core/daily_report.py`（唯一实现）。

用法:
  python daily_forward_report.py                    # 到点（>=12:30）才发；未到点直接退出
  python daily_forward_report.py --dry              # 只打印正文，不发、不写"已发"标记
  python daily_forward_report.py --day 2026-11-09   # 转发明细改取指定日期（人工补查）
  python daily_forward_report.py --force            # 立刻发（人工补发；覆盖当天"已发"标记）
"""
import os
import sys
import json
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# 每日邮件的唯一实现在 mailbots_next 里。🔴 必须 **append** 仓库根，不能用
# insert(0)：仓库根也有 config.py，插到最前面会把 `mailbots/config.py` 顶掉。
if ROOT not in sys.path:
    sys.path.append(ROOT)

# 不再 import WeComBot/config：那会把「读企微 secrets.json」变成本脚本的启动硬依赖
# （缺文件直接 RuntimeError），而发信早已交给 mailbots_next（读它自己的凭证）。


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}")


# ---------------------------------------------------------------- 旧正文（仅取数/对照）
#
# 🔴 任务 16：`build_admin_body()` 不再是邮件文本骨架（新正文是五节的
# `mailbots_next/core/daily_report.py`）。保留它是因为验收要拿旧输出做对照，
# 也便于运维核对"按同事分组 + 全部明细"的口径没变。**不要接回发送路径。**

def _group_by_robot(rows):
    g = {}
    for r in rows:
        g.setdefault(r["robot"] or "?", []).append(r)
    return g


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

    lines.append("（本函数自任务 16 起仅作取数/对照，不再作为邮件文本骨架；正式正文见 mailbots_next/core/daily_report.py）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 发送（唯一那一封）

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="转发明细取哪一天 YYYY-MM-DD，默认昨天")
    ap.add_argument("--dry", action="store_true", help="只打印正文，不发送")
    ap.add_argument("--force", action="store_true",
                    help="立刻发送（不等到 12:30、忽略当天已发标记；人工补发用）")
    args = ap.parse_args()

    from mailbots_next.config import OPS_OWNER_EMAIL
    from mailbots_next.core.daily_report import send_daily_report

    res = send_daily_report(day=args.day, force=args.force, dry=args.dry)
    reason = res.get("reason")
    if args.dry:
        print(res.get("body") or "")
        return 0
    if reason == "not_due":
        log("未到 12:30，今日汇总邮件不在此刻发送（要立即发请加 --force）。")
        return 0
    if reason == "already_sent":
        log("今天这封已经发过了（每天恰好一封），本次跳过。")
        return 0
    if reason == "sent":
        log(f"✅ 已发送 → {OPS_OWNER_EMAIL} | {res.get('subject')}")
        return 0
    log(f"❌ 发送失败：{reason}（详见日志）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
