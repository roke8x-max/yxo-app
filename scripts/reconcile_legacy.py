#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G1：旧系统遗留件 → 新系统 dedup 的一次性对账（任务 20 / 2.5）。

为什么必须有这个脚本
--------------------
新系统的 `dedup` 只认"自己认领过的"，旧系统那套账（`pending_queue` /
`processed_mails` / `forward_log`）它一概不知。切换 LIVE 之后，同一封旧邮件在
新系统眼里就是"从未处理" ⇒ 会被再发一次（2026-11-16 09:52 那两封就是这么出去的
同一条线）。所以**切 LIVE 前必须跑一遍本脚本**，把旧账补进新 `dedup`。

🔴 映射口径按 action 分，**不许一律 sent**
------------------------------------------
旧 `processed_mails.action` 里 `pending` 的语义是"待人工确认、**没发**"。一律标
sent 会让本该发的件永不发（新系统里 sent 是硬终态）。完整映射见 `_map_action()`：

| 旧来源 / action                     | 新 status | 说明                                   |
|-------------------------------------|-----------|----------------------------------------|
| `forward_log`(test=0)               | sent      | 有实物发送记录                          |
| `auto_forwarded_split` / `forward*` | sent      | 机器人已发                              |
| `notified`                          | closed    | 运单号 WAY_B「只通知不转发」            |
| `record`                            | closed    | 草单 C2/W「只记录」                     |
| `historical_skip`                   | closed    | 历史门禁，按设计不发                    |
| **`pending`**                       | **closed**| **旧语义=等人工确认；脚本另出清单**      |
| `pending_queue` status=confirmed    | sent      | 人已确认并补发                          |
| `pending_queue` status=skipped      | closed    | 人已跳过                                |
| 其它未知 action                     | closed    | 保守不发，并在输出里逐个列出            |

写入规则：**只增不删、已存在的键不覆盖**（INSERT OR IGNORE）；键是**裸
message_id**（旧库只有整封粒度，拆不出 row_key）；`claimed_by='reconcile'`、
`source='legacy_manual'`。默认 dry-run，`--apply` 才写。重复跑结果不变（幂等）。

用法
----
    python scripts/reconcile_legacy.py                # dry-run，只打印
    python scripts/reconcile_legacy.py --apply        # 真写
    python scripts/reconcile_legacy.py --ledger D:/YXO_DATA/MailBots/data/draft_forward_ledger.db
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OLD_DATA = ROOT / "mailbots" / "data"

# status 取值与 dedup.py 保持一致（这里不 import 常量，避免 --help 也要建库）
STATUS_SENT = "sent"
STATUS_CLOSED = "closed"
SOURCE_LEGACY = "legacy_manual"

# 输出里 pending 清单的列（02_TASK 2.5 写死 4 列）
PENDING_COLUMNS = ("message_id", "subject", "code", "box")


def _open_ro(path: Path):
    """只读打开。文件不存在 / 打不开 → None（并打印原因，绝不静默）。"""
    try:
        if not path.exists():
            print(f"  [跳过] 文件不存在：{path}")
            return None
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except (OSError, sqlite3.Error) as e:
        print(f"  [跳过] 打不开 {path}：{type(e).__name__}: {e}")
        return None


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _cols(conn, table: str):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _map_action(action: str):
    """旧 action → 新 status（写死，见模块头的映射表）。未知一律 closed。"""
    text = (action or "").strip()
    low = text.lower()
    if low == "auto_forwarded_split":
        return STATUS_SENT
    if low.startswith("forward"):
        # 草单的 forward|full|app 等：机器人真的发了
        return STATUS_SENT
    # notified / record / historical_skip / pending / 未知：一律 closed（保守不发）
    return STATUS_CLOSED


def _read_pending_queue(path: Path):
    """旧 pending_queue：confirmed → sent、skipped → closed。open 的不动（新系统自己判）。"""
    out = []
    conn = _open_ro(path)
    if conn is None:
        return out
    try:
        if not _has_table(conn, "pending_queue"):
            print("  [跳过] pending_queue 表不存在")
            return out
        cols = _cols(conn, "pending_queue")
        if "status" not in cols or "message_id" not in cols:
            print("  [跳过] pending_queue 缺 status/message_id 列")
            return out
        rows = conn.execute(
            "SELECT message_id, status, subject, code, box FROM pending_queue "
            "WHERE status IN ('confirmed', 'skipped')"
        ).fetchall()
        for message_id, status, subject, code, box in rows:
            if not message_id:
                continue
            out.append({
                "message_id": str(message_id),
                "status": STATUS_SENT if status == "confirmed" else STATUS_CLOSED,
                "reason": f"pending_queue:{status}",
                "subject": subject or "",
                "code": code or "",
                "box": box or "",
                "action": "",
            })
    except sqlite3.Error as e:
        print(f"  [警告] 读 pending_queue 失败：{type(e).__name__}: {e}")
    finally:
        conn.close()
    return out


def _read_ledger(path: Path):
    """旧 processed_mails：按 action 映射（**pending 一律 closed**）。"""
    out = []
    conn = _open_ro(path)
    if conn is None:
        return out
    try:
        if not _has_table(conn, "processed_mails"):
            print("  [跳过] processed_mails 表不存在")
            return out
        cols = _cols(conn, "processed_mails")

        def col(name):
            return name if name in cols else "''"

        rows = conn.execute(
            f"SELECT message_id, {col('action')}, {col('subject')}, "
            f"{col('code')}, {col('box')} FROM processed_mails"
        ).fetchall()
        for message_id, action, subject, code, box in rows:
            if not message_id:
                continue
            out.append({
                "message_id": str(message_id),
                "status": _map_action(action),
                "reason": f"processed_mails:{action or '-'}",
                "subject": subject or "",
                "code": code or "",
                "box": box or "",
                "action": action or "",
            })
    except sqlite3.Error as e:
        print(f"  [警告] 读 processed_mails 失败：{type(e).__name__}: {e}")
    finally:
        conn.close()
    return out


def _read_forward_log(path: Path):
    """旧 forward_log：test=0 的 message_id → sent（这一源可以判 sent）。

    ⚠️ 生产实测：旧表 DDL（mailbots/forward_log.py:24）**没有 message_id 列**
    （只有 robot/day/ts/owner/company/code/box/subject/to_list/sender/note/test）。
    没有这一列就无法把行还原成某封邮件 ⇒ 本源不可用，打印说明后跳过，不猜。
    """
    out = []
    conn = _open_ro(path)
    if conn is None:
        return out
    try:
        if not _has_table(conn, "forward_log"):
            print("  [跳过] forward_log 表不存在")
            return out
        cols = _cols(conn, "forward_log")
        if "message_id" not in cols:
            n = conn.execute("SELECT COUNT(*) FROM forward_log").fetchone()[0]
            print(f"  [跳过] 旧 forward_log 无 message_id 列（实测 DDL 如此），"
                  f"本源不可用：该表共 {n} 行，未参与对账")
            return out
        rows = conn.execute(
            "SELECT message_id, subject, code, box FROM forward_log "
            "WHERE test = 0 AND COALESCE(message_id, '') <> ''"
        ).fetchall()
        for message_id, subject, code, box in rows:
            out.append({
                "message_id": str(message_id),
                "status": STATUS_SENT,
                "reason": "forward_log:test=0",
                "subject": subject or "",
                "code": code or "",
                "box": box or "",
                "action": "forward_log",
            })
    except sqlite3.Error as e:
        print(f"  [警告] 读 forward_log 失败：{type(e).__name__}: {e}")
    finally:
        conn.close()
    return out


def _print_pending_list(entries):
    """输出旧 pending 清单（4 列）—— 人工核对后用 --pool retry 逐条放回池子。"""
    pending = [e for e in entries if e.get("action") == "pending"]
    print("")
    print(f"【旧 processed_mails action='pending' 共 {len(pending)} 条】")
    print("旧语义 = 等人工确认、**没发**（所以本次一律标 closed，不是 sent）。")
    print("请逐条核对后执行： python mailbots_next/serve.py --pool retry <message_id>")
    print("  " + " | ".join(PENDING_COLUMNS))
    for e in pending:
        print("  " + " | ".join(str(e.get(c) or "-") for c in PENDING_COLUMNS))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="旧系统遗留件对账（G1）：把 pending_queue / processed_mails / "
                    "forward_log 补进新 dedup。默认 dry-run。",
    )
    parser.add_argument("--pending-queue", default=str(_DEFAULT_OLD_DATA / "pending_queue.db"),
                        help="旧 pending_queue.db 路径")
    parser.add_argument("--ledger", default=str(_DEFAULT_OLD_DATA / "draft_forward_ledger.db"),
                        help="旧 draft_forward_ledger.db 路径")
    parser.add_argument("--forward-log", default=str(_DEFAULT_OLD_DATA / "forward_log.db"),
                        help="旧 forward_log.db 路径")
    parser.add_argument("--dedup-db", default="",
                        help="新 dedup.db 路径（默认取 DEDUP_DB_PATH / mailbots_next/data/dedup.db）")
    parser.add_argument("--apply", action="store_true",
                        help="真写入（默认 dry-run，一行都不写）")
    args = parser.parse_args(argv)

    # DEDUP_DB_PATH 必须在 import mailbots_next 之前设好（settings 在 import 期取值）
    if args.dedup_db:
        os.environ["DEDUP_DB_PATH"] = str(Path(args.dedup_db).expanduser())
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    mode = "APPLY（会写入）" if args.apply else "DRY-RUN（不写入，只统计）"
    print(f"=== 旧系统遗留件对账 / {mode} ===")
    print(f"  pending_queue : {args.pending_queue}")
    print(f"  ledger        : {args.ledger}")
    print(f"  forward_log   : {args.forward_log}")

    entries = []
    entries += _read_pending_queue(Path(args.pending_queue))
    entries += _read_ledger(Path(args.ledger))
    entries += _read_forward_log(Path(args.forward_log))
    print(f"  读到 {len(entries)} 条旧记录")

    from mailbots_next.core.dedup import DEDUP_DB_PATH, init_db, insert_if_absent, get_status

    init_db()
    print(f"  新 dedup      : {DEDUP_DB_PATH}")

    inserted = skipped = 0
    by_status = {}
    unknown_actions = set()
    planned = []
    for e in entries:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
        if e.get("action") and not _is_known_action(e["action"]):
            unknown_actions.add(e["action"])
        existing = get_status(e["message_id"])          # 裸键
        if existing is not None:
            skipped += 1
            continue
        planned.append(e)
        if args.apply:
            ok = insert_if_absent(
                e["message_id"], e["status"], source=SOURCE_LEGACY,
                claimed_by="reconcile",
                close_reason=("legacy_" + e["status"]) if e["status"] == STATUS_CLOSED else "",
            )
            inserted += 1 if ok else 0
            skipped += 0 if ok else 1
        else:
            inserted += 1

    print("")
    print(f"  映射结果：sent {by_status.get(STATUS_SENT, 0)} 条 / "
          f"closed {by_status.get(STATUS_CLOSED, 0)} 条")
    print(f"  {'已写入' if args.apply else '将写入'} {inserted} 条；跳过（已存在，不覆盖）{skipped} 条")
    if unknown_actions:
        print("  [注意] 未识别的 action（已按 closed 保守处理）："
              + ", ".join(sorted(unknown_actions)))
    if not args.apply:
        print("  DRY-RUN：**未写入任何行**。确认无误后加 --apply 再跑一次。")

    _print_pending_list(entries)
    print("")
    print("完成。重复跑结果不变（只增不删 + 已存在键不覆盖）。")
    return 0


_KNOWN_ACTIONS = {
    "auto_forwarded_split", "notified", "record", "historical_skip", "pending",
}


def _is_known_action(action: str) -> bool:
    low = (action or "").strip().lower()
    return low in _KNOWN_ACTIONS or low.startswith("forward")


if __name__ == "__main__":
    sys.exit(main())
