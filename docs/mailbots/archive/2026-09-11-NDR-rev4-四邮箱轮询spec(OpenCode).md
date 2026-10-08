# NDR rev4 spec（OpenCode）—— 四邮箱轮询方案（不依赖数科部配邮箱）

- 出具人：芙蕾雅 日期：2026-09-11 分支：dev
- **本文件取代** `docs/2026-09-10-NDR退信监控增强spec(OpenCode).md` 中关于「专用退信邮箱 + VERP 基址」的部分（原 v3 的 forward_id 纯hex、`_forward_split` 三处统一、`_meta` 注入、poll 先于 sweep 等**均已验收，继续有效，不得回退**）。
- 触发原因：**数科部可能不给配 `mailbots-bounce@` 专用邮箱**，原方案的投递前提不成立。
- 关联：② 大邮件重放按**方案 A**（`docs/2026-09-11-NDR补强spec(OpenCode)-端到端用例与大邮件重放.md` §2.2）一并实施。

---

## 0. 铁律

1. 不瞎改已做好内容：`make_forward_id` 纯 hex、`_forward_split` 的 sub_fid 三处统一、`send_ctx["_meta"]` 注入、`poll_bounces` 先于 `sweep_once` —— **一律不动**。
2. 不自己 commit / push。
3. 做完自测：全量 **0 failed**。
4. 如如实报告，不替拍板；有疑问先问，别猜。

> 跑测试铁律：**默认 basetemp**，禁止 `--basetemp=<项目内目录>`（本机 shim 会把项目内路径送回收站 → 假失败）。命令：`python -m pytest -q -p no:cacheprovider > out.txt 2>&1`。

---

## 1. 新方案（一句话）

**退信本来就该回到"发件人"。而我们的发件人就是 4 位同事本人** → envelope MAIL FROM 直接用发信账号，NDR 落进同事自己的收件箱，`poll_bounces` 轮询这 4 个 INBOX，靠 `X-YXO-Forward-Id` 信头认领。

| 项 | 原方案（v3） | **新方案（rev4）** |
|---|---|---|
| envelope MAIL FROM | `bounce+<fid>@cqtransit.com` | **发信账号本人**（如 `maoxiaoyang@cqtransit.com`） |
| NDR 落点 | 专用退信邮箱 | **4 位同事各自 INBOX** |
| 主关联键 | VERP 本地名的 fid | **`X-YXO-Forward-Id` 信头**（VERP 已不可用） |
| poll_bounces | 1 个邮箱 | **4 个账号的 INBOX**（复用 `DEFAULT_ACCOUNTS` + `get_accounts()` 凭证） |
| NDR 过滤 | 邮箱里几乎都是 NDR | **必须严格判定**（见 §3 坑1） |
| 幂等 | 邮箱信少 | **本地 handled 索引**（见 §5 D1） |
| 新增邮箱 | 需要 | **不需要**（零依赖数科部） |

**已核实的前提（读码实证，可放心依赖）**
- 发信账号必落在 `DEFAULT_ACCOUNTS` 4 人内：`serve.py:325-331` `sender_email = get_sender_by_email(routing.responsible_person)`（失败时回退到收信账号，也在 4 人内）。
- **IDLE 只盯 5 个具名文件夹**（`settings.py:90-106`：运单草单/运单号/DSK/ATB/Tracing），**不含 INBOX** → 轮询 INBOX 与现有 ingestion 无冲突、不会重复处理。
- IMAP 凭证可用 `get_accounts()[account]`（`secrets.py:43-60`，与 SMTP 同一份密码）。**但上线前必须实测 IMAP 登录**（见 §7 探测）。

---

## 2. 改动清单（逐文件）

### 2.1 `config/settings.py`
- 新增 `BOUNCE_ENVELOPE_MODE = os.environ.get("BOUNCE_ENVELOPE_MODE", "sender")`，取值：
  - `sender`（**新默认**）：envelope = 本封发信账号；
  - `fixed`：envelope = `BOUNCE_ADDRESS`；
  - `verp`：envelope = `bounce+<fid>@<BOUNCE_ADDRESS 域名>`（保留，将来若拿到专用邮箱可切回）。
- 新增 `BOUNCE_POLL_ACCOUNTS`（逗号分隔；留空时：`mode=sender` → `DEFAULT_ACCOUNTS`，否则 → `[BOUNCE_IMAP_USER]`）。
- 保留 `BOUNCE_ADDRESS` / `BOUNCE_USE_VERP` / `BOUNCE_IMAP_*` / `BOUNCE_FOLDER`（`verp`/`fixed` 模式及回退用）。
- `config/__init__.py` 同步导出新常量。

### 2.2 `core/act.py`
- 新增 `_envelope_from(forward_id, sender_email) -> str`：按 `BOUNCE_ENVELOPE_MODE` 返回三种值之一。
- `execute_action`（:498-499）与 `_forward_split`（:406-408、:434-435）里的 `mail_from=_bounce_addr(...)` 全部改调 `_envelope_from(sub_fid, sender_email)`。
  ⚠️ `sender_email` 已是这两个函数的形参，直接用，不要新增全局。
- `_record_forward`（:160-171）增加写入 `sent_by = sender_email`（见 2.4）。`sender_email` 需从 `execute_action`/`_forward_split` 传入（这两个函数本来就有该形参）。

### 2.3 `core/bounce.py`（本次重点）
1. **加固 `parse_ndr`**（**坑1，必做**）：
   现有实现只读各 MIME 部件的 **header**（`part.get("X-YXO-Forward-Id")`）。但很多 MTA 把原信以 **`text/rfc822-headers`**（信头当**正文文本**）回传，此时 header 取不到 → 唯一关联键丢失。
   加固顺序：
   - ① 既有 header-walk 保留；
   - ② 对 `text/rfc822-headers`、`text/plain`、`message/delivery-status` 部件，**解码正文**后正则 `(?im)^x-yxo-forward-id:\s*([0-9a-f]{16}-[0-9a-f]{8})\s*$`；
   - ③ 兜底：对整封 raw 做同样正则。
   三者任一命中即用，优先级 ①>②>③。
2. **新增 `is_ndr(msg) -> bool`**（严格判定，防误伤同事正常邮件）：
   满足任一即判为 NDR —— (a) 存在 `message/delivery-status` 部件；(b) 顶层 `Content-Type: multipart/report` 且 `report-type=delivery-status`；(c) `text/plain`／`message/delivery-status` 正文里存在 `Action:` 行。
   **并且**：解析出的 `action` 必须是 **`failed`**；`delayed`／`delivered` 一律**忽略**（不计入，也不入队）。
3. **改造 `poll_bounces()`**：
   - 账号列表来自 `BOUNCE_POLL_ACCOUNTS`；密码 `get_accounts().get(acct)`（空则跳过并 WARN 计数）。
   - 校验：`BOUNCE_MONITOR_ENABLED` 为真，且账号列表非空。
   - 每账号：`IMAP4_SSL(BOUNCE_IMAP_SERVER, BOUNCE_IMAP_PORT, timeout=60)` → `login(acct, pwd)` → `select(BOUNCE_FOLDER)` → `search(None, "UNSEEN")`。
   - 逐封：`is_ndr` 过滤 → `parse_ndr` → **关联**（见下）→ 命中则 `dedup.add_error(...)` 重入队 + `mark_forward_bounced` → 记 handled。**失败继续下一封，单账号异常不得中断其它账号**（整体 try/except + 每账号 try/except，返回统计里带 `errors`）。
   - 返回统计扩展为 `{"processed","requeued","unknown","skipped_non_ndr","deduped","errors"}`。
4. **关联逻辑（双键，重要）**：
   - **主键**：`fid` 命中 `get_forward_log(fid)` 且 `bounced==0` → 正常重入队（沿用现有逻辑）。
   - **次键（新增）**：`fid` 缺失时，用 `failed_recipient`（NDR 的 `Final-Recipient`）+ 本封所在收信账号 → 查 `forward_log`（新函数 `find_forward_log_by_recipient(recipient, sent_by)`，取**最近一条未 bounced**）；**唯一命中**才当命中，否则 `unknown` 告警（不猜）。
   - 两者都无 → `unknown` + `send_program_error` 告警（保持现有行为，**不得静默**）。

### 2.4 `core/store.py`
- `forward_log` 表新增列 **`sent_by TEXT`**（SMTP 发信账号）。`init_forward_log` 建表语句加该列，并对旧库做容错迁移（`ALTER TABLE forward_log ADD COLUMN sent_by TEXT`，`OperationalError` 忽略）。
- `write_forward_log(...)` 增参 `sent_by`，落库。
- 新增 `find_forward_log_by_recipient(recipient, sent_by=None) -> Optional[dict]`：在 `to_list`/`cc_list`（JSON 文本）里找含 `recipient` 且（给了 `sent_by` 则要求相等）且 `bounced=0` 的**最新**一行。

### 2.5 `serve.py`
- `run_sweeper` 里 `poll_bounces()` 调用点不变（遍历逻辑在 bounce 内部）。放在 `sweep_once()` 之前的要求**保持**。

---

## 3. 两个必须修的坑（不做则方案静默失效）

**坑1 —— 关联键解析盲区（§2.3.1）**：`parse_ndr` 只读部件 header。`text/rfc822-headers` 形态的 NDR 会**完全取不到 fid**；在 VERP 已不可用的新方案里，这意味着**整条 NDR 链路静默失效**（退信照进来，却认不出是哪封）。必须按 §2.3.1 加固，并加回归用例。

**坑2 —— 没有可用的次键（§2.4）**：`forward_log` 目前**不记 SMTP 发信账号**（`account` 字段是"原信从哪个邮箱收到的"，`sender` 字段是**上游发件人**如 `docwbfb@yxologistics.com`，都不是我们发信用谁）。一旦 fid 缺失就**完全无法关联**。必须补 `sent_by`，否则次键无从谈起。

---

## 4. 测试要求

**新增/修改单测（`mailbots_next/tests/`，优先新开文件避免影响已通过用例）**
1. `parse_ndr` 四种形态：① `multipart/report`+`delivery-status`；② `text/rfc822-headers` 正文携带 `X-YXO-Forward-Id`（**坑1 回归锁**）；③ 无任何 forward_id（返回 None）；④ fid 出现在正文而非部件 header。
2. `is_ndr`：① 真 NDR 判 True；② 普通客户邮件（纯 text/plain、无 delivery-status）判 **False**；③ `Action: delayed` 的延迟通知 → **不当失败**（不计入 requeued）。
3. `poll_bounces` 多账号：mock 4 账号，仅 1 个账号有 NDR → 该封入队、其余账号的正常邮件不误判；统计字段正确。
4. 次键关联：fid 缺失 + `failed_recipient` 与 `sent_by` **唯一**命中 → 入队；**不唯一** → unknown 告警且不入队。
5. 幂等：同一封 NDR 连续两轮轮询 → 只入队一次。
6. **①端到端用例**（原补强 spec §1 的链路，envelope 改为 `sender` 模式）+ **②`fetch_raw_by_uid`**（方案 A）按原补强 spec 实施。

**验收标准（贴原始输出）**
- 全量：`python -m pytest -q -p no:cacheprovider` → **0 failed**（默认 basetemp）。
- 报告内贴：全量汇总行 + `git diff --name-only`。
- 确认**未回退**已验收项：`make_forward_id` 仍纯 hex；`_forward_split` 三处仍用同一 `sub_fid`；`poll_bounces` 仍在 `sweep_once` 之前。

---

## 5. 待洋拍板的 3 个决策点

- **D1 幂等方式**：我建议**新增本地 handled 索引**（记 `account+uid+message_id`），**不要动 `\Seen`** —— 现有 `poll_bounces` 把 NDR 标已读，会让同事**看不出自己的邮件被退了**（未读角标消失）。若你无所谓，沿用 `\Seen` 也行（改动更小）。
- **D2 读同事 INBOX 的知情**：机器人本来就用这 4 个账号登录读 5 个业务文件夹，所以**技术上早已具备全量读取权**；真正新增的只是 INBOX（含私人往来）。建议**明确告知这 4 位同事**（是透明化，不是求批准）。
- **D3 是否顺手向数科部要「更轻的诉求」**：不是"新建邮箱"，而是**在现有某个邮箱上加一个别名** `mailbots-bounce@`（别名比新建账号阻力小）。若给 → 可切回单邮箱方案（`BOUNCE_ENVELOPE_MODE=fixed|verp` + `BOUNCE_POLL_ACCOUNTS=<该邮箱>`），改造更小且更干净。**这条建议与 rev4 并行去问，不要卡住 rev4 落地。**

---

## 6. 非目标
- 不改 `make_forward_id` / `_forward_split` 的 sub_fid 逻辑 / `_meta` 注入 / poll-sweep 顺序。
- 不改客户可见的邮件内容（不得把 forward_id 写进 Subject）。
- 不 commit / push。

---
*§1 的三条"已核实前提"与 §3 的两个坑均来自 2026-09-11 读码实证。实施前若有与代码不符处，先回报再动手。*
