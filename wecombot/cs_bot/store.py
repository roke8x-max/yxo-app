# -*- coding: utf-8 -*-
"""
数据层：yxo.db 记录查询/更新 + cs_bot.db（撤销历史、外部用户绑定）。
"""
import os
import sys
import json
import sqlite3
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import YXO_DB_PATH, CS_BOT_DB

DISPLAY_FIELDS = ["客户编码", "发班时间", "班列号", "口岸", "目的站", "开票子公司名称",
                  "箱号", "封号", "草单", "报放单", "随车", "状态", "备注"]


def _yxo():
    conn = sqlite3.connect(YXO_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _bot():
    os.makedirs(os.path.dirname(CS_BOT_DB), exist_ok=True)
    conn = sqlite3.connect(CS_BOT_DB, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS ops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT, ts TEXT, kind TEXT, payload TEXT, undone INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bindings (
        external_id TEXT PRIMARY KEY, name TEXT, ts TEXT)""")
    return conn


# ==================== 查询 ====================

def find_by_code(code):
    """按客户编码精确/前缀查询，返回 list[dict]。"""
    with _yxo() as conn:
        rows = conn.execute(
            "SELECT * FROM records WHERE 客户编码 = ? COLLATE NOCASE", (code,)).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT * FROM records WHERE 客户编码 LIKE ? COLLATE NOCASE LIMIT 10",
                (code + "%",)).fetchall()
        return [dict(r) for r in rows]


def find_by_code_core(code):
    """数字段(含前缀)相同、但到站字母不同的候选客编。
    例如输入 CQWLJT260819001-DX、库内有 CQWLJT260819001-D 时返回后者。
    仅当 input 含 '-' 且 head(前缀+数字，即 '-' 之前部分)命中其它记录时返回 list[dict]。
    用于对话侧「字母不符→询问是否更新」场景。
    """
    code = (code or "").strip()
    if "-" not in code:
        return []
    head = code.split("-", 1)[0]
    if not head:
        return []
    with _yxo() as conn:
        # head 精确匹配(无 dash 存储) + head+'-%'(dash 紧跟 head，避免吞掉更长数字串)
        rows = conn.execute(
            "SELECT * FROM records WHERE (客户编码 = ? COLLATE NOCASE "
            "OR 客户编码 LIKE ? COLLATE NOCASE) AND 客户编码 <> ? COLLATE NOCASE",
            (head, head + "-%", code)).fetchall()
        return [dict(r) for r in rows]


def find_by_box(box):
    with _yxo() as conn:
        rows = conn.execute(
            "SELECT * FROM records WHERE 箱号 = ? COLLATE NOCASE LIMIT 10", (box,)).fetchall()
        return [dict(r) for r in rows]


def distinct_companies():
    """库里出现过的全部开票子公司名称。"""
    with _yxo() as conn:
        rows = conn.execute(
            "SELECT DISTINCT 开票子公司名称 FROM records").fetchall()
        return [r[0] for r in rows if r[0] and str(r[0]).strip()]


def stats_source(months, companies=None):
    """货源情况统计。
    months: ['2026/07', '2026/08']；companies: 公司列表或 None（全部）。
    返回 {公司: {货源类型: 票数}}（货源类型空值归为 '未填'）。
    """
    if not months:
        return {}
    with _yxo() as conn:
        sql = ("SELECT 开票子公司名称 AS comp, 货源类型 AS src, COUNT(*) AS n "
               "FROM records WHERE substr(发班时间,1,7) IN (%s)"
               % ",".join(["?"] * len(months)))
        args = list(months)
        if companies:
            sql += " AND 开票子公司名称 IN (%s)" % ",".join(["?"] * len(companies))
            args += list(companies)
        sql += " GROUP BY comp, src"
        out = {}
        for row in conn.execute(sql, args).fetchall():
            comp = str(row["comp"] or "").strip() or "（未填公司）"
            src = str(row["src"] or "").strip() or "未填"
            out.setdefault(comp, {})
            out[comp][src] = out[comp].get(src, 0) + row["n"]
        return out


def find_by_train(train):
    """按班列号查询全部票。
    返回 {月份标签: [记录]}，按发班时间年月倒序排列，最新月份在前。
    便于处理跨年重复班列号：用户优先看到最新班次，并可往下翻查历史。
    """
    with _yxo() as conn:
        rows = conn.execute(
            "SELECT * FROM records WHERE 班列号 = ? COLLATE NOCASE "
            "ORDER BY substr(发班时间,1,7) DESC, 开票子公司名称, 客户编码", (train,)).fetchall()
        groups = {}
        for r in rows:
            dep = r["发班时间"] or ""
            month = str(dep)[:7].replace("/", "-") if dep else "未填发班时间"
            groups.setdefault(month, []).append(dict(r))
        return groups


def format_record(rec, brief=False):
    """把一条记录格式化为微信文本。"""
    if brief:
        return (f"{rec.get('客户编码','')} | {rec.get('班列号','')} | "
                f"箱号:{rec.get('箱号') or '—'} | 草单:{rec.get('草单') or '—'} "
                f"报放:{rec.get('报放单') or '—'} 随车:{rec.get('随车') or '—'}")
    lines = []
    for f in DISPLAY_FIELDS:
        v = rec.get(f)
        if v not in (None, ""):
            lines.append(f"{f}：{v}")
    return "\n".join(lines) if lines else "（空记录）"


# ==================== 更新 ====================

def update_record(rec_id, fields, operator):
    """更新本地库一条记录的若干字段，返回 (old_values, ok)。"""
    with _yxo() as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (rec_id,)).fetchone()
        if not row:
            return None, False
        old = {k: row[k] for k in fields}
        sets = ", ".join(f"[{k}] = ?" for k in fields)
        vals = list(fields.values()) + [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), operator, rec_id]
        conn.execute(
            f"UPDATE records SET {sets}, updated_at = ?, updated_by = ? WHERE id = ?", vals)
        conn.commit()
        return old, True


# ==================== 撤销历史 ====================

def push_op(user, kind, payload):
    """记录一次写操作。kind: db_update / file_save / mail_sent"""
    with _bot() as conn:
        conn.execute("INSERT INTO ops(user, ts, kind, payload) VALUES (?,?,?,?)",
                     (user, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      kind, json.dumps(payload, ensure_ascii=False)))
        # 每人只保留最近 10 条可撤销
        conn.execute("""DELETE FROM ops WHERE user = ? AND id NOT IN
            (SELECT id FROM ops WHERE user = ? ORDER BY id DESC LIMIT 10)""", (user, user))
        conn.commit()


def pop_op(user):
    """取最近一条未撤销的写操作。"""
    with _bot() as conn:
        row = conn.execute(
            "SELECT id, kind, payload, ts FROM ops WHERE user = ? AND undone = 0 "
            "ORDER BY id DESC LIMIT 1", (user,)).fetchone()
        if not row:
            return None
        return {"id": row[0], "kind": row[1], "payload": json.loads(row[2]), "ts": row[3]}


def mark_undone(op_id):
    with _bot() as conn:
        conn.execute("UPDATE ops SET undone = 1 WHERE id = ?", (op_id,))
        conn.commit()


# ==================== 外部用户绑定 ====================

def get_binding(external_id):
    with _bot() as conn:
        row = conn.execute("SELECT name FROM bindings WHERE external_id = ?",
                           (external_id,)).fetchone()
        return row[0] if row else None


def set_binding(external_id, name):
    with _bot() as conn:
        conn.execute("INSERT OR REPLACE INTO bindings(external_id, name, ts) VALUES (?,?,?)",
                     (external_id, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
