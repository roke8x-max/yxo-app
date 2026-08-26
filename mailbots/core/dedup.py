# -*- coding: utf-8 -*-
"""全局去重（spec §3 dedup / 08 §7）：
主键=Message-ID；缺 ID 用作用域内容哈希兜底(synthetic=1)；
claim→成功保持 / 失败 release→下轮重试；崩溃靠 reclaim_stale 兜底。"""
import hashlib


def synthetic_key(account, folder, sender, date_hdr, subject):
    payload = "|".join([account or "", folder or "", sender or "",
                        date_hdr or "", subject or ""])
    return "SYN::" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def try_claim(conn, key, synthetic=False):
    cur = conn.execute(
        "INSERT OR IGNORE INTO dedup_global(key, synthetic) VALUES(?,?)",
        (key, 1 if synthetic else 0))
    conn.commit()
    return cur.rowcount > 0


def release(conn, key):
    """处理失败：释放占用，下一轮允许重试（保证每封业务邮件至少转发一次）。"""
    conn.execute("DELETE FROM dedup_global WHERE key=?", (key,))
    conn.commit()


def reclaim_stale(conn, hours=6):
    cur = conn.execute(
        "DELETE FROM dedup_global "
        "WHERE claimed_at < datetime('now', ?)", ("-{} hours".format(hours),))
    conn.commit()
    return cur.rowcount
