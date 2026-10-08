# ops —— 部署 / 环境 / 发布 / 仓库治理

装机器、配认证、部署、回滚、仓库可见性与分支保护。**这一模块的文档是"照着做"的操作手册，不是设计稿。**

## 当前生效（Current）

| 文档 | 讲什么 |
|---|---|
| [新环境接入](specs/新环境接入.md) | **每台新机器 / 每次重新 clone 都要走一遍**：clone → **装 hook** → 配身份 → 配代理（服务器专用）→ 配 GitHub 认证（PAT） |
| [仓库转私有](specs/仓库转私有.md) | 转私有的操作 + 转后每台机器要做什么 + 常见误解 |

## 讨论中（Drafts）

暂无。

## 已归档（Archive，6 份）

| 文档 | 讲什么 |
|---|---|
| [2026-09-11-VERP标签部署验证手册](archive/2026-09-11-VERP标签部署验证手册.md) | VERP 标签的部署验证 |
| [2026-09-14-上线前执行清单(给小叽)](archive/2026-09-14-上线前执行清单(给小叽).md) | 邮件机器人上线前的服务器执行清单 |
| [2026-09-14-提交方案与PR切分](archive/2026-09-14-提交方案与PR切分.md) | 当时那批改动的提交与 PR 切分方案 |
| [2026-09-29-deploy-checklist-B](archive/2026-09-29-deploy-checklist-B.md) | 部署清单 B |
| [2026-09-30-deploy-handoff-combined](archive/2026-09-30-deploy-handoff-combined.md) | 合并后的部署交接单（D-1 部署权限模块时按它执行） |
| [2026-09-30-taskbook-D4-dryrun-logging](archive/2026-09-30-taskbook-D4-dryrun-logging.md) | D4：dry-run 日志丢失问题的任务书 |

## ⚠️ 两条必须记住的环境事实

1. **hook 不跟着 clone 走** —— 它存在 `.git/hooks/`，不入库。**每台机器、每次重新 clone 都要重装**（`scripts/install-hooks.ps1`）。转私有后 GitHub 免费版**不再提供服务端分支保护**，本地 hook 就从"第二道防线"变成**唯一一道防线**。
2. **`gh` CLI 可用，与面板状态无关** —— 本机 `gh` 已登录（`roke8x-max`，含 `repo` 权限），可直接 `gh pr create` / `gh pr view`。**不要因为看到连接器面板显示 GitHub "disconnected" 就以为不能用 CLI。**（2026-10-08 核实：这是两个独立通道）

## 相关代码与脚本

`scripts/install-hooks.ps1`｜`scripts/hooks/{pre-push,post-fetch}`｜`scripts/deploy.ps1`｜`scripts/rollback.ps1`｜`scripts/deploy/DEPLOYMENT_MANUAL.md`（旧邮件机器人 nssm 部署手册）
