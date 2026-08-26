#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""bot_config 草单/运单号 company 路由种子迁移 CLI（Plan-B Task 7 数据闸门）。
背景：新架构路由读 bot_config(bot, scope='company')，但 draft/waybill 两 bot
无任何 company 行；现役路由源是 dsk_config_cache.json 的 default_map（或 dsk
bot 已迁好的 company 行）。本脚本一次性把源映射种进目标 bot。

用法：
  python scripts_seed_routing.py --db D:\...\yxo.db \
      (--from-cache D:\...\dsk_config_cache.json | --from-bot dsk) \
      --into draft,waybill [--dry-run|--apply]
默认 dry-run 只报数不写库；--apply 用 INSERT OR IGNORE 幂等写入（依赖表上
UNIQUE(bot,scope,key)，重复执行不会产生重复行）。"""
import argparse
import json
import os
import sqlite3
import sys


def _fail(msg):
    raise SystemExit("错误: " + msg)


def _load_cache_map(path):
    """读 dsk_config_cache.json 的 default_map：{公司: {'to': [...], 'cc': [...]}}。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        _fail("无法读取缓存文件 {}: {}".format(path, e))
    default_map = data.get("default_map") if isinstance(data, dict) else None
    if not isinstance(default_map, dict) or not default_map:
        _fail("缓存文件 {} 缺少非空 default_map 键".format(path))
    out = {}
    for comp, info in default_map.items():
        comp = str(comp or "").strip()
        if not comp:
            continue
        info = info if isinstance(info, dict) else {}
        out[comp] = ([str(x) for x in (info.get("to") or [])],
                     [str(x) for x in (info.get("cc") or [])])
    return out


def _load_bot_map(conn, bot):
    """从既有 bot 的 scope='company' 行复制（原文照抄，不做改写）。"""
    rows = conn.execute(
        "SELECT key, to_addrs, cc_addrs FROM bot_config "
        "WHERE bot=? AND scope='company' ORDER BY key", (bot,)).fetchall()
    if not rows:
        _fail("源 bot={} 无任何 scope='company' 行，闸门无法关闭".format(bot))
    return {str(r[0] or "").strip(): (r[1], r[2]) for r in rows}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="bot_config 草单/运单号 company 路由种子迁移")
    ap.add_argument("--db", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-cache", metavar="DSK_CONFIG_CACHE_JSON",
                     help="读该 JSON 的 default_map 键作为源映射")
    src.add_argument("--from-bot", metavar="BOT",
                     help="复制该 bot 现有 scope='company' 行（如 dsk）")
    ap.add_argument("--into", required=True,
                    help="逗号分隔的目标 bot 列表，如 draft,waybill")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="只打印将写入条数，不写库（默认行为）")
    mode.add_argument("--apply", action="store_true")
    # 显式传入 argv 时按 sys.argv 惯例含脚本名，去掉首元素再解析
    rest = list(argv[1:]) if argv else None
    args = ap.parse_args(rest)

    bots = []
    for b in args.into.split(","):
        b = b.strip()
        if b and b not in bots:
            bots.append(b)
    if not bots:
        _fail("--into 为空")

    conn = sqlite3.connect(args.db, timeout=30)
    try:
        if args.from_cache:
            source = _load_cache_map(args.from_cache)
            src_desc = "cache:" + os.path.basename(args.from_cache)
        else:
            source = _load_bot_map(conn, args.from_bot)
            src_desc = "bot:" + args.from_bot
        if not source:
            _fail("源映射为空")
        # cache 源序列化为紧凑 JSON（中文原样）；bot 源保持原文
        if args.from_cache:
            source = {k: (json.dumps(v[0], ensure_ascii=False),
                          json.dumps(v[1], ensure_ascii=False))
                      for k, v in source.items()}

        report = {}
        for bot in bots:
            existing = {r[0] for r in conn.execute(
                "SELECT key FROM bot_config WHERE bot=? AND scope='company'",
                (bot,))}
            fresh = [k for k in source if k not in existing]
            report[bot] = {"companies": len(source),
                           "existing": len(existing), "new": len(fresh)}
            if args.apply:
                for k in fresh:
                    to, cc = source[k]
                    conn.execute(
                        "INSERT OR IGNORE INTO bot_config"
                        "(bot,scope,key,to_addrs,cc_addrs) VALUES(?,'company',?,?,?)",
                        (bot, k, to, cc))
        if args.apply:
            conn.commit()
    finally:
        conn.close()

    tag = "APPLIED" if args.apply else "DRY-RUN"
    for bot, r in report.items():
        print("{} {}: 源 {} 家公司({}), 已存在 {}, {} {}".format(
            tag, bot, r["companies"], src_desc, r["existing"],
            "新增" if args.apply else "待写入", r["new"]))
    return report


if __name__ == "__main__":
    main()
