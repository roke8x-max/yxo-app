# RBAC 模块落码验收 · 补强核查报告（2026-09-29）

**主导验收**：芙蕾雅｜**落码**：OpenCode｜**决策**：洋（已认同 ds 四点反馈）
本报告对洋 9/29 转达的 ds 四点反馈逐条给出**代码实证结论**与**OpenCode 必须补的缺口**。
验收仍由芙蕾雅主导，业务代码改动归 OpenCode；本文只列事实与待办，不替洋拍板技术细节。

| 洋的指令 | 结论 |
|---|---|
| 1. manager 权限按 spec 放行 + 旧注释改掉 | ✅ 权限符合 §3.1；旧注释待 OpenCode 改 |
| 2. /api/stamp：强随机 + compare_digest + IP 白名单/限流 | ❌ 三项全缺/不全 → 阻塞生产 |
| 3. 门禁补强 ①②③ | ①✅已接线；②❌init_db 仍写 dev 占位；③❌同 stamp 弱默认 |
| 4① 16 测试→§14 映射 + 两特确认 | visitor 断言✅；自检「漏登 DEBUG 退出」测试❌缺失 |
| 4② 回滚方案 + modal 三条件 | modal 三条件齐✅（含重定向兜底）；回滚可行、未执行 |

---

## 1. manager 权限（确认放行）

- 代码实证：`admin_api.py:_check_user`（158–213）已**彻底删除 body-user 检查**，改服务端 `g.identity` + `require_permission`；按 `config:manage`/`price:manage` 放行，manager 持 `config:manage` → 4 人全过。
- 符合 spec §3.1（manager 除 `user:manage` 外全有权，含 `admin:view`/`config:manage`）。**洋确认放行，本项通过。**
- ⚠️ 待 OpenCode：`admin_api.py:712`、`:757` 仍留「配置管理仅毛骁洋」旧注释，已不准确，顺手改掉。

## 2. /api/stamp（public+token 接受，但三项未完成）

代码实证（`app.py:856-862` + `config.py:21`）：

- 令牌来源：`STAMP_TOKEN = os.environ.get("YXSTAMP_TOKEN", "yxo_stamp_local_2026")` → **默认是已知弱串**，生产若漏设即裸奔。
- 比较：`if request.headers.get("X-Stamp-Token") != STAMP_TOKEN:` → **plain `!=`，非 `hmac.compare_digest`** → 存在时序侧信道。
- 防护：**无 IP 白名单、无限流**（全局 grep 无 `remote_addr`/rate 相关）。
- 函数内确有 `!= STAMP_TOKEN → 403` 真校验（非完全裸奔），但这三条仍不达标。

**结论：当前就是 ds 担心的「弱口令公开写接口」。OpenCode 必须补（见末尾清单）。**

## 3. 门禁补强

**① YXO_AUTH_SECRET 同时管 app.secret_key + CSRF HMAC —— ✅ 已满足**
- `auth/__init__.py:118-119`：`if not app.secret_key: app.secret_key = config.AUTH_SECRET_KEY or service._secret()`。
- `auth/service.py:152-167`：`_secret()` 取 `config.AUTH_SECRET_KEY`（=YXO_AUTH_SECRET），CSRF HMAC 即用它。二者同源。
- 残留风险：YXO_AUTH_SECRET 不设时 `_secret()` 退回进程随机值 → 重启后 CSRF token 失效 → 登录 POST 失败（「重启掉登录」正是此因）。**生产必须设 YXO_AUTH_SECRET。**
- 建议（非阻塞）：生产若未设 YXO_AUTH_SECRET 也应 `raise`（`_secret()` 当前仅 print WARNING），与②一致。

**② schema.init_db 在 YXO_AUTH_PASSWORD_* 未设时必须报错退出，不得写 dev 占位口令 —— ❌ 不满足**
- `auth/schema.py:88-93`：`_seed_password` 在 env 缺失时返回 `"yxo-dev-"+dev_key` + WARNING；`init_db`（96–123）照写，不报错。
- **必须改为：缺失即 `sys.exit(1)`/`raise`，不写 dev 占位。**
- ⚠️ 关键交互：现有 16 测试 fixture 直接 `schema.init_db()` 且**不设密码 env**（`tests/auth_gate_test.py:65`）。若无条件硬失败，测试会在 fixture 崩溃。**推荐门控**：`init_db(strict=False)` 默认非严格；`init_auth` 调 `init_db(strict=not (app.debug or app.testing))` → 测试（`TESTING=True`）跳过，生产（两者皆否）才硬退。

**③ YXSTAMP_TOKEN 生产必须强随机 —— ❌ 同②/stamp 弱默认**
- 见 §2：默认 `yxo_stamp_local_2026` 是可猜串。生产必须设强随机（见部署清单），且建议无 `YXSTAMP_TOKEN` 时拒绝启动（与②同门控）。

## 4. 补两项验证

### 4① 16 测试 → §14 用例映射表

| # | 测试 | 覆盖 §14 要点 |
|---|---|---|
| 1 | test_unauth_rows_401 | 未登录受保护 API → 401 |
| 2 | test_spoofed_user_query_param_ignored | `?user=` 忽略，以 session 为准（IDOR） |
| 3 | test_no_session_delete_401 | 无 session 写 → 401/403 |
| 4 | test_non_admin_admin_api_forbidden | 非 admin 访问 /api/admin/* → 403；/admin 页公开 200 |
| 5 | test_manager_has_admin_view | manager 持 admin:view → 200 |
| 6 | test_lockout_after_5_fails | 连续 5 次失败锁定（§5.4） |
| 7 | test_logout_then_401 | 登出后受保护 API → 401 |
| 8 | test_disabled_user_401 | 禁用用户即时失效（§5.3） |
| 9 | test_scope_none_returns_empty | scope=none → `[]` 绝不全量（§8 铁律） |
| 10 | test_visitor_companies_filtered | visitor 只返回绑定公司 |
| 11 | test_visitor_post_forbidden | visitor 写 → 403 |
| 12 | test_unregistered_route_403 | 默认拒绝安全网：漏登 → 403（运行时门禁） |
| 13 | test_updated_by_ignores_client_user | body `user` 忽略，updated_by=MGR（§10） |
| 14 | test_change_password_flow | 弱口令拒 + 改密成功（§5.2） |
| 15 | test_csrf_required_on_post | 缺 CSRF → 403（§5.1） |
| 16 | test_route_table_covers_url_map | url_map ⊆ 权限表（§6.3 自检逻辑） |

**洋要确认的两点：**
- ✅ **visitor「只返回这两家」**：`test_visitor_companies_filtered:181` 用**精确集合相等** `assert got == {"太平洋、港九港铁", "保时达"}`；种子插了三家公司（含「沙坪坝、中欧木业」），断言排除第三方 → IDOR 端到端坐实。
- ❌ **「启动自检测了漏登 DEBUG 退出」无测试覆盖**：
  - `test_unregistered_route_403` 测的是**运行时门禁** 403（临时摘条目），不是启动自检。
  - `test_route_table_covers_url_map` 只断言 `missing==[]`（代码正确时恒过），**未模拟「漏登 + DEBUG → raise」**。
  - 自检代码本身存在且正确（`auth/__init__.py:103-107`：`if missing and app.debug: raise RuntimeError`），但**缺测试证明它会真崩**。
  - **OpenCode 需补**：设 `app.debug=True` + 摘掉一条路由 → 调 `auth._self_check(app)` → 断言 `RuntimeError`。

### 4② 回滚方案 + 登录 modal

**登录 modal 三条件 —— 三样都在，洋可直接点测 `/`：**
1. ✅ `static/app.js`：`/api/csrf`（`routes.py:26`）、`/api/login`（`routes.py:37`）存在；401 拦截 `app.js:22-24`（`status===401 → showLoginModal()`）；`showLoginModal` 逻辑 `app.js:39-43`（`getElementById("loginModal").classList.remove("hidden")`）。
2. ✅ `templates/index.html:205` 有 `<div id="loginModal" class="user-gate hidden">`。admin/tuoshu/manifest 无该容器，但各自内联脚本在 401 时 `location.href="./"` 重定向回 `/`（`admin.html:318` / `tuoshu.html:200`），不会因缺元素报错 → 设计可接受。
3. ✅ 未登录访问 `/`：`init()`（`app.js:2472`）链 `apiAuthMeta` → 未登录 `/api/auth_meta`（`routes.py:80`，门禁标 authenticated → 401）→ `app.js:2474` `if(!meta||!meta.ok) showLoginModal()` 弹出。

→ **洋可直接在浏览器点测**：开 `/` → 应弹登录层 → 登录 → 改格 → 导出。

**回滚方案（spec §12）：删 `auth.db` + git 切回 pre-auth。** 可行性确认：
- 改动均**未 commit**（OpenCode 停工作区），回滚 = `git checkout -- <文件>` 或 `git stash`，可逆。
- `auth.db` 由 `init_db` 生成且幂等（`if row: continue`，`schema.py:112-114`），删后重跑即重建，无数据损失。
- ⚠️ **我未实际执行回滚**（会清掉 OpenCode 未提交工作，违反「停工作区等验收」）。仅确认方案可行；如需演练，命令如下（勿在生产乱跑）：
  ```bash
  git stash           # 暂存所有改动（含 auth/、app.py 等）
  rm -f data/auth.db  # 清本地 dev 种子库
  git stash pop       # 确认无误后恢复
  ```

---

## OpenCode 必须补清单（阻塞 merge/生产）

1. **/api/stamp 加固**：`hmac.compare_digest` 替代 `!=`；加 IP 白名单（或限流）；`YXSTAMP_TOKEN` 缺失时生产拒绝启动（与②同门控）；去掉 `yxo_stamp_local_2026` 弱默认。
2. **schema.init_db 硬失败**：`YXO_AUTH_PASSWORD_*` 任一缺失且非 test/debug → `raise`/退出，不写 dev 占位。推荐 `init_db(strict=False)` + `init_auth` 传 `strict=not(app.debug or app.testing)`，避免 16 测试崩溃。
3. **补自检 raise 测试**：debug + 漏登 → `RuntimeError`（证明 §6.3 自检真生效）。
4. **改旧注释**：`admin_api.py:712`、`:757`「配置管理仅毛骁洋」→ 按 §3.1 现状描述。
5. （建议）YXO_AUTH_SECRET 未设时生产 `raise`（与②一致）。

## 洋/小叽 部署前必做（环境变量，非代码）

- ⚠️ `YXO_AUTH_SECRET` = `python -c "import secrets;print(secrets.token_urlsafe(48))"`（强随机，CSRF + secret_key 同源）
- ⚠️ `YXO_AUTH_PASSWORD_MAOXIAOYANG` / `_FENGQIAN` / `_YANGYAWEN` / `_HANWENHAO` / `_VISITOR_DEMO` 各设强随机
- ⚠️ `YXSTAMP_TOKEN` = `python -c "import secrets;print(secrets.token_urlsafe(32))"`
- `pip install argon2-cffi`（装上自动升级哈希强度）
- 上线前删本机 `data/auth.db`（避免 dev 占位口令 `yxo-dev-*` 被带上线；`init_db` 幂等重建）
- 真机点测：登录 → 改格 → 导出（JS 仅人工复核，必须由人点一遍）
