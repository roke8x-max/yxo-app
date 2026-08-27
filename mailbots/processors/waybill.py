# -*- coding: utf-8 -*-
"""运单号处理器（spec §5 WAY_A 行级拆分 / WAY_B 忽略 / 留底表）：
- can_handle：白名单（默认 docwbfb@yxologistics.com，cfg 可覆）且（folder∈运单类 或 有 .xls 附件）
- 处理序：
  1. 主题含「单证审核驳回」→ ignored（audit 日志一行，零通知零待办——2026-08-26 拍板 WAY_B 不做任何处理）
  2. WAY_A：xlsio.parse_waybill_xls → 行级独立处理（拍板：报警行不阻断其他行）：
     - 每行 classify_match：T1→resolve→分组键(to,cc)；T2/T3/T6/T7→pending_rows；T4/T5/T0→alarm_rows（notify_alarm 即时逐行，含 owner 若识别）
     - 退舱行→T4(cancelled_only) alarm（统一规则）
     - 分组发送：rewrite_xls_filtered 生成该公司小 xls（失败保守附原始）→ build_forward(raw,"WAY_A") 重组
       → identity.sender_for(company) 发信 → ledger_insert_waybill（识别即留底 pending）
       → 成功 ledger_mark_waybill_sent + forward_log.record(note="WAY_A 拆分转发(N行)")
     - 无公司/无路由/发送失败行→unresolved→pending（info 带 rows 子集）
  3. 全部行 resolved 且发送成功→mark seen；存在 unresolved→pending 队列（复用 add_pending, category="WAY_A"）

ctx 注入契约（serve 层供给；测试全 fake）：
    conn_events / idx / accounts / live / smtp / resolve(code, box, train_id="")
    / send(msg, from, pwd, to, cc) / add_pending(info, raw, test, simulated)
    / alarm(names, reason, text) / notify(name, text)
    / ledger_insert_waybill / ledger_mark_waybill_sent
dedup 的 claim/release 由 serve 层统一负责：本处理器不触碰 dedup，
仅在「发送全拒」时通过可选 ctx.release(message_id) 钩子请求释放（下轮重试）。
"""
import email
import logging
import os
import re
from collections import defaultdict
from email.mime.multipart import MIMEMultipart

from core import events_store as es
from core import identity
from core import matching
from core.models import ProcessResult
from processors import xlsio
from processors.forward_builder import (
    CATEGORY_LABEL, build_forward, decode_any, test_subject)

_log = logging.getLogger(__name__)

# 运单号文件夹名（IMAP modified-UTF7 与中文名均兼容）
WAYBILL_FOLDERS = {"运单号", "&j9BTVVP3-"}
# 发件人白名单（可由配置覆盖）
DEFAULT_WHITELIST = {"docwbfb@yxologistics.com"}
# 运单号正则（用于附件名/主题判断）
XLS_RE = re.compile(r"\.xlsx?$", re.I)
# 退舱关键词
CANCELLED_STATUS = "退舱"
# WAY_B 关键词
WAY_B_KEYWORD = "单证审核驳回"

# 管理员兜底
ADMIN_MAILBOX = "maoxiaoyang@cqtransit.com"
ADMIN_NAME = "毛骁洋"


def _attachment_names(msg):
    names = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn:
            names.append(decode_any(fn))
    return names


def _has_xls_attachment(msg):
    """判断是否有 .xls/.xlsx 附件（按文件名正则）"""
    for name in _attachment_names(msg):
        if XLS_RE.search(name):
            return True
    return False


def _get_first_xls_attachment(msg):
    """提取第一个 .xls/.xlsx 附件的原始字节与文件名"""
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn and XLS_RE.search(fn):
            payload = part.get_payload(decode=True)
            if payload:
                return payload, decode_any(fn)
    return None, None


def _extract_body_text(msg, limit=2000):
    plain, html = None, None
    for part in msg.walk():
        if part.is_multipart():
            continue
        ct = part.get_content_type()
        disp = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in disp:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ct == "text/plain" and plain is None:
            plain = text
        elif ct == "text/html" and html is None:
            html = text
    return (plain or (re.sub(r"<[^>]+>", " ", html) if html else ""))[:limit]


def _company_of(mr):
    if mr.record and mr.record.get("company"):
        return mr.record["company"]
    for c in mr.candidates:
        if c.get("company"):
            return c["company"]
    return ""


def _alarm_names(owner):
    names = []
    for n in (owner, ADMIN_NAME):
        if n and n not in names:
            names.append(n)
    return names


class WaybillProcessor:
    """Processor 协议实现；一切外发通道经 ctx 注入，LIVE 门控由 ctx.live 表达。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.whitelist = set(ctx.get("whitelist", DEFAULT_WHITELIST)) if hasattr(ctx, "get") else DEFAULT_WHITELIST

    # ---- 协议入口 ----

    def can_handle(self, event):
        """白名单发件人 + (运单号文件夹 或 有 .xls 附件)"""
        sender = (event.sender or "").lower()
        if sender not in self.whitelist:
            return False
        if event.folder in WAYBILL_FOLDERS:
            return True
        # 无法直接从 event 判断附件，serve 层应在 folder 非运单号时仅投递有 xls 的邮件
        # 这里兜底：若 subject 含运单号/箱号特征则放行（保守）
        subj = decode_any(event.subject)
        if "运单号" in subj or re.search(r"[A-Z]{4}\d{7}", subj):
            return True
        return False

    def process(self, event):
        ctx = self.ctx
        subj = decode_any(event.subject)
        sender = (event.sender or "").lower()

        if not self.can_handle(event):
            return ProcessResult(event=event, action="skip", detail="not_waybill")

        # 1. WAY_B：主题含「单证审核驳回」→ ignored（仅 audit，零通知零待办）
        if WAY_B_KEYWORD in subj:
            _log.info(f"[WAY_B ignored] msg_id={event.message_id} subject={subj[:80]}")
            return ProcessResult(event=event, action="ignored", detail="waybill_rejected")

        # 2. 加载原始邮件
        raw = self._load_raw(event)
        if not raw:
            return ProcessResult(event=event, action="ignored", detail="no_raw")
        msg = email.message_from_bytes(raw)

        # 3. 提取 xls 附件
        xls_bytes, xls_name = _get_first_xls_attachment(msg)
        if not xls_bytes:
            # 无 xls 附件：容错忽略（spec: W04_no_xls → ignored）
            return ProcessResult(event=event, action="ignored", detail="no_xls_attachment")

        # 4. 解析 xls 行
        rows = xlsio.parse_waybill_xls(xls_bytes)
        if not rows:
            return ProcessResult(event=event, action="ignored", detail="xls_empty")

        # 5. 行级独立处理
        return self._process_rows(event, raw, msg, subj, rows, xls_bytes, xls_name)

    # ---- 行级处理 ----

    def _process_rows(self, event, raw, msg, subj, rows, xls_bytes, xls_name):
        ctx = self.ctx
        idx = ctx.idx

        # 分类收集
        forward_groups = defaultdict(list)    # key=(to_tuple, cc_tuple) -> {company, owner, rows[]}
        pending_rows = []                     # [(row, tier, reason, company, owner)]
        alarm_rows = []                       # [(row, tier, reason, owner)]
        unresolved_rows = []                  # [(row, reason)]  # 无公司/无路由/发送失败
        cancelled_rows = []                   # 退舱行单独记录

        for row in rows:
            code = (row.get("客户编码") or "").strip()
            box = (row.get("箱号") or "").strip()
            waybill = (row.get("运单号") or "").strip()

            # classify_match
            mr = matching.classify_match(code, box, idx)
            company = _company_of(mr)
            owner = identity.real_name_of(company) if company else None

            # 退舱行特殊处理：按统一规则 T4(cancelled_only) alarm
            rec = mr.record
            if rec and rec.get("status") == CANCELLED_STATUS:
                cancelled_rows.append((row, mr.tier, mr.reason, owner))
                continue

            if mr.tier == "T1":
                # 精准匹配：resolve 路由
                targets, reason = ctx.resolve(code, box, train_id="")
                if not targets:
                    # T1 但无路由配置 → unresolved → pending
                    unresolved_rows.append((row, f"no_route:{reason or 'unknown'}"))
                    continue
                # 分组键
                for t in targets:
                    gkey = (tuple(t.to), tuple(t.cc))
                    forward_groups[gkey].append({
                        "company": company,
                        "owner": owner or ADMIN_NAME,
                        "target": t,
                        "row": row,
                        "code": code,
                        "box": box,
                        "waybill": waybill,
                    })
            elif mr.tier in ("T2", "T3", "T6", "T7"):
                # 模糊/多解 → pending
                reason = WAYBILL_PENDING_REASON.get(mr.tier, "未匹配（{}）".format(mr.reason))
                if mr.tier == "T2":
                    reason = reason.format(code=code or "-")
                pending_rows.append((row, mr.tier, reason, company, owner))
            else:
                # T0/T4/T5 → alarm（逐行即时 notify_alarm）
                alarm_rows.append((row, mr.tier, mr.reason, owner))

        # 6. 处理 alarm_rows（逐行 notify_alarm，不阻断）
        for row, tier, reason, owner in alarm_rows:
            names = _alarm_names(owner)
            ctx.alarm(names, tier,
                      "{}\n客编:{} 箱号:{} 运单号:{}".format(
                          subj, row.get("客户编码", "-"), row.get("箱号", "-"), row.get("运单号", "-")))

        # 7. 处理 cancelled_rows（退舱行 alarm）
        for row, tier, reason, owner in cancelled_rows:
            names = _alarm_names(owner)
            ctx.alarm(names, "T4",
                      "{} (退舱)\n客编:{} 箱号:{} 运单号:{}".format(
                          subj, row.get("客户编码", "-"), row.get("箱号", "-"), row.get("运单号", "-")))

        # 8. 处理 pending_rows（入待办队列）
        for row, tier, reason, company, owner in pending_rows:
            info = self._pending_info(event, "WAY_A", company, subj,
                                      row.get("客户编码", ""), "", row.get("箱号", ""),
                                      (), (), owner or ADMIN_NAME, reason)
            info["candidates"] = [dict(c) for c in mr.candidates] if 'mr' in dir() else []
            ctx.add_pending(info, raw, test=not ctx.live, simulated=not ctx.live)

        # 9. 分组发送（每组生成小 xls + 发信）
        sent_any = False
        for gkey, group in forward_groups.items():
            to_list, cc_list = gkey
            company = group[0]["company"]
            owner = group[0]["owner"]
            target = group[0]["target"]
            group_rows = [g["row"] for g in group]

            # 无外部收件人（内部公司）：仅企微通知，不发邮件
            if not to_list and not cc_list:
                # 留底表：识别即留底（每行一条）
                msg_id = event.message_id
                for g in group:
                    r = g["row"]
                    ctx.ledger_insert_waybill(
                        r.get("客户编码", ""), r.get("箱号", ""), r.get("运单号", ""),
                        "", "", g["company"], msg_id)
                # 标记 ledger sent（内部公司视为已处理）
                ctx.ledger_mark_waybill_sent(msg_id)
                # 企微通知负责人
                codes_str = " / ".join(g["code"] for g in group)
                ctx.notify(owner,
                           f"✅ 运单号已按公司拆分自动转发（{len(group)}行，内部公司仅通知）\n"
                           f"主题: {subj[:60]}\n"
                           f"客编: {codes_str}")
                sent_any = True
                continue

            # 生成小 xls
            small_bytes, small_name = xlsio.rewrite_xls_filtered(xls_bytes, xls_name, group_rows)
            if small_bytes is None:
                # 重写失败：保守附原始 xls
                small_bytes, small_name = xls_bytes, xls_name
                _log.warning(f"[WAY_A] rewrite_xls_filtered 失败，附原始 xls: {company}")

            # 留底表：识别即留底（每行一条）
            msg_id = event.message_id
            for g in group:
                r = g["row"]
                ctx.ledger_insert_waybill(
                    r.get("客户编码", ""), r.get("箱号", ""), r.get("运单号", ""),
                    "", "", g["company"], msg_id)

            # 构造转发邮件
            fwd, fwd_subject = build_forward(raw, "WAY_A")
            # 替换附件为小 xls
            fwd = self._replace_xls_attachment(fwd, small_bytes, small_name)
            orig_to = ",".join(list(to_list) + list(cc_list)) or "(待确认/无路由)"
            if not ctx.live:
                fwd["Subject"] = test_subject(fwd_subject, orig_to, CATEGORY_LABEL.get("WAY_A", ""))
            else:
                fwd["Subject"] = fwd_subject

            # 发送身份
            sender_email, sender_name = identity.sender_for(company)
            sender_pwd = (ctx.accounts or {}).get(sender_email, "")
            if not sender_email or sender_email not in (ctx.accounts or {}):
                sender_email = ADMIN_MAILBOX
                sender_pwd = (ctx.accounts or {}).get(sender_email, "")

            res = ctx.send(fwd, sender_email, sender_pwd, list(to_list), list(cc_list))
            if res.ok:
                sent_any = True
                # 标记 ledger sent
                ctx.ledger_mark_waybill_sent(msg_id)
                # forward_log 记录
                codes_str = " / ".join(g["code"] for g in group)
                boxes_str = " / ".join(g["box"] for g in group)
                try:
                    import forward_log
                    forward_log.record(robot="运单号", owner=owner, company=company,
                                       code=codes_str, box=boxes_str, subject=subj,
                                       to_list=list(to_list), sender=sender_email,
                                       note=f"WAY_A 拆分转发({len(group)}行)")
                except Exception:
                    pass
                # 企微通知负责人
                ctx.notify(owner,
                           f"✅ 运单号已按公司拆分自动转发（{len(group)}行）\n"
                           f"主题: {subj[:60]}\n"
                           f"客编: {codes_str}\n"
                           f"已发往: {', '.join(to_list)}")
            else:
                # 发送失败 → 该组行 unresolved
                for g in group:
                    unresolved_rows.append((g["row"], f"send_failed:{res.error or ','.join(res.refused)}"))

        # 10. 处理 unresolved_rows（入待办）
        if unresolved_rows:
            info = self._pending_info(event, "WAY_A", "", subj,
                                      "", "", "",
                                      (), (), ADMIN_NAME, "部分行无法自动转发，需人工处理")
            info["rows"] = [{"客户编码": r.get("客户编码", ""),
                             "箱号": r.get("箱号", ""),
                             "运单号": r.get("运单号", ""),
                             "reason": reason} for r, reason in unresolved_rows]
            ctx.add_pending(info, raw, test=not ctx.live, simulated=not ctx.live)

        # 11. 确定最终 action
        has_forward = sent_any or bool(forward_groups)
        has_pending = bool(pending_rows) or bool(unresolved_rows)
        has_alarm = bool(alarm_rows) or bool(cancelled_rows)

        if has_forward and not has_pending:
            # 全部行 resolved 且发送成功
            return ProcessResult(event=event, action="forward", tier="T1",
                                 detail=f"split_forward:{len(forward_groups)}组")
        elif has_pending:
            return ProcessResult(event=event, action="pending", tier="T1",
                                 detail=f"unresolved:{len(unresolved_rows)} pending:{len(pending_rows)}")
        elif has_alarm:
            return ProcessResult(event=event, action="alarm", tier="T4",
                                 detail=f"alarms:{len(alarm_rows)+len(cancelled_rows)}")
        else:
            return ProcessResult(event=event, action="record", tier="T1",
                                 detail="no_actionable_rows")

    # ---- 辅助 ----

    def _load_raw(self, event):
        p = getattr(event, "eml_path", "") or ""
        if p and os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    return f.read()
            except Exception:
                pass
        return b""

    def _replace_xls_attachment(self, fwd_msg, new_bytes, new_name):
        """替换转发邮件中的 xls 附件为新的小 xls"""
        # 移除现有 xls 附件
        new_parts = []
        for part in fwd_msg.walk():
            if part.is_multipart():
                continue
            fn = part.get_filename()
            if fn and XLS_RE.search(fn):
                continue  # 跳过旧 xls
            new_parts.append(part)

        # 重建 multipart
        if isinstance(fwd_msg, MIMEMultipart):
            # 保留非 xls 部分
            payload = fwd_msg.get_payload()
            if isinstance(payload, list):
                new_payload = [p for p in payload if not (p.get_filename() and XLS_RE.search(p.get_filename() or ""))]
                fwd_msg.set_payload(new_payload)
        else:
            # 非 multipart，直接用新的
            pass

        # 添加新 xls 附件
        from email.mime.base import MIMEBase
        from email import encoders
        np = MIMEBase("application", "octet-stream")
        np.set_payload(new_bytes)
        encoders.encode_base64(np)
        np.add_header("Content-Disposition", "attachment", filename=("utf-8", "", new_name))
        fwd_msg.attach(np)
        return fwd_msg

    def _pending_info(self, event, category, company, subj,
                      code, seq, box, to, cc, owner, reason):
        return {
            "message_id": event.message_id,
            "subject": subj,
            "sender": event.sender,
            "date": event.date_hdr,
            "category": category,
            "code": code or "",
            "num": seq or "",
            "box": box or "",
            "company": company or "",
            "owner": owner or ADMIN_NAME,
            "reason": reason,
            "candidates": [],
            "boxes_seen": [event.account],
            "to": tuple(to),
            "cc": tuple(cc),
        }


# 本地 PENDING_REASON（避免污染全局 matching 模块）
WAYBILL_PENDING_REASON = {
    "T2": "客编数字段命中但完整客编不同（库里可能到站字母未更新）: 邮件={code}",
    "T3": "客编数字段命中但箱号与库内记录不一致，请人工核对",
    "T6": "箱号命中多行记录（可能箱子复用, 需人工挑）",
    "T7": "客编数字段命中多条不同后缀记录，请人工挑",
}