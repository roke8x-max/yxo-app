# -*- coding: utf-8 -*-
"""
本地邮件去重指纹库（SQLite）
================================================================
芙蕾雅 2026-07-31 · P1 改造

背景
----
DSK / ATB 机器人原先每次运行都要 `fs.get_all_records(TABLE_DSK_LOG)`
把飞书日志表整张拉下来构造去重集合。该表**从不清理**，两年后会持续变长，
导致「每次转发都全表扫描」越来越慢（5 万行约 100 页 HTTPS，一次 5~20 秒）。

本模块把去重指纹落到本地 SQLite，改为**主键点查 O(1)**：
  - 首次运行自动 seed（从飞书日志表 + 旧 atb_forwarded.json 灌一次种）
  - 之后每次运行 **不再拉飞书**，判重走本地索引
  - 90 天自动清理，容量恒定不膨胀

注意
----
* 去重命名空间与改造前保持一致：DSK 与 ATB 共用 `dsk_log` 这一个 source，
  因为原代码两者都读整张 TABLE_DSK_LOG。**不要**擅自拆开，否则历史指纹会失配。
* 飞书 `add_record(TABLE_DSK_LOG, ...)` 仍然保留（作为审计流水），
  只是不再作为「去重数据源」。将来彻底弃用飞书时，删掉那次写入即可，
  本模块无需改动。
* DSK / ATB 两个计划任务可能时间重叠，故启用 WAL + busy_timeout 防锁。
"""

import os
import sqlite3
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "dedup.db")

# 默认保留天数：DSK/ATB 邮件均为当天处理，90 天与运踪表清理策略对齐
DEFAULT_RETENTION_DAYS = 90


def _connect():
    """打开连接并确保表结构存在（WAL + busy_timeout 防并发锁）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_dedup (
            source     TEXT NOT NULL,
            message_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (source, message_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dedup_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # 清理用索引（按时间范围删除）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dedup_created "
        "ON mail_dedup(source, created_at)"
    )
    conn.commit()
    return conn


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==================== 判重 / 记录 ====================

def is_processed(source, message_id):
    """该 Message-ID 是否已处理过。主键点查，O(1)，不做全表扫描。"""
    if not message_id:
        return False
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM mail_dedup WHERE source=? AND message_id=? LIMIT 1",
            (source, str(message_id).strip()),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark(source, message_id):
    """标记单条已处理。重复调用安全（INSERT OR IGNORE）。"""
    if not message_id:
        return
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO mail_dedup(source, message_id, created_at) "
            "VALUES (?,?,?)",
            (source, str(message_id).strip(), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def mark_many(source, message_ids):
    """批量标记（seed 用）。返回实际写入条数。"""
    ids = [str(m).strip() for m in message_ids if m and str(m).strip()]
    if not ids:
        return 0
    conn = _connect()
    try:
        ts = _now()
        cur = conn.executemany(
            "INSERT OR IGNORE INTO mail_dedup(source, message_id, created_at) "
            "VALUES (?,?,?)",
            [(source, m, ts) for m in ids],
        )
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(ids)
    finally:
        conn.close()


def count(source=None):
    """当前指纹条数。"""
    conn = _connect()
    try:
        if source:
            row = conn.execute(
                "SELECT COUNT(*) FROM mail_dedup WHERE source=?", (source,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM mail_dedup").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# ==================== seed 标记 ====================

def is_seeded(source):
    """是否已从飞书做过一次性播种。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM dedup_meta WHERE key=?", (f"seeded::{source}",)
        ).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def set_seeded(source):
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO dedup_meta(key, value) VALUES (?,?)",
            (f"seeded::{source}", _now()),
        )
        conn.commit()
    finally:
        conn.close()


# ==================== 清理 ====================

def purge(source=None, days=DEFAULT_RETENTION_DAYS):
    """删除 N 天前的指纹，返回删除条数。容量因此恒定，不会随年份膨胀。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        if source:
            cur = conn.execute(
                "DELETE FROM mail_dedup WHERE source=? AND created_at < ?",
                (source, cutoff),
            )
        else:
            cur = conn.execute(
                "DELETE FROM mail_dedup WHERE created_at < ?", (cutoff,)
            )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


if __name__ == "__main__":
    # 简易自检：python dedup_store.py
    print("DB:", DB_PATH)
    print("总指纹:", count())
    print("dsk_log:", count("dsk_log"), "seeded:", is_seeded("dsk_log"))
