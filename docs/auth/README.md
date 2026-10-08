# auth —— 权限模块

主站的登录 / 会话 / CSRF / 按路由权限门禁。**代码已入库，但生产尚未部署**（开关默认 `AUTH_ENABLED=0`，此时门禁完全不执行）。

## 当前生效（Current）

| 文档 | 讲什么 |
|---|---|
| [角色与权限-勾选结果(确认版)](specs/2026-10-06-角色与权限-勾选结果(确认版).md) | **逐人权限矩阵的唯一权威**：4 人全部 `scope=all`、权重按人配置、废弃 `manager`、需新拆 4 项权限 |

## 讨论中（Drafts）

| 文档 | 讲什么 |
|---|---|
| [权限模块定位与上线spec](drafts/2026-10-06-权限模块定位与上线spec.md) | 上线范围收敛 + G1~G9 缺口清单 + 10 条验收标准。**待毛骁洋拍板** |

## 🔴 上线硬前置（两条，缺一不可）

1. **先给 `manifest.html` 补 CSRF 头** —— 它现在是四个页面里**唯一不带 CSRF** 的；权限模块一翻 `=1`，`/api/manifest/*` 的全部 POST 会当场 403（`auth/README.md` §5）
2. **角色重划落码并验收通过后**，才允许把 `AUTH_ENABLED` 翻成 `1`

> 权限模块的设计本意是**影子期先行**：先部署、开关保持 0（只记账不拦），观察够了再开闸。

## 早期设计稿（已归入本模块）

- **总体设计** → [`specs/2026-09-21-permission-rbac-module-design.md`](specs/2026-09-21-permission-rbac-module-design.md) —— `auth/` 内联包的由来，**仍是现行架构依据**
- **9-29 / 9-30 加固与验收批次** → 在 `archive/`：[rbac-hardening-taskbook](archive/2026-09-29-rbac-hardening-taskbook.md)、[rbac-acceptance-verify](archive/2026-09-29-rbac-acceptance-verify.md)、[taskbook-A-auth-switch](archive/2026-09-29-taskbook-A-auth-switch.md)、[taskbook-B-stamp-hardening](archive/2026-09-29-taskbook-B-stamp-hardening.md)、[rbac-A-acceptance](archive/2026-09-30-rbac-A-acceptance.md)、[defects-nonblocking](archive/2026-09-29-defects-nonblocking.md)、[handoff-xiaoji-stamp-check](archive/2026-09-29-handoff-xiaoji-stamp-check.md)

## 相关代码与说明

- **代码模块说明见 [`../../auth/README.md`](../../auth/README.md)**（内联包、外接点只有三处、`data/auth.db` 七张表）
- 关键实现：`auth/__init__.py`（`before_request` 门禁）、`auth/rbac.py`（角色与权限映射）、`auth/schema.py`（建表）、`scripts/init_auth_db.py`
