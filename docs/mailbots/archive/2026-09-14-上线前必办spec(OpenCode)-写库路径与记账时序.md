# 上线前必办 spec（OpenCode）—— 写库路径 + 记账时序 + 记账缺失可观测

- 出具人：芙蕾雅 日期：2026-09-14 分支：dev
- 触发：洋转来"其他对话发现的 UNC 写库隐患"，芙蕾雅读码复核后**确认存在，且实际后果比记录更严重**
- 洋 2026-09-14 追加拍板：H2 立即交办；H1 走部署执行单（零代码）；H3 只 WARN 不拒启；**并新增 H2 补丁：记账失败计数必须进每日 digest、显著呈现，且 digest 文案须改为运维可读的自然语言**
- 基线：当前全量 **337 passed / 0 failed**（rev4.3 已验收）

## 0. 铁律

1. 不瞎改已做好内容：NDR 全链路（rev4→rev4.3 已验收项）**一律不动**。
2. 不自己 commit / push。
3. 做完自测：全量 **0 failed**（基线 337）。
4. 如实报告，逐条列出改动文件。

> 跑测试铁律：**默认 basetemp**，禁止 `--basetemp=<项目内目录>`。

---

## 1. 问题定级与证据（读码实证）

### 1.1 事实链
1. `config/settings.py:9`：`YXO_DB_PATH` 默认 `r"\\10.0.199.184\yxo_data\yxo_app\data\yxo.db"` —— **默认是 UNC 网络路径**。
2. 旧系统 `mailbots/core/paths.py`：`_CANDIDATE_ROOTS = [r"D:\YXO_DATA", r"\\10.0.199.184\yxo_data"]` —— **本地优先**；注释明写"写测会在业务高峰制造锁竞争"，说明历史上确实吃过这个坑。
3. 新系统**确实回写** yxo.db：`store.py` 的 `write_dsk_timestamp`(:153) / `write_tracing_log`(:171) / `write_tracing_snapshot`(:199) 均以 `readonly=False` 打开（`mode=rw`），**且执行 `PRAGMA journal_mode=WAL`**（store.py:22）。⚠️ **SQLite 官方明确 WAL 不支持网络文件系统**（依赖共享内存/mmap），SMB 上典型症状正是 `attempt to write a readonly database` / disk I/O error。

### 1.2 后果被放大的一环（**本次真正的严重处**）
`core/act.py:517-527`（单目标路径；`_forward_split` 两处同构）：

```python
result = send_smtp(...)
if result["success"]:
    _record_forward(...)                     # 写 bot_config.db
    if row.email_type in ("dsk","atb") and row.container_no:
        write_dsk_timestamp(...)             # 写 yxo.db（可能 UNC/WAL）
    return True, "forwarded"
...
except Exception as e:
    return False, str(e)                     # ← 邮件已发出，却被判为"失败"
```

`serve.py:402-410`：detail 为失败 → `add_error(..., "ACTION_FAILED", ..., raw_bytes=raw_bytes)` 入 `error_queue` → **`sweep_once` 重放 → 同一封邮件再发给同一批客户**。

**即：写库失败 → 重复发信给客户**（与"静默丢信"是一对孪生风险）。

---

## 2. H1【必做 · 部署配置，零代码】生产显式把 `YXO_DB_PATH` 指向本地盘

**不由 OpenCode 落码**；已由芙蕾雅补进 `docs/2026-09-09-联运改造-生产部署执行单.md` 新增的 **§4.5 环境变量核对**，小叽上线时核对：

```
YXO_DB_PATH=D:\YXO_DATA\yxo_app\data\yxo.db
```

理由：**机器人就跑在 10.0.199.184 本机上**，本地路径与 UNC 指向同一份库；用本地路径可彻底避开 SMB 锁与 WAL 不兼容问题。**无代码改动、零风险。**

---

## 3. H2【必做 · 代码 P0】发信成功后的记账不得翻转"成功"结论

### 3.1 语义要求（**必须**）
`send_smtp` 一旦返回 `success=True`，**任何后续异常都不得使 `execute_action` 返回失败**。记账（`_record_forward` / `write_dsk_timestamp`）失败只允许：写 ERROR 日志 + `increment_counter("post_send_bookkeeping_failed")`，**不得**进入 `error_queue`（否则触发重发）。

### 3.2 建议改法（实现细节可定，语义照上）

```python
result = send_smtp(msg, sender_email, sender_password, to_list, cc_list,
                   mail_from=_envelope_from(forward_id, sender_email))
if not result["success"]:
    return False, f"SMTP failed: {result.get('error')}"

# 邮件已发出：此后任何失败都不得翻转结论，否则重放会导致重复发信
try:
    _record_forward(forward_id, message_id, row, send_ctx,
                    to_list, cc_list, original_raw, sender_email)
    if row.email_type in ("dsk", "atb") and row.container_no:
        write_dsk_timestamp(row.container_no, row.email_type.upper(),
                            datetime.now().strftime("%m/%d %H:%M"))
except Exception as e:
    _log.error(f"Post-send bookkeeping failed (mail already sent) | msg={message_id}: {e}")
    increment_counter("post_send_bookkeeping_failed")
return True, "forwarded"
```

要点：
- **外层 `try/except` 不得再把这些记账异常吞成 `False`**（要么按上面包成内层 try，要么把外层 except 限定在 `send_smtp` 之前）。
- `_forward_split` 的**两处**成功后分支（waybill/draft 与 dsk/atb）同样处理。
- 与 H1 是双保险：H1 消除"必然写失败"，H2 保证"万一写失败也不会重复发信"。

---

## 4. H2 补丁【必做 · 代码】`post_send_bookkeeping_failed` 进每日 digest：显著呈现 + 文案可读

### 4.1 动机
H2 把"写库失败"从**客户事故**（重复发信）降级为**记账缺失**，这是正确的取舍；但**记账缺失必须可观测**，否则等价于把异常从 `error_queue` 转移到**无人读取的日志**中。每日 digest（`DIGEST_HOUR` 默认 9 → 09:00，`serve.py:698-699`）是当前架构中**唯一被运维实际查看**的通知通道，因此该计数必须在该通道中可见。

### 4.2 现状（读码结论，避免做多余改动）
- `act.py` 的 `increment_counter("post_send_bookkeeping_failed")` 与其它计数写**同一份 counters 文件**；
- 链路：`serve.flush_and_notify()` → `flush_counters()` → `notify.send_digest(OPS_OWNER_EMAIL, counts)`；
- `notify.py::send_digest` 当前实现：首行 `📊 每日汇总 (YYYY-MM-DD)`，随后逐项渲染 `  <key>: <value>`。
- **结论：该计数已会自动出现在 digest 中**，但以**原始变量名 + 数字**呈现（如 `post_send_bookkeeping_failed: 2`），辨识度低、对运维不可读。
  → **本次只需补两件事：显著性、可读文案。**

### 4.3 需求（技术要求）

| 编号 | 要求 | 级别 |
|---|---|---|
| **R1 显著呈现** | `counts.get("post_send_bookkeeping_failed", 0) > 0` 时，在 digest 正文中**首行（标题行）之后、普通计数行之前**插入一条告警行，前缀用醒目符号（如 `⚠️`） | 必做 |
| **R2 文案可读（自然语言）** | 该告警行**禁止**只输出变量名与数字；必须以**中文自然语言**表达三要素：①当天发生次数；②影响——**邮件已正常发给客户、不会重发**（明确阻止人工重发）；③需人工核对项——对应的 **DSK/ATB 时间戳或运踪记录可能缺失**，请按需核对补齐。面向运维读者，措辞直白 | 必做 |
| **R3 普通计数行的可读化（范围受控）** | digest 面向人工阅读：允许并**建议**把 `post_send_bookkeeping_failed` 在普通计数行中的显示键由原始键名改为**可读中文标签**（如 `发信后记账失败`）。**其它计数行的显示键保持原样**（避免波及既有断言与运维习惯）；若确需统一改造，须在报告中说明并同步调整既有用例 | 建议 |
| **R4 零值语义** | 计数为 0 或缺键 → **不出现**告警行；既有 digest 行为不变（含 `flush_and_notify()` 的"计数为空则不发送 digest"语义） | 必做 |
| **R5 改动边界** | 不改 `flush_and_notify()` 的空计数语义；不改 `notify()` / 企微映射；不改 digest 的调度与发送时机 | 必做 |

**实现位置**：`core/notify.py::send_digest`（正文组装处）。普通计数行**保留**，告警行为**额外**插入。

**文案基线（可微调，但须满足 R2 三要素）**：

```
⚠️ 注意：今天有 N 次「邮件已发出但记账失败」——邮件已正常发给客户，不会重发；
   但对应的 DSK/ATB 时间戳或运踪记录可能没写上，请按需人工核对。
```

### 4.4 测试要求
- `send_digest` 传入含 `post_send_bookkeeping_failed: 2` 的 counts → 正文中该告警行**位置**在标题行之后、普通计数行之前；该行含次数 `2`，且含"邮件已发出"与"不会重发"语义（或以选定文案的关键词断言）。
- counts 不含该键、或值为 `0` → 正文**不出现**告警行，且不抛异常。
- R3 若实现：断言普通计数行出现可读中文标签（且其计数正确）。
- 既有 digest 用例（如 `test_p1_coverage.py::TestSweeper::test_flush_and_notify`、`test_flush_and_notify_empty`）必须继续通过。

---

## 5. H3【顺手做 · 代码，不阻塞】UNC 启动 WARN（**只告警，不拒启**）

洋 2026-09-14 定：**一律不 fail-fast**，避免误伤测试/开发环境。

1. **启动自检**：`YXO_DB_PATH` 以 `\\` 开头（UNC）时，输出**显式 WARN**：打印实际路径、建议的本地路径，并说明"WAL 不兼容网络盘，写库可能失败"。
2. **rw 连接**：路径为 UNC 时**不要执行** `PRAGMA journal_mode=WAL`（退回默认 DELETE 模式），并 WARN。
3. 测试：UNC 路径 → 断言 WARN 被记录 / 未执行 WAL PRAGMA；本地路径 → 无 WARN、仍执行 WAL。

---

## 6. 测试要求汇总

1. **H2 回归锁（防重复发信）**：`write_dsk_timestamp` 抛异常 + 发信成功 → `execute_action` 仍返回 `(True, "forwarded")`，且 `get_pending_errors(100)` 中**没有**该 message_id（修前必挂）。
2. **H2 回归锁 2**：`_record_forward` 抛异常 + 发信成功 → 同上。
3. **H2 回归锁 3**：`_forward_split` 的 waybill/draft 与 dsk/atb **两分支**各一条，同上。
4. **H2 反例**：`send_smtp` 返回 `success=False` → 仍**必须**返回 `(False, ...)` 并**入 `error_queue`**（不得把该拦的也放过）。
5. **§4 digest**：≥3 条（告警行位置与文案要素、零值/缺键不出现、既有用例不回归）；R3 若实现再加 1 条。
6. **§5 WARN**：UNC → WARN 且不启用 WAL；本地 → 无 WARN。
7. 既有 **337** 用例必须继续全过；settings/环境变更一律走 `monkeypatch`。

## 7. 验收标准（贴原始输出）

1. 全量 `python -m pytest -q -p no:cacheprovider` → **0 failed**（基线 337，预期上升）。
2. `grep -rn "post_send_bookkeeping_failed" mailbots_next/` → 至少出现在 `act.py`（计数）与 `notify.py`（告警行）。
3. 报告贴：全量汇总行 + `git diff --name-only`，并**逐条列出改动文件**。
4. 确认未回退：NDR 链路（`BODY.PEEK`、`bounce_handled`、`is_ndr`、双键、`_candidate_seqnos` 全量 UNSEEN、`_bounce_poll_due`、`float("-inf")`）均原样。

## 8. 非目标
- **H1 不由 OpenCode 落码**（走部署执行单 §4.5）。
- 不把记账改成异步队列；不改 `error_queue` 语义或 sweeper 重试策略。
- 不改 `flush_and_notify` 的空计数语义；不改企微映射与 digest 的调度/发送时机。
- 不做 digest 全量文案重构（R3 仅限该计数一行）。
- 不动 NDR 相关代码；不改 `ingest.py` 取信形态（Q1 待实测）。
- 不 commit / push。

---
*§1 的四条事实（settings 默认 UNC、旧系统本地优先、三个写函数 rw+WAL、记账在成功门内）均为 2026-09-14 读码实证；§3.1 的语义要求与 §4.3 的 R1–R5 是本次核心。*
