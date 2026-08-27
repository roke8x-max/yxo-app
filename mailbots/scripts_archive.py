#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""yxo.db 历史表按月归档 CLI（spec §6 / 刀3）。
用法：
  python scripts_archive.py --db D:\...\yxo.db \
      --tables tracing_log,forward_log --date-col ts --keep-days 90 [--dry-run|--apply]
默认 dry-run。--apply 才真正搬运。"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import events_store  # noqa: E402
import sqlite3  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--tables", default="tracing_log,forward_log")
    ap.add_argument("--date-col", required=True,
                    help="时间列名（tracing_log=log_date, forward_log=ts）")
    ap.add_argument("--keep-days", type=int, default=90)
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将搬运行数，不写库（默认行为）")
    ap.add_argument("--apply", action="store_true")
    # 显式传入 argv 时按 sys.argv 惯例含脚本名，去掉首元素再解析
    rest = list(argv[1:]) if argv else None
    args = ap.parse_args(rest)

    conn = sqlite3.connect(args.db, timeout=30)
    report = {}
    for t in [x.strip() for x in args.tables.split(",") if x.strip()]:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (t,)).fetchone() is not None
        if not exists:
            report[t] = "表不存在，跳过"
            continue
        if args.apply:
            moved = events_store.archive_before(conn, t, args.date_col,
                                                keep_days=args.keep_days)
        else:
            cutoff_sql = ("SELECT COUNT(*) FROM {t} WHERE date(\"{c}\") < "
                          "date('now', ?)").format(t=t, c=args.date_col)
            moved = conn.execute(
                cutoff_sql, ("-{} days".format(args.keep_days),)).fetchone()[0]
        report[t] = moved
    conn.close()
    for t, n in report.items():
        print("{}: {} {} 行".format("APPLIED" if args.apply else "DRY-RUN", t, n))
    return str(report)


if __name__ == "__main__":
    main()
