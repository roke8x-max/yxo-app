"""NDR /退信监控：收 NDR、解析失败、把对应转发重新拉回 error_queue。

rev4（2026-09-11）：不依赖专用退信邮箱。envelope MAIL FROM 即发信账号本人，
NDR 落回同事自己收件箱；poll_bounces 轮询 4 个账号的 INBOX，主关联键为
X-YXO-Forward-Id 信头（VERP 不可用），fid 缺失时用次键
（failed_recipient + 收信账号 → forward_log 唯一命中）兜底。

rev4.1：取信一律 BODY.PEEK（RFC822 会隐式置 Seen，吃掉同事未读角标）；
幂等改用本地 bounce_handled 索引，不再写 Seen；两段式轮询（HEADER 搜索 +
头部预筛，fail-open）；is_ndr 判据 (c) 收紧为 Action + Final-Recipient/
Diagnostic-Code 同时成立。
"""
import imaplib
import email
import re
from email.parser import BytesParser
from mailbots_next.core import dedup
from mailbots_next.core.store import (
    get_forward_log,
    mark_forward_bounced,
    _forward_log_candidates,
    is_bounce_handled,
    mark_bounce_handled,
    purge_old_bounce_handled,
)
from mailbots_next.core.notify import get_notifier
from mailbots_next.config import settings
from mailbots_next.core.log import get_logger

_log = get_logger(__name__)

# F3 daily purge bookkeeping (module-level, process lifetime)
from datetime import date as _date

_last_purge_date = None  # type: ignore

_FID_RE = re.compile(r"(?im)^x-yxo-forward-id:\s*([0-9a-f]{16}-[0-9a-f]{8})\s*$")
_VERP_RE = re.compile(r"bounce\+([0-9a-fA-F\-]+)@")
_UID_RE = re.compile(rb"UID\s+(\d+)")

# Subject keywords for the header prescreen (fail-open: miss nothing).
_NDR_SUBJECT_KWS = (
    "Undeliverable", "Delivery Status", "Delivery Failure",
    "Mail delivery failed", "Returned mail",
    # Chinese (F2) — almost zero cost
    "退信", "退信通知", "无法投递", "未送达", "投递失败", "邮件退回",
)
_NDR_FROM_KWS = ("mailer-daemon", "postmaster",)


def _decode_part_text(part) -> str:
    """取部件正文文本（处理嵌套的 text/plain 子部分）。"""
    payload = part.get_payload(decode=True)
    if payload:
        return payload.decode("utf-8", "replace")
    txt = ""
    for subpart in part.walk():
        if subpart is part:
            continue
        if subpart.get_content_type() == "text/plain":
            sub_payload = subpart.get_payload(decode=True)
            if sub_payload:
                txt = sub_payload.decode("utf-8", "replace")
                break
    return txt


def parse_ndr(raw: bytes) -> dict:
    """解析一封 NDR，返回 {'forward_id': str|None, 'action': str,
    'diagnostic': str, 'failed_recipient': str, 'raw_headers': dict}。
    关联键优先级：VERP 本地名(bounce+<hex>@) > 部件 header 的 X-YXO-Forward-Id
    > text/rfc822-headers 及正文里的 X-YXO-Forward-Id > 整封 raw 正则兜底。"""
    msg = BytesParser().parsebytes(raw)
    out = {"forward_id": None, "action": "", "diagnostic": "", "failed_recipient": "", "raw_headers": {}}
    # 1) VERP：NDR 的 To 即 envelope 收件人 bounce+<hex>@（forward_id 为纯 hex，正则干净匹配）
    to_hdr = msg.get("To", "")
    m = _VERP_RE.search(to_hdr)
    if m:
        out["forward_id"] = m.group(1)
    # 2) 兜底：原始信头里的 X-YXO-Forward-Id（multipart/report 的 returned message 部分）
    for part in msg.walk():
        cid = part.get("X-YXO-Forward-Id")
        if cid:
            out["forward_id"] = out["forward_id"] or cid
        ctype = part.get_content_type()
        if ctype in ("message/delivery-status", "text/plain"):
            txt = _decode_part_text(part)
            for line in txt.splitlines():
                if line.lower().startswith("action:"):
                    out["action"] = line.split(":", 1)[1].strip()
                elif line.lower().startswith("diagnostic-code:"):
                    out["diagnostic"] = line.split(":", 1)[1].strip()
                elif line.lower().startswith("final-recipient:"):
                    out["failed_recipient"] = line.split(";", 1)[-1].strip()
    # 3) 正文形态：text/rfc822-headers 把原信头当正文文本回传，header 取不到
    if not out["forward_id"]:
        for part in msg.walk():
            if part.get_content_type() in ("text/rfc822-headers", "text/plain",
                                           "message/delivery-status"):
                m2 = _FID_RE.search(_decode_part_text(part))
                if m2:
                    out["forward_id"] = m2.group(1)
                    break
    # 4) 兜底：整封 raw 正则
    if not out["forward_id"]:
        m3 = _FID_RE.search(raw.decode("utf-8", "replace"))
        if m3:
            out["forward_id"] = m3.group(1)
    return out


def is_ndr(msg) -> bool:
    """严格判定是否为 NDR（防误伤同事正常邮件）。
    (a) 存在 message/delivery-status 部件；(b) 顶层 multipart/report 且
    report-type=delivery-status；(c) 某正文块同时含 Action: 行与
    Final-Recipient:/Diagnostic-Code: 行（单 Action 行不算，防会议纪要误判）。"""
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            return True
    if msg.get_content_type() == "multipart/report" and \
            "delivery-status" in (msg.get_param("report-type") or ""):
        return True
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "message/delivery-status"):
            lines = [l.lower() for l in _decode_part_text(part).splitlines()]
            has_action = any(l.startswith("action:") for l in lines)
            has_detail = any(l.startswith("final-recipient:") or
                             l.startswith("diagnostic-code:") for l in lines)
            if has_action and has_detail:
                return True
    return False


def _looks_like_ndr_headers(hdr_text: str) -> bool:
    """头部预筛（fail-open：没把握就取全文，宁可多取不可漏）。
    仅当头部**明确像一封普通私人邮件**（有真实 From 且零 NDR 标记）时才跳过；
    头部缺失/模糊的一律取全文。"""
    t = hdr_text or ""
    tl = t.lower()
    if "multipart/report" in tl or "report-type" in tl or "delivery-status" in tl:
        return True
    for kw in _NDR_SUBJECT_KWS:
        if kw.lower() in tl:
            return True
    for kw in _NDR_FROM_KWS:
        if kw in tl:
            return True
    if "auto-submitted:" in tl and ("auto-replied" in tl or "auto-generated" in tl):
        return True
    m = re.search(r"(?im)^from:\s*(.+?)\s*$", t)
    if m and m.group(1).strip():
        return False  # 有真实发件人且无任何 NDR 标记 → 普通邮件，跳过全文
    return True  # 连 From 都没有：形态模糊，取全文，fail-open


def _poll_accounts():
    """返回本轮要轮询的账号列表。BOUNCE_POLL_ACCOUNTS 显式配置优先；
    其次沿用遗留单邮箱配置 [BOUNCE_IMAP_USER]（与旧单测及 fixed/verp 部署兼容）；
    否则 sender 模式 → DEFAULT_ACCOUNTS。"""
    raw = (settings.BOUNCE_POLL_ACCOUNTS or "").strip()
    if raw:
        return [a.strip() for a in raw.split(",") if a.strip()]
    if settings.BOUNCE_IMAP_USER:
        return [settings.BOUNCE_IMAP_USER]
    if (settings.BOUNCE_ENVELOPE_MODE or "sender").lower() == "sender":
        from mailbots_next.config import DEFAULT_ACCOUNTS
        return list(DEFAULT_ACCOUNTS)
    return []


def _fetch_uid(conn, num):
    """取该封的 IMAP UID（handled 索引键）。解析失败返回 None（绝不回退序号，
    序号漂移会导致 handled 误命中而漏退信）。"""
    try:
        _, data = conn.fetch(num, "(UID)")
        for t in data or []:
            first = t[0] if isinstance(t, tuple) and t else (t if isinstance(t, bytes) else b"")
            if isinstance(first, bytes):
                m = _UID_RE.search(first)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return None


def _fetch_payload(conn, num, item):
    """PEEK 取信（永不置 Seen）。返回部件字节，取不到返回 b''。"""
    _, data = conn.fetch(num, item)
    for t in data or []:
        if isinstance(t, tuple) and len(t) > 1 and isinstance(t[1], (bytes, bytearray)) and t[1]:
            return bytes(t[1])
    return b""


def _handle_ndr(acct, ndr, stats):
    """已判定为 NDR 的关联与入队。返回 outcome 字符串。Seen 一律不写。"""
    if not ndr["forward_id"]:
        # 次键：failed_recipient + 本封所在收信账号 → forward_log 唯一命中
        cands = []
        if ndr["failed_recipient"]:
            cands = _forward_log_candidates(ndr["failed_recipient"], sent_by=acct)
        if len(cands) == 1:
            ndr = dict(ndr, forward_id=cands[0]["forward_id"])
        else:
            _log.warning(
                "NDR with no forward_id (subkey candidates=%d): %s"
                % (len(cands), ndr["diagnostic"])
            )
            get_notifier().send_program_error(
                settings.OPS_OWNER_EMAIL, "0",
                "Unmatched NDR (no forward_id): %s" % ndr["diagnostic"])
            stats["unknown"] += 1
            return "unknown"
    rec = get_forward_log(ndr["forward_id"])
    if rec and not rec["bounced"]:
        dedup.add_error(
            rec["message_id"], rec["row_key"], "act", "BOUNCE",
            "NDR bounce: %s -> %s" % (ndr["diagnostic"], ndr["failed_recipient"]),
            "bounce-%s" % ndr["forward_id"],
            account=rec["account"], folder=rec["folder"], uid=rec["uid"],
            subject=rec["subject"], sender=rec["sender"], date_hdr=rec["date_hdr"],
            raw_bytes=bytes.fromhex(rec["raw_hex"]),
        )
        mark_forward_bounced(ndr["forward_id"])
        get_notifier().send_program_error(
            settings.OPS_OWNER_EMAIL, "bounce-%s" % ndr["forward_id"],
            "Forward bounced, re-queued for retry: %s (%s)"
            % (ndr["forward_id"], ndr["diagnostic"]))
        stats["requeued"] += 1
        return "requeued"
    if rec and rec["bounced"]:
        _log.info("NDR already handled for %s; skip" % ndr["forward_id"])
        return "dup"
    _log.warning("NDR forward_id not in forward_log: %s" % ndr["forward_id"])
    get_notifier().send_program_error(
        settings.OPS_OWNER_EMAIL, "0",
        "NDR for unknown forward_id %s: %s" % (ndr["forward_id"], ndr["diagnostic"]))
    stats["unknown"] += 1
    return "unknown"


def _poll_one(conn, acct, num, stats):
    """处理一个候选序号：UID → 头预筛 → 全文 → 严格判定 → handled 幂等 → 关联。
    若拿不到稳定 UID（None），绕开 handled 索引照常处理（最坏重复一次，
    下游 bounced/UNIQUE 兜着，不漏信）。"""
    from mailbots_next.core.notify import increment_counter
    uid = _fetch_uid(conn, num)
    if uid is None:
        _log.warning("Bounce: no UID, handled index bypassed | acct=%s seq=%s" % (acct, num))
        increment_counter("bounce_uid_missing")
    hdr = _fetch_payload(conn, num, "(BODY.PEEK[HEADER])")
    if not _looks_like_ndr_headers(hdr.decode("utf-8", "replace")):
        stats["skipped_non_ndr"] += 1
        return
    raw = _fetch_payload(conn, num, "(BODY.PEEK[])")
    if not raw:
        stats["errors"] += 1
        return
    msg = BytesParser().parsebytes(raw)
    if not is_ndr(msg):
        stats["skipped_non_ndr"] += 1
        return
    if uid is not None and is_bounce_handled(acct, uid):
        stats["deduped"] += 1
        return
    ndr = parse_ndr(raw)
    stats["processed"] += 1
    # 只处理失败类退信；delayed/delivered 一律忽略（仍记 handled 防重复看）
    if (ndr["action"] or "").lower() != "failed":
        _log.info("NDR action=%s ignored (not failed) | acct=%s"
                  % (ndr["action"] or "?", acct))
        if uid is not None:
            mark_bounce_handled(acct, uid, "", ndr["forward_id"] or "", "ignored")
        return
    outcome = _handle_ndr(acct, ndr, stats)
    if uid is not None:
        mark_bounce_handled(acct, uid, "", ndr.get("forward_id") or "", outcome)


def _candidate_seqnos(conn):
    """候选 = 全部未读。绝不使用 HEADER 定向搜索做替代：
    SEARCH ... HEADER 只匹配顶层信头，是 UNSEEN 的子集，
    用它替代会漏掉顶层非 multipart/report 形态的 NDR。"""
    typ, data = conn.search(None, "UNSEEN")
    return data[0].split() if data and data[0] else []


def poll_bounces() -> dict:
    """轮询各账号 INBOX 的 NDR。单账号异常不得中断其它账号。永不写 Seen。"""
    from mailbots_next.config.secrets import get_accounts

    global _last_purge_date

    if not settings.BOUNCE_MONITOR_ENABLED:
        return {"skipped": True}
    accounts = _poll_accounts()
    if not accounts:
        _log.warning("Bounce monitor enabled but no poll accounts; skipping")
        return {"skipped": True}
    stats = {"processed": 0, "requeued": 0, "unknown": 0,
             "skipped_non_ndr": 0, "deduped": 0, "errors": 0}
    creds = get_accounts()
    for acct in accounts:
        if acct == settings.BOUNCE_IMAP_USER and settings.BOUNCE_IMAP_PASSWORD:
            pwd = settings.BOUNCE_IMAP_PASSWORD
        else:
            pwd = creds.get(acct, "")
        if not pwd:
            _log.warning("Bounce poll skipped for %s: no IMAP credentials" % acct)
            continue
        try:
            conn = imaplib.IMAP4_SSL(settings.BOUNCE_IMAP_SERVER,
                                     settings.BOUNCE_IMAP_PORT, timeout=60)
            try:
                conn.login(acct, pwd)
                conn.select(settings.BOUNCE_FOLDER)
                for num in _candidate_seqnos(conn):
                    try:
                        _poll_one(conn, acct, num, stats)
                    except Exception as e:
                        _log.error("Bounce message handling failed | acct=%s: %s" % (acct, e))
                        stats["errors"] += 1
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            _log.error("Bounce poll failed for %s: %s" % (acct, e))
            stats["errors"] += 1
    # F3: daily purge (cheap for small table, but no need every 5 min)
    today = _date.today()
    if _last_purge_date != today:
        try:
            purge_old_bounce_handled()
            _last_purge_date = today
        except Exception as e:
            _log.warning("bounce_handled purge failed: %s" % e)
    return stats
