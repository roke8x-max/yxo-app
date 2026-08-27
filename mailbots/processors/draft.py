# -*- coding: utf-8 -*-
"""草单处理器（spec §5.2/§5.4 分类/动作矩阵落地，处理序九步逐字实现）：
1 can_handle（文件夹门+运单号白名单让位）→ 2 剥回复前缀 → 3 噪音清单前置
→ 4 草单件判定（无发件人门槛）→ 5 提取客编/箱号 → 6 A/B 台账（遇到即写）
→ 7 classify_match 八档动作（T1 转发 / T2·T3·T6·T7 待办 / T0·T4·T5 报警）
→ 8 C1 非 T1 只记录、C2/内部回复链只记录 → 9 返回 ProcessResult。

ctx 注入契约（serve 层供给；测试全 fake）：
    conn_events / idx / accounts / live / smtp / resolve(code, box, train_id="")
    / send(msg, from, pwd, to, cc) / add_pending(info, raw, test, simulated)
    / alarm(names, reason, text) / notify(name, text)
dedup 的 claim/release 由 serve 层统一负责：本处理器不触碰 dedup，
仅在「发送全拒」时通过可选 ctx.release(message_id) 钩子请求释放（下轮重试）。"""
import email
import os
import re

from core import events_store
from core import identity
from core import matching
from core.models import ProcessResult
from processors.forward_builder import (
    CATEGORY_LABEL, build_forward, decode_any, test_subject)

# 过渡期兼容旧夹具名（IMAP 原始 UTF-7 名 &j9BTVYNJU1U- = 「草单运单号」）
FOLDERS = {"草单运单号", "&j9BTVYNJU1U-", "运单草单"}
ADMIN_MAILBOX = "maoxiaoyang@cqtransit.com"
ADMIN_NAME = "毛骁洋"
WAYBILL_KEYWORD = "运单号"  # 白名单件归 waybill 处理器（spec §5.1 W 归属）

NOISE_RE = re.compile(r"出区放行|报关单|INVOICE|clipboard|货协运单", re.I)

# 提取正则自备（core/matching 只有 ISO 形态判定，无搜索式正则）
CODE_TOKEN_RE = re.compile(r"[A-Za-z]+\d+(?:-[0-9A-Za-z]+)*")
CONTAINER_RE = re.compile(r"[A-Z]{4}\d{7}(?![A-Za-z0-9])")
ENC_PDF_RE = re.compile(r"^[A-Z]{4}\d{7}-\d{6}-\d{6}.*\.pdf$", re.I)
BOX_PDF_RE = re.compile(r"^[A-Z]{4}\d{7}\.pdf$", re.I)
UPDATE_KEYWORDS = ("更新草单", "草单更新", "最新的草单", "请查收更新")

PENDING_REASON = {
    "T2": "客编数字段命中但完整客编不同（库里可能到站字母未更新）: 邮件={code}",
    "T3": "客编数字段命中但箱号与库内记录不一致，请人工核对",
    "T6": "箱号命中多行记录（可能箱子复用, 需人工挑）",
    "T7": "客编数字段命中多条不同后缀记录，请人工挑",
}


def strip_reply_prefix(subject):
    """循环剥离 Re:/回复:/答复:/【草单更新】直至稳定，得 clean_subject。"""
    out = (subject or "").strip()
    while True:
        if out.lower().startswith("re:"):
            out = out[3:].strip()
        elif out.startswith("回复:"):
            out = out[len("回复:"):].strip()
        elif out.startswith("答复:"):
            out = out[len("答复:"):].strip()
        elif out.startswith("【草单更新】"):
            out = out[len("【草单更新】"):].strip()
        else:
            return out


def attachment_names(msg):
    names = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn:
            names.append(decode_any(fn))
    return names


def extract_body_text(msg, limit=2000):
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
        text = payload.decode(part.get_content_charset() or "utf-8",
                              errors="replace")
        if ct == "text/plain" and plain is None:
            plain = text
        elif ct == "text/html" and html is None:
            html = text
    return (plain or (re.sub(r"<[^>]+>", " ", html) if html else ""))[:limit]


def is_draft_attachment(att_names, body):
    """判据 1: 已加密命名；判据 2(兜底): 箱号.pdf + 正文更新关键词。"""
    for fn in att_names:
        if ENC_PDF_RE.match(fn.strip()):
            return True
    for fn in att_names:
        if BOX_PDF_RE.match(fn.strip()) and any(k in body for k in UPDATE_KEYWORDS):
            return True
    return False


class DraftProcessor:
    """Processor 协议实现；一切外发通道经 ctx 注入，LIVE 门控由 ctx.live 表达。"""

    def __init__(self, ctx):
        self.ctx = ctx

    # ---- 协议入口 ----

    def can_handle(self, event):
        if event.folder not in FOLDERS:
            return False
        return WAYBILL_KEYWORD not in decode_any(event.subject)

    def process(self, event):
        ctx = self.ctx
        subj = decode_any(event.subject)
        if not self.can_handle(event):
            return ProcessResult(event=event, action="skip",
                                 detail="not_draft_folder")
        raw = self._load_raw(event)
        msg = email.message_from_bytes(raw)
        att_names = attachment_names(msg)
        body = extract_body_text(msg)

        # 2 剥回复前缀
        clean = strip_reply_prefix(subj)
        was_reply = bool(subj.strip()) and clean != subj.strip()

        # 3 噪音清单前置：附件名或主题命中 → 只记录不转发
        noise_hay = clean + "\n" + "\n".join(att_names)
        if NOISE_RE.search(noise_hay):
            return self._record(event, detail="noise")

        # 4 草单件判定（无发件人门槛）；非草单件按域名分流（§5.4）
        domain = event.sender.split("@")[-1].lower() if "@" in event.sender else ""
        if is_draft_attachment(att_names, body):
            category = None  # 待第 6 步 A/B 台账裁决
        elif "运单号" in subj:
            category = "W"
        elif domain == "yxologistics.com":
            category = "C1"  # 内部反馈：走档位流程，但非 T1 只记录（见下）
        else:
            category = "C2"

        # 5 提取：clean_subject 先 CODE_RE 全匹配 → 按 is_client_code_candidate 过滤
        code = None
        for token in CODE_TOKEN_RE.findall(clean):
            if matching.is_client_code_candidate(token, ctx.idx):
                code = token
                break
        box = self._extract_box(clean, att_names)
        seq = (matching.parse_code(code) or {}).get("seq", "") if code else ""

        # 6 A/B 台账「遇到即写」（无 seq 不写，防空串行污染台账）
        if seq:
            is_new = events_store.seen_seq_add(ctx.conn_events, seq,
                                               event.message_id)
        else:
            is_new = True
        if category is None:
            category = "A" if is_new else "B"

        # 7 匹配 + 八档动作
        mr = matching.classify_match(code, box, ctx.idx)
        company = self._company_of(mr)

        # C2/W 类：直接记录（不转发、不待办、不报警）
        if category in ("C2", "W"):
            return self._record(event, tier=mr.tier, detail=mr.reason or category)
        if category == "C1" and mr.tier != "T1":
            # §5.4：内部反馈非精准匹配 → 只记录（不进待办不报警，防刷台账/误发客户）
            return self._record(event, tier=mr.tier,
                                detail="C1_not_routable:" + mr.tier)
        if mr.tier == "T1":
            return self._forward(event, mr, category, company, subj, raw,
                                 code, box)
        if mr.tier in ("T2", "T3", "T6", "T7"):
            return self._pending(event, mr, category, company, subj, raw,
                                 code, box, seq)
        # T0/T4/T5 → 报警（不进待办）
        return self._alarm(event, mr, subj, code, box)

    # ---- 动作分支 ----

    def _forward(self, event, mr, category, company, subj, raw, code, box):
        ctx = self.ctx
        targets, reason = ctx.resolve(code, box, train_id="")
        if not targets:
            # T1 完全命中但无路由配置 → 沿旧语义入待确认
            txt = "记录已完全命中, 但该公司没有配置转发路由"
            if reason:
                txt += "（{}）".format(reason)
            info = self._pending_info(event, category, company, subj,
                                      code, "", box, (), ())
            ctx.add_pending(info, raw or None)
            return ProcessResult(event=event, action="pending", tier=mr.tier,
                                 detail=txt)
        owner = identity.real_name_of(company) or ADMIN_NAME
        sender_email = self._sender_email(company)
        sender_pwd = (ctx.accounts or {}).get(sender_email, "")
        delivered_all, refused_all, any_ok = [], [], False
        for t in targets:
            fwd, fwd_subject = build_forward(raw, category)
            orig_to = ",".join(list(t.to) + list(t.cc)) or "(待确认/无路由)"
            if not ctx.live:
                # TEST 干跑：主题必须自设（A/C 类 build_forward 不设主题）
                fwd["Subject"] = test_subject(
                    fwd_subject, orig_to, CATEGORY_LABEL.get(category, ""))
            elif category != "B":
                # LIVE：B 类已由 build_forward 内聚加前缀，其余用原始主题
                fwd["Subject"] = fwd_subject
            res = ctx.send(fwd, sender_email, sender_pwd,
                           list(t.to), list(t.cc))
            delivered_all.extend(res.delivered or ())
            refused_all.extend(res.refused or ())
            any_ok = any_ok or bool(res.ok)
            if res.ok and ctx.live:
                self._forward_log(owner, company, code, box, fwd_subject,
                                  t, sender_email)
        if not any_ok and not delivered_all:
            # 全拒/发送失败：请求 serve 层释放 claim，下轮重试
            release = getattr(ctx, "release", None)
            if callable(release):
                release(event.message_id)
            return ProcessResult(
                event=event, action="skip", tier=mr.tier, route=tuple(targets),
                detail="send_failed_all_refused:"
                       + (",".join(refused_all) or "unknown"))
        if refused_all:
            # 部分拒收：报警但仍 mark/save/notify
            ctx.alarm(self._alarm_names(owner), "smtp_failed",
                      "{}\n部分收件被拒: {}".format(subj, ",".join(refused_all)))
        self._save_eml(event, raw)
        ctx.notify(owner, "[{}]\n{}\n客编:{} 箱号:{}\n已转发至: {}".format(
            CATEGORY_LABEL.get(category, "转草单"),
            "测试预览" if not ctx.live else "已转发",
            subj[:200], code or "-", box or "-",
            ",".join(delivered_all) or "(测试干跑)"))
        detail = "sent:" + (",".join(delivered_all) or "dry_run")
        if refused_all:
            detail = "partial:" + ",".join(refused_all) + ";" + detail
        return ProcessResult(event=event, action="forward", tier=mr.tier,
                             route=tuple(targets), detail=detail)

    def _pending(self, event, mr, category, company, subj, raw, code, box, seq):
        reason = PENDING_REASON.get(mr.tier, "未匹配（{}）".format(mr.reason))
        if mr.tier == "T2":
            reason = reason.format(code=code or "-")
        info = self._pending_info(event, category, company, subj,
                                  code, seq, box, (), ())
        info["reason"] = reason
        info["candidates"] = [dict(c) for c in mr.candidates]
        self.ctx.add_pending(info, raw or None)
        return ProcessResult(event=event, action="pending", tier=mr.tier,
                             detail=reason)

    def _alarm(self, event, mr, subj, code, box):
        owner = identity.real_name_of(self._company_of(mr)) or ADMIN_NAME
        self.ctx.alarm(self._alarm_names(owner), mr.tier,
                       "{}\n客编:{} 箱号:{}".format(subj, code or "-", box or "-"))
        return ProcessResult(event=event, action="alarm", tier=mr.tier,
                             detail=mr.reason or "")

    def _record(self, event, tier=None, detail=""):
        return ProcessResult(event=event, action="record", tier=tier,
                             detail=detail)

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

    @staticmethod
    def _extract_box(clean, att_names):
        m = CONTAINER_RE.search(clean + " ")
        if m:
            return m.group(0)
        for fn in att_names:
            m = CONTAINER_RE.search(fn + " ")
            if m:
                return m.group(0)
        return ""

    @staticmethod
    def _company_of(mr):
        if mr.record and mr.record.get("company"):
            return mr.record["company"]
        for c in mr.candidates:
            if c.get("company"):
                return c["company"]
        return ""

    @staticmethod
    def _alarm_names(owner):
        names = []
        for n in (owner, ADMIN_NAME):
            if n and n not in names:
                names.append(n)
        return names

    def _sender_email(self, company):
        accounts = self.ctx.accounts or {}
        mapped, _name = identity.sender_for(company)
        if mapped and mapped in accounts:
            return mapped
        return ADMIN_MAILBOX

    def _pending_info(self, event, category, company, subj,
                      code, seq, box, to, cc):
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
            "owner": identity.real_name_of(company) or ADMIN_NAME,
            "boxes_seen": [event.account],
            "to": tuple(to),
            "cc": tuple(cc),
        }

    def _save_eml(self, event, raw):
        repo = getattr(self.ctx, "eml_repo", "")
        if repo and raw:
            try:
                events_store.save_eml(repo, event.account, event.message_id,
                                      raw)
            except Exception:
                pass

    def _forward_log(self, owner, company, code, box, subject, target,
                     sender_email):
        try:
            import forward_log
            forward_log.record(robot="草单", owner=owner, company=company,
                               code=code, box=box, subject=subject,
                               to_list=list(target.to), sender=sender_email)
        except Exception:
            pass
