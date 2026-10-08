# gongdan —— 工单系统

**状态：尚未开工**（设计已定稿，代码未落）。

## 当前生效（Current）

暂无。

## 讨论中（Drafts）

暂无。

## 早期设计稿（在 `docs/superpowers/`，不搬迁）

- [`../superpowers/specs/2026-09-08-ticket-system-design.md`](../superpowers/specs/2026-09-08-ticket-system-design.md) —— **工单系统设计定稿**（2026-09-08）

### 该设计的关键约束（摘录，详见上文）

- **架构**：方案 A —— 双进程（5021 事业部 / 5022 渝新欧）共享代码与 `tickets.db`，nssm 托管
- **核心模型**：**「球在谁场地」** —— 只存 `status`(open/progress/closed) + `last_actor_role`，渲染时按访问者身份翻译中文词
- **账号**：复用独立模块 `yxo_auth/`（表前缀 `auth_`）
- **硬约束**：`tickets.db` 必须在**服务器本地盘**（禁 UNC —— SQLite 锁在 SMB 不可靠）；`yxo.db` 严格只读
- **待洋拍板**：C1 公网 HTTPS（最高优先级安全项）/ C2 端口路径 / C3 标题字段 / C5 不支持改关联项

> ⚠️ 本目录在当前文档树里是**空的**（设计稿在 `superpowers/` 不能拆）。工单开工后，新文档放本目录 `specs/`。
