# G9 舱单页 CSRF + 登录入口 落码任务书（OpenCode）

> 本任务书**仅含 G9**，独立于已合并的 G1–G8（PR #30，commit `a2f1198`）。
> 分支：`chore-auth-g9`（扁平命名，禁斜杠）。
> **独立复核已通过**：芙蕾雅于 2026-10-09 完成最终 gate 复核（spec §2.1-11 硬性要求），结论=可落码；仅芙蕾雅复核，小叽不参与（CSRF 代码复核超其服务器侧范围）。

---

## §1 规格依据（spec 原文要点，2026-10-06 确认版 §2.1-11）

- G9 是「翻 `AUTH_ENABLED=1` 的硬前置」：翻开关**之前**必须给 `manifest.html` 补上与其余三页等价的 CSRF 头，并一并补 401 处理与登录入口（见 G3）。
- 后果：开关一开，`/api/manifest/upload|apply|revert|restore` 会因缺 CSRF 全部返 403，舱单页当场不可用。
- 安全边界：『部署代码但开关保持 0』不会打断任何功能，可先部署观察。
- 执行方落码前须芙蕾雅独立复核一次（本条已签）。

---

## §2 当前事实（main @ `a2f1198` 后，已核）

**`templates/manifest.html`（待改页）**
- `api()` 包装 `:189-194`：`const r = await fetch(path, opts)`，仅判 `!r.ok` 抛错 —— **无 CSRF 头、无 401 处理、无登录入口**。
- 全文件 grep `csrf|login|modal|401` **零命中** → 三件套全缺。
- 受影响非 GET 接口：`api/manifest/upload`（`bindUpload` 内）、`api/manifest/apply`（`:648`）、`api/manifest/revert`（`:795`/`:800`）、`api/manifest/restore`（`:806`）。

**门禁与 CSRF 基础设施（已就位，勿改）**
- `auth/__init__.py:81-84`：`AUTH_ENABLED=0` 时门禁 `return`，CSRF 与权限两层都跳过。
- `auth/__init__.py:88-90`：开关一开，CSRF 层立刻生效；`:89` `if request.path not in service.CSRF_EXEMPT_PATHS and not service.verify_csrf(request)` → 403。
- `auth/service.py:183` `CSRF_EXEMPT_PATHS = frozenset({"/api/stamp"})` —— **不含任何 manifest 接口**。
- `auth/service.py:20-21`：cookie 名 `csrf_seed`、头名 `X-CSRF-Token`。
- 取种路由 `auth/routes.py:26-31`：`GET /api/csrf`（PUBLIC，`auth/rbac.py:151`）回 `{ok:True, csrf_token:<hmac>}`。

**范本（照抄，不得另起炉灶）**
- `templates/admin.html` 已实现的完整三件套：
  - HTML 弹层 `:87-88` `<div id="loginModal" class="hidden">…</div>`（含 `loginUser`/`loginPass`/`loginGo`/`loginErr`/`loginRemember`）。
  - 脚本 `:270-370`：`let CSRF_TOKEN=""` + `window.fetch` 重写（same-origin、非 GET 挂 `X-CSRF-Token`）+ `showLoginModal(msg)`（`:328`）+ 全局 401→弹框（`:352-361`）+ 初始化取种 `_origFetch("api/csrf",…)→CSRF_TOKEN`（`:362-368`）。
- `templates/tuoshu.html:174-242` 同款（可作交叉参照）。

---

## §3 实现配方（在 `manifest.html` 落地，逐条对照 admin.html 范本）

1. **复制登录弹层 HTML**：取 `admin.html:87-88` 整块 `<div id="loginModal">…</div>` 粘到 `manifest.html` 的 `<body>`（元素 id 完全一致：`loginModal`/`loginUser`/`loginPass`/`loginGo`/`loginErr`/`loginRemember`）。
2. **脚本顶部加 CSRF 基础设施**：在 `manifest.html` `<script>` 顶部加
   `let CSRF_TOKEN = "";` 与 `window.fetch` 重写（与 `admin.html:274-285` 逐行一致：设 `credentials:"same-origin"`、非 GET 同源且 `CSRF_TOKEN` 存在时挂 `opts.headers["X-CSRF-Token"]`）。
3. **改 `api()`（`:189-194`）**：保留现有 `!r.ok` 判错；**新增** `if (r.status === 401) { showLoginModal(); }` 分支（401 先弹登录层再 throw），保证受保护接口未登录时弹框而非静默失败。
4. **复制登录逻辑**：把 `showLoginModal(msg)`（含 `api/login` POST → `location.reload()`）与全局 401 重写（`admin.html:328-361`）粘入 `manifest.html` 脚本。
5. **初始化取种**：在 `init()`（`:196`）开头加
   `_origFetch("api/csrf", {credentials:"same-origin"}).then(r=>r.json()).then(j=>{CSRF_TOKEN=j.csrf_token||"";})`（与 `admin.html:362-368` 同）。
   > **注意**：`manifest.html` 当前**不调用** `api/auth_meta`（已核实 grep 全文件 `auth_meta`/`csrf`/`X-CSRF`/`api/csrf` **零命中**），故无此 401 分支；未登录兜底完全由第 3 步 `api()` 的 401→`showLoginModal()` 分支承担，第 5 步**无需**额外加 `auth_meta` 处理。
- **机制说明**：第 2 步已重写 `window.fetch`，`manifest.html` 的 `api()` 内部 `fetch(path, opts)` 自动继承 —— 非 GET 同源 POST 会**自动带 `X-CSRF-Token`**，无需在 `api()` 手动加头。

---

### §3.1 落码自检（防 JS 细节坑，ds 提醒）

OpenCode 落码时逐项肉眼确认，避免解析期/作用域坑：

- **`_origFetch` 必须先定义**：第 2 步 `window.fetch` 重写时须先保存原始引用（如 `let _origFetch = window.fetch;` 再 `window.fetch = function...`），且 `init()` 取种调用（第 5 步）位于其后——同 `<script>` 作用域内 `let` 顺序或函数提升均满足，但须确认 `_origFetch` 不是未声明标识符。
- **`showLoginModal` 须为函数声明（hoisted）**：第 3 步 `api()` 的 401 分支引用 `showLoginModal`，而该函数体在第 4 步才粘入。用 `function showLoginModal(msg){...}` 声明可提升、解析期即可引用；若误写成 `const showLoginModal = (msg)=>{...}` 则存在 TDZ 风险（虽运行时 `api()` 在用户操作后才触发、引用必已存在，但落码时仍须确认其为函数声明而非 const 箭头，避免隐患）。


---

## §4 验收

**A. OpenCode 交付前（`AUTH_ENABLED` 保持 0）**
- 静态：grep `manifest.html` 确认存在 `CSRF_TOKEN`、`X-CSRF-Token`、`showLoginModal`、`loginModal`、`api/csrf`，且 `api()` 含 401 分支。
- 本地起服务（开关 0）开 `/manifest`：未登录也能看到页面（开关 0 不拦）；上传/应用/回退/还原走通（开关 0 无 CSRF 校验，应正常）。
- 测试（建议）：在 `tests/` 加一个断言 `manifest.html` 含 `#loginModal` 与 `api/csrf` 取种脚本的用例，防回退。
- （建议，非强制）开关 0 本地走通**不能**证明「开关 1 时 CSRF 真挂上」。建议追加：在测试环境翻 `AUTH_ENABLED=1`，构造**带** `X-CSRF-Token` 的 POST 期望 200、**不带**期望 403（断言中央 `_auth_gate` 在开关 1 下生效）；若全局开关难在单测翻，则以本段 grep 静态证据为准，并依赖 §4B 翻 1 实跑兜底。

**B. 翻 1 实跑（由洋/小叽在测试环境执行，非 OpenCode 职责）**
- 翻 `AUTH_ENABLED=1` 后，舱单 **上传 → 看差异 → 应用 → 回退 → 还原** 逐项实跑通过（附输出）。此步是 spec 硬性要求，OpenCode 无法在 prod 翻开关，列为独立 gate，由洋确认。

---

## §5 硬约束

- **独立复核已签**：芙蕾雅 2026-10-09 最终 gate 通过（spec §2.1-11）。无未决项。
- **部署安全**：本改动先部署、开关保持 0 不破坏任何功能（spec §2.1-11 明示），可先合并观察。
- **不做范围**（spec §2.2）：不扩审计能力、不建权限运营界面、不做字段级权限；**不改 manifest 业务逻辑**，只补 CSRF + 登录入口 + 401 处理。
- **git 纪律**：分支 `chore-auth-g9`；`git add` **仅** `templates/manifest.html`（及 §4A 要求的测试文件），**禁 `git add -A`**，不带入 `docs/*drafts*`；commit 由芙蕾雅执行、PR 由芙蕾雅开、merge 由洋在网页端 Squash。

## §6 待洋确认项

- 无。G9 范围与配方在 spec 与 admin/tuoshu 范本中已完全确定；翻 1 实跑由洋/小叽执行。
