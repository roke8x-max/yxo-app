# 权限模块 G1–G9 落码任务书（给 OpenCode）

> **读者**：OpenCode（无状态，本任务书须自包含）。禁止"按上次说的办"——所有背景、为什么、行号都已写明。
> **基线**：`main` @ `99474db`（实时核实：本地/远端均此值，工作树干净）。
> **切分支**：从 `main` 切扁平命名临时分支（如 `chore-auth-g1-g9`，**不带斜杠**）。
> **范围**：`auth/`、`config.py`、`app.py` 的权限接线处、`admin_api.py`、`templates/*.html`、`.gitignore`、`requirements.txt`、`scripts/init_auth_db.py`、`tests/auth_gate_test.py`。
> **权威依据**：角色/权限矩阵以 `docs/auth/specs/2026-10-06-角色与权限-勾选结果(确认版).md` §2 为准；本任务书不重抄矩阵，执行方直接引该文。
> **上游 spec**：`docs/auth/drafts/2026-10-06-权限模块定位与上线spec.md`（G1–G9 出处，已待毛骁洋拍板，本任务书即其落地）。

---

## 0. 别部署旧版（最高优先）

- `main` 当前的 auth 角色模型（`admin`/`manager`/`visitor` 三角色，含 `manager` 的整库还原权限）**是过时的**，请勿按 `docs/ops/archive/2026-09-30-deploy-handoff-combined.md` 部署。
- **执行方只落码，不部署、不翻 `AUTH_ENABLED`、不碰生产 `auth.db`。** 落码 + 芙蕾雅验收通过后，才由芙蕾雅出新版部署单（`AUTH_ENABLED=0` 影子期起步）。
- 服务器侧有冻结通知（`docs/ops/drafts/2026-10-08-部署冻结-勿跑9-30旧单.md`）已要求小叽不得部署旧模型。

---

## 1. 背景（为什么做这些）

1. RBAC 代码已合入 `main`，但**生产从未部署**（小叽未执行旧部署单），生产现处"无鉴权 + 署名为 system"状态。
2. 洋 2026-10-06 勾选确认新版权限矩阵，**推翻了 9 月设计**：废弃 `manager` 角色、改按人角色、从 `record:write`/`config:manage` 新拆 4 项权限、4 位内部同事 `scope=all`。
3. 本任务书 = 雪儿权限 spec G1–G9 的落地，目标是：把代码对齐确认版 + 补齐加固书里写了但没落码的缺口 + 修两个已登记未修缺陷。

---

## 2. G1–G9 逐条

### G1 · `init_db` 缺环境变量即拒绝启动（strict）

- **文件**：`auth/schema.py:101` `init_db()`；调用方 `auth/__init__.py:159`、`scripts/init_auth_db.py:35`。
- **当前事实**：`init_db()` 无参数；`_seed_password`（`:93-98`）缺 env 时返回开发占位 `yxo-dev-<key>` 仅 WARNING，绝不报错。
- **改法**：
  - `init_db(strict: bool = False)`。
  - strict=True 时，遍历"必需账号"的 env key，任一缺失即 `raise RuntimeError("缺少必需口令环境变量 YXO_AUTH_PASSWORD_<KEY>")`。
  - **必需账号范围（洋 2026-10-08 确认含外部只读账号）**：4 位内部同事 `MAOXIAOYANG`/`FENGQIAN`/`YANGYAWEN`/`HANWENHAO` + 外部只读方 `游客`/`YOUKE`（权威来源 `auth/schema.py:22-34` `SEED_USERS`）。`visitor_demo` 为禁用演示账号（G2 置 `disabled=1`），**不**进 strict 必需清单。
  - **strict 判定逻辑**：遍历 `SEED_USERS`，凡未标记 `disabled` 的账号，其 `YXO_AUTH_PASSWORD_<ENVKEY>` 缺失即 `raise`。由此自动覆盖上述 5 个必需账号、自动排除 `visitor_demo`，无需手写名单。
  - **顺带修正文档**：`scripts/init_auth_db.py:9` 的 docstring 当前列了 `_VISITOR_DEMO` 却漏 `_YOUKE`，须改为列出 `MAOXIAOYANG/FENGQIAN/YANGYAWEN/HANWENHAO/YOUKE`（`VISITOR_DEMO` 已禁用，从"必需口令"描述中去掉）。
  - `auth/__init__.py:159` 与 `scripts/init_auth_db.py:35` 调用处改为 `init_db(strict=True)`。
  - **测试/调试自动降级（防 fixture 崩，雪儿 spec G1）**：`init_db` 内部算 `enforce = strict and not (os.environ.get("PYTEST_CURRENT_TEST") or _test_or_debug())`；`_test_or_debug()` 实现固定为 **`try: from flask import current_app; return bool(current_app.testing or current_app.debug) except Exception: return False`**（惰性 import 避免循环导入；无 app 上下文时 `current_app.testing` 抛 RuntimeError 被兜底 → False）。即：生产/普通启动严格生效；pytest 运行期（`PYTEST_CURRENT_TEST` 由 pytest 设置）与 Flask 测试/调试模式自动降级为不严格，避免 `init_auth(app)`（`:159`）在测试套件因口令 env 未设而崩。`tests/auth_gate_test.py` 须显式设 5 个口令 env（或 fixture 直接 `init_db(strict=False)`），确保现有用例不崩。
  - **⚠️ 排序前置**：本项**须晚于或同批于 G2 落地**——本项 strict 判定读取 `SEED_USERS` 的 `disabled` 标记，该标记由 G2 引入（G2 未落地时 `visitor_demo` 无 disabled 标记，strict 会误要求其口令）。
- **验收**：
  - 分别清空 `YXO_AUTH_PASSWORD_MAOXIAOYANG` 与 `YXO_AUTH_PASSWORD_YOUKE` 后启动 → 进程均直接失败并给出明确错误（`缺少必需口令环境变量 YXO_AUTH_PASSWORD_xxx`），**绝不**写入 `yxo-dev-*` 占位；清空 `YXO_AUTH_PASSWORD_VISITOR_DEMO` 不影响（该账号被 G2 禁用，不进必需清单）。附实跑输出末几行。
  - **新增 strict 单测（用干净子进程证明，避开关卡互斥）**：同进程内 `PYTEST_CURRENT_TEST` 恒置位 → `enforce` 恒降级，**无法**证明 strict 真生效，故"清空口令 env 后 `init_db(strict=True)` 须 `raise RuntimeError`"必须在**子进程**里验证（ds 推荐方案 2）：探测脚本位置须明确——写到 `tests/_strict_probe.py`（`_` 前缀避开 pytest 测试收集），或在测试内用 `tempfile.NamedTemporaryFile` 现场生成。测试用 `subprocess.run([sys.executable, 该脚本路径], env={清空全部 YXO_AUTH_PASSWORD_*}` 在**临时库路径**下执行（**隔离必做**：子进程 env 须同时设 `YXO_AUTH_DB`=<临时文件>，因 `config.py:152` 用 `os.environ.get("YXO_AUTH_DB", ...)` 解析库路径，不设会落到生产 `data/auth.db`；注：正确 env 名是 `YXO_AUTH_DB`，不是 `YXO_AUTH_DB_PATH`）；脚本内容仅两行：`from auth.schema import init_db` + `init_db(strict=True)`（子进程无 `PYTEST_CURRENT_TEST`、无 app 上下文 → `enforce=True`）→ 断言返回码非 0 且 stderr 含 `缺少必需口令环境变量`。另：同进程用例验证"设齐 5 env 后 `init_db(strict=True)` 成功"与"pytest 运行期下 `init_auth(app)` 不崩（降级生效）"。

### G2 · `visitor_demo` 种子直接置 `disabled=1`

- **文件**：`auth/schema.py:22-34` `SEED_USERS`；`:116-126` `init_db` 种子循环。
- **当前事实**：`SEED_USERS` 元组为 4 元素 `(role, scope, companies, env_key)`；`visitor_demo`（`:28`）未含 disabled，种子 INSERT（`:121-126`）不写 disabled 列（默认 0=启用）。
- **改法**：元组扩为 5 元素 `(role, scope, companies, env_key, disabled=False)`；`visitor_demo` 置 `disabled=True`。`init_db` 种子 INSERT 增加 `disabled` 列写入（注意 `INSERT OR IGNORE` 幂等：已存在行跳过，所以**改完需重建库或 ALTER**——见验收）。
- **⚠️ 排序前置**：本项须**先于或同批于 G1** 落地（G1 strict 判定依赖本项引入的 `disabled` 标记，见 G1 排序说明）。
- **验收**：全新库跑 `scripts/init_auth_db.py` → `SELECT username,disabled FROM auth_users WHERE username='visitor_demo'` 返回 `1`。**注意**：幂等跳过已存在行，验收须用全新 `auth.db`（删旧库后重跑）。

### G3 · `admin.html` / `tuoshu.html` 补登录弹窗

- **当前事实**：grep 确认全仓仅 `templates/index.html:205` 有 `<div id="loginModal" class="user-gate hidden">`；`admin.html`/`tuoshu.html` 无等价弹窗，未登录时页面加载但无法登录。
- **改法**：以 `index.html:205` 的 `loginModal` 为模板，在 `admin.html`、`tuoshu.html` 复制等价登录弹窗 + 未登录 `/api/auth_meta` 返回 401 的处理逻辑。
- **验收**：未登录直接开 `/admin`、`/tuoshu` → 页面弹出登录框且能登录成功（两个页面各附一次实跑/截图证据）。

### G4 · 修 `.gitignore` 裸 `test_*.py`

- **文件**：`.gitignore:72` `test_*.py`；`:74` 仅 `!mailbots_next/tests/test_*.py` 例外。
- **当前事实**：裸规则会静默忽略主站或其他目录新建的 `test_*.py`（如 `tests/test_xxx.py`）。
- **改法**：保留 `mailbots_next/tests` 例外，新增 `!tests/test_*.py`（主站测试可入库）；或将裸规则改为路径限定。
- **验收**：`git check-ignore -v tests/auth_gate_test.py` 返回空（不再命中）。

### G5 · 声明口令哈希依赖（意图明确）

- **文件**：`requirements.txt`（仅 `flask`/`openpyxl`）；`auth/service.py:40-42`（实际退到 `pbkdf2`）。
- **当前事实**：无 argon2/bcrypt 声明，哈希退化为 stdlib `pbkdf2`。
- **改法**：在 `requirements.txt` 加注释说明"口令哈希使用 stdlib `pbkdf2`（有意选择，不引入额外依赖）"；或在 `auth/service.py` 注释写明。
- **验收**：`pip install -r requirements.txt` 一次装齐；`auth/service.py` 哈希路径可运行；注释说明所选算法。

### G6 · 路由权限清单单一来源（**决策：移除装饰器，单一来源 = 中央 `ROUTE_PERMISSIONS` 门禁**）

- **文件**：`auth/rbac.py` `ROUTE_PERMISSIONS`（`:77-145`，强制在 `auth/__init__.py:71` `_auth_gate` 的 `before_request`）；`app.py:26` 装饰器导入 + `:645/711/729/748/793/820` 的 `@require_permission`；`app.py:1001` `_check_tuoshu_user()` 定义，调用点 `:1176/1193/1212/1301/1332`。
- **当前事实（已核）**：`auth/__init__.py:_auth_gate` 是 `before_request`，对**所有**路由按 `ROUTE_PERMISSIONS` 表强制、未登记默认拒绝 403（`:94-95`），启动自检（`_self_check` `:116`）还会标出漏登路由；`@require_permission`（rbac.py:151）是视图层**完全重复**的二次判断，与中央表判定逻辑相同。另 `app.py:1001 _check_tuoshu_user()` 在 **5 处**（`dest_map` GET/POST 等）自定义判断，**绕过了中央表**（表内 GET dest_map=`AUTHENTICATED`、POST=`tuoshu:generate`，视图却自定义）。
- **改法（拍板：单点，移除装饰器）**：
  - 删除 `app.py:26` 的 `from auth.rbac import require_permission` 导入 + 6 处 `@require_permission(...)` 装饰器（`:645/711/729/748/793/820`）。**并删除 `auth/rbac.py:151` 的 `def require_permission(perm):` 定义本体**（先 `grep -rn "require_permission" auth/ app.py` 确认全仓除已删处外无其余引用；为保持"路由权限单一来源"目标，要求彻底删定义，不保留死函数）。
  - 删除 `app.py:1001` `_check_tuoshu_user()` 函数及全部 5 个调用点（`:1176/1193/1212/1301/1332`），统一走中央表（dest_map GET/POST 已在表内，无需视图级判断）。**删前核查（防误删改行为）**：已核 5 个调用点上下文均为 `if not _check_tuoshu_user(): return _tuoshu_forbid()` 纯拒绝/放行权限检查（`:1176/1193/1212/1301/1332`），返回值不参与后续业务分支，故直接删安全；OpenCode 落码时仍须逐点确认无 `if _check_tuoshu_user(): ... else: 其它路径` 类业务分支——若有，先登记（不顺手改逻辑）再处理。
  - **安全前置（防误删引入越权/误放）**：删除装饰器前，逐一对 6 个视图确认「装饰器 perm 参数 == `ROUTE_PERMISSIONS[(method,rule)]`」；**以中央表为权威**（spec §2.1-8），若二者不一致，**改表使其一致**（必要时同步 G7 的权限重划），**绝不**为对齐去复活装饰器。G7 的 `ROUTE_PERMISSIONS` 重写须先于/同于本步完成，保证删除装饰器后中央表已是正确的最终权限（事实依据：6 个装饰器路由均已在中央表登记，且中央表 default-deny，故移除装饰器不改变鉴权结果，纯去重）。
- **验收**：`grep -rn "require_permission\|_check_tuoshu_user" app.py` 全空；`grep` 证明同一路由权限只在 `ROUTE_PERMISSIONS` 一处定义；`tests/auth_gate_test.py` 全绿；`_self_check` 无漏登/反向残留告警。

### G7 · 角色重划（最大项，按确认版）

**权威**：`docs/auth/specs/2026-10-06-角色与权限-勾选结果(确认版).md` §2 逐人矩阵。

- **文件**：`auth/rbac.py:31-60`（`PERMISSIONS`/`ROLES`/`ROLE_PERMS`）、`:77-145` `ROUTE_PERMISSIONS`；`auth/schema.py:22-34` `SEED_USERS`。
- **当前事实**：`ROLES={"admin","manager","visitor"}`；`ROLE_PERMS` 中 `manager` 含除 `user:manage` 外全部权限（含 `manifest:apply` 整库还原）；`schema.py` 把 3 位同事种子为 `manager`。
- **改法**：
  - `PERMISSIONS` 新增：`data:import`、`options:manage`、`trash:purge`、`train:manage`（保留原 10 项）。
  - `ROLES = {"admin","hanwenhao","yangyawen","fengqian","visitor"}`（**删 `manager`**）。
  - `ROLE_PERMS` 按确认版 §2 矩阵重写：`admin`=全+`user:manage`；`hanwenhao`/`yangyawen`/`fengqian`=各自权限（见确认版 §2 表）；`visitor`=`record:read`+`record:export`。
  - `ROUTE_PERMISSIONS` 映射更新：
    - `("POST","/api/import")` 与 `("POST","/api/import_upload")`：`record:write` → **`data:import`**
    - `("POST","/api/field_options")`：`record:write` → **`options:manage`**
    - `("DELETE","/api/trash/<int:rid>")`：`record:write` → **`trash:purge`**（restore `POST /api/trash/<int:rid>/restore` 留 `record:write`）
    - `("POST","/api/train_status")`：`config:manage` → **`train:manage`**
  - `SEED_USERS`：冯茜/杨雅雯/韩文豪由 `("manager",...)` → `("fengqian"/"yangyawen"/"hanwenhao","all",[],<ENVKEY>)`；毛骁洋保持 `admin`；`visitor_demo`/`游客` 保持（visitor 角色）。
- **验收**：用每个角色（`admin`/`hanwenhao`/`yangyawen`/`fengqian`/`visitor`）各一个测试账号登录，对每一项权限跑"该做能做、该不能做返回 403"，输出逐项实测表；专门验证前端一致性（无权限者**看不到**入口，非看得到点了报错）；证明 `manager` key 已不存在于代码；证明 `manifest:apply`（含 revert/restore）、`price:manage`、`config:manage`、`data:import` 仅 `admin` 持有。

### G8 · 收敛散落名单（"谁能做什么"只有一处定义）

- **文件**：`config.py:30` `USERS`、`:33` `TUOSHU_ADMINS`；`admin_api.py:32` `ADMIN_USER`、`:36` `LIMITED_ADMINS`；`templates/tuoshu.html:158` 前端硬编码名单；`auth/rbac.py:52-60` 角色权限。
- **当前事实**："谁能做什么"定义散在 5 处，且彼此不同步（已核实上述 4 个位置 + rbac）。
- **改法**：
  - **先 `grep` 全仓确认 `USERS`/`TUOSHU_ADMINS` 无读取点**（仅定义、无引用）后再删；若 grep 发现仍有读取点，先改为从 auth 身份派生（如 `g.identity.role` / `g.identity.scope_type` / `g.identity.companies`——身份对象是 `auth/__init__.py:41` 的 `SimpleNamespace`，属性为 `.companies`（由 DB 列 `scope_companies` 解析而来），**不是** `.scope_companies`），**不得保留硬编码名单**，再删常量。
  - **先 `grep` 确认 `admin_api.py:32 ADMIN_USER`、`:36 LIMITED_ADMINS` 无引用**（已核实为死代码，仅定义），确认后删除。
  - `templates/tuoshu.html:158` 前端硬编码名单改为从服务端返回（经 `/api/auth_meta` 或新增接口）。
  - **保留** `config.py:166 USER_COMPANIES`（用途唯一：邮件转发/企微通知"这票归谁"路由）与 `config.py:173 USER_EMAILS`（邮件路由）。
- **验收**：grep 证明"谁能做什么"只有 `auth/rbac.py` 一处定义；4 个死常量已删；`tuoshu.html` 不再硬编码名单。

### G9 · `manifest.html` 补 CSRF（翻开关硬前置）

- **文件**：`templates/manifest.html`（grep 确认零 CSRF 头，`:189-194` 是裸 `fetch`）；`auth/__init__.py:81-90`（开关=0 时 CSRF 与权限两层都不执行，开则 CSRF 立即生效；豁免清单仅 `auth/service.py:181` `CSRF_EXEMPT_PATHS={"/api/stamp"}`）。
- **当前事实**：翻 `AUTH_ENABLED=1` 后，`/api/manifest/upload`、`/api/manifest/apply`、`/api/manifest/revert`、`/api/manifest/restore` 会全部 403，舱单页当场不可用。
- **改法**：给 `manifest.html` 的 fetch 加与其余三页（`static/app.js`/`admin.html`/`tuoshu.html`）等价的 CSRF 头；**一并补 401 处理与登录入口**（与 G3 同款）。
- **⚠️ 落码前强制独立复核（spec §2.1-11，毛骁洋 2026-10-06 明确要求）**：OpenCode **动手前**，本条须先由 **芙蕾雅** 独立复核一次——对照 spec §2.1-11 与确认版矩阵，确认 CSRF 头取值、豁免清单（`auth/service.py:181` `CSRF_EXEMPT_PATHS`）、401 处理与登录入口与另外三页一致。复核通过并**留痕（谁/何时/结论）** 后，OpenCode 方可落码。（小叽为服务器侧运维，不参与 CSRF 代码复核，本条复核仅由芙蕾雅承担。）
- **验收**：**落码前**须附芙蕾雅独立复核留痕；**翻 `AUTH_ENABLED=1` 之前**完成；同批验收给证据：翻 1 后舱单上传→看差异→应用→回退/还原逐项实跑通过（附输出）。
- **安全边界**："部署代码但开关保持 0"不打断任何功能，可先部署观察。

---

## 3. 明确不做（避免顺手扩范围）

- 不重写权限模块（只收敛范围 + 补缺口 + 修缺陷）。
- 不做审计查询界面/报表、不做权限运营界面（角色/权限增删改）、不做字段级权限、不把 `auth/` 抽成独立包、不改 `yxo.db` 表结构、不动 `AUTH_ENABLED` 灰度语义、不碰工单系统/邮件机器人/企微机器人。

---

## 4. 回归验收（汇总）

- `tests/auth_gate_test.py`（32 例）全绿。
- `mailbots_next/tests/` 全套通过（不受影响，但若改动波及 auth 导入须重跑）。
- 各 G 项独立验收见 §2。

---

## 5. 执行方硬约束

1. **禁止 `commit` / `push` / 开 PR**；代码完成即停，报告等验收。
2. `git add <具体文件>`，**禁 `git add -A`**；分支名扁平不带斜杠，从 `main` 切。
3. 报告须附实跑证据：命令 + 输出末几行 + `文件:行号`。禁"已证实""应该没问题"。
4. **不得为测试变绿放宽任何业务/权限判据**；测试红先定性（数据/环境/逻辑）。
5. 测试不得连生产库，用临时库。
6. 发现计划外问题**只登记，不顺手修**。
7. 与 `AGENTS.md` 冲突时以 `AGENTS.md` 为准并立即报告。
8. **G9 落码前须芙蕾雅独立复核并留痕**（spec §2.1-11；小叽为服务器侧，不参与 CSRF 代码复核），未附复核结论不得视为完成。

---

## 6. 待毛骁洋确认项

- **G1「必需账号」范围（已拍板）**：4 位内部同事（`MAOXIAOYANG`/`FENGQIAN`/`YANGYAWEN`/`HANWENHAO`）+ 外部只读方 `游客`/`YOUKE`。`visitor_demo` 禁用演示账号，不进 strict 清单。
- 其余 G 项均按确认版与 spec 执行，无歧义。
