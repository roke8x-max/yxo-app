# NDR / 退信监控 增强 spec（OpenCode 执行版）— v2

> 背景：2026-09-10 生产事故 `CQWLJT260912004-D` 静默丢单。根因诊断指出三处旧系统缺陷，新系统 `mailbots_next` 已用 `act.py` 返回值校验 + `serve.py`「有错误就不标已读」+ error_queue 重试/告警 堵住了「异常/部分拒收」类失败；但**「服务器返回空 refused 的纯静默丢弃」**这一盲区新老系统都检测不到——`act.py:127` 的 `if refused:` 对空字典 `{}` 无效。根治只能靠 **NDR（退信）监控**：主动收 NDR、解析失败、把对应的已「成功」转发重新拉回 error_queue 重试/告警。本 spec 即为此增强。
>
> 范围：**仅代码改造**，不涉及生产库灌数据/补发（洋已另行安排小叽处理）。遵循四铁律：①不瞎改已做好内容 ②不自 commit/push ③改完跑 pytest 保持全绿（当前 135 passed）④如实报告不替拍板。
>
> **v2 修订（采纳 ds 反馈）**：① `forward_id` 改为**纯 hex 安全令牌**（原 `message_id|row_idx|uuid` 含 `<>`/`@` 非法字符，会破坏 VERP 本地名与正则匹配，严重级）②告警统一走 `get_notifier().send_program_error`（原 spec 误写 `dedup.send_program_error`，该函数不存在）③补充 `forward_log` 清理债（长期运行会膨胀）④明确 VERP `+` 标签不支持时的退化配置 ⑤bounce 轮询置于 sweep 之前。
>
> **v3 修订（采纳 ds 第二轮反馈 + 实测校正）**：①**陷阱1 真实 bug 已修**——原 §3.1 把父级 `bounce_addr` 透传给 `_forward_split`，但子邮件各自生成子 forward_id 写 forward_log/头，导致 envelope 与 forward_log 的 forward_id 错位、NDR 漏检「拆分后子邮件丢失」；修正为 `_forward_split` 内每子邮件独立生成 `sub_fid` 并贯穿 头/envelope/log，父 forward_id 仅单目标路径使用。②**陷阱2 实测为伪告警**——`add_error`(dedup.py:205-219) 与 `error_queue` 表(:40-61) 已扩展支持全部回放字段，无需前置扩展；但新增对 `serve.py:572`「raw_hex 或 account 为空则跳过重放」闸门的复核要求。③补充**实测发现的第三处缺陷**：`execute_action`/`_forward_split` 作用域无 `account/folder/uid/sender/subject/date_hdr`，原 spec 直接引用会 `NameError`；改为由 `serve.py::process_email` 注入 `send_ctx["_meta"]`，`_record_forward` 从中读取。

---

## 0. 目标与验收红线

**目标**：补上「空 refused 静默丢」这一结构性盲区——任何被 SMTP 服务器静默丢弃的转发，在 NDR 到达后能被检测到、重新入 error_queue 重试、超限升级人工、并告警，杜绝「记成功 + 标已读 + 永不复发 + 无告警」四连。

**验收红线**：
- `pytest mailbots_next/tests` 全绿，总数 ≥ 135（新增测试计入）。
- 新增单测：构造合成 NDR（multipart/report），`bounce.parse_ndr()` 能正确析出 `forward_id`（**hex 安全令牌**）+ `diagnostic` + `failed_recipient`。
- 新增集成测：预置一条 `forward_log` + 合成 NDR → 调用 `poll_bounces()` → 断言 `error_queue` 出现 `error_type='BOUNCE'` 条目、`forward_log.bounced=1`、且触达告警（mock `get_notifier().send_program_error`）。
- 不改动既有 `act.py` 的 `refused` 校验逻辑。
- 测试/生产开关：`BOUNCE_MONITOR_ENABLED=False` 时 `poll_bounces()` 直接返回、不发任何 IMAP 连接。

---

## 1. 设计总览

```
转发成功时（execute_action / _forward_split）            NDR 到达时（poll_bounces，周期触发）
─────────────────────────────────────                  ─────────────────────────────────────
生成 forward_id = make_forward_id(message_id, row_key)   # 纯 hex 安全令牌
   ↓
build_forward_message 写入头  X-YXO-Forward-Id: <forward_id>
send_smtp 用 envelope MAIL FROM = _bounce_addr(forward_id)
   （VERP: bounce+<forward_id>@cqtransit.com，纯 hex 本地名，RFC 5321 合规）
   ↓ 成功
写 forward_log(forward_id, message_id, row_key, account, folder, uid,
               raw_hex, to_list, cc_list, sent_at, bounced=0)
                                                      ↓
                                          IMAP 连 bounce 邮箱取 UNSEEN
                                                       ↓
                                          parse_ndr() → forward_id(hex) + diagnostic
                                                       ↓
                                          forward_log 查 forward_id
                                             ├─ 命中且 bounced=0：
                                             │    add_error(..., error_type='BOUNCE', raw_bytes=forward_log.raw_hex)
                                             │    forward_log.bounced=1
                                             │    get_notifier().send_program_error(...)   # 告警
                                             ├─ 命中且 bounced=1：跳过（防 NDR 重投风暴）
                                             └─ 未命中：log + get_notifier().send_program_error（人工排查）
                                                      ↓
                                          sweep_once 按既有逻辑重放该 error_queue 条目
                                          （重新 extract→route→forward，复用 raw_hex）
```

**关键设计决策**：
1. **forward_id 用「sha256(message_id|row_key) 前16位 - uuid8」，纯 hex + 短横线**：① 可安全进入邮件地址本地名（RFC 5321 `atext`/`dot-atom` 合规，无 `<>`/`@`/`|` 等非法字符）；② NDR 的 `To:` 即 `bounce+<hex>@cqtransit.com`，`parse_ndr` 正则能干净匹配；③ 业务关联（message_id/row_key）另存 `forward_log` 两列，令牌本身无需携带明文。
2. **envelope MAIL FROM 走 VERP**（默认 `bounce+<forward_id>@cqtransit.com`），NDR 直接投递到该地址，本地名即 forward_id，无需解析原始头即可关联。同时 `X-YXO-Forward-Id` 头作为兜底（部分 MTA 改写 envelope 时）。
3. **`From:` 头仍用同事邮箱**（fengqian@ 等）不变——只改 envelope MAIL FROM，不影响收件方展示。
4. **bounce 失败并入 error_queue**：复用 `sweep_once` 现有重放（raw_hex 满载）、`SWEEP_MAX_RETRY=3`、超限 `mark_error_manual` + 每日汇总。
5. **`forward_log` 存 raw_hex**：bounce 重放需原始入站邮件，成功转发不进 error_queue（那里才有 raw_hex），故在此单独留底。

---

## 2. 配置（mailbots_next/config/settings.py）

在 `FORWARD_SINCE`（约 line 61）之后新增：

```python
# ── NDR / 退信监控（2026-09-10 增强，堵「空 refused 静默丢」盲区）──
# 默认关闭；上线前由运维确认 bounce 邮箱 + VERP 授权后开启。
BOUNCE_MONITOR_ENABLED = os.environ.get("BOUNCE_MONITOR_ENABLED", "0") in ("1", "true", "True")
# 退信监控专用邮箱（接收 NDR）。建议与同事发件账号同域、单独邮箱。
# 同时作为 VERP 基址：VERP 本地名 = "bounce+" + forward_id，域名取自本地址 @ 之后。
BOUNCE_ADDRESS = os.environ.get("BOUNCE_ADDRESS", "mailbots-bounce@cqtransit.com")
# 是否启用 VERP（+ 标签）。阿里企业邮实测不支持 + 标签时设 "0"，
# 此时 envelope MAIL FROM 直接用 BOUNCE_ADDRESS，关联仅靠 X-YXO-Forward-Id 头。
BOUNCE_USE_VERP = os.environ.get("BOUNCE_USE_VERP", "1") in ("1", "true", "True")
BOUNCE_IMAP_SERVER = os.environ.get("BOUNCE_IMAP_SERVER", IMAP_SERVER)
BOUNCE_IMAP_PORT = int(os.environ.get("BOUNCE_IMAP_PORT", str(IMAP_PORT)))
BOUNCE_IMAP_USER = os.environ.get("BOUNCE_IMAP_USER", "")
BOUNCE_IMAP_PASSWORD = os.environ.get("BOUNCE_IMAP_PASSWORD", "")
# 读 NDR 的邮箱文件夹（多数 NDR 落在 INBOX）
BOUNCE_FOLDER = os.environ.get("BOUNCE_FOLDER", "INBOX")
# 轮询周期，复用 sweep 节奏；置于 sweep 之前，让 NDR 尽快入队
BOUNCE_POLL_SEC = int(os.environ.get("BOUNCE_POLL_SEC", str(SWEEP_INTERVAL_SEC)))
```

**不在本 spec 改动**：`SMTP_SERVER/PORT`、`IMAP_SERVER/PORT`、`SWEEP_INTERVAL_SEC`、`SWEEP_MAX_RETRY`、`FORWARD_SINCE` 全部沿用。

---

## 3. 代码改动清单

### 3.1 `core/act.py` — 出站信打标 + 可指定 envelope from + 安全 forward_id

**顶部新增 import**：`import hashlib, uuid`（若尚未导入）。

**新增 helper**（放在 `send_smtp` 附近）：

```python
def make_forward_id(message_id: str, row_key: str) -> str:
    """生成 VERP 安全的 forward_id：纯 hex + 短横线，可安全进入邮件地址本地名。
    message_id/row_key 的业务关联另存 forward_log，故令牌本身无需携带明文。"""
    digest = hashlib.sha256(f"{message_id}|{row_key}".encode("utf-8")).hexdigest()[:16]
    return f"{digest}-{uuid.uuid4().hex[:8]}"


def _bounce_addr(forward_id: str) -> str:
    """返回 envelope MAIL FROM。
    VERP 开启：bounce+<forward_id>@<BOUNCE_ADDRESS 域名>（forward_id 为纯 hex，RFC 合规）。
    VERP 关闭：直接返回 BOUNCE_ADDRESS（关联仅靠 X-YXO-Forward-Id 头）。"""
    if settings.BOUNCE_USE_VERP:
        domain = settings.BOUNCE_ADDRESS.partition("@")[2]
        return f"bounce+{forward_id}@{domain}"
    return settings.BOUNCE_ADDRESS
```

**`build_forward_message`（line 37-106）**：在 `msg["Subject"] = ...`（line 53）之后加：

```python
    if forward_id:                              # ← 新增
        msg["X-YXO-Forward-Id"] = forward_id    # ← 新增：NDR 关联兜底（head 值可含任意字符，安全）
```

**`send_smtp`（line 109-132）**：新增 `mail_from` 形参（默认 None=用 sender_email）：

```python
def send_smtp(msg, sender_email, sender_password, to_list, cc_list=None,
              mail_from: Optional[str] = None):                # ← 新增
    cc_list = cc_list or []
    all_recipients = list(set(to_list + cc_list))
    msg["From"] = sender_email
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    env_from = mail_from or sender_email                        # ← 新增：NDR VERP
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.login(sender_email, sender_password)
            refused = server.sendmail(env_from, all_recipients, msg.as_string())  # ← 改 env_from
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        return {"success": True, "refused": {}}
    except Exception as e:
        _log.error(f"SMTP send failed: {e}")
        return {"success": False, "error": str(e)}
```

**`execute_action`（line 407-461）**：单目标路径在 `result = send_smtp(...)`（line 451）前生成 forward_id 并透传；成功后写 forward_log：

```python
    if decision.action != "forward":
        return False, f"Unknown action: {decision.action}"

    # 测试模式不发送、不写库（沿用既有 gate）
    if not config.is_live():
        _log.info(f"TEST MODE: skip actual send/write for {message_id}")
        return (True, "test-mode-skipped")

    original_msg = email.message_from_bytes(original_raw, policy=email.policy.default)
    try:
        if routing.success:
            to_list = routing.to_list
            cc_list = routing.cc_list
        else:
            return False, "No valid recipients"

        # 多目标拆分：子 forward_id / envelope from / forward_log 全部由各子邮件
        # 在 _forward_split 内部独立生成（见下方 §3.1 _forward_split 修正）。
        # 父级 forward_id 不传入、不参与——否则 envelope 用父 id、forward_log 用
        # 子 id，NDR 回来对不上 → 漏检「拆分后子邮件丢失」（采纳 ds 反馈，真实 bug）。
        if send_ctx and send_ctx.get("multi"):
            return _forward_split(decision, row, routing, original_msg,
                                  sender_email, sender_password,
                                  message_id, send_ctx, original_raw)

        # 单目标路径：同一 forward_id 贯穿 头 / envelope(VERP) / forward_log 三处。
        forward_id = make_forward_id(message_id, str(row.row_idx))   # ← 纯 hex 令牌
        msg = build_forward_message(original_msg, to_list, cc_list, sender_email,
                                    forward_id=forward_id)           # ← 写 X-YXO-Forward-Id 头
        result = send_smtp(msg, sender_email, sender_password, to_list, cc_list,
                           mail_from=_bounce_addr(forward_id))       # ← envelope VERP
        if result["success"]:
            _record_forward(forward_id, message_id, str(row.row_idx), send_ctx,
                            to_list, cc_list, original_raw)           # ← 写 forward_log
            if row.email_type in ("dsk", "atb") and row.container_no:
                write_dsk_timestamp(...)
            return True, "forwarded"
        return False, f"SMTP failed: {result.get('error')}"
```

> **新增 helper `_record_forward`（act.py 内）**：成功转发后写 `forward_log`，仅 `config.is_live()` 时落库。邮件级元数据（account/folder/uid/sender/subject/date_hdr）从 `send_ctx["_meta"]` 读取（由 `serve.py::process_email` 注入，见 §3.5）——**不得**在 `execute_action` 作用域里直接引用这些变量（它们不是其形参，会 `NameError`）。act.py 顶部 import（line 19）追加 `write_forward_log`：`from .store import write_dsk_timestamp, write_tracing_log, write_tracing_snapshot, write_forward_log`；并新增 `from ..config import settings`（供 `_bounce_addr` 读 `settings.BOUNCE_USE_VERP` / `BOUNCE_ADDRESS`）。

```python
def _record_forward(forward_id, message_id, row_key, send_ctx, to_list, cc_list, original_raw):
    """成功转发后写 forward_log（仅 is_live 落库）。元数据取自 send_ctx['_meta']。"""
    if not config.is_live():
        return
    meta = (send_ctx or {}).get("_meta", {}) or {}
    from .store import write_forward_log
    write_forward_log(
        forward_id, message_id, row_key,
        meta.get("account", ""), meta.get("folder", ""),
        int(meta.get("uid", 0) or 0),
        meta.get("subject", ""), meta.get("sender", ""), meta.get("date_hdr", ""),
        original_raw, to_list, cc_list,
    )
```

**`_forward_split`（line 347-404）关键修正（采纳 ds 反馈）**：

> ⚠️ **陷阱 1（ds 指出，已验证为真实 bug）**：原 §3.1 让 `execute_action` 把父级 `bounce_addr=_bounce_addr(forward_id)` 透传给 `_forward_split`，但 `_forward_split` 内部每个子邮件又各自 `make_forward_id` 写 `forward_log` + `X-YXO-Forward-Id` 头。结果 **envelope MAIL FROM 用父 forward_id、forward_log/头用子 forward_id**，NDR 回来按 envelope 取父 id → `forward_log` 查不到 → 落入 "unknown forward_id" → 恰好漏掉「拆分后子邮件丢失」这一最致命场景。
>
> **修正**：`_forward_split` **不接收任何外部 bounce_addr**；每个子邮件在内部独立生成 `sub_fid = make_forward_id(message_id, str(key))`（`key` 即 waybill/draft 的 `company` 或 dsk/atb 的 `(company, box)`，uuid 段已保证唯一），并把它同时用于：① `X-YXO-Forward-Id` 头、② `send_smtp(..., mail_from=_bounce_addr(sub_fid))` 的 envelope、③ `_record_forward(sub_fid, ...)` 写 `forward_log`。父级 `forward_id` 仅单目标路径使用。

具体改动（替换 `_forward_split` 现有 `try` 内的发送逻辑）：

```python
def _forward_split(decision, row, routing, original_msg, sender_email,
                   sender_password, message_id, send_ctx, original_raw) -> Tuple[bool, str]:
    """Per-company/per-box fan-out for multi-target mails (same contract as before)."""
    email_type = str(send_ctx.get("email_type", ""))
    key = send_ctx.get("keys", {}).get(row.row_idx)
    if key is None or row.row_idx != send_ctx.get("designated", {}).get(key):
        _log.info(f"Group-covered, skip duplicate send | msg_id={message_id[:50]} | row={row.row_idx}")
        return True, "group-covered"

    sub_fid = make_forward_id(message_id, str(key))   # ← 子 forward_id（贯穿 头/envelope/log）

    try:
        if email_type in ("waybill", "draft"):
            company = key
            parts = split_waybill_by_company(
                original_msg, send_ctx["group_rows"][key], send_ctx["company_routes"])
            part = next((p for p in parts if p.get("company") == company), None)
            if part is None:
                return False, f"No split part for company {company}"
            part["msg"]["X-YXO-Forward-Id"] = sub_fid                # ← 写头（split 不走 build_forward_message）
            result = send_smtp(part["msg"], sender_email, sender_password,
                               part["to"], part["cc"],
                               mail_from=_bounce_addr(sub_fid))      # ← envelope 用 sub_fid
            if result["success"]:
                _record_forward(sub_fid, message_id, str(row.row_idx), send_ctx,
                                part["to"], part["cc"], original_raw)
                return True, "forwarded"
            return False, f"SMTP failed: {result.get('error')}"
        elif email_type in ("dsk", "atb"):
            company, box = key
            cropped, matched = split_dsk_by_box(
                original_msg, send_ctx["html_body"], send_ctx["attachments"],
                send_ctx["container_rows"], box)
            unrelated = [a for a in send_ctx["attachments"]
                         if not any(b and b.upper() in (a.get("filename") or "").upper()
                                    for b in send_ctx["all_boxes"])]
            to_list, cc_list = send_ctx["company_routes"][company]
            if email_type == "dsk":
                code = _box_customer_code(send_ctx.get("records"), box)
                subject = build_dsk_subject(
                    decode_header_safe(original_msg.get("Subject", "")), box, code)
            else:
                subject = decode_header_safe(original_msg.get("Subject", ""))
            msg = build_forward_message(original_msg, to_list, cc_list, sender_email,
                                        html_override=cropped,
                                        attachments_override=matched + unrelated,
                                        forward_id=sub_fid)          # ← 写 X-YXO-Forward-Id 头
            msg.replace_header("Subject", subject)
            result = send_smtp(msg, sender_email, sender_password, to_list, cc_list,
                               mail_from=_bounce_addr(sub_fid))       # ← envelope 用 sub_fid
            if result["success"]:
                if row.container_no:
                    write_dsk_timestamp(row.container_no, email_type.upper(),
                                        datetime.now().strftime("%m/%d %H:%M"))
                _record_forward(sub_fid, message_id, str(row.row_idx), send_ctx,
                                to_list, cc_list, original_raw)
                return True, "forwarded"
            return False, f"SMTP failed: {result.get('error')}"
        else:
            return False, f"Unsupported split type: {email_type}"
    except Exception as e:
        _log.error(f"Split forward failed for {message_id}: {e}")
        return False, str(e)
```

### 3.2 新增 `core/bounce.py` — NDR 解析 + 轮询

```python
"""NDR / 退信监控：收 NDR、解析失败、把对应转发重新拉回 error_queue。"""
import imaplib, email, re
from email.parser import BytesParser
from mailbots_next.core import dedup
from mailbots_next.core.store import write_forward_log, get_forward_log, mark_forward_bounced
from mailbots_next.core.notify import get_notifier          # ← 告警统一入口（唯一正确来源）
from mailbots_next.config import settings
from mailbots_next.core.log import get_logger
_log = get_logger(__name__)


def parse_ndr(raw: bytes) -> dict:
    """解析一封 NDR，返回 {'forward_id': str|None, 'action': str,
    'diagnostic': str, 'failed_recipient': str, 'raw_headers': dict}。
    关联键优先级：VERP 本地名(bounce+<hex>@) > 原始信 X-YXO-Forward-Id 头。"""
    msg = BytesParser().parsebytes(raw)
    out = {"forward_id": None, "action": "", "diagnostic": "", "failed_recipient": "", "raw_headers": {}}
    # 1) VERP：NDR 的 To 即 envelope 收件人 bounce+<hex>@（forward_id 为纯 hex，正则干净匹配）
    to_hdr = msg.get("To", "")
    m = re.search(r"bounce\+([0-9a-fA-F\-]+)@", to_hdr)
    if m:
        out["forward_id"] = m.group(1)
    # 2) 兜底：原始信头里的 X-YXO-Forward-Id（multipart/report 的 returned message 部分）
    for part in msg.walk():
        cid = part.get("X-YXO-Forward-Id")
        if cid:
            out["forward_id"] = out["forward_id"] or cid
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            payload = part.get_payload(decode=True) or b""
            txt = payload.decode("utf-8", "replace")
            for line in txt.splitlines():
                if line.lower().startswith("action:"):
                    out["action"] = line.split(":", 1)[1].strip()
                elif line.lower().startswith("diagnostic-code:"):
                    out["diagnostic"] = line.split(":", 1)[1].strip()
                elif line.lower().startswith("final-recipient:"):
                    out["failed_recipient"] = line.split(";", 1)[-1].strip()
    return out


def poll_bounces() -> dict:
    if not settings.BOUNCE_MONITOR_ENABLED:
        return {"skipped": True}
    if not (settings.BOUNCE_IMAP_USER and settings.BOUNCE_IMAP_PASSWORD):
        _log.warning("Bounce monitor enabled but IMAP creds missing; skipping")
        return {"skipped": True}
    stats = {"processed": 0, "requeued": 0, "unknown": 0}
    try:
        conn = imaplib.IMAP4_SSL(settings.BOUNCE_IMAP_SERVER, settings.BOUNCE_IMAP_PORT, timeout=60)
        conn.login(settings.BOUNCE_IMAP_USER, settings.BOUNCE_IMAP_PASSWORD)
        conn.select(settings.BOUNCE_FOLDER)
        typ, data = conn.search(None, "UNSEEN")
        for num in (data[0].split() if data and data[0] else []):
            _, raw = conn.fetch(num, "(RFC822)")
            ndr = parse_ndr(raw[0][1])
            stats["processed"] += 1
            if not ndr["forward_id"]:
                _log.warning(f"NDR with no forward_id: {ndr['diagnostic']}")
                get_notifier().send_program_error(
                    settings.OPS_OWNER_EMAIL, "0",
                    f"Unmatched NDR (no forward_id): {ndr['diagnostic']}")   # ← get_notifier，非 dedup
                stats["unknown"] += 1
                conn.store(num, "+FLAGS", "\\Seen")
                continue
            rec = get_forward_log(ndr["forward_id"])
            if rec and not rec["bounced"]:
                dedup.add_error(
                    rec["message_id"], rec["row_key"], "act", "BOUNCE",
                    f"NDR bounce: {ndr['diagnostic']} -> {ndr['failed_recipient']}",
                    f"bounce-{ndr['forward_id']}",
                    account=rec["account"], folder=rec["folder"], uid=rec["uid"],
                    subject=rec["subject"], sender=rec["sender"], date_hdr=rec["date_hdr"],
                    raw_bytes=bytes.fromhex(rec["raw_hex"]),
                )
                mark_forward_bounced(ndr["forward_id"])
                get_notifier().send_program_error(
                    settings.OPS_OWNER_EMAIL, f"bounce-{ndr['forward_id']}",
                    f"Forward bounced, re-queued for retry: {ndr['forward_id']} ({ndr['diagnostic']})")
                stats["requeued"] += 1
            elif rec and rec["bounced"]:
                _log.info(f"NDR already handled for {ndr['forward_id']}; skip")
            else:
                _log.warning(f"NDR forward_id not in forward_log: {ndr['forward_id']}")
                get_notifier().send_program_error(
                    settings.OPS_OWNER_EMAIL, "0",
                    f"NDR for unknown forward_id {ndr['forward_id']}: {ndr['diagnostic']}")
                stats["unknown"] += 1
            conn.store(num, "+FLAGS", "\\Seen")
        conn.logout()
    except Exception as e:
        _log.error(f"Bounce poll failed: {e}")
    return stats
```

> **`add_error` / `error_queue` 已支持全部回放字段（ds 第二轮反馈澄清，无需扩展）**：ds 担心 `add_error` 只有 6 个位置参数、`error_queue` 无 `raw_bytes/account/folder/uid` 列 → 调用会 `TypeError`。**实测代码（dedup.py:205-219 与 :40-61）已扩展**：`add_error` 已含 `account/folder/uid/subject/sender/date_hdr/raw_bytes` 全部可选 kwarg（默认空/0），`error_queue` 表已有 `raw_hex/account/folder/subject/sender/date/extra/uid` 列。故本 §3.2 的 `add_error(...)` 调用**直接可用，无需任何前置扩展**。
>
> **但须验证 sweep_once 重放闸门（ds「额外提醒」成立之处）**：`serve.py:570-572` 的 `if not raw_hex or not account:` 会**跳过**任何 `raw_hex` 或 `account` 为空的条目（burn attempt 后转 manual，永不重放）。BOUNCE 重入队要能被重放，必须保证 `forward_log` 的 `raw_hex` 与 `account` 均非空——而这依赖 §3.1 的 `_record_forward` 确有写入（→ 依赖 §3.5 的 `_meta` 注入）以及 §3.3 的 `write_forward_log` 确实存了 `raw_bytes`/`account`。**§8 复核点须含：「构造 NDR → poll_bounces → 断言 error_queue 条目 raw_hex 与 account 均非空，且 sweep_once 能重放成功」**。

> **告警入口铁律**：统一走 `from mailbots_next.core.notify import get_notifier; get_notifier().send_program_error(admin, error_id, detail)`（与 `serve.py:582/607/625`、`extractors/draft.py:107`、`extractors/dsk.py:76` 完全一致）。**`dedup.send_program_error` 不存在，严禁调用。**

### 3.3 `core/store.py` — forward_log 表 + 读写函数

复用 dedup.db（与 error_queue 同库，见 README:217）。新增：

```python
def init_forward_log():
    with _db_lock:
        conn = _connect()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS forward_log (
                forward_id TEXT PRIMARY KEY,
                message_id TEXT, row_key TEXT,
                account TEXT, folder TEXT, uid INTEGER,
                subject TEXT, sender TEXT, date_hdr TEXT,
                raw_hex TEXT, to_list TEXT, cc_list TEXT,
                sent_at TEXT, bounced INTEGER DEFAULT 0)""")
            conn.commit()
        finally:
            conn.close()

def write_forward_log(forward_id, message_id, row_key, account, folder, uid,
                      subject, sender, date_hdr, raw_bytes, to_list, cc_list):
    import json
    if not config.is_live():
        return
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO forward_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),0)",
                (forward_id, message_id, row_key, account, folder, uid, subject, sender,
                 date_hdr, raw_bytes.hex() if raw_bytes else "",
                 json.dumps(to_list, ensure_ascii=False), json.dumps(cc_list, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()

def get_forward_log(forward_id):
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM forward_log WHERE forward_id=?", (forward_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def mark_forward_bounced(forward_id):
    with _db_lock:
        conn = _connect()
        try:
            conn.execute("UPDATE forward_log SET bounced=1 WHERE forward_id=?", (forward_id,))
            conn.commit()
        finally:
            conn.close()
```

并在模块 **import 时**（`init_bot_config_db()` 附近）调用 `init_forward_log()`，确保表随库初始化自动建好（与 `init_bot_config_db` 同机制，避开「简版表固化」雷）。

### 3.4 `serve.py` — 接入周期轮询

定位驱动 `sweep_once` 的调度点（搜 `sweep_once(` 调用 / `threading.Timer` / 主循环）。**在 `sweep_once()` 之前**追加 `poll_bounces()`（让 NDR 当周期尽快入队，下一节拍即被 sweep 重放；放之后最多延迟一个周期，亦可接受，但前置更优）：

```python
# 在 sweep 调度循环内，sweep_once() 之前：
if settings.BOUNCE_MONITOR_ENABLED:
    try:
        poll_bounces()
    except Exception as e:
        _log.error(f"bounce poll tick failed: {e}")
sweep_once()   # 既有逻辑不动
```

> 频率由 `BOUNCE_POLL_SEC`（默认 = `SWEEP_INTERVAL_SEC`）控制；不宜快于 sweep（否则 bounce 重投但 sweep 尚未重放会重复入队——`bounced=1` 防护已兜底）。

---

### 3.5 `serve.py::process_email` 元数据注入（前向依赖，必做）

`execute_action` / `_forward_split` 形参里**没有** `account/folder/uid/sender/subject/date_hdr`（这些只在 `process_email` 作用域）。`_record_forward` 写 `forward_log` 需要它们，故在 `process_email` 内 `_build_send_ctx` 返回后、进入 row 循环前，把元数据注入 `send_ctx["_meta"]`：

```python
        send_ctx = self._build_send_ctx(email_type, rows, raw_bytes)
        # NDR 增强：把邮件级元数据带入 send_ctx，供 _record_forward 写 forward_log
        send_ctx["_meta"] = {
            "account": account, "folder": folder, "uid": uid,
            "sender": sender, "subject": subject, "date_hdr": date_hdr,
        }
```

> 不通过改 `execute_action` 签名传参（调用点仅 serve.py:347 一处，但 `send_ctx` 已是邮件级上下文，注入更内聚，且避免 `__init__` 之外的改动面）。

---

## 4. 测试（mailbots_next/tests/）

1. **`test_act_forward_id.py`（新）**
   - `make_forward_id("msg-1", "0")` → 断言结果匹配 `^[0-9a-f]{16}-[0-9a-f]{8}$`（纯 hex，可入地址）。
   - `build_forward_message(..., forward_id="a1b2c3d4e5f6a7b8-c0ffee12")` → 断言 `msg["X-YXO-Forward-Id"] == "a1b2c3d4e5f6a7b8-c0ffee12"`。
   - mock `smtplib.SMTP_SSL`，调用 `send_smtp(..., mail_from="bounce+a1b2c3d4e5f6a7b8-c0ffee12@cqtransit.com")` → 断言 `server.sendmail` 第一参数为该 envelope from（非 sender_email）。
   - `_bounce_addr` 单测：VERP 开 → `bounce+<id>@cqtransit.com`；VERP 关（`BOUNCE_USE_VERP=False`）→ 返回 `BOUNCE_ADDRESS` 原值。

2. **`test_bounce_parse.py`（新）**
   - 用 `email` 手工拼 `multipart/report; report-type=delivery-status`：
     - 外层 `To: bounce+a1b2c3d4e5f6a7b8-c0ffee12@cqtransit.com`
     - `message/delivery-status` 含 `Action: failed` / `Diagnostic-Code: smtp; 550 5.1.1` / `Final-Recipient: rfc822; bad@x.com`
     - 嵌套原始信含 `X-YXO-Forward-Id: a1b2c3d4e5f6a7b8-c0ffee12`
   - 断言 `parse_ndr` 返回 `forward_id='a1b2c3d4e5f6a7b8-c0ffee12'`、`action='failed'`、`diagnostic` 含 `550`、`failed_recipient` 含 `bad@x.com`。
   - **对照用例（VERP 缺失兜底）**：NDR 外层 `To` 无 `bounce+` 前缀、仅嵌套信含 `X-YXO-Forward-Id` → 仍能析出 forward_id。
   - **反例（v1 旧令牌不可恢复）**：用旧式 `bounce+<CABc123@mail.gmail.com>|0|deadbeef@...` 应**匹配不到**合法 forward_id（验证 hex 收紧正则的必要性）。

3. **`test_bounce_requeue.py`（新，集成）**
   - 预置 `forward_log` 一条（`forward_id='a1b2c3d4e5f6a7b8-c0ffee12'`、`bounced=0`、`raw_hex` 为合法入站邮件 hex）。
   - mock `poll_bounces` 的 IMAP 返回上述合成 NDR；mock `get_notifier().send_program_error`。
   - 调用 `poll_bounces()` → 断言：
     - `get_forward_log(id)["bounced"] == 1`
     - `dedup.get_pending_errors()` 含 `error_type='BOUNCE'` 且 `raw_hex` 非空（可被 sweep 重放）
     - `get_notifier().send_program_error` 被调用一次
   - 再投同一 NDR（bounced 已=1）→ 断言**不再**新增 error_queue 条目（防风暴）。

4. **开关测试**：`BOUNCE_MONITOR_ENABLED=0` 时 `poll_bounces()` 返回 `{"skipped": True}` 且不建立任何 IMAP 连接（mock `imaplib.IMAP4_SSL` 确认未被调用）。

---

## 5. 上线前置 / 运维须知（非代码，列此供洋/小叽确认）

1. **bounce 邮箱必须存在且能收 NDR**：NDR 投递到 envelope MAIL FROM。需确认 `mailbots-bounce@cqtransit.com`（或 VERP `bounce+*@cqtransit.com`）在阿里企业邮已开通、且**能接收外域退信**（部分服务商默认把 NDR 当垃圾或丢弃）。
2. **🔴 VERP `+` 标签实测（上线硬前置）**：上线前从生产环境发一封测试信，`envelope MAIL FROM = bounce+test@cqtransit.com`，人为制造退信，确认 NDR 真的投到 `bounce+test@cqtransit.com`。
   - **支持** → 保持 `BOUNCE_USE_VERP=1` 默认。
   - **不支持** → 设 `BOUNCE_USE_VERP=0` 且 `BOUNCE_ADDRESS=mailbots-bounce@cqtransit.com`，关联仅靠 `X-YXO-Forward-Id` 头（仍可靠，因为头是我们自己写入的）。
3. **凭据**：`BOUNCE_IMAP_USER/PASSWORD` 走环境变量/机密，不入库、不进 git。
4. **开启时机**：`BOUNCE_MONITOR_ENABLED=1` 在周一切换当天、且与 §2 灌 `bot_config.db` 数据**同一批**操作；先 dry-run 观察 NDR 是否真进 bounce 邮箱再全开。
5. **与既有机制互补**：不替代 `act.py` 的 `refused` 校验，不改变 dedup/sweep 语义；只补「空 refused 静默丢」这一条缝。

---

## 6. 不做的事（明确划界）

- 不改 `act.py:127` 的 `if refused:` 逻辑。
- 不碰 `dedup.py` 的 claim/release 语义、不新增绕过 dedup 的重发路径（bounce 重放仍走 `sweep_once` → `process_email` → 重新 claim，dedup 仍是唯一真相源）。
- 不做「自动解析 NDR 后智能换收件人」之类高级逻辑——只做「重新入队重试 + 告警」。
- 不改动旧系统 `mailbots/`（已退休，洋另安排补发）。
- 不自动 mark-seen bounce 邮箱以外的任何行为；NDR 处理完标 `\\Seen` 即可。
- **不实现 `forward_log` 自动清理（见 §7 债）**。

---

## 7. 已知债（非阻塞，登记待下期）

**`forward_log` 无限增长**：每条成功转发都写一行且含 `raw_hex`（原始邮件全文）。按 500 封/天估算，约一年 18 万行 × 平均 50KB ≈ 9GB，SQLite 单表会有性能问题。

**下期方案（本次不实现，仅登记）**：
- 新增 `purge_old_forward_log(days=30)`，删除 `sent_at` 早于 30 天的行（覆盖绝大多数 NDR 到达窗口——NDR 通常在数分钟到数天内到达）。
- 清理任务挂入 sweeper 每日调度（复用 `dedup.purge_old_dedup(days)` 的同位置机制，见 `dedup.py:368`）。
- 更早历史可归档至冷存储后再删（可选）。

> 本期因 raw_hex 仅在 bounced 重放时需要，亦可考虑：bounced=1 且超过 N 天后将 raw_hex 置空（保留关联元数据、释放大字段）。列入同上债。

---

## 8. 交付后我这边要复核的点（给洋）

- OpenCode 改完跑 `pytest`，确认 ≥ 135 全绿、且新增 4 类测试都在。
- 我实读 `act.py`/`bounce.py`/`store.py`/`serve.py` 改动，确认：
  - `make_forward_id` 产出纯 hex 令牌（正则 `^[0-9a-f]{16}-[0-9a-f]{8}$`）。
  - forward_id 透传链路完整（execute_action → send_smtp → 头 `X-YXO-Forward-Id` + envelope `bounce+<hex>@`；成功 → forward_log）。
  - 告警**全部**走 `get_notifier().send_program_error`，无 `dedup.send_program_error` 残留。
- 确认 `init_forward_log()` 在 import 时调用（不依赖手动建表）。
- 确认 `BOUNCE_MONITOR_ENABLED=False` 默认、且不连 IMAP。
- 确认 `forward_log` 清理债已在 §7 登记（不阻塞本次合入）。
