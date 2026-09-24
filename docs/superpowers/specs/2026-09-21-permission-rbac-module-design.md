# 权限与隔离模块设计（内部简化版 · RBAC + 服务端 Session）

> ⚠️ 设计稿 · 未落码（2026-09-24 归档）：本文是设计阶段产物，代码尚未实现。
> 归档目的是保留“设计意图”供将来参考；看到本文不代表相关模块已落码。

- **日期**：2026-09-21
- **状态**：设计定稿 v2.4（v2.3 基础上：补 2 处落码必崩问题——① §6.1 第二层加 `request.url_rule is None → return`（防 500 替代 404）+ 显式放行 `endpoint=='static'`（防 CSS/JS 被 403 与 §6.3 自检误杀）；② §6.3 自检跳过 static endpoint；顺手精简 §6.1 第一层豁免清单、§14 补「前端不显示 admin 入口」），待 OpenCode 落码、芙蕾雅验收
- **范围决策来源**：洋拍板「内部简化版」（**ds 仅为第三方评审建议，最终决策以洋为准**；本版已按洋决策**加回访客角色**、§9 加反向依赖硬约束、§7 改「不靠猜、按写入表定」）
- **作者**：芙蕾雅（Freya）｜**落码**：OpenCode｜**验收**：芙蕾雅
- **关联文档**：
  - `docs/superpowers/specs/2026-09-08-ticket-system-design.md`（工单系统 spec，其 §「yxo_auth 独立包」假设与本 spec §9 分歧，见 §9）
  - `docs/superpowers/specs/2026-09-06-unified-error-handling.md`（统一错误处理已就位，401/403 直接复用其 JSON 错误格式）

---

## 0. 当前现状（一切以代码为准）

| 事实 | 代码落点 | 说明 |
|---|---|---|
| 无认证 | `README.md:97`「无登录鉴权，4 人全可操作」 | 身份来自前端「我是」下拉框 |
| 后端信任客户端自报 `user` | `app.py:710 / 980 / 1416 / 1589` | `request.args.get("user")` 直接当身份，可伪造 |
| 主数据接口零权限零过滤 | `app.py:638 / 707 / 786 / 812 / 887` | `/api/rows`、`/api/row` PATCH/DELETE、`/api/cells`、`/api/price` 任何人可读写删全量 |
| 仅有的「权限」是名字硬编码比对 | `_check_manifest_user`(app.py:1414 `u=="毛骁洋"`)、`_check_tuoshu_user`(:978)、admin `_check_user`(admin_api.py:161) | 客户端传 `user=毛骁洋` 即全权 |
| `USER_COMPANIES` 现仅用于发信选人 | `config.py:149` | 从不用于数据访问控制 |
| 无 `secret_key`/login/session/csrf | `app.py` 全文件 | 从零建设 |
| 已有 `before_request` 钩子 | `app.py:100` `_assign_req_id` | 鉴权闸门挂同层即可 |

**结论**：当前「权限」= 客户端自报身份 + 几处字符串相等，既无认证也无服务端隔离。本 spec 建立真正闸门。

---

## 1. 目标

1. 真实认证：密码登录，服务端校验，不再信任客户端自报 `user`。
2. 真实授权：RBAC（角色↔权限），服务端强制，绕过 UI 直调 API 也被挡。
3. 数据范围隔离：DAO 层带 `scope` 参数；**访客（外部只读方）本期实现 `companies:[...]` 过滤**，验证隔离真实可用，而非仅预留。
4. 默认拒绝：未登记权限的 `/api` 路由一律 403（安全网），杜绝「半保护」。
5. 模块化但非独立包：auth 代码收口到一个 `auth/` 模块 + 单一 `auth.db`；不强求全量 modular 重构（那是二期）。`auth/` 不得反向依赖业务模块（见 §9）。

---

## 2. 范围决策（砍掉清单 / 必做清单）

> 本节是洋对 ds 二轮反馈的最终拍板，**以本节为准**。ds 的反馈是评审建议，不是决策；涉及「砍掉/增加」某项功能，以洋在本节的明确表述为准。

### 2.1 明确不做（砍掉）

| # | 砍掉项 | 理由 |
|---|---|---|
| 1 | 企微 OAuth / 扫码 / 内网穿透 | 内网 4 人，密码登录足够 |
| 2 | **JWT** | 同源 Flask+静态 app.js，HttpOnly Cookie + 服务端 session + CSRF 更安全；JWT 仅留作未来跨系统 SSO 可选项 |
| 3 | operator 角色 | 暂缓，4 人皆是 admin/manager（**访客角色本期做，见 §2.2**） |
| 4 | **公司映射表（名→ID）** | 4 人全量，字符串隐患暂不触发；未来加 scoped 用户再补 |
| 5 | **独立 `yxo_auth` 可复用包 / 共享物理库** | 内部单应用，一个 `auth.db` 即可；未来工单系统复用再抽包（见 §9） |

### 2.2 必做（最小必要集）

- 密码登录 + **Argon2**（或 bcrypt 兜底），**明文密码零容忍**。
- **服务端 session**：HttpOnly + SameSite=Lax + CSRF token；内网 HTTP 下 Secure 加不了，至少前两者 + CSRF。
- 「记住我」长 session（7–30 天），支持登出/禁用即时失效。
- RBAC：admin / manager（均 `record:all`）+ **visitor（外部只读，`record:read`+`record:export`，无写）**。
- **全路由登记 + 默认拒绝**（未登记 → 403 + 日志）+ **启动自检**（见 §6.3，防漏登打挂）。
- DAO `scope` 注入：**admin/manager（`all`）不过滤；visitor（`companies:[...]`）实际过滤**，验证 §3.3 铁律。
- 审计日志：登录/失败/登出/越权/权限变更/导入/生成托书/改价/删除/导出。
- `updated_by` 从 `g.identity` 取，删除 `request.args.get("user")`。
- 登录限流（失败锁定）。
- 手动改密接口 `POST /api/change_password`（authenticated，仅改本人；非强制改密）。
- 迁移 / 回滚脚本（`auth.db` 初始化、种子、备份）。

### 2.3 对 ds 反馈的采纳（洋确认）

- **第 1 条「无范围≠全量」**：即使 4 人全量也必须坚持，代码**绝不**留「无范围=全量」后门。
- **第 2 条「不能上线即 403」**：合理 → 改为「默认拒绝作兜底 + 部署前全路由登记 + §6.3 启动自检」。
- **第 5 条「4 人全给 record:all」**：合理 → 解耦是模型能力，seed 成 `all` 是当前业务默认值。
- **ds 提「可砍掉访客」→ 洋决策：访客要做**（见 §2.2 / §3.1）。

---

## 3. 角色与范围模型

### 3.1 角色（本期 3 个）

| 角色 | 功能权限 | 数据范围（seed） | 持有者 |
|---|---|---|---|
| `admin` | 全部 + `user:manage` | `all`（`record:all`） | 毛骁洋 |
| `manager` | `record:*` + `manifest:*` + `tuoshu:*` + `price:*` + `config:*` + `admin:view`（不含 `user:manage`） | `all`（`record:all`） | 冯茜 / 杨雅雯 / 韩文豪 |
| `visitor` | `record:read` + `record:export`（**无写**） | `companies:[...]`（外部只读，按负责公司过滤） | `visitor_demo`（本期 seed 的验收账号）+ 未来/当前外部只读方 |

> operator 不在本期；visitor 本期实现（其 `companies:[...]` 范围真正驱动 §8 DAO 过滤，验证 §3.3 铁律）。

### 3.2 范围枚举（模型能力，本期真实生效）

```
scope ∈ { all, companies:[...], none }
```

- `all` → 不追加 `WHERE`，全量（admin/manager）。
- `companies:[...]` → DAO 注入 `WHERE 开票子公司名称 IN (...)`（**visitor 真实使用，必须可用**）。
- `none` / 空 / 缺失 / 配置丢失 → **返回空结果或 403，绝不全量**。（§3.3 铁律）

### 3.3 铁律（最高优先级，不可妥协）

> **无范围、范围为空、配置缺失、迁移失败 → 空或 403，绝不全量。**
> 无论 4 人当前是否全量，代码路径必须如此。这是防「新建用户/漏配/迁移失败变全量泄露」的唯一保险。DAO 实现必须显式区分「`scope==all`」与「`scope` 未设定」两条分支。

### 3.4 权限动词（资源:动作）

```
record:read  record:write  record:export
manifest:import  manifest:apply
tuoshu:generate
price:manage
config:manage  admin:view  user:manage
```

`@require_permission("record:write")` 声明在路由函数上（与 §6 路由表互补，路由表为兜底强制）。

---

## 4. 模块结构（内联 `auth/`，单一 `auth.db`）

```
yxo-app/
  auth/
    __init__.py     # init_auth(app): 注册 auth_bp、挂 _auth_gate、设 secret_key
    routes.py       # /api/login /api/logout /api/meta /api/csrf
    service.py      # 认证/会话/限流/密码校验
    dao.py          # auth.db 读写：auth_users/roles/permissions/audit/attempts
    rbac.py         # 权限定义、require_permission 装饰器、ROUTE_PERMISSIONS 表
    audit.py        # 审计写入
    schema.py       # 建表 + 种子（Argon2 哈希口令）
  data/
    records_dao.py  # records 查询统一入口（注入 scope：all 不过滤 / companies[] 过滤）
  app.py            # 注册 auth_bp；新增 _auth_gate before_request；设 secret_key
  config.py         # 增 AUTH_DB_PATH、SESSION 配置、初始口令来源
```

- **数据库**：单一 `auth.db`（SQLite），路径 `config.AUTH_DB_PATH`。**`yxo.db` 严格只读，不动其结构**（遵循 AGENTS §5）。
- **不抽独立包**：本期 yxo-app 内联；工单系统启动时再抽 `yxo_auth` 包并复用（见 §9）。
- **硬约束（见 §9）**：`auth/` 内不得 import 任何业务模块。

---

## 5. 认证与会话

### 5.1 登录
- `POST /api/login` `{username, password, remember_me?}`
- `service.verify_password`：Argon2 校验（`argon2-cffi`；bcrypt 作兜底）。**明文密码零容忍**。
- 成功 → 建**服务端 session** → `Set-Cookie`：
  - `HttpOnly=true`、`SameSite=Lax`、`Path=/`
  - 若 nginx 已上 HTTPS：`Secure=true`（内网纯 HTTP 暂不加 Secure，但前两者必备）
  - `remember_me` → session 寿命 7–30 天；否则会话级。
- **Session 存储后端（写死，勿留 OpenCode 猜）**：`SESSION_TYPE=sqlite`（Flask-Session 支持），单文件、单进程最稳（内网 4 人）。若后续上 gunicorn 多 worker，改用 `SESSION_TYPE=redis` 或 `filesystem`+单 worker，避免多 worker 文件锁问题。
- 失败 → 记审计 + 触发限流（§5.4）。

### 5.2 登出 / 失效
- `POST /api/logout`：删服务端 session。
- 禁用用户：**标记 `auth_users.disabled` 后，`_auth_gate` 在 §6.1 第二层每次请求都查该标志，命中即 401，即时失效**；也可同时删 session 双保险。不做分布式令牌吊销（单应用 session 删除即够，§2.1 已简化）。

### 5.3 CSRF（覆盖登录接口本身）
- **所有非 GET 请求（含 `POST /api/login`）均需 CSRF token**；这是「CSRF 白名单」——仅 `/api/csrf`、`/api/version`、静态资源豁免（见 §6.1 第一层）。`/api/login` **不豁免 CSRF**。
- **token 来源分两阶段（必须写清，否则 OpenCode 会卡在「登录前拿不到 /api/meta」）**：
  - **登录前**：`GET /api/csrf`（公开）取 token，随 `POST /api/login` 带 `X-CSRF-Token`。
  - **登录后**：前端从 `GET /api/meta`（authenticated）取 CSRF token；之后所有 `fetch` 带 `credentials:'same-origin'` + `X-CSRF-Token`。

### 5.4 登录限流
- `auth_login_attempts` 表：按 username/IP 记录失败。
- 连续失败 5 次 → 锁定 15 分钟 + 响应延迟；审计记「登录失败-锁定」。

---

## 6. 授权门禁（默认拒绝 + 全路由登记）

### 6.1 鉴权闸门 `_auth_gate()`（`@app.before_request`，挂在 `app.py:100` 同层）

闸门分**两层**，顺序执行，专门解决「`/api/login` 既要过 CSRF、又不能因未登录被 401 挡死」的矛盾：

**第一层 · CSRF 校验（所有非 GET 请求）**
- 豁免：GET 请求、静态资源（`endpoint == 'static'`）。（`/api/csrf`、`/api/version` 均为 GET，已被「GET 请求」覆盖，不必单列。）
- 其余非 GET 请求（**含 `POST /api/login`**）必须带合法 `X-CSRF-Token`，否则 **403**。
- 即：`/api/login` **不豁免 CSRF**。

**第二层 · 登录与权限校验（基于 `ROUTE_PERMISSIONS` 表，含页面路由）**
- **前置兜底（防 500 / 防静态资源被误杀，必须先于查表）**：
  1. `if request.url_rule is None: return` —— 若请求未匹配任何路由（如 `/nonexistent`）时 `request.url_rule` 为 `None`，直接 `.rule` 会抛 `AttributeError` → 500 而非 404；判空后交回 Flask 走原生 404。（防御性写法，零成本覆盖自定义 404 / 边界路径。）
  2. `if request.endpoint == 'static': return` —— Flask 的 `/static/<path:filename>` 会出现在 `app.url_map` 但**不在 §7 登记表**；若让其走查表，会因「未登记 → 403」把 CSS/JS 拦成页面裸奔，也会让 §6.3 自检在 DEBUG 下误杀退出。静态资源是框架级路由，不该进业务权限表，故此处显式放行（§6.3 自检同步跳过 `static` endpoint）。
- 对每个请求，按 `request.url_rule.rule`（**含 `<rid>` 占位符的规则串，不是实际 URL 字符串**；例：`/api/row/123` 必须用 `request.url_rule.rule == "/api/row/<rid>"` 查表）查 `ROUTE_PERMISSIONS[method][rule]`：
  - **`public`** → 直接放行，不要求登录。用于：`/api/csrf`、`/api/version`、`/api/login`，以及**所有页面 HTML 路由 `/`、`/tuoshu`、`/manifest`、`/admin`**（见下「页面路由处理」）。
  - **`authenticated`** → 要求已登录（解析 session 注入 `g.identity`）；未登录 → **401 JSON**（前端拦截弹 modal）。
  - **具体权限（如 `record:read`）** → 要求已登录且持有该权限；未登录 → 401；已登录缺权限 → 403（审计「越权访问」）；**未登记 → 403 + 日志（默认拒绝安全网）**。
- **页面路由处理（关键，防 v2.1 同类自锁）**：`/`、`/tuoshu`、`/manifest`、`/admin` 在表中标 `public`，**不拦截、不返 401**——直接返回 HTML。页面照常加载后，前端 JS 调 `/api/meta`：401（未登录）则弹登录 modal，200 则按返回 `permissions` 隐藏无权入口。**若本层对页面路由也返 401 JSON，浏览器只拿到 `{"error":"unauthorized"}`、JS 无机会执行、modal 永远弹不出，登录入口被自锁。故页面 HTML 路由一律公开，权限完全在 API 层强制。**

> 关键：`/api/login` 标 `public` 但仍过第一层 CSRF——登录前 `GET /api/csrf` 取 token → `POST /api/login` 带 token → 通过（否则永远登不进）。所有 `/api/*` 非白名单请求默认拒绝（未登记 → 403）。开发阶段先把 §7.1/§7.2/§7.4 全部路由登记完 + §6.3 启动自检（覆盖全部需权限路由，含页面路由，不仅 /api/*）通过再上线。

### 6.2 装饰器
`@require_permission("...")` 作显式声明（与路由表互补）；路由表是兜底强制，两者并存不冲突。

### 6.3 启动自检（防漏登记打挂应用）
`init_auth(app)` 启动时，对**全部需要权限的路由**做双向比对——**不止 `/api/*`，还包括 `/`、`/tuoshu`、`/manifest`、`/admin` 等页面路由**（否则这些漏登上线后要么打挂、要么越权）：
1. 收集 `app.url_map` 中所有规则（含非 `/api` 前缀的页面路由），**跳过 `endpoint == 'static'` 的规则（框架级静态路由，不进业务权限表，见 §6.1 第二层前置兜底）**，对每个 `(method, url_rule)` 与 `ROUTE_PERMISSIONS` 对比；
2. 两个方向都查：
   - **未登记路由**（url_map 有、表无）：开发环境（`config.DEBUG`）直接报错退出；生产环境 warning + 记日志（仍按 §6.1 默认拒绝 403，但告警暴露漏登）；
   - **反向残留**（表有、url_map 无）：warning（防重构后残留死配置）。
3. 这样「默认拒绝」是安全网而非事故源——页面路由漏登也会被抓出。

---

## 7. 全路由权限登记表（以代码枚举为准）

> 来源：`app.py` 41 路由 + `admin_api.py` 21 路由（grep `@\w*\.route` 实测）。

### 7.1 app.py

| 路由 | 方法 | 所需权限 | 备注 |
|---|---|---|---|
| `/` | GET | public | 首页 HTML，公开；权限在 API 层强制（见 §6.1 页面路由处理） |
| `/api/field_options` | GET | `record:read` | |
| `/api/field_options` | POST | `record:write` | |
| `/api/version` | GET | public | 健康检查白名单 |
| `/api/train_summary` | GET | authenticated | |
| `/api/train_status` | POST | **待定（§7.3）** | 按写入目标表定 |
| `/api/service_status` | GET | authenticated | |
| `/api/rows` | GET | `record:read` | 主数据读取 |
| `/api/row` | POST | `record:write` | 插入 |
| `/api/row/insert` | POST | `record:write` | |
| `/api/row/<rid>` | DELETE | `record:write` | 删除 |
| `/api/trash` | GET | `record:read` | |
| `/api/trash/<rid>/restore` | POST | `record:write` | |
| `/api/trash/<rid>` | DELETE | `record:write` | |
| `/api/row/<rid>` | PATCH | `record:write` | 改行 |
| `/api/cells` | POST | `record:write` | 批量改格 |
| `/api/stamp` | POST | **待定（§7.3）** | 按写入目标表定 |
| `/api/price/<rid>` | POST | `price:manage` | |
| `/api/price/batch` | POST | `price:manage` | |
| `/api/export` | POST | `record:export` | 导出（走 DAO 按 scope 过滤） |
| `/tuoshu` | GET | public | 页面 HTML，公开；入口按 /api/meta 权限隐藏 |
| `/api/tuoshu/meta` | GET | authenticated | |
| `/api/tuoshu/preview` | POST | `tuoshu:generate` | |
| `/api/tuoshu/generate` | POST | `tuoshu:generate` | |
| `/api/tuoshu/dest_map` | GET | authenticated | |
| `/api/tuoshu/dest_map` | POST | `tuoshu:generate` | |
| `/api/import` | POST | **待定（§7.3）** | 按写入目标表定 |
| `/api/import_upload` | POST | **待定（§7.3）** | 按写入目标表定 |
| `/manifest` | GET | public | 页面 HTML，公开；入口按 /api/meta 权限隐藏 |
| `/api/manifest/upload` | POST | `manifest:import` | |
| `/api/manifest/apply` | POST | `manifest:apply` | |
| `/api/manifest/batches` | GET | `manifest:import` | |
| `/api/manifest/batch/<id>` | GET | `manifest:import` | |
| `/api/manifest/revert` | POST | `manifest:apply` | |
| `/api/manifest/restore` | POST | `manifest:apply` | |
| `/api/state` | GET/POST | authenticated | 个人筛选状态 |

### 7.2 admin_api.py（`url_prefix` 见 `app.py:36` 注册，路径前缀 `/api/admin`）

| 路由 | 方法 | 所需权限 |
|---|---|---|
| `/admin` | GET | public | 页面 HTML 公开（空壳）；其下 `/api/admin/*` 数据接口仍按 `admin:view`/`config:manage` 强制（见 §6.1 页面路由处理） |
| `/api/admin/status` | GET | `admin:view` |
| `/api/admin/logs` | GET | `admin:view` |
| `/api/admin/table/<key>` | GET | `admin:view` |
| `/api/admin/table/<key>` | POST | `config:manage` |
| `/api/admin/table/<key>/<record_id>` | PATCH | `config:manage` |
| `/api/admin/table/<key>/<record_id>` | DELETE | `config:manage` |
| `/api/admin/draft_robot_config` | GET | `admin:view` |
| `/api/admin/draft_robot_config` | POST | `config:manage` |
| `/api/admin/bot_config/<name>` | GET | `admin:view` |
| `/api/admin/bot_config/<name>` | POST | `config:manage` |
| `/api/admin/price` | GET | `price:manage` |
| `/api/admin/price/exrate` | POST | `price:manage` |
| `/api/admin/price/entry` | POST | `price:manage` |
| `/api/admin/price/entry/delete` | POST | `price:manage` |
| `/api/admin/price/import` | POST | `price:manage` |
| `/api/admin/price/import/confirm` | POST | `price:manage` |
| `/api/admin/price/recalc` | POST | `price:manage` |

### 7.3 ⚠️ 待定路由的判定规则（不靠猜，按写入目标表定）

> 以下 4 条**不拍脑袋定权限**。由 OpenCode 落码时打开对应函数体，确认实际写入哪张表，按规则定档位，并在函数上方加一行注释 ` # 写入目标表 = <表名>`，然后**回填本表 §7.1**。

| 路由 | 判定规则 | 建议档位 |
|---|---|---|
| `/api/train_status` | 若只读写内部任务状态（不碰 records/manifest/price） | `authenticated` 可保留；若有写库 → `config:manage` |
| `/api/stamp` | 盖章写库，且是 records 的写操作 | `record:write` 可保留；若涉及印章实体单独管理 → 新增 `stamp:execute` |
| `/api/import` | 关键：导入目标是哪张表？ | records → `record:write`；price → `price:manage`；manifest → `manifest:import` |
| `/api/import_upload` | 通常仅暂存上传，不直接写主库 | 仅暂存 → `authenticated`；若直接写库 → 同 `/api/import` |

**通用映射**：写入 `records` → `record:write`；`price` → `price:manage`；`manifest` → `manifest:import`；只读写内部状态、不碰业务表 → `authenticated` 或 `config:manage`。

### 7.4 认证模块自身路由（亦须登记，不可遗漏）

| 路由 | 方法 | 所需权限 | 备注 |
|---|---|---|---|
| `/api/login` | POST | public（**需 CSRF**，见 §5.3/§6.1） | 登录入口：免登录检查但不过 CSRF |
| `/api/logout` | POST | authenticated | 删 session |
| `/api/csrf` | GET | public | 登录前取 CSRF token |
| `/api/meta` | GET | authenticated | 登录后取身份 + CSRF |
| `/api/change_password` | POST | authenticated（仅改本人） | 手动改密，非强制；**须验旧密码（Argon2）+ 新密码强度校验（长度≥12、含大小写数字）** |

> 这些路由同属「全路由登记」范围，§6.3 启动自检应覆盖（auth 蓝图注册后 `url_map` 含它们）。

---

## 8. 数据范围收口（DAO 层，本期真实可用）

- 新建 `data/records_dao.py`：所有 `records` 查询经统一入口，注入 `scope` 条件。
- `scope==all`（admin/manager）→ 不追加 `WHERE`（全量）。
- **`scope==companies:[...]`（visitor）→ 实际注入 `WHERE 开票子公司名称 IN (...)`**；这是 scope 过滤的首个真实使用者，该分支**必须可用，不能仅「预留」**。
- **公司名归一化（防 visitor_demo 验收空结果）**：`companies:[...]` 与 `records.开票子公司名称` 两端匹配前，统一做**去首尾空格、全角→半角、大小写折叠**；且 visitor_demo 绑定的公司名必须与 `yxo.db` 中 `开票子公司名称` 的**实际取值完全一致**——当前实际值为「太平洋、港九港铁」「保时达」「同程配、东盟」「沙坪坝、中欧木业」「联运」，示例绑定前两个完整字符串，**不能用「太平洋」简写**（否则对不上返回空，§14 用例废掉）。
- `scope==none/空/缺失` → DAO 返回空或抛 403（§3.3 铁律）。
- **覆盖所有路径**：导出、统计、聚合、通知、价格、附件、日志——凡读 `records` 必走 DAO。
- **禁止**业务代码散落 raw SQL 绕过 DAO。第一阶段先「查询入口收口」，不强求全量 modular 重构。
- `@require_permission` 只答「能不能」，`records_dao` 答「哪些行」——两者必须同时在场。

---

## 9. 一致性说明（重要分歧，需洋知悉）

1. **与工单系统 spec 的分歧**：`2026-09-08-ticket-system-design.md` 假设「`yxo_auth` 独立包（auth_ 表前缀），工单系统是第一个调用方」。本 spec 改为 **yxo-app 内联 `auth/` + 单一 `auth.db`，不抽包**。理由：单应用、YAGNI、避免过早抽象；工单 spec 的「独立包」是当时假设非既成事实，改它无成本。工单系统（5021/5022）启动时再抽 `yxo_auth` 包复用，彼时 `auth.db` 所有权归 yxo-app——需届时再定。
2. **`USER_COMPANIES`（`config.py:149`）**：本期仅作 seed 参考（4 人 scope=all），不改其现有发信用途。
3. **`README.md:97`「无登录鉴权」**：上线后由 OpenCode 更新该段。
4. **硬约束（抽包零阻力）**：`auth/` 模块内**不得 import 任何业务模块**（records、manifest、tuoshu、price 等）。它只对外暴露 `init_auth(app)`、`g.identity`、`require_permission`、以及 `records_dao` 的 scope 接口。这样未来抽 `yxo_auth` 包只动目录 + import，无反向依赖阻力。

---

## 10. `updated_by` 收口（删除客户端自报）

| 现代码 | 改造 |
|---|---|
| `app.py:710` `user = request.args.get("user","")` | → `g.identity.username` |
| `app.py:980` `u = request.args.get("user") or ...` | → `g.identity.username` |
| `app.py:1416` `u = request.args.get("user") or ...` | → `g.identity.username` |
| `app.py:1589` `user = request.args.get("user") or ...` | → `g.identity.username` |
| `_check_manifest_user`(:1414 `u=="毛骁洋"`) | 改为基于 `g.identity.role` 的服务端判断 |
| `_check_tuoshu_user`(:978) | 同上 |
| admin `_check_user`(:161) | 同上 |
| 前端「我是」下拉 + `localStorage USER` | 删除；改从 `/api/meta` 取身份 |

---

## 11. 审计日志（`auth.db` 内 `auth_audit`）

| 字段 | 说明 |
|---|---|
| ts, user, ip, req_id | 时间/人/来源/请求号 |
| event | 登录成功 / 登录失败 / 登出 / 越权(403) / 权限变更 / 导入 / 生成托书 / 改价 / 删除 / 导出 |
| target, detail | 对象与摘要 |

覆盖：登录成功失败、登出、越权访问、权限变更、导入、生成托书、改价、删除、导出。

---

## 12. 迁移 / 回滚

- **schema 初始化**：`auth/schema.py` 建表 `auth_users / auth_roles / auth_user_roles / auth_permissions / auth_role_permissions / auth_audit / auth_login_attempts`。
- **种子**：
  - 4 用户（Argon2 哈希，口令来源 **仅环境变量 / 密钥文件**，**绝不写进 `config.py` 默认值**——那等于硬编码）；admin/manager 均 `scope=all`。
  - **seed 一个 `visitor_demo` 验收账号**：scope=`companies:[...]`，绑定 `yxo.db` 中 `开票子公司名称` 的**实际取值**「太平洋、港九港铁」「保时达」两个完整字符串（必须与 records 表完全一致，禁用「太平洋」类简写），仅用于 §14 IDOR 验收；真实外部只读方接入时再建正式账号替换，**不影响 4 个内部用户**。
  - （**本期不做强制改密**，初始口令从 env/密钥文件注入；如需可后续补，避免 OpenCode 卡在缺失流程上。）
- **备份**：部署前备份 `yxo.db`（只读不动）+ 新建 `auth.db`。
- **回滚**：删 `auth.db` + git 切回 pre-auth 分支/`app.py`。
- **初始口令**：绝不硬编码进仓库、绝不写进 `config.py` 默认值；**仅从环境变量 / 密钥文件读**。**4 人初始口令应各自独立**（分别注入），避免共用口令互相知悉。**本期提供手动改密** `POST /api/change_password`（authenticated，仅改本人，须验旧密码+新密码强度），不做强制改密。

---

## 13. 前端改造（`static/app.js`）

- 删除「我是」下拉与 `localStorage USER`。
- 新增**登录 modal（不新增独立 `/login` 路由）**：前端拦截 401 → 显示登录 modal → 先 `GET /api/csrf` 取 token → `POST /api/login`（带 CSRF）→ 成功后从 `/api/meta` 取 `{username, role, permissions, scope}` 与 CSRF token。
- 未登录访问受保护路由 → 前端拦截 401 显示登录 modal（**不重定向到独立页**）。
- 所有 `fetch`：`credentials:'same-origin'` + `X-CSRF-Token`。
- 按钮显隐按 `permissions`（仅 UI 友好，非安全边界——真闸门在服务端）。

---

## 14. 验收门禁（DoD）/ IDOR 测试矩阵

| 用例 | 期望 |
|---|---|
| 未登录 `GET /api/rows` | 401 |
| 带旧式 `?user=毛骁洋` 直调 | 被忽略，以 session 为准 |
| 无 session `DELETE /api/row/<rid>` | 401/403 |
| 非 admin 访问 `/admin` 页面 | 看到空壳 HTML（200），**前端不显示 admin 入口**（按 `/api/meta` 的 permissions 隐藏）；其下 `/api/admin/*` 数据接口 → 403（页面公开、权限在 API 层强制，见 §6.1） |
| 连续错密 5 次 | 锁定 15 分钟 |
| 登出后访问受保护接口 | 401 |
| 禁用用户即时失效 | 401 |
| `scope=none` 用户 `GET /api/rows` | 空或 403（**非全量**，防回归） |
| **`visitor_demo`（companies=[太平洋,保时达]）`GET /api/rows`** | **仅返回这两家，非全量（验证 §8 companies[] 过滤真实生效）** |
| **`visitor_demo` `POST /api/row`** | **403（无写权限）** |
| 未登记路由被调 | 403 + 审计「越权/未授权」 |
| 启动自检：漏登路由 | 开发环境退出 / 生产 warning |
| 上述事件均落 `auth_audit` | 通过 |

---

## 15. 工期（4 天内，洋已确认可接受）

| 天 | 内容 |
|---|---|
| D1 | schema+种子 / 登录登出 / 服务端 session(sqlite) / CSRF(含登录) / `_auth_gate` + §7 全路由表（默认拒绝） + §6.3 启动自检 |
| D2 | `updated_by` 收口（4 处）+ 删客户端 user 信任 + 前端登录 modal + `/api/meta` + CSRF 注入 |
| D3 | 审计日志 + 登录限流 + `records_dao` scope 注入（all 不过滤 / companies[] 过滤） |
| D4 | §7.3 四函数写入表确认回填 + IDOR 测试矩阵 + 迁移/回滚脚本 + 联调 + 上线 |

> 落码：OpenCode｜验收：芙蕾雅。⚠️ §7.3 四条路由权限由 OpenCode 落码时按写入表定并回填 §7.1。

---

## 16. 给 OpenCode 的执行单（自包含）

1. 按 §4 建 `auth/`（**不得 import 业务模块**，见 §9）与 `data/records_dao.py`，单一 `auth.db`。
2. 按 §5 实现密码登录（Argon2）+ 服务端 session（**SESSION_TYPE=sqlite**）+ CSRF（**含登录接口，双层闸门见 §6.1**）+ 限流。
3. 按 §6 实现 `_auth_gate` 默认拒绝 + **§6.3 启动自检**；按 §7 登记全部路由（**见 §7.1/§7.2/§7.4**）权限，含页面路由 `/`、`/tuoshu`、`/manifest`、`/admin` 标 `public`。
4. 按 §8 实现 `records_dao` scope 注入（all 不过滤 / **companies[] 真实过滤**），铁律见 §3.3。
5. 按 §10 收口 `updated_by`（4 处 + 3 个 check 函数）。
6. 按 §11/§12 实现审计与迁移/回滚（**本期不做强制改密**；提供 `POST /api/change_password` 手动改密）。
7. 按 §13 改前端（含 CSRF 取 token → 登录 → 带 token）。
8. 按 §7.3 打开 4 个待定函数，标注「写入目标表」并回填 §7.1 权限。
9. 按 §14 跑 IDOR 测试矩阵，全绿后交付芙蕾雅验收。
10. **红线**：不得留「无范围=全量」后门；不得信任 `request.args.get("user")`；不得明文存密码；`auth/` 不得反向依赖业务模块。
