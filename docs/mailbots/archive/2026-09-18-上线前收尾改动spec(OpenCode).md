# 上线前收尾改动 · spec（给 OpenCode）

> 日期：2026-09-18 ｜ 编写：芙蕾雅 ｜ 交付对象：OpenCode ｜ 来源：小叽第二轮复验（T2/T3/T4/T5）+ 洋 2026-09-18 拍板
> 前置阅读：`AGENTS.md` §6（含新增 §6.1 退舱约定）、`WORKFLOW.md` §3–§5
> **本轮共五组改动（A / B / C / D / E）**，分**两批**交付：
> - **第一批（可立即开工）**：**A、B、D、E 四组**，一个分支、**四个 commit**（便于单独 revert）。
> - **第二批（独立批次）**：**C 组**，**有外部前置** —— 需小叽先在 WeComBot 加 `/api/notify` 端点（见 C.1 与给她的回执 §3-A）。**端点就绪前不要落码**。
> **不要顺手改别的。**

---

## 0. 总则（先看这段）

**纪律四铁律**：① 不瞎改已验收通过的部分 ② 不 commit / 不 push ③ 做完先自测并**贴原始输出** ④ 如实报告、不替洋拍板。

**不得回退（已验收，动它们就是回归）**：12 条轮询拓扑、UID 水位线推进条件（只在"成功/已入队"时推进）、`UID STORE`/`UID FETCH` 语义、`process_email` 返回布尔契约、`decide_tracing` 兼容 list 与单个、`data/` 自建、`FORWARD_SINCE` 门禁。

**跑测试的唯一方式**（这台机器）：
```powershell
$PY = "C:\Users\Roke8x\.workbuddy\binaries\python\versions\3.13.12\python.exe"
& $PY -m pytest -q -p no:cacheprovider
```
⚠️ 该解释器带安全删除垫片，全量约 160 秒；若"跑到 100% 却无汇总行 + EXIT=1"，先清 `%TEMP%\pytest-of-Roke8x` 再跑 —— **那不是测试失败**。

**通用要求**：每条新增/修改逻辑都要有**真行为断言**的用例（不是只断言"函数被调用了"），并且**验证过是真锁**（把实现改回旧行为 → 用例必须挂；验证过程不得改动源文件或改完必须字节还原）。

---

## A 组 · T2 公司收件人初始化（数据准备脚本，上线阻断项）

### A.1 背景（已核实）

- `mailbots_next` 用**它自己的** `mailbots_next/data/bot_config.db`（`BOT_CONFIG_DB_PATH`，`settings.py:10`）。
- 代码里只有 `scope='owner'` 的种子（8 家负责人），**没有任何 `scope='company'` 初始化**；`get_recipients()`（`store.py:124`）**只读、无回退**，查不到就 `return [], []` ⇒ `no_route` ⇒ **一封都转不出去**。
- 生产 `yxo.db.bot_config` 现有 **7 行** `scope='company'`（`bot='shared'`/`source='cached'`）：东盟 / 中欧木业 / 保时达 / 同程配 / 太平洋 / 沙坪坝 / 港九港铁。
- 第 8 家「联运」**不在任何库里**，但**洋 2026-09-09 已确认真值**，写在 `docs/2026-09-09-联运改造-数据灌入执行单.md`（第 46-53/96-98 行）——**洋 2026-09-18 拍板：沿用该真值**。

### A.2 要求

新增 **`scripts/seed_company_recipients.py`**（独立脚本，`--dry-run` 为默认，`--apply` 才写）：

1. **镜像 7 家**：只读打开生产 `yxo.db`，`SELECT bot, scope, key, to_addrs, cc_addrs FROM bot_config WHERE scope='company'`，
   **按 `key` 合并**多个 bot 的 to/cc（并集 + 去重 + 保序）；写成 `bot='all'`, `scope='company'`, `key=公司名`，
   `to_addrs`/`cc_addrs` 为 **JSON 数组文本**，`extra='{}'`。
2. **插入联运（8 家补齐）**：常量写死并注明出处，**不是占位**：
   ```python
   # 洋 2026-09-09 确认（非占位）；出处 docs/2026-09-09-联运改造-数据灌入执行单.md §2.2
   LIANYUN_TO = ["gongqilin@cqjzxly.cn", "yuyanling@cqjzxly.cn",
                 "jjb@cqjzxly.cn", "wanglu@cqjzxly.cn"]
   LIANYUN_CC = ["3841559246@qq.com"]
   ```
3. **幂等且绝不覆盖已有行**：本地表有 `UNIQUE(bot, scope, key)`（`store.py:69`）⇒
   ```sql
   INSERT INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra, updated_at)
   VALUES ('all','company',?,?,?,'{}',datetime('now'))
   ON CONFLICT(bot, scope, key) DO NOTHING
   ```
   ⇒ 人工在库里改过的收件人**永远不会被脚本冲掉**。要强制刷新必须显式 `--force`（覆盖前打印将被改的旧值）。
4. ⚠️ **本地表没有 `source` 列**（生产有）⇒ **INSERT 不得带 `source`**，也不得给本地表加列。
5. 打印摘要：每家 `key / to 数 / cc 数`、跳过（已存在）几家、写入几家。**邮箱按脱敏口径打印**（`a***@domain`）——本仓日志规范。

### A.3 启动自检（放进 `mailbots_next/core/store.py` 的 seed 区）

`seed_owner_mapping()` 旁边加一个**只读**自检（**不要**在启动期写 company 行）：
若 `SELECT COUNT(*) FROM bot_config WHERE bot='all' AND scope='company'` = 0
⇒ `_log.error("No company recipients configured — 所有邮件将判 no_route。请跑 scripts/seed_company_recipients.py --apply")`。

### A.4 验收判据

- 在**临时库**上跑脚本：`get_recipients('沙坪坝')`、`get_recipients('联运')` 等 **8 家均返回非空 to**；
- **连跑两次结果完全一致**（幂等），第二次全部计入"跳过"；
- 手工把某家 `to_addrs` 改成别的值 → 再跑脚本（不带 `--force`）→ **该值不变**；
- 带 `--force` → 该值被覆盖回镜像值（并在日志里打印旧值）；
- 启动自检：company 行数为 0 时**必有 ERROR 日志**（用例锁住）；
- 新增用例：`mailbots_next/tests/test_seed_company_recipients.py`（用临时 db，勿碰真库）。

---

## B 组 · T3a 企微通知"绝不静默失败"

### B.1 现状（已核实）

`core/notify.py:62` `from wecombot.cs_bot.wecom_api import notify_by_name` 失败时**只** `_log.warning("WeCom client not available")`，`_notify_by_name = None`；`notify()` 于是返回 `False`，而 **6 处调用方全部忽略返回值**（`bounce.py:210/226/236`、`extractors/draft.py:109`、`extractors/dsk.py:76`、`ingest.py:491/510`）⇒ **出事了没人知道**。

### B.2 要求（不依赖 C 组走哪条路，独立可做）

在 `core/notify.py` 这个**单一咽喉**上留痕（不必改 6 个调用方）：

1. 客户端不可用（缺配置 / 构造失败）：启动时 `_log.error` **一次**（模块级 flag 防刷屏），而不是 warning；
2. `notify()` / `send_program_error()` 在"发不出去"的每条返回路径上：
   `_log.error(...)`（含 notify_type 与脱敏收件人）+ `increment_counter("notify_failed")`，再 `return False`；
3. 保持**返回值语义不变**（仍是 bool），不做重试、不抛异常（不能因为企微挂了阻断转发主链路 —— 这条是铁律）；
4. `mailbots_next/README.md`「已知限制 1」更新为**当前事实**（说明：不可用时是 loud fail + `notify_failed` 计数，运维用计数看）。

### B.3 验收判据

- 用例：monkeypatch 让客户端构造失败 ⇒ 断言 **ERROR 日志出现** + `notify_failed` 计数 +1 + 返回 `False`；
- 用例：正常路径 ⇒ 计数**不增加**；
- **真锁**：把 `increment_counter` 那行删掉 → 用例必挂。

---

## C 组 · T3b：`mailbots_next` 改走 **HTTP 调 WeComBot**（洋拍板选 ①；**独立批次**）

> ⚠️ **本组有外部前置**：需要小叽先在 `WeComBot` 加 `/api/notify` 端点（说明见给她的回执 §3-A）。
> **端点就绪前不要开始落码**；但可以先按本文把代码与单测写好（单测用 HTTP 桩，不依赖端点）。

### C.1 为什么是 ①（背景，供理解，不必实现）

- 必须去掉对旧包 `wecombot` 的 import：它靠 `sys.path.insert` 把 `from config import` 解析到 `wecombot/config.py`，脆弱且已踩过。
- 洋 2026-09-18 定：**企微逻辑只留在 WeComBot 一处**。`mailbots_next` 只发一个 HTTP 请求 ——
  这样**双通道（微信客服 → 应用消息）、token 缓存、1800 字节分段、错误码处理、客服绑定表**全部留在 WeComBot，
  `mailbots_next` 不必复制这 300+ 行，也不用在两份代码里各维护一套企微实现。**职责边界比"代码依赖解耦"更重要。**

### C.2 要求（`mailbots_next` 侧**只做 HTTP 客户端 + loud fail**）

新增 **`mailbots_next/core/wecom_notify.py`**（薄客户端，目标 **~60 行**）：

1. **调用**：`POST {WECOM_NOTIFY_URL}`，JSON body：
   ```json
   {"name": "<同事真名>", "text": "<通知正文>", "try_kf": true}
   ```
   请求头带 `X-Notify-Token: <WECOM_NOTIFY_TOKEN>`。
2. **配置**（gitignored 的 `secrets.json` 或环境变量，**环境变量优先**）：
   `WECOM_NOTIFY_URL`（生产默认 `http://127.0.0.1:5001/api/notify`）、`WECOM_NOTIFY_TOKEN`。
   **缺任一 ⇒ loud fail**（按 B 组口径），不要瞎填。
3. **超时**：connect 3s / **read 45s**；**不重试**（通知是"喊人"，快速失败比慢慢重试更有价值，重试只会把告警延迟拉长）。
   ⚠️ **read 必须 ≥45s，不是 5s** —— 2026-09-20 生产实测修正：`try_kf=true` 时 WeComBot 侧会先走微信客服通道、**耗时 35.8 秒**才失败回退到应用消息，然后才返回 `200`。
   若 read 设成 5s，**白名单内同事的每一条通知都会变成"我们这边报超时失败、对方其实收到了"**（误报 + 无谓的 `notify_failed` 计数）。
   设 45s 不会让其他人变慢：白名单外的人走 `try_kf=false` ⇒ WeComBot 直接发应用消息、秒回，45s 只是**上限**、不是实际等待。
   （也可按 `try_kf` 分档：`false` ⇒ 5s、`true` ⇒ 45s；统一 45s 更简单，且不产生额外等待。）
4. **成功判据**：HTTP **200 且** body `{"ok": true}`。其余（超时 / 连接失败 / 401 / 非 200 / `ok=false`）
   ⇒ `_log.error` + `increment_counter("notify_failed")` + 返回 `False`（即 B 组的 loud fail 口径）。
   失败日志只记**状态码 / 异常类型 / channel**，**不记 token、不记正文全文**。
5. **收件人**：`notify.py` 现在按**邮箱**指定收件人 ⇒ 用它现成的 `_WECOM_NAME_BY_EMAIL` 转成**真名**再发
   （企微侧按姓名路由；那张表已是本仓唯一允许出现真名的位置）。
6. **`try_kf` 由配置决定**：新增 **`WECOM_KF_EMAILS`**（邮箱白名单）—— 放在 **`mailbots_next/secrets.json`**（与 `WECOM_NOTIFY_URL` / `WECOM_NOTIFY_TOKEN` 同一处，便于一起管理），同名环境变量可覆盖。
   ⚠️ **它不是热生效的**：`config/secrets.py:9/18-21` 有进程内缓存（`_SECRETS_CACHE`）⇒ **改完要重启 `mailbots_next`**（与 WeComBot 的行为一致）。
   邮箱**在该名单内 ⇒ `try_kf=true`**，否则 **`false`**。
   目的：**不需要微信直收的同事不再触发微信客服通道** —— 那条通道对"48 小时内没和客服说过话"的人必然失败，
   现网每次要**重试 5 次、约 30 秒**才回退（**2026-09-20 生产实测：默认双通道单次耗时 35.8 秒**，
   而同轮 `try_kf=false` 的请求是秒回）；白名单机制让这部分人**一次请求就直达应用消息**。
   ⚠️ **用白名单必须先知道代价**：进了白名单的人，**每条通知都要先等微信客服那条约 30 秒的失败路径**，
   才落到应用消息。而运维告警的收件人 `OPS_OWNER_EMAIL` **只有一个**（当前就是毛骁洋），
   通知又是**同步调用**、其中一部分正落在收信/处理链路上（`ingest.py:506/525`、`serve.py:443/449`）
   ⇒ **白名单里放谁，约等于"谁的每条告警都要拖 30 秒"**。
   ⇒ **白名单取值由洋定**；在他确认"微信客服通道对他是稳定可用的"（即他会持续与客服保持会话）之前，
   **建议先留空** = 所有人 `try_kf=false`（走应用消息、秒发）。留空不是"不做双通道"，而是"暂时不启用慢通道"。
   ⚠️ 该设计**不改变 WeComBot 既有调用方**（它们不传 `try_kf`，端点按原样走双通道）。
7. `notify.py` 删除 `from wecombot...`，**不再 import 旧包**；`get_notifier()` 的对外接口保持不变
   （`notify()` / `send_program_error()` 的签名与返回值语义不变）。

> 📌 **给端点实现者的两条事实（`WeComBot` 侧，已实核 2026-09-18，别猜）**：
> 1. **`WECOM_USER_MAP` 的方向是 `{UserID: 姓名}`**（键 = 企微 UserID，如 `MaoXiaoYang`；值 = 中文姓名）—— 见 `config.py:96-101`。
>    它**没有反向索引** ⇒ 按姓名查 UserID **必须反向查找**：
>    `uid = next((k for k, v in WECOM_USER_MAP.items() if v == name), None)` —— WeComBot 自己的 `cs_bot/wecom_api.py:318` 就是这么写的。
>    ⚠️ **不要写 `WECOM_USER_MAP.get(name)`（或 `.get(中文姓名)`）—— 那永远返回 `None`。**
> 2. **`wecom_api.notify_by_name(name, text)` 返回二元组 `(ok: bool, channel: str)`**，`channel` 取值为 `"kf"` / `"app"` / `""`（见 `cs_bot/wecom_api.py:307-321` 的 docstring 与三处 return）。可直接 `ok, channel = notify_by_name(...)`。
> 3. 并发无隐患：生产用 **waitress `threads=8`**（`server.py:311`），Flask 回退分支的 `app.run` 自 1.0 起默认 `threaded=True` ⇒ 多条轮询线程同时调用**不会串行排队**。

### C.3 不得做的事（别把 ① 做成 ③）

- **不要在 `mailbots_next` 里实现**企微 token / 双通道 / 微信客服 / `external_userid` / 1800 字节分段 —— 那是 WeComBot 的职责。
- **不需要** `corp_id` / `agent_id` / `secret` / `open_kfid` / `KF_SECRET` / 客服绑定表 —— 这些**都不进 `mailbots_next`**。
- **不 import `wecombot`、也不 import WeComBot 的任何模块**（只走 HTTP）—— 否则又回到 `sys.path` 地雷。

### C.4 验收判据

- 用例（**全部用假 HTTP 服桩，禁止打真企微**）：
  1. 成功：桩返回 `200 {"ok": true}` ⇒ 返回 True；并断言请求体里 `name` 是**真名**（由邮箱转来）、`try_kf` 按白名单取值；
  2. `401` / 非 200 / `ok=false` ⇒ False + `notify_failed` 计数 + ERROR；
  3. 连接失败 / 超时 ⇒ False + 计数 + ERROR，**且不重试**（断言只调 1 次）；
  4. 配置缺失（URL 或 token 未配）⇒ loud fail，**且不发起请求**；
  5. 邮箱在白名单内 ⇒ `try_kf=true`；不在 ⇒ `try_kf=false`；
  6. 邮箱在 `_WECOM_NAME_BY_EMAIL` 里查不到 ⇒ 不静默（ERROR + 计数 + False）；
  7. **失败日志里不含 token、不含正文全文**（对日志文本断言）。
- **真锁**：把 `try_kf` 改成恒 `true` ⇒ 用例 5 必挂；把 `increment_counter` 删掉 ⇒ 用例 2/3 必挂。
- `grep -rn "wecombot" mailbots_next/ --include=*.py`（排除 tests）= **0**；
- 全量套件 0 failed。

### C.5 ✅ 端点已就绪（2026-09-20）—— 验证分两部分，来源分开列

生产 `D:\YXO_DATA\WeComBot\` 已加好端点并重启（旧 PID 7316 → 新 PID 5912）。

**A. 芙蕾雅独立只读验证（我自己跑的）**

| 验证 | 结果 |
|---|---|
| `GET /health` | `200`（服务在跑） |
| `POST /api/notify` **不带 token** | `401 {"msg":"unauthorized","ok":false}` —— 鉴权在业务之前 ⇒ **该请求不会触发任何消息发送** |
| `POST /api/notify` **错误 token** | `401`（同上） |
| 生产 `server.py:295-301` 实现 | 与本 spec §C.2 的要求**逐字一致**（含 `hmac.compare_digest`） |
| `import hmac` / `NOTIFY_API_TOKEN` | 均已就位（`server.py:10`、`config.py:38`、`server.py:33` 的 import 列表） |

**B. 运维侧实测（小叽在服务器上执行，数据来自她的回执 —— 非我实测）**

| 验证 | 结果 |
|---|---|
| 空 text / 未知姓名 | `400 bad request` / `400 unknown name` |
| `try_kf=false` | `200 {"ok":true,"channel":"app"}`（**秒回**） |
| 默认（双通道） | `200 {"ok":true,"channel":"app"}`，**耗时 35.8 秒** ← 即 §C.2 第 3 条把 read 改成 45s 的直接依据 |
| 给毛骁洋发了 2 条「【联调测试】」消息 | **待他本人确认是否收到**（未确认前不算验证通过） |

⇒ **C 组前置已满足，可以开始落码。**

---

## D 组 · T4 连接复用（降低登录频率）+ T5 七项清理

### D.1 T4 连接复用（洋拍板选 a）

**现状**（`core/ingest.py` `_poll_loop`）：`conn = self._connect()` 在 `while` 内，每轮结束 `finally: close(); logout()`
⇒ 每 30 秒一轮全量重连：**12 条 × 30s = 24 次登录/分钟 ≈ 3.5 万次/天**（原 IDLE 设计是 29 分钟一次，放大约 58 倍）。阿里云企业邮箱对登录频率有风控。

**要求**：一轮处理完**保留连接**，下一轮复用；仅在异常时重连。

⚠️ **这不是"把 close/logout 挪出去"就完了**，四条必须一并实现（漏任何一条都是新的事故）：

1. **保活**：`poll_secs=30` 时每轮本来就有 `EXAMINE` + `UID SEARCH` 命令 ⇒ 天然保活；但仍需处理**服务端主动断连**；
2. **断连识别**：捕获 `imaplib.IMAP4.abort` / `IMAP4.error` / `OSError` / `socket.timeout` ⇒ 丢弃连接 → 下轮重连。
   **绝不允许**把"连接已死"当成"这轮没有新邮件"（那就是变相的空转，正是 IDLE 事故的形态）；
3. 🔴 **每轮必须 `select`，且 `UIDVALIDITY` 必须取自"本轮 select 之后"的响应**：值与水位文件里记的不一致 ⇒ 走 `FORWARD_SINCE` 门禁重扫（**不是 `UID 1:*`**）。
   漏掉这条 = **把历史邮件批量转发给客户**。这是本组最高危的一条。
   ⚠️ **实现事实（`ingest.py:258-274`，别猜）**：`UIDVALIDITY` **不在 `select` 的返回值里** —— 现有代码用 `_get_uidvalidity(conn)` 从 **`conn.untagged_responses["UIDVALIDITY"]`** 读，读不到再退 `conn.response("UIDVALIDITY")`，**再读不到就返回 `None` ⇒ 保守重扫**（`:378-381`）。调用点在 `_process_new_messages` 里，**每轮每文件夹都会调**。
   ⇒ 所以真正的风险**不是"忘记重读"，而是"连接复用后图省事跳过 `select`"** —— `untagged_responses` 会**残留上一轮的响应**（甚至是别的文件夹的）⇒ `UIDVALIDITY` 判错 ⇒ **该重扫时不扫（漏信）**。
   ⇒ **硬约束：复用连接时，每轮仍必须对每个文件夹执行 `select`，且 `_get_uidvalidity` 必须紧跟在本次 `select` 之后调用**（中间不得插入其它会刷新/污染 `untagged_responses` 的命令）。
4. **断线期间水位不动**（沿用"只在成功/已入队时推进"）。

**验收判据**：
- 连续 N 轮（≥3）只出现 **1 次 `LOGIN`**（用假 IMAP 服务器，统计协议命令）；
- **每轮仍各有 1 次 `EXAMINE`（select）**（防止"复用连接"被顺手做成"跳过 select"）；
- 新增用例：服务端在轮间**主动断开** ⇒ 客户端自动重连、**不丢邮件**、且日志能看出"重连"而非"无新邮件"；
- 新增用例：重连后 `UIDVALIDITY` 变化 ⇒ 重扫起点受 `FORWARD_SINCE` 约束；
- 新增用例（**锁"响应新鲜度"**）：让假服务器在两次 `select` 之间返回**不同的 `UIDVALIDITY`** ⇒ 断言客户端用的是**本次 select 之后**的值（`_get_uidvalidity` 被调用时 `untagged_responses` 已被本轮 select 刷新），且 `untagged_responses` 里若没有 UIDVALIDITY ⇒ 走**保守重扫**而不是沿用旧值；
- 真链路用例（`test_true_link_smoke_tls_proves_callback`）仍绿。

### D.2 T5 七项清理（逐条，含洋的取舍）

| # | 项 | 怎么做 |
|---|---|---|
| 1 | 每轮 `EXAMINE` **两次** | `_poll_loop` 选一次、`_process_new_messages` 里又选一次（`ingest.py:362` 与 `:567`）。**去掉一次**：把"已选中"作为前置条件，让 `_process_new_messages` 不再自己 select。 🔴 **与 D.1 第 3 条强耦合，必须一起保证**：`UIDVALIDITY` 是从 `conn.untagged_responses` 读的（见 D.1），所以**去掉的必须是"后一次"，并且 `_get_uidvalidity(conn)` 必须紧跟在剩下的那次 `select` 之后**；**绝不能为了去重而让某一轮完全没有 `select`**（那会让 `untagged_responses` 变成陈旧值 ⇒ UIDVALIDITY 判错 ⇒ 漏信）。 |
| 2 | `--once` 是空参数 | **实现它**（洋拍板"实现"，不删）：语义 = **顺序跑一轮就退出**（不 `start()` 线程、不循环），便于部署自检。`serve.py:788` |
| 3 | `IngestManager` 死代码 | **删除**（`ingest.py:582` 起）。删前确认全仓零引用（含 `core/__init__.py` 导出、tests；有引用就一并清理） |
| 4 | `get_idle_groups()` 命名残留 | **改名 `get_folder_groups`**（`config/provider.py:138`），同步 `config/__init__.py` 的导入与 `__all__`、`serve.py:30`、以及测试引用。**不要留别名**（命名残留的教训就在 `ingest.py` 那句注释里） |
| 5 | `_last_outcome` 是实例属性 | 12 条轮询线程共享同一实例并发写（`serve.py:73`）。改 **`threading.local()`**，**行为不变**（sweeper 仍能拿到自己那次的值） |
| 6 | 空邮箱每轮"全量搜索" | **洋拍板：登记不改**。在 `mailbots_next/README.md` 技术债表加一行说明即可 |
| 7 | 每封邮件整份读写水位 JSON | 改为**整轮落盘一次**：`_advance_watermark` 只更新内存 + 标 dirty，轮末统一 flush（原子写不变）。 ⚠️ spec 已确认语义：**崩在中间 ⇒ 水位不推进 ⇒ 下轮重复处理 ⇒ 由 dedup 兜住（不会重复转发）**；**漏信才危险**，所以这个方向的失败是安全的 |

**验收判据**：全量 0 failed；`grep -rn "IngestManager\|get_idle_groups" mailbots_next/ --include=*.py` = **0**（生产代码）；`--once` 有一条用例断言"跑一轮即退出、且处理了该轮发现的邮件"；水位落盘次数用例（单轮 3 封 ⇒ JSON 只写 1 次）。

---

## E 组 · J/K：`import_excel.py` 的退舱 / 软删守卫（洋拍板：**两个都加**）

### E.1 现状（已核实，是真缺口）

`import_excel.py:221-225`（**现行**网页端导入入口，`app.py:22` 在调）：

```python
exist = conn.execute('SELECT id FROM records WHERE "客户编码"=?', (key,)).fetchone() if key else None
if exist:
    conn.execute(f"UPDATE records SET {sets} WHERE id=?", ...)   # 覆盖全部 BASE_FIELDS
```

- **无 `状态<>'退舱'` 过滤** ⇒ 命中退舱记录即覆盖其字段（违反 `AGENTS.md` §6.1 铁律 2）；
- **无 `is_deleted=0` 过滤** ⇒ 连软删记录也会被覆盖（顺带把软删行"复活"）；
- 不写 `update_log` ⇒ **静默**，事后查不出。

**风险已实测**：`CQWLJT260923001-D` 有**两条客户编码完全相同的记录**（id1827 正常 / id2433 退舱），`fetchone()` 无 `ORDER BY` ⇒ 当前侥幸命中正常那条，**属未定义顺序**。

### E.2 要求（洋拍板 K：**两个过滤都加**）

把那条 SELECT 改为：

```sql
SELECT id FROM records
 WHERE "客户编码"=?
   AND COALESCE("状态",'')<>'退舱'
   AND COALESCE(is_deleted,0)=0
 ORDER BY id LIMIT 1
```

- 命中不到 ⇒ 走原有 **INSERT 新增**分支（**这是洋确认的行为变更**：软删记录不再被复活，改为新增一行）；
- `ORDER BY id` 明确"取最早那条"，消除未定义顺序；
- **不要**顺手改 `analyze_import`（`app.py:1371` 用的预览路径）的逻辑，除非它也用同一段 SQL —— 若共用，一并改并说明。

### E.3 验收判据

- 用例（临时库，勿碰真库）：① 造一条**退舱**记录 + 一条**软删**记录，导入同客户编码的 Excel 行 ⇒ 断言**两条都未被改动**（逐字段比对）+ **新增了 1 行**；
- 用例：正常记录仍然被"更新"（不回归）—— 覆盖行为对正常行保持不变；
- **真锁**：把两个过滤删掉 → 前一条用例必挂（并把验证过程与原始输出贴回）；
- 全量 0 failed。

---

## 交付要求（逐条回给芙蕾雅）

1. 每组改动的**原始输出**：跑测试的汇总行、新增用例的 `-v` 输出、**真锁验证**（改回旧行为必挂）的原始输出；
2. `git status` 改动的文件清单（**不得出现本轮范围外的文件**）；
3. **如实报告偏差与疑问**，不替洋拍板；本 spec 未覆盖的一律**不做**，先问；
4. **不要 commit、不要 push**；
5. **C 组属第二批**：端点就绪前**不要动**；做的时候请在报告里附上"端点已就绪"的证据（例如一次真实 `curl` 调用的原始返回）。

## 附：本轮不需要做的事（避免越界）

- 不改 `mailbots/`（旧系统）、不改 `app.py` 的网页端手工编辑/删除路径（洋 9-18 拍板 **L 不加守卫**：其他同事的账号看不到这些记录，等后续账号按密码分开、权限完善后自然不存在）；
- 不改 `manifest_engine.py`（其退舱剔除已落地）；
- 不动 `dev` 分支、不动生产目录、不碰生产库。
