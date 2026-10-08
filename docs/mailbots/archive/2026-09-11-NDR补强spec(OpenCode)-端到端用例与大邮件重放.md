# NDR 补强 spec（OpenCode）—— ①端到端用例 + ②大邮件重放

- 出具人：芙蕾雅 日期：2026-09-11 分支：dev
- 上游：`docs/2026-09-11-NDR增强-验收报告.md` §6 遗留项 ① ②
- 涉及文件：`mailbots_next/tests/test_bounce_e2e.py`（**新建**，仅测试）、`mailbots_next/core/ingest.py`、`mailbots_next/serve.py`（② 生产代码，方案见 §3）

---

## 0. 铁律（同既往）

1. 不瞎改已做好内容 —— 已验收通过的 NDR 逻辑（`core/bounce.py`、`act.py` 的 forward_id/VERP/`_forward_split`）**一律不动**。
2. 不自己 commit / push。
3. 做完先自测：全量套件必须 **0 failed**，且**必须在题面要求的顺序下跑**（见 §5）。
4. 如实报告，不替拍板。

> ⚠️ 运行环境的坑（芙蕾雅实测）：跑 pytest **必须用默认 basetemp**，不要传 `--basetemp=<项目内目录>`。本机安全 shim 会劫持 `Path.unlink`，项目内路径会被送回收站、沙箱回收站不可用 → 抛 `OSError(SAFE_DELETE_FAIL_CLOSED)` → 测试在断言前假失败。收尾清理若卡住，把输出重定向到文件读汇总行即可。
> 命令：`python -m pytest -q -p no:cacheprovider > out.txt 2>&1`

---

## 1. ① 端到端用例：forward_log → NDR 重入队 → sweep 重放

### 1.1 为什么要补

现状 `test_bounce.py::test_poll_bounces_requeues_on_ndr` **把 `dedup.add_error` mock 掉了**，因此
「真实写 forward_log → NDR 命中 → 真入队 error_queue（带 raw_hex/account）→ sweep_once 真重放成功」
这条**最关键的闭环没有任何用例覆盖**——而这正是 `CQWLJT260912004-D` 静默丢信事故的盲区。必须锁死。

### 1.2 新建文件与夹具

新建 `mailbots_next/tests/test_bounce_e2e.py`。**不要改 `test_bounce.py`**（它没有 autouse DB 夹具，改动风险外溢；铁律 1）。

文件内建一个 autouse 夹具，**照抄** `test_p1_coverage.py:66-89` 的 `_p1_dbs` 写法（删除 4 个 DB 文件 → `init_db()` / `init_bot_config_db()` / `init_forward_log()` / `seed_owner_mapping()` → 播种 `scope='company'` 收件人；teardown `invalidate_cache()`）。

### 1.3 用例步骤与断言（必须全部覆盖）

用例名建议 `test_bounce_requeue_then_sweep_replays_end_to_end`。

**Step 1 —— 走真实转发路径，产出 forward_log**
- `monkeypatch.setenv("MAILBOT_MODE", "live")`（`is_live()` 是运行期读 env，**不需要** `importlib.reload`）。
- 造一封单目标草单邮件 raw（含附件），`patch("mailbots_next.serve.extract_email", return_value=[rows])`，
  构造 `MailProcessor` 并把 `proc.records` 设为能路由成功的合成记录（`scope='company'` 已播种该 company），`proc.notifier = Mock()`。
- `patch("mailbots_next.core.act.send_smtp", return_value={"success": True, "refused": {}})` → 调 `proc.process_email(...)`。

**Step 2 —— 断言 forward_log 已落库且带全重放字段（v3 §8 的闸门）**
- 直连 `get_bot_config_connection()`，`SELECT * FROM forward_log` 取**唯一**一行，取得 `fid = row["forward_id"]`。
- 断言：`row["raw_hex"] != ""`、`row["account"] != ""`、`row["uid"] > 0`、`row["folder"] != ""`。
- 断言 `fid` 满足 `^[0-9a-f]{16}-[0-9a-f]{8}$`。

**Step 3 —— 构造 NDR 并真跑 poll_bounces**
- 造 NDR raw，其 `To:` 为 `bounce+{fid}@cqtransit.com`（VERP 命中路径）。
- `monkeypatch.setattr(settings, "BOUNCE_MONITOR_ENABLED", True)` 及 `BOUNCE_IMAP_USER/PASSWORD/SERVER/PORT/FOLDER`
  （**必须走 monkeypatch**，禁止直接给 settings 赋值——这正是上一轮泄漏的成因）。
- `patch("mailbots_next.core.bounce.get_notifier", Mock())`；
  `patch("mailbots_next.core.bounce.imaplib.IMAP4_SSL")` 返回假连接：`search → ("OK", [b"1"])`、`fetch → [None, [(None, ndr_raw)]]`、`store/logout` 可 mock。
- 调 `poll_bounces()`，断言返回 `processed == 1` 且 `requeued == 1`。

**Step 4 —— 断言"真入队"，且 payload 完整（关键，不得再 mock add_error）**
- `get_pending_errors(100)` 里该 `message_id` 的行：`error_type == "BOUNCE"`、`raw_hex == <原始 raw 的 hex>`、`account != ""`、`folder != ""`、`uid > 0`。
- `get_forward_log(fid)["bounced"] == 1`。

**Step 5 —— 真跑 sweep_once 重放并断言闭环**
- `patch("mailbots_next.core.act.send_smtp", return_value={"success": True, "refused": {}})`；
  `patch("mailbots_next.serve.enqueue_mark_seen")`。
- 调 `stats = sweep_once(processor=proc)`。
- 断言：`stats["retried"] >= 1`；该 `message_id` 已**不在** `get_pending_errors` 中（已 resolved）；
  `enqueue_mark_seen` 被调用且首个实参为该行 `account`、`folder`、`uid`。

### 1.4 反面用例（建议一并加，防退化）
- **payload 缺失时必须拒绝重放、不得误判成功**：造一条 `raw_hex=""` 的 pending 行，`patch send_smtp`，
  断言 `send_smtp` **未被调用**、`attempt_count` +1（与 `test_p1_coverage.py::TestSweepReplay::test_payloadless_entry_never_misjudged_success` 同义，此处针对 BOUNCE 类型再锁一次）。

---

## 2. ② 超 2MB 邮件退信无法自动重放

### 2.1 病灶（读码实证）

- `dedup.add_error`（`dedup.py:230-239`）：`len(raw_bytes) > RAW_MAX_BYTES`（默认 2MB，`settings.py:45`）时**不存 payload**，只 WARN + 计数。
- `sweep_once`（`serve.py:577-594`）：`if not raw_hex or not account:` → 直接跳过重放，`increment_attempt` → 到 `SWEEP_MAX_RETRY` 转 manual + E1。
- 结果：**一封 2MB+ 的转发邮件一旦被退信，永远无法自动重试，直接落人工。**
- 附带隐患：`write_forward_log`（`store.py:287`）存 `raw_bytes.hex()` **无任何上限**；且 `error_queue`/`forward_log` **都没有清理机制**（全仓仅 `purge_old_dedup` 清 dedup 表）。

### 2.2 方案（**默认按 A 实现**，若你要改 B 告诉我）

**方案 A（推荐）：按 UID 从 IMAP 重取原文，取代"存全文"**
- 新增 `core/ingest.py::fetch_raw_by_uid(account, folder, uid) -> Optional[bytes]`，复用 `ingest.py:242-278` 既有模式：
  `IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=60)` → `login(account, <get_accounts() 里的密码>)` → `select(_server_folder(folder), readonly=True)` → `fetch(str(uid).encode(), "(RFC822)")` → 返回 `msg_data[0][1]`。任何异常 → 返回 `None` 并 WARN。
- `sweep_once` 改造（`serve.py:576-594`）：
  1. 若 `raw_hex` 非空 → **保持现状**（有 payload 就不发网络请求，最小改动）。
  2. 若 `raw_hex` 为空但 `account` 与 `uid` 俱全 → 调 `fetch_raw_by_uid`。
     - 取到 → 用它继续走原重放分支（`bytes.fromhex` 那步改为直接用取回的 bytes）。
     - 取不到 → 保持现有语义（`increment_counter("retry_refetch_failed")` + 原"烧次数→manual"路径）。
  3. `account` 或 `uid` 也缺 → 维持现状（WARN + 计数 + 烧次数）。
- 收益：**任意大小**邮件均可自动重放；`error_queue` 不再需要存大 payload，DB 不膨胀。

**方案 B（备选，最小 diff）：调大阈值 + 补清理**
- 仅把默认 `RAW_MAX_BYTES` 调大（如 20MB）并新增 `error_queue` 清理；**但**失败邮件全文会长期堆库，且不解决 `forward_log` 无上限存储——治标不治本。

### 2.3 与 ② 相关的建议（可选登记，不阻塞本次）
- `forward_log` 无需再存全文（改了 A 之后，bounce 只需 `account/folder/uid` 即可重取）→ 可评估停存 `raw_hex`，或至少补 30 天清理（原 NDR spec §7 已把它登记为债）。

---

## 3. 验收标准（必须实测，贴原始输出）

1. **全量套件 0 failed**：`python -m pytest -q -p no:cacheprovider`（默认 basetemp）→ 期望 `>=308 passed, 0 failed`（新增用例后总数上升）。
2. **单跑新旧 NDR 用例全过**：
   `python -m pytest mailbots_next/tests/test_bounce.py mailbots_next/tests/test_bounce_e2e.py -q`
3. **新增用例必须真跑非 mock 的入队路径**：代码里**不得**出现 `patch("mailbots_next.core.bounce.dedup.add_error")`。
4. **② 必须有用例**：
   - 新增「payload 缺失 + account/uid 俱全 → 从 IMAP 重取成功 → 重放成功 → resolved」用例（`fetch_raw_by_uid` 打桩返回原始 raw）。
   - 新增「重取失败 → 烧次数、不误判成功」用例。
5. 报告里原样贴出：全量汇总行、`git diff --name-only`。

## 4. 非目标
- 不改 `core/bounce.py` 的解析/关联逻辑、不改 `act.py` 的 forward_id/VERP、不改 `_forward_split`。
- 不引入新的清理任务（清理另开）。
- 不 commit / push。

---
*§1 的链路与 §2 的行号均来自 2026-09-11 读码实证；② 若改走方案 B 请先告知芙蕾雅再落码。*
