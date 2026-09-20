# -*- coding: utf-8 -*-
"""公司收件人初始化（T2，上线阻断项）。

把生产 yxo.db.bot_config(scope='company') 的 7 家镜像进
mailbots_next 自有 bot_config.db(bot='all', scope='company')，
再补第 8 家「联运」（洋 2026-09-09 确认真值）。

- 默认 --dry-run：只打印要写什么，不落库；--apply 才真正写入。
- 幂等且绝不覆盖已有行（ON CONFLICT DO NOTHING）；--force 才覆盖（打印旧值）。
- 只读打开生产库；INSERT 不带 source 列（本地表没有该列，也不加列）。
- 用法（服务器）：python scripts/seed_company_recipients.py --apply
"""
import argparse
import json
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YXO_DB_DEFAULT = r"\\10.0.199.184\yxo_data\yxo_app\data\yxo.db"
BOT_DB_DEFAULT = os.path.join(REPO_ROOT, "mailbots_next", "data", "bot_config.db")

# 洋 2026-09-09 确认（非占位）；出处 docs/2026-09-09-联运改造-数据灌入执行单.md §2.2
LIANYUN_TO = ["gongqilin@cqjzxly.cn", "yuyanling@cqjzxly.cn",
              "jjb@cqjzxly.cn", "wanglu@cqjzxly.cn"]
LIANYUN_CC = ["3841559246@qq.com"]


def mask_email(addr):
    """脱敏：a***@domain（本仓日志规范：收件人邮箱掩码）。"""
    if not addr or "@" not in addr:
        return "***"
    local, domain = addr.split("@", 1)
    return (local[:1] + "***@" + domain) if local else "***@" + domain


def _union_keep_order(*lists):
    out = []
    for lst in lists:
        for item in lst or []:
            if item and item not in out:
                out.append(item)
    return out


def _parse_addrs(text):
    try:
        data = json.loads(text or "[]")
    except Exception:
        return []
    return [a for a in data if isinstance(a, str) and a] if isinstance(data, list) else []


def load_source_companies(yxo_db_path):
    """只读打开生产 yxo.db，按 key 合并各 bot 的 to/cc（并集+去重+保序）。
    返回 {公司名: {"to": [...], "cc": [...]}}。读不到返回 {}（调用方决定退出码）。"""
    companies = {}
    try:
        conn = sqlite3.connect(f"file:{yxo_db_path}?mode=ro", uri=True)
    except Exception as e:
        print(f"[种子] 打不开生产库（只读）: {yxo_db_path}: {e}")
        return {}
    try:
        try:
            rows = conn.execute(
                "SELECT bot, scope, key, to_addrs, cc_addrs FROM bot_config WHERE scope='company'"
            ).fetchall()
        except Exception as e:
            print(f"[种子] 读取 bot_config 失败: {e}")
            return {}
        for _bot, _scope, key, to_addrs, cc_addrs in rows:
            if not key:
                continue
            entry = companies.setdefault(key, {"to": [], "cc": []})
            entry["to"] = _union_keep_order(entry["to"], _parse_addrs(to_addrs))
            entry["cc"] = _union_keep_order(entry["cc"], _parse_addrs(cc_addrs))
        # 库可读但 scope='company' 一行都没有：下面仍会注入常量「联运」，
        # 结果就是"看起来成功、实际只灌了 1 家"，其余公司收件人全空 ⇒ 邮件判 no_route。
        # 这句必须显式喊出来，否则只在摘要里数个数会被看漏。
        if not companies:
            print("[种子] ⚠️ 源库可读，但 bot_config 中 scope='company' 为 0 行 —— "
                  "本次只有常量注入的「联运」1 家会被写入，其余公司收件人为空、"
                  "相关邮件会判 no_route。请先核对源库路径与查询口径是否正确。")
    finally:
        conn.close()
    companies["联运"] = {"to": list(LIANYUN_TO), "cc": list(LIANYUN_CC)}
    return companies


def seed_company_recipients(yxo_db_path, bot_db_path, dry_run=True, force=False):
    """灌库。返回摘要 {"total","written","skipped","rows":[(key,to_n,cc_n,status)]}。"""
    companies = load_source_companies(yxo_db_path)
    summary = {"total": len(companies), "written": 0, "skipped": 0, "rows": []}
    if not companies and dry_run:
        print("[种子] 生产库无 company 行（或不可读），dry-run 只打印空摘要")
    if dry_run:
        for key in sorted(companies):
            to_n, cc_n = len(companies[key]["to"]), len(companies[key]["cc"])
            print(f"[dry-run] {key} to={to_n} cc={cc_n} "
                  f"to=[{','.join(mask_email(a) for a in companies[key]['to'])}]")
            summary["rows"].append((key, to_n, cc_n, "预演"))
        return summary
    conn = sqlite3.connect(bot_db_path)
    try:
        for key in sorted(companies):
            to_txt = json.dumps(companies[key]["to"], ensure_ascii=False)
            cc_txt = json.dumps(companies[key]["cc"], ensure_ascii=False)
            if force:
                old = conn.execute(
                    "SELECT to_addrs, cc_addrs FROM bot_config WHERE bot='all' AND scope='company' AND key=?",
                    (key,)).fetchone()
                conn.execute(
                    """INSERT INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra, updated_at)
                       VALUES ('all','company',?,?,?,'{}',datetime('now'))
                       ON CONFLICT(bot, scope, key) DO UPDATE SET
                         to_addrs=excluded.to_addrs, cc_addrs=excluded.cc_addrs, updated_at=datetime('now')""",
                    (key, to_txt, cc_txt))
                if old:
                    print(f"[force覆盖] {key} 旧to=[{','.join(mask_email(a) for a in _parse_addrs(old[0]))}] "
                          f"旧cc=[{','.join(mask_email(a) for a in _parse_addrs(old[1]))}]")
                summary["written"] += 1
                summary["rows"].append((key, len(companies[key]["to"]), len(companies[key]["cc"]), "写入"))
            else:
                cur = conn.execute(
                    """INSERT INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra, updated_at)
                       VALUES ('all','company',?,?,?,'{}',datetime('now'))
                       ON CONFLICT(bot, scope, key) DO NOTHING""",
                    (key, to_txt, cc_txt))
                if cur.rowcount and cur.rowcount > 0:
                    summary["written"] += 1
                    summary["rows"].append((key, len(companies[key]["to"]), len(companies[key]["cc"]), "写入"))
                else:
                    summary["skipped"] += 1
                    summary["rows"].append((key, len(companies[key]["to"]), len(companies[key]["cc"]), "跳过"))
            print(f"[{summary['rows'][-1][3]}] {key} to={len(companies[key]['to'])} cc={len(companies[key]['cc'])}")
        conn.commit()
    finally:
        conn.close()
    print(f"[种子] 共 {summary['total']} 家：写入 {summary['written']} 家，跳过 {summary['skipped']} 家")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="公司收件人初始化（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真正写入（缺省只打印）")
    parser.add_argument("--force", action="store_true", help="覆盖已有行（默认绝不覆盖；覆盖前打印旧值）")
    parser.add_argument("--yxo-db", default=YXO_DB_DEFAULT, help="生产 yxo.db 路径（只读打开）")
    parser.add_argument("--bot-db", default=BOT_DB_DEFAULT, help="本地 bot_config.db 路径")
    args = parser.parse_args(argv)
    summary = seed_company_recipients(args.yxo_db, args.bot_db, dry_run=not args.apply, force=args.force)
    if args.apply and summary["total"] == 0:
        print("[种子] 源端无数据，未写入任何行")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
