"""任务 24：把历史误进 error_queue 的下游回复草单回填进 manual_queue。

一次性脚本（幂等），上线后由运维在服务器执行**一次**：

    python -m mailbots_next.backfill_manual_queue

筛选条件逐字固定（洋 2026-11-20 拍板）：

    stage='identify' AND error_type='UNCLASSIFIED' AND status IN ('manual','pending')
    AND id IN (3,4)

id=5 洋已手工处理（resolved）止血，**保持不动**，所以不在筛选范围内。

每行的处理：
  ① 用 parse_draft_reply_ids(subject) 现算客编/箱号（库里没这两列）；
  ② add_manual(...) 落 manual_queue（UNIQUE(message_id) 保证重跑不重复）；
  ③ mark_error_resolved(id) 把原 error_queue 行标 resolved（对应「归入 B 区、不再在 A 区显示」）。

⚠️ 执行前先备份 dedup.db。
⚠️ 本文件名不以 test_ 开头 ⇒ pytest 默认不收集（与 backfill_ 前缀无关）。
⚠️ 禁止在服务器跑 pytest（test_inbound 夹具会 DELETE FROM forward_log）。
"""
import sys

from mailbots_next.core.dedup import (
    add_manual,
    get_connection,
    has_manual,
    mark_error_resolved,
)
from mailbots_next.core.draft_reply import parse_draft_reply_ids

# 逐字固定：只回填这两条。改这个元组等于改需求。
BACKFILL_IDS = (3, 4)

_SELECT_SQL = (
    "SELECT id, message_id, account, folder, uid, subject, sender, date "
    "  FROM error_queue "
    " WHERE stage = 'identify' AND error_type = 'UNCLASSIFIED' "
    "   AND status IN ('manual', 'pending') "
    "   AND id IN (%s) "
    " ORDER BY id ASC"
) % ", ".join("?" * len(BACKFILL_IDS))


def _fetch_targets():
    """读出待回填的 error_queue 行（只读，连接用完即关）。"""
    conn = get_connection()
    try:
        return conn.execute(_SELECT_SQL, BACKFILL_IDS).fetchall()
    finally:
        conn.close()


def main() -> int:
    rows = _fetch_targets()
    if not rows:
        print("没有需要回填的行（筛选：stage='identify' / UNCLASSIFIED / status IN (manual,pending) / id IN (3,4)）")
        print("回填 0 条，跳过 0 条，失败 0 条")
        return 0

    migrated = skipped = failed = 0
    for r in rows:
        error_id = int(r["id"])
        message_id = r["message_id"] or ""
        subject = r["subject"] or ""
        try:
            code, box = parse_draft_reply_ids(subject)
            if has_manual(message_id):
                skipped += 1
                print(f"  跳过 #{error_id}: manual_queue 已有 {message_id[:50]}")
            else:
                add_manual(
                    message_id,
                    account=r["account"] or "",
                    folder=r["folder"] or "",
                    uid=int(r["uid"] or 0),
                    subject=subject,
                    sender=r["sender"] or "",
                    date_hdr=r["date"] or "",   # error_queue 里叫 date，manual_queue 叫 date_hdr
                    code=code,
                    box=box,
                )
                migrated += 1
                print(f"  回填 #{error_id} → manual_queue: {message_id[:50]} "
                      f"code={code or '-'} box={box or '-'}")
            # 幂等：已经在 manual_queue 的那条也要把 error_queue 行收掉（重跑不会重复计数）
            if mark_error_resolved(error_id):
                print(f"    error_queue #{error_id} → resolved")
            else:
                print(f"    error_queue #{error_id} 已是 resolved，未改")
        except Exception as e:
            # 逐条报，不中断脚本（一条坏行不该拦住另外一条）
            failed += 1
            print(f"  失败 #{error_id}: {type(e).__name__}: {e}")

    print(f"回填 {migrated} 条，跳过 {skipped} 条，失败 {failed} 条（扫描 {len(rows)} 行）")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
