# -*- coding: utf-8 -*-
"""events 单事件库（spec §3 events_store / §6 DDL）：
WAL + busy_timeout；dedup_global / waybill_ledger / draft_seen_seq 三表；
.eml 原件落盘仓库（处理器永不回 IMAP 取件）；通用按月归档。"""
import os
import re
import sqlite3
from datetime import datetime, timedelta

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dedup_global (
    key        TEXT PRIMARY KEY,
    synthetic  INTEGER DEFAULT 0,
    claimed_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS waybill_ledger (
    id          INTEGER PRIMARY KEY,
    code        TEXT,
    box         TEXT,
    waybill     TEXT,
    train_no    TEXT,
    depart_at   TEXT,
    company     TEXT,
    msg_id      TEXT,
    forward_status TEXT DEFAULT 'pending',
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_waybill_msg ON waybill_ledger(msg_id);
CREATE TABLE IF NOT EXISTS draft_seen_seq (
    seq TEXT PRIMARY KEY,
    msg_id TEXT,
    first_seen_at TEXT DEFAULT (datetime('now'))
);
"""


def connect(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def ensure_schema(conn):
    conn.executescript(SCHEMA_SQL)
    conn.commit()


_SANITIZE = re.compile(r"[^A-Za-z0-9._@+-]+")


def save_eml(repo_dir, account, message_id, raw):
    """原件落盘 YYYY/MM/<sanitized>.eml；同 Message-ID 幂等覆盖同路径。"""
    dt = datetime.now()
    safe = _SANITIZE.sub("_", (message_id or "noid"))[:80]
    sub = os.path.join(repo_dir, dt.strftime("%Y"), dt.strftime("%m"),
                       _SANITIZE.sub("_", account.split("@")[0]))
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, safe + ".eml")
    with open(path, "wb") as f:
        f.write(raw)
    return path


def ledger_insert_waybill(conn, code, box, waybill, train_no, depart_at,
                          company, msg_id):
    """识别即留底（spec P1-4）：按 msg_id+box+waybill 幂等（表无唯一约束，
    先查后插；新插 True，已存在 False）。"""
    exists = conn.execute(
        "SELECT 1 FROM waybill_ledger "
        "WHERE msg_id=? AND box=? AND waybill=? LIMIT 1",
        (msg_id, box, waybill)).fetchone()
    if exists is not None:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO waybill_ledger"
        "(code, box, waybill, train_no, depart_at, company, msg_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (code, box, waybill, train_no, depart_at, company, msg_id))
    conn.commit()
    return cur.rowcount > 0


def ledger_mark_waybill_sent(conn, msg_id):
    cur = conn.execute(
        "UPDATE waybill_ledger SET forward_status='sent' "
        "WHERE msg_id=? AND forward_status='pending'", (msg_id,))
    conn.commit()
    return cur.rowcount


def seen_seq_add(conn, seq, msg_id):
    """A/B 类判定的序号台账：遇到即写（spec 2026-08-26 补充定义）。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO draft_seen_seq(seq, msg_id) VALUES(?,?)",
        ((seq or "").strip(), msg_id))
    conn.commit()
    return cur.rowcount > 0


def archive_before(conn, table, date_col, keep_days=90):
    """把 table 中 date_col 早于 keep_days 天的行搬入 {table}_archive（spec §6）。"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    cols = [r[1] for r in conn.execute("PRAGMA table_info({})".format(table))]
    collist = ", ".join('"{}"'.format(c) for c in cols)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS {t}_archive AS "
        "SELECT * FROM {t} WHERE 0".format(t=table))
    cur = conn.execute(
        "INSERT INTO {t}_archive ({cols}) SELECT {cols} FROM {t} "
        'WHERE date("{d}") < date(?)'.format(t=table, cols=collist, d=date_col),
        (cutoff,))
    n = cur.rowcount
    conn.execute('DELETE FROM {t} WHERE date("{d}") < date(?)'
                 .format(t=table, d=date_col), (cutoff,))
    conn.commit()
    return n
