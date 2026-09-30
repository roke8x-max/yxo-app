# RBAC 补强 · A 工单：AUTH_ENABLED 灰度开关 + system 兜底 + dry-run 日志（独立落码）

- **日期**：2026-09-29
- **状态**：**正式发出（自包含，给 OpenCode 落码；验收：芙蕾雅）**
- **性质**：本工单是 `2026-09-21-permission-rbac-module-design.md`（v2.5）+ `2026-09-29-rbac-hardening-taskbook.md`（A+B 总任务书）中 **A 段**的**独立拆单**。A 复用 B 已落地的 `auth/common.py:is_production`（同分支部署必然已存在，不得重复定义）。**A 与 B+v2.5 同一次部署：部署时 `AUTH_ENABLED=0` ⇒ B 的 stamp 加固立即生效，v2.5 门禁关着（dry-run），`updated_by` 署 `system`。**
- **落码：OpenCode；验收：芙蕾雅。**
- ⚠️ **本工单对总任务书 §2 做了两处修正**（已 grep 核实代码事实，非拍脑门）：
  - §1.3：`system.permissions` 必须是**全权限**（`list(PERMISSIONS.keys())`），**不是 `[]`**——否则 disabled 模式整站 403。
  - §1.2：噪声过滤**只留 `static` + 404**，**不能跳 HEAD**——HEAD 会实际执行 GET 视图。

---

## 0. 代码现状（已核实，无需再查）

- `auth/__init__.py`：`app.before_request(_auth_gate)` **无条件注册**（第 126 行）；`config.py` 无 `AUTH_ENABLED`。
- `_auth_gate`（第 46–81 行）：CSRF 第一层 + 登录/权限第二层，**无开关分支**。
- `app.py` 用 `g.identity` 共 17 处，其中 **13 处裸 `g.identity.username`** 零 None 保护；`g.identity` 唯一注入点是门禁。若门禁不注册 → 13 处全 `AttributeError`。
- **⚠️ 已 grep 核实的权限判定点（证明 `permissions=[]` 会踩雷，见 §1.3）**：除 `@require_permission` 装饰器（app.py **6 处视图**：645/711/729/748/793/820 行）外，还有**内联判定**也依赖 `ident.permissions` 非空，disabled 模式会一并挡死：
  - app.py:1003 `_check_tuoshu_user`：`"tuoshu:generate" in (ident.permissions or [])`
  - app.py:1447 `_check_manifest_import`：`"manifest:import" in (ident.permissions or [])`
  - app.py:1453 `_check_manifest_apply`：`"manifest:apply" in (ident.permissions or [])`
  - admin_api.py:166 `perms = ident.permissions or []` → 175 行返回 403
  - 以上全部要求 `ident.permissions` 非空；给 `[]` 则 disabled 模式整站 403。

---

## 1. 改动

### 1.1 配置（config.py）
在「权限认证」段（约第 159 行 `AUTH_LOCK_MINUTES` 之后）加：
```python
# RBAC 灰度开关（A 工单）：默认关。关 = 门禁只观察不拒绝（dry-run），
# system 身份全权限 + scope=all 放行，等价于 RBAC 上线前行为；开 = 门禁全效。
AUTH_ENABLED = os.environ.get("YX_AUTH_ENABLED", "0") == "1"
```

### 1.2 门禁始终注册，disabled 时「只观察不拒绝」
`auth/__init__.py` 顶部补 `import config`（模块级）。`_auth_gate` 结构改为**先噪声过滤、再开关分支**（噪声过滤仅 `static` 与 `url_rule is None`）：
```python
def _auth_gate():
    # 噪声过滤（disabled 与 enabled 都要跳）
    if request.url_rule is None:          # 404 路径
        return None
    if request.endpoint == "static":
        return None
    # ⚠️ 不跳 OPTIONS / HEAD：HEAD 会实际执行 GET 视图（见 §5 回归注意），
    #    若在此 return 会让 13 处裸 .username 在 HEAD 上 AttributeError。

    # —— A 工单：灰度开关 ——
    if not config.AUTH_ENABLED:
        g.identity = _system_identity()   # username="system"，全权限 + scope=all
        _dry_run_log(request)             # 见 1.4
        return None                        # 不拒任何请求

    # 以下为 enabled 正常逻辑（CSRF + 登录/权限），保持现有实现不变
    ...
```
> **关键**：disabled 时门禁**仍然运行并注入 `g.identity=system`**，因此 13 处裸 `.username` 永不命中 None、不崩；所有权限判定点（`@require_permission` + 内联 `ident.permissions` 三处 + admin_api）因 system 持**全权限**而全部放行 —— disabled 模式 == RBAC 上线前行为。
> 早返回 `return None` ⇒ Flask 继续走视图；视图层 `@require_permission` / 内联 `ident.permissions` 判定此时见到的是 system（全权限）→ 通过。

### 1.3 g.identity 兜底实现（**修正总任务书 §2.3**）
`_system_identity()` 返回与正常 identity **同形状**对象——照 `auth/__init__.py:_load_identity` 的 `SimpleNamespace` 形状逐字段写（ds 第 3 点，禁止「或等效」）：
```python
from types import SimpleNamespace
# 顶部 import 行补 PERMISSIONS：from auth.rbac import AUTHENTICATED, PUBLIC, ROUTE_PERMISSIONS, ROUTE_PERMISSIONS_SET, PERMISSIONS

def _system_identity():
    return SimpleNamespace(
        username="system",
        role="system",                              # 仅供形状对齐；disabled 期不参与任何 role 判定
        permissions=list(PERMISSIONS.keys()),       # ⚠️ 修正：必须全权限，非 []
        scope_type="all",                           # 与 enabled 分支同字段名；records_dao 放行全量
        companies=[])                               # 与 enabled 分支同字段名
```
> **⚠️ 修正说明（务必照做，漏做 = disabled 整站 403）**：总任务书 §2.3 原写 `permissions=[]`，理由「disabled 不判权限，空列表最安全」。但**已 grep 核实**（§0）：`@require_permission`（6 处）+ app.py 三处内联 `ident.permissions` 判定 + admin_api.py:166 的 `ident.permissions` 判定，全部依赖 `ident.permissions` 非空。给 `[]` ⇒ disabled 模式这些判定把**全站 403 挡死**（门禁早返回拦不到视图层/内联判定）。故 `permissions` 必须是 `list(PERMISSIONS.keys())`（全部权限），使 disabled 模式等价于「门禁完全不生效」。`scope_type="all"` 同理让 `records_dao.resolve_scope` 全量返回（等同上线前）。
> **铁律**：字段名必须含 `scope_type` 与 `companies`。下游 `data/records_dao.py:resolve_scope` 用 `getattr(identity,'scope_type')` / `getattr(identity,'companies')` 读取，不读 `scope`；enabled 分支 `_load_identity` 也正是这五个字段。disabled 必须完全对齐，否则切 enabled 会因字段缺失报错。
> enabled 时仍由 session 注入真人 identity（不变）；system 身份**仅在 disabled 期存在**，切到 enabled 后无越权残留。

### 1.4 AUTH_DRY_RUN 日志（ds 第 2 点）
`_dry_run_log(request)` 对每个**非噪声**请求输出一行结构化日志（`logging`，level INFO，前缀 `AUTH_DRY_RUN`）：

| 字段 | 取值 |
|---|---|
| `path` | `request.path`（**实际路径**，如 `/api/row/123`，不是 `request.url_rule.rule`） |
| `method` | `request.method` |
| `ip` | `request.remote_addr` |
| `matched_perm` | 该 `(method, rule)` 在 `ROUTE_PERMISSIONS` 中的判定：`public` / `authenticated` / `<具体权限>` / `未登记`（`rule` 用 `request.url_rule.rule` 查表，`path` 用 `request.path` 记，两处口径见本表，ds 第3点） |
| `has_session` | disabled 期恒 `False`（**不解析 session**，仅供切 enabled 后对比；不代表真实无人登录） |

实现要点：
```python
import logging
logger = logging.getLogger("auth")

def _dry_run_log(req):
    if req.url_rule is None:
        matched = "未登记"
    else:
        key = (req.method, req.url_rule.rule)
        matched = "未登记" if key not in ROUTE_PERMISSIONS_SET else ROUTE_PERMISSIONS[key]
    logger.info("AUTH_DRY_RUN path=%s method=%s ip=%s matched=%s has_session=False",
                req.path, req.method, req.remote_addr, matched)
```
> 此日志是「观察期判断会误伤谁」的唯一依据，字段必须齐全（`ip` + `matched_perm` 供 §3 过滤）。`path` 取实际路径，与 §3 过滤 `path == /api/stamp` 口径一致。

---

## 2. 共享辅助（已随 B 落地，直接复用，勿重复定义）

- `auth/common.py:is_production(app)`：A 若需判定生产，直接 `from auth.common import is_production`。**该文件已由 B 工单创建，A 不得重复定义。**

---

## 3. A 自测项（OpenCode 落码后须自测并附证据）

- `AUTH_ENABLED=0`（默认）下 `import app` 不崩；13 处裸 `g.identity.username` 读取返回 `"system"`、不 `AttributeError`。
- disabled 模式对**任意**受保护接口均返回 **200**（全权限放行），**不 401/403** —— 证明 §1.3 修正生效、内联判定未挡死：
  - `POST /api/row`（record:write）
  - `GET /api/admin/status`（admin:view）
  - `POST /api/tuoshu/generate`（tuoshu:generate，过 app.py:1003 内联判定）
  - `POST /api/manifest/apply`（manifest:apply，过 app.py:1453 内联判定）
  - admin_api 任意管理接口（过 admin_api.py:166 的 `ident.permissions` 判定）
- disabled 模式 `AUTH_DRY_RUN` 每行含 5 字段；`matched_perm` 与 `ROUTE_PERMISSIONS` 一致；`path` 为实际路径（如 `/api/row/123`）。
- `AUTH_ENABLED=1` 时门禁恢复全效：未登录 `GET /api/rows` → 401；visitor `admin:view` → 403；`/api/stamp` 仍按 B 逻辑（与开关无关）。
- 全仓 `pytest -q` 不退化；`tests/auth_gate_test.py` 现有用例在 `AUTH_ENABLED` 默认 0 下仍应通过（若既有用例假设门禁生效，需在 fixture 显式设 `AUTH_ENABLED=1`，但**不得为过 A 而删既有断言**）。
- 启动自检（`_self_check`）在 disabled 下仍跑（url_map 比对与开关无关），missing/stale 行为不变。

---

## 4. 部署注意（非落码，洋/小叽做）

- **与 B+v2.5 同批**：A 落码后提交到 **`rbac-v2.5-stamp-b` 分支**（即 PR #25 所在分支），使一次 merge 同时部署 B+v2.5+A。部署时 **`YX_AUTH_ENABLED` 不设置（默认 0）** ⇒ B 的 stamp 加固立即生效，v2.5 门禁关着（dry-run）。
- **灰度通知（部署前）**：上线 `AUTH_ENABLED=0` 前，明确通知 4 位同事——观察期内所有数据修改的 `updated_by` 会署 `system`（门禁未启用、无真人身份），属预期、不是 bug。
- **dry-run 观察（≥数天覆盖业务高峰）**：拉 `AUTH_DRY_RUN` 日志，过滤 `ip=="127.0.0.1"` **且** `matched_perm` 命中 `authenticated`/具体权限（非 `public`）→ 若有命中，说明旧 localhost 机器人在调受保护接口，开开关后会被 401 挡死；处理二选一：① 给这些接口加 127.0.0.1 例外；② 先改机器人带合法 session。
- **`/api/stamp` 单独抽查**：其 `matched_perm==public`，不在上面过滤条件。切门禁前必须单独看 `path==/api/stamp` 的 dry-run 行：若 `ip==127.0.0.1` 但带 `X-Forwarded-For` 或频率异常 → nginx 已暴露 `/api/stamp`（B 白名单被绕过，见 B 工单 §4）。
- **翻 `=1` 前置**：仅当「受保护接口过滤为空 **且** `/api/stamp` 抽查无异常（或 nginx 已摘 location）」才允许设 `YX_AUTH_ENABLED=1`（重启生效）。
- **回滚**：`AUTH_ENABLED=0` 时回滚只需 `YX_AUTH_ENABLED=0` 或 revert，不需删 auth.db；切到 1 后回滚同 B/v2.5（删 auth.db + git 切回）。

---

## 5. 不得改动 / 回归注意

- **不得**把 `_auth_gate` 在 disabled 时摘钩（仍 `before_request` 注册）——摘钩会让 13 处裸 `.username` 崩，且失去 dry-run 观察能力。
- **不得**在噪声过滤里跳 `HEAD`：`HEAD` 请求 Flask 会**实际调用 GET 视图**（返回体被剥离），若在此 `return` 不注入 system，视图内 13 处裸 `.username` 命中 None → `AttributeError`。仅 `static` 与 `url_rule is None`（404）可跳。
- **不得**改 B 的 stamp 逻辑、不得改 v2.5 的 `records_dao` scope 判定；A 只加开关 + system 兜底 + 日志。
- `permissions` 必须是 `list(PERMISSIONS.keys())`（全权限）——**这是 §1.3 对总任务书 §2.3 的修正，漏做会导致 disabled 模式整站 403**。
- system 身份仅在 disabled 期存在；切 enabled 后由 session 注入真人，无越权残留。
