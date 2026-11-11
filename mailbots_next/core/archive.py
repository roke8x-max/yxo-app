# -*- coding: utf-8 -*-
"""放箱令 / 还箱令 —— 复制归档与每日核对删除（任务 12 / spec 04）。

一句话：转发成功后把邮件**复制**到「放箱令 / 还箱令」夹（当天不删）；
每天 12:00 核对副本确实存在之后，才把源邮件从收信夹删掉。

本项目唯一不可逆的动作是"删邮件"，所以拆成两段：

  第一段 · 实时（`copy_after_forward` / `archive_copy_many`）
    查目标夹是否已有副本（幂等）→ 取源夹 UIDVALIDITY → UID COPY
    → 记账 `status='copied'`。⚠️ 源邮件一封都不删。

  第二段 · 每天 12:00（`purge_verified`）
    取 `status IN ('copied','verified')` 且复制满 1 小时 →
    目标夹核对副本存在（→ `verified`）→ 源夹比对 UIDVALIDITY →
    删前重新 FETCH 校验 Message-ID → `UID STORE \\Deleted` → `UID EXPUNGE`
    → `status='deleted'`。

三条协议事实（阿里企业邮实测，见 spec §2）：
  * **没有 MOVE 扩展** ⇒ 只能 COPY + STORE + EXPUNGE 三步，顺序不能反；
  * **有 UIDPLUS** ⇒ `UID EXPUNGE <uid 列表>` 精确清指定 UID，不动同夹其它信；
  * **`UID SEARCH HEADER Message-ID` 可用** ⇒ 能跨夹精确核对副本。

四条红线：
  1. 删除只用 `UID EXPUNGE <uid 列表>`；本模块不出现整夹清理命令，
     也不出现不带 uid 的 expunge（那会清掉整个文件夹）。
  2. 核对不到副本 ⇒ `failed_verify`，**永久不删**，只告警。
  3. UIDVALIDITY 变了 ⇒ 该组直接落 `failed_verify(uidvalidity_changed)` 终态。
     "保持原状态"会让取数条件（含 verified）每天重捞它、每天告警、永远出不去
     —— 这是"卡在状态入口"，必须靠外部事实（人工确认）收尾。
  4. 非 live 模式 0 次 COPY / 0 次 EXPUNGE，且**不写库**（写 skipped 只会污染库）。

时间口径（踩过的坑）：`copied_at` 用 SQLite 的 `datetime('now')`（**UTC**）写入，
时间闸也用 `datetime('now','-1 hour')` 在 SQL 里比。**不要**混用 Python 本地时间
字符串（UTC+8），否则闸门恒不成立、purge 永远扫不到到期记录。
"""
import base64
import email
import email.policy
import imaplib
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import config
from ..config import IMAP_PORT, IMAP_SERVER, OPS_OWNER_EMAIL
from ..config.secrets import get_accounts
from .dedup import _db_lock
from .dedup import get_connection as get_dedup_connection
from .ingest import (
    _extract_raw_from_fetch,
    _get_uidvalidity,
    _parse_uid_search,
    _server_folder,
    mark_seen,
)
from .log import get_logger
from .notify import get_notifier, increment_counter

_log = get_logger(__name__)

# ---------------------------------------------------------------- 常量

ST_COPIED = "copied"
ST_VERIFIED = "verified"
ST_DELETED = "deleted"
ST_FAILED_COPY = "failed_copy"
ST_FAILED_VERIFY = "failed_verify"
ST_SKIPPED = "skipped"

# 幂等集合：这三态是"真的完成过"，再来一次直接返回 exists。
# 🔴 failed_copy / failed_verify **不在**这里 —— "不自动重试" ≠ "永久拒绝"。
# 把它们放进幂等集合，用户即使重放邮件也永远修不好归档失败。
_DONE_STATES = (ST_COPIED, ST_VERIFIED, ST_DELETED)

# 目标夹映射：本期只归档放箱令 / 还箱令（其它 5 类不改拓扑，返回 None 即跳过）
TARGET_BY_TYPE = {
    "release": "放箱令",
    "release_cancel": "放箱令",
    "retbox": "还箱令",
}

# 只允许自动建这 8 个固定名字。第 8 个「自动化」是收信夹本身（被删了要能建回来）。
_AUTO_CREATE = {"放箱令", "还箱令", "DSK", "ATB", "运单草单", "运单号", "Tracing", "自动化"}

PURGE_LIMIT = 500          # 单次上限（洋定，超了顺延次日）
PURGE_MIN_AGE_SEC = 3600   # 时间闸：复制满 1 小时才处理

_TIME_GATE = "'-1 hour'"

_DDL = """
CREATE TABLE IF NOT EXISTS archive_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT NOT NULL,
    account       TEXT NOT NULL,
    src_folder    TEXT NOT NULL,
    src_uid       INTEGER NOT NULL,
    src_uidvalidity INTEGER DEFAULT 0,
    target_folder TEXT NOT NULL,
    copy_uid      INTEGER DEFAULT 0,
    copied_at     TEXT,
    verified_at   TEXT DEFAULT '',
    deleted_at    TEXT DEFAULT '',
    status        TEXT DEFAULT 'copied',
    fail_reason   TEXT DEFAULT '',
    UNIQUE(account, message_id, target_folder)
);
CREATE INDEX IF NOT EXISTS idx_al_status ON archive_log(status, copied_at);
"""

_COPYUID_RE = re.compile(r"COPYUID\s+\d+\s+([\d,:*]+)\s+([\d,:*]+)", re.IGNORECASE)


# ---------------------------------------------------------------- 建表 / 连接

def init_table() -> None:
    """建 `archive_log`（启动期执行，**不可抛异常** —— 照 store.init_forward_log 的写法）。

    落 `dedup.db`（与 error_queue / release_batch 同库），用 dedup 的
    `get_connection` + 它的 `_db_lock`；若照抄 store 那套 bot_config 连接 helper，
    表会被建到 `bot_config.db`（错库）。
    """
    try:
        with _db_lock:
            dbc = get_dedup_connection()      # ← sqlite 连接（与 IMAP conn 区分开）
            try:
                dbc.executescript(_DDL)
                dbc.commit()
            finally:
                dbc.close()
    except Exception as e:
        _log.error(f"init_table failed (non-fatal): {type(e).__name__}: {e}")


def _rows(sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
    with _db_lock:
        dbc = get_dedup_connection()
        try:
            return [dict(r) for r in dbc.execute(sql, params).fetchall()]
        finally:
            dbc.close()


def _exec(sql: str, params: Tuple = ()) -> int:
    with _db_lock:
        dbc = get_dedup_connection()
        try:
            cur = dbc.execute(sql, params)
            dbc.commit()
            return cur.rowcount
        finally:
            dbc.close()


# ---------------------------------------------------------------- 文件夹名编码

def _utf7_encode(name: str) -> str:
    """普通字符串 → IMAP modified UTF-7（RFC 3501 §5.1.3）。

    imaplib 只接受 ASCII mailbox 名，直接把「放箱令」交给 `conn.select()` 会抛
    UnicodeEncodeError。IDLE_GROUPS 里配了 `folder_utf7` 的走 `_server_folder`，
    没配的（放箱令 / 还箱令 / 自动化 等归档目标夹）必须在这里现场编码。

    自检：`运单草单 -> &j9BTVYNJU1U-`、`运单号 -> &j9BTVVP3-`
    （与 settings.IDLE_GROUPS 里手写的值逐字一致）。
    """
    out: List[str] = []
    buf: List[str] = []

    def _flush() -> None:
        if not buf:
            return
        b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii")
        out.append("&" + b64.replace("/", ",").rstrip("=") + "-")
        del buf[:]

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            _flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    _flush()
    return "".join(out)


def _imap_folder(folder: str) -> str:
    """显示名 → 能直接发给服务器的 mailbox 名（先查 IDLE_GROUPS，再兜底编码）。"""
    mapped = _server_folder(folder or "")
    if not mapped:
        return mapped
    try:
        mapped.encode("ascii")
        return mapped
    except UnicodeEncodeError:
        return _utf7_encode(mapped)


def _new_conn() -> imaplib.IMAP4:
    return imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=60)


def _login(account: str) -> Tuple[Optional[imaplib.IMAP4], str]:
    """登录一个账号。返回 (conn, err)；err 非空时 conn 为 None。"""
    password = get_accounts().get(account, "")
    if not password:
        return None, f"no IMAP credentials for account={account}"
    try:
        conn = _new_conn()
        conn.login(account, password)
        return conn, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _logout(conn) -> None:
    if conn is None:
        return
    try:
        conn.logout()
    except Exception:
        pass


def _select(conn, server_folder: str, readonly: bool = False) -> str:
    """SELECT 一个夹，返回 typ（失败统一 "NO"，不抛）。"""
    try:
        typ, _ = conn.select(server_folder, readonly=readonly)
        return typ or "NO"
    except Exception as e:
        _log.warning(f"IMAP select failed | folder={server_folder} | {type(e).__name__}: {e}")
        return "NO"


# ---------------------------------------------------------------- IMAP 小工具

def _require_uidvalidity(conn) -> Optional[int]:
    """取**当前已 SELECT 夹**的 UIDVALIDITY；取不到返回 None。

    🔴 两条写 `copied` 的路径（正常 COPY / recovered 补记录）都必须过这一关。
    漏了 recovered 那条 ⇒ 补出来的记录 `src_uidvalidity=0` ⇒ purge 判"不一致"
    ⇒ `failed_verify` + 告警 ⇒ 这批邮件永不删除、天天有人查告警，
    而根因只是补库时少取一个字段。
    """
    try:
        raw = _get_uidvalidity(conn)
        val = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _search_message_id(conn, message_id: str) -> bool:
    """目标夹里是否已有这个 Message-ID 的副本。"""
    mid = "".join(str(message_id or "").split()).replace('"', "")
    if not mid:
        return False
    typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{mid}"')
    if typ != "OK":
        raise RuntimeError(f"UID SEARCH HEADER Message-ID rejected: typ={typ}")
    return bool(_parse_uid_search(data))


def _uid_exists(conn, uid: int) -> bool:
    """UID 是否还在（只用于"fetch 没拿到 payload"时二次确认）。判不了一律当"还在"。"""
    try:
        typ, data = conn.uid("SEARCH", None, "UID", str(int(uid)))
    except Exception:
        return True
    if typ != "OK":
        return True
    return bool(_parse_uid_search(data))


def _fetch_message_id(conn, uid: int) -> Optional[str]:
    """第③闸：删前重新 FETCH 源邮件头，拿 Message-ID。

    返回 None  = **这个 UID 已经不存在** ⇒ 上次删成功了、只是没记账（标 deleted，不告警）
    返回 str   = 邮件还在（可能是空串：头取不到 ⇒ 调用方按"不一致"处理，绝不删）

    🔴 "UID 不存在"和"Message-ID 不一致"必须分开判：前者是成功后的记账缺失，
    后者是 UID 被别的信复用的危险信号。混成一个分支，要么白告警刷屏、要么删错信。
    """
    typ, data = conn.uid("FETCH", str(int(uid)), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    if typ != "OK":
        return ""
    raw = _extract_raw_from_fetch(data)
    if raw is None:
        # 没 payload 多半是 UID 已不存在；但先二次确认 ——
        # 把"取不到头"当成"已删"会直接删掉一封没核对过的邮件。
        return "" if _uid_exists(conn, uid) else None
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        return str(msg.get("Message-ID") or "").strip()
    except Exception:
        return ""


def _norm_mid(value) -> str:
    """Message-ID 归一化后再比（去尖括号 / 空白 / 大小写）。"""
    s = "".join(str(value or "").split())
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1]
    return s.lower()


def _parse_copyuid(data) -> int:
    """从 UIDPLUS 的 COPYUID 响应里取副本在新夹里的 UID；取不到存 0。"""
    for item in (data or []):
        if isinstance(item, tuple):
            item = item[0]
        text = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item or "")
        m = _COPYUID_RE.search(text)
        if m:
            head = m.group(2).split(",")[0].split(":")[0].strip()
            try:
                return int(head)
            except ValueError:
                return 0
    return 0


# ---------------------------------------------------------------- 记账

_INSERT_SQL = """
INSERT INTO archive_log
    (message_id, account, src_folder, src_uid, src_uidvalidity, target_folder,
     copy_uid, copied_at, status, fail_reason)
VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
ON CONFLICT(account, message_id, target_folder) DO UPDATE SET
    src_folder      = excluded.src_folder,
    src_uid         = excluded.src_uid,
    src_uidvalidity = excluded.src_uidvalidity,
    copy_uid        = excluded.copy_uid,
    copied_at       = excluded.copied_at,
    verified_at     = '',
    deleted_at      = '',
    status          = excluded.status,
    fail_reason     = excluded.fail_reason
"""


def _write_record(account: str, folder: str, uid: int, message_id: str,
                  target_folder: str, status: str, reason: str = "",
                  src_uidvalidity: int = 0, copy_uid: int = 0) -> None:
    """写/更新一条 archive_log。唯一键 (account, message_id, target_folder) 是幂等依据。

    ⚠️ `message_id` 为空时唯一键退化（同一账号+目标夹只有一行），这是 DDL 的
    既定取舍 —— 这类信只做"留痕 + 计数"，本来也不允许跨夹核对/删除。
    """
    try:
        _exec(_INSERT_SQL, (message_id or "", account or "", folder or "", int(uid or 0),
                            int(src_uidvalidity or 0), target_folder or "", int(copy_uid or 0),
                            status, reason or ""))
    except Exception as e:
        _log.error(f"archive_log write failed | msg={message_id[:50]} status={status}: "
                   f"{type(e).__name__}: {e}")


def _get_record(account: str, message_id: str, target_folder: str) -> Optional[Dict[str, Any]]:
    rows = _rows("SELECT * FROM archive_log WHERE account=? AND message_id=? AND target_folder=?",
                 (account or "", message_id or "", target_folder or ""))
    return rows[0] if rows else None


def _set_row_status(rec_id: int, status: str, reason: Optional[str] = None,
                    verified_at: bool = False, deleted_at: bool = False) -> None:
    sets = ["status=?"]
    params: List[Any] = [status]
    if reason is not None:
        sets.append("fail_reason=?")
        params.append(reason)
    if verified_at:
        sets.append("verified_at=datetime('now')")
    if deleted_at:
        sets.append("deleted_at=datetime('now')")
    params.append(int(rec_id))
    _exec(f"UPDATE archive_log SET {', '.join(sets)} WHERE id=?", tuple(params))


def _mark_rows(rows: Iterable[Dict[str, Any]], status: str, reason: str) -> None:
    """逐条落状态（同时把内存行改成新状态，避免后续步骤再碰它）。"""
    for r in rows:
        _set_row_status(r["id"], status, reason)
        r["status"] = status
        r["fail_reason"] = reason


def _mark_group_uidvalidity(account: str, src_folder: str, target_folder: str) -> int:
    """UIDVALIDITY 永久不一致：该小组（含 target_folder）统一落到 failed_verify。

    🔴 过滤条件必须与分组层级对齐：处理是"大组(target_folder) → 小组(account, src_folder)"
    两级，UPDATE 少了 `target_folder` 会把同一账号同一源夹下**别的目标夹**的记录一起改掉。
    """
    return _exec(
        "UPDATE archive_log SET status='failed_verify', fail_reason='uidvalidity_changed'"
        " WHERE account=? AND src_folder=? AND target_folder=?"
        " AND status IN ('copied','verified')",
        (account or "", src_folder or "", target_folder or ""),
    )


def _customer_codes(message_ids: Iterable[str]) -> Dict[str, str]:
    """可选增强：从 release_item（任务 11 的池表，同库）反查客户编码，让告警可定位。

    表可能还没建（老库 / 单测）⇒ 静默返回空 dict，绝不影响归档主流程。
    """
    out: Dict[str, str] = {}
    ids = [m for m in message_ids if m]
    if not ids:
        return out
    try:
        marks = ",".join("?" for _ in ids)
        for r in _rows(f"SELECT message_id, customer_code FROM release_item"
                       f" WHERE message_id IN ({marks})", tuple(ids)):
            if r.get("customer_code"):
                out[r["message_id"]] = r["customer_code"]
    except Exception:
        return {}
    return out


def _alert(detail: str) -> None:
    """告警走 OPS_OWNER_EMAIL。告警本身失败只记 ERROR，绝不外抛。"""
    try:
        get_notifier().send_program_error(OPS_OWNER_EMAIL, "", detail)
    except Exception as e:
        _log.error(f"archive alert failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 第一段：复制

def copy_after_forward(account: str, folder: str, uid: int, message_id: str,
                       target_folder: str) -> str:
    """转发成功后把一封邮件复制到目标夹（**不删源邮件**）。返回结果标记串。

    - 非 live ⇒ `skipped_dry_run`（不写库、不连 IMAP）
    - message_id 为空 ⇒ 写 `skipped`(no_message_id) 再返回（留痕，否则会反复尝试）
    - 已完成（copied/verified/deleted）⇒ `exists`（幂等）
    - 失败一律落 `failed_copy` + 告警，**异常不外抛**（邮件已经发出去了）
    """
    if not config.is_live():
        _log.info(f"Archive copy skipped (test mode) | folder={folder} uid={uid} "
                  f"target={target_folder}")
        return "skipped_dry_run"

    message_id = (message_id or "").strip()
    if not target_folder:
        return "no_target"
    if not message_id:
        _write_record(account, folder, uid, "", target_folder, status=ST_SKIPPED,
                      reason="no_message_id")
        increment_counter("archive_skipped_no_message_id")
        _log.warning(f"Archive skipped, empty Message-ID | account={account} "
                     f"folder={folder} uid={uid}")
        return "skipped_no_message_id"

    existing = _get_record(account, message_id, target_folder)
    if existing and existing["status"] in _DONE_STATES:
        return "exists"
    # failed_copy / failed_verify 继续往下走 —— 重放 = 用户显式介入的信号

    conn, err = _login(account)
    if conn is None:
        _write_record(account, folder, uid, message_id, target_folder,
                      status=ST_FAILED_COPY, reason="login_failed")
        _alert(f"归档复制失败（登录）｜账号 {account}｜夹 {folder}｜uid {uid}｜{err}")
        increment_counter("archive_copy_failed")
        return "failed_login"
    try:
        return _copy_on_conn(conn, account, folder, uid, message_id, target_folder)
    except Exception as e:
        _log.error(f"Archive copy exception (mail already sent) | msg={message_id[:50]} "
                   f"uid={uid}: {type(e).__name__}: {e}")
        _write_record(account, folder, uid, message_id, target_folder,
                      status=ST_FAILED_COPY, reason=f"{type(e).__name__}: {e}"[:200])
        _alert(f"归档复制异常｜账号 {account}｜夹 {folder}｜uid {uid}｜"
               f"{type(e).__name__}: {e}")
        increment_counter("archive_copy_failed")
        return "failed_exception"
    finally:
        _logout(conn)


def _copy_on_conn(conn, account: str, folder: str, uid: int, message_id: str,
                  target_folder: str) -> str:
    """在已登录的连接上完成一封的复制（第 4~9 步）。"""
    target_srv = _imap_folder(target_folder)
    typ = _select(conn, target_srv, readonly=False)
    if typ != "OK":
        # 第 4 步：目标夹不存在 ⇒ 白名单内才建，白名单外只告警
        if target_folder not in _AUTO_CREATE:
            _write_record(account, folder, uid, message_id, target_folder,
                          status=ST_FAILED_COPY, reason="target_missing")
            _alert(f"归档复制失败（目标夹不存在且不在白名单）｜账号 {account}｜"
                   f"目标夹「{target_folder}」｜msg={message_id[:60]}")
            increment_counter("archive_copy_failed")
            return "failed_target_missing"
        try:
            conn.create(target_srv)
            _log.info(f"Archive target folder created | account={account} folder={target_folder}")
        except Exception as e:
            _log.warning(f"Archive CREATE failed | target={target_folder}: "
                         f"{type(e).__name__}: {e}")
        typ = _select(conn, target_srv, readonly=False)
        if typ != "OK":
            _write_record(account, folder, uid, message_id, target_folder,
                          status=ST_FAILED_COPY, reason="target_missing")
            _alert(f"归档复制失败（建夹后仍 SELECT 不到）｜账号 {account}｜"
                   f"目标夹「{target_folder}」｜msg={message_id[:60]}")
            increment_counter("archive_copy_failed")
            return "failed_target_missing"

    # 第 5 步：COPY 之前先查一次 —— 否则"复制成功但记账失败"会在下一轮重放时
    # 再复制一份，客户收到两份放箱令。
    try:
        has_copy = _search_message_id(conn, message_id)
    except Exception as e:
        _log.warning(f"Archive pre-copy SEARCH failed, not copying | msg={message_id[:50]}: "
                     f"{type(e).__name__}: {e}")
        _write_record(account, folder, uid, message_id, target_folder,
                      status=ST_FAILED_COPY, reason="search_failed")
        increment_counter("archive_copy_failed")
        return "failed_search"

    src_srv = _imap_folder(folder)
    typ = _select(conn, src_srv, readonly=False)
    if typ != "OK":
        _write_record(account, folder, uid, message_id, target_folder,
                      status=ST_FAILED_COPY, reason="src_missing")
        _alert(f"归档复制失败（源夹 SELECT 不到）｜账号 {account}｜源夹「{folder}」｜"
               f"msg={message_id[:60]}")
        increment_counter("archive_copy_failed")
        return "failed_src_missing"

    # 🔴 两条路径共用的一关：UIDVALIDITY 拿不到就不许写 copied。
    # 取不到就存 0 ⇒ 删除时 current(非0)!=0 ⇒ 这条记录永远删不掉，而且没人知道为什么。
    validity = _require_uidvalidity(conn)
    if validity is None:
        _write_record(account, folder, uid, message_id, target_folder,
                      status=ST_FAILED_COPY, reason="no_uidvalidity")
        _alert(f"归档未记录（取不到 UIDVALIDITY）｜账号 {account}｜源夹「{folder}」｜"
               f"msg={message_id[:60]}｜该邮件不会被自动删除，请人工确认")
        increment_counter("archive_copy_failed")
        return "failed_no_uidvalidity"

    if has_copy:
        # recovered：上次复制成功、记账失败（或崩在记账之前）。只补记录，不再复制。
        # ⚠️ 这条路径也过了上面那道 UIDVALIDITY 关。
        _write_record(account, folder, uid, message_id, target_folder,
                      status=ST_COPIED, src_uidvalidity=validity)
        try:
            mark_seen(account, folder, uid)
        except Exception as e:
            _log.warning(f"mark_seen failed | msg={message_id[:50]}: {e}")
        _log.info(f"Archive recovered (copy already existed) | account={account} "
                  f"folder={folder} uid={uid} target={target_folder}")
        return "recovered"

    # 第 6 步：UID COPY（没有 MOVE 扩展，只能 COPY 一步）
    typ, data = conn.uid("COPY", str(int(uid or 0)), target_srv)
    if typ != "OK":
        _write_record(account, folder, uid, message_id, target_folder,
                      status=ST_FAILED_COPY, reason=f"copy_rejected: {typ}")
        _alert(f"归档复制失败（UID COPY 被拒）｜账号 {account}｜源夹「{folder}」｜"
               f"uid {uid}｜msg={message_id[:60]}")
        increment_counter("archive_copy_failed")
        return "failed_copy"

    _write_record(account, folder, uid, message_id, target_folder,
                  status=ST_COPIED, src_uidvalidity=validity,
                  copy_uid=_parse_copyuid(data))
    try:
        mark_seen(account, folder, uid)
    except Exception as e:
        _log.warning(f"mark_seen failed | msg={message_id[:50]}: {e}")
    _log.info(f"Archive copied | account={account} folder={folder} uid={uid} "
              f"target={target_folder} uidvalidity={validity}")
    increment_counter("archive_copied")
    return "copied"


def archive_copy_many(items: List[Dict[str, Any]], target_folder: str) -> int:
    """批量复制（专列用）。返回成功条数（含 recovered）。

    🔴 硬约束：**每个账号只 login 一次**。逐条调 `copy_after_forward` 会变成
    "55 票 = 55 次新建连接 + 登录"（55–110 秒，阿里企业邮还可能限流），
    这和任务 11「不许循环调 fetch_raw_by_uid」是同一条红线。

    `items` 必须是**同一个账号**的一组（调用方 `_group_by_account` 已分好）。
    单条失败只记 `failed_copy` 继续下一条 —— 归档不是主流程，不能因为一封失败
    就不管其余 54 封。
    """
    if not config.is_live():
        _log.info(f"Archive batch skipped (test mode) | items={len(items or [])}")
        return 0
    items = [it for it in (items or []) if it]
    if not items or not target_folder:
        return 0

    account = str(items[0].get("account") or "")
    if any(str(it.get("account") or "") != account for it in items):
        # 混账号 ⇒ 一次登录处理不了；宁可炸给调用方看，也不静默用错账号登录
        raise ValueError("archive_copy_many expects items of a single account")

    password = get_accounts().get(account, "")
    if not password:
        raise RuntimeError(f"no IMAP credentials for account={account}")

    conn = _new_conn()
    done = 0
    try:
        conn.login(account, password)          # ← 每账号唯一一次登录
        target_srv = _imap_folder(target_folder)
        typ = _select(conn, target_srv, readonly=False)
        if typ != "OK" and target_folder in _AUTO_CREATE:
            try:
                conn.create(target_srv)
            except Exception as e:
                _log.warning(f"Archive CREATE failed | target={target_folder}: "
                             f"{type(e).__name__}: {e}")
            typ = _select(conn, target_srv, readonly=False)
        if typ != "OK":
            for it in items:
                _write_record(account, it.get("folder") or "", it.get("uid") or 0,
                              (it.get("message_id") or "").strip(), target_folder,
                              status=ST_FAILED_COPY, reason="target_missing")
            _alert(f"归档批量失败（目标夹不可用）｜账号 {account}｜目标夹「{target_folder}」"
                   f"｜{len(items)} 条未归档")
            increment_counter("archive_copy_failed")
            return 0

        # ① 逐条 SEARCH 判副本（IMAP 一次只能搜一个 Message-ID，没有批量语法；
        #    同一连接内是毫秒级）。搜失败 ⇒ 这一条判 failed_copy，绝不盲目 COPY
        #    （多一份放箱令比多一次 SEARCH 严重）。
        has_copy = set()
        for it in items:
            mid = (it.get("message_id") or "").strip()
            if not mid:
                continue
            try:
                if _search_message_id(conn, mid):
                    has_copy.add(mid)
            except Exception as e:
                _log.warning(f"Archive batch SEARCH failed | msg={mid[:50]}: "
                             f"{type(e).__name__}: {e}")
                _write_record(account, it.get("folder") or "", it.get("uid") or 0, mid,
                              target_folder, status=ST_FAILED_COPY, reason="search_failed")
                has_copy.add(mid)   # 当"已有副本"处理 ⇒ 本轮不复制

        by_folder: Dict[str, List[Dict[str, Any]]] = {}
        for it in items:
            by_folder.setdefault(it.get("folder") or "", []).append(it)

        for folder, fitems in by_folder.items():
            if _select(conn, _imap_folder(folder), readonly=False) != "OK":
                for it in fitems:
                    _write_record(account, folder, it.get("uid") or 0,
                                  (it.get("message_id") or "").strip(), target_folder,
                                  status=ST_FAILED_COPY, reason="src_missing")
                _alert(f"归档批量失败（源夹 SELECT 不到）｜账号 {account}｜源夹「{folder}」"
                       f"｜{len(fitems)} 条未归档")
                increment_counter("archive_copy_failed")
                continue

            # ② UIDVALIDITY 每组（同账号同源夹）只取一次
            validity = _require_uidvalidity(conn)
            for it in fitems:
                mid = (it.get("message_id") or "").strip()
                uid = int(it.get("uid") or 0)
                if not mid:
                    _write_record(account, folder, uid, "", target_folder,
                                  status=ST_SKIPPED, reason="no_message_id")
                    increment_counter("archive_skipped_no_message_id")
                    continue
                existing = _get_record(account, mid, target_folder)
                if existing and existing["status"] in _DONE_STATES:
                    continue
                if validity is None:
                    _write_record(account, folder, uid, mid, target_folder,
                                  status=ST_FAILED_COPY, reason="no_uidvalidity")
                    increment_counter("archive_copy_failed")
                    continue
                if mid in has_copy:
                    # recovered：目标夹已有副本，只补记录（同样带真实 UIDVALIDITY）
                    _write_record(account, folder, uid, mid, target_folder,
                                  status=ST_COPIED, src_uidvalidity=validity)
                    done += 1
                    try:
                        mark_seen(account, folder, uid)
                    except Exception as e:
                        _log.warning(f"mark_seen failed | msg={mid[:50]}: {e}")
                    continue
                try:
                    typ, data = conn.uid("COPY", str(uid), target_srv)
                    if typ != "OK":
                        raise RuntimeError(f"UID COPY rejected: typ={typ}")
                except Exception as e:
                    _log.warning(f"Archive batch COPY failed | msg={mid[:50]} uid={uid}: "
                                 f"{type(e).__name__}: {e}")
                    _write_record(account, folder, uid, mid, target_folder,
                                  status=ST_FAILED_COPY, reason=f"copy_failed: {e}"[:200])
                    increment_counter("archive_copy_failed")
                    continue
                _write_record(account, folder, uid, mid, target_folder,
                              status=ST_COPIED, src_uidvalidity=validity,
                              copy_uid=_parse_copyuid(data))
                done += 1
                try:
                    mark_seen(account, folder, uid)
                except Exception as e:
                    _log.warning(f"mark_seen failed | msg={mid[:50]}: {e}")
            if validity is None:
                _alert(f"归档批量未记录（取不到 UIDVALIDITY）｜账号 {account}｜"
                       f"源夹「{folder}」｜{len(fitems)} 条｜这些邮件不会被自动删除")
    finally:
        _logout(conn)
    return done


# ---------------------------------------------------------------- 第二段：每日核对删除

def _load_due(limit: int) -> List[Dict[str, Any]]:
    return _rows(
        "SELECT * FROM archive_log"
        " WHERE status IN ('copied','verified')"
        "   AND copied_at IS NOT NULL"
        f"   AND copied_at <= datetime('now', {_TIME_GATE})"
        " ORDER BY copied_at ASC LIMIT ?",
        (int(limit),),
    )


def _gb(rows: List[Dict[str, Any]], keys: Tuple[str, ...]) -> Dict[Tuple, List[Dict[str, Any]]]:
    out: Dict[Tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(tuple(r.get(k) or "" for k in keys), []).append(r)
    return out


def purge_verified(limit: Optional[int] = None) -> Dict[str, int]:
    """每天 12:00 的核对删除。返回统计 dict。

    ⚠️ 函数名沿用现有规划（`purge_verified`），但它处理的是 `copied` + `verified`
    **两种**状态 —— 取数条件含 `verified` 是必须的：第 3a 步写 verified、第 3d 步
    才删，崩在两步之间（进程被杀 / 断电 / 部署更新）的记录若只查 copied，
    会被永远漏掉，**而且不告警**。
    """
    stats = {"scanned": 0, "verified": 0, "deleted": 0,
             "failed": 0, "recovered_deleted": 0, "terminated": 0}
    if not config.is_live():
        _log.info("Archive purge skipped (test mode): no COPY, no EXPUNGE, no DB write")
        return stats

    rows = _load_due(limit if limit is not None else PURGE_LIMIT)
    stats["scanned"] = len(rows)
    if not rows:
        _log.info("Archive purge: nothing due")
        return stats

    by_account = _gb(rows, ("account",))
    for (account,), arows in by_account.items():
        conn, err = _login(account)
        if conn is None:
            # 登录失败是暂时性的（网络 / 限流），不是"外部事实" ⇒ 保持原状态，明天再试
            _log.error(f"Archive purge login failed, group kept unchanged | "
                       f"account={account} rows={len(arows)} | {err}")
            increment_counter("archive_purge_login_failed")
            continue
        try:
            _purge_account(conn, account, arows, stats)
        except Exception as e:
            _log.error(f"Archive purge group error, rows kept unchanged | account={account} | "
                       f"{type(e).__name__}: {e}")
            increment_counter("archive_purge_error")
        finally:
            _logout(conn)

    _notify_summary(stats)
    return stats


def _purge_account(conn, account: str, rows: List[Dict[str, Any]],
                   stats: Dict[str, int]) -> None:
    """大组级（target_folder）核副本，再小组级（account, src_folder）比对+删除。"""
    # 本轮没能确认副本是否存在的行（SEARCH 异常）：本轮**绝不删**，也不落终态 ——
    # 这是暂时性错误，明天再来；落 failed_verify 会让一封本来就好的邮件永久留在收信夹。
    blocked: set = set()
    # ---- 3a 大组级：同一目标夹只 SELECT 一次 ----
    for (target,), trows in _gb(rows, ("target_folder",)).items():
        if _select(conn, _imap_folder(target), readonly=False) != "OK":
            _mark_rows(trows, ST_FAILED_VERIFY, "target_missing")
            stats["failed"] += len(trows)
            stats["terminated"] += len(trows)
            _alert(f"归档核对终止（目标夹不存在）｜账号 {account}｜目标夹「{target}」｜"
                   f"{len(trows)} 条｜这些邮件不会自动删除，请确认副本是否还在")
            continue

        misses: List[Dict[str, Any]] = []
        for r in trows:
            try:
                hit = _search_message_id(conn, r["message_id"])
            except Exception as e:
                _log.warning(f"Archive verify SEARCH failed, deferred to next run | "
                             f"id={r['id']} msg={str(r['message_id'])[:50]}: "
                             f"{type(e).__name__}: {e}")
                increment_counter("archive_verify_search_failed")
                blocked.add(r["id"])
                continue
            if hit:
                _set_row_status(r["id"], ST_VERIFIED, verified_at=True)
                r["status"] = ST_VERIFIED
                stats["verified"] += 1
            else:
                _set_row_status(r["id"], ST_FAILED_VERIFY, "copy_not_found")
                r["status"] = ST_FAILED_VERIFY
                stats["failed"] += 1
                stats["terminated"] += 1
                misses.append(r)

        if misses:
            codes = _customer_codes([str(r["message_id"]) for r in misses])
            lines = "\n".join(
                f"  msg={str(r['message_id'])[:60]} 客编={codes.get(str(r['message_id']), '-')}"
                f" 源夹={r['src_folder']} uid={r['src_uid']}"
                for r in misses[:20]
            )
            _alert(f"归档核对失败（目标夹里搜不到副本）｜账号 {account}｜目标夹「{target}」｜"
                   f"{len(misses)} 条｜这些邮件不会自动删除\n{lines}")

    # ---- 3.1 小组级：UIDVALIDITY 是**源夹**属性，必须按 (account, src_folder) 比 ----
    for (acct, src), srows in _gb(rows, ("account", "src_folder")).items():
        _purge_source_group(conn, acct, src, srows, stats, blocked)


def _purge_source_group(conn, account: str, src_folder: str,
                        rows: List[Dict[str, Any]], stats: Dict[str, int],
                        blocked: Optional[set] = None) -> None:
    if _select(conn, _imap_folder(src_folder), readonly=False) != "OK":
        _mark_rows(rows, ST_FAILED_VERIFY, "src_missing")
        stats["failed"] += len(rows)
        stats["terminated"] += len(rows)
        _alert(f"归档核对终止（源夹 SELECT 不到）｜账号 {account}｜源夹「{src_folder}」｜"
               f"{len(rows)} 条｜这些邮件不会自动删除")
        return

    blocked = blocked or set()
    alive = [r for r in rows
             if r.get("status") in (ST_COPIED, ST_VERIFIED) and r["id"] not in blocked]
    if not alive:
        return

    cur_validity = _require_uidvalidity(conn)
    if cur_validity is None:
        # 比不了就不许删；且必须落终态 —— 否则每天重捞、每天告警、永远出不去
        _mark_rows(alive, ST_FAILED_VERIFY, "no_uidvalidity")
        stats["failed"] += len(alive)
        stats["terminated"] += len(alive)
        _alert(f"归档核对终止（取不到 UIDVALIDITY）｜账号 {account}｜源夹「{src_folder}」｜"
               f"{len(alive)} 条｜这些邮件不会自动删除，请人工确认副本在不在")
        return

    bad = [r for r in alive if int(r.get("src_uidvalidity") or 0) != cur_validity]
    if bad:
        # 🔴 落到终态，不是"保持原状态"：源夹重建后 UIDVALIDITY 永久对不上，
        # 没有任何自动判据能判"到底删没删过"，继续重试就是死循环。
        # 同一 (account, src_folder) 下可能挂多个 target_folder，UPDATE 逐个带
        # target_folder 过滤，避免误伤别的目标夹的记录。
        for target in {r.get("target_folder") or "" for r in alive}:
            _mark_group_uidvalidity(account, src_folder, target)
        for r in alive:
            r["status"] = ST_FAILED_VERIFY
            r["fail_reason"] = "uidvalidity_changed"
        stats["failed"] += len(alive)
        stats["terminated"] += len(alive)
        _alert(
            f"归档核对终止（UIDVALIDITY 已变化，源夹被重建过）｜账号 {account}｜"
            f"源夹「{src_folder}」｜{len(alive)} 条｜记录里 {bad[0].get('src_uidvalidity')}"
            f" / 当前 {cur_validity}｜**这些邮件不会自动删除**，请到目标夹确认副本都在后"
            f"人工收尾（本组已标 failed_verify，purge 不再重试）"
        )
        return

    # ---- c. 第③闸：删前逐封重新 FETCH，按三路分流 ----
    pending_uids: List[int] = []
    pending_rows: List[Dict[str, Any]] = []
    for r in alive:
        uid = int(r.get("src_uid") or 0)
        try:
            mid = _fetch_message_id(conn, uid)
        except Exception as e:
            # 判据不足 ⇒ 绝不删；保持 verified，明天再来（这是暂时性错误，不是外部事实）
            _log.warning(f"Archive verify FETCH failed, kept as verified | id={r['id']} "
                         f"uid={uid}: {type(e).__name__}: {e}")
            increment_counter("archive_verify_fetch_failed")
            continue

        if mid is None:
            # 源邮件不在了 —— 只可能是上次删成功、没来得及记账 ⇒ 补记 deleted，不告警
            _set_row_status(r["id"], ST_DELETED, deleted_at=True)
            r["status"] = ST_DELETED
            stats["recovered_deleted"] += 1
            _log.info(f"Archive source already gone, bookkeeping only | id={r['id']} "
                      f"uid={uid} msg={str(r['message_id'])[:50]}")
            continue

        if _norm_mid(mid) != _norm_mid(r["message_id"]):
            # UID 被别的信复用了 —— 危险信号，绝不删
            _mark_rows([r], ST_FAILED_VERIFY, "message_id_mismatch")
            stats["failed"] += 1
            stats["terminated"] += 1
            _alert(f"归档核对失败（UID 已被别的邮件复用，拒绝删除）｜账号 {account}｜"
                   f"源夹「{src_folder}」｜uid={uid}｜"
                   f"记录 {str(r['message_id'])[:60]} / 实际 {mid[:60]}")
            continue

        pending_uids.append(uid)
        pending_rows.append(r)

    if not pending_uids:
        return

    uids = ",".join(str(u) for u in sorted(set(pending_uids)))

    # ---- d. 先 STORE 再精确清理 ----
    try:
        typ, _ = conn.uid("STORE", uids, "+FLAGS", "(\\Deleted)")
        if typ != "OK":
            raise RuntimeError(f"UID STORE \\Deleted rejected: typ={typ}")
    except Exception as e:
        _log.warning(f"Archive STORE \\Deleted failed, kept as verified | "
                     f"folder={src_folder} uids={uids}: {type(e).__name__}: {e}")
        increment_counter("archive_store_deleted_failed")
        return

    try:
        # 🔴 只能 UID EXPUNGE <这些 uid>：带 uid 才精确，否则会清掉整个收信夹
        typ, _ = conn.uid("EXPUNGE", uids)
        if typ != "OK":
            raise RuntimeError(f"UID EXPUNGE rejected: typ={typ}")
    except Exception as e:
        # 邮件已带 \Deleted 标记但没消失 ⇒ 保持 verified 下次重试。
        # 绝不标 deleted：那会让 purge 不再碰它、人工也看不出问题。
        _log.error(f"Archive UID EXPUNGE failed, rows kept verified | folder={src_folder} "
                   f"uids={uids}: {type(e).__name__}: {e}")
        increment_counter("archive_expunge_failed")
        return

    for r in pending_rows:
        _set_row_status(r["id"], ST_DELETED, deleted_at=True)
        r["status"] = ST_DELETED
        _log.info(f"Archive source deleted | account={account} folder={src_folder} "
                  f"uid={r['src_uid']} target={r['target_folder']} "
                  f"msg={str(r['message_id'])[:50]}")
    stats["deleted"] += len(pending_rows)
    increment_counter("archive_deleted", len(pending_rows))


def _notify_summary(stats: Dict[str, int]) -> None:
    """汇总（核过 N / 已删 N / 核对失败 M / 补记已删 K / 终止 T）。

    任务 16：**不再发企微 `archive_summary`** —— 改为落地成当日快照，
    由 12:30 的每日邮件第四节列出（见 `daily_report.write_archive_stats`）。
    归档失败告警 `_alert()` 是**异常**，仍然走企微、本函数不碰。
    """
    try:
        from . import daily_report
        daily_report.write_archive_stats(stats)
    except Exception as e:
        _log.error(f"archive summary snapshot failed: {type(e).__name__}: {e}")
