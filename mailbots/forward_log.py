# -*- coding: utf-8 -*-
"""
统一转发日志（2026-07-31 毛骁洋定规则）

规则:
  DSK / ATB / 草单 / 运单号 四个机器人共用一条规则 ——
    · 转发了就企微通知（各机器人自身已实现）
    · 每天早上 8:00 给对应同事邮箱发一份汇总邮件，说明"昨天"的转发情况（本模块 + daily_forward_report.py）

本模块只负责"记一笔"，被各机器人在转发成功后调用。
设计要求: 绝不能因为记日志失败而影响转发主流程 —— 所有调用点都用 try/except 包住，
         本模块内部也全程吞异常。
"""
import os
import sqlite3
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "forward_log.db")

DDL = """
CREATE TABLE IF NOT EXISTS forward_log(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    robot    TEXT,      -- DSK / ATB / 草单 / 运单号
    day      TEXT,      -- YYYY-MM-DD, 便于按天汇总
    ts       TEXT,      -- YYYY-MM-DD HH:MM:SS
    owner    TEXT,      -- 负责同事真实姓名
    company  TEXT,
    code     TEXT,      -- 客户编码
    box      TEXT,      -- 箱号
    subject  TEXT,
    to_list  TEXT,      -- 实际收件人
    sender   TEXT,      -- 发信邮箱
    note     TEXT,
    test     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fl_day   ON forward_log(day);
CREATE INDEX IF NOT EXISTS idx_fl_owner ON forward_log(owner);
"""


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.executescript(DDL)
    return conn


def record(robot, owner="", company="", code="", box="", subject="",
           to_list=None, sender="", note="", test=False):
    """记一笔转发。任何异常都吞掉，绝不影响调用方的转发主流程。"""
    try:
        now = datetime.now()
        if isinstance(to_list, (list, tuple, set)):
            to_s = ",".join(str(x) for x in to_list)
        else:
            to_s = str(to_list or "")
        conn = _conn()
        conn.execute(
            "INSERT INTO forward_log(robot,day,ts,owner,company,code,box,subject,"
            "to_list,sender,note,test) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (robot, now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S"),
             owner or "", company or "", code or "", box or "",
             (subject or "")[:200], to_s, sender or "", note or "",
             1 if test else 0))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def fetch_day(day, include_test=False):
    """取某天(YYYY-MM-DD)的全部转发记录，按同事分组返回 {owner: [row, ...]}。"""
    out = {}
    try:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM forward_log WHERE day=?"
        if not include_test:
            sql += " AND test=0"
        sql += " ORDER BY ts"
        for r in conn.execute(sql, (day,)):
            out.setdefault(r["owner"] or "(未匹配)", []).append(dict(r))
        conn.close()
    except Exception:
        pass
    return out


def stats_day(day, include_test=False):
    """某天总体统计: (总条数, {robot: 条数})。"""
    total, by_robot = 0, {}
    try:
        conn = _conn()
        sql = "SELECT robot, COUNT(*) FROM forward_log WHERE day=?"
        if not include_test:
            sql += " AND test=0"
        sql += " GROUP BY robot"
        for robot, n in conn.execute(sql, (day,)):
            by_robot[robot or "?"] = n
            total += n
        conn.close()
    except Exception:
        pass
    return total, by_robot
