# 发送后自检（双重保险）：已发送 + QQ 旁路

状态：**设计稿 v2（2026-10-29 讨论 + 实测 + ds 复核，待洋拍 §12 两条后出任务书）**
撰写：芙蕾雅 ｜ 日期：2026-10-29 ｜ 适用：`mailbots_next`（老 `mailbots/` 不在范围）

> 本文是**自包含**的：不依赖任何对话记录，只靠本文即可继续设计/实施。
> 文中的每一条「事实」都标了验证方式；§3.7 的结论全部来自 2026-10-29 16:16 的实测。

**v2 相对 v1**：补 5 条实施细节（路 A 凭据来源、失败粒度按账号、`SKIP_*` 计数、存量处理、
A_ONLY 降噪 + 收件人分级），并把「QQ 文件夹白名单」改成**动态枚举**（消除故障模式，
比事后告警更根本）。**架构未变。**

**v1 相对 v0 的改动（重要，别照 v0 实施）**

| # | v0 的说法 | v1 实测结论 |
|---|---|---|
| 1 | 要给 `forward_log` 加 `out_msg_id` | ❌ **不加**。外发信早就带 `X-YXO-Forward-Id`（= `forward_log` 主键），两侧实测都保留 |
| 2 | 「轮末 + 内存队列」 | ❌ 新系统没有「轮」；改 **DB 驱动 + sweep 线程**，加 `checked_at` |
| 3 | 「已发送 / QQ 按 `Message-ID` 精确匹配」 | 改为按 **`X-YXO-Forward-Id`** 精确匹配；`Message-ID` 降级为可选合规项（附录 A） |
| 4 | 未评估 IMAP 服务端搜索能力 | 🔴 **四个坑**（见 §3.7），直接决定实现写法 |
| 5 | tracing 「待定」 | 洋 2026-10-29 拍板：**不纳入** |

---

## 1. 要解决什么

现在机器人把邮件转发出去后，**没有任何机制确认它真的出去了**。
可能出现的情形：

| 情形 | 现在能发现吗 |
|---|---|
| SMTP 抛异常 | ✅ 有（写 `errors` 表 / `forward_failed`） |
| SMTP 返回成功，但邮件没出去（中继丢弃、策略拦截） | ❌ **完全不知道** |
| 转给了错的公司 | ❌ 不知道（判定层面另有 `box_reuse` 告警，但发送层面无感） |
| 部分收件人没收到 | ❌ 不知道（除非退信 NDR，由 `bounce.py` 覆盖） |

本功能要做的：**每封转发结束后，主动去两个地方核对「这一封到底出去了没有」，缺失就告警。**

---

## 2. 目标与非目标

**目标**

1. 对「决定转发且 SMTP 返回成功」的每一封，能在两路独立证据里找到它。
2. 任一路缺失 → 企微告警给**对应公司负责人 + 毛骁洋**，且同一封只报一次。
3. 只增加读取动作，不改变现有发送链路。

**非目标**

- ❌ 不保证「客户真正看到」（垃圾箱、对方白名单、延迟阅读）。这一层只能靠退信（NDR）。
- ❌ 不重发、不自动修复。发现缺失只告警，处置由人决定。
- ❌ **不覆盖 tracing**（洋 2026-10-29 拍板）。tracing 量大、扇出多，先只覆盖 `atb` / `dsk` / `draft` / `waybill`。
- ❌ 不覆盖「发了但没记账」的信（见 §2.1 盲区说明）。

### 2.1 🔴 结构性盲区（必须写在文档里，别让后人以为「没报警 = 一定发出去了」）

候选集来自 `forward_log`，而 `_record_forward` 失败时（`act.py:602-611`，已有
`post_send_bookkeeping_failed` 计数器）这封信**已经发出去了却没有日志行** ⇒ 自检**永远核不到它**。

> **自检的覆盖率上限 = `forward_log` 的完整率。**
> 兜底只有那个计数器；若某天 `post_send_bookkeeping_failed > 0`，说明当天的自检有盲区。

---

## 3. 现状事实（全部实测）

### 3.1 已发送里有机器人副本 —— 阿里服务端自动存档

实测 4 个账号（`maoxiaoyang` / `yangyawen` / `fengqian` / `hanwenhao`）的 `\Sent`（IMAP 名 `&XfJT0ZAB-`）：

| 头部 | 机器人发的副本（老系统） | 人工从网页发的 |
|---|---|---|
| `Date` | **无** | 有 |
| `Message-ID` | **无** | 有 |
| `X-Mailer` | **无** | `[Alimail-Mailagent …]` |

⇒ 服务端把 SMTP 发件按**原始字节**存进「已发送」，**不需要我们 APPEND**。

### 3.2 🔑 新系统的外发邮件已经带唯一键 `X-YXO-Forward-Id`

**代码事实**（不是推测）：

| 位置 | 内容 |
|---|---|
| `act.py:92` | `build_forward_message()`：`msg["X-YXO-Forward-Id"] = forward_id`（单目标路径） |
| `act.py:494` | `_forward_split()`（草单/运单扇出）：`part["msg"]["X-YXO-Forward-Id"] = sub_fid` |
| `act.py:526` | DSK/ATB 扇出走 `build_forward_message(..., forward_id=sub_fid)`，同样带该头 |
| ENVELOPE | `_envelope_from()` 在 VERP 模式下把这个 id 放进 `MAIL FROM`（`bounce+<fid>@…`） |

而这个值**就是 `forward_log.forward_id` 主键**（`store.py:301`）——已经是 NDR 关联
（`bounce.py`）和 VERP 的键。

**实测（2026-10-29 16:16–16:30，测试信见 §3.7）**：

| 位置 | `X-YXO-Forward-Id` | `Message-ID` | `Date` |
|---|---|---|---|
| 阿里 `\Sent`（seq 3687） | ✅ 原样 `6b9d0b6c8008e957-a1ec5a26` | ✅ 原样 | ✅ 原样 |
| QQ `INBOX` | ✅ 原样（字符串精确相等） | ✅ 原样 | ✅ 原样 |

⇒ **匹配键不用新建**：直接用 `forward_log.forward_id` 比对外发信上的 `X-YXO-Forward-Id`。

### 3.3 QQ 测试邮箱现在就在收副本（靠 CC，不是 BCC）

`yxo.db bot_config` 的 `scope='company'` 8 行，`cc_addrs` **全部包含 `3841559246@qq.com`**：

| 公司 | to | cc |
|---|---|---|
| 太平洋 | zhaoguofu@…, pacific-opt5@… | **3841559246@qq.com** |
| 同程配 | zhoujiyang@… | **3841559246@qq.com** |
| 保时达 | pyx@…, xyli@… | **3841559246@qq.com** |
| 东盟 | hgn@…, zcl@…, hy@… | **3841559246@qq.com** |
| 中欧木业 | 109391245@… | wangjianquan@…, **3841559246@qq.com**, nadya@… |
| 沙坪坝 | bingo.leeht@… | 185405359@…, **3841559246@qq.com** |
| 港九港铁 | chenkai@… | **3841559246@qq.com** |
| 联运 | gongqilin@… 等 4 个 | **3841559246@qq.com** |

⇒ **第二路证据链现在就在跑，只是没有程序去比对。本功能不需要修改发信代码加 BCC。**

⚠️ 附带事实：这是 **CC**，收件客户在抄送栏里能看到 `3841559246@qq.com`。
洋 2026-10-29 明确：**不改成 BCC**（保持现状）。本功能不涉及此改动。

### 3.4 QQ 侧结构与归档规则（核对必须跨文件夹）

| 文件夹（IMAP 名） | 封数（2026-10-29） | 内容 |
|---|---|---|
| `INBOX` | 181 | 各类型副本混放 |
| `&UXZO1mWHTvZZOQ-/DSK`（其他文件夹/DSK） | 32 | DSK 副本 |
| `&UXZO1mWHTvZZOQ-/ATB` | 65 | ATB 副本 |
| `&UXZO1mWHTvZZOQ-/tracing` | 3320 | tracing 副本（**不在本功能范围**） |
| `&UXZO1mWHTvZZOQ-/&g0lTVQ-`（运单草单） | 404 | 客户回复为主 |

洋在 QQ 侧建了归档规则 ⇒ **同一封可能在核对时还在 INBOX、几秒后被移走**
⇒ 核对**必须跨文件夹查**，否则归档延迟会产生假缺失。

⚠️ 另实测：`Sent Messages` / `Deleted Messages` 用 `EXAMINE(readonly=True)` 会报
`[CLIENTBUG] EXAMINE parameters`（腾讯拒绝）⇒ **只查白名单 5 个文件夹**，不要遍历全部。

### 3.5 延迟实测

| 项 | 结果 | 时间 |
|---|---|---|
| SMTP 接收 | 0.9 s | 2026-10-29 10:26 |
| **到达 QQ 邮箱** | **7.9 s** | 同上 |
| **已发送出现副本** | **< 21 s** | 同上 |
| 另一封（16:16:37 发出 → 16:16:59 收） | ≈ 22 s | 2026-10-29 16:16 |

⇒ 核对延迟初值取 **120 s**（洋 2026-10-29 拍板），约实测的 5~15 倍余量。
**不做二次复查**——120 s 直接定案。

### 3.6 转发量级（决定核对成本）

`MailBots/data/forward_log.db` 近 10 天：**3~11 封/天**，平均约 6 封；
历史 382 条（草单 204 / 运单 121 / ATB 42 / DSK 15）。
⇒ 一天 6 次核对、每次窗口内几十封邮件头，成本可忽略。

### 3.7 🔴 IMAP 能力实测：四个坑（决定实现写法，不能想当然）

测试信：`maoxiaoyang@cqtransit.com → 3841559246@qq.com`（**To only，无 Cc**），
`X-YXO-Forward-Id: 6b9d0b6c8008e957-a1ec5a26`，
`Message-ID: <179326181921.34792.14333155169944487338@cqtransit.com>`，
`X-YXO-Src-MsgId: <probe-src-ee1c736bfa91@cqtransit.com>`，2026-10-29 16:16:59 到达。
复现脚本：`\\10.0.199.184\yxo_data\芙蕾雅_开发沙盒\工具脚本\xheader_probe.py`。

| # | 坑 | 实测证据 | 对策 |
|---|---|---|---|
| **P1** | `SEARCH HEADER <自定义头>` 两侧都不可信 | 阿里：查 `X-YXO-Forward-Id 6b9d…` → **0 命中**（该信确实在）。腾讯：同样查询 → **返回全部 181 封**（等于忽略条件） | 🚫 **禁用** `SEARCH HEADER` 搜自定义头 |
| **P2** | 阿里 `HEADER.FIELDS` 请求自定义头被**静默忽略** | `(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT X-YXO-FORWARD-ID)])` 返回的 raw **只有 Subject + Date**，与只请求 `(DATE SUBJECT)` 完全相同；`BODY.PEEK[HEADER]` 全头才看得到 | 一律拉**全头** `BODY.PEEK[HEADER]` 本地解析。腾讯侧 `HEADER.FIELDS` 正常，但为了同一份代码，两边都用全头 |
| **P3** | 腾讯 `SEARCH SINCE` **完全失效** | `SINCE 28-Oct-2026` → INBOX **181/181**、tracing **3320/3320**、DSK **32/32**（全部返回） | 腾讯侧只能「**取序号尾部 K 封**」（序号正序，越大越新，已确证）；阿里侧 `SINCE` 有效（47 / 3688），可用 |
| **P4** | QQ 侧 tracing 副本**没有 `Date` 头** | 拉 tracing 尾部 300 封，`Date` 全部解析失败（印证 §3.1「机器人副本无 Date」）；ATB 65 封 `Date` 全在 2026-07-22（一次性归档） | **不能按 Date 过滤**，只能按序号尾部 + 本地比对 id |

**抓取成本实测**（批量 `FETCH 1,2,3…` 一次拉）：

| 文件夹 | 拉取封数 | 耗时 |
|---|---|---|
| QQ `tracing` | 300 | 6.7 s |
| QQ `INBOX` | 181 | 9.4 s |
| QQ `ATB` | 65 | 4.8 s |
| 阿里 `\Sent` | 120 | 6.7 s |

⇒ 一天 6 次、每次几秒，**成本完全可接受**。`K` 建议取 **300**（tracing 约 80 封/天 ⇒ 覆盖约 3.7 天）。

---

## 4. 设计

### 4.1 数据来源：本轮要核对什么

来源 = 新系统 `forward_log`（`bot_config.db` 内表），DDL 见 `core/store.py:300-307`：

```
forward_log(forward_id PK, message_id, row_key, account, folder, uid,
            subject, sender, date_hdr, raw_hex, to_list, cc_list,
            sent_by, sent_at, bounced)
```

**只需补 3 列**（沿用文件里已有的 `ALTER TABLE ... ADD COLUMN` + `try/except` 先例，`store.py:310-314`）：

| 要补的列 | 用途 |
|---|---|
| `company TEXT DEFAULT ''` | 告警要发给「对应公司负责人」，现在表里没有公司 |
| `checked_at TEXT DEFAULT ''` | 空 = 待核对；核对后写 UTC 时间戳 ⇒ **天然去重、重启不丢** |
| `check_result TEXT DEFAULT ''` | `AB` / `A_ONLY` / `B_ONLY` / `NONE` / `SKIP_NO_ID` / `SKIP_LEGACY` / `SKIP_ERR` |

> 🔴 **不要加 `out_msg_id`**：匹配键就是主键 `forward_id`（见 §3.2）。

**存量行的一次性标记（用 `PRAGMA table_info` 判断，不要靠事务）**

```python
try:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(forward_log)")}
    for _col, _ddl in (
        ("company", "ALTER TABLE forward_log ADD COLUMN company TEXT DEFAULT ''"),
        ("checked_at", "ALTER TABLE forward_log ADD COLUMN checked_at TEXT DEFAULT ''"),
        ("check_result", "ALTER TABLE forward_log ADD COLUMN check_result TEXT DEFAULT ''"),
    ):
        if _col not in cols:
            conn.execute(_ddl)
    conn.commit()
    # 独立、幂等、可自愈：只标「一天前就存在、且从未核对过」的行
    conn.execute(
        "UPDATE forward_log SET checked_at=datetime('now'), check_result='SKIP_LEGACY' "
        "WHERE checked_at='' AND check_result='' AND sent_at < datetime('now','-1 day')"
    )
    conn.commit()
except Exception as _e:
    conn.rollback()
    _log.warning(f"forward_log 迁移未完成（不影响启动，下次启动会重试）: {type(_e).__name__}: {_e}")
```

⚠️ **`try/except` 不能省**：`init_forward_log()` 是在 `store.py` 模块底部直接调用的（import 时执行），
`ALTER`/`UPDATE` 只要抛一次（库被锁、磁盘满），异常就会从 import 阶段冒出去、**整个进程起不来**。
旁边那个老的 `try: ALTER sent_by / except: pass` 是同样的用意。

⚠️ `UPDATE` 的**幂等条件**（`sent_at < datetime('now','-1 day')`）而不是「只在本次加过列时跑」：
否则第一次 ALTER 成功、UPDATE 失败时，下次启动列已存在 ⇒ 存量行**永远标不上**，每轮白扫。

🔴 **为什么不能写成「三条 ALTER 包一个 try + 同事务」—— 实测过**（Python 3.13.14 / SQLite 3.53.1）：

| 写法 | 结果 |
|---|---|
| 默认 `isolation_level=''`，三条 ALTER 中间不 commit、`except` 里 `rollback()` | ❌ **回滚无效**。第一条执行完 `conn.in_transaction` 就是 `False` —— Python 的 legacy 事务管理**只对 INSERT/UPDATE/DELETE 自动 BEGIN，DDL 不自动 BEGIN** ⇒ 每条 ALTER 直接 autocommit 落盘，`rollback()` 是空操作 |
| 显式 `conn.execute("BEGIN")` 后再执行 DDL | ✅ 能回滚（SQLite 本身支持事务性 DDL） |

按错误写法的后果正是要防的那个：第 1 列加成功、第 2 列抛异常 → `rollback()` 什么都没撤回、
`except` 吞掉异常 → 下次启动第 1 条 ALTER 直接抛、整段跳过 → **第 2、3 列永远补不上**。

`PRAGMA` 版本不依赖事务语义：每次读现有列、缺哪补哪，天然幂等，也能修复「历史上被手工加过一列」的残缺状态。
标记存量的那句 `UPDATE` 自带 `sent_at < datetime('now','-1 day')` 条件，所以每次启动都跑、
上次半途失败也能自愈 —— **不引入任何「只跑一次」的标志变量**，代码里也没有这种判断。

> 📌 实测（2026-10-29）：生产 `mailbots_next/data/bot_config.db` 的 `forward_log` **0 行**，
> `error_queue` 表甚至不存在 ⇒ 新系统从未 live 跑过，这段迁移今天是空转。
> ⚠️ 但 **382 条历史是「老系统」`MailBots/data/forward_log.db`**，不在这一层，别把两个库混了。

**`company` 怎么取（发送时就有，不用反查）**

- 单目标路径：`routing.company`（`RoutingResult` 本来就有这个字段，`routing.py:21,58`）
- 扇出路径：`send_ctx["keys"][row.row_idx]` 的 key 本身——
  草单/运单是 `company`，DSK/ATB 是 `(company, box)`（`act.py:488,509`）

⇒ `_record_forward()` 加一个 `company` 形参，两处调用点各传一次即可。

⚠️ `sent_at` 用的是 SQLite `datetime('now')` = **UTC**。查询条件也必须用
`datetime('now', '-120 seconds')`（同为 UTC），**不要混本地时间**。

### 4.2 流程（DB 驱动，不是「轮末内存队列」）

```
① 发送成功时（act.py 现有 _record_forward 调用点）：
     多写 company；其余不变。不做任何「入队」动作。

② 调度：挂到已有的 sweep 线程（serve.py:770 run_sweeper，SWEEP_INTERVAL_SEC=300）
     ⚠️ 新系统 live 是 start_pollers 常驻轮询（INGEST_POLL_SEC=30），
        根本不存在「一轮」边界。/「轮末」这个词在本系统没有对应物。

③ 取候选（一条 SQL）：
     SELECT forward_id, sent_by, company, subject, to_list, sent_at
       FROM forward_log
      WHERE checked_at = '' AND bounced = 0
        AND sent_at <= datetime('now', '-120 seconds')
      ORDER BY sent_at LIMIT 200

     一个候选都没有 → 立刻返回，不建任何 IMAP 连接（绝大多数 sweep 属于这种）

④ 分组抓取：
     路 A：按 sent_by 分组，每个账号登录一次，只查 \Sent(&XfJT0ZAB-)
           🔑 凭据 = secrets.json 的 ACCOUNTS[<该账号邮箱>]，即 config.secrets.get_accounts()
              —— **与 SMTP 发信、IMAP 收信完全同一套**（ingest.py:97 就是这么取的），
              不新增任何凭据、不用探测用的临时密码。
           阿里 SEARCH SINCE <今天> → 拉全头 → 本地按 fid 建集合
     路 B：登录 3841559246@qq.com 一次（凭据见 §4.4 QQ_VERIFY_*）
           文件夹 = **动态枚举**，不写死白名单（见下）
           每个文件夹取序号尾部 K=300 → 拉全头 → 本地按 fid 建集合

           动态枚举规则（关键：消除「新建归档夹没进白名单」这类系统性假缺失）：
             folders = IMAP list()
             取「其他文件夹」下的**全部**子夹（前缀 &UXZO1mWHTvZZOQ-/）
             + INBOX
             排除：Sent Messages / Deleted Messages / Drafts / Junk
                   （前两个实测用 EXAMINE 打开会报 [CLIENTBUG]，见 §3.4）
             再跳过：QQ_VERIFY_SKIP_SUBFOLDERS（默认 ("tracing",)）
                     —— 动态枚举本来会把 tracing 扫进来（它就在那个前缀下），
                        而决策是 tracing 不纳入核对 ⇒ 跳过，省约 7 s/轮。
                        跳过不影响判定：候选集来自 forward_log，tracing 的信永远不在候选里。
           ⚠️ 洋在 QQ 侧可以随时新增归档规则/文件夹 ⇒ 硬编码 5 个文件夹会在那天集体假缺失。

⑤ 判定（每封）：
     A有 B有 → AB       正常（不告警）
     A有 B无 → A_ONLY   「已存档但未见外部投递」→ 告警
     A无 B有 → B_ONLY   「收到但未存档」（极少见）→ 告警
     A无 B无 → NONE     「该转没转出去」→ 告警（最严重）

⑥ 落库（无论正常与否都写，天然去重）：
     UPDATE forward_log SET checked_at=datetime('now'), check_result=? WHERE forward_id=?

⑦ 告警（只对 3 种异常）：
     收件人分级见 §12-②（待洋拍板）
     文案含：公司、主题、收件人、发信账号、forward_id、缺的是哪一路、sent_at
     走 notify 折叠窗口；通知失败要落 ok/fail（企微会静默失败）
     同一公司本轮多封 A_ONLY 时合并成一条（见 §5 降噪）

⑧ IMAP 登录/连接失败 ≠ 邮件没发出去 —— **粒度按账号 / 按路，不是按批**：
     · 路 A 某个发信账号登录失败 ⇒ 只把**该账号名下**的候选留在 checked_at=''（下一轮再核），
       其余账号的候选照常判定、照常落库。一个账号故障不能阻塞全量自检。
     · 路 B（QQ）失败分两级（这一级不分清就会做出假缺失）：
         登录/连接失败            ⇒ 整批不写 checked_at（只有 A 路会把每封都判 A_ONLY）
         单个文件夹 SELECT/FETCH 失败 ⇒ 记入 failed_folders 后继续（别的夹可能也有同一个 fid）
         INBOX 失败 或 所有夹都失败  ⇒ 视为路 B 失败，整批不写 checked_at
         理由：参考实现里单夹异常是「打警告继续」，若直接沿用，那个夹里的副本会被判成 A_ONLY。
       ⇒ 因此 `collect_qq()` 的返回必须是 `(fids: set[str], failed_folders: list[str])` 元组，
         `run_once()` 的返回 dict 也要带 `failed_folders`（测试 8b/8c 断言用）。
     · 任何情况下「查不到」都不当成「没发出去」告警。

⑨ 计数（进每日汇总，与 post_send_bookkeeping_failed 同类 —— 四个键语义不许混用）：
     increment_counter("send_check_skip_no_id")      ← SKIP_NO_ID（邮件没带匹配键）
     increment_counter("send_check_skip_no_sender")  ← 候选行 sent_by 为空（数据异常，不是网络）
     increment_counter("send_check_failed")          ← IMAP 抓取失败（账号数 + 文件夹数；
                                                        路 B 整路失败时至少 +1，否则故障进不了汇总）
     🔴 send_check_skip_no_id 持续 > 0 = **代码 bug 信号**（某条转发路径漏写 X-YXO-Forward-Id），
        不是业务现象。必须能在每日汇总里一眼看到。

     ⚠️ **不实现 `send_check_legacy`**（v1 曾列过，已删）：`SKIP_LEGACY` 是启动时一次性打的标记、
        不是「核对结果」；生产 `forward_log` 现 0 行 ⇒ 永远是 0；要逐行计数得给 `store`
        引入 `notify` 依赖（现在不依赖），成本 > 收益。
```

### 4.3 为什么两路都要（这段是设计依据，不要简化掉）

- **路 A 只证明「信被自己的邮箱存档了」**：存档 ≠ 投递，中继在存档之后就丢了也是可能的。
- **路 B 证明「信真的走出了阿里、到了另一个邮件服务商」**——这是**跨域投递**的实证。
- 两路都缺 = 连门都没出去。
- 但两路都不能证明「客户看到了」，这一层只能靠退信（`bounce.py`）兜。

### 4.4 实现位置

| 文件 | 作用 |
|---|---|
| `core/store.py` | `forward_log` 加 3 列；`write_forward_log` 增 `company=`/`no_fid=` 形参；新增 `get_pending_checks()` 与 `mark_check_result()` |
| `core/act.py` | `_record_forward()` 加 `company` 形参；单目标 + split 两处传值（**发送行为零改动**） |
| **新增** `core/send_check.py` | 纯逻辑：`build_candidates()` / `probe_sent()` / `probe_qq()` / `classify()` / `run_once()`。**IMAP 客户端注入**（默认实现 + 测试用桩），不连真邮箱也能单测 |
| `serve.py` | `run_sweeper` 里加一行调用；告警文案（薄层，≤30 行） |
| `config/settings.py` | `QQ_VERIFY_*`（主机/端口/用户/密码/`OTHER_ROOT`/`EXCLUDE`/`SKIP_SUBFOLDERS`；**文件夹动态枚举 + 跳过 tracing**）、`SEND_CHECK_ENABLED`（默认 0）、`SEND_CHECK_DELAY_SEC=120`、`SEND_CHECK_TAIL_N=300`、`SENT_FOLDER_UTF7` |
| `core/notify.py` | 新增告警类型 + 中文标签；失败计数 |

> **凭据来源点名（否则实施第一天就卡住）**
> - **路 A（查各发信账号的 `\Sent`）**：直接用 `config.secrets.get_accounts()[<该账号邮箱>]`，
>   即 `secrets.json` 的 `ACCOUNTS` —— **与 SMTP 发信、IMAP 收信完全同一套**（`ingest.py:97` 就是这么取的）。
>   不新增凭据，也不用任何探测用的临时密码。
> - **路 B（查 QQ）**：`secrets.json` 的 `QQ_VERIFY_USER` / `QQ_VERIFY_PASSWORD`，env 优先
>   （与 `WECOM_*` / `BOUNCE_*` 同一套路）。**不进代码、不进 git。**
>   ⚠️ `settings.py` 里只能读 env —— 它 import `secrets` 会**循环导入**
>   （`config/secrets.py` 本身 `from .settings import SECRETS_PATH`）。
>   secrets 回退必须写在 `send_check.py` 的 `_qq_credentials()` 里。
> - **密码取到空串 ⇒ 视为该账号 / 路 B 登录失败**（计入 `failed_accounts` 或整批搁置），
>   **不要拿空密码去 `login()`** —— 那会返回一个误导性的认证错误，排查方向全跑偏。

### 4.5 键与健壮性

- 匹配键 = `X-YXO-Forward-Id` 字符串精确相等（**不做去 `<>` 之外的任何归一化**）。
- 老系统发出的信没有这个头 ⇒ 那些信一律 `SKIP_NO_ID`，**不计入告警**（切换前本来就没这功能，不能回溯罚）。
- 一封 `forward_log` 行对应的 `forward_id` 若在两侧都找不到，才是 `NONE`。
- 同一 `forward_id` 在某一侧出现**多份**（重试/重发）⇒ 判定「存在即可」，不因此告警。

- 🔴 **「信上有没有这个头」必须在写库那一刻就判掉**（`v2` 新增，别省）：
  `forward_id` 是发送方自己生成、写进 `forward_log` 的，**和「信上是否真的带了这个头」是两件事**。
  将来若有某条发送路径漏写头，这行 log 照样存在、id 照样有值，但信在两侧都查不到
  ⇒ 会被判成 `NONE`（最严重档）并**误报给公司负责人**。
  ⇒ `_record_forward()` 增加 `no_fid` 形参；`no_fid=True` 时该行直接写成
  `check_result='SKIP_NO_ID'` + 非空 `checked_at`（天生不进候选），并
  `increment_counter("send_check_skip_no_id")`。
  三个调用点各传 `not msg.get("X-YXO-Forward-Id")`。

### 4.6 A_ONLY 降噪（两级，防「系统性假缺失」刷屏）

有两种会导致**批量** A_ONLY 的故障，都不是真丢件：

1. 某家公司的 `cc_addrs` 里把 QQ 邮箱删了 ⇒ 这家以后**每封**都是 A_ONLY；
2. QQ 侧新建了归档文件夹 ⇒ 若实现用固定白名单，就会集体假缺失。

对策：

- **第一级（从根上消除）**：文件夹**动态枚举**（§4.2 ④），第 2 种故障此后不存在。
- **第二级（兜第 1 种）**：**同一 `company` 在同一轮 sweep 内出现 ≥2 封 A_ONLY ⇒ 合并成一条告警**，
  文案写「本轮该公司 N 封未见外部投递，QQ 副本链路可能已断（是否把 3841559246@qq.com 从 cc 里删了？）」，
  不逐封发。
  - **不跨轮次保持状态**：日均 6 封，一轮里同公司多封本身就是异常信号，够用；
    跨轮次「连续 N 封」要维护额外状态，收益不成比例。
- 收件人分级见 §12-②（待洋拍板）。

---

## 5. 边界与不做

| 不做 | 原因 |
|---|---|
| 不 BCC | QQ 已经是 CC（8 家全含），洋 2026-10-29 明确不改 |
| 不重发 | 只告警；重发可能造成客户收到两份 |
| **不加 `out_msg_id` 列** | `X-YXO-Forward-Id` 已是唯一键，实测两侧保留（§3.2） |
| **不用 `SEARCH HEADER` 搜自定义头** | 阿里假阴、腾讯假阳（§3.7 P1） |
| **不用 `HEADER.FIELDS` 取自定义头** | 阿里静默忽略（§3.7 P2） |
| **腾讯侧不用 `SEARCH SINCE`** | 完全失效，返回全部（§3.7 P3） |
| 不用「主题 + 收件人」模糊匹配 | 主题会被服务端改写（`Re:`、`【箱号】` 前缀） |
| 不在发完立刻查 | 实测 8~22 s，立即查会假告警 |
| **不查 tracing** | 洋 2026-10-29 拍板；量大（3320 封）、扇出多 |
| 不做「忽略本次」入口 | 洋 2026-10-29 拍板；量太小，噪音靠 `SEND_CHECK_DELAY_SEC` 调 |
| 不发运营（`luohua@yxologistics.com`） | 洋 2026-10-29 明确：**她是渝新欧的，算外部单位** |

---

## 6. 风险

| 风险 | 对策 |
|---|---|
| QQ 归档规则延迟 → 假缺失 | 跨文件夹查；延迟 120 s；同一封只核一次（`checked_at` 落库） |
| QQ 邮箱容量/限频 | 一天 ~6 次、每次拉几百个全头（几秒），远低于限制 |
| 已发送延迟 > 120 s | 报错文案带「可能是延迟，稍后可能补上」；必要时调 `SEND_CHECK_DELAY_SEC` |
| 授权码泄露 | 只进 `secrets.json`，不进代码、不进 git |
| IMAP 登录失败本身 | 记 `send_check_failed` 进每日汇总，**不写 `checked_at`**、**不当成"邮件没发出去"** |
| 阿里侧序号非时间序 | 阿里用 `SINCE` + 全量结果比对；**不要**取「尾部 N 封」（实测会漏） |
| 自检覆盖率有上限 | §2.1 已写明；`post_send_bookkeeping_failed > 0` 那天要人工看 |
| **`send_check_skip_no_id` 持续 > 0** | 这是**代码 bug 信号**（某条转发路径漏写 `X-YXO-Forward-Id`，如 `_forward_split` 漏传），不是业务现象；进每日汇总，与 `post_send_bookkeeping_failed` 同级看待 |
| QQ 侧新增归档文件夹 | v2 改**动态枚举**，新夹自动覆盖；只需确保 `Sent Messages`/`Deleted Messages`/`Drafts`/`Junk` 被排除 |

---

## 7. 验收判据

1. **不改变发送链路**：`diff` 只涉及 §4.4 列的文件；`act.py` 的改动只允许「传一个已有变量」，发送/收件人/主题/正文/附件零变化。
2. 单测覆盖 §4.2 ⑤ 的 4 种分级 **+ 跳过与失败语义**，全部用注入的桩 IMAP 客户端，不连真邮箱：
   - `SKIP_NO_ID`（候选上没有 `X-YXO-Forward-Id`）且计数 +1
   - 路 A **单账号**登录失败 ⇒ 只有该账号的候选留空 `checked_at`，其他账号照常判定落库
   - 路 B **登录失败** ⇒ 本轮**全部**候选都不写 `checked_at`，且**零告警**
   - 路 B **单夹失败但 INBOX 成功** ⇒ 照常判定落库，`failed_folders` 非空
   - 路 B **INBOX 失败**（或所有夹失败）⇒ 整批搁置、零告警
   - 无候选 ⇒ 一个 IMAP 连接都不建（用桩计数断言）
   - **动态枚举**：桩的 `list()` 返回一个新增文件夹名 ⇒ 必须出现在被查询列表里
   - **跳过 tracing**：`QQ_VERIFY_SKIP_SUBFOLDERS` 里的夹不进查询列表
   - `init_forward_log()` 连跑两次不抛异常（迁移幂等）
3. **真实端到端演练**（验收方做，不用改代码）：
   - 把某封真实转发的 `forward_log.forward_id` 改成不存在的值 → 必须报 `NONE`；
   - 在 QQ 侧把那封临时移到 Deleted → 必须报 `A_ONLY`；
   - 两封都正常 → 必须 `AB` 且**无告警**。
4. 告警文案里的公司名、负责人与 `forward_log.company` 一致，且**同一封只发一次**（连跑两轮 sweep 不重复报）。
5. 跨文件夹 + 动态枚举：把 QQ 的副本移到一个**新建的**归档子夹（不是现有 5 个之一），仍能判成 `AB`。
6. 全量 `0 failed`；新增测试条数写进 REPORT。
7. **不产生假告警**：连续跑 3 天真实流量，告警数应为 0（若有，逐条定性）。

---

## 8. 附录 A：补 `Message-ID` + 合规头（**可选，不阻塞本功能**）

`core/act.py` 的 `build_forward_message()` 现在只设 `From/To/Cc/Subject/X-YXO-Forward-Id`，
**没有 `Date` 和 `Message-ID`**。

```python
msg["Date"] = formatdate(localtime=True)
msg["Message-ID"] = make_msgid(domain=发送账号的域名)
```

收益（都**不是**本功能的前提）：

- 外发邮件合规（RFC 5322 要求 `Date` + `Message-ID`）
- **退信 NDR 关联更准**（`bounce.py` 现在靠 `forward_id`，缺 `Message-ID` 时人工排查困难）
- 已发送副本多一个可读标识，便于人肉核对

> v0 把它写成「前置依赖项」是错的：§3.2 已证明自检的匹配键不缺。**建议单独开一个小单做。**

---

## 9. 附录 B：`empty_rows` 计数进每日汇总（已拍板，独立小改）

洋 2026-10-29 决定：无提取行的邮件**不单独告警**，只在每日汇总显示一个数字。

| # | 文件:行 | 改什么 |
|---|---|---|
| 1 | `serve.py:144`（`if not rows:` 分支） | 加 `increment_counter("empty_rows")`（该分支现在只有 `add_error`，**没有计数**） |
| 2 | `core/notify.py:311 send_digest` | 加中文标签 `"无提取行(未转发)" if k == "empty_rows"` |
| 3 | `tests/` | 断言计数器 +1、汇总文案含「无提取行」 |

> 注意：**任务 07 修完 DSK 提取器后，DSK 的 `empty_rows` 预期为 0**（42 封实测从 4 → 0）。
> 也就是说这个数字平时应该一直是 0，一旦非 0 就是真异常。

---

## 10. 附录 C：配置收敛（另一单，不在本功能范围）

现状是「各自写各自的」，至少 8 个面；加一家公司要改 5~6 处。洋 2026-10-29 决定：**只解决新系统**。

| 现位置（新系统） | 收敛后 |
|---|---|
| `core/store.py:245-254` `seed_owner_mapping()` **硬编码 8 家公司** | 移到 `bot_config` 数据里，代码不出现公司名 |
| `bot_config.db` 的 `company` / `owner` / `train` 三个 scope | 保留，作为**唯一真源** |
| `config/settings.py:140` `COMPANY_ALIAS` | 保留，语义收窄为「别名 → 标准名」 |
| `core/notify.py:149` `_WECOM_NAME_BY_EMAIL` | 保留（只是「邮箱 → 企微姓名」转换） |

目标：**新增一家公司 = 只写 `bot_config` 数据，不改代码。**

---

## 11. 决策记录

**洋 2026-10-29 已拍板**

| # | 议题 | 决定 |
|---|---|---|
| 1 | tracing 纳不纳入 | **不纳入** |
| 2 | 核对延迟 | **120 s**，不做二次复查 |
| 3 | 告警发不发运营 | **不发**（`luohua@yxologistics.com` 是渝新欧的，**算外部单位**） |
| 4 | 「忽略本次」入口 | **不做** |
| 5 | QQ 凭据位置 | `secrets.json` 的 `QQ_VERIFY_*`，env 优先 |
| 6 | 是否改 BCC | **不改**，保持 CC 现状 |
| 7 | 无行是否告警 | **不告警**，只进每日汇总计数（附录 B） |
| 8 | 配置收敛范围 | **只解决新系统**（附录 C） |

**仍需在任务书里写清、但不需要洋再拍的（v1 已定）**

- 匹配键 = `X-YXO-Forward-Id`，不加 `out_msg_id` 列
- 调度挂 `run_sweeper`，`checked_at` 落库去重
- 四个 IMAP 坑（§3.7）对应的写法约束
- 告警收件人 = `get_owner_email(company)` ∪ `OPS_OWNER_EMAIL`，去重

**已知未决（不阻塞）**

1. 阿里 `\Sent` 里若同一 `forward_id` 出现两份（重试/重发），判定取「任一份存在即可」——§4.5 已写明。
2. 新系统上线早期 `forward_log` 为空的窗口：建议 `SEND_CHECK_ENABLED` 默认 0，切换验证完再打开。

---

## 12. 告警策略（洋 2026-10-29 已拍板）

**① A_ONLY 只发毛骁洋；只有 NONE 才发「公司负责人 + 毛骁洋」**

| 判定 | 收件人 |
|---|---|
| `AB` | 不告警 |
| `A_ONLY`（存档有 / 外部无） | **仅 `OPS_OWNER_EMAIL`**（毛骁洋） |
| `B_ONLY`（外部有 / 存档无） | **仅 `OPS_OWNER_EMAIL`** |
| `NONE`（两侧都无） | `get_owner_email(company)` ∪ `OPS_OWNER_EMAIL`，**先滤掉 `None`/空串再去重** |

> ⚠️ `get_owner_email()` 在查不到该公司时返回 `None`。`{None, OPS}` 去重后仍是两项，
> 把 `None` 传给 `notify()` 会让整条通知**静默失败** ⇒ 组装时必须是
> `sorted({r for r in (owner, OPS_OWNER_EMAIL) if r})`。

理由：`A_ONLY` 最可能是延迟或 QQ 归档还没到位；让负责人收到「可能没送到」但其实只是延迟，
他会去打电话找客户核实，制造额外工作量。**`NONE` 才是真丢，必须让负责人知道。**

**② 同一公司批量 A_ONLY 合并成一条**

- 判定阈值：**同一轮 sweep 内、同一 `company`、A_ONLY ≥ 2 封**
- 输出：一条汇总告警「本轮该公司 N 封未见外部投递，QQ 副本链路可能已断
  （是否把 3841559246@qq.com 从 cc 里删了？）」，附 `forward_id` 列表
- 收件人：仍按 ①（A_ONLY ⇒ 仅毛骁洋）
- 不跨轮次保持状态（理由见 §4.6）

> 注：`B_ONLY` 本身就是「存档缺失」的异常，与公司无关，按逐封报即可（极少见）。
