# 任务书：改密码界面 + 后台用户管理（OpenCode 执行）

- 日期：2026-10-23
- 派单：芙蕾雅 · spec 见 `docs/auth/specs/2026-10-22-改密界面与后台用户管理-spec.md`
- 业务拍板：毛骁洋 2026-10-22（Q1 全踢会话 / Q2 不做强制改密 / Q3 不做改 scope / Q4 后台可见性保持现状）
- **代码基线：main `44ecb66`**（PR #41 合并后）
- 目标分支：从最新 main 拉**扁平命名**临时分支（连字符、不带斜杠），如 `feat-change-pw-and-user-admin`
- **优先级**：不阻塞下一次 pull（影子期），但**翻 `YX_AUTH_ENABLED=1` 之前必须完成**

---

## 〇、开工前必读

1. `AGENTS.md` 硬规则（尤其 5：不改 `yxo.db` 表结构 —— 本次只碰 `auth.db`，且**不加列**）。
2. `docs/auth/README.md`（模块说明）+ `docs/auth/specs/2026-09-21-permission-rbac-module-design.md`（权限门禁设计）。
3. 跑测试基线，判据 **`0 failed`**（不要写死条数）：
   ```powershell
   $PY = "<这台机器的 python.exe 绝对路径>"
   & $PY -m pytest -q -p no:cacheprovider
   ```
   芙蕾雅侧实测基线：**600 passed / 0 failed**（2026-10-22）。

### 硬红线

- ❌ **不要 `git commit` / `push` / 开 PR** —— 落码后停在工作区，报告即可。
- ❌ **不要改登录/会话机制**，不要改 `ROLE_PERMS` 权限矩阵，不要给 `auth_users` 加列。
- ❌ **不要动 `yxo.db` 任何东西**（本次与订舱记录无关）。
- ❌ **不要把用户管理接口写进 `admin_api.py`** —— 那里的 `_check_user()`（`admin_api.py:158-168`）只认 `config:manage`/`price:manage`，会造成第二套判定。**一律放 `auth/routes.py`**，靠中央门禁按 `user:manage` 拦。
- ❌ **`pw_hash` 不得出现在任何响应里**（包括 `reset_password` 的返回）。
- ❌ **明文密码不得进审计日志、不得进数据库**。

---

## 一、要做的四件事（一句话版）

| # | 改什么 | 关键点 |
|---|---|---|
| 1 | **影子期守卫**（4 个接口） | `AUTH_ENABLED=0` 时一律 403 —— 否则内网谁都能重置所有人密码 |
| 2 | **踢会话**（改密/重置/停用） | `dao.delete_user_sessions(username)`；改密连自己一起踢 |
| 3 | **主站「修改密码」弹窗** | 影子期不显示；成功后弹登录层 |
| 4 | **后台「用户管理」tab** | 仅 `user:manage` + `AUTH_MODE==="auth"` 可见 |

---

## 二、后端（`auth/routes.py` + `auth/dao.py` + `auth/rbac.py`）

### 2.1 `auth/dao.py`：新增 `list_users()`

放在 `list_shadow_users()`（`:151-159`）旁边，**不复用 `get_user()`**（那是 `SELECT *`，含 `pw_hash`）：

```python
def list_users():
    """后台用户管理：全部用户（含游客、含已停用），按 id 排序。绝不返回 pw_hash。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, username, role, scope_type, scope_companies, disabled, created_at "
            "FROM auth_users ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
```

（`connect()` 已设 `row_factory = sqlite3.Row`，`dict(r)` 即可 JSON 化。）

### 2.2 `auth/routes.py`：影子期守卫（**4 个接口都要**）

在 `_err` 下面加一个小helper：

```python
def _shadow_denied():
    """影子期（AUTH_ENABLED=0）无鉴权：所有账号安全相关写操作一律拒绝。"""
    if not config.AUTH_ENABLED:
        return _err("HTTP_403", "该功能仅在登录模式可用（当前为影子期）", 403)
    return None
```

每个视图**第一行**：

```python
    denied = _shadow_denied()
    if denied:
        return denied
```

要加在：`api_change_password`（`:102`）+ 下面三个新视图。

> ⚠️ `_err` 直接把 code 串塞进 JSON，**没有错误码表**，`"HTTP_403"` 可以直接用（`_deny` 已在用，`auth/__init__.py:98/103/123`）。

### 2.3 `api_change_password` 补踢会话（`:102-117`）

在 `dao.set_password(...)` **之后**、`log_event` 之前加一行：

```python
    dao.delete_user_sessions(username)      # 改密后旧会话立即失效（含当前会话）
```

⚠️ 前端拿到 200 后会被踢下线，属预期（见 §3.1）。

### 2.4 三个新接口（都放 `auth/routes.py`）

```python
@auth_bp.route("/api/admin/users", methods=["GET"])
def api_admin_users():
    denied = _shadow_denied()
    if denied:
        return denied
    return jsonify({"ok": True, "users": dao.list_users()})


@auth_bp.route("/api/admin/users/reset_password", methods=["POST"])
def api_admin_user_reset_password():
    denied = _shadow_denied()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return _err("HTTP_400", "缺少 username", 400)
    if not dao.get_user(username):
        return _err("HTTP_404", "用户不存在", 404)

    # password 语义（写死）：字段不存在或 None → 生成随机密码；空串 → 400；非字符串 → 400；有值 → 过强度
    if "password" in data and data["password"] is not None:
        raw = data["password"]
        if not isinstance(raw, str):
            return _err("HTTP_400", "新密码格式错误", 400)     # 别把 123 报成"不能为空"
        if raw == "":
            return _err("HTTP_400", "新密码不能为空", 400)
        ok, msg = service.password_strength(raw)
        if not ok:
            return _err("HTTP_400", msg, 400)
        new_pw, generated = raw, False
    else:
        new_pw, generated = _gen_password(), True

    dao.set_password(username, service.hash_password(new_pw))
    dao.delete_user_sessions(username)               # 旧会话必须失效
    audit.log_event("重置密码", g.identity.username, target=f"user:{username}")
    resp = {"ok": True, "username": username,
            "self": username == getattr(g.identity, "username", "")}   # 目标是自己 → 前端要弹登录层
    if generated:
        resp["password"] = new_pw                    # 只在随机生成时回传一次
    return jsonify(resp)


@auth_bp.route("/api/admin/users/set_disabled", methods=["POST"])
def api_admin_user_set_disabled():
    denied = _shadow_denied()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    flag = _to_bool01(data.get("disabled"))
    if not username:
        return _err("HTTP_400", "缺少 username", 400)
    if flag is None:
        return _err("HTTP_400", "disabled 只能是 0 或 1", 400)
    if not dao.get_user(username):                   # 先 404，再 403（顺序别反）
        return _err("HTTP_404", "用户不存在", 404)
    if username == getattr(g.identity, "username", ""):
        return _err("HTTP_403", "不能停用自己的账号", 403)
    dao.set_disabled(username, bool(flag))
    if flag:
        dao.delete_user_sessions(username)           # 停用即时踢下线
    audit.log_event("停用账号" if flag else "启用账号",
                    g.identity.username, target=f"user:{username}")
    return jsonify({"ok": True, "username": username, "disabled": flag})
```

> `get_user()` 是 `SELECT *`（会带出 `pw_hash`，但不进响应），这里只用来判存在，**够用**；
> 想更省可以另写 `user_exists()`，**非必须**，别为此扩大改动面。
>
> `_gen_password` 放 `auth/routes.py` 与视图同文件即可；若你更愿意放 `auth/service.py`（与 `password_strength` 相邻、更好单测）**也可以**——
> **两处选一处，别两边都放**。

**两个辅助函数**（放 `auth/routes.py` 顶部，`import secrets, string`）：

```python
def _gen_password(length=14):
    """生成满足 password_strength 的随机密码：三类字符各先放 1 个，再补随机，最后打乱。
    不要写成"纯随机 12 位再祈祷合规"——审查时会问"重试也失败怎么办"。"""
    upper, lower, digits = string.ascii_uppercase, string.ascii_lowercase, string.digits
    pool = upper + lower + digits
    for _ in range(50):
        chars = [secrets.choice(upper), secrets.choice(lower), secrets.choice(digits)]
        chars += [secrets.choice(pool) for _ in range(max(0, length - 3))]
        secrets.SystemRandom().shuffle(chars)
        pw = "".join(chars)
        ok, _msg = service.password_strength(pw)
        if ok:
            return pw
    raise RuntimeError("随机密码生成失败")


def _to_bool01(v):
    """disabled 归一化：1/True/'1'/'true' → 1；0/False/'0'/'false' → 0；其他 → None。"""
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int) and v in (0, 1):
        return v
    if isinstance(v, str) and v.strip().lower() in ("0", "1", "true", "false"):
        return 1 if v.strip().lower() in ("1", "true") else 0
    return None
```

### 2.5 `auth/rbac.py`：登记三个新路由（**漏了会 403 / DEBUG 起不来**）

加到 `ROUTE_PERMISSIONS` 的 auth/admin 段（`change_password` 那条 `:159` 附近）：

```python
    ("GET",  "/api/admin/users"):                 "user:manage",
    ("POST", "/api/admin/users/reset_password"):  "user:manage",
    ("POST", "/api/admin/users/set_disabled"):    "user:manage",
```

> 未登记 → 门禁默认拒绝 403（`auth/__init__.py:100-103`），且 **DEBUG 下启动自检直接抛错**（`:146-149`）。PR 里请确认 `_self_check` 无 `未登记路由` 警告。

---

## 三、前端

### 3.1 主站「修改密码」（`templates/index.html` + `static/app.js`）

**HTML**：`templates/index.html:12-17` 的 `topbar-right`，`#logoutBtn` 后面加：

```html
<button id="chpwBtn" class="tbtn ghost hidden" title="修改密码">修改密码</button>
```

**弹窗**：照 `#loginModal`（`:204-217`）的 `user-gate` 结构，加 `#chpwModal`：
旧密码 / 新密码 / 确认新密码 / 提示行 / 取消 + 提交（提交按钮 id 用 `#chpwGo`）。

**JS（`static/app.js`）**：

```js
function openChangePw() {
  ["chpwOld", "chpwNew", "chpwNew2"].forEach((i) => { const el = document.getElementById(i); if (el) el.value = ""; });
  const err = document.getElementById("chpwErr"); if (err) err.textContent = "";
  document.getElementById("chpwModal").classList.remove("hidden");
  const first = document.getElementById("chpwOld"); if (first) first.focus();
}

function submitChangePw() {
  const oldPw = document.getElementById("chpwOld").value;
  const p1 = document.getElementById("chpwNew").value;
  const p2 = document.getElementById("chpwNew2").value;
  const err = document.getElementById("chpwErr");
  const btn = document.getElementById("chpwGo");
  if (!p1) { err.textContent = "新密码不能为空"; return; }          // 前端只校验这两条
  if (p1 !== p2) { err.textContent = "两次输入的新密码不一致"; return; }
  btn.disabled = true;                       // 防重复提交
  fetch("api/change_password", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old_password: oldPw, new_password: p1 }),
  }).then((r) => r.json()).then((j) => {
    btn.disabled = false;
    if (!j.ok) { err.textContent = j.msg || "修改失败"; return; }   // 不清空输入框
    document.getElementById("chpwModal").classList.add("hidden");
    _pending = {};                            // 清待提交队列（变量名已核实：app.js:1222 `let _pending = {}`）
    toast("密码已修改，请用新密码重新登录");
    showLoginModal("密码已修改，请用新密码重新登录");
  }).catch(() => { btn.disabled = false; err.textContent = "网络错误，请重试"; });
}
```

> ✅ **`_pending` 就是真实变量名**（`static/app.js:1222` `let _pending = {}`，`flushPending()` 在 `:1227` 读的也是它）。
> 直接 `_pending = {}` 即可清空，**不要改名、不要新建变量**。
>
> ✅ **`showLoginModal(msg)` 接受一个参数**（已核）：`static/app.js:42-47` 会把 `msg` 写进 `#loginErr`；
> 后台页的同名函数（`templates/admin.html:338-342`）签名一致。所以传文案能显示在登录框里，**不用改签名**。

**显隐**：`applyAuthMode()` 里（`static/app.js:2557` 那行"影子期无登出"旁边）：

```js
  const chpwBtn = document.getElementById("chpwBtn");
  if (chpwBtn) chpwBtn.classList.toggle("hidden", AUTH_MODE !== "auth");
```

> ⚠️ 别在文件顶部就判 `AUTH_MODE`：它的初始值故意是 `"auth"`（严格侧），必须等 `applyAuthMode()` 跑完。

### 3.2 后台「用户管理」（`templates/admin.html`）

1. **tab**（`:102-108`）加第 7 个，**默认 hidden**：
   ```html
   <div class="tab hidden" data-tab="users">用户管理</div>
   ```
2. **面板**：照现有 `<section id="tab-xxx">` 的写法加 `<section id="tab-users" class="hidden">`，
   内含一个 `<table class="tbl">`，列：用户名 / 角色 / 数据范围 / 状态 / 创建时间 / 操作。
3. **显隐**：在 `enterAdmin()`（`:297-331`）里，拿 `AUTH_MODE`（`auth_mode.js` 的全局变量，本页 `:278` 已引入）与 `has("user:manage")`：
   ```js
   const usersTab = document.querySelector('.tab[data-tab="users"]');
   const usersPanel = document.getElementById("tab-users");
   const canManageUsers = has("user:manage") && AUTH_MODE === "auth";   // 影子期即使权限全量也不给
   if (usersTab) usersTab.classList.toggle("hidden", !canManageUsers);
   if (usersPanel && !canManageUsers) usersPanel.classList.add("hidden");
   ```
   点开该 tab 时（现有 tab 点击逻辑里挂一处）首次调 `loadUsers()`。

   > ✅ **`AUTH_MODE` 在后台页确实被正确赋值**（已核）：`templates/admin.html:380` 就是
   > `if (applyAuthMode(m) === "shadow") { enterShadowAdmin(m); return; }` —— `applyAuthMode(m)` 在 `enterAdmin()` **之前**执行，
   > 影子期走 `enterShadowAdmin` 但里面同样调 `enterAdmin(...)`（`:395`），所以这段判据在两种模式下都成立。
   > **不要**在文件顶部判 `AUTH_MODE`（初始值是严格侧 `"auth"`）。
4. **渲染**：
   - 数据范围：`scope_type==='all'` → 「全部」；`'companies'` → `JSON.parse` 后顿号串，**先判 `null`/`""`/异常 → 显示「—」**（别让 `JSON.parse(null)` 抛错打挂整表）；`'none'` → 「无」。
   - 状态：`disabled` → 「已停用」（行加灰样式），否则「启用」。
   - 操作列：
     - 「重置密码」→ 弹窗二选一（输入型：新密码 + 确认新密码 + 二次确认；或点「生成随机密码」→ **请求体不带 `password` 字段**）→ 成功后若返回 `password`，弹一次「请抄给对方：xxxx」+ 复制按钮。
     - **`reset_password` 返回 `self === true`（管理员重置的是自己）** → 走和改密成功同一套收尾：`toast("密码已重置，请用新密码重新登录")` + `_pending = {}` + `showLoginModal(...)`。
       （不加也能跑——下一个请求 401 会兜底弹窗——但让管理员干等到点下一个 tab 才被踢，体验差一截。）
     - 「停用 / 启用」→ `disabled` 切换；**当前用户那一行的「停用」按钮直接 `disabled`**。
5. **接口调用**：直接用现有的 `api(path, opts)`（`:399-401`）或裸 `fetch` 即可 —— 后台页有两层包装器串联：
   - `:284-295` 负责 `credentials` + 影子 `?user=` + **自动补 `X-CSRF-Token`**；
   - `:362-371` 负责 401 → 弹登录层。
   ⇒ **不要手写 CSRF 头**，也不要在调用处再包一层 fetch。

### 3.3 静态资源版本号

改了 `static/app.js` ⇒ `templates/index.html:222` 的 `?v=` **换新值**（如 `v=20261023chpw`）。

---

## 四、测试（**加进 `tests/auth_gate_test.py`，不新建文件**）

现有夹具直接用：`client`（`AUTH_ENABLED=1`）、**`dclient`（`AUTH_ENABLED=0`，`:63-66`）**、`_authed()`、`_login()`、`ADMIN`、`MGR`(hanwenhao)。

必测（对应 spec §七）：

| # | 用例 | 用哪个夹具 |
|---|---|---|
| A5 | 改密成功后**旧会话失效**：用旧 cookie 请求任一受保护接口 → 401 | `client` |
| A8 | 影子期 `POST /api/change_password` → **403** | `dclient` |
| B9 | `MGR`（hanwenhao）调 `/api/admin/users` → **403** | `client` |
| B10 | `ADMIN` 拉列表：至少 5 个种子账号在，且 `"pw_hash" not in response_text` | `client` |
| B11 | `ADMIN` 重置 `MGR` 密码（输入型）→ 200，且 `MGR` 能用新密码登录；**返回体不含 `password`** | `client` |
| B13 | 停用后原会话失效（旧 cookie → 401） | `client` |
| B15 | `ADMIN` 停用自己 → **403** | `client` |
| B16/17 | 影子期：`dclient` 调三个新接口 → **403** | `dclient` |
| B18 | 重置时设弱密码 `123` → 400 | `client` |
| B19 | 重置不存在的用户 → 404 | `client` |
| B20 | `password: ""` → **400「新密码不能为空」** | `client` |
| B21 | `disabled: "yes"` → 400 | `client` |
| B22 | `disabled: true` / `"true"` → 归一化成 1 | `client` |
| B23 | `ADMIN` 重置自己的密码 → 200 且响应 `self == True`；旧会话随即 401 | `client` |
| 附 | 随机生成（不传 `password`）→ 200 且回传的 password 能登录成功 | `client` |

> 改密/停用都会踢会话，写测试时注意**重新 `_login`** 拿新 token，别拿旧 cookie 断言后面的事。

---

## 五、交付时请在报告里给

1. 改了哪些文件、每个函数/接口的**新行号**。
2. pytest 输出：**`0 failed`**（含新增用例名）。
3. 一句确认：**`pw_hash` 未出现在任何响应里**（B10 已断言，这里再确认一次）。
4. 一句确认：**`_self_check` 没有"未登记路由"警告**（启动日志）。
5. 一句确认：影子期三件事没变 —— 不弹登录窗、身份卡正常、**退舱守卫仍生效**。
6. 一句说明：改密/重置/停用后**会话已失效**的实现点在哪个函数。
7. ⚠️ 部署提示照抄：**改了 `app.py`/`auth/*` ⇒ 测试环境(:5012) 与生产(:5011) 都要重启 Flask**；`app.js` 要 bump `?v=` + 浏览器强刷。
8. 提醒：本次**不做**强制改初始密码、**不做**改数据范围（洋 2026-10-22 拍板）。
