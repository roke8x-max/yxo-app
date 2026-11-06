# 转发 OpenCode · PR-A sync_departure 排除退舱（2026-10-12）

> 任务书：`docs/mainsite/taskbooks/2026-10-12-主站四件套-taskbook-OpenCode.md`
> 芙蕾雅核定版 —— **正文（至「不做任何 git 写操作」为止）即为发给 OpenCode 的全部内容**；文末「附录」是内部留档，**OpenCode 无需阅读、可不发送**。

---

OpenCode，任务书 `docs/mainsite/taskbooks/2026-10-12-主站四件套-taskbook-OpenCode.md` 第 1 节已由毛骁洋拍板消项，可以开工。

**本轮只做 PR-A · `sync_departure` 排除退舱行。**
分支名 `fix-sync-departure-exclude-cancel` —— 仅供芙蕾雅建分支时使用，**你不用做任何 git 写操作**（含 `checkout -b`）。

## 改动点只有两处，严格按任务书 PR-A 节
1. `app.py:780-785` —— UPDATE 的 `WHERE` 追加 `AND COALESCE("状态",'')<>'退舱'`（**不动** `old_year` 语义、`id<>?`、`rowcount` 返回）；
2. `static/app.js:1270-1283` —— `syncDepartureLocal()` 的 `DATA.forEach` 里加 `if (String(r["状态"] || "").trim() === "退舱") return;`。

⚠️ **注意**：`app.py` 与 `static/app.js` 这两个文件**别的 PR（B/C/D）也会改** —— 所以不要按「文件」划边界，要按**改动点**划边界：只动上面两处，其余一行都不要动。

## 明确不做
任务书第 3 节登记表 **R1–R4 全部不要顺手做**（退舱单元格编辑限位 / 启动补导入 / 两处价格端点退舱缺口）。

## 两个已替你核实的事实（不要在落码前再猜）
1. **`sync_departure` 的 JSON 返回字段名就是 `synced`** —— `app.py:812` `jsonify({"ok": True, "synced": synced})`；前端已在消费（`static/app.js:1253`、`:1258`）。**字段名不要改。**
2. **`tests/auth_gate_test.py` 夹具用法**（PR-A 测试照它写）：
   - fixture：`client`（`AUTH_ENABLED=1`）/ `dclient`（`=0`），**返回值是元组 `(c, appmod)`**，别当单个 client 用；
   - 建库样板：`_make_client(tmp_path, monkeypatch, auth_enabled)` → `monkeypatch.setattr(config, "AUTH_DB_PATH", …)`、`config.AUTH_ENABLED`（`raising=False`）、`monkeypatch.setattr(appmod, "DB", …)`；`_seed_records(yxo_db)`；`schema.init_db(strict=False)`；`service.create_user(...)`；
   - 登录 / CSRF：`_login(c, user, pw)` + `_csrf(c)`；或直接用 `_authed(c, "t_admin")`（返回新的 CSRF token）；
   - **写请求必须带 `X-CSRF-Token`**：CSRF 是全局 `before_request` 校验（`auth/__init__.py:92`，头名见 `auth/service.py:21`），漏了会 403。

新测试文件：`tests/test_sync_departure_cancel.py`（`.gitignore:76` 已放行 `tests/test_*.py`）。

## 完成后的报告必须含
- 改了哪些文件 + `文件:行号`；
- 实跑命令 + **输出末几行**；
- 证据两步：① 新文件单跑绿 —— `& $PY -m pytest tests/test_sync_departure_cancel.py -q -p no:cacheprovider`；② **全量** `0 failed` —— `& $PY -m pytest -q -p no:cacheprovider`（`$PY` = 本机 python.exe 绝对路径，先 `& $PY -c "import sys;print(sys.executable)"` 自检；输出**不要**重定向到仓库内文件，要存就存 `%TEMP%`）；
- 计划外发现：**只登记，不顺手修**。

不做任何 git 写操作（`add` / `commit` / `push` / 开 PR / 建分支都由芙蕾雅来）。

---

<!-- ⬇⬇ 以下为芙蕾雅内部留档 · OpenCode 无需阅读 ⬇⬇ -->

## 附录（内部留档 · OpenCode 无需阅读）
与原稿（ds 版）的差异：
| # | 原稿 | 问题 | 改为 |
|---|---|---|---|
| 1 | 「不要碰 PR-B/C/D 的**任何文件**」 | ❌ **事实性误导**：PR-B 改 `app.py`、PR-C 改 `app.py`、PR-D 改 `static/app.js` —— 按文件划界等于让 PR-A 别动这两个文件，**自相矛盾** | 改为**按改动点**划界（只动那两处） |
| 2 | 只列 R1/R2 不要顺手做 | 登记表已扩到 R1–R4 | 改为 **R1–R4** |
| 3 | 「分支 fix-…」 | 与「不做 git 写操作」冲突（建分支也是写操作） | 注明"仅供芙蕾雅建分支用" |
| 4 | 证据只提 `tests/test_sync_departure_cancel.py` 的 0 failed | 任务书判据是**全量** `0 failed` | 拆成两步：新文件单跑绿 → 全量 0 failed；并补 `$PY` 自检与"输出勿落仓库" |

（原稿替你核实的两个问题，答案见上：字段名**确实是 `synced`**；夹具用法照 `auth_gate_test.py`。）
