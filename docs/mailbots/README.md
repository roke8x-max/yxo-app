# mailbots —— 邮件机器人

收信 → 识别（订舱 / 草单 / 运单号 / 运踪 / DSK / ATB / 退信）→ 转发给对应负责人。项目里**唯一被验证"人真的在用"的自动化**。

## 当前生效（Current）

| 文档 | 状态 |
|---|---|
| **暂无独立 spec** | 现行设计口径以代码模块说明为准：`mailbots_next/README.md`（新系统）、`mailbots/README.md`（旧系统，**生产此刻跑的还是它**） |

> ⚠️ **新旧并存中**：新系统 `mailbots_next/` 已就绪，**但切换尚未开始**。切换前置条件见 `mailbots/README.md` §2。

## 讨论中（Drafts）

| 文档 | 讲什么 |
|---|---|
| [邮件转发与主站在线表-现状与建议(给芙蕾雅)](drafts/2026-10-06-邮件转发与主站在线表-现状与建议(给芙蕾雅).md) | 新旧切换的验收方式建议（离线回放而非在线并行）、在线表轮询优化、三个待核实的技术问题 |
| [给小叽-只读导出任务(转发日志与字段填写率)](drafts/2026-10-06-给小叽-只读导出任务(转发日志与字段填写率).md) | 服务器上的只读导出任务单（为切换准备对照物） |

## 已归档（Archive）

**48 份**（2026-09-09 ~ 2026-09-24），是邮件机器人从"收信通路失效"到"上线收尾"的完整施工记录：

- **联运新增公司**（5 份，9-09）—— 新增"联运"公司的影响面与改造
- **NDR 退信监控**（10 份，9-10 ~ 9-14）—— rev4 → rev4.3 迭代，四邮箱轮询
- **收信通路修复**（5 份，9-16 ~ 9-17）—— IDLE 失效改为方案 A 轮询
- **转发层修复**（2 份，9-17）—— 运单号正文表格与附件口径
- **上线前收尾**（9-18 ~ 9-20）—— 改动 spec、C 组（企微改 HTTP）、poll-secs 上限
- **草单 A/B 分类与告警折叠**（5 份，9-21）
- **WAY_B 改静默 / 单证驳回**（2 份，9-22）
- **命名清理**（5 份，9-23 ~ 9-24）—— `WAY_A/WAY_B` → `waybill_rejected` 等

> 这批**全部已完成并合并**（多数有配对验收报告）。逐份的完成性核实见 `meta/drafts/2026-10-08-国庆文档批次-*`。

## 早期设计稿（2026-08，已归入本模块 archive）

- [2026-08-26-mailbots-refactor-design](archive/2026-08-26-mailbots-refactor-design.md) —— **新旧系统重构的总体设计**（`mailbots_next` 的由来）
- [2026-08-26-mailbots-plan-a-core-foundation](archive/2026-08-26-mailbots-plan-a-core-foundation.md) —— Plan A 核心地基
- [2026-08-26-mailbots-plan-b-processors-idle](archive/2026-08-26-mailbots-plan-b-processors-idle.md) —— Plan B 处理器（⚠️ **已废弃**：IDLE 方案后来被轮询方案取代）

## 相关代码

`mailbots_next/`（新，就绪未切换）｜`mailbots/`（旧，生产在跑）｜`mailbots_next/core/`（收信 ingest、转发、通知 notify、企微 wecom_notify）
