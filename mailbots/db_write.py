#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_write.py — MailBots 共享的 yxo.db 写入模块（D 类工程 P2 用）
- 让邮件机器人（草单/运单号/ATB/DSK）解析后直接写入 yxo_app 的 records 表，
  使 yxo.db 成为唯一数据源，逐步摆脱飞书。
- upsert_draft：按「箱号」去重匹配现有订舱行 → 回填草单/补空字段；
  匹配不到时默认只记录、不新建（allow_insert=False），避免凭空造重复行。
- 兼容软删除（只匹配 is_deleted=0 的行，不误复活已删行）。
- 写入后 bump_version，让网页端实时刷新。
"""
import os
import sqlite3

# 复用 WeComBot 的 YXO_DB_PATH（与网页库是同一个文件）
sys_path_inserted = False
try:
    from config import YXO_DB_PATH  # type: ignore
except Exception:
    import sys
    sys.path.insert(0, r"D:\YXO_DATA\WeComBot")
    from config import YXO_DB_PATH  # type: ignore

# 班列号规范化（体检报告 1.1 / 3.4 复合键消歧用）
try:
    from common_io import norm_train_no  # type: ignore
except Exception:
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common_io import norm_train_no  # type: ignore


def get_conn():
    conn = sqlite3.connect(YXO_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def bump_version(conn):
    conn.execute(
        "INSERT INTO meta_kv (key, value) VALUES ('data_version', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
    )


def norm_box(b):
    return (b or "").strip().upper()


def next_seq(conn):
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM records").fetchone()
    return (row["m"] + 1) if row else 1


def is_box_cancelled(box_no):
    """退舱(状态='退舱')的记录不应再接收 DSK/ATB/草单/运单号 等按箱号匹配的邮件。
    返回 True 表示该箱号在 records 中存在未软删的退舱记录。
    查询失败按"未退舱"处理（保守：宁可发，不因数库异常漏发）。"""
    box = norm_box(box_no)
    if not box:
        return False
    try:
        conn = get_conn()
        row = conn.execute(
            'SELECT 1 FROM records WHERE COALESCE(is_deleted,0)=0 AND 箱号=? AND 状态=?',
            (box, "退舱"),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        print(f"  ⚠ is_box_cancelled 查询失败(箱号 {box}): {e}")
        return False


def upsert_draft(conn, code, box, company, email_date,
                 dry_run=False, allow_insert=False, allow_refresh=False,
                 train_no=None, depart_year=None):
    """
    把一封草单邮件解析出的 (客编, 箱号, 公司, 邮件日期) 写入 records。
    返回 (action, target_id, detail)
      action: skip / noop / update / inserted / would_update / would_insert

    匹配键（体检报告 3.4）：箱号是集装箱物理编号，会被反复复用，仅按箱号匹配会在
    跨年 / 跨班列时把新数据静默覆盖到旧记录上。改为复合键消歧，优先级（顺序即优先级）：
      ① train_no 精确匹配 —— 调用方若能提供班列号，最精准（未来草单若能解析班列号即用此）
      ② depart_year 与邮件年份对齐 —— 默认走这条：草单邮件年份 ≈ 该箱所属班列发班年份，
         直接挡掉跨年复用导致的误覆盖
      ③ 退化为最近一行（rowid DESC）—— 兼容绝大多数“单箱唯一”场景，行为同旧版
    """
    box = norm_box(box)
    if not box:
        return ("skip", None, "无箱号")

    cur = conn.cursor()
    # 取该箱号下所有未软删记录，按 rowid 倒序（最近优先），交由下方复合键消歧
    cur.execute(
        'SELECT id, "客户编码", "开票子公司名称", 草单, 台账月份, "班列号", "发班时间" '
        'FROM records WHERE COALESCE(is_deleted,0)=0 AND 箱号=? ORDER BY rowid DESC',
        (box,),
    )
    rows = cur.fetchall()

    # —— 复合键消歧（顺序即优先级）——
    row = None
    if train_no:
        tn = norm_train_no(train_no)
        for r in rows:
            if norm_train_no(r["班列号"]) == tn:
                row = r
                break
    if row is None and depart_year:
        y = str(depart_year)
        for r in rows:
            if (r["发班时间"] or "")[:4] == y:
                row = r
                break
    if row is None and rows:
        row = rows[0]  # 最近一行兜底（旧行为）

    if row:
        rid = row["id"]
        updates = {}
        # 草单：A类只补空；B类 allow_refresh 时强制刷新为「已收」
        if allow_refresh or not (row["草单"] or "").strip():
            updates["草单"] = "已收"
        if not (row["客户编码"] or "").strip() and code:
            updates["客户编码"] = code
        if not (row["开票子公司名称"] or "").strip() and company:
            updates["开票子公司名称"] = company
        if not (row["台账月份"] or "").strip() and email_date:
            updates["台账月份"] = email_date[:7]  # YYYY-MM

        if not updates:
            return ("noop", rid, "无空字段可补")
        if dry_run:
            return ("would_update", rid, updates)
        set_sql = ", ".join(f'"{k}"=?' for k in updates)
        cur.execute(f"UPDATE records SET {set_sql} WHERE id=?", list(updates.values()) + [rid])
        bump_version(conn)
        conn.commit()
        return ("update", rid, updates)

    # 未匹配到现有行
    new_row = {
        "客户编码": code or "",
        "箱号": box,
        "开票子公司名称": company or "",
        "草单": "已收",
        "台账月份": (email_date or "")[:7],
        "班列类型": "散舱",
        "状态": "正常",
    }
    if dry_run or not allow_insert:
        return ("would_insert" if dry_run else "insert_skip", None, new_row)
    seq = next_seq(conn)
    cur.execute(
        'INSERT INTO records (seq, "客户编码", "箱号", "开票子公司名称", 草单, 台账月份, 班列类型, 状态) '
        "VALUES (?,?,?,?,?,?,?,?)",
        (seq, new_row["客户编码"], box, new_row["开票子公司名称"], "已收",
         new_row["台账月份"], "散舱", "正常"),
    )
    rid = cur.lastrowid
    bump_version(conn)
    conn.commit()
    return ("inserted", rid, new_row)
