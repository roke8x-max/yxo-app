# -*- coding: utf-8 -*-
"""
init_bot_config.py — 配置表 / 日志表 地基（D类任务 B方案 #148）

背景：
  飞书全线下（P4）之后，原飞书 TABLE_DSK_CONFIG(收件人路由规则) 与
  TABLE_DSK_LOG(去重日志) 需要落到 yxo.db，成为唯一数据源的一部分。
  - bot_config : 路由规则（box/company/default -> To/CC），这部分**无法从 records 推导**，必须人工维护。
  - bot_log    : 去重 / 审计日志（替代 TABLE_DSK_LOG；当前运行时已由本地 dedup_store 兜底）。

本脚本只建地基 + 种 DEFAULT 占位，**不改动任何机器人代码**。
飞书实际数据迁移由 mirror_from_feishu() 在服务器侧（持有飞书凭证）择机执行。

回退：DROP TABLE bot_config; DROP TABLE bot_log;
"""
import os
import sqlite3

import config  # 复用 config.DB_PATH，避免指向根目录的空壳 yxo.db

DB = config.DB_PATH
BOTS = ["dsk", "atb", "draft", "waybill", "tracking"]


def init(conn):
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bot_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot TEXT NOT NULL,
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        to_addrs TEXT NOT NULL DEFAULT '[]',
        cc_addrs TEXT NOT NULL DEFAULT '[]',
        extra TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT 'manual',
        updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(bot, scope, key)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bot_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot TEXT NOT NULL,
        dedup_key TEXT NOT NULL,
        record_ref TEXT,
        action TEXT,
        detail TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(bot, dedup_key)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bot_log_bot ON bot_log(bot, created_at)")
    # 种 DEFAULT 占位，保证每个 bot 至少有基线
    for b in BOTS:
        cur.execute(
            "INSERT OR IGNORE INTO bot_config(bot,scope,key,to_addrs,cc_addrs,source) VALUES(?,?,?,?,?,?)",
            (b, "default", "DEFAULT", "[]", "[]", "seed"),
        )
    conn.commit()


def mirror_from_feishu(records):
    """
    占位：从飞书 TABLE_DSK_CONFIG 拉到的规则列表写入 bot_config。
    records: list of dict {bot, scope, key, to_addrs:list, cc_addrs:list, extra:dict}
    在服务器侧持有飞书凭证时调用（注意不覆盖 source='manual' 的人工维护值）。
    当前未接入飞书 SDK，调用方负责取数后传入。
    """
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    for r in records:
        cur.execute(
            """INSERT INTO bot_config(bot,scope,key,to_addrs,cc_addrs,extra,source,updated_at)
               VALUES(:bot,:scope,:key,:to,:cc,:extra,'feishu',datetime('now','localtime'))
               ON CONFLICT(bot,scope,key) DO UPDATE SET
                 to_addrs=excluded.to_addrs, cc_addrs=excluded.cc_addrs,
                 extra=excluded.extra, source='feishu', updated_at=excluded.updated_at
               WHERE excluded.source='feishu'""",
            {
                "bot": r["bot"], "scope": r["scope"], "key": r["key"],
                "to": __import__("json").dumps(r.get("to_addrs", []), ensure_ascii=False),
                "cc": __import__("json").dumps(r.get("cc_addrs", []), ensure_ascii=False),
                "extra": __import__("json").dumps(r.get("extra", {}), ensure_ascii=False),
            },
        )
    conn.commit()
    conn.close()


def health(conn):
    cur = conn.cursor()
    print("== bot_config 现状 ==")
    cur.execute("SELECT bot, scope, key, to_addrs, cc_addrs, source FROM bot_config ORDER BY bot, scope, key")
    for row in cur.fetchall():
        print("  ", row)
    print("== 各 bot 配置条数 ==")
    cur.execute("SELECT bot, COUNT(*) FROM bot_config GROUP BY bot")
    for row in cur.fetchall():
        print("  ", row)
    print("== bot_log 现状 ==")
    cur.execute("SELECT COUNT(*) FROM bot_log")
    print("   总行数:", cur.fetchone()[0])


def main():
    conn = sqlite3.connect(DB)
    init(conn)
    print("bot_config / bot_log 表已就绪 (幂等)")
    health(conn)
    conn.close()


if __name__ == "__main__":
    main()
