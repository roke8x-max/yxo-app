# MailBots 重构设计（Spec）

> 日期：2026-08-26
> 状态：待骁洋终审
> 上游文档：`plans/06-重构设计`（D1–D8）、`plans/07-业务需求 v1.1`、`plans/08-技术路线 v1.1`、`Desktop/MailBots重构方案-①②`、`WorkBuddy/运单号草单_匹配四档判定补全规范.md`（T0–T7 v2）
> 本文取代上述文档中与本 spec 冲突的条目；未冲突处继续有效。
> 代码基准：`yxo-app/mailbots/` git dev 分支。桌面 `MailBots` 快照已废弃（P20）。

---

## 0. 本轮新拍板决策（在 D1–D8 基础上）

| # | 决策 | 内容 |
|---|---|---|
| N1 | 运单号转发对象 | 解析归属后统一走 **负责公司→外部联系人**（bot_config）路由转发邮件 |
| N2 | 通知分层 | realtime 只发负责同事本人企微；管理员/骁洋只收 alarm + 每日日报 |
| N3 | 新业务线 | 只预留 Processor 插件接口，不实现铁海/进口/清关业务 |
| N4 | 服务化执行 | 本机出脚本+操作手册 → GitHub 同步 → 生产服务器上的智能体执行 nssm 安装/PID 锁部署 |
| N5 | 数据增长 | 全套本次做完：索引 + match 内存缓存 + tracing_log/forward_log 按月归档 |
| N6 | 合并文件夹 | 「运单号」+「运单草单」合并为邮箱文件夹 **「草单运单号」**（用户改邮箱规则），单 IDLE watch |
| N7 | gate NLP | 暂缓，本次不做 |
| N8 | 进程模型 | **分阶段统一**：本期草单+运单号进新框架长驻；运踪/DSK/ATB 用公共层打补丁但保持 15 分钟短进程；二期再并入同一长驻进程 |
| N9 | 专列路由（修订 P22） | **不加新字段、不自动回填**。骁洋手动在 yxo_app 维护专列记录的负责公司（库列名 `开票子公司名称`）。机器人只从 records 推导；空路由→alarm 兜底。`scope='train'` 保留为应急覆盖层 |
| N10 | 飞书 | 彻底删除全部飞书依赖（lark_oapi / FeishuBitable / seed 读路径）；`Database_Syncer` 废弃归档；`dsk_config_cache.json` 消灭 |

## 1. 字段命名约定

**负责公司** = records 表列 `开票子公司名称`（yxo_app 界面已改名"负责公司"）。本文档业务描述用"负责公司"，代码访问用库列名。

## 2. 总体架构（分阶段统一）

```
┌─────────────────────────── NSSM/看门狗 托管 ───────────────────────────┐
│ mailbot_serve.py 单长驻进程                                            │
│  ├ core/imap_fetcher   IDLE watch「草单运单号」(4账号×1连接)            │
│  ├ processors/draft    草单 A/B/C1/C2                                  │
│  ├ processors/waybill  WAY_A(.xls 拆分)；WAY_B 直接忽略                 │
│  ├ core/routing+derep+notify 公共层                                    │
│  └ commands/engine_api 企微确认/跳过 进程内调用                         │
└─────────────────────────────────────────────────────────────────────────┘
┌─────────────── Windows 任务计划 15 分钟短进程（保留+打补丁）─────────────┐
│ Tracing_Robot_IMAP.py / Dsk_Robot.py / Atb_Robot.py                    │
│  改为调用 core/routing + core/dedup + core/notify（业务逻辑不动）       │
└─────────────────────────────────────────────────────────────────────────┘
真相源 yxo.db：records(主数据) / bot_config(company=To/Cc 配置; train=应急覆盖层)
              / dedup_global / waybill_ledger / tracing_log(+snapshot) / forward_log
```

二期（本 spec 不实施）：tracing/dsk/atb 迁入同一 serve 进程。

## 3. 核心模块（mailbots/core/）

| 模块 | 职责 | 关键点 |
|---|---|---|
| paths.py | 启动自检 | yxo.db 可写、config_local 存在；失败拒启并明确报错 |
| models.py | 数据类 | MailEvent / MatchResult(tier,reason,candidates) / RouteTarget；全量类型注解 |
| matching.py | 统一匹配 | `classify_match(code, box, idx)` T0–T7（见 §4）；active 过滤铁律 |
| routing.py | 统一路由 | `resolve_recipients(code,box,train_id)`：records 推导（散舱+专列同链路读开票子公司名称）→ bot_config(company) To/Cc；scope='train' 仅覆盖；空结果返回 None 并触发 alarm |
| dedup.py | 全局去重 | 表 `dedup_global(key PRIMARY KEY, synthetic, claimed_at)`；INSERT OR IGNORE 原子 claim；成功转发才 mark；缺 ID 业务文件夹内作用域哈希兜底 |
| events_store.py | 事件仓库 | WAL+busy_timeout；.eml 落盘仓库（处理器不回 IMAP 取件）；waybill_ledger 识别即 INSERT(forward_status=pending)→成功 UPDATE sent |
| imap_fetcher.py | 抓取 | IDLE watch「草单运单号」；UIDNEXT/UIDVALIDITY 持久化重连；29min idle 刷新；指数退避；排除已发送/机器人/垃圾箱 |
| notify.py | 分层通知 | `notify(level,target,text,reason)` level∈realtime/alarm/digest；alarm 强制 reason 非空+5min 同因聚合+**能识别归属时同时通知负责同事本人**；digest=每日日报 |
| sending.py | 发送语义 | **成功定义 = `sendmail()` 返回空 dict**（smtplib 部分拒收只返回 dict 不抛异常，必须检查返回值）。全部拒收→不 mark dedup、退避重试、连续 N 次失败 alarm(reason=smtp_failed)；部分拒收→立即 alarm 携带被拒地址明细并正常 mark（防止对已送达收件人重复投递），缺口人工补投；**SMTP 成功但 ledger/写库失败→仅记日志不重发**（幂等优先于补账） |

**Processor 接口**（插件槽位，N3 只预留）：
```python
class Processor(Protocol):
    can_handle(event) -> bool
    process(event) -> ProcessResult   # 含 tier/动作/告警需求
```

## 4. 匹配分级（取代旧四档；实现自写）

客编解析：`^(?P<prefix>[A-Za-z]+)(?P<seq>\d+)(?:-(?P<suffix>.+))?$`（不写死 CQWLJT）。
**客编/箱号消歧规则**（防箱号被通用正则误判为客编，如 `TRIU1234567`）：
1. ISO 箱号形态 `^[A-Z]{4}\d{7}$` → 一律判箱号；
2. 客编 prefix 白名单从数据学习：启动时 `SELECT DISTINCT` records 全量客编的前缀集合入内存索引，token 的 prefix 不在白名单且无 `-后缀` → 不作为客编候选；
3. 提取优先级：主题 > 附件名 > 正文；同一 token 同时命中两类的场景以上述 1→2 裁决。
加载 records 铁律：`WHERE 状态<>'退舱' AND is_deleted=0`；建 by_full_code/by_seq/by_box 索引（内存缓存，决策 N5）。

| 档 | 条件 | 动作 |
|---|---|---|
| T1 | 客编精确命中 | 自动转发（箱号不卡） |
| T2 | 序号+箱号命中、到站后缀异 | 待办 |
| T3 | 序号命中、箱号不命中/空 | 待办 |
| T4 | 序号不在库 | **alarm** |
| T5 | 无客编+箱号唯一命中 | **alarm** |
| T6 | 无客编+箱号命中≥2 | 待办 |
| T7 | 序号跨≥2 active 后缀 | 待办（防御 guard） |
| T0 | 无客编且无/零箱命中 | **alarm** |

对外呈现三态 **full / pending / alarm**（内部 reason 存八档明细）。

**判定顺序**（显式，消除歧义）：
```
有码:  by_full_code 精确命中 → T1
       else 序号查 active 索引:
            命中 → 后缀异+箱号同→T2；多后缀→T7；否则 T3
            未命中 → 再查全量索引(含退舱/软删):
                     命中 → T4(reason=cancelled_only)
                     未命中 → T4(reason=unknown)
无码:  by_box: 0命中→T0；1命中→T5；≥2→T6     # 无码分支绝不进 T1–T3
```

**缓存失效机制**：每处理周期读 yxo.db `meta_kv` 版本号（yxo_app `bump_version()` 已写入）+ TTL 5 分钟兜底；版本变化即重建内存三索引。防止新订舱导入后被误判 T4。

**多行邮件（WAY_A 拆分）行间独立处理**（2026-08-26 拍板，取代 v2 §2.3 整封阻断规则）：报警行（T4/T5/T0）各自产生 alarm，**不阻断其他行**——T1 行照常拆分转发、待办行照常入队。测试集 W08 的 `skip_rows` 应相应更新为 `alarm_rows`（VXN 行 T4 报警）。

## 5. 分类与转发矩阵（基于 2026-08-26 真实邮箱实证）

实证基础：4 账号 × 5 文件夹采样 344 封 + 附件深度解析（样本已入仓库 `mailbots/tests/fixtures/real_imap_samples/`，可作测试 fixture）。

### 5.1 各文件夹真实结构（摘要）

- **草单运单号（合并后）**：
  - 运单号 WAY_A：单一发件人 `docwbfb@yxologistics.com`，主题 `YYYY-MM-DD YXO-2026-NNN CQWLJT运单号`，.xls 表头 8 列（客户编码/箱型/起始站/目的站/箱号/运单号/托盘数量/货物毛重）。
  - WAY_B：主题含「单证审核驳回」→ **完全忽略**（仅审计日志防重扫，零通知零待办）。
  - 草单：多发件人；主题结构化 `运单草单_车次_CQWLJT<seq>-<到站>_<箱号>`，变体前缀 `Re:`/`回复:`/`【草单更新】`需剥离；凭证=加密PDF `<BOX>-<N>-<N>已加密.pdf`；噪音类型（出区放行/报关单/invoice/clipboard 截图等）进排除清单只记录不转发。
  - **旧 W 类废除**（运单号邮件全归 waybill 处理器）。
- **Tracing**：单一发件人 `tracing-system@`；主题 `Tracing info of YXO 2026 train NNN - date [YXO-2026-]NNN-K`；.xls 三 sheet，箱号清单在 `Daily container list`；同班列多封分段(-1/-2/-3)各自独立处理。
- **ATB**：单一发件人 `atb@`；主题 `ATB for YXO-2026-NNN CODE BOX` 或 `pickup reference for ...`；PDF 以箱号命名。
- **DSK**：`kasa@rtsb.de`（偶发 deutschebahn.com）；主题 `YXO-NNN / DSK/ 公司 DD.MM.YYYY`；正文 HTML 表含箱号+订舱号。

### 5.2 分类器设计原则

**门禁分处理器设计**（实证驱动）：
- **草单处理器不设发件人白名单**——草单件判据（加密 PDF 命名/箱号PDF+更新关键词）本身即门禁。实证外部代理直发草单是常态（`pyx@cqbestar.cn`、`evelyn.hu@cail56.com`、`pacific-opt5@p-shipping.com` 均发过加密草单），按域拦截会漏。
- **运单处理器保留发件人白名单**（默认 `docwbfb@yxologistics.com` 可配置；实证 80/80 单一发件人）。
- 链条：剥回复前缀（`Re:`/`回复:`/`【草单更新】`等）→ **噪音排除清单前置**（出区放行/报关单/invoice/clipboard 截图等命中即只记录，先于一切转发类判定）→ 草单件判定（无发件人门槛）→ 运单号主题/.xls 判定（白名单内）→ 其余按域分内部/外部走 C1/内部回复链/C2。不再以附件命名为草单判定主键。

### 5.3 转发矩阵（最终）

| 处理器 | 拆分 | 发送形式 |
|---|---|---|
| draft (A/B/C1) | **不拆** | 原样整封转发；按公司→同事分组，每组一封，从对应同事邮箱发出 |
| waybill (WAY_A) | **拆分** | .xls 按行过滤出各公司自己的行，逐封转发（每同事每公司一封） |
| tracing | **不拆** | 原样整封转发给"箱号命中该公司"的所有负责公司组；各组一封原件（含全部箱子，可见性即业务预期），从对应同事邮箱发 |
| dsk | **拆分** | 解析正文 HTML 表格与附件名中的箱号→按负责公司映射到负责同事，每公司一封（仅含该公司箱号的内容与对应附件），从其邮箱发出 |
| atb | **拆分** | 解析主题/附件中的箱号→按负责公司映射到负责同事，逐封转发。实证当前每封仅含 1 箱（单箱邮件=单公司一封）；未来若出现多箱混发邮件则按箱过滤后分company重建 |

**发送身份统一机制**（沿用现有 `USER_COMPANIES`/`sender_for_company` 并抽入 core）：负责公司 →(关键词映射)→ 负责同事 → 以该同事 cqtransit.com 邮箱 SMTP 发信；收件地址 = bot_config(scope='company') 该公司 to/cc。映射找不到同事 → alarm。

### 5.4 草单动作表

| 类 | 判定 | 动作 |
|---|---|---|
| A 新草单 | 草单件且序号首次出现 | T1→转发+通知本人；否则按档位 pending/alarm |
| B 更新草单 | 草单件且序号已见 | 同 A |
| C1 内部杂件 | 内部域名、非草单件、**新鲜主题（非回复链）** | 路由依据=标准匹配管线：T1 命中→按该公司路由转发+企微通知本人；**非 T1 → 只记录+结构化审计日志**（不进待办不报警，防内部杂件刷队列/误发客户，2026-08-26 补充定义） |
| 内部回复链 | 内部域名、主题带 `Re:`/`回复:` 前缀、无草单件 | **只记录不转发**（防内部沟通外泄到客户邮箱，2026-08-26 拍板） |
| C2 外部杂件 | 其他外部发件人非草单件 | 只记录台账不转发（2026-08-26 拍板维持；外部草单已由 A/B 覆盖） |
| WAY_B | 主题「单证审核驳回」 | **忽略**（审计日志一条） |

退舱跳过逻辑保留（单箱口径）。

## 6. 数据模型变更

无 records schema 变更（N9）。新增/变更仅：

```sql
CREATE TABLE IF NOT EXISTS dedup_global(
  key TEXT PRIMARY KEY, synthetic INTEGER DEFAULT 0,
  claimed_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS waybill_ledger(
  id INTEGER PRIMARY KEY, code TEXT, box TEXT, waybill TEXT,
  train_no TEXT, depart_at TEXT, company TEXT, msg_id TEXT,
  forward_status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT (datetime('now')));
CREATE INDEX IF NOT EXISTS idx_records_code   ON records("客户编码");
CREATE INDEX IF NOT EXISTS idx_records_box    ON records("箱号");
CREATE INDEX IF NOT EXISTS idx_records_train  ON records("班列号");
CREATE INDEX IF NOT EXISTS idx_records_company ON records("开票子公司名称");
CREATE INDEX IF NOT EXISTS idx_waybill_msg    ON waybill_ledger(msg_id);
CREATE TABLE IF NOT EXISTS draft_seen_seq(
  seq TEXT PRIMARY KEY, msg_id TEXT,
  first_seen_at TEXT DEFAULT (datetime('now')));
-- tracing_log/forward_log 按月归档至 *_archive（保留策略：在线 90 天）
```
**草单 A/B 台账（draft_seen_seq）**：新架构单库新增表 `draft_seen_seq(seq TEXT PRIMARY KEY, msg_id TEXT, first_seen_at TEXT DEFAULT (datetime('now')))`。语义：处理器**遇到即写**（INSERT OR IGNORE，无论后续转发/待办/报警），同序号再次出现 → B 类（更新草单）。取代旧 draft_forward_ledger 的 draft_nums 职责。

状态回写双语义（08 §4）：状态列覆盖式一次写入；dsk/ATB 时间戳首写即定不覆盖。

## 7. 可观测性（第一刀，D4）

1. `daemon_loop` traceback 必须落日志文件（error_logs/<bot>_error_YYYYMMDD.log，目录自动创建）。
2. 所有跳过/未匹配/无路由分支打印结构化原因（JSON 行日志）。
3. 无路由跳过 → alarm（骁洋 + 能识别归属时的负责同事）+ 标已读防循环（根治 P3）。
4. `/health` 端点暴露 last_mail_at/pending_count/idle 线程状态。

## 8. 实施顺序（9 刀，每刀独立提交可回滚）

| # | 刀 | 内容 | 主要消灭 |
|---|---|---|---|
| 1 | 可观测性 | traceback 日志 + 结构化跳过原因 | P3/P12 |
| 2 | core 地基 | paths/models/matching(T0-T7)/routing/dedup/events_store + 单测（yxo_test.db） | P7 |
| 3 | Schema | dedup_global/waybill_ledger/索引/归档函数（先建表不切流） | P19 |
| 4 | 处理器迁移① | draft+waybill 进新框架（T0-T7、.eml 落盘、WAY_B 忽略、W 类废除） | P2/P4/P5 |
| 5 | IDLE 长驻 | 「草单运单号」fetcher + UIDVALIDITY + 双模式并行期去重兜底。⚠ **gate：合并文件夹的邮箱规则须由骁洋人工改完方可上线**（否则 fetcher 抓空）；并行期同时 watch 旧两文件夹+新文件夹 | 秒级触达 |
| 6 | 短进程打补丁 | tracing/dsk/atb 接 core/routing+dedup+notify；**新增 tracing .xls `Daily container list` 箱号解析能力**（现状 box_no 100% 空，扇形转发依赖此新解析） | P8'/P1/P22 兜底 |
| 7 | 通知+日报 | notify 分层 + alarm 聚合 + C1 通知 + digest 日报 | P12/N2 |
| 8 | 数据增长 | 内存缓存切换 + 归档任务上线 | P19 |
| 9 | 生命周期收尾 | PID 锁+DETACHED+nssm 脚本包（交服务器智能体）、飞书代码删除、Database_Syncer/dsk_config_cache 下线、legacy 归档 | P6/P15-P18/N10 |

顺序原则：先可见→再地基→草单运单号先上→低频三机器人打补丁→通知→性能→运维收尾。

## 9. 测试与验收

- 单测：matching T0–T7 全档位（含 v2 规范 §5 用例：VXN 退舱歧义消失、FWRU0192384/PONU8175063→T6）、dedup 原子 claim、routing 专列/散舱双链路+空路由报警、UIDVALIDITY 重连模拟。
- 回归基准（已入仓库，防丢失）：`mailbots/tests/testset/`——38 夹具 + manifest.json（含 13 条结构化拆分预期）+ records.csv + build/validate 脚本（自 Desktop\test 移入）；`mailbots/tests/fixtures/real_imap_samples/`——31 个真实 .eml/.xls 样本 + sample.jsonl 采样清单（自探针临时目录抢救入 git）；主数据测试库 `mailbots/tests/data/yxo_test.db`（.gitignore 显式例外入库）。按 2026-08-26 拍板同步：D07/D08 维持 T4 报警；**W08 的 skip_rows 改为 alarm_rows**（VXN 行 T4 报警，其余行照常拆分转发）。
- 补样任务：① 运踪分段完整序列（同一班列 -1/-2/-3 全套）实采一列验证互不合并；② testset 的 tracing 夹具附件现为占位 .xls，刀 6 前需替换为真实 `Daily container list` 结构以测箱号解析。
- 验收标准：
  1. 草单运单号新邮件→企微通知中位数 <5s、P99<15s；
  2. 零静默丢失：所有跳过分支有结构化日志；无路由必报警且带 reason；
  3. 双模式并行期零重复转发（dedup_global claim 验证）;
  4. T4/T5/T0 报警到达骁洋+负责同事；WAY_B 零动作；
  5. Tracing 专列车（如 WB794/795 在负责公司维护后）正确扇形转发；
  6. 回滚演练：停 IDLE→恢复计划任务 <5 分钟，业务无感知。

## 10. 部署执行模型（N4)

- 所有生产侧操作固化为 `scripts/deploy/` 下脚本 + 操作手册（Markdown）。
- 本机开发→GitHub push→生产服务器智能体 pull 后按手册执行（nssm install、计划任务改造、邮箱规则由骁洋人工改）。
- 运行模型 Y：生产直跑 git 检出目录 `D:\YXO_DATA\yxo_app\mailbots`。

## 11. 明确不做（YAGNI 边界）

gate NLP（N7）/ :5001 审批端点（并入 n8n 计划）/ 新业务线实现（N3）/ IMAP IDLE 用于 tracing-dsk-atb（保持短进程）/ 专列数据回填自动化（N9，人工维护）/ yxo-web 待办界面（未来项）。

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| 邮箱规则重定向期间两类邮件仍分落旧文件夹 | 双模式并行期 fetcher 同时 watch 旧两文件夹+新合并文件夹，dedup 兜底；稳定一周后停旧 watch |
| IDLE 连接被服务端限制 | 退回 60s 轮询模式（配置开关），仍优于 15 分钟 |
| 专列负责公司人工维护不及时 | 空路由 alarm 即时提醒；scope='train' 应急覆盖层可用 |
| 短进程接公共层引入回归 | 第 6 刀独立 feature 分支+对照测试后合入；可单独回滚 |

## 13. 实施进度

- [x] 刀1 可观测性（2026-08-26，commit: 3e7d74e）
- [x] 刀2 core 地基（同上）
- [x] 刀3 Schema/归档（同上；归档 CLI 默认 dry-run，生产首跑 --apply 待窗口期）
- [x] 刀4 处理器迁移①（2026-08-27，commit: 8652982 / 270c5b7 / 65ac9f5 / b0ecd3d / 270c5b7 / ed67b49 / 2d8c0bc / 78a5bcf / 3e7d74e / 0de9153）
- [x] 刀5 IDLE 长驻（同上；双模式并行已验证，邮箱规则重定向为前置 gate）
- [x] 刀6 短进程打补丁（tracing/dsk/atb 接 core 公共层）
- [x] 刀7 通知+日报（notify 分层 + alarm 聚合 + C1 通知 + digest 日报）
- [x] 刀8 数据增长（索引 + 内存缓存 + 按月归档）
- [x] 刀9 生命周期收尾（PID 锁/DETACHED/nssm 脚本包，飞书代码删除，legacy 归档）

> **注**：Plan B（刀4–5）核心模块已全部交付，134 测试全绿。Plan C（刀6–9）为后续计划。
