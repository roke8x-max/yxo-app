#!/usr/bin/env python3
import argparse
import email
import email.policy
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import List, Dict, Any, Optional
import json

from mailbots_next import config
from mailbots_next.config import (
    DEFAULT_ACCOUNTS,
    IDLE_GROUPS,
    INGEST_POLL_SEC,
    NON_AUTO_DRAFT_CATEGORIES,
    INBOUND_PORT,
    INBOUND_SHARED_SECRET,
    OPS_OWNER_EMAIL,
    FORWARD_SINCE,
    BOUNCE_MONITOR_ENABLED,
    BOUNCE_POLL_SEC,
    DAILY_REPORT_HOUR,
    DAILY_REPORT_MINUTE,
    EmailType,
    get_accounts,
    is_type_enabled,
    get_folder_groups,
    snapshot,
    ConfigSnapshot,
)
from mailbots_next.core import (
    Poller,
    extract_email,
    route_row,
    decide,
    execute_action,
    get_notifier,
    try_claim,
    add_error,
    has_pending_error,
    load_records,
    get_responsible_person,
    get_recipients,
    get_sender_by_email,
    increment_counter,
    flush_counters,
    has_pending_error,
    has_pending_message,
    mark_seen,
    enqueue_mark_seen,
    EmailLogger,
    get_connection,
    poll_bounces,
    record_issue,
    write_digest_snapshot,
)
# 任务 12：复制归档 / 每日核对删除（独立模块，不参与 core 的 __init__ 汇总导出）
from mailbots_next.core import archive
# 任务 13：发件人域规范化 —— 与 core/extractors/draft.py 共用同一份实现，
# 不许在本文件里再写一遍（两份实现早晚漂移，只改一处 = 白改）。
from mailbots_next.core.sender_addr import normalize_sender_domain

_log = EmailLogger("mailbots_next.serve")

_shutdown_event = threading.Event()
_pollers: List[Poller] = []
_inbound_server: Optional[HTTPServer] = None
_last_bounce_poll_ts: float = float("-inf")  # G3: guarantee first tick polls


class MailProcessor:
    def __init__(self, config: ConfigSnapshot):
        self.config = config
        self.records = load_records()
        self.accounts = get_accounts()
        self.notifier = get_notifier()
        # 行级明细的线程本地槽（T5-5）：12 条轮询线程共享同一 processor 实例，
        # 实例属性会被并发覆盖；sweeper 在自己线程内 set 后 get，行为不变。
        self._outcome_local = threading.local()

    def _set_last_outcome(self, outcome: dict) -> None:
        self._outcome_local.value = outcome

    def _get_last_outcome(self):
        return getattr(self._outcome_local, "value", None)

    def process_email(self, account: str, folder: str, message_id: str, uid: int, subject: str, sender: str, date_hdr: str, raw_bytes: bytes) -> bool:
        """统一契约：返回布尔，True = 该封已纳入持久化流程（全成功 或 至少已入 error_queue）。

        - 任何单封级失败（含 _identify_type / extract_email 前置阶段）内部负责入队（带 raw_bytes）并正常返回 True。
        - 只有致命错误（DB 不可达等、连入队本身都无法完成）才向上抛异常。
        - 返回 False = 未纳入（理论上不应出现），外层不得推进水位。
        - 外层（Poller）判断只依赖这一个值。
        """
        _log.log_email_received(message_id, folder, sender, subject)

        if not message_id:
            import hashlib
            message_id = f"SYN::{hashlib.sha256(f'{account}|{folder}|{uid}'.encode()).hexdigest()[:32]}"

        try:
            email_type = self._identify_type(sender, subject, raw_bytes)
        except Exception as e:
            error_id = _log.error(f"Identify failed: {type(e).__name__}: {e}", error_id=None)
            if not has_pending_message(message_id):
                add_error(message_id, "", "identify", "IDENTIFY_EXCEPTION",
                          f"{type(e).__name__}: {e}", error_id,
                          account=account, folder=folder, uid=uid,
                          subject=subject, sender=sender, date_hdr=date_hdr,
                          raw_bytes=raw_bytes)
            increment_counter("identify_failed")
            self._set_last_outcome({"queued": 1})
            return True
        if not email_type:
            _log.log_unclassified_email(message_id, folder, sender, subject)
            error_id = _log.error(f"Unclassified email: folder={folder}, from={sender}, subject={subject[:100]}")
            if not has_pending_message(message_id):
                add_error(message_id, "", "identify", "UNCLASSIFIED",
                          f"Unclassified email: {subject[:100]}", error_id,
                          account=account, folder=folder, uid=uid,
                          subject=subject, sender=sender, date_hdr=date_hdr,
                          raw_bytes=raw_bytes)
            # 任务 16：未分类不再逐封弹企微，改为记进当日汇总项（每日邮件第三节）；
            # 逐条 add_error / 日志保持不变。
            record_issue(
                "unclassified",
                f"来自 {sender}「{subject[:60]}」",
            )
            self._set_last_outcome({"queued": 1})
            return True

        if not is_type_enabled(email_type):
            _log.log_type_skipped(message_id, email_type, "Type disabled via config")
            self._set_last_outcome({"skipped": 1})
            return True

        _log.log_type_identified(message_id, email_type, f"sender={sender}, subject={subject[:50]}")

        try:
            rows = extract_email(email_type, raw_bytes)
        except Exception as e:
            error_id = _log.error(f"Extract failed: {type(e).__name__}: {e}", error_id=None)
            if not has_pending_message(message_id):
                add_error(message_id, "", "extract", "EXTRACT_EXCEPTION",
                          f"{type(e).__name__}: {e}", error_id,
                          account=account, folder=folder, uid=uid,
                          subject=subject, sender=sender, date_hdr=date_hdr,
                          raw_bytes=raw_bytes)
            increment_counter("extract_failed")
            self._set_last_outcome({"queued": 1})
            return True
        if not rows:
            _log.warning(f"No rows extracted for {message_id} type={email_type}")
            error_id = _log.error(f"Empty extract: type={email_type} subject={subject[:100]}", error_id=None)
            if not has_pending_message(message_id):
                add_error(message_id, "", "extract", "EMPTY_ROWS",
                          f"No rows extracted type={email_type}", error_id,
                          account=account, folder=folder, uid=uid,
                          subject=subject, sender=sender, date_hdr=date_hdr,
                          raw_bytes=raw_bytes)
            self._set_last_outcome({"queued": 1})
            return True

        send_ctx = self._build_send_ctx(email_type, rows, raw_bytes)

        # NDR 增强：把邮件级元数据带入 send_ctx，供 _record_forward 写 forward_log
        # （单目标邮件 send_ctx 为 None，起空 dict 承载 _meta；空 dict 仍为
        # falsy，不影响 execute_action 的 multi 分支判断）
        if send_ctx is None:
            send_ctx = {}
        send_ctx["_meta"] = {
            "account": account, "folder": folder, "uid": uid,
            "sender": sender, "subject": subject, "date_hdr": date_hdr,
        }

        has_manual = False
        # 入池的票还没发出去 ⇒ 本封不能标已读（发送成功后由 flush_batch 补标）
        has_pooled = False
        outcome = {"forwarded": 0, "alarm": 0, "pending": 0, "queued": 0,
                   "manual": 0, "duplicate": 0, "skipped": 0,
                   "pooled": 0}
        for row in rows:
            from mailbots_next.core.rowkey import serve_row_key
            row_key = serve_row_key(row)
            if send_ctx is not None:
                # 单一来源：_record_forward 优先取此键，缺失才自行推导
                send_ctx["_row_key"] = row_key
            try:
                row_outcome = self._process_row(row, row_key, email_type, message_id, raw_bytes,
                                                account, folder, uid, sender, subject, date_hdr,
                                                send_ctx)
                if row_outcome == "manual":
                    has_manual = True
                elif row_outcome == "pooled":
                    # 重复票（is_dup）走的也是 "pooled"（见 _process_routing_result），
                    # 所以这里天然覆盖 —— 它同样还没进任何合并邮件，标已读等于漏处理。
                    has_pooled = True
                outcome[row_outcome] = outcome.get(row_outcome, 0) + 1
            except Exception as e:
                # Pure last-resort guard: one row's unexpected failure must
                # never abort the remaining rows of the same email.
                error_id = _log.error(
                    f"Row failed unexpectedly: row={row.row_idx} "
                    f"error={type(e).__name__}: {e}",
                    error_id=None,
                )
                if not has_pending_error(message_id, row_key):
                    add_error(message_id, row_key, "row", "ROW_EXCEPTION",
                              f"{type(e).__name__}: {e}", error_id,
                              account=account, folder=folder, uid=uid,
                              subject=subject, sender=sender, date_hdr=date_hdr,
                              raw_bytes=raw_bytes)
                increment_counter("row_exception")
                outcome["queued"] += 1
                continue

        # End-of-mail mark-seen: only when live, with a real uid, no manual
        # rows, and no unsettled error-queue entries for this message.
        # Mark-seen is a UX aid only — forward-first ordering is guaranteed
        # because this runs after the whole row loop above.
        if config.is_live() and uid and not has_manual and not has_pooled \
                and not has_pending_message(message_id):
            mark_seen(account, folder, uid)
        # sweep_once 需要行级明细（queued  vs forwarded）来决定 resolve/attempt，
        # 但 Poller 水位线只依赖布尔返回值。两全：返回 True，同时把明细挂在实例上。
        self._set_last_outcome(outcome)
        return True

    def _build_send_ctx(self, email_type, rows, raw_bytes):
        """Pre-resolve per-row companies for multi-target split sending.

        Returns None for single-target mails (legacy whole-mail path, zero
        behavior change) and for tracing (per-company full copies are already
        correct). Otherwise a dict with group keys (company, or
        (company, box) for dsk/atb), the designated sending row per key, the
        row dicts, per-company recipients, and the parsed mail parts.
        """
        from mailbots_next.core.extract import parse_email
        from mailbots_next.core.extractors.dsk import DSKExtractor
        import email as email_mod

        if email_type == EmailType.TRACING or len(rows) <= 1:
            return None
        per_box = (email_type == EmailType.DSK or email_type == EmailType.ATB)
        keys, companies_seen = {}, []
        for row in rows:
            routing = route_row(email_type, row, self.records)
            company = routing.company if getattr(routing, "success", False) else None
            if not company:
                continue
            box = (row.container_no or "") if per_box else ""
            key = (company, box) if per_box else company
            keys[row.row_idx] = key
            if company not in companies_seen:
                companies_seen.append(company)
        uniq_keys = []
        for idx in sorted(keys):
            if keys[idx] not in uniq_keys:
                uniq_keys.append(keys[idx])
        if len(uniq_keys) <= 1:
            return None
        designated = {}
        for idx in sorted(keys):
            designated.setdefault(keys[idx], idx)
        group_rows = {k: [] for k in uniq_keys}
        for row in rows:
            key = keys.get(row.row_idx)
            if key is None:
                continue
            wb_row = (row.raw_data.get("waybill_row") or {}) if isinstance(row.raw_data, dict) else {}
            group_rows[key].append({
                "客户编码": row.customer_code or "",
                "箱号": row.container_no or "",
                "运单号": wb_row.get("运单号", ""),
                "company": key[0] if per_box else key,
            })
        company_routes = {c: get_recipients(c) for c in companies_seen}
        ctx = {
            "email_type": str(email_type),
            "multi": True,
            "keys": keys,
            "designated": designated,
            "group_rows": group_rows,
            "company_routes": company_routes,
            "records": self.records,
            "all_boxes": set(),
            "html_body": None,
            "attachments": [],
            "container_rows": [],
        }
        if per_box:
            msg = email_mod.message_from_bytes(raw_bytes, policy=email_mod.policy.default)
            parsed = parse_email(msg)
            ctx["html_body"] = parsed.get("html_body")
            ctx["attachments"] = parsed.get("attachments", [])
            ctx["container_rows"] = DSKExtractor()._extract_container_table(ctx["html_body"])
            ctx["all_boxes"] = {r.container_no for r in rows if r.container_no}
        return ctx

    def _process_row(self, row, row_key, email_type, message_id, raw_bytes,
                     account, folder, uid, sender, subject, date_hdr, send_ctx=None):
        """Process one extracted row: claim, manual-skip, route, act.

        Returns the row outcome: "duplicate" | "manual" | "forwarded" |
        "alarm" | "pending" | "queued" | "skipped". "queued" means this row
        produced (or kept) an error-queue entry and still needs attention."""
        full_msg_id = f"{message_id}|{row_key}"

        if not try_claim(full_msg_id):
            _log.log_dedup_claim(full_msg_id, False, row_key)
            return "duplicate"

        _log.log_dedup_claim(full_msg_id, True, row_key)

        # Manual categories leave it to humans: skip before routing so
        # they never enter the error queue (留人工，不标已读).
        if getattr(row, "draft_category", None) in NON_AUTO_DRAFT_CATEGORIES:
            _log.info(
                f"Manual skip | msg_id={message_id[:50]} | row={row.row_idx} | "
                f"category={row.draft_category} | left unread for humans"
            )
            return "manual"

        # waybill_rejected 单证审核驳回：业务上不需要通知（渝新欧会直接与同事沟通），
        # 故静默跳过 —— 不通知、不转发、不进错误队列、不记 ERROR。
        # 🔴 仍必须在路由【之前】：路由失败会走错误队列分支，又变回噪音。
        if getattr(row, "waybill_rejected", False):
            _log.info(
                f"waybill_rejected skipped by design (no notify) | msg_id={message_id[:50]} | "
                f"row={row.row_idx} | code={row.customer_code or '-'} | subject={subject[:100]}"
            )
            increment_counter("action_wayb_rejected_skipped")
            return "rejected"

        routing = route_row(email_type, row, self.records)
        outcomes = []
        if isinstance(routing, list):
            for r in routing:
                outcomes.append(self._process_routing_result(r, row, email_type, message_id, row_key, raw_bytes, account, folder, uid, sender, subject, date_hdr, send_ctx))
        else:
            outcomes.append(self._process_routing_result(routing, row, email_type, message_id, row_key, raw_bytes, account, folder, uid, sender, subject, date_hdr, send_ctx))
        # A multi-company fan-out row is only as good as its worst outcome.
        for terminal in ("queued", "forwarded", "alarm", "pending", "skipped"):
            if terminal in outcomes:
                return terminal
        return "queued"

    def _identify_type(self, sender: str, subject: str, raw_bytes: bytes) -> Optional[str]:
        from mailbots_next.config import TYPE_ROUTES, ENC_PDF_RE, BOX_PDF_RE, UPDATE_KEYWORDS
        import re

        sender_lower = sender.lower() if sender else ""
        subject = subject or ""
        # 规范化后的发件人域：只有配置了 sender_domain_whitelist 的规则才用到，
        # 惰性算一次（其它规则不受影响，现有 5 条行为 100% 不变）。
        sender_domain = None

        for route in TYPE_ROUTES:
            # 停用的规则整条跳过（现有 5 条均为 enabled: True，行为不变）
            if not route.get("enabled", True):
                continue

            match = route["match"]

            # 主题必须包含 / 必须不含（仅新规则配置了这两个键，
            # 现有 5 条没有 ⇒ match.get() 恒为 None ⇒ 行为 100% 不变）
            subject_pattern = match.get("subject_pattern")
            if subject_pattern and not re.search(subject_pattern, subject):
                continue

            subject_exclude = match.get("subject_exclude")
            if subject_exclude and re.search(subject_exclude, subject):
                continue

            if "sender_whitelist" in match:
                for wl in match["sender_whitelist"]:
                    if wl.lower() in sender_lower:
                        return route["type"].value
            if "sender_domain_whitelist" in match:
                # 与上面的子串匹配不同：域必须**精确相等**，
                # 否则 notyxologistics.com 之类会误命中。
                if sender_domain is None:
                    sender_domain = normalize_sender_domain(sender)
                whitelist = {
                    (d or "").strip().strip("<>").strip().lower()
                    for d in match["sender_domain_whitelist"]
                }
                if sender_domain and sender_domain in whitelist:
                    return route["type"].value
            if "attachment_pattern" in match:
                msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
                att_names = []
                for part in msg.walk():
                    if part.is_multipart():
                        continue
                    fn = part.get_filename()
                    if fn:
                        att_names.append(fn)
                pattern = match["attachment_pattern"]
                for fn in att_names:
                    if re.search(pattern, fn, re.I):
                        excluded = False
                        for ex in match.get("sender_exclude", []):
                            if ex.lower() in sender_lower:
                                excluded = True
                                break
                        if not excluded:
                            return route["type"].value
        return None

    def _process_routing_result(self, routing, row, email_type, message_id, row_key, raw_bytes, account, folder, uid, sender, subject, date_hdr, send_ctx=None):
        from mailbots_next.core import Decision

        if not routing.success:
            # Tracing zero-hit is a normal business branch (type recognized,
            # just no company owns these boxes): INFO only, no forward, no
            # alarm, no error-queue entry.
            if email_type == EmailType.TRACING and routing.route_source == "no_company_match":
                _log.info(
                    f"Tracing zero hit | msg_id={message_id[:50]} | row={row.row_idx} | "
                    f"train={getattr(row, 'train_no', '')} box={getattr(row, 'container_no', '')} | "
                    "no responsible company, skipped without alert"
                )
                increment_counter("tracing_zero_hit")
                return "skipped"
            _log.log_routing_failed(message_id, row.row_idx, routing.route_source)
            error_id = _log.error(f"Routing failed: {routing.route_source}", error_id=None)
            add_error(message_id, row_key, "routing", "NO_ROUTE", routing.route_source, error_id,
                      account=account, folder=folder, uid=uid,
                      subject=subject, sender=sender, date_hdr=date_hdr,
                      raw_bytes=raw_bytes)
            increment_counter("routing_failed")
            # E2 config-missing: company resolved but no recipients configured —
            # log + queue，并记进当日汇总项（任务 16：不再逐封弹企微）。
            if routing.company:
                record_issue(
                    "config_missing",
                    f"无收件人：公司 {routing.company} 未配置收件人"
                    f"（type={email_type} source={routing.route_source} subject={subject[:60]}）",
                    code="no_recipients",
                )
            return "queued"

        _log.log_routing(message_id, row.row_idx, routing.company, routing.responsible_person or "", routing.to_list, routing.cc_list)

        decision = decide(email_type, row, routing, self.records)
        _log.log_decide(message_id, row.row_idx, decision.tier, decision.action, decision.reason)

        # 阶段三：专列聚合池（任务 11 / spec 03）。入池既不发送、也不标已读 ——
        # 邮件的"已读"改由 aggregate.flush_batch 发送成功后补标，
        # 这样「未读 = 还没发出去」，人工待处理视图才准。
        if decision.action == "pool":
            from mailbots_next.core import aggregate
            added, is_dup = aggregate.pool_add(
                company=routing.company,
                train_no=getattr(row, "train_no", "") or "",
                email_type=email_type,
                message_id=message_id,
                account=account, folder=folder, uid=uid,
                customer_code=row.customer_code or "",
                release_no=row.raw_data.get("release_no") or "",
                code_no=row.raw_data.get("code_no") or "",
                box_no=row.container_no or "",
                note=row.raw_data.get("tail_note") or "",
                validity=row.raw_data.get("validity") or "",
                link=row.raw_data.get("link") or "",
                retbox_doc=row.raw_data.get("retbox_doc") or "",
            )
            if is_dup:
                increment_counter("release_pool_duplicate")
            # 🔴 两种（新增 / 已在池里）都返回 "pooled"，绝不返回 "duplicate"：
            # 后者在 _process_row 里是 dedup claim 命中的意思，混用会污染 outcome
            # 统计；而且重复票同样还没发出去 ⇒ 同样不能标已读（见 process_email）。
            _log.info(
                f"Release pooled | msg={message_id[:50]} | company={routing.company} | "
                f"train={getattr(row, 'train_no', '')} | type={email_type} | "
                f"added={added} dup={is_dup}"
            )
            return "pooled"

        sender_info = None
        if routing.responsible_person:
            sender_info = get_sender_by_email(routing.responsible_person)
        if not sender_info:
            sender_info = (account, self.accounts.get(account, ""))

        sender_email, sender_password = sender_info

        # E2 missing-credentials: no SMTP password for the sender account —
        # never attempt the login, alert the ops owner instead.
        if not sender_password:
            eid = _log.error(
                f"Missing SMTP credentials for sender {sender_email or account}",
                error_id=None,
            )
            add_error(message_id, row_key, "act", "MISSING_CREDENTIALS",
                      f"No SMTP password for {sender_email or account}", eid,
                      account=account, folder=folder, uid=uid,
                      subject=subject, sender=sender, date_hdr=date_hdr,
                      raw_bytes=raw_bytes)
            increment_counter("missing_credentials")
            # 任务 16：不再逐封弹企微，改记进当日汇总项（每日邮件第三节）
            record_issue(
                "config_missing",
                f"无 SMTP 密码：发信账号 {sender_email or account} 缺密码"
                f"（type={email_type} subject={subject[:60]}）",
                code="no_smtp_credentials",
            )
            return "queued"

        success, detail = execute_action(decision, row, routing, raw_bytes, sender_email, sender_password, message_id, send_ctx)

        if success:
            _log.log_action(message_id, row.row_idx, decision.action, detail)
            increment_counter(f"action_{decision.action}")

            # 归档（任务 12）：邮件**已经发出去了**才复制到「放箱令 / 还箱令」夹。
            # 🔴 detail 有两个值表示"已发出"：forwarded（本行单独发了一封）和
            # group-covered（本行被公司组邮件覆盖，组邮件已发出）。只判 forwarded
            # 会让被组合邮件覆盖的票永远不归档、也就永远不删，在收信夹越积越多。
            # 失败只记 ERROR + 计数，**绝不影响主流程**（邮件已经发出去了）。
            target_folder = archive.TARGET_BY_TYPE.get(str(email_type or ""))
            if detail in ("forwarded", "group-covered") and target_folder:
                try:
                    archive.copy_after_forward(
                        account, folder, uid, message_id, target_folder,
                    )
                except Exception as e:
                    _log.error(
                        f"Archive copy failed (mail already sent) | "
                        f"msg={message_id} detail={detail}: {type(e).__name__}: {e}"
                    )
                    increment_counter("archive_copy_failed")

            # Group-covered rows ride on their company's group mail: no
            # extra notify, no extra send.
            if detail == "forwarded" and decision.action == "forward":
                # 任务 16：运踪成功通知不再发给洋（只发负责同事）；其它类型不变。
                # 🔴 只信任 boolean 形参，绝不用空串/None/伪造邮箱顶管理员那一位。
                self.notifier.send_forwarded(
                    routing.responsible_person or "",
                    OPS_OWNER_EMAIL,
                    f"Email forwarded: {subject[:100]}",
                    cc_admin=(email_type != EmailType.TRACING),
                )
            elif decision.action == "alarm":
                self.notifier.send_alarm(
                    routing.responsible_person or "",
                    OPS_OWNER_EMAIL,
                    f"Alarm: {decision.reason} | {subject[:100]}"
                )
            elif decision.action == "pending":
                # 待办仍实时发企微给洋（判据 8），同时记进当日汇总项（邮件第三节）
                detail_text = f"Pending: {decision.reason} | {subject[:100]}"
                self.notifier.send_pending(
                    routing.responsible_person or "",
                    OPS_OWNER_EMAIL,
                    detail_text
                )
                record_issue(
                    "pending",
                    f"{decision.reason} | {email_type} "
                    f"客编{row.customer_code or '-'} 箱{row.container_no or '-'}"
                    f" | {subject[:60]}",
                )
            # NB: outcome vocabulary uses "forwarded" (past) while
            # Decision.action uses "forward" — map explicitly.
            if detail == "group-covered":
                return "forwarded"
            return {"forward": "forwarded", "alarm": "alarm",
                    "pending": "pending"}.get(decision.action, "skipped")
        else:
            error_id = _log.error(f"Action failed: {detail}", error_id=None)
            _log.log_action_failed(message_id, row.row_idx, decision.action, detail, error_id)
            add_error(message_id, row_key, "act", "ACTION_FAILED", detail, error_id,
                      account=account, folder=folder, uid=uid,
                      subject=subject, sender=sender, date_hdr=date_hdr,
                      raw_bytes=raw_bytes)
            increment_counter("action_failed")
            return "queued"


def signal_handler(signum, frame):
    _log.info(f"Received signal {signum}, shutting down...")
    _shutdown_event.set()


# 轮询周期上限：25 分钟，相对常见的 30 分钟服务端空闲超时留 5 分钟余量（洋 2026-09-20 拍板）。
# 必须在两条入口（INGEST_POLL_SEC / --poll-secs）合流之后夹取 —— 放在 settings.py 会被 --poll-secs 绕过。
POLL_SECS_MAX = 1500


def _clamp_poll_secs(secs, source: str) -> int:
    """轮询周期上限夹取：超限 ⇒ WARN + 夹到 POLL_SECS_MAX。

    周期越大，连接空闲越久，越可能被服务端空闲超时断开；而断开只能由"下一条命令失败"
    发现 ⇒ 告警延迟最长一个周期。夹取让这个延迟有上界。
    （不会漏信：重连后会重跑本轮，且水位不推进，邮件仍在。）
    """
    secs = int(secs)
    if secs > POLL_SECS_MAX:
        _log.warning(
            f"poll_secs={secs} exceeds POLL_SECS_MAX={POLL_SECS_MAX} ({source}),"
            f" clamped to {POLL_SECS_MAX}"
        )
        return POLL_SECS_MAX
    return secs


def _build_pollers(processor: MailProcessor, poll_secs: int = 0):
    """组装 12 条 Poller（3 组 × 4 账号），不起线程。start_pollers / poll_once 共用。"""
    from mailbots_next.config import FORWARD_SINCE as _FS, INGEST_POLL_SEC as _DEF
    if not poll_secs or poll_secs <= 0:
        poll_secs = _DEF
    poll_secs = max(1, int(poll_secs))
    pollers = []
    for group_config in IDLE_GROUPS.values():
        folders = list(zip(group_config["folders"], group_config["folder_utf7"]))
        for account in DEFAULT_ACCOUNTS:
            password = processor.accounts.get(account)
            if not password:
                _log.warning(f"No password for account {account}, skipping")
                continue

            def callback(acct, folder, folder_utf7, message_id, uid, subject, sender, date_hdr, raw_bytes):
                return processor.process_email(acct, folder, message_id, uid, subject, sender, date_hdr, raw_bytes)

            pollers.append(Poller(
                account=account,
                password=password,
                folders=folders,
                on_raw=callback,
                state_path="",
                poll_secs=poll_secs,
                forward_since=_FS,
            ))
    return pollers, poll_secs


def start_pollers(processor: MailProcessor, poll_secs: int = 0):
    """Start poll listeners. Topology is fixed: one Poller per group x account
    (3 groups x 4 accounts = 12 connections). Each Poller traverses all folders
    in its group every round (UID watermark discovery)."""
    global _pollers
    built, poll_secs = _build_pollers(processor, poll_secs)
    for poller in built:
        _pollers.append(poller)
        poller.start()

    n_folders = sum(len(g.get("folders", [])) for g in IDLE_GROUPS.values())
    _log.info(f"Started {len(_pollers)} ingest pollers (poll {poll_secs}s)")
    _log.info(f"Ingest mode: poll {poll_secs}s | connections={len(_pollers)} | folders={n_folders} | discovery=uid-watermark")


def poll_once(processor: MailProcessor, poll_secs: int = 0) -> dict:
    """顺序单轮（--once / 部署自检）：12 条 Poller 逐个 run_once，不起线程即退出。"""
    built, poll_secs = _build_pollers(processor, poll_secs)
    for poller in built:
        poller.run_once()
    _log.info(f"Once scan complete | pollers={len(built)}")
    return {"pollers": len(built)}


def stop_pollers():
    global _pollers
    for poller in _pollers:
        poller.stop()
    _pollers.clear()
    _log.info("All ingest pollers stopped")


class InboundHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self):
        if self.path != "/internal/forward":
            self._respond(404, b"Not Found")
            return

        if INBOUND_SHARED_SECRET:
            auth = self.headers.get("X-Forward-Secret", "")
            if auth != INBOUND_SHARED_SECRET:
                self._respond(401, b"Unauthorized")
                return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, b"Invalid JSON")
            return

        error_id = data.get("error_id")
        if not error_id or not isinstance(error_id, int):
            self._respond(400, b"Missing or invalid error_id")
            return

        from mailbots_next.core import claim_manual_forward, reopen_error, get_connection

        # Direct lookup by error_id instead of O(n) iteration.
        # Materialize the Row to a plain dict BEFORE closing: Row field
        # access after conn.close() raises ProgrammingError on some
        # sqlite builds, which would turn every manual forward into a 500.
        conn = get_connection()
        cursor = conn.execute(
            "SELECT * FROM error_queue WHERE id = ?", (error_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            self._respond(404, b"Error record not found or already processed")
            return
        target = dict(row)

        if not claim_manual_forward(error_id):
            self._respond(404, b"Already being processed")
            return

        try:
            processor = MailProcessor(snapshot())
            raw_bytes = bytes.fromhex(target["raw_hex"]) if target["raw_hex"] else b""
            if not raw_bytes:
                raise ValueError("No raw email data stored")

            processor.process_email(
                target["account"] or "",
                target["folder"] or "",
                target["message_id"],
                target["uid"] or 0,
                target["subject"] or "",
                target["sender"] or "",
                target["date"] or "",
                raw_bytes
            )
            self._respond(200, b"Forwarded successfully")
        except Exception as e:
            _log.error(f"Inbound forward failed for error_id={error_id}: {e}")
            reopen_error(error_id)
            self._respond(500, f"Forward failed: {e}".encode())


def start_inbound_server():
    global _inbound_server
    _inbound_server = HTTPServer(("127.0.0.1", INBOUND_PORT), InboundHandler)
    thread = threading.Thread(target=_inbound_server.serve_forever, daemon=True)
    thread.start()
    _log.info(f"Inbound HTTP server started on 127.0.0.1:{INBOUND_PORT}")


def stop_inbound_server():
    global _inbound_server
    if _inbound_server:
        _inbound_server.shutdown()
        _inbound_server = None
        _log.info("Inbound HTTP server stopped")


def sweep_once(processor=None) -> dict:
    """Single sweeper pass over the error queue.

    Each pending entry with a replay payload is really reprocessed through
    the full pipeline. Dedup conflict is resolved by releasing the original
    claim first ((a)): the replay re-claims synchronously in this same
    thread, so a concurrent poll pass can only make our claim fail — i.e. a
    wasted cycle, never a double forward. Dedup stays the single source of
    truth; no bypass flag exists anywhere.
    Success closes the entry (+mark-seen enqueue when no sibling rows are
    still pending); failure increments attempts, escalating to manual + one
    E1 at the cap. Entries without payload can't replay: WARN + counter.
    Returns {"retried": n, "escalated": n}; retried counts real replays.
    """
    from mailbots_next.core.dedup import (
        get_pending_errors, get_manual_errors, increment_attempt,
        mark_error_manual, mark_error_resolved, is_error_notified,
        set_error_notified, release_claim,
    )
    from mailbots_next.config import SWEEP_MAX_RETRY

    if processor is None:
        processor = MailProcessor(snapshot())

    from mailbots_next.core.ingest import fetch_raw_by_uid

    stats = {"retried": 0, "escalated": 0}
    for e in get_pending_errors(100):
        raw_hex = e.get("raw_hex") or ""
        account = e.get("account") or ""
        raw_bytes = bytes.fromhex(raw_hex) if raw_hex else b""
        if not raw_bytes and account and (e.get("uid") or 0):
            # 大邮件重放：无 payload 但 account/uid 俱全 → 从 IMAP 按 UID 重取原文
            raw_bytes = fetch_raw_by_uid(account, e.get("folder") or "", e.get("uid")) or b""
            if not raw_bytes:
                increment_counter("retry_refetch_failed")
        if not raw_bytes or not account:
            _log.warning(
                f"Sweep retry skipped, no replay payload | id={e['id']} "
                f"msg_id={e['message_id'][:50]}"
            )
            increment_counter("retry_no_payload")
            increment_attempt(e["id"])
            if e["attempt_count"] >= SWEEP_MAX_RETRY:
                mark_error_manual(e["id"])
                if not is_error_notified(e["id"]):
                    get_notifier().send_program_error(OPS_OWNER_EMAIL, e["error_id"], f"Max retries reached for {e['message_id']}")
                    set_error_notified(e["id"])
                stats["escalated"] += 1
            else:
                stats["retried"] += 1
            continue

        row_key = e.get("row_key") or ""
        release_claim(f"{e['message_id']}|{row_key}" if row_key else e["message_id"])
        try:
            result = processor.process_email(
                account, e.get("folder") or "", e["message_id"],
                e.get("uid") or 0, e.get("subject") or "",
                e.get("sender") or "", e.get("date") or "",
                raw_bytes,
            )
            # process_email 新契约返回布尔；行级明细挂在线程本地槽上供 sweep 决策。
            # 兼容：布尔 True + 有明细 → 用明细；裸 True（mock）→ 视为成功；dict（旧）→ 直接用。
            if isinstance(result, dict):
                outcome = result
            elif result is True:
                outcome = processor._get_last_outcome() or {"forwarded": 1}
            elif result is False:
                outcome = {"queued": 1}
            else:
                outcome = processor._get_last_outcome() or ({"forwarded": 1} if result else {"queued": 1})
        except Exception as exc:
            _log.error(f"Sweep replay crashed: id={e['id']} error={type(exc).__name__}: {exc}")
            outcome = {"queued": 1}
        stats["retried"] += 1
        if outcome.get("queued", 0) > 0:
            increment_attempt(e["id"])
            if e["attempt_count"] >= SWEEP_MAX_RETRY:
                mark_error_manual(e["id"])
                if not is_error_notified(e["id"]):
                    get_notifier().send_program_error(OPS_OWNER_EMAIL, e["error_id"], f"Max retries reached for {e['message_id']}")
                    set_error_notified(e["id"])
                stats["escalated"] += 1
            continue
        if outcome.get("forwarded", 0) + outcome.get("alarm", 0) \
                + outcome.get("pending", 0) + outcome.get("manual", 0) \
                + outcome.get("skipped", 0) == 0:
            # Nothing was actually reprocessed (e.g. duplicate-claimed by a
            # concurrent pass): leave pending without burning an attempt.
            continue
        mark_error_resolved(e["id"])
        if (e.get("uid") or 0) and not has_pending_message(e["message_id"]):
            enqueue_mark_seen(account, e.get("folder") or "", e["uid"])

    for e in get_manual_errors(100):
        # Manual entries were already E1-alerted at escalation time; the daily
        # digest (not the 5-minute sweeper) carries the ongoing reminder.
        if not is_error_notified(e["id"]):
            get_notifier().send_program_error(OPS_OWNER_EMAIL, e["error_id"], f"Manual intervention needed: {e['message_id']} ({e['stage']})")
            set_error_notified(e["id"])
            stats["escalated"] += 1
    return stats


def _bounce_poll_due(now: float, last_ts: float) -> bool:
    """距上次轮询是否已达 BOUNCE_POLL_SEC。纯函数，便于直测。"""
    return (now - last_ts) >= BOUNCE_POLL_SEC


def run_sweeper():
    from mailbots_next.config import SWEEP_INTERVAL_SEC
    # 发送后自检开关（默认 0）。从 settings 直接取，避免扩大 config/__init__ 的导出面。
    from mailbots_next.config.settings import SEND_CHECK_ENABLED

    global _last_bounce_poll_ts

    while not _shutdown_event.is_set():
        time.sleep(SWEEP_INTERVAL_SEC)
        if _shutdown_event.is_set():
            break

        # Bounce polling runs before sweep to re-queue NDRs in the same cycle.
        # F4: honor BOUNCE_POLL_SEC (default = SWEEP_INTERVAL_SEC, so no
        # behavior change; set larger to reduce load).
        if BOUNCE_MONITOR_ENABLED:
            now = time.monotonic()
            if _bounce_poll_due(now, _last_bounce_poll_ts):
                _last_bounce_poll_ts = now
                try:
                    poll_bounces()
                except Exception as e:
                    _log.error(f"bounce poll tick failed: {e}")

        sweep_once()

        # 发送后自检：与 sweep 同节奏（默认 300s），默认关闭。
        # 函数内 import 避免循环导入；单次异常绝不能把 sweeper 线程打死。
        if SEND_CHECK_ENABLED:
            try:
                from mailbots_next.core.send_check import run_once
                run_once()
            except Exception as e:
                _log.error(f"send_check tick failed: {e}")


def flush_and_notify() -> bool:
    """09:00：冲平当日计数器，并落地成当日 digest 快照。

    任务 16：**不再发企微 digest**（原计数改由 12:30 那封每日邮件第四节承载）。
    `flush_counters()` 仍然要调用 —— 但计数器被清零，所以必须先把这一段落地，
    否则邮件 12:30 读到的是空的。

    返回值语义保留：True = 这一段有计数，False = 空。
    """
    counts = flush_counters()
    write_digest_snapshot(counts)
    if counts:
        _log.info(f"Daily counters flushed to digest snapshot for the 12:30 report: {counts}")
        return True
    return False


def _next_digest_time(now: datetime) -> datetime:
    """Next 09:00 digest run at or after `now`. Plain date arithmetic rolls
    over month ends correctly (replace(day=+1) would explode on 1/31)."""
    from mailbots_next.config import DIGEST_HOUR
    next_run = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run = next_run + timedelta(days=1)
    return next_run


def run_digest():
    while not _shutdown_event.is_set():
        now = datetime.now()
        next_run = _next_digest_time(now)
        wait_secs = (next_run - now).total_seconds()
        if wait_secs > 86400:
            wait_secs = 86400

        for _ in range(int(wait_secs)):
            if _shutdown_event.is_set():
                break
            time.sleep(1)

        if _shutdown_event.is_set():
            break

        flush_and_notify()


# 归档核对删除的每日小时（洋定 12:00，与 09:00 的日报错开）。
ARCHIVE_PURGE_HOUR = 12


def _next_purge_time(now: datetime) -> datetime:
    """下一个 12:00（当天还没到就是今天，否则明天）。照 _next_digest_time 的算法。"""
    next_run = now.replace(hour=ARCHIVE_PURGE_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run = next_run + timedelta(days=1)
    return next_run


def run_archive_purge():
    """每天 12:00 的归档核对删除线程（结构照 run_digest）。

    删除不可逆，所以这条链路有**三道闸**（时间 / 副本 / 新鲜），缺一不删；
    任何异常只记日志，绝不把线程打死（明天还要跑）。
    """
    while not _shutdown_event.is_set():
        now = datetime.now()
        next_run = _next_purge_time(now)
        wait_secs = (next_run - now).total_seconds()
        if wait_secs > 86400:
            wait_secs = 86400

        for _ in range(int(wait_secs)):
            if _shutdown_event.is_set():
                break
            time.sleep(1)

        if _shutdown_event.is_set():
            break

        try:
            stats = archive.purge_verified()
            _log.info(f"Archive purge done: {stats}")
        except Exception as e:
            _log.error(f"Archive purge failed: {type(e).__name__}: {e}")


# 每日一封汇总邮件的发送时点（12:30 —— 12:00 归档核对跑完之后，数据才齐）。
# 常量在 config/settings.py（DAILY_REPORT_HOUR / DAILY_REPORT_MINUTE），此处不重复写死。


def _next_daily_report_time(now: datetime) -> datetime:
    """下一个 12:30（当天还没到就是今天，否则明天）。照 _next_digest_time 的算法。"""
    next_run = now.replace(hour=DAILY_REPORT_HOUR, minute=DAILY_REPORT_MINUTE,
                           second=0, microsecond=0)
    if next_run <= now:
        next_run = next_run + timedelta(days=1)
    return next_run


def run_daily_report():
    """每天 12:30 发**当天唯一那封**汇总邮件（任务 16 / 06_EMAIL-DESIGN.md）。

    「一天一封」由三层保证：① 本线程每天只触发一次；② 发送函数内部有 12:30
    时点闸；③ 发完写当日「已发」标记 —— 旧系统的 8:00 计划任务若还没改时点，
    撞上来也只会被这个标记挡掉，不会出现第二封。
    """
    from mailbots_next.core import daily_report

    while not _shutdown_event.is_set():
        now = datetime.now()
        next_run = _next_daily_report_time(now)
        wait_secs = (next_run - now).total_seconds()
        if wait_secs > 86400:
            wait_secs = 86400

        for _ in range(int(wait_secs)):
            if _shutdown_event.is_set():
                break
            time.sleep(1)

        if _shutdown_event.is_set():
            break

        mode = os.environ.get("MAILBOT_MODE", "test").lower()
        if mode != "live":
            _log.info("Daily report skipped (test mode): 不发真实邮件")
            continue
        try:
            res = daily_report.send_daily_report()
            _log.info(f"Daily report: {res.get('reason')} | {res.get('subject')}")
        except Exception as e:
            # 邮件发不出去绝不能把线程打死（明天还要跑）
            _log.error(f"Daily report failed: {type(e).__name__}: {e}")


def main():
    # Argparse first: MAILBOT_MODE must be set before any runtime is_live()
    # check below can observe it (import-time MODE constants stay frozen).
    parser = argparse.ArgumentParser(description="MailBot Next - Unified Email Pipeline")
    parser.add_argument("--live", action="store_true", help="Run in LIVE mode (default: test)")
    parser.add_argument("--poll-secs", type=int, default=0, help="Poll interval seconds (unique ingest mechanism, default INGEST_POLL_SEC=30)")
    parser.add_argument("--once", action="store_true", help="Single scan and exit")
    parser.add_argument("--errors", choices=["list", "retry", "resolve"], help="Error queue management")
    parser.add_argument("--error-id", type=int, help="Error ID for retry/resolve")
    args = parser.parse_args()

    os.environ["MAILBOT_MODE"] = "live" if args.live else "test"

    mode = os.environ.get("MAILBOT_MODE", "test").lower()
    if mode not in ("test", "live"):
        print("ERROR: MAILBOT_MODE must be 'test' or 'live'")
        sys.exit(1)

    # Live mode guard: FORWARD_SINCE must be set to prevent historical mail forwarding
    if mode == "live":
        from mailbots_next.config import FORWARD_SINCE
        if not FORWARD_SINCE:
            raise SystemExit(
                "Refusing to start in live mode without FORWARD_SINCE: "
                "set FORWARD_SINCE=YYYY-MM-DD (e.g. the go-live date) so historical "
                "unread mail is not forwarded to customers."
            )

    _log.info(f"Starting MailBot Next in {mode.upper()} mode")

    if args.errors:
        from mailbots_next.core.dedup import get_pending_errors, get_manual_errors, mark_error_resolved
        if args.errors == "list":
            pending = get_pending_errors(100)
            manual = get_manual_errors(100)
            print(f"Pending: {len(pending)}, Manual: {len(manual)}")
            for e in pending + manual:
                print(f"  #{e['id']} [{e['status']}] {e['message_id'][:30]} {e['stage']} attempts={e['attempt_count']}")
        elif args.errors == "retry" and args.error_id:
            mark_error_resolved(args.error_id)
            print(f"Marked #{args.error_id} as resolved (retry)")
        elif args.errors == "resolve" and args.error_id:
            mark_error_resolved(args.error_id)
            print(f"Marked #{args.error_id} as resolved")
        return

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    config = snapshot()
    processor = MailProcessor(config)

    # 启动自检（只读）：company 收件人缺失则 ERROR（绝不在启动期写行）
    from mailbots_next.core.store import check_company_recipients_configured
    check_company_recipients_configured()

    # --once：顺序跑一轮就退出（部署自检；不 start 线程、不循环、不起 sweeper/digest）
    if args.once:
        from mailbots_next.config import INGEST_POLL_SEC as _DEF_POLL
        _once_secs = args.poll_secs if (args.poll_secs and args.poll_secs > 0) else _DEF_POLL
        poll_once(processor, poll_secs=max(1, _clamp_poll_secs(_once_secs, "--once")))
        _log.info("Once scan complete, exiting")
        return

    from mailbots_next.core.ingest import get_batcher
    from mailbots_next.config import INGEST_POLL_SEC as _DEF_POLL
    get_batcher().start()

    # --poll-secs 是唯一真值：已解析但从未使用过的参数现在真正接上
    poll_secs = args.poll_secs if (args.poll_secs and args.poll_secs > 0) else _DEF_POLL
    poll_secs = max(1, _clamp_poll_secs(poll_secs, "--poll-secs/INGEST_POLL_SEC"))
    start_pollers(processor, poll_secs=poll_secs)
    start_inbound_server()

    sweeper_thread = threading.Thread(target=run_sweeper, daemon=True)
    sweeper_thread.start()

    digest_thread = threading.Thread(target=run_digest, daemon=True)
    digest_thread.start()

    # 阶段三：专列聚合。建表 + 启动恢复（pending 与崩在 flush 中途的 flushing 都要救），
    # 然后起独立线程按 10 秒节奏扫到期批次。
    from mailbots_next.core import aggregate
    aggregate.init_release_tables()
    # 任务 12：archive_log（与 release_batch 同库 dedup.db）。启动期执行，不可抛异常。
    archive.init_table()
    try:
        _recovery = aggregate.flush_due_on_startup()
        _log.info(f"Release aggregate startup recovery: {_recovery}")
    except Exception as e:
        # 恢复失败不能拦住收信主链路
        _log.error(f"Release aggregate startup recovery failed: {type(e).__name__}: {e}")

    aggregator_thread = threading.Thread(target=aggregate.run_aggregator, daemon=True)
    aggregator_thread.start()

    # 任务 12：每天 12:00 核对副本确实存在后，才删源邮件（当天复制、次日再删）
    purge_thread = threading.Thread(target=run_archive_purge, daemon=True)
    purge_thread.start()

    # 任务 16：每天 12:30 发唯一那封汇总邮件（12:00 归档线程之后才起算，数据才齐）
    daily_report_thread = threading.Thread(target=run_daily_report, daemon=True)
    daily_report_thread.start()

    try:
        while not _shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    _log.info("Shutting down...")
    stop_pollers()
    stop_inbound_server()
    from mailbots_next.core.ingest import get_batcher
    get_batcher().stop()
    _log.info("Shutdown complete")


if __name__ == "__main__":
    main()