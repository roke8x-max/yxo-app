# RBAC 模块补强任务书（A+B）

- **日期**：2026-09-29
- **状态**：**正式发出（自包含，给 OpenCode 落码；验收：芙蕾雅）**
- **性质**：本任务书是 `2026-09-21-permission-rbac-module-design.md` **v2.5** 的**补强 addendum**。v2.5 已落码并停工作区待验收；本文件只定义**尚未落码的 5 项补强 + 部署顺序 + 切换安全前置**。未提及的以 v2.5 spec 为准。
- **来源**：ds 两轮评审 + 洋 9/29 决策（g.identity disabled 用**方案 1 统一 system**）。
- **修订**：2026-09-29 收 ds 三点补强——① §4.1 `init_db` 默认翻 `strict=True`（堵 `scripts/init_auth_db.py` 独立入口绕过）；② §1.1 stamp 加「拒代理头」防同机 nginx 反代绕过白名单；③ §3 补 `/api/stamp` 单独抽查 + nginx 暴露检查时机（B 部署后即查，不等 A 灰度）。**收 ds 二轮 4 处——④ §3 nginx 暴露顺序矛盾修正（先 B → 看日志 → 再摘）；⑤ §1 补「非测试」判定移到 `init_auth` + 测试 `compare_digest(None)` 防护；⑥ §2.3 删「或等效」照真实 `SimpleNamespace` 形状；⑦ §2.4 `has_session` 注明 disabled 恒 `False` 不具参考价值。收 ds 三轮 6 处（2 卡点 + 2 细节 + 2 注）——⑧ §2.3 `permissions=role_permissions("system")` 改 `permissions=[]`（方案A；注：当前 `rbac.py:role_permissions` 用 `.get(role,[])` 实际不会崩，但 `[]` 更干净、不依赖兜底语义），role 注明不参与判定；⑨ §1.3/§4.1 抽共享 `auth/common.py:is_production(app)`（`bool()` 包裹防 `PYTEST_CURRENT_TEST` 字符串污染），两处统一调用消除不一致；⑩ §2.4 示例 `path` 改实际路径 `/api/row/123`（与 `request.path` 字段定义一致）；⑪ §3 「nginx 错误日志」改「nginx access log」（403 是 access 非 error）；⑫ §2.3 role 不参与判定注（ds 第5点可选项）。**

---

## 0. 部署顺序（硬约束，ds 第 3 点 + 洋确认）

**B 先、A 后，两次独立部署，不要打包成一次。**

- **B（/api/stamp 加固）**：不依赖灰度开关，旧 DSK/ATB 机器人在跑，现在就做；部署后旧机器人照常工作（白名单 127.0.0.1）。**B 已拆为独立工单**：`2026-09-29-taskbook-B-stamp-hardening.md`（含 §1 四项改动 + §5 B 自测 + §3「B 部署后看 nginx access log」，自包含，可不依赖本文件落码）。
- **A（AUTH_ENABLED 开关）**：后做；部署时 `AUTH_ENABLED=0`（关着 + dry-run 观察），确认无误再翻 `=1`。**A 等 B 部署观察一阵再落**（符合 B 先 A 后）。

---

## 1. B 任务：/api/stamp 加固（独立，立即做）

**现状（已核实）**：
- `app.py:861`：`if request.headers.get("X-Stamp-Token") != STAMP_TOKEN:` —— 用 `!=` 比较（时序侧信道）；无 IP 限制。
- `config.py:21`：弱默认 `yxo_stamp_local_2026`，缺 env 即生效。
- `rbac.py` 已登记 `stamp → public + X-Stamp-Token`（注释写明）。
- 调用方：旧 `mailbots/Dsk_Robot.py:730` / `Atb_Robot.py` 写死 `http://127.0.0.1:5011/api/stamp`，是**同机 localhost 进程**（非企微、非公网）。`mailbots_next` 已绕过此接口直写 yxo.db。

**改动（4 项）**：
1. **拒代理头 + IP 白名单**（纵深防御同机 nginx 反代绕过白名单，ds 部署坑1）：函数体最前先拒代理头、再白名单——
   `if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"): abort(403)`
   `if request.remote_addr not in ("127.0.0.1", "::1"): abort(403)`
   本机 DSK/ATB 机器人直连 `http://127.0.0.1:5011/api/stamp`（`mailbots/Dsk_Robot.py:730`）不带代理头、remote_addr=127.0.0.1，**不误伤**；经 nginx 反代进来的外部请求带 `X-Forwarded-For`/`X-Real-IP` 会被直接拒（白名单命中后再验 token）。
2. **compare_digest**：`import hmac; if not hmac.compare_digest(request.headers.get("X-Stamp-Token", "") or "", STAMP_TOKEN): abort(403)`。
3. **去弱默认 + 缺失拒启（测试环境不崩，ds 第 2 点）**：
   - `config.py`：删除 `yxo_stamp_local_2026` 默认值；`STAMP_TOKEN = os.environ.get("YXSTAMP_TOKEN")`（**导入期不 raise**，因为 `app.debug`/`app.testing` 此时尚未赋值）。
   - **缺失判定移到 `init_auth(app)`**（此处才有 `app` 对象），调用**共享**判定 `from auth.common import is_production; if is_production(app) and STAMP_TOKEN is None: raise RuntimeError("YXSTAMP_TOKEN 未设置，拒绝启动 /api/stamp")`。`is_production` 定义见 §4.2 公共辅助——**§1.3 与 §4.1 统一调它，避免各自写判定出现不一致，ds 第2点**。
   - **`/api/stamp` 函数体内比较前先判 `None`**：`if not STAMP_TOKEN or not hmac.compare_digest(request.headers.get("X-Stamp-Token","") or "", STAMP_TOKEN): abort(403)`。`not STAMP_TOKEN` 短路，**避免测试环境 `STAMP_TOKEN=None` 时 `compare_digest(x, None)` 抛 `TypeError`**；测试 fixture 也可直接 `monkeypatch`/设 env 注入 fake token。
4. `rbac.py` 注释补「白名单 127.0.0.1 + compare_digest，弱默认已删」。

**自测**：本机 `curl -H "X-Stamp-Token: $YXSTAMP_TOKEN" http://127.0.0.1:5011/api/stamp` 成功；从非 127.0.0.1 调用返 403；错 token 不可靠 timing 区分。

---

## 2. A 任务：AUTH_ENABLED 灰度开关 + g.identity 兜底 + dry-run 日志

**现状（已核实）**：
- `auth/__init__.py` 中 `app.before_request(_auth_gate)` **无条件注册**；`config.py` 无 `AUTH_ENABLED`。
- `app.py` 用 `g.identity` 17 处，其中 **13 处裸 `g.identity.username` 零 None 保护**；`g.identity` 唯一注入点是门禁。若门禁不注册，13 处全 `AttributeError`。

**改动**：

### 2.1 配置（config.py）
`AUTH_ENABLED = os.environ.get("YX_AUTH_ENABLED", "0") == "1"`（默认关）。

### 2.2 门禁始终注册，disabled 时「只观察不拒绝」
`init_auth` 仍 `app.before_request(_auth_gate)`（**不摘钩**）。`_auth_gate` 顶部：
```python
if not config.AUTH_ENABLED:
    g.identity = _system_identity()   # username="system"
    _dry_run_log(request)              # 见 2.4
    return                             # 不拒任何请求
# 以下为 enabled 正常逻辑（CSRF + 登录/权限）
```
> **关键**：disabled 时门禁**仍然运行并注入 `g.identity=system`**，因此 13 处裸 `.username` 永不命中 None、不崩。
> **洋决策（方案 1）**：disabled 统一 `system`，**不回退 `?user=`**。理由：前端已删「我是」下拉，方案 2 拦不到真人、只给旧脚本留漏洞；灰度期是暴露问题不是保留旧漏洞。

### 2.3 g.identity 兜底实现
`_system_identity()` 返回与正常 identity **同形状**对象——**照 `auth/__init__.py:_load_identity` 的 `SimpleNamespace` 形状逐字段写，禁止写「或等效」**（ds 第 3 点）：
```python
from types import SimpleNamespace
def _system_identity():
    return SimpleNamespace(
        username="system",
        role="system",        # 仅供形状对齐；disabled 期不参与任何 role 判定（ds 第5点）
        permissions=[],       # disabled 不判权限，空列表最安全（方案1，ds 第1点；不依赖 role_permissions 兜底语义）
        scope_type="all",     # 与 enabled 分支同字段名
        companies=[])         # 与 enabled 分支同字段名
```
> **铁律**：字段名必须含 `scope_type` 与 `companies`。下游 `data/records_dao.py:resolve_scope` 用 `getattr(identity, 'scope_type')` / `getattr(identity, 'companies')` 读取，**不读 `scope`**；enabled 分支 `_load_identity` 也正是这五个字段。disabled 必须完全对齐，否则将来从 disabled 切 enabled 会因字段缺失报错。enabled 时仍由 session 注入真人 identity（不变）。

### 2.4 AUTH_DRY_RUN 日志（ds 第 2 点）
`_dry_run_log(request)` 对每个请求输出一行结构化日志（建议 `logging`，level INFO，前缀 `AUTH_DRY_RUN`；跳过 `static` 与 `OPTIONS/HEAD` 降噪）：

| 字段 | 取值 |
|---|---|
| `path` | `request.path` |
| `method` | `request.method` |
| `ip` | `request.remote_addr` |
| `matched_perm` | 该 `(method, rule)` 在 `ROUTE_PERMISSIONS` 中的判定：`public` / `authenticated` / `<具体权限>` / `未登记` |
| `has_session` | 是否存在有效 session（disabled 下恒 `False`；**⚠️ 本字段仅供切 `enabled` 后对比用，disabled 期恒 `False` 不具参考价值——不代表真实无人登录，只是「未去解析」**） |

示例：`AUTH_DRY_RUN path=/api/row/123 method=POST ip=127.0.0.1 matched=record:write has_session=False`（注意：`path` 取 `request.path` **实际路径** `/api/row/123`，**不是** `request.url_rule.rule` 的 `/api/row/<rid>`；§3 过滤 `path == /api/stamp` 也按实际路径，OpenCode 两处取值口径必须一致，ds 第3点）

> 此日志是「观察期判断会误伤谁」的唯一依据，字段必须齐全，尤其 `ip` 与 `matched_perm`，供 §3 过滤。

### 2.5 灰度通知（部署前，洋/小叽做，非 OpenCode 代码）
上线 `AUTH_ENABLED=0` 前，**明确通知 4 位同事**：观察期内所有数据修改的 `updated_by` 会署名为 `system`（门禁未启用、无真人身份），属预期、不是 bug。

---

## 3. 切换安全前置：旧 mailbots 调用方排查（ds 第 4 点，洋强调为切 AUTH_ENABLED=1 的关键）

- A 部署且 `AUTH_ENABLED=0` 跑足够时间（建议 ≥ 数天覆盖业务高峰）后，拉 `AUTH_DRY_RUN` 日志。
- **过滤条件（受保护接口）**：`ip == "127.0.0.1"` **且** `matched_perm` 命中 `authenticated` 或具体权限（即非 `public`）。
  - 若**有命中** → 旧 localhost 机器人/脚本在调 yxo-app 受保护接口（无 session，开开关后会被 401 挡死、业务断）。处理二选一（OpenCode 按实际选，需洋确认）：
    - (a) 在 `ROUTE_PERMISSIONS` 给这些接口加 `localhost` 例外（仅 127.0.0.1 放行）；**或**
    - (b) 先改 mailbots 让它们带合法 session/token 再调。
- **⚠️ `/api/stamp` 单独抽查（ds 部署坑2）**：`/api/stamp` 的 `matched_perm == public`，**不在上面的过滤条件里**。切门禁前必须单独看 `path == /api/stamp` 的 dry-run 行：若出现 `ip == 127.0.0.1` 但带 `X-Forwarded-For` 代理头、或调用频率远高于正常 mailbot，说明 nginx 已把 `/api/stamp` 暴露公网（白名单被绕过，见 §1.1）。
- **nginx 暴露检查时机（ds 第 1 点，修正顺序矛盾）**：**B 部署后（不提前摘 nginx）立即做一次**——B 已加白名单+拒代理头，若 nginx 仍暴露 `/api/stamp`，外部 stamp 请求会被直接 403，**立刻能在 nginx access log 看到 403 响应（`grep "api/stamp" access.log | grep 403`，这正是最宝贵的观察窗口，证明 nginx 在暴露；注意 403 是 4xx 响应、记在 access.log 而非 error.log，ds 第4点）**。
  - ⚠️ **不要把「摘 nginx location」写成 B 的前置条件**：若先摘再部署 B，就看不到上述 403 观察窗口了。正确顺序：**先部署 B → 看 nginx access log 是否出现外部 stamp 403 → 再摘掉 nginx 的 `/api/stamp` location**（B 已防住，摘掉只是纵深清理）。
  - 小叽的服务器三查（§0 三条命令）只负责**记录**当前 nginx 是否已暴露 `/api/stamp`，**不要求提前摘除**；摘除动作放到 B 部署并确认观察日志之后。
- 仅在「受保护接口过滤为空 **且** `/api/stamp` 抽查无异常（或 nginx 已摘 location）」时才允许把 `AUTH_ENABLED` 翻 `=1`。

---

## 4. 部署安全门禁（沿用 9/29 已确认项，必须落）

### 4.1 init_db 严格模式（ds 9/29 第 3②点）
`auth/schema.py` 当前 `def init_db():`（schema.py:96）无 `strict` 参数、缺失密码即写 `yxo-dev-*` 占位（schema.py:88-93）。**改为安全默认「拒绝」**：
  - `def init_db(strict=True)` —— **默认严格**，缺失 `YXO_AUTH_PASSWORD_*` 即 `raise`，**绝不写 dev 占位口令**。
  - `scripts/init_auth_db.py:35` 直接 `schema.init_db()` 不传参、不走 `init_auth` —— **吃默认 `strict=True`**，生产跑它若没设密码 env 立刻 `raise`，堵住这扇独立入口的窗（ds 关键问题：原 `strict=False` 默认会让该脚本绕过门控写 dev 占位）。
  - `init_auth` 调用传 `init_db(strict=is_production(app))`（`is_production` 见 §4.2）：生产（非 debug/testing/pytest）`strict=True` 拒绝；测试 `strict=False` 放松。**判定口径与 §1.3 唯一一致，不各写各的**。
  - `tests/auth_gate_test.py` 的 fixture **显式** `init_db(strict=False)`（改一行），避免 fixture 崩。

### 4.2 公共辅助 `is_production(app)`（ds 第2点：消除 §1.3 / §4.1 判定不一致）
新增 `auth/common.py`（OpenCode 新建），两处统一调用：
```python
import os
def is_production(app):
    # 唯一判定口径；bool() 包裹避免 PYTEST_CURRENT_TEST 返回字符串污染 or 表达式
    return not (
        bool(getattr(app, "debug", False)) or
        bool(getattr(app, "testing", False)) or
        bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )
```
- §1.3 调用：`if is_production(app) and STAMP_TOKEN is None: raise ...`（见 §1 改动 3）
- §4.1 调用：`init_auth` 内 `init_db(strict=is_production(app))`
- `auth/schema.py` 顶部 `from auth.common import is_production` 以使用。
- ⚠️ 测试 fixture 仍**显式** `init_db(strict=False)` 覆盖，不依赖此判定。
  - **安全默认必须是「拒绝」，测试显式放松，方向绝不能反。**

### 4.3 其余 env 必做（洋/小叽，非 OpenCode 代码）
- 设 `YXO_AUTH_SECRET`（强随机；已同时管 `app.secret_key` + CSRF HMAC，9/29 第 3①点已核实接线正确，仅生产必须设，否则重启后 CSRF 失效、登不进）。
- 设 5 个 `YXO_AUTH_PASSWORD_{MAOXIAOYANG,FENGQIAN,YANGYAWEN,HANWENHAO,VISITOR_DEMO}`。
- 强随机 `YXSTAMP_TOKEN`（见 §1.3）。
- 上线前删本机 `data/auth.db`（避免 dev 占位口令被带上线；schema 对已有用户跳过不覆盖）。
- 生产 `pip install argon2-cffi`（自动升级哈希强度，缺失则降级链仍可用）。

---

## 5. 验收门禁（OpenCode 自测 + 芙蕾雅验收）

OpenCode 落码后须自测并附证据：
- **B**：本机 localhost 调 stamp 成功、非 127.0.0.1 返 403、错 token 用 `compare_digest`、缺 `YXSTAMP_TOKEN` 启动即 `raise`。
- **A**：`AUTH_ENABLED=0` 下 `import app` 不崩、13 处裸 `.username` 不报 `AttributeError`、`AUTH_DRY_RUN` 每行含 5 字段、`matched_perm` 与 `ROUTE_PERMISSIONS` 一致。
- **§4.1**：测试环境 `init_db` 不 `raise`；模拟生产（`debug=False`）缺失密码即 `raise`。
- 新增/修改测试写入 `tests/auth_gate_test.py`（命名避开 gitignore 对 `test_*.py` 误伤）。

---

## 6. 不得改动 / 回归注意

- **不回退**已删的 `?user=` / 「我是」下拉 / localStorage USER（方案 1 明确不恢复）。
- **不改** `g.identity` 13 处调用点（统一在 `_auth_gate` 兜底，调用点免改）。
- B 部署不影响旧机器人（白名单 127.0.0.1 放行）。
- 回滚：spec §12（删 auth.db + git 切回）仍有效；`AUTH_ENABLED=0` 时回滚只需 `YX_AUTH_ENABLED=0` 或 revert，不需删库。

---

## 7. 给 OpenCode 的执行顺序

1. **B**：§1 的 4 项（stamp 白名单 + compare_digest + 去弱默认拒启 + 注释）。
2. **A**：§2.1–2.5（开关配置 + 门禁常注册 + system 兜底 + dry-run 日志）。
3. **§4.1** init_db 严格模式。
4. 自测 + 补测试（§5）。
5. **分别提交/部署**：B 先合、先部署；A 后合、后部署（`AUTH_ENABLED=0`）。
