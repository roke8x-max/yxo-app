# RBAC · A 工单验收报告（独立验收：芙蕾雅）

- **日期**：2026-09-30
- **验收对象**：A 工单 `2026-09-29-taskbook-A-auth-switch.md` 落码
  - 代码提交：`29bfbfa`（OpenCode，未 push 时）；本次验收后随文档 `9532a07` 一并 push 至 `origin/rbac-v2.5-stamp-b`（即 PR #25）。
  - 改动文件（diff 核对）：`config.py`(+4)、`auth/__init__.py`(+43/−5)、`app.py`(D3)、`tests/auth_gate_test.py`(+113)。
- **验收方法**：① `git show 29bfbfa` 逐文件 diff 对照工单 §1；② 读 `auth/__init__.py` / `config.py` / `app.py` 最终代码；③ 跑 `pytest tests/auth_gate_test.py` + 全仓 `pytest`；④ grep 核实 `abort` 残留、范围零触碰。
- **结论**：✅ **PASS** —— 代码逐字对齐工单，范围零触碰，测试全绿。

---

## 1. 逐项核对（vs A 工单 §1）

| 工单条款 | 落码实况 | 判定 |
|---|---|---|
| §1.1 `config.py`：`AUTH_ENABLED = os.environ.get("YX_AUTH_ENABLED","0")=="1"`，默认关 | diff 确认，置于 `AUTH_LOCK_MINUTES` 之后 | ✅ |
| §1.2 噪声过滤只留 `static` + `url_rule is None`；**删 OPTIONS 早返**；disabled 注入 system + 日志 + `return None` | diff 确认删除 `if request.method=="OPTIONS": return None`；`_auth_gate` 结构为「噪声→开关分支→enabled 原逻辑不变」 | ✅ |
| §1.3 `_system_identity()` 5 字段同形；`permissions=list(PERMISSIONS.keys())`（**全权限，非 `[]`**） | 代码确认；这是 §1.3 对总任务书 §2.3 的修正，已落实 | ✅ 关键修正到位 |
| §1.4 `_dry_run_log()` 5 字段：`path`(实际路径)/`method`/`ip`/`matched`/`has_session=False` | 代码确认 `path=req.path`、`matched` 经 `ROUTE_PERMISSIONS_SET/ROUTE_PERMISSIONS` 查表 | ✅ |
| §5 门禁仍 `before_request` 注册（不得摘钩） | `init_auth` 末行 `app.before_request(_auth_gate)` 未动 | ✅ |
| §5 不得改 B 的 stamp / v2.5 的 `records_dao` scope | diff 未触及 `records_dao.py`；`records_dao.resolve_scope` 判定经 `test_system_identity_shape` 验证对 `companies=[]` 仍全量返回 `("all",[])` | ✅ 范围零触碰 |

## 2. 范围零触碰

`git show 29bfbfa --stat` 仅 4 文件：`app.py` / `auth/__init__.py` / `config.py` / `tests/auth_gate_test.py`。`records_dao.py`、`admin_api.py`、v2.5 门禁 enabled 逻辑均未被改。OpenCode 自称「范围外零触碰」属实。

## 3. D3（`/api/stamp` 拒响应改 jsonify）

- `app.py` 三处 `abort(403)` → `return jsonify(ok=False, error="HTTP_403", msg=...), 403`（`msg` 区分三种原因：拒代理转发 / 仅允许本机调用 / token 校验失败）。
- `flask` 导入移除 `abort`；grep 全 `app.py` 确认 `abort(` 零残留（否则全仓测试会 ImportError，而 517 全过已间接证明安全）。

## 4. 测试证据

- `pytest tests/auth_gate_test.py` → **31 passed**（基线 16 + B 7 + A 8；既有断言一个未删，新增 8 个 A 用例 + 1 个 D3 断言）。
- 全仓 `pytest` → **517 collected，零失败**（达 `[100%]`，全程无 `FAILED`/`failed`/`ERROR`/`assert`；`EXIT=1` 为沙箱 safe-delete 清理钩子假象，已两次实证，`collect-only` 亦确认 517）。
- 关键 A 自测项均被用例覆盖：`test_system_identity_shape`（全权限+scope）、`test_disabled_row_200` / `test_disabled_admin_status_200` / `test_disabled_tuoshu_manifest_not_blocked`（内联 `ident.permissions` 判定未挡死）、`test_disabled_delete_marks_system`（裸 `.username` 不崩 + `updated_by=system`）、`test_dry_run_log_fields` / `test_dry_run_skips_noise`（5 字段齐、噪声不记）、`test_enabled_gate_still_enforced`（开关翻 1 后门禁全效 + stamp 拒体 JSON）。

## 5. 对 OpenCode 自报「三点实跑发现」的复核

| OpenCode 提请注意 | 芙蕾雅复核结论 |
|---|---|
| D3 的 HTML 前提部分不成立：`app.py:132 _on_http_error` 本会把 API 路径 `abort(403)` 转 JSON | 属实。D3 改完后 stamp 不再依赖该 handler，结果一致且更直接；**不影响结论，无碍**。 |
| `resolve_scope` 先判 `scope_type=="all"` 直接全量，`companies=[]` 不影响 → `records_dao` 未动 | 属实，已用单测 `test_system_identity_shape` 固化证据。 |
| B 拒启行为保持：裸 shell 无 `YXSTAMP_TOKEN` 时 `import app` raise（B 设计） | 属实，`test_init_auth_refuses_without_token` + `test_is_production_false_under_pytest` 双锁。 |
| **删 OPTIONS 早返的副作用**：enabled 下跨域 OPTIONS 预检会进 CSRF 层（非豁免路径无 token → 403） | **属实且为工单 §1.2/§5 明确要求（不跳 OPTIONS）**。同源应用无预检，风险低；但若将来引入跨域调用，需回看此点。记为 **accept-with-watch**，非阻塞。 |

## 6. 已知约束（accept-with-watch，非阻塞）

- **跨域 OPTIONS 预检**：删除 OPTIONS 早返后，enabled 模式下跨源 `OPTIONS` 预检请求会进入 CSRF 校验层，未带 token 的非豁免路径将返回 403。当前渝新欧前端与后端同源，不触发；未来若加跨域 API 调用，需在 `service.CSRF_EXEMPT_PATHS` 或预检豁免上补处理。
- **观察期 `updated_by` 署 `system`**：部署 `AUTH_ENABLED=0` 期间所有数据修改的 `updated_by` 为 `system`（门禁未启用、无真人身份），属预期，部署前须通知 4 位同事。

## 7. 部署提示（给小叽，详见合并部署交接单）

- 合并部署 = B（stamp 加固）+ v2.5（门禁）+ A（开关）同批，提交在 `rbac-v2.5-stamp-b` / PR #25。
- **初始部署 `YX_AUTH_ENABLED` 不设置（默认 0）** ⇒ B 的 stamp 加固立即生效、v2.5 门禁关着（dry-run）、`updated_by` 署 `system`。
- 观察数天拉 `AUTH_DRY_RUN` 日志确认无 127.0.0.1 旧 mailbots 调受保护接口后，再翻 `YX_AUTH_ENABLED=1`。
