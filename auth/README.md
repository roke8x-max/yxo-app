# auth —— 权限模块（RBAC）

> 状态：**代码已入库，生产尚未部署**；灰度开关默认关。
> 目标定位：**4 人自用 + 外部只读**，不做完整审计。

---

## 1. 它是什么

一个**内联包**（不是独立发行包——设计明示"本期内联、不抽包"），为 Flask 主站提供登录、会话、CSRF、按路由的权限门禁。

**设计硬约束（已成立，别破坏）**：`auth/` 内**不得 import 任何业务模块**（只依赖 flask + config + 包内），这样将来若要抽包零阻力。

**外接点只有三处**：
- `app.py` 顶部 `from auth.rbac import require_permission`
- `app.py` 末尾 `_init_auth(app)`
- `auth/__init__.py` 注册 `app.before_request(_auth_gate)`

---

## 2. 数据

- 独立库 **`data/auth.db`**（与 `yxo.db` 分离，`config.py` 的 `AUTH_DB_PATH`）。
- 7 张表：`users` / `roles` / `permissions` / `role_permissions` / `sessions` / `audit` / `login_attempts`。
- 建号脚本：`scripts/init_auth_db.py`。

---

## 3. 门禁怎么生效

唯一拦截层 = `@app.before_request`，按 `(HTTP 方法, Flask rule)` 查表：

- **未登记的路由 → 403**（默认拒绝的安全网）。
- **页面 HTML 一律 public**，权限只在 API 层强制（否则未登录时浏览器只拿到 JSON，登录框永远弹不出来）。
- 两层顺序：**CSRF → 登录/权限**。
- CSRF 豁免清单只有一条：`/api/stamp`（靠它自己的 token + IP 白名单）。

---

## 4. ⚠️ 灰度开关（理解错了会出事）

`AUTH_ENABLED` 默认 **`"0"`**：

| 开关 | 行为 |
|---|---|
| **=0（当前）** | 门禁**提前 return**：CSRF 与权限**两层都不执行**；注入 `system` 身份（全权限 + 全数据范围），只在日志里打 `AUTH_DRY_RUN`。**等价于上线前的行为。** |
| **=1** | 门禁全效。 |

**推论（很重要）**：
- ✅ **"部署代码但开关保持 0" 不会打断任何功能** —— 可以先部署、先观察。
- ⛔ **翻 `=1` 有硬前置**：见下一节。

---

## 5. ⛔ 翻开关的硬前置：先给 `manifest.html` 补 CSRF

**事实**：开关一开，CSRF 立刻生效；而四个页面的实测结果是——

| 页面 | 带 `X-CSRF-Token`？ |
|---|---|
| `static/app.js`（主表） | ✅ |
| `templates/admin.html` | ✅ |
| `templates/tuoshu.html` | ✅ |
| **`templates/manifest.html`** | ❌ **零**（裸 `fetch`） |

⇒ **翻 `=1` 之后，`/api/manifest/*` 的全部 POST 会返回 403，舱单导入页当场不可用。**

**要求**：翻 1 之前，先给 `manifest.html` 补 CSRF（并一并补 401 处理与登录入口）；翻 1 的同一批验收里，必须实跑「舱单上传 → 看差异 → 应用 → 回退/还原」并附输出。

---

## 6. 待修缺口（登记，逐条有行号）

| # | 缺口 |
|---|---|
| G1 | 加固书要求「缺环境变量即拒绝启动」**未落码**：`init_db` 无 `strict` 参数，缺 env 时写弱占位口令 |
| G2 | 测试帐号 `visitor_demo` 种子为**启用态**，代码不禁用，生产靠人工 SQL |
| G3 | `admin.html` / `tuoshu.html` / `manifest.html` **缺登录弹窗**（只有 `index.html` 有） |
| G4 | `.gitignore` 的裸规则 `test_*.py` 只在 `mailbots_next/tests/` 开了例外 ⇒ **在主站新建测试文件会被静默忽略** |
| G5 | 口令哈希依赖未在 `requirements.txt` 声明，实际退到 `pbkdf2` |
| G6 | 同一路由的权限要求存在**两处定义**，会漂移 |
| G7 | **`manager` 角色权限过宽**（≈除用户管理外全部，含"整库还原"）——**已拍板重划**，见 `docs/2026-10-06-角色与权限-勾选结果(确认版).md` |
| G8 | **权限名单散在 5 处且不同步**（`config.USERS`、`config.TUOSHU_ADMINS`、`tuoshu.html` 前端硬编码、`admin_api.LIMITED_ADMINS`(死代码)、`auth` 角色表）——这正是"给某人开了权限他却用不了"的结构性原因 |
| **G9** | **翻开关的硬前置 = 给 `manifest.html` 补 CSRF**（见 §5） |

---

## 7. 角色与权限（现状 → 目标）

**现状**（`rbac.py`）：3 角色 `admin` / `manager` / `visitor`。

**已拍板的目标**（毛骁洋 2026-10-06）：
- **4 人全部 `scope=all`**（都能看到所有公司）⇒ **数据范围不再是区分维度**；`USER_COMPANIES` 只留作邮件/企微的"这票归谁"路由，**不再做数据隔离**；数据隔离只对 `visitor` 有意义。
- **权限按人配置，废弃角色抽象**：`admin` / `hanwenhao` / `yangyawen` / `fengqian` / `visitor`。
- **必须新拆 4 项权限**：从 `record:write` 拆出 `data:import`、`options:manage`、`trash:purge`；从 `config:manage` 拆出 `train:manage`。（回收站的**恢复**仍留在 `record:write`。）

> 逐人矩阵见 `docs/2026-10-06-角色与权限-勾选结果(确认版).md`；落码方案见 `docs/2026-10-06-权限模块定位与上线spec.md`。

---

## 8. 明确不做

- ❌ 不做完整审计能力（审计失败**绝不能打挂业务**——现有写法保持）。
- ❌ 不做权限运营界面。
- ❌ 不做细粒度字段级权限。
- ❌ 不把 `auth/` 抽成独立包。
- ❌ 不改 `yxo.db` 表结构。

---

## 9. 测试

```powershell
& $PY -m pytest -q -p no:cacheprovider tests/auth_gate_test.py
```

覆盖权限门禁矩阵（逐路由 401/403 对照）。⚠️ 它是**主站唯一**的测试文件——其余主站逻辑（算价、写入、导出）**没有测试**。
