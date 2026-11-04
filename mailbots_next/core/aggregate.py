# -*- coding: utf-8 -*-
"""放箱令 / 取消放箱令 / 还箱令 —— 聚合与合并发送（任务 11 / spec 03）。

一句话：专列的 N 封（同公司 + 同班列号）攒成一批，合成 **1 封**发出 ——
正文是全部票的 HTML 表格，附件是打包好的 zip。

三条不能颠倒的顺序（`02_TASK.md` §4）：

1. `flush_batch` 第一步把 batch 从 `pending` 抢成 `flushing`；
   `pool_add` 找批次只认 `pending`（看到 `flushing` 当不存在、另建）。
   否则 flush 那几秒里进来的新票会被标成 `sent` 的批次吞掉，静默丢票。
2. `send_smtp` → 写 `forward_log` → 标 `sent`。**写库必须早于标 sent** ——
   启动恢复的判据就是「`forward_log` 里有 `BATCH::<batch_id>` ⇒ 邮件已发出」。
   顺序反了，崩在两步之间的批次会被判成"没发" ⇒ **重发**（客户收到两封一样的放箱令）。
3. 第 6.5 步（写 `forward_log`）的异常必须**吞掉**：邮件已经发出去了，
   让异常冒泡会把 batch 卡在 `flushing`，启动恢复同样判"没发" ⇒ 重发。

附件不落库（`extractors/release.py::build_attachments` 的 docstring 写死
「payload 只在内存里传给下游打包」），所以 flush 时必须按 uid 重新拉：
**一次登录 + 批量 UID FETCH**（不是循环调单封重取接口：它每次新建 IMAP 连接 +
登录，55 票会把连接数打满）。任何一票取不到 ⇒ 整批 `failed`，
绝不做「53/55 发出去」——客户会以为只有 53 箱。
"""
import email
import email.policy
import html
import imaplib
import io
import os
import re
import time
import zipfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..config import IMAP_PORT, IMAP_SERVER, OPS_OWNER_EMAIL, EmailType, settings
from ..config.secrets import get_accounts, get_sender_by_email
from .act import _record_forward, build_forward_message, make_forward_id, send_smtp
from .dedup import _db_lock
from .dedup import get_connection as get_dedup_connection
from .extract import parse_email
from .extractors.release import RE_VALID, build_attachments
from .ingest import _server_folder, mark_seen
from .log import get_logger
from .notify import get_notifier, increment_counter
from .store import (
    find_forward_log_by_message_id,
    get_recipients,
    get_responsible_person,
)

_log = get_logger(__name__)

# 聚合器扫描间隔（秒）。精度要求高于 sweeper 的 300s：静默闸 120s，
# 10 秒粒度意味着最长延迟 130 秒而不是 420 秒。
AGGREGATOR_INTERVAL_SEC = 10

_TS_FMT = "%Y-%m-%d %H:%M:%S"

ST_PENDING = "pending"
ST_FLUSHING = "flushing"
ST_SENT = "sent"
ST_FAILED = "failed"
ST_ABORTED = "aborted"

_RETBOX = EmailType.RETBOX.value
_RELEASE_CANCEL = EmailType.RELEASE_CANCEL.value

# 表结构与 spec 03 §2.1 一致。🔴 UNIQUE 必须带 box_no：
# 还箱令的 code_no 恒为空串（retbox.py 写死 None），唯一键只有
# (batch_id, customer_code, code_no) 时，同一客编的第二个箱号会被
# INSERT OR IGNORE 直接吞掉 ⇒ 一家公司一票多箱时静默少发。
# 放箱令的 box_no 是空串，加进唯一键不影响原有去重（同客编同数字编码仍是重复）。
_DDL = """
CREATE TABLE IF NOT EXISTS release_batch (
    batch_id      TEXT PRIMARY KEY,
    company       TEXT NOT NULL,
    train_no      TEXT NOT NULL,
    email_type    TEXT NOT NULL,
    item_count    INTEGER DEFAULT 0,
    first_item_at TEXT,
    last_item_at  TEXT,
    status        TEXT DEFAULT 'pending',
    forward_id    TEXT DEFAULT '',
    attachment_bytes INTEGER DEFAULT 0,
    parts         INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now')),
    sent_at       TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS release_item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    account TEXT, folder TEXT, uid INTEGER,
    customer_code TEXT, release_no TEXT, code_no TEXT,
    box_no        TEXT, note TEXT,
    validity      TEXT DEFAULT '',
    link          TEXT DEFAULT '',
    retbox_doc    TEXT DEFAULT '',
    status        TEXT DEFAULT 'pooled',
    created_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(batch_id, customer_code, code_no, box_no)
);
CREATE INDEX IF NOT EXISTS idx_ri_batch ON release_item(batch_id, status);
"""

_TH_STYLE = ("border: 1px solid #999; padding: 4px 10px; "
             "text-align: left; white-space: nowrap;")
_TD_STYLE = "border: 1px solid #999; padding: 4px 10px; white-space: nowrap;"


# ---------------------------------------------------------------- 基础工具

def _now() -> datetime:
    return datetime.now()


def _ts(dt: Optional[datetime] = None) -> str:
    """统一时间戳格式（本地时间，定长 ⇒ 字符串比较等价于时间比较）。

    ⚠️ 不用 SQLite 的 `datetime('now')` 参与窗口比较：那是 **UTC**，
    而这里写的是本地时间（UTC+8），混用会让 `last_item_at <= now-120s`
    恒不成立 ⇒ 聚合器永远扫不到到期批次。
    """
    return (dt or datetime.now()).strftime(_TS_FMT)


def _as_dict(row) -> Dict[str, Any]:
    return dict(row)


def _rows(sql: str, params: Tuple = (), fetch: bool = True):
    with _db_lock:
        conn = get_dedup_connection()
        try:
            cur = conn.execute(sql, params)
            if fetch:
                return [_as_dict(r) for r in cur.fetchall()]
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def init_release_tables() -> None:
    """建两张表（启动期执行，**不可抛异常** —— 照 dedup.py::init_db 的写法）。

    落 `dedup.db`（与 error_queue 同库），不是 bot_config.db；
    所以用 dedup 的 `get_connection`（别名 get_dedup_connection）+ 它的 `_db_lock`。
    照抄 store.py::init_forward_log() 的话表会建到错库。
    """
    try:
        with _db_lock:
            conn = get_dedup_connection()
            try:
                conn.executescript(_DDL)
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        _log.error(f"init_release_tables failed (non-fatal): {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 入池

def _next_batch_suffix(conn, base_id: str) -> str:
    """同 id 已存在（任何非 pending 状态）时加 -2 / -3 后缀，保证新票不被吞。"""
    n = 2
    bid = f"{base_id}-{n}"
    while conn.execute("SELECT 1 FROM release_batch WHERE batch_id=?", (bid,)).fetchone():
        n += 1
        bid = f"{base_id}-{n}"
    return bid


def _new_batch_id(conn, company: str, train_no: str, email_type: str) -> str:
    """新建 batch_id：`公司|班列号|类型|YYYYMMDD`（四段，类型不能缺）。

    🔴 日期只在这里用一次（可读标识：这批是哪天开的），**不参与查找**。
    取消件与放箱令走的是同一条 records 记录（同一客编 → 同一班列号），
    少了 email_type 会让取消件插进放箱令批次 ⇒ 正文按放箱令渲染、标题没有【取消】。
    """
    batch_id = f"{company}|{train_no}|{email_type}|{datetime.now():%Y%m%d}"
    if conn.execute("SELECT 1 FROM release_batch WHERE batch_id=?", (batch_id,)).fetchone():
        batch_id = _next_batch_suffix(conn, batch_id)
    return batch_id


def pool_add(company: str, train_no: str, email_type: str, message_id: str,
             account: str, folder: str, uid: int,
             customer_code: str, release_no: str, code_no: str, box_no: str,
             note: str, validity: str = "", link: str = "",
             retbox_doc: str = "") -> Tuple[bool, bool]:
    """把一票放进聚合池。返回 `(added, is_dup)`。

    added  = 本次是否真的新增了一行（False 也可能是"这票已在池里"，见 is_dup）
    is_dup = 命中唯一键（同 batch + 客编 + 数字编码 + 箱号），这票已在池里

    ⚠️ `customer_code` / `code_no` / `box_no` 一律传**空串不是 None**：
    SQLite 认为 NULL != NULL，NULL 会让唯一约束完全失效。
    """
    company = (company or "").strip()
    train_no = (train_no or "").strip()
    email_type = str(getattr(email_type, "value", email_type) or "").strip()
    if not email_type:
        _log.warning(f"pool_add skipped, empty email_type | msg={str(message_id)[:50]}")
        return (False, False)
    if not company:
        # 不丢弃：照常入池，flush 时 get_recipients('') 返空 ⇒ failed + E2 告警，
        # 问题会显式暴露给运维，而不是这封信静默消失。
        _log.warning(f"pool_add got empty company | msg={str(message_id)[:50]}")

    now = _ts()
    with _db_lock:
        conn = get_dedup_connection()
        try:
            # 找现有待处理批次 —— 只按 company + train_no + email_type + status。
            # 🔴 不带日期条件：23:59 建了 ...|20261103 的批，00:01 按 ...|20261104
            # 查不到就会又建一批，同一晚的放箱令被拆成两封发给客户。
            row = conn.execute(
                "SELECT batch_id FROM release_batch"
                " WHERE company=? AND train_no=? AND email_type=? AND status='pending'"
                " ORDER BY created_at DESC LIMIT 1",
                (company, train_no, email_type),
            ).fetchone()
            if row is None:
                # flushing 的批次在上面这条 SQL 里天然查不到 ⇒ 当不存在、另建，
                # 这样 flush 期间进来的票不会进那个即将被标 sent 的批次。
                batch_id = _new_batch_id(conn, company, train_no, email_type)
                conn.execute(
                    "INSERT INTO release_batch (batch_id, company, train_no, email_type,"
                    " item_count, first_item_at, last_item_at, status)"
                    " VALUES (?,?,?,?,0,?,?,'pending')",
                    (batch_id, company, train_no, email_type, now, now),
                )
            else:
                batch_id = row["batch_id"]

            cur = conn.execute(
                "INSERT OR IGNORE INTO release_item"
                " (batch_id, message_id, account, folder, uid, customer_code,"
                "  release_no, code_no, box_no, note, validity, link, retbox_doc,"
                "  status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pooled',?)",
                (batch_id, message_id or "", account or "", folder or "", int(uid or 0),
                 customer_code or "", release_no or "", code_no or "", box_no or "",
                 note or "", validity or "", link or "", retbox_doc or "", now),
            )
            added = cur.rowcount > 0
            if added:
                conn.execute(
                    "UPDATE release_batch SET item_count = item_count + 1, last_item_at=?"
                    " WHERE batch_id=?",
                    (now, batch_id),
                )
            conn.commit()
        finally:
            conn.close()

    if added:
        _log.info(f"release pooled | batch={batch_id} | msg={str(message_id)[:50]}")
        return (True, False)
    _log.info(f"release pool duplicate ignored | batch={batch_id} | msg={str(message_id)[:50]}")
    return (False, True)


# ---------------------------------------------------------------- 三闸

def list_due_batches(quiet_sec: Optional[int] = None,
                     max_items: Optional[int] = None,
                     max_wait_sec: Optional[int] = None) -> List[Dict[str, Any]]:
    """到期批次（三闸，先到先发）。只查 `pending` —— `flushing` 天然不会被重复选中。"""
    quiet = int(settings.RELEASE_QUIET_SEC if quiet_sec is None else quiet_sec)
    maxi = int(settings.RELEASE_MAX_ITEMS if max_items is None else max_items)
    maxw = int(settings.RELEASE_MAX_WAIT_SEC if max_wait_sec is None else max_wait_sec)
    now = _now()
    quiet_cut = _ts(now - timedelta(seconds=quiet))
    wait_cut = _ts(now - timedelta(seconds=maxw))
    rows = _rows(
        "SELECT * FROM release_batch WHERE status='pending' AND ("
        "  COALESCE(last_item_at, '') <= ?"
        "  OR COALESCE(item_count, 0) >= ?"
        "  OR COALESCE(first_item_at, '') <= ?"
        ") ORDER BY first_item_at ASC",
        (quiet_cut, maxi, wait_cut),
    )
    return rows


def _quiet_elapsed(batch: Dict[str, Any]) -> bool:
    last = (batch.get("last_item_at") or "").strip()
    if not last:
        return True
    cut = _ts(_now() - timedelta(seconds=int(settings.RELEASE_QUIET_SEC)))
    return last <= cut


# ---------------------------------------------------------------- 状态流转

def _claim_batch(batch_id: str) -> bool:
    """第 0 步：单条 UPDATE 把 pending 抢成 flushing。rowcount==0 ⇒ 已被别处抢走。"""
    n = _rows(
        "UPDATE release_batch SET status='flushing' WHERE batch_id=? AND status='pending'",
        (batch_id,), fetch=False,
    )
    return bool(n)


def _set_batch_status(batch_id: str, status: str) -> None:
    _rows("UPDATE release_batch SET status=? WHERE batch_id=?", (status, batch_id), fetch=False)


def _mark_batch_sent(batch_id: str, forward_id: str, item_count: int,
                     attachment_bytes: int = 0, parts: int = 1) -> None:
    _rows(
        "UPDATE release_batch SET status='sent', sent_at=?, forward_id=?,"
        " item_count=?, attachment_bytes=?, parts=? WHERE batch_id=?",
        (_ts(), forward_id or "", int(item_count or 0), int(attachment_bytes or 0),
         int(parts or 1), batch_id),
        fetch=False,
    )


def _mark_items_sent(batch_id: str) -> None:
    _rows("UPDATE release_item SET status='sent' WHERE batch_id=? AND status='pooled'",
          (batch_id,), fetch=False)


def _load_pooled_items(batch_id: str) -> List[Dict[str, Any]]:
    return _rows(
        "SELECT * FROM release_item WHERE batch_id=? AND status='pooled' ORDER BY id",
        (batch_id,),
    )


def _alert(detail: str, kind: str = "program") -> None:
    """告警走 OPS_OWNER_EMAIL。kind=config ⇒ E2 配置缺失语义，否则程序错误 E1。"""
    try:
        notifier = get_notifier()
        if kind == "config":
            notifier.send_config_missing(OPS_OWNER_EMAIL, "", detail)
        else:
            notifier.send_program_error(OPS_OWNER_EMAIL, "", detail)
    except Exception as e:
        _log.error(f"release batch alert failed: {type(e).__name__}: {e}")


def _fail_batch(batch_id: str, detail: str, kind: str = "program") -> None:
    """整批 failed（终态，留人工处理）。绝不在这里发半批。"""
    _log.error(detail)
    _set_batch_status(batch_id, ST_FAILED)
    increment_counter("release_batch_failed")
    _alert(detail, kind=kind)


# ---------------------------------------------------------------- 收件人与发件账号

def _resolve_sender(responsible: Optional[str],
                    items: List[Dict[str, Any]]) -> Tuple[str, str]:
    """负责人有值 → 取他的发信账号；取不到退回该批第一票的收信账号。

    ⚠️ 聚合器线程里没有 routing 对象（那是单封处理时的上下文，早就没了），
    所以账号与收件人都必须在 flush 时现查。
    """
    if responsible:
        info = get_sender_by_email(responsible)
        if info and info[1]:
            return info
    account = (items[0].get("account") or "") if items else ""
    return account, get_accounts().get(account, "")


# ---------------------------------------------------------------- 取附件（批量 UID FETCH）

def _norm_mid(value: str) -> str:
    return (value or "").strip().strip("<>").strip().casefold()


def _same_message_id(fetched: str, expected: str) -> bool:
    """校验取回来的邮件头 Message-ID 与池里记的一致（防 uid 漂移）。

    池里的 message_id 是收信时从同一封邮件头取的；只有原邮件**没有**
    Message-ID 时 Poller 会合成 `SYN::…`，这种情况下没有可比对的头，放行。
    """
    expected = (expected or "").strip()
    if not expected:
        return False
    if expected.startswith("SYN::"):
        return True
    return _norm_mid(fetched) == _norm_mid(expected)


def _parse_uid_fetch(data, uids: List[int]) -> Dict[int, bytes]:
    """把 `UID FETCH 1,2,3 (UID BODY.PEEK[])` 的响应拆成 {uid: raw_bytes}。"""
    out: Dict[int, bytes] = {}
    unparsed: List[bytes] = []
    for part in data or []:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            head = part[0].decode("ascii", "replace") if isinstance(part[0], (bytes, bytearray)) else str(part[0] or "")
            m = re.search(r"UID\s+(\d+)", head, re.I)
            if m:
                out[int(m.group(1))] = bytes(part[1])
            else:
                unparsed.append(bytes(part[1]))
    if not out and unparsed and len(unparsed) == len(uids):
        # 少数服务器不在前缀里回 UID：按请求顺序对齐（Message-ID 仍逐票校验）
        out = {int(u): raw for u, raw in zip(uids, unparsed)}
    return out


def _fetch_item_attachments(items: List[Dict[str, Any]],
                            batch: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    """按 uid 重新取原文并解析附件，返回 `{item_id: [attachment, ...]}`。

    ⚠️ **一次登录、批量 UID FETCH** —— 一个批次里的票按收件账号分组，
    每个账号登录一次，用逗号 UID 列表一次拿多封。不能循环调
    单封重取接口（每次都新建 IMAP 连接 + 登录，55 票会把连接数打满）。
    任何一票失败 ⇒ 抛异常，调用方整批 failed。
    还箱令（retbox）无附件：不连 IMAP，直接返回空表。
    """
    if str(batch.get("email_type") or "") == _RETBOX:
        return {int(it["id"]): [] for it in items}

    accounts = get_accounts()
    out: Dict[int, List[Dict[str, Any]]] = {}
    by_account: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        by_account.setdefault(it.get("account") or "", []).append(it)

    for account, group in by_account.items():
        password = accounts.get(account, "")
        if not password:
            raise RuntimeError(f"no IMAP credentials for account={account} (items={len(group)})")
        by_folder: Dict[str, List[Dict[str, Any]]] = {}
        for it in group:
            by_folder.setdefault(it.get("folder") or "", []).append(it)

        conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=60)
        try:
            conn.login(account, password)
            for folder, fitems in by_folder.items():
                typ, _ = conn.select(_server_folder(folder), readonly=True)
                if typ != "OK":
                    raise RuntimeError(f"cannot select folder={folder} account={account}")
                uids = [int(it.get("uid") or 0) for it in fitems]
                if any(u <= 0 for u in uids):
                    raise RuntimeError(f"item without uid | account={account} folder={folder}")
                typ, data = conn.uid("FETCH", ",".join(str(u) for u in uids), "(UID BODY.PEEK[])")
                if typ != "OK":
                    raise RuntimeError(
                        f"UID FETCH rejected | account={account} folder={folder} uids={len(uids)}")
                by_uid = _parse_uid_fetch(data, uids)
                for it in fitems:
                    uid = int(it["uid"])
                    raw = by_uid.get(uid)
                    if not raw:
                        raise RuntimeError(
                            f"UID FETCH empty | account={account} folder={folder} uid={uid}")
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    got = (msg.get("Message-ID") or "").strip()
                    if not _same_message_id(got, it.get("message_id") or ""):
                        raise RuntimeError(
                            f"Message-ID mismatch (uid drifted?) | account={account} "
                            f"folder={folder} uid={uid} expected={it.get('message_id')} got={got}")
                    out[int(it["id"])] = build_attachments(parse_email(msg))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- 正文

def _clean_note(note: str) -> str:
    """渲染前剔掉 note 末尾的「有效期…」。

    实测 `tail_note` 是「数字编码之后的全部正文」，末尾就带着有效期那句，
    和独立的 validity 字段一字不差；两个都渲染，客户会看到**两次有效期**。
    ⚠️ 只改渲染，不改入库：release_item.note 里保留原文（将来要追溯）。
    """
    return RE_VALID.sub("", note or "").strip(" ：:，,。")


def build_subject(batch: Dict[str, Any], n_items: int) -> str:
    train = batch.get("train_no") or ""
    et = str(batch.get("email_type") or "")
    if et == _RETBOX:
        return f"YXO-{train} 还箱令｜{n_items} 票"
    if et == _RELEASE_CANCEL:
        return f"【取消】渝新欧放箱｜{train}｜{n_items} 票"
    return f"渝新欧放箱｜{train}｜{n_items} 票"


def _tr(cells: List[str], header: bool = False) -> str:
    tag = "th" if header else "td"
    style = _TH_STYLE if header else _TD_STYLE
    inner = "".join(f'<{tag} style="{style}">{html.escape(str(c))}</{tag}>' for c in cells)
    return f"<tr>{inner}</tr>"


def _span(items: List[Dict[str, Any]]) -> str:
    """合并邮件的时间区间（首票 – 末票），放脚注。"""
    stamps = sorted((it.get("created_at") or "").strip() for it in items if (it.get("created_at") or "").strip())
    if not stamps:
        return ""
    first, last = stamps[0], stamps[-1]
    if first[:10] == last[:10]:
        return f"{first[:16]}–{last[11:16]}" if first != last else first[:16]
    return f"{first[:16]}–{last[:16]}"


def _wrap_html(intro: str, table: str, below: List[str], items: List[Dict[str, Any]]) -> str:
    parts = [
        '<div style="font-family: Tahoma, Arial, \'微软雅黑\', SimSun; font-size: 13px;">',
        f"<p>{html.escape(intro)}</p>",
        table,
    ]
    for snippet in below:
        parts.append(snippet)
    span = _span(items)
    foot = f"本封由系统合并 {len(items)} 封原始邮件生成"
    if span:
        foot += f"（{span}）"
    parts.append(f'<p style="color: #888; font-size: 12px;">{html.escape(foot)}</p>')
    parts.append("</div>")
    return "".join(parts)


def _build_html(batch: Dict[str, Any], items: List[Dict[str, Any]]) -> str:
    """正文 = 全部票的 HTML 表格（不是 markdown，也不是 `|` 竖线）。

    规则（spec 03 §5.2）：
      全票 note 相同 → 表格下方只写一次；有差异 → 加「备注」列逐条列原文；
      全为空 → 整段省略；有效期为独立字段：全票相同 → 加粗一行，有差异 → 加列。
    """
    if str(batch.get("email_type") or "") == _RETBOX:
        return _build_retbox_html(batch, items)

    if str(batch.get("email_type") or "") == _RELEASE_CANCEL:
        intro = "您好！以下客户编码的放箱令已取消分配，请查收附件。"
    else:
        intro = "您好！以下客户编码已分配放箱令，请查收附件。"

    notes = [_clean_note(it.get("note")) for it in items]
    valids = [(it.get("validity") or "").strip() for it in items]

    note_all_same = len(set(notes)) == 1
    valid_all_same = len(set(valids)) == 1
    show_note_col = not note_all_same
    show_valid_col = not valid_all_same

    headers = ["客户编码", "放箱令号", "数字编码"]
    if show_valid_col:
        headers.append("有效期")
    if show_note_col:
        headers.append("备注")

    rows = []
    for it, note, valid in zip(items, notes, valids):
        cells = [it.get("customer_code") or "-",
                 it.get("release_no") or "-",
                 it.get("code_no") or "-"]
        if show_valid_col:
            cells.append(valid or "-")
        if show_note_col:
            cells.append(note or "-")
        rows.append(_tr(cells))

    table = ('<table style="border-collapse: collapse; font-size: 13px;">'
             f"<thead>{_tr(headers, header=True)}</thead>"
             f"<tbody>{''.join(rows)}</tbody></table>")

    below: List[str] = []
    if note_all_same:
        shared = notes[0] if notes else ""
        if shared:
            below.append(f"<p>{html.escape(shared)}</p>")
    if valid_all_same:
        shared_valid = valids[0] if valids else ""
        if shared_valid:
            below.append(f"<p><b>有效期：{html.escape(shared_valid)}</b></p>")
    return _wrap_html(intro, table, below, items)


def _build_retbox_html(batch: Dict[str, Any], items: List[Dict[str, Any]]) -> str:
    """还箱令：无附件，列 客户编码 / 箱号 / 班列号 / 还箱令文件 / 链接。"""
    intro = "您好！以下箱号请查收还箱令，谢谢。"
    links = [(it.get("link") or "").strip() for it in items]
    link_all_same = len(set(links)) == 1
    show_link_col = not link_all_same

    headers = ["客户编码", "箱号", "班列号", "还箱令文件"]
    if show_link_col:
        headers.append("链接")

    rows = []
    for it, link in zip(items, links):
        cells = [it.get("customer_code") or "-",
                 it.get("box_no") or "-",
                 batch.get("train_no") or "-",
                 it.get("retbox_doc") or "-"]
        if show_link_col:
            cells.append(link or "-")
        rows.append(_tr(cells))

    table = ('<table style="border-collapse: collapse; font-size: 13px;">'
             f"<thead>{_tr(headers, header=True)}</thead>"
             f"<tbody>{''.join(rows)}</tbody></table>")

    below: List[str] = []
    if link_all_same and links and links[0]:
        safe = html.escape(links[0], quote=True)
        below.append(f'<p><b>还箱令链接：</b><a href="{safe}">{html.escape(links[0])}</a></p>')
    return _wrap_html(intro, table, below, items)


# ---------------------------------------------------------------- zip 打包

def _ext_of(att: Dict[str, Any]) -> str:
    ext = os.path.splitext(att.get("filename") or "")[1]
    if ext:
        return ext
    ct = (att.get("content_type") or "").lower()
    if ct == "application/pdf":
        return ".pdf"
    if ct in ("image/jpeg", "image/jpg"):
        return ".jpg"
    return ".bin"


def _arcname(item: Dict[str, Any], ext: str) -> str:
    """zip 内文件名：`{放箱令号} 【{数字编码}】{原扩展名}`，例 `0156-33596910-1 【49】.pdf`。"""
    release_no = item.get("release_no") or item.get("customer_code") or ""
    code_no = item.get("code_no") or ""
    return f"{release_no} 【{code_no}】{ext}"


def _zip_basename(batch: Dict[str, Any]) -> str:
    train = batch.get("train_no") or "unknown"
    m = re.search(r"(\d{8})", batch.get("batch_id") or "")
    day = m.group(1) if m else datetime.now().strftime("%Y%m%d")
    if str(batch.get("email_type") or "") == _RELEASE_CANCEL:
        return f"{train}_取消放箱令_{day}"
    return f"{train}_放箱令_{day}"


def _zip_bytes(chunk: List[Tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    seen: Dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in chunk:
            arc = name
            if arc in seen:
                # 同名不覆盖（业务上一票一文件，理论不会撞；撞了也不能静默丢）
                seen[arc] += 1
                stem, ext = os.path.splitext(arc)
                arc = f"{stem}({seen[arc]}){ext}"
            else:
                seen[arc] = 0
            zf.writestr(arc, payload)
    return buf.getvalue()


def _split_entries(entries: List[Tuple[str, bytes]], limit: int) -> List[List[Tuple[str, bytes]]]:
    """按未压缩体积切卷：单卷超过 limit 才切（PDF 已压缩，zip 几乎不再变小）。"""
    if limit <= 0:
        return [entries] if entries else []
    parts: List[List[Tuple[str, bytes]]] = []
    cur: List[Tuple[str, bytes]] = []
    cur_size = 0
    for name, payload in entries:
        size = len(payload)
        if cur and cur_size + size > limit:
            parts.append(cur)
            cur, cur_size = [], 0
        cur.append((name, payload))
        cur_size += size
    if cur:
        parts.append(cur)
    return parts


def _build_zip_parts(items: List[Dict[str, Any]],
                     attachments_by_item: Dict[int, List[Dict[str, Any]]],
                     batch: Dict[str, Any],
                     max_bytes: Optional[int] = None) -> List[Dict[str, Any]]:
    """一票一文件（业务要求「一箱一单」，不按内容去重），超限才分卷。

    返回 [{"payload": bytes, "filename": str, "content_type": "application/zip"}]。
    某票无附件 ⇒ 该票仍在表格里、zip 内无文件，且**告警**（不能静默少一个）。
    还箱令无附件，直接返回 []（build_forward_message 里 `[]` 是 falsy，不会生空附件）。
    """
    if str(batch.get("email_type") or "") == _RETBOX:
        return []

    limit = int(settings.RELEASE_ZIP_MAX_BYTES if max_bytes is None else max_bytes)
    entries: List[Tuple[str, bytes]] = []
    missing: List[Dict[str, Any]] = []
    for it in items:
        atts = attachments_by_item.get(int(it["id"])) or []
        kept = 0
        for att in atts:
            payload = att.get("payload") or b""
            if not payload:
                continue
            entries.append((_arcname(it, _ext_of(att)), payload))
            kept += 1
        if not kept:
            missing.append(it)

    if missing:
        sample = ", ".join(
            f"{it.get('customer_code') or '-'}/{it.get('release_no') or '-'}" for it in missing[:10])
        detail = (f"release item(s) without attachment | batch={batch.get('batch_id')} |"
                  f" count={len(missing)} | {sample}")
        _log.warning(detail)
        increment_counter("release_item_no_attachment")
        _alert(detail, kind="program")

    if not entries:
        return []

    parts = _split_entries(entries, limit)
    base = _zip_basename(batch)
    total = len(parts)
    out: List[Dict[str, Any]] = []
    for idx, chunk in enumerate(parts, start=1):
        name = f"{base}.zip" if total == 1 else f"{base}_part{idx}.zip"
        out.append({
            "payload": _zip_bytes(chunk),
            "filename": name,
            "content_type": "application/zip",
        })
    return out


# ---------------------------------------------------------------- flush

def _mark_items_seen(items: List[Dict[str, Any]]) -> None:
    """第 9 步：补标已读。失败只 WARN —— 邮件已经发出去了。"""
    for it in items:
        uid = int(it.get("uid") or 0)
        if not uid:
            continue
        try:
            mark_seen(it.get("account") or "", it.get("folder") or "", uid)
        except Exception as e:
            _log.warning(f"mark_seen failed (mail already sent) | item={it.get('id')} | {e}")


def _migrate_leftovers(old_bid: str, batch: Dict[str, Any]) -> int:
    """第 10 步收尾复查：仍留在旧批的 pooled item 迁到新批次（4 步一个事务）。

    只有极小的竞态窗口会走到这里（抢占有 UPDATE 原子性，正常进不到）。
    新批的 first/last_item_at 用**当前时间**，让它按正常静默闸节奏走，
    不会一建出来就被硬上限放行。
    """
    now = _ts()
    with _db_lock:
        conn = get_dedup_connection()
        try:
            leftover = conn.execute(
                "SELECT id FROM release_item WHERE batch_id=? AND status='pooled'",
                (old_bid,),
            ).fetchall()
            n = len(leftover)
            if not n:
                return 0
            new_bid = _next_batch_suffix(conn, old_bid)
            conn.execute(
                "INSERT INTO release_batch (batch_id, company, train_no, email_type,"
                " item_count, first_item_at, last_item_at, status)"
                " VALUES (?,?,?,?,?,?,?,'pending')",
                (new_bid, batch.get("company") or "", batch.get("train_no") or "",
                 batch.get("email_type") or "", n, now, now),
            )
            conn.execute(
                "UPDATE release_item SET batch_id=? WHERE batch_id=? AND status='pooled'",
                (new_bid, old_bid),
            )
            conn.execute(
                "UPDATE release_batch SET item_count = item_count - ? WHERE batch_id=?",
                (n, old_bid),
            )
            conn.commit()
        finally:
            conn.close()
    _log.warning(f"batch leftover migrated | from={old_bid} to={new_bid} items={n}")
    return n


def flush_batch(batch: Dict[str, Any]) -> bool:
    """把一批合成 1 封发出。返回 True = 发出去了（或已确认发过）。

    顺序见 `02_TASK.md` §4.5，**不要调整**。
    """
    bd = dict(batch)
    batch_id = bd["batch_id"]

    # 0. 抢占 pending → flushing（rowcount==0 ⇒ 别人抢走了，不重复发）
    if not _claim_batch(batch_id):
        _log.info(f"flush skipped, not pending anymore | batch={batch_id}")
        return False

    # 1. 取该批全部 pooled item
    items = _load_pooled_items(batch_id)
    if not items:
        _set_batch_status(batch_id, ST_ABORTED)
        _log.warning(f"batch aborted, no pooled items | batch={batch_id}")
        return False

    # 2.5 取附件（一次登录批量 UID FETCH）—— 任一票失败 ⇒ 整批 failed，不发半批
    try:
        attachments_by_item = _fetch_item_attachments(items, bd)
    except Exception as e:
        _fail_batch(
            batch_id,
            f"release batch attachment refetch failed, batch NOT sent | batch={batch_id} |"
            f" company={bd.get('company')} | items={len(items)} |"
            f" {type(e).__name__}: {e}",
        )
        return False

    # 2.6 现查收件人（release_batch 只存了 company）
    # 🔴 to_list 为空必须 failed：send_smtp([]) 会让 server.sendmail(env_from, [], ...)
    # 返回成功 ⇒ 代码标 sent 但一封都没发出去（测试里 send_smtp 又是桩的 ⇒ 全绿而功能假）。
    to_list, cc_list = get_recipients(bd.get("company") or "")
    responsible = get_responsible_person(bd.get("company") or "")
    if not to_list:
        _fail_batch(
            batch_id,
            f"E2 missing recipients for company {bd.get('company')} | batch={batch_id} |"
            f" items={len(items)}",
            kind="config",
        )
        return False
    sender_email, sender_password = _resolve_sender(responsible, items)
    if not sender_password:
        _fail_batch(
            batch_id,
            f"E2 missing SMTP credentials for sender {sender_email or '-'} | batch={batch_id}",
            kind="config",
        )
        return False

    # 3/4/5. 正文 → zip → 邮件
    html_body = _build_html(bd, items)
    try:
        zip_parts = _build_zip_parts(items, attachments_by_item, bd,
                                     settings.RELEASE_ZIP_MAX_BYTES)
    except Exception as e:
        _fail_batch(batch_id,
                    f"release batch zip build failed | batch={batch_id} |"
                    f" {type(e).__name__}: {e}")
        return False

    subject = build_subject(bd, len(items))
    fid = make_forward_id(f"BATCH::{batch_id}", f"batch::{batch_id}")
    shell = email.message.Message()
    shell["Subject"] = subject
    # subject_prefix 必须空串：build_forward_message 里是 f"{subject_prefix}{original_subject}"，
    # 非空会把前缀和 shell 的 Subject 拼在一起。original_msg 只用于取 Subject。
    msg = build_forward_message(
        shell, to_list, cc_list, sender_email,
        subject_prefix="",
        html_override=html_body,
        attachments_override=zip_parts,
        forward_id=fid,
    )

    # 6. 发信
    result = send_smtp(msg, sender_email, sender_password, to_list, cc_list)
    if not (isinstance(result, dict) and result.get("success")):
        err = result.get("error") if isinstance(result, dict) else "unknown"
        _fail_batch(batch_id,
                    f"release batch SMTP failed, will NOT retry automatically |"
                    f" batch={batch_id} | company={bd.get('company')} | {err}")
        return False

    # 6.5 记账 —— 🔴 必须在标 sent **之前**：崩溃恢复判据靠这个顺序。
    # 异常必须吞掉：邮件已经发出去了，冒泡会把 batch 卡在 flushing ⇒ 启动恢复
    # 判"没发" ⇒ 重发（客户收到两封一样的放箱令）。
    try:
        _record_forward(
            forward_id=fid,
            message_id=f"BATCH::{batch_id}",
            row=None,                                   # 合并场景没有单行
            send_ctx={"_row_key": f"batch::{batch_id}"},  # 🔴 必须给，否则 serve_row_key(None) 会炸
            to_list=to_list,
            cc_list=cc_list,
            original_raw=msg.as_bytes(),
            sent_by=sender_email,
            company=bd.get("company") or "",
        )
    except Exception as e:
        _log.error(f"record_forward failed (mail already sent) | batch={batch_id} |"
                   f" {type(e).__name__}: {e}")
        increment_counter("post_send_bookkeeping_failed")

    # 7. 标 sent（+ forward_id）
    total_bytes = sum(len(p.get("payload") or b"") for p in zip_parts)
    _mark_batch_sent(batch_id, fid, len(items), total_bytes, len(zip_parts))

    # 8. item 全标 sent
    _mark_items_sent(batch_id)

    # 9. 对整批补标已读（失败只 WARN）
    _mark_items_seen(items)

    # 10. 收尾复查：flush 期间万一还有票漏进来，迁到新批次
    _migrate_leftovers(batch_id, bd)

    _log.info(
        f"release batch sent | batch={batch_id} | items={len(items)} |"
        f" parts={len(zip_parts)} | bytes={total_bytes} | to={len(to_list)}cc{len(cc_list)}"
    )
    return True


# ---------------------------------------------------------------- 线程 / 启动恢复

def run_aggregator(stop_event=None, interval_sec: Optional[int] = None):
    """聚合器独立线程：每 10 秒扫一次到期批次。

    退出信号与 run_sweeper / run_digest 一致，用 serve 的 `_shutdown_event`
    （不自己发明 stop 机制：那些线程也是 daemon，进程退出即结束；
    另起一个没人 set 的 Event 就是死代码）。
    """
    stop = stop_event
    if stop is None:
        from ..serve import _shutdown_event as stop  # 延迟导入，避免 serve ↔ core 循环
    interval = int(AGGREGATOR_INTERVAL_SEC if interval_sec is None else interval_sec)

    while not stop.is_set():
        try:
            for b in list_due_batches():
                try:
                    flush_batch(b)
                except Exception as e:
                    # 一批炸不能拖垮整个循环
                    _log.error(f"flush_batch failed | batch={b.get('batch_id')} |"
                               f" {type(e).__name__}: {e}")
        except Exception as e:
            _log.error(f"aggregator error: {type(e).__name__}: {e}")
        if hasattr(stop, "wait"):
            stop.wait(interval)
        else:
            time.sleep(interval)


def _load_recovery_batches() -> List[Dict[str, Any]]:
    return _rows("SELECT * FROM release_batch WHERE status IN ('pending', 'flushing')"
                 " ORDER BY first_item_at ASC")


def _forward_log_has_batch(batch_id: str) -> bool:
    """`forward_log` 里有 `BATCH::<batch_id>` ⇒ 这批**已经发出去了**。

    🔴 不要拿 batch.forward_id 比对：崩在写 forward_log 与标 sent 之间时
    batch.forward_id 还是空的，比对会判"不一致"⇒ 把已发的批当成没发 ⇒ 重发。
    只认 message_id，命中即算发过（forward_id 反而用它回填）。
    """
    return find_forward_log_by_message_id(f"BATCH::{batch_id}") is not None


def _finalize_sent(batch: Dict[str, Any], reason: str = "") -> None:
    """崩在 send 成功之后、标 sent 之前：补状态、补标已读，**不重发**。"""
    batch_id = batch["batch_id"]
    row = find_forward_log_by_message_id(f"BATCH::{batch_id}") or {}
    fid = row.get("forward_id") or batch.get("forward_id") or ""
    items = _load_pooled_items(batch_id)
    _mark_batch_sent(batch_id, fid, batch.get("item_count") or len(items),
                     batch.get("attachment_bytes") or 0, batch.get("parts") or 1)
    _mark_items_sent(batch_id)
    _mark_items_seen(items)
    _log.warning(f"batch recovered as sent ({reason}) | batch={batch_id} |"
                 f" fid={fid} | items={len(items)}")


def flush_due_on_startup() -> Dict[str, int]:
    """启动恢复：`pending` **和** `flushing` 都要扫。

    - `flushing` + `forward_log` 有记录 ⇒ 已发出：标 sent、item 全 sent、补标已读，**不重发**
    - `flushing` + 无记录 ⇒ 崩在发送之前：重置 `pending` 交回正常流程（超静默窗口立即发）
    - `pending` + 已过静默窗口 ⇒ 立即 flush；否则交给聚合器线程按节奏处理
    """
    init_release_tables()
    stats = {"pending": 0, "flushing": 0, "recovered_sent": 0, "reset": 0, "flushed": 0}
    try:
        batches = _load_recovery_batches()
    except Exception as e:
        _log.error(f"release startup recovery scan failed: {type(e).__name__}: {e}")
        return stats

    for b in batches:
        try:
            if b.get("status") == ST_FLUSHING:
                stats["flushing"] += 1
                if _forward_log_has_batch(b["batch_id"]):
                    _finalize_sent(b, reason="crash after send")
                    stats["recovered_sent"] += 1
                    continue
                _set_batch_status(b["batch_id"], ST_PENDING)
                stats["reset"] += 1
                _log.warning(f"batch reset flushing→pending (never sent) | batch={b['batch_id']}")
            else:
                stats["pending"] += 1
            if _quiet_elapsed(b) and flush_batch(b):
                stats["flushed"] += 1
        except Exception as e:
            _log.error(f"release startup recovery failed | batch={b.get('batch_id')} |"
                       f" {type(e).__name__}: {e}")
    _log.info(f"release startup recovery: {stats}")
    return stats
