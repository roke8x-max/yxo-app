# 收信通路修复 spec（唯一机制：短周期轮询 + UID 水位线发现）—— 分步实现

> 日期：2026-09-16 ｜ **v7.2**：§5.1 验收的 grep 拆成"分文件"两条，并禁止为了凑验收在 `settings.py` 里新增 `UIDVALIDITY` 之类常量（ds 2026-09-17 反馈的表述歧义）。
> **v7.1**：消除 Step 1.9 第 5 条内"代码示例 vs 统一契约"的张力 —— ① `except` 分支**明确标注为"致命异常专用兜底"**，正常路径（含"部分行已入队"）**不经过它**；② **`process_email` 返回值形态定死**（改返回布尔，`True` = 已纳入持久化流程），外层判断**只依赖这一个值**；③ 外层判断收敛为**三条分支**；④ 测试补第 4 个场景。
> **v7**：按 ds 交付前反馈再补 3 点 —— ① **`process()` 契约**明确定义（"部分行已入队 → 仍正常返回并推进"；外层兜底入队**须幂等检查**，否则重复入队）② **`UIDVALIDITY` 获取方式实测写死**（`conn.untagged_responses['UIDVALIDITY']`；`select()` 返回值不含它）+ 假服务器**必须扩展 UID 语义**（实测 `uid('SEARCH')` 现返回 `('OK',[None])`）③ **状态文件忽略规则已确认**（`.gitignore:79` 已有 `data/*.json`，无需新增规则；**但必须放在 `data/` 下**）。
> **v6**：按 ds 反馈修正 —— 水位推进条件改为"明确充分条件"（"claim 成功"是**禁止的伪条件**）、UIDVALIDITY 重扫起点受 `FORWARD_SINCE` 约束、退信侧保留 `UNSEEN` 的三条理由预先写入。
> **v5**：洋拍板 ①**确认不做 IDLE**（`INGEST_POLL_SEC` 默认 30 为唯一机制、IDLE 分支整体删除、保留 `_idle_loop` 重连结构）②**UID 水位线本期做** ③**Step 1.8 改名做**。
> 编写：芙蕾雅 ｜ 交付对象：OpenCode
>
> **前置事实（双方独立实测，已定案）**：`core/ingest.py:328-337` 调用的 `conn.idle()` / `idle_check()` / `idle_done()` **在 Python 标准库 `imaplib` 中不存在**（3.14 才新增 `idle(duration)`，API 也不同）⇒ 20 条连接全部陷入"每 10 秒重连报错"的死循环，**一封邮件都不会被处理**。测试因全部 mock 而全绿。

---

## 1. 非目标（本期明确不做）

1. **不保留任何 IDLE 实现**（不引入 `imaplib2`、不升级 3.14、不自实现 raw-IDLE）。将来若真需要实时推送，**另立 spec 重新评估**。
2. **不改业务文件夹与识别逻辑**：仍是 5 个文件夹（`运单草单`、`运单号`、`Tracing`、`DSK`、`ATB`）；`_identify_type`、各 extractor、按箱号拆分、多公司扇出、A/B 规则 **一律不动**。
3. **连接拓扑回归原设计**：每账号 3 组 = **12 条**连接（见 Step 1.0）。
4. **不动**已验收的不变量：`bounce_handled` 幂等、`core/rowkey.py` 单一实现、`poll_bounces` 先于 `sweep_once`、H2"发信成功不得翻转结论"、退信侧 `BODY.PEEK`。
5. **不涉及**草单"更新/作废"标记（独立线，见 §4）。
6. **不 commit、不 push**。

---

## 2. 本期改动（P0，9 步）

### Step 1.0 —— 连接拓扑：每账号 3 组、共 12 条（回归原设计）

**依据**：`plan-b-processors-idle.md:693` 的 `Idler(..., folders_srv: list[str], ...)` ⇒ 原设计"一条连接管一组文件夹" ⇒ 3 组（草单+运单号 / DSK+ATB / Tracing）× 4 账号 = **12 条**。
现在的 20 条（`IDLE_PER_FOLDER` 默认 True）是偏离，理由见 `serve.py:419-421`（IDLE 模式下别堵 29 分钟）——**轮询无此问题**。
**决定**：**恒按组构建（12 条）**；`IDLE_PER_FOLDER` 随之删除（Step 1.6）。

### Step 1.1 —— 【P0 · 漏信级】每轮必须遍历组内**所有**文件夹

现状 `ingest.py:313-316` 内层 `while` 永不退出 ⇒ 外层 `for` 走不到第二个文件夹 ⇒ 3 组拓扑下 **「运单号」与「ATB」的全部邮件被静默丢弃**（8 个文件夹实例、4 账号；不报错、无日志、无错误队列）。

```python
while not self._stop.is_set():
    try:
        conn = self._connect()
        while not self._stop.is_set():                      # 保留原重连结构
            for folder_name, folder_utf7 in self.folders:   # 每轮遍历组内所有文件夹
                if self._stop.is_set():
                    break
                self._select_folder(conn, folder_utf7)
                self._process_new_messages(conn, folder_name, folder_utf7)
            time.sleep(self.poll_secs)
        conn.close()
        conn.logout()
    except Exception as e:
        _log.error(f"[{self.account}] ingest error: {e}")
        time.sleep(10)                                      # 重连退避，保留
```

### Step 1.2 —— `INGEST_POLL_SEC`（唯一轮询周期）

- `settings.py`：`INGEST_POLL_SEC = int(os.environ.get("INGEST_POLL_SEC", "30"))`，并在 `config/__init__.py` 导出。
- 默认 **30**（平均 15s、最坏 30s，上界可预测）。
- **`<= 0` 的 fail-safe**：已无 IDLE 可退，`0` = "彻底不收信" ⇒ **WARN + 回落 30**，不允许配出静默不收信的系统。
- 参数改名：`poll_fallback_secs` → **`poll_secs`**；同步调用点 `serve.py:448`、`ingest.py:403`（死代码 `IngestManager` 也要同步，否则导入即报错）。

### Step 1.3 —— 接线 `serve.py:441-452`
用**解析后的值**（见 1.4）；删掉 `max_idle=MAX_IDLE_SEC` 参数。

### Step 1.4 —— 把已解析但从未使用的 `--poll-secs`（`serve.py:729`）真正接上

```python
poll_secs = args.poll_secs if (args.poll_secs and args.poll_secs > 0) else INGEST_POLL_SEC
poll_secs = max(1, poll_secs)
```
`poll_secs` 是**唯一真值**：传给 Idler、打日志、重连判断都只用它。同步修正 `README.md:141`。

### Step 1.5 —— 启动日志（**上线自检靠它**）
```
Ingest mode: poll {N}s | connections={count} | folders={n} | discovery=uid-watermark
```
`count` 应为 **12**、`n` 应为 5。

### Step 1.6 —— 【核心】彻底删除 IDLE 代码路径（不留装饰）

| 位置 | 处理 |
|---|---|
| `ingest.py::_idle_once()`（`:325-338`） | **整体删除** |
| `_idle_loop` 的 `if poll_… > 0 / else` 分支（`:313-318`） | **删除分支**，只留轮询路径 |
| `max_idle` 参数 + `self.max_idle`（`:225/:234`） | **删除** |
| `poll_fallback_secs`（`:226/:235`） | 改名 `poll_secs` |
| 线程名 `Idler-{account}`（`:342`） | 见 Step 1.8 |
| `IngestManager.start()`（`:403`） | `poll_secs=INGEST_POLL_SEC` |
| `log_imap(..., "idle_start")` | 改 `"poll_start"` |
| 类/docstring 所有"IDLE"描述 | **改成事实** |
| `settings.py:55 IDLE_PER_FOLDER`、`:57 MAX_IDLE_SEC`、`config/__init__.py:38/39/110/111` | **全部删除** |
| `serve.py:20/21` 导入、`:418-454` 里 `if IDLE_PER_FOLDER:` 拓扑分支、`:419-421` docstring、`:447/:448`、`:454` 日志 | 删除分支；日志改 `Started {n} ingest pollers (poll {N}s)` |

**必须保留**：`_idle_loop`（Step 1.8 后为 `_poll_loop`）的**重连结构** —— 外层 `while not stop` + `try/except` + `_connect()` + `close/logout` + 异常后 `sleep(10)` 重连。

### Step 1.7 —— 【必须】改写受影响的现有测试（**不许直接删掉了事**）

`tests/test_p1_coverage.py::TestIdleTopology`（`:822-879`）测的是被删路径：

| 用例 | 要求 |
|---|---|
| `test_per_folder_idler_count`（`:836`，patch `IDLE_PER_FOLDER=True`，断言 20 条/每 Idler 1 文件夹）+ `test_legacy_group_topology`（`:850`，断言 12 条） | **改写合并**：不再 patch 已删常量；断言**恒 12 条**、每个 Idler 的 `folders` 长度按 `IDLE_GROUPS` 为 2 或 1 |
| `test_idle_done_called` / `..._on_check_error`（`:859/:870`，调 `_idle_once`） | **删除**；覆盖价值**转移到 4.2**（改断言"每轮遍历所有文件夹 + 从不调用 `idle*`"） |

**原则：删代码不能连覆盖一起删 —— 每个被删用例都要在 §2 Step 4 有轮询版替代。**

### Step 1.8 —— 【本期做】改名去 IDLE 味（ds 两条约束）

**理由**（ds）：本次事故的本质就是"**名字/API 承诺了不存在的东西**"——`Idler` 让所有人（含测试）都以为"IDLE 在工作"。名字是认知负荷的最小来源。

**建议新名**：`Idler` → **`Poller`**；`start_idle_processors`/`stop_idle_processors` → **`start_pollers`/`stop_pollers`**；`_idlers` → **`_pollers`**；`_idle_loop` → **`_poll_loop`**（结构不变）。**不要**在名字里保留 `idle`。

**约束 1（一次改齐，前后对账）**：
```bash
# 改之前先拉全部引用点，一个不漏
grep -rn "Idler\|idle_processors\|_idlers" mailbots_next/ --include="*.py"
# 改完后同一 grep 必须为 0
```
已知引用点：`core/ingest.py`（类定义、线程名）、`core/__init__.py`（导入 + `__all__`）、`serve.py`（导入、`_idlers`、两个函数名、`main` 里两处调用）、`tests/test_p1_coverage.py`（`patch.object(serve_mod, "Idler")`、`start/stop_idle_processors`）。

**约束 2（import 冒烟，3 秒抓错）**：
```bash
python -c "import mailbots_next.serve; import mailbots_next.core.ingest"
```
必须成功。改名最容易翻车的地方就是 `__init__.py` 的导出和 `from … import …` 的悬空引用 —— 这条比跑全量 pytest 快得多。

### Step 1.9 —— 【本期做】发现机制改为 **UID 水位线**（取代 `SEARCH UNSEEN`）

**为什么本期做（洋 2026-09-16）**："虽然同事现在会等着机器人处理（= 不先读邮件），但 UID 水位线更保险。"
即：**当前**风险低（邮件到达时仍是 UNSEEN），但**一旦同事习惯性先打开邮件**，`SEARCH UNSEEN` 就变成漏信机制——而这正是"手动转发"这类场景的常态。**别把漏信机制留在唯一收信通路上。**

**设计要求（按计划书原设计，`plan-b-processors-idle.md:691-692`）**：

1. 新增本地状态：`(account, folder) -> (uidvalidity, last_uid)`，JSON **原子写**（临时文件 + `os.replace`）。路径建议 `DATA_DIR / "imap_state.json"`，并支持环境变量覆盖（如 `IMAP_STATE_PATH`，便于测试隔离）；新配置须在 `settings.py` 定义并导出。
2. 取信流程改为：
   - `select(folder, readonly=True)`
   - 读 `UIDVALIDITY`：**与状态不符 ⇒ 清空 `last_uid` 并全量重扫**（UID 在 UIDVALIDITY 变化后会重排）
     - 🔴 **"全量重扫"的起点不是 `UID=1`，而是 `UID SEARCH SINCE <FORWARD_SINCE>` 的最小 UID** —— 即**重扫同样受 `FORWARD_SINCE` 门禁约束**（ds 2026-09-16 澄清）。从 `UID=1` 起扫会把门禁之前的历史邮件全部拉出来，既违背 `FORWARD_SINCE` 的意图、又白做一次全量扫描。
   - `UID SEARCH UID {last_uid+1}:*`
   - 逐封 **`UID FETCH (BODY.PEEK[])`** ← **不再用 `(RFC822)`**（见第 4 点）
   - 更新水位并落盘
3. **`FORWARD_SINCE` 门禁必须保留且更关键**：**首次运行（state 为空）时，起点由 `FORWARD_SINCE` 决定**（`UID SEARCH SINCE <FORWARD_SINCE>`），并**保留客户端 `Date` 头双重校验 + `historical_skip` 计数**（现有逻辑不动语义）。⚠️ 若把它漏掉，上线首次运行会把**全部历史邮件转发给客户** —— 本步最高危的一点。
4. **取信一律改 `BODY.PEEK[]`**：
   - 既然不再靠 UNSEEN 排除"已处理"，PEEK 不会造成重复取回（水位线保证同一 UID 只取一次）⇒ **不再需要 `RFC822` 的隐式置已读**。
   - **这同时闭环了 Q1**：`ingest.py:277` 与 `fetch_raw_by_uid`（`:365`）的 `(RFC822)` 一并改为 `BODY.PEEK[]` / `UID FETCH`。
5. 🔴 **水位推进时机（安全铁律，表述已按 ds 2026-09-16 修正）**

   **水位推进的唯一充分条件 = 该封已完成下面二者之一：**
   - **(A) 处理成功**：已转发完成，或已被判定为 `skip`/`duplicate` 等终态；**或**
   - **(B) 已成功写入 `error_queue`**（带 `raw_bytes`，能被 `sweep_once` 重放）。

   **明确禁止的伪条件**：
   - ❌ **`dedup` claim 成功 ≠ 可以推进**。claim 只是"占位"，只代表进入了处理流程，**处理结果未定**。若在 claim 后即推进水位，则"处理失败且未入队"（例如 extract 抛异常但没被捕获入队）的邮件会**永不重扫 = 静默漏信**。
   - ❌ 也**不允许"先推进水位、再处理"**。

   **实现形状（建议）**：
   ```python
   try:
       ok = process()                      # 见下方「统一契约」：正常返回即代表
                                           #   该封已纳入持久化流程（全成功 或 部分行已入队）
       if ok:
           advance_watermark(uid)          # (A) 成功 → 推进
       # 正常返回但 ok 为 False（理论上不应出现）⇒ 不推进，记 ERROR + 告警

   except Exception:                       # ⚠️ 只在【致命异常】时才会走到这里，
                                           #    见下方「统一契约」——正常路径下单封级失败
                                           #    已在 process() 内部入队并正常返回，
                                           #    不会进这个分支
       if enqueue_error_queue(uid, raw, ...):   # (B) 兜底入队成功（带 raw）
           advance_watermark(uid)          # → 推进
       # 入队也失败 → 不推进，下一轮重扫同一封
   ```

   > ⚠️ **这个 `except` 分支不是"每次都要检查入队"** —— 它是**致命异常专用兜底**。正常路径（含"部分行失败但内部已入队"）走的是 `ok = process()` 那一行，**不经过 `except`**。若误把它理解成"每轮都要额外入队一次"，会造出**重复入队**（故 `except` 内的入队**必须先做幂等检查**，见下）。

   **验收要覆盖这个区分**：既有"处理失败时水位不推进"，也要有"**claim 成功但处理抛异常、且入队失败 → 水位不动**"的用例（Step 4.7 ③）。

   🔴 **`process_email` 的契约必须先定义清楚，否则上面这段会被写歪（ds 2026-09-16 补 + 09-17 补返回值形态，已核实现状）**

   现状事实（已读码）：`serve.py::process_email` **没有返回值**（返回 `None`）；它对**每一行**有 `try/except` → 失败时内部调 `add_error(..., raw_bytes=raw_bytes)` 并继续（`serve.py:128-144`）；但 **`_identify_type`（`:82`）与 `extract_email`（`:95`）这两个前置步骤没有保护**，它们抛异常会直接冒泡出 `process_email`。

   **统一契约（要求照此实现）**：
   - 🔴 **返回值形态必须先明确**：现状 `process_email` 返回 `None`、**无任何状态**，外层无从判断。**要求改为返回布尔**（`True` = 该封**已纳入持久化流程**：全成功 或 至少已入 `error_queue`；`False` = 未纳入，外层不得推进水位）。若你选择返回枚举/状态对象也可以，但**必须能区分"已纳入"与"未纳入"**，且外层判断只依赖这一个值。
   - `process_email` **内部负责把"任何单封级失败"落到 `error_queue`（带 `raw_bytes`）并正常返回 `True`** —— 包括把 `_identify_type` / `extract_email` 阶段的失败也纳入（现在是裸的，要补保护）。
   - 只有**致命错误**（无法建立处理上下文：DB 不可达等）才**向上抛异常**。判断口径：**该错误是否让"把这一封记入 error_queue"本身都无法完成** —— 若是 ⇒ 抛；若"这封处理得不好但仍能入队" ⇒ 内部入队 + 返回 `True`。
   - 因此外层判断变成（**只有这三条分支，没有第四条**）：
     | `process_email` 行为 | 含义 | 外层动作 |
     |---|---|---|
     | 返回 `True` | 全成功，或部分行/整封已入 `error_queue` | **推进水位** |
     | 返回 `False` | 未纳入持久化流程 | **不推进** + ERROR + 告警 |
     | 抛异常（致命） | 连入队都没做成 | **不推进** + ERROR + `increment_counter(...)` + `send_program_error` 告警 + **尝试兜底入队**（入队成功后再推进） |
   - 🔴 **兜底入队必须做幂等检查**：入队前先用 `has_pending_error(message_id, …)` 确认该封没有已存在的 pending 记录，否则会与"内部已入队的行"产生**重复入队**。
   - ⚠️ **不加兜底入队会造出另一种静默失效**：同一封每 30 秒重试一次、**永不入 error_queue、只在日志里刷 ERROR** —— 隐蔽程度和 IDLE 空转一个量级。所以"抛异常"这条路必须有终点（入队或明确告警），不能只是"下轮重扫"。
6. 🔴 **连带修正：现状名为 `uid` 的其实是「序号」**（`ingest.py:258/277` 用的是 `conn.search`/`conn.fetch`，**非** `UID` 变体）⇒ 改水位线时**必须同步改成真 UID**，否则会与下游混用。受影响下游：
   - `send_ctx["_meta"]["uid"]` → `forward_log.uid`（NDR 关联用）：改真 UID 后，`fetch_raw_by_uid` 也**必须改用 `UID FETCH`**（否则序号漂移会取错邮件）。
   - **`MarkSeenBatcher` 的 `STORE` 用的是序号**（`ingest.py:113` `conn.store(seq, "+FLAGS", "\\Seen")`）⇒ 传入真 UID 会**标错邮件**。**必须一并改成 `UID STORE`**（`conn.uid("STORE", …)`），这也顺带消除 expunge 后的漂移。**这条最容易漏，请务必覆盖。**
7. **测试要求（Step 4.7）**：① 首次运行取全量、第二次只取新增；② `UIDVALIDITY` 变化 → 全量重扫，**且重扫起点仍受 `FORWARD_SINCE` 约束**（断言起点不是 UID=1）；③ **水位不推进的两种情形都要测**：(a) 处理失败/异常时；(b) **claim 成功后处理抛异常、且入队失败时**（防止把"claim 成功"当成推进条件）；④ 首次运行受 `FORWARD_SINCE` 门禁；⑤ **核心回归锁：邮件已被标 `\Seen` 仍能被取到**（断言搜索条件**不含 `UNSEEN`**）——这条直接钉死本次要修的病；⑥ 连带：mark-seen 走的是 `UID STORE`；⑦ **`process_email` 契约四种场景**（对应上文"统一契约"）：**返回 `True`（全成功）→ 推进** / **部分行失败且内部已入队 → 仍返回 `True` 并推进** / **返回 `False` → 不推进 + 告警** / **致命异常 → 外层兜底入队成功 → 推进**，并断言**没有重复入队**、且**每次水位推进都发生了对应的"已纳入持久化"事实**（即不存在"推进了但既没成功也没入队"的情形）。
8. **状态文件落在 `data/` 下即已被忽略（无需改 `.gitignore`）** —— 实测：`.gitignore` **第 79 行已有 `mailbots_next/data/*.json`**（第 78 行是 `data/*.db*`），`git check-ignore -q` 对 `imap_state.json` 与 `daily_counters.json` 均返回"已忽略" ✓。⇒ **不要新增冗余规则；但状态文件必须放在 `DATA_DIR` 下**（放到 `data/` 之外就不再受忽略保护）。
9. **`UIDVALIDITY` 的获取方式（实测确认，照此写）** ——
   - `conn.select(folder, readonly=True)` 的返回值**只有 EXISTS 计数**（实测 `('OK', [b'1'])`），**不含 UIDVALIDITY**；
   - UIDVALIDITY 要从 **`conn.untagged_responses['UIDVALIDITY']`** 取（实测 `[b'1']`，是个 **bytes 列表**），等价写法 `conn.response('UIDVALIDITY')` → `('UIDVALIDITY', [b'1'])`（`imaplib` 会把 `* OK [UIDVALIDITY 1] …` 这个 bracketed code 收进 untagged_responses）；
   - **取不到时**（服务器未发该 code）：**WARN + 保守按"UIDVALIDITY 已变化"处理**（即重扫，且重扫起点仍受 `FORWARD_SINCE` 约束，见第 2/3 条）；
   - 顺带实测：`readonly=True` 实际发出的是 **`EXAMINE`**（不是 `SELECT`）—— 与"只读不改"的意图一致，保持即可。

---

### Step 2 —— 修"运踪邮件 100% 不转发"（同为上线阻塞）

**现象（实测）**：`route_tracing()` 命中返回 **list**（`routing.py:173`），但 `serve.py:251-256` 对 list 做**多公司扇出**、把**单个** `RoutingResult` 传下去；`decide_tracing`（`decide.py:109-114`）**只接受 list** ⇒ `isinstance(routing, list)` 恒假 ⇒ **恒 `TRACE_MISS/skip`**，**不留错误记录**。

**修法（保守，改判定不改扇出）**：`decide_tracing` 接受两种形态 —— 单个且 `routing.success` → `TRACE_HIT`；list 非空 → `TRACE_HIT`；其余 → `TRACE_MISS`。
**约束**：不得破坏多公司扇出（命中 N 家 → N 次转发）；同步修正类型契约（`routing.py::route_row` 返回注解 + `serve.py` 注释写明"可能返回 `list[RoutingResult]`"）。

### Step 3 —— 修"全新部署首启即崩"

`mailbots_next/data/` **不进 git**（`.gitignore:78` 忽略 `data/*.db*`），而代码只 mkdir `LOGS_DIR`（`log.py:19`）与 counters 父目录（`notify.py:28`）⇒ 首启 `sqlite3.connect` 抛 `unable to open database file`。
**修法**：打开 `bot_config.db`/`dedup.db`/`draft_nums.db` **以及 Step 1.9 的 state 文件**前确保父目录存在（幂等）。**约束**：不影响测试的路径隔离。

---

### Step 4 —— 测试（本期必做）

**4.1 【最重要】收信真链路冒烟 —— 目标不是覆盖率，是"证明真有邮件被回调"**
- **断言 `on_raw_calls >= 1`**。
- **必须**用可驱动的假 IMAP 服务器（真 TLS + 最小 IMAP 协议）；**禁止**用 `patch("…imaplib.IMAP4_SSL")` 的 MagicMock 冒充（这正是本次事故被掩盖的原因）。
- **允许** monkeypatch `IMAP_SERVER`/`IMAP_PORT`（**换地址**）；**禁止** patch `imaplib.IMAP4_SSL`（**换客户端**）。
- 参照实现（已实测可用，约 120 行）：
  ```
  //10.0.199.184/yxo_data/.workbuddy/tmp/idle_probe/fake_imap.py
  ```
  ⚠️ **路径用正斜杠**；反斜杠在多一层调用/脚本传递时会被转义吞掉一个 `\`，变成非法 UNC 报"找不到路径"（已踩过）。**可访问性已实测**（`listdir` ✓、读 4605 字节 ✓、`copyfile` ✓）；读不到就按描述自行实现，不阻塞。
  ⚠️ **假服务器当前不支持 UID 语义，必须扩展**（2026-09-16 实测结论）：
  - `conn.uid("SEARCH", "UID", "1:*")` → `('OK', [None])`、`conn.uid("FETCH", "1", "(BODY.PEEK[])")` → `('OK', [None])` —— 因为 fake 把 `UID` 当**未知命令**只回 `OK completed`、**不发任何 untagged 行**。
  - 已经能用的：`EXAMINE/SELECT` 会带 `* OK [UIDVALIDITY 1] …`（实测 `conn.untagged_responses['UIDVALIDITY'] == [b'1']` ✓）、`SEARCH` 恒返回 `* SEARCH 1`、`FETCH` 回一封固定邮件、`STORE` 回 `* 1 FETCH (FLAGS (\\Seen))`。
  - ⇒ **必须补**：解析 `UID` 前缀命令；`UID SEARCH` 回 `* SEARCH <uid …>`；`UID FETCH` 回 `* <seq> FETCH (UID <n> BODY[] {len}…`（`BODY.PEEK[]` 与 `UID` 都要能取到）；`UID STORE` 回 `* <seq> FETCH (FLAGS (\\Seen) UID <n>)`；并支持"投喂第 2 封邮件"（否则测不了"第二次只取新增"）。
  - 另外它**不会主动推 `* n EXISTS`**（本期测轮询用不到，但要测连接保活/通知时需扩展）。
- **TLS 证书**：客户端是 `IMAP4_SSL`，假服务器必须真 TLS；同目录备有 `cert.pem`(1076B)/`key.pem`(1732B) 可复用，或提交一份 **TEST-ONLY** 自签证书（`mailbots_next/tests/fixtures/`）。**不要因为造证书麻烦就退回 MagicMock。**

**4.2 轮询分支**：`poll_secs>0` 时 `conn.idle/idle_check/idle_done` **从未被调用**；每轮 `sleep` 恰一次；**【对应 1.1，旧代码必挂】组内 2 个文件夹时两个都被轮询**；连接异常后仍会重连。

**4.3 运踪（必须走完整路径）**：经 **`process_email` → `_process_row` → 运踪分支 → `decide_tracing`**，**禁止直调**；断言 ①单公司→`forward` ②多公司→**N 次** forward ③无命中→`skip`。

**4.4 data 目录**：删除 `mailbots_next/data/` 后 TEST 模式启动 → 不崩、自动建目录。

**4.5 基线回归**：**不得引用会漂移的绝对值**（本条原写"现有 372 个用例" —— 那是**另一个口径的陈旧数字，是 spec 的错**，2026-09-17 芙蕾雅认领并改掉）。
正确判据＝**相对量**：

- 采样命令写死：`python -m pytest -q -p no:cacheprovider mailbots_next/tests/`（**这一层**的基线，不要把仓库根的全量混进来 —— 仓库根是 `mailbots_next/tests/` + `mailbots/tests/` 两个集合，数字完全不同）；
- 判据：**0 failed**，且 **用例总数变化 = 新增用例数 − 按 Step 1.7 改写/删除的用例数**（须逐条说明增减来源）；
- 🔴 另有一条**不可替代**的判据：`test_true_link_smoke_tls_proves_callback` **必须在无第三方依赖的前提下真跑通**（它是防"全 mock 掩盖"的那一环，见 §4.1）。

**4.6 文档与实现一致**：`README.md` 的 `--poll-secs` 改为事实；`--once`/`--errors retry` 删除或标注"未实现"；`:227` 那段"20 条长连接 / `IDLE_PER_FOLDER=0` 回退"改为"12 条轮询连接"。

**4.7 UID 水位线**：见 Step 1.9 第 7 条（**7 项**，含 `process_email` 契约**四场景**）。

---

## 3. 下期（P1，本期**不实现代码**）

> ⚠️ 例外：**README 的假承诺必须本期一并处理**（4.6）。

1. `--once`（`serve.py:730` 解析、全仓零引用）：**实现**（单轮扫完全部文件夹后退出）**或**从 README 删除。
2. `--errors retry` 与 `resolve` 行为完全相同（`:762-766` 都只 `mark_error_resolved`）⇒ **retry 并不重试**：改名或真接上重放。
3. `IngestManager`（`core/ingest.py:377`）死代码、`_on_raw` 只打日志不接流水线 → 清理或加显式保护。
4. `purge_old_dedup` 定义了但**全仓零调用**，`README.md:290` 却称"已有" → 接线或改说明。
5. `store.py` 两处静默 `except Exception: return False`（`write_tracing_log`/`write_tracing_snapshot`）—— **当前零调用（死代码），不是活风险**；等启用时再修。
6. `xlwt` 在 `requirements.txt:12` 标"测试专用"，但生产 `act.py:304` 在用 → 修正标注。

---

## 4. 独立线（**不在本 spec**）：重新启用「更新/作废」

1. **草单"更新台账"只有读、没有写**（`draft.py:100` 只读；全仓唯一建表/写入在测试夹具里）⇒ `draft_category` 恒为 A，且每封草单刷一次 ERROR + 企微程序告警。
2. **人工转发对机器人不可见**（不监控任何「已发送」）⇒ "更新"的语义前提不成立。
3. 判定需升级为**版本指纹台账**（票号 + 文档类型 + 内容指纹，**跨 4 账号共享**），保守规则：**指纹相同 → 必须退化为 A，不打任何"更新/作废"字样**。

---

## 5. 验收（须提交原始输出，不接受结论式描述）

1. **grep 断言（逐条贴计数）**
   - `grep -rn "idle_check\|idle_done" mailbots_next/ --include="*.py"` → **0 条**（洋明确要求）
     - ⚠️ **口径补充（2026-09-17 验收实测）**：该断言应理解为**生产代码 0 条**。全仓含测试时会有 **2 条**，位于 `tests/test_p1_coverage.py` 的 `assert not hasattr(conn, "idle_check")` —— 那是**否定断言**（证明"不再调用"），不是可执行路径，属良性。验收报告请按 **"生产代码 0 条；全仓 2 条均为否定断言"** 表述。
   - `grep -rn "conn\.idle\b" …` → **0 条**；`grep -rn "_idle_once\|MAX_IDLE_SEC\|IDLE_PER_FOLDER\|poll_fallback_secs" …` → **0 条**
   - **改名对账**：`grep -rn "Idler\|idle_processors\|_idlers" mailbots_next/ --include="*.py"` → **0 条**
   - `grep -rin "idle" mailbots_next/ --include="*.py"` → 剩余命中**逐条列出并说明性质**（允许：历史解释性注释、IMAP 能力字符串；**不允许**任何可执行调用）
   - **发现机制**：`grep -rn "UNSEEN" mailbots_next/ --include="*.py"` → 收信侧（`core/ingest.py`）**必须 0 条**；**退信侧 `core/bounce.py` 允许保留**，理由（**预先写明，不必再追问**）：
     1. NDR 邮件由退信方按需送达，**不存在"被同事先读掉"的场景**（这正是收信侧要改 UID 水位线的原因，退信侧没有这个病）；
     2. 退信侧已有 `bounce_handled(account, uid)` **幂等表**，去重不依赖 `UNSEEN`；
     3. 退信侧的 UID 语义需**独立评估**（`bounce.py:285` 现在是 `conn.search(None, "UNSEEN")` 用**序号**搜、`:171 _fetch_uid` 再从序号反查 UID）—— **不在本 spec 范围**，不要顺手改。
     ⇒ 所以报告里请写成"收信侧 0 条；退信侧 N 条，属既定例外（三条理由见 spec）"，**不要**为此犹豫或擅自改动退信侧。
   - **分文件核对**（不要把两个文件写进同一条 grep，否则"均出现"会被误读成"每个文件的每个关键字都出现"—— `settings.py` 不该出现 `UIDVALIDITY`，那是 IMAP 协议字段、不是配置项）：
     - `grep -n "INGEST_POLL_SEC\|IMAP_STATE" mailbots_next/config/settings.py` → 均出现
     - `grep -n "IMAP_STATE\|UIDVALIDITY\|uidvalidity" mailbots_next/core/ingest.py` → 均出现
     - ⚠️ **不要在 `settings.py` 里新增 `UIDVALIDITY` 之类的常量**去"凑"这条验收——它属于协议交互，只在 `ingest.py` 出现。
2. **必须贴启动日志那一行** `Ingest mode: poll 30s | connections=12 | folders=5 | discovery=uid-watermark`。
3. **必须贴** 4.1 冒烟用例原始输出（证明"确有邮件被回调"）+ 4.7 第⑤条（已标 `\Seen` 的邮件仍被取到）原始输出。
4. **import 冒烟**：`python -c "import mailbots_next.serve; import mailbots_next.core.ingest"` 成功。
5. **README 与实现一致**（§4.6）。
6. 全量：`python -m pytest -q -p no:cacheprovider` → **0 failed**。
   ⚠️ **必须用默认 basetemp**；**禁止** `--basetemp=<项目内目录>`（会触发本机 safe-delete 拦截 → 假失败 + 套件 33 秒→230 秒）。
7. `git diff --name-only` 逐条列出改动文件；**非必要不动** `WORKFLOW.md`/`AGENTS.md`/生产配置，动了必须写明原因。

---

## 6. 铁律（照旧）

1. **不瞎改已做好内容**（§1.4 的不变量一律不回退）。
2. **不自己 commit / push**。
3. **做完先自测**，把原始输出贴回来。
4. **如实报告，不替拍板** —— 有偏差/疑问/与代码冲突，直接写出来，不要自己改设计。

---

## 7. 部署侧配套（芙蕾雅自己做）

`docs/2026-09-14-上线前执行清单(给小叽).md`：
1. `:200` 删掉 `--once` 干跑（该参数是死的）；
2. `:146` 依赖补 `xlwt`（生产在用）；
3. 新增一步：**先建 `mailbots_next\data\` 目录**（或依赖 Step 3）；
4. 新增 `INGEST_POLL_SEC`（默认 30，可不设；**已无 IDLE 可切**）与 UID 状态文件路径说明；
5. **启动自检更新**：无 `IDLE error: Unknown IMAP4 command`；出现 `Ingest mode: poll …` 且 **connections=12**；**`FORWARD_SINCE` 仍是必设项**（首次运行的历史邮件门禁，Step 1.9 后更关键）。
