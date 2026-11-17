import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any

from ..config import DEDUP_DB_PATH, DEDUP_RETENTION_DAYS
from .log import get_logger

_log = get_logger(__name__)
_db_lock = threading.Lock()

# ---------------------------------------------------------------- 任务 20：三态状态机
#
# dedup.status 只有三种取值（**禁止第四种**）：
#   sent       已发出（forward_log 有对应发送单元，或人工/对账认定）—— 永不重发
#   closed     判定为「不需要发」（manual/rejected/skipped/原文已删）—— 永不重判
#   unresolved 认领了但没发出去 = 未发出池 —— **唯一允许自动重判**的状态
#
# 键的语义：try_claim 把 f"{message_id}|{row_key}" 写进 dedup.message_id 这一列
# （row_key 为空时就是裸 message_id）。主键天然就是 (message_id, row_key) 的拼接，
# **不新增 row_key 列、不迁移主键**；拆键一律 key.split("|", 1)（不许 rsplit：
# row_key 里含 ":"，message_id 侧不保证不含分隔符）。
STATUS_SENT = "sent"
STATUS_CLOSED = "closed"
STATUS_UNRESOLVED = "unresolved"
STATUSES = (STATUS_SENT, STATUS_CLOSED, STATUS_UNRESOLVED)

# 只加这 8 个列（DDL 共 8 条 ALTER），**不加 row_key 列、不改主键**。
# 老库的历史行 status 落到 DEFAULT 'sent'：保守，绝不重发。
_DEDUP_NEW_COLUMNS = (
    ("status", "ALTER TABLE dedup ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'"),
    ("last_try_at", "ALTER TABLE dedup ADD COLUMN last_try_at TEXT"),
    ("source", "ALTER TABLE dedup ADD COLUMN source TEXT NOT NULL DEFAULT ''"),
    ("close_reason", "ALTER TABLE dedup ADD COLUMN close_reason TEXT NOT NULL DEFAULT ''"),
    ("account", "ALTER TABLE dedup ADD COLUMN account TEXT NOT NULL DEFAULT ''"),
    ("folder", "ALTER TABLE dedup ADD COLUMN folder TEXT NOT NULL DEFAULT ''"),
    ("uid", "ALTER TABLE dedup ADD COLUMN uid INTEGER NOT NULL DEFAULT 0"),
    ("retry_count", "ALTER TABLE dedup ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"),
)


def join_key(message_id: str, row_key: str = "") -> str:
    """拼接键（唯一实现）：row_key 为空时退化成裸 message_id。"""
    return f"{message_id}|{row_key}" if row_key else message_id


def split_key(key: str):
    """拼接键 → (message_id, row_key)。只许 split("|", 1)，不许 rsplit。"""
    text = key or ""
    if "|" not in text:
        return (text, "")
    return tuple(text.split("|", 1))


def _connect() -> sqlite3.Connection:
    Path(DEDUP_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DEDUP_DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_connection() -> sqlite3.Connection:
    """Get a connection to the dedup database (for direct queries)."""
    return _connect()


def init_db():
    with _db_lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dedup (
                    message_id TEXT PRIMARY KEY,
                    claimed_at TEXT NOT NULL,
                    claimed_by TEXT
                );
                CREATE TABLE IF NOT EXISTS error_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    row_key TEXT,
                    stage TEXT NOT NULL,
                    error_type TEXT,
                    error_detail TEXT,
                    error_id TEXT,
                    attempt_count INTEGER DEFAULT 0,
                    last_attempt_at TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    raw_hex TEXT,
                    account TEXT,
                    folder TEXT,
                    subject TEXT,
                    sender TEXT,
                    date TEXT,
                    extra TEXT DEFAULT '{}',
                    uid INTEGER DEFAULT 0,
                    UNIQUE(message_id, row_key, stage)
                );
                CREATE INDEX IF NOT EXISTS idx_error_queue_status ON error_queue(status);
                CREATE INDEX IF NOT EXISTS idx_error_queue_message_id ON error_queue(message_id);
                -- 任务 20 / D3：告警折叠的窗口与计数。**必须落库**（放内存重启就重复告警）。
                CREATE TABLE IF NOT EXISTS alarm_state (
                    key          TEXT PRIMARY KEY,   -- D3 的 alarm key
                    window_start TEXT NOT NULL,      -- 首次触发时间（UTC，与 datetime('now') 同基准）
                    count        INTEGER NOT NULL DEFAULT 0,
                    last_sent_at TEXT
                );
                """
            )
            # Migrations for DBs created before these columns existed.
            for _ddl in (
                "ALTER TABLE error_queue ADD COLUMN extra TEXT DEFAULT '{}'",
                "ALTER TABLE error_queue ADD COLUMN uid INTEGER DEFAULT 0",
            ):
                try:
                    conn.execute(_ddl)
                except sqlite3.OperationalError:
                    pass  # column already present
            # 任务 20：dedup 三态状态机新增 8 列。老库（只有 3 列）在这里补齐；
            # 先 PRAGMA 判断再 ALTER，新库/重复启动都幂等（不靠「异常回滚」，DDL 在
            # Python sqlite3 里不会自动开事务）。
            _dedup_cols = {_r[1] for _r in conn.execute("PRAGMA table_info(dedup)")}
            for _col, _ddl in _DEDUP_NEW_COLUMNS:
                if _col not in _dedup_cols:
                    conn.execute(_ddl)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dedup_status ON dedup(status)")
            conn.commit()
        finally:
            conn.close()


def try_claim(message_id: str, row_key: str = "", *, account: str = "",
              folder: str = "", uid: int = 0) -> bool:
    """三态认领（任务 20 / D2）。True = 本次可以进入 route + decide。

    判定顺序（写死）：
      ① 先查拼接键 `f"{mid}|{row_key}"`：
         · 不存在 → ② 再查裸 mid（G1 对账写的是整封级键，拆不出 row_key）
         · 存在且 unresolved → **允许再次认领**（刷 last_try_at、补定位字段、
           retry_count 不清零），返回 True。缺这一条，池子里的件永远进不到
           _process_row，D2 直接失效。
         · 存在且 sent / closed → 返回 False（已发永不重发 / 已判永不重判）
      ② 裸 mid 存在且 sent / closed ⇒ 整封视为已处理，**返回 False**（该封所有行都不再发）。
         裸 mid 是 unresolved ⇒ 走「允许重判」分支，但**一律按拼接键插入/更新，
         不复用裸 mid 那一行**（对账不会写 unresolved，此分支仅为防御）。
      ③ 两者都不存在 → 插入 status='unresolved' + last_try_at + 定位字段，返回 True。

    account / folder / uid 由 _process_row 传入并落库：pending 与 alarm 分支**不写**
    error_queue，这三列是它们进池后取原文的**唯一**来源，空着就只能等 mail_gone。
    """
    if not message_id:
        return False
    full_key = join_key(message_id, row_key)
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT status FROM dedup WHERE message_id = ?", (full_key,)
            ).fetchone()
            if row is not None:
                if row["status"] == STATUS_UNRESOLVED:
                    conn.execute(
                        "UPDATE dedup SET last_try_at = datetime('now'), "
                        "  account = CASE WHEN COALESCE(account, '') = '' THEN ? ELSE account END, "
                        "  folder = CASE WHEN COALESCE(folder, '') = '' THEN ? ELSE folder END, "
                        "  uid = CASE WHEN COALESCE(uid, 0) = 0 THEN ? ELSE uid END "
                        "WHERE message_id = ?",
                        (account or "", folder or "", int(uid or 0), full_key),
                    )
                    conn.commit()
                    _log.log_dedup_claim(message_id, True, row_key)
                    return True
                _log.log_dedup_claim(message_id, False, row_key)
                return False
            if row_key:
                bare = conn.execute(
                    "SELECT status FROM dedup WHERE message_id = ?", (message_id,)
                ).fetchone()
                if bare is not None and bare["status"] != STATUS_UNRESOLVED:
                    _log.log_dedup_claim(message_id, False, row_key)
                    return False
            conn.execute(
                "INSERT INTO dedup (message_id, claimed_at, claimed_by, status, "
                "                   last_try_at, source, account, folder, uid, retry_count) "
                "VALUES (?, datetime('now'), ?, ?, datetime('now'), 'auto', ?, ?, ?, 0)",
                (full_key, threading.current_thread().name, STATUS_UNRESOLVED,
                 account or "", folder or "", int(uid or 0)),
            )
            conn.commit()
            _log.log_dedup_claim(message_id, True, row_key)
            return True
        finally:
            conn.close()


def release_claim(message_id: str, row_key: str = ""):
    """**仅供人工路径删除脏键**（例：--pool 时发现键本身是坏的）。

    🔴 任务 20 / D1：sweep_once 的重放**一律不许调它** —— 认领只有三种状态
    （sent / closed / unresolved），重放只改 status、不释放认领。释放会把
    「已发出的件」重新变回可处理，正是 2026-11-16 09:52 重复转发的成因。
    """
    if not message_id:
        return
    full_key = join_key(message_id, row_key)
    with _db_lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM dedup WHERE message_id = ?", (full_key,))
            conn.commit()
        finally:
            conn.close()


def is_claimed(message_id: str, row_key: str = "") -> bool:
    if not message_id:
        return False
    full_key = f"{message_id}|{row_key}" if row_key else message_id
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute("SELECT 1 FROM dedup WHERE message_id = ?", (full_key,))
            return cursor.fetchone() is not None
        finally:
            conn.close()


def set_status(message_id: str, row_key: str = "", status: str = "",
               source: str = "", close_reason: str = "") -> bool:
    """唯一的状态写入口。非法 status 抛 ValueError（禁止第四种取值）。

    source 为空时**保留原值**（不覆盖）：source 记的是「谁把这件放到这个状态」。
    自动重判再判一次仍是 unresolved，并不改变「这件是人工 reopen 放进来的」这个
    事实 —— 覆盖成空会让 `--pool retry` 之后查不到人工留痕（05_ACCEPTANCE 判据 18
    要求 reopen 后 source='manual'）。
    """
    if status not in STATUSES:
        raise ValueError(f"invalid dedup status: {status!r}")
    if not message_id:
        return False
    full_key = join_key(message_id, row_key)
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE dedup SET status = ?, "
                "  source = CASE WHEN ? = '' THEN source ELSE ? END, "
                "  close_reason = ?, last_try_at = datetime('now') "
                "WHERE message_id = ?",
                (status, source or "", source or "", close_reason or "", full_key),
            )
            if cursor.rowcount == 0:
                # 键还不存在（例：--pool close 一个人工认定的件）：补插一行，
                # claimed_by 留痕为 source。
                conn.execute(
                    "INSERT INTO dedup (message_id, claimed_at, claimed_by, status, "
                    "                   last_try_at, source, close_reason, retry_count) "
                    "VALUES (?, datetime('now'), ?, ?, datetime('now'), ?, ?, 0)",
                    (full_key, source or "manual", status, source or "", close_reason or ""),
                )
            conn.commit()
            return True
        finally:
            conn.close()


def reopen(message_id: str, row_key: str = "", operator: str = "") -> bool:
    """人工把 closed 放回池子：closed → unresolved。

    · closed  → unresolved：清 close_reason、source='manual'、retry_count=0、
      claimed_by 记 operator（留痕）。
    · sent    → **一律拒绝并返回 False**（已发永不重发，不提供强行重发开关）。
    · unresolved → 幂等返回 True（本来就在池子里，只补 operator 留痕）。
    · 键不存在 → False。
    """
    if not message_id:
        return False
    full_key = join_key(message_id, row_key)
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT status FROM dedup WHERE message_id = ?", (full_key,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] == STATUS_SENT:
                return False
            if row["status"] == STATUS_UNRESOLVED:
                if operator:
                    conn.execute(
                        "UPDATE dedup SET claimed_by = ? WHERE message_id = ?",
                        (operator, full_key),
                    )
                    conn.commit()
                return True
            conn.execute(
                "UPDATE dedup SET status = ?, source = 'manual', close_reason = '', "
                "last_try_at = datetime('now'), retry_count = 0, claimed_by = ? "
                "WHERE message_id = ?",
                (STATUS_UNRESOLVED, operator or "manual", full_key),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def bump_retry(message_id: str, row_key: str = "") -> int:
    """原文取不到的「连续次数」+1，返回新值。**落盘**，不放内存（重启会归零）。"""
    if not message_id:
        return 0
    full_key = join_key(message_id, row_key)
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE dedup SET retry_count = COALESCE(retry_count, 0) + 1, "
                "last_try_at = datetime('now') WHERE message_id = ?",
                (full_key,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT retry_count FROM dedup WHERE message_id = ?", (full_key,)
            ).fetchone()
            return int(row["retry_count"] or 0) if row else 0
        finally:
            conn.close()


def reset_retry(message_id: str, row_key: str = "") -> None:
    """原文取到后把「连续取不到」计数归零（成功路径清零，口径见 REPORT）。"""
    if not message_id:
        return
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE dedup SET retry_count = 0 WHERE message_id = ?",
                (join_key(message_id, row_key),),
            )
            conn.commit()
        finally:
            conn.close()


def _to_item(row) -> Dict[str, Any]:
    """dedup 行 → 对外 dict（message_id 拆成裸 mid，key 保留拼接原值）。"""
    bare_mid, row_key = split_key(row["message_id"])
    return {
        "message_id": bare_mid,
        "row_key": row_key,
        "key": row["message_id"],
        "status": row["status"],
        "claimed_at": row["claimed_at"],
        "last_try_at": row["last_try_at"],
        "retry_count": int(row["retry_count"] or 0),
        "source": row["source"] or "",
        "close_reason": row["close_reason"] or "",
        "account": row["account"] or "",
        "folder": row["folder"] or "",
        "uid": int(row["uid"] or 0),
    }


def list_unresolved(older_than_days: Optional[int] = None,
                    limit: int = 500) -> List[Dict[str, Any]]:
    """未发出池：全部 unresolved 行（最老的在前）。

    older_than_days 给了就只取「最后一次尝试早于 N 天」的（超期告警用）。
    时间基准统一 UTC（写侧 datetime('now')），不在 Python 里拼本地时间串。
    """
    sql = "SELECT * FROM dedup WHERE status = ?"
    params: List[Any] = [STATUS_UNRESOLVED]
    if older_than_days is not None:
        sql += " AND COALESCE(last_try_at, claimed_at) <= datetime('now', ?)"
        params.append(f"-{int(older_than_days)} days")
    sql += " ORDER BY COALESCE(last_try_at, claimed_at) ASC, claimed_at ASC LIMIT ?"
    params.append(int(limit))
    with _db_lock:
        conn = _connect()
        try:
            return [_to_item(r) for r in conn.execute(sql, tuple(params))]
        finally:
            conn.close()


def item_age_days(message_id: str, row_key: str = "") -> int:
    """该件「已在池天数」= now - COALESCE(last_try_at, claimed_at)（整数天）。

    时间基准统一 UTC（SQLite datetime('now')），不在 Python 里拼本地时间串 ——
    混基准会让「超 2 天」静默失效（本仓踩过这个坑）。键不存在返回 0。
    """
    if not message_id:
        return 0
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT CAST(julianday('now') - julianday("
                "  COALESCE(last_try_at, claimed_at)) AS INTEGER) AS d "
                "FROM dedup WHERE message_id = ?",
                (join_key(message_id, row_key),),
            ).fetchone()
            return int(row["d"] or 0) if row else 0
        finally:
            conn.close()


def count_unresolved() -> int:
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM dedup WHERE status = ?", (STATUS_UNRESOLVED,)
            ).fetchone()
            return int(row["c"] or 0)
        finally:
            conn.close()


def is_sent(message_id: str, row_key: str = "") -> bool:
    """状态查询：同样先查拼接键、再查裸 mid。True 只代表 status == 'sent'。"""
    if not message_id:
        return False
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT status FROM dedup WHERE message_id = ?",
                (join_key(message_id, row_key),),
            ).fetchone()
            if row is not None:
                return row["status"] == STATUS_SENT
            if row_key:
                row = conn.execute(
                    "SELECT status FROM dedup WHERE message_id = ?", (message_id,)
                ).fetchone()
                return bool(row is not None and row["status"] == STATUS_SENT)
            return False
        finally:
            conn.close()


def get_status(message_id: str, row_key: str = "") -> Optional[str]:
    """键的状态（拼接键优先、裸 mid 兜底）；不存在返回 None。"""
    if not message_id:
        return None
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT status FROM dedup WHERE message_id = ?",
                (join_key(message_id, row_key),),
            ).fetchone()
            if row is not None:
                return row["status"]
            if row_key:
                row = conn.execute(
                    "SELECT status FROM dedup WHERE message_id = ?", (message_id,)
                ).fetchone()
                return row["status"] if row is not None else None
            return None
        finally:
            conn.close()


def insert_if_absent(message_id: str, status: str, source: str = "",
                     claimed_by: str = "reconcile", close_reason: str = "") -> bool:
    """只增不删、**已存在的键不覆盖**（G1 对账专用）。True = 本次新写入。

    message_id 由调用方给**裸 mid**（旧库只有整封粒度，拆不出 row_key）。
    status 合法性仍在这里把关，非法值抛 ValueError（禁止第四种取值）。
    """
    if status not in STATUSES:
        raise ValueError(f"invalid dedup status: {status!r}")
    if not message_id:
        return False
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO dedup (message_id, claimed_at, claimed_by, status, "
                "                   last_try_at, source, close_reason, retry_count) "
                "VALUES (?, datetime('now'), ?, ?, datetime('now'), ?, ?, 0)",
                (message_id, claimed_by or "reconcile", status, source or "",
                 close_reason or ""),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


# ---------------------------------------------------------------- 未发出池统计（日报用）

_REASON_NAMES = {
    ("identify", "UNCLASSIFIED"): "unclassified",
    ("identify", "IDENTIFY_EXCEPTION"): "unclassified",
    ("extract", "EXTRACT_EXCEPTION"): "extract_failed",
    ("extract", "EMPTY_ROWS"): "extract_failed",
    ("routing", "NO_ROUTE"): "no_route",
    ("act", "ACTION_FAILED"): "act_failed",
    ("act", "MISSING_CREDENTIALS"): "config_missing",
    ("act", "BOUNCE"): "bounced",
}


def _reason_of(stage: Optional[str], error_type: Optional[str],
               error_detail: Optional[str]) -> str:
    """错误条目 → 池子分组名（**只用于日报归类**，不参与任何判定）。"""
    if (error_type or "") == "NO_ROUTE" and (error_detail or "") == "num_multi_company":
        return "num_multi_company"
    name = _REASON_NAMES.get((stage or "", error_type or ""))
    if name:
        return name
    if error_type:
        return "other:%s" % error_type
    # 没有任何 error_queue 条目 = pending / alarm 分支（走通知路径，不写错误队列）
    return "pending"


def unresolved_summary(limit: int = 2000) -> Dict[str, Any]:
    """未发出池汇总：总件数 / 按原因分组 / 最老一件几天（日报「未发出池」节用）。"""
    with _db_lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT d.message_id, "
                "       CAST(julianday('now') - julianday(d.claimed_at) AS INTEGER) AS age_days, "
                "       e.stage AS e_stage, e.error_type AS e_error_type, "
                "       e.error_detail AS e_error_detail "
                "  FROM dedup d "
                "  LEFT JOIN error_queue e "
                "    ON d.message_id = e.message_id || CASE WHEN COALESCE(e.row_key, '') = '' "
                "         THEN '' ELSE '|' || e.row_key END "
                "   AND e.status IN ('pending', 'manual') "
                " WHERE d.status = ? "
                " ORDER BY d.claimed_at ASC LIMIT ?",
                (STATUS_UNRESOLVED, int(limit)),
            ).fetchall()
        finally:
            conn.close()

    by_reason: Dict[str, int] = {}
    seen = set()
    oldest_days = 0
    for r in rows:
        key = r["message_id"]
        if key in seen:            # 同一个键可能有多条 error_queue（不同 stage）
            continue
        seen.add(key)
        reason = _reason_of(r["e_stage"], r["e_error_type"], r["e_error_detail"])
        by_reason[reason] = by_reason.get(reason, 0) + 1
        oldest_days = max(oldest_days, int(r["age_days"] or 0))
    return {"total": len(seen), "by_reason": by_reason, "oldest_days": oldest_days}


# ---------------------------------------------------------------- 告警折叠状态（D3 落库）

def get_alarm_state(key: str) -> Optional[Dict[str, Any]]:
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT key, window_start, count, last_sent_at FROM alarm_state WHERE key = ?",
                (key,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def bump_alarm(key: str, window_sec: int = 86400) -> Dict[str, Any]:
    """同一 key 的 24h 折叠：返回 {"send": bool, "count": n, "previous": m}。

    · 窗口不存在 → 建窗 + 发（send=True, count=1）
    · 窗口仍在   → 只累加计数，不发（send=False）
    · 窗口已过期 → 重建窗口 + 发（send=True, previous=旧计数，供「共 N 次」补发）
    时间一律走 SQLite datetime('now')（UTC），与写侧同基准，不混本地时间串。
    """
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT window_start, count FROM alarm_state WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO alarm_state (key, window_start, count, last_sent_at) "
                    "VALUES (?, datetime('now'), 1, datetime('now'))",
                    (key,),
                )
                conn.commit()
                return {"send": True, "count": 1, "previous": 0}
            prev = int(row["count"] or 0)
            expired = conn.execute(
                "SELECT CASE WHEN ? <= datetime('now', ?) THEN 1 ELSE 0 END AS x",
                (row["window_start"], f"-{int(window_sec)} seconds"),
            ).fetchone()["x"]
            if expired:
                conn.execute(
                    "UPDATE alarm_state SET window_start = datetime('now'), count = 1, "
                    "last_sent_at = datetime('now') WHERE key = ?", (key,)
                )
                conn.commit()
                return {"send": True, "count": 1, "previous": prev}
            conn.execute("UPDATE alarm_state SET count = count + 1 WHERE key = ?", (key,))
            conn.commit()
            return {"send": False, "count": prev + 1, "previous": prev}
        finally:
            conn.close()


def list_expired_alarms(window_sec: int = 86400,
                        limit: int = 200) -> List[Dict[str, Any]]:
    """窗口已过期的告警 key（sweeper 补发一条「共 N 次」后删掉）。"""
    with _db_lock:
        conn = _connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT key, window_start, count FROM alarm_state "
                "WHERE window_start <= datetime('now', ?) LIMIT ?",
                (f"-{int(window_sec)} seconds", int(limit)),
            )]
        finally:
            conn.close()


def delete_alarm_state(key: str) -> None:
    with _db_lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM alarm_state WHERE key = ?", (key,))
            conn.commit()
        finally:
            conn.close()


def claim_manual_forward(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET status='resolved' WHERE id=? AND status IN ('pending','manual')",
                (error_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def _read_extra(conn, error_id: int) -> dict:
    import json
    cursor = conn.execute("SELECT extra FROM error_queue WHERE id=?", (error_id,))
    row = cursor.fetchone()
    if not row or not row["extra"]:
        return {}
    try:
        data = json.loads(row["extra"])
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_error_notified(error_id: int) -> bool:
    """True iff an E1 alert was already sent for this error-queue entry
    (extra.notified_manual_at set)."""
    with _db_lock:
        conn = _connect()
        try:
            return bool(_read_extra(conn, error_id).get("notified_manual_at"))
        finally:
            conn.close()


def set_error_notified(error_id: int) -> bool:
    """Record that the E1 first-alert for this entry has been sent."""
    import json
    from datetime import datetime
    with _db_lock:
        conn = _connect()
        try:
            extra = _read_extra(conn, error_id)
            extra["notified_manual_at"] = datetime.now().isoformat(timespec="seconds")
            cursor = conn.execute(
                "UPDATE error_queue SET extra=? WHERE id=?",
                (json.dumps(extra, ensure_ascii=False), error_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def reopen_error(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET status='manual', attempt_count=0 WHERE id=? AND status='resolved'",
                (error_id,),
            )
            if cursor.rowcount:
                # Reopened entries may alert again on the next sweep.
                extra = _read_extra(conn, error_id)
                extra.pop("notified_manual_at", None)
                import json
                conn.execute(
                    "UPDATE error_queue SET extra=? WHERE id=?",
                    (json.dumps(extra, ensure_ascii=False), error_id),
                )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def add_error(
    message_id: str,
    row_key: str,
    stage: str,
    error_type: str,
    error_detail: str,
    error_id: str,
    account: str = "",
    folder: str = "",
    uid: int = 0,
    subject: str = "",
    sender: str = "",
    date_hdr: str = "",
    raw_bytes: bytes = b"",
) -> int:
    """Queue a failed row for retry, together with its replay payload.

    Extra replay fields are keyword-only with defaults so old 6-argument
    callers keep working. Raw mail over RAW_MAX_BYTES is NOT stored (a
    truncated payload could never replay correctly); that case logs WARN +
    counter instead of silently truncating.
    """
    from .notify import increment_counter
    from ..config import RAW_MAX_BYTES

    raw_hex = ""
    if raw_bytes:
        if len(raw_bytes) > RAW_MAX_BYTES:
            _log.warning(
                f"error_queue payload too large, not stored | msg_id={message_id[:50]} "
                f"| bytes={len(raw_bytes)} limit={RAW_MAX_BYTES}"
            )
            increment_counter("raw_too_large")
        else:
            raw_hex = raw_bytes.hex()
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO error_queue
                   (message_id, row_key, stage, error_type, error_detail, error_id, attempt_count, last_attempt_at, status, created_at,
                    raw_hex, account, folder, uid, subject, sender, date)
                   VALUES (?, ?, ?, ?, ?, ?, 0, datetime('now'), 'pending', datetime('now'),
                           ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, row_key, stage, error_type, error_detail, error_id,
                 raw_hex, account, folder, uid or 0, subject, sender, date_hdr),
            )
            conn.commit()
            if cursor.lastrowid:
                return cursor.lastrowid
            cursor = conn.execute(
                "SELECT id FROM error_queue WHERE message_id=? AND row_key=? AND stage=?",
                (message_id, row_key, stage),
            )
            row = cursor.fetchone()
            return row[0] if row else -1
        finally:
            conn.close()


def has_pending_error(message_id: str, row_key: str) -> bool:
    """True iff a pending error-queue entry already exists for this row.
    Used by the per-row guard so one row failure lands exactly one entry."""
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "SELECT 1 FROM error_queue WHERE message_id=? AND row_key=? AND status='pending'",
                (message_id, row_key),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


def has_pending_message(message_id: str) -> bool:
    """True iff any pending error-queue entry exists for this whole email.
    Used by the end-of-mail mark-seen rule: a mail with unsettled rows
    stays unread."""
    if not message_id:
        return False
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "SELECT 1 FROM error_queue WHERE message_id=? AND status='pending'",
                (message_id,),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


def get_pending_errors(limit: int = 100) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM error_queue WHERE status='pending' ORDER BY created_at LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()


def get_manual_errors(limit: int = 100) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM error_queue WHERE status='manual' ORDER BY created_at LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()


def increment_attempt(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET attempt_count = attempt_count + 1, last_attempt_at = datetime('now') WHERE id=?",
                (error_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def mark_error_resolved(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET status='resolved' WHERE id=?",
                (error_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def mark_error_manual(error_id: int) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE error_queue SET status='manual' WHERE id=?",
                (error_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def purge_old_dedup(days: int = DEDUP_RETENTION_DAYS) -> int:
    """🔴 保持**零调用点**（任务 20）—— 认领必须永不过期。

    删掉过保留期的 dedup 行 = 把「已发」这个硬终态抹掉，那些件会在下一轮扫描里
    被当成新邮件重发。D1 之后状态机是唯一真相，过期清理的语义与它直接冲突，
    所以本函数只保留定义（人工应急用），任何自动路径都不许调。
    """
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "DELETE FROM dedup WHERE claimed_at < datetime('now', ?)", (f"-{days} days",)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


init_db()