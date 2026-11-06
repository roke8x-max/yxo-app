# 主站「四件套」落码任务书（给 OpenCode）

> 生成 2026-10-12 ｜ **落码：OpenCode ｜ 验收：芙蕾雅 ｜ merge：毛骁洋**
> 依据：`docs/mainsite/specs/2026-10-12-主站落码四件套.md` + 对 `origin/main`（`34cfdb6`）逐条实读核对
> **自包含**：你不记得上一轮讨论，背景与行号都写在本文里。
> ✅ **第 1 节三条待定项已由毛骁洋于 2026-10-12 拍板** —— **PR-A / PR-B / PR-C / PR-D 已消项，可直接开工**。
> 🕓 **第 3bis 节「本期扩展 · PR-E」3 条已拍板**（R3/R4 转正修）→ 完整顺序 **A → B → E → C → D**。E 排在 B 之后（复用它打开的同一函数，冲突最小），**不影响 A/B 先开工**。
>
> 📌 **进度（2026-10-17）**：**PR-A 已合并**（`4685719`）｜**PR-B 已合并**（`1a9a90e`）｜**PR-E 已合并**（`10e1bb2`）｜**PR-C 已合并**（`b801d26`）｜**下一个（也是最后一件）是 PR-D** —— 用转发稿 `docs/mainsite/forwards/2026-10-17-转发OpenCode-PR-D.md`（**锚点已按 A/B/C/E 合并后的 main 全部重新实读**，并补了本节一处重大遗漏 + 一处回调形式的调用点；见 PR-D 节顶部说明）。

---

## 0. 本次范围（对"四件套"的核对结论）

四件任务**全部尚未落码**（已在 `origin/main` 逐条实读确认）：

| PR | 事项 | 现状证据（实读 origin/main） | 结论 |
|---|---|---|---|
| A | `sync_departure` 排除退舱 | `app.py:780-785` WHERE 无状态条件 | 待做 |
| B① | 读价失败报错 | `admin_api.py:810-816`、`app.py:378-384` 均 `except: return 空配置` | 待做 |
| B② | COC 无价不回落 SOC | `app.py:415-416`、`admin_api.py:854-855` 均有回落 | 待做 |
| B③ | 提示分开 | `app.py:908-910` 单一句 msg | 待做 |
| B④ | 备份保留上限 + 恢复入口 | `admin_api.py:819-826` 只有写前备份，无保留策略、无恢复端点 | 待做 |
| B⑤ | 价格端点审计 | `admin_api.py` 全文 `_audit` 命中 **0 处** | 待做 |
| C | 停用 Excel 台账导入 | `app.py:1360/1372` 路由在；`config.py:24`；`auth/rbac.py:31/56/120/121`；`static/app.js:2568-2604`；`templates/index.html:38-39` | 待做（含 1 处冲突 → 已按第 1 节 A1 方案 a 定） |
| D | 只渲染当前视图 | `static/app.js:1202 renderAll()` 无条件全量渲染；`renderActive()`（1189）**已存在**却未被复用，9 处调用仍走 `renderAll` | 待做 |

### 相对原 spec 的 3 处修正 / 补充（芙蕾雅核对后新增，务必一并做）

1. **A-补 · 前端也要排除退舱**：`static/app.js:1270-1283 syncDepartureLocal()` 要对退舱行短路。否则后端已排除、前端本地却把退舱行的发班时间改成新值 → 屏幕显示与服务端不一致（要刷新才"跳回"）。
2. **B①-补 · 第二份实现**：原 spec 只点了 `admin_api._load_price_cfg()`；`app.py:378-384 load_price_config()` 是**同一毛病的第二份实现**（被 `/api/price/<id>`、`/api/price/batch` 使用），必须同步修，否则主表单格/批量算价仍会"读失败 → 当成无价"。
3. **C-冲突 · 删 `IMPORT_FILE` 会崩保留文件**：原 spec 要求删 `config.IMPORT_FILE`，同时要求保留 `import_excel.py`，而 `import_excel.py:146` 正是 `fp = filepath or config.IMPORT_FILE` → 删了会 `AttributeError`。→ **已拍板：不删（方案 a）**，见第 1 节 A1。（ℹ️ 落地后该行因 `import_excel.py` 头部新增 3 行注释而移到 **`:149`**。）

> 另：原 spec 共同约束 #1 写"由毛骁洋执行 git"。按 2026-10-09 定下的常驻分工，**已更新为：OpenCode 不 commit；`git add`+commit+push+开 PR 由芙蕾雅做；merge 必须由毛骁洋在网页端做**（见第 2 节 #1）。

---

## 1. 拍板结果（2026-10-12 毛骁洋拍板 · 已消项）

> 本节原为 3 条待定项，**已拍板**；下文即最终口径，OpenCode 照做。

### A1 · PR-C：`config.IMPORT_FILE` 处理 = **方案 a（不删）**
- **不删** `config.IMPORT_FILE`（`config.py:24`）。
- `config.py:24` 加注释：**「仅为保留参考实现（`import_excel.py`）而保留；`/api/import` 已停用，无业务入口读取」**。
- `import_excel.py` 文件头加「已停用」注释（见 PR-C 改动 5）。
- **PR-C 验收 grep 口径**：`config.py`、`import_excel.py` 里的命中**算预期保留项**；**其它文件一律不得再有引用** —— 含 `app.py`（尤其 `app.py:9` 头注释里的 `config.IMPORT_FILE` 字样要改掉）、`auth/rbac.py`、`static/app.js`、`templates/*.html`。
- 📌 登记项（不顺手修）：`app.py:372-373` 首次启动 `records` 为空时会 `run_import(conn)`（`filepath=None` → 回落到 `IMPORT_FILE`）。**本轮保留现状**。这也正是选方案 a 更稳的原因 —— 若删 `IMPORT_FILE`，该路径会 `AttributeError`。

### A2 · 退舱单元格编辑限位 → **不并入 PR-A**
- PR-A **只做** `sync_departure` 前后端排除退舱，范围严格锁定，不夹带其它改动。
- 「退舱行只放开「备注」、其余单元格不可改」（`static/app.js:929` 单元格构造附近）**单独排期**，登记为后续独立 PR（见第 3 节登记表）。**本轮不顺手修。**

### A3 · PR-B④ 备份保留 + 恢复入口
- 保留份数 **N=30**。
- 恢复入口：`templates/admin.html` 价格维护区「价格表」卡片**底部**。
- 权限 `price:manage`（`allow_limited=True`）；**二次确认**；**恢复前先备份当前**（即先 `_save_price_cfg(当前)`）；**恢复动作写审计**（event=`恢复价格`，detail=`file=<备份文件名>`）。

---

## 2. 共同硬约束（四件都适用）

1. **OpenCode 不做 git 写操作**（不 `commit` / 不 `push` / 不开 PR）—— 落码后停下报告；**`git add <具体文件>` + commit + push + 开 PR 由芙蕾雅做；merge（Squash and merge）必须由毛骁洋在网页端做**。
2. **禁止 `git add -A`**；分支名**扁平、不带斜杠**（如 `fix-mainsite-sync-departure`），从 `main` 拉。
3. **一次 PR 只做一件事**（A/B/C/D 四个独立分支 + 四个 PR，**串行**推进：一件落码 → 芙蕾雅提交开 PR → 再下一件，避免工作树交叉污染）。
4. 报告必须附实跑证据：命令 + 输出末几行 + `文件:行号`。禁止「已证实」「应该没问题」。
5. **不得为了让测试变绿而放宽任何业务判据**；测试红先定性（数据 / 环境 / 逻辑）。
6. 测试**不得连生产库**；一律用临时库（`tmp_path`）。
7. 发现计划外问题**只登记，不顺手修**。
8. 与 `AGENTS.md` 冲突时**以 `AGENTS.md` 为准**并立即报告。
9. 新测试放 `tests/`，命名可用 `test_xxx.py`（`.gitignore:76` 已放行 `!tests/test_*.py`）。
10. **跑测试**（判据 `0 failed`，不要写死用例数）：
```powershell
$PY = "<本机 python.exe 绝对路径>"   # 先 & $PY -c "import sys;print(sys.executable)" 自检
& $PY -m pytest -q -p no:cacheprovider
```
⚠️ 输出**不要**重定向到仓库内文件（会污染 git status）；要存就存 `%TEMP%`。

---

## PR-A · `sync_departure` 排除退舱行

分支建议：`fix-sync-departure-exclude-cancel`

### 背景
`AGENTS.md` 硬规则 12 与 `docs/mainsite/specs/退舱记录处理约定.md` §3/§4：退舱记录**不得作为批量操作的落点**（人工手动单条删除除外）。改发班时间是**按班列批量**操作 —— 改一条会把同班列所有行一起改，最容易无意触发。

### 改动 1（后端）
`app.py:780-785` 的 UPDATE `WHERE` 追加一行条件 `AND COALESCE("状态",'')<>'退舱'`：
```python
cur = conn.execute(
    'UPDATE records SET "发班时间"=?, updated_at=?, updated_by=? '
    'WHERE "班列号"=? AND id<>? AND COALESCE(is_deleted,0)=0 '
    'AND COALESCE("状态",\'\')<>\'退舱\' '
    'AND "发班时间"<>? '
    'AND (substr("发班时间",1,4)=? OR "发班时间" IS NULL OR "发班时间"=\'\')',
    (value, now, user, train, rid, value, year))
```
**不要**动其它任何逻辑（尤其 `old_year` 年份锁定语义、`id<>?` 排除本行、返回 `rowcount`）。

### 改动 2（前端本地同步 —— 与原 spec 的差异）
`static/app.js:1270-1283 syncDepartureLocal()` 的 `DATA.forEach` 内，加与后端一致的短路：
```javascript
DATA.forEach((r) => {
  if (r.id === srcId) return;
  if (String(r["状态"] || "").trim() === "退舱") return;   // ← 新增
  ...
});
```

### 影响面
`sync_departure` 只被两处调用（`app.py:808` 单格 PATCH、`app.py:841` 批量 `api_cells` 兜底），改一处两处都受益，**无需**改调用方。

> ⛔ **范围红线（2026-10-12 拍板）**：本 PR **只做**上面这两处排除退舱。「退舱行只放开「备注」、其余单元格不可改」**不在本 PR**（已登记为后续独立 PR，见第 3 节）—— **不要顺手做**。

### 验收（芙蕾雅主导）
新建 `tests/test_sync_departure_cancel.py`（临时库 + `app.test_client()`；夹具样板照抄 `tests/auth_gate_test.py` 的 `_make_client` / `_seed_records`，注意 `monkeypatch.setattr(config, "AUTH_DB_PATH", ...)`、`monkeypatch.setattr(appmod, "DB", ...)`、登录后取 CSRF 头）：
1. 造同一班列 `WB9999` 两条：正常行 `发班时间=2026-08-01`、退舱行 `发班时间=2026-08-02`（`状态="退舱"`）；
2. `PATCH /api/row/<正常行id>` `{"field":"发班时间","value":"2026-08-15"}`（带 session + `X-CSRF-Token`）；
3. 断言：正常行 = `2026-08-15`；**退舱行仍 = `2026-08-02`**；响应 `synced == 0`；
4. 断言退舱行可**人工单条软删**（`DELETE /api/row/<退舱行id>`）—— 符合"人能删，系统不替人删"；
5. 回归：`0 failed`。

---

## PR-B · 价格维护加固（5 项）

分支建议：`fix-price-admin-hardening`

> ⛔ **范围红线（本 PR 有两处高危交叉点，务必遵守）**
> 1. **B①** 会打开 `app.py` 的 `/api/price/batch`（**= 登记项 R3 的锚点 `app.py:922-945`**）。
>    → **只加"读价失败 → 就地报错、跳过写库"的 catch**；**不改** `/api/price/batch` 的查询语义 —— 退舱守卫、`is_deleted` 过滤**一律不碰**（R3 只登记，不修）。
>    「不写库」指的是**读价失败时不要落库**，**不是**要删掉正常路径的算价/写入逻辑。
> 2. **B⑤** 会打开 `admin_api.py` 的 `api_price_recalc`（**= 登记项 R4 的锚点 `admin_api.py:1109-1111`**，同一函数）。
>    → **B⑤ 只加审计日志**，**不改**该函数的查询与守卫逻辑（R4 只登记，不修）。
> 一句话：**你在 `app.py:922-945` 与 `admin_api.py:1095+` 里都会看到"退舱/软删行被写入"的缺口 —— 只登记，不修。**

### B① 读价格文件失败 → 报错（两处）
- `admin_api.py:810-816 _load_price_cfg()`：把 `except Exception: return {"prices":{},"exrates":{}}` 改为**抛出** `RuntimeError("价格配置读取失败: <原因>")`。
- `app.py:378-384 load_price_config()`：同样改抛。
- **统一出口**：在 `admin_bp` 上加错误处理（`@admin_bp.errorhandler` 或每条端点 try/except），把该错误变成 `{"ok": False, "msg": "价格配置读取失败：<原因>（本次未改动任何数据）"}` + 5xx。**绝不能**再让"读失败"渲染成"空价格表" —— 否则用户一保存就把整份 `price_config.json` 覆盖成只剩一条（现状最大风险）。
- `app.py` 侧 `/api/price/<id>`、`/api/price/batch` 也要 catch 后返回同义错误，**不写库**。

### B② COC 无价时不回落 SOC（两处必须同步）
- `app.py:415-416`：删除
```python
if rp is None and ct == "COC":
    rp = pi.get("SOC")
```
- `admin_api.py:854-855`：同样删除。
- 口径：COC 无价 → 该单元格**留空**（返回 `None`），**不**用 SOC 价顶。两处必须一致，否则同一单在不同入口算出不同的价。

### B③ 提示分开
`app.py:908-910`（`/api/price/<id>`）：
- 配置读取失败 → `{"ok": False, "msg": "价格配置读取失败，未改动数据"}`（由 B① 处理）；
- 配置正常但算不出价 → 保留 `{"ok": False, "msg": "无匹配价格（检查 口岸/目的站/发班时间）"}`；可加 `reason` 字段区分"COC 无价"子情况（前端可选展示）。
- 两句话必须**明显不同**，不能同一句。

### B④ 备份保留上限 + 从备份恢复
- `admin_api.py:819-826 _save_price_cfg()`：写前备份后调用新函数 `_prune_price_backups(keep=30)` —— 按文件名时间戳倒序保留最新 **30** 份（**2026-10-12 拍板 N=30**），多余删除。写成纯函数便于单测。
- 新增端点（`admin_bp`，权限走 `allow_limited=True`，即 `price:manage`）：
  - `GET /api/admin/price/backups` → `{"ok": true, "items":[{"file","mtime","size"}]}`（倒序）
  - `POST /api/admin/price/restore` `{"file":"price_config_20261012_101500.json"}` → 校验文件名白名单（只允许 `price_config_*.json`、禁止路径穿越）→ **恢复前先 `_save_price_cfg(当前)`（留一份）** → 写回 → **写审计**（event=`恢复价格`）→ `{"ok": true}`
- UI：`templates/admin.html` 价格维护区「价格表」卡片（`~193-219`）**底部**加一行：备份下拉（`GET /backups`）+「从备份恢复」按钮（**二次 `confirm`**）；JS 加 `prLoadBackups()` / `prRestore(...)`，成功后 `loadPrice()` 刷新。

### B⑤ 价格端点补审计
参照 `app.py:917-918` 写法（`from auth import audit as _audit` / `_audit.log_event("改价", g.identity.username, target=..., detail=...)`），给 `admin_api.py` 价格端点补审计：

| 端点 | 行 | event | detail |
|---|---|---|---|
| `api_price_exrate` | 901 | 改价·汇率 | `month rate 旧→新` |
| `api_price_entry_save` | 919 | 改价·条目 | `month port\|dest 旧→新` |
| `api_price_entry_delete` | 949 | 删价·条目 | `month port\|dest 值` |
| `api_price_import_confirm` | 1073 | 导入价格 | `months 覆盖月份` |
| `api_price_recalc` | 1096 | 重算价格 | `month priced=N` |
| `api_price_restore`（新） | — | 恢复价格 | `file=<备份文件名>` |

- **旧值**要从**改前的 cfg** 里取（写前先读一次），别只记新值。
- 身份用 `g.identity.username`（`_check_user` 已保证存在）。

### 验收
1. 把 `PRICE_CONFIG` 临时指向不存在路径 → `GET /api/admin/price` 返回**明确"读取失败"**错误（**不是** 200 空表）；保存被拒；
2. 造一条只有 SOC 价、无 COC 价的规则 → 对 COC 箱算价 → 单价**留空**（不回落到 SOC），有可见提示；
3. "真的无匹配价"的提示与第 1 条**明显不同**；
4. 造 **31** 份备份 → 触发清理 → 只剩 **30** 份（单测直接调 `_prune_price_backups(keep=30)`）；
5. 从「从备份恢复」入口恢复一次 → 内容回到备份时点；恢复前后各留一份备份，且 `auth_audit` 有 `恢复价格` 记录；
6. 改一次价格 → `auth_audit` 表能查到"谁·何时·旧值→新值"；
7. 回归 `0 failed`。

---

## PR-C · 停用 Excel 台账导入

> ✅ **PR-C 转发稿（自包含、行号已按 A/B/E 合并后的 main 重新锚定，直接用这份）**：`docs/mainsite/forwards/2026-10-16-转发OpenCode-PR-C.md`
> ⚠️ 本节旧行号已漂（`api_import` 实为 `app.py:1389-1398`、`api_import_upload` 实为 `app.py:1401-1447`）；**以转发稿为准**。转发稿另补了本节漏掉的 3 处：① 待删路由后面紧跟**舱单模块级辅助函数**（不可误删）② `tests/auth_gate_test.py` 有 **3 处**会因删 `data:import` / 删路由登记而**必红**（任务书原漏）③ 启动自检对"反向残留"**只 warning 不抛**，须靠 grep 兜底。

分支建议：`chore-disable-excel-ledger-import`

> ⛔ **范围红线（五条）**
> 1. 验收要 grep `run_import` / `IMPORT_FILE` 的引用，会查到 `app.py:371-373`「首次启动 `records` 为空时仍 `run_import(conn)`」（**= 登记项 R2 的锚点**）。
>    → **不要动它**（R2 只登记）。本 PR 只删路由 / 前端入口 / 清引用，**不碰** `init_db` 里的补导入逻辑。
> 2. ⚠️ **不要删 `app.py:23` 的 `from import_excel import run_import`** —— 删掉两个路由后它看起来"没人用了"，但 **`app.py:373`（R2 路径）仍在用**，删了会 `ImportError`。这一行**必须保留**。
> 3. 🆕 **不要删 `app.py:1450-1451` 的「舱单 / 箱号统一导入」注释头与 `:1452` 起的 `_check_manifest_user` 等模块级辅助函数** —— 它们紧跟待删的 `api_import_upload`（`:1447` 结束）之后，是**舱单功能**。**按"范围"删，不要按"从这儿删到那儿"删。**
> 4. 🆕 **`tests/auth_gate_test.py` 的 `:545` / `:551` / `:561-562` 必须同步最小修正**（见转发稿第 7 条），否则 `0 failed` 必不成立。⛔ 不许改别的断言、不许 `skip`。
> 5. 🆕 **不要修改 `docs/**` 或 `auth/README.md`** —— 它们会 grep 到 `data:import`，但属**历史记录**（改了破坏审计链）。只登记、不修。新测试另加**负向断言**（`"data:import" not in PERMISSIONS / not in ROLE_PERMS["admin"]`）把"删干净"钉住，不靠 grep 兜。

### 改动
1. 删路由：`app.py:1389-1398 api_import`、`app.py:1401-1447 api_import_upload`（**旧值 `1360-1369`/`1372-1418` 已漂，以转发稿为准**）；
2. 删 `auth/rbac.py` 登记：第 31 行权限表条目 `"data:import"`、第 56 行角色清单里的 `"data:import"`、第 120-121 行 `("POST","/api/import")` 与 `("POST","/api/import_upload")` 两条路由登记（⚠️ 连带检查第 21-22 行注释、以及 `data:import` 是否还有别处引用，一并清干净）；
3. 删前端入口：`templates/index.html:38-39`（`importBtn` + `importFile` 两个元素）、`static/app.js:2569-2606`（按钮绑定 + `doUpload` 那段；**旧值 `2568-2604` 已漂 1 行**）；
4. `config.IMPORT_FILE`（`config.py:24`）：**不删**（2026-10-12 拍板方案 a）—— 在该行加注释：
```python
# 仅为保留参考实现（import_excel.py）而保留；/api/import 已停用，无业务入口读取。
IMPORT_FILE = r"D:\YXO_DATA\output\八月记录汇总.xlsx"
```
5. `import_excel.py`：**保留**，文件头加醒目注释：
```python
# ⚠️ 已停用（2026-10-12）：/api/import 与 /api/import_upload 已删除。
# 保留本文件作为【退舱守卫】参考实现（见 :222-226），并有测试在跑。
# 彻底删除需毛骁洋单独确认。
```
6. `app.py:9` 头注释里的 `config.IMPORT_FILE` 字样**要改掉**（这是文件级注释，不属于保留项，grep 会命中）。

### 验收
1. `grep -rn "api/import\b\|import_upload\|IMPORT_FILE" --include=*.py --include=*.js --include=*.html .` → **只允许三个文件命中**：`config.py`（`:23` 注释 + `:24` 定义）、`import_excel.py`（`:2` 已停用注释 + `:149` 保留实现）、**以及新增的 `tests/test_import_disabled.py` 自身**（它必须写出被删的路由名才能断言"已消失"，**这是护栏不是残留**）。**其余任何文件命中即不合格**（`app.py`（含 `:9` 注释）、`auth/rbac.py`、`static/app.js`、`templates/*.html` 必须零命中）；
   > 📌 **2026-10-16 更正**：本条原写"只允许 `config.py` 与 `import_excel.py` 两个文件"，**漏了"新测试自身必然命中"** —— 已按实况补正（落码方指出，核实成立）。
2. 主表页找不到「导入」入口；
3. 直接 `POST /api/import`、`POST /api/import_upload` → **404**（附转发稿第 8 条新增的 `tests/test_import_disabled.py` 输出）。🆕 **该文件须自带一份 `client` fixture（`tests/` 无 `conftest.py`，不能跨模块 import；已有三个先例）**；另一个"权限删干净"用例**不带 fixture**（`rbac.py` 零 import、纯静态表，import 无 IO）。
4. 回归 `0 failed`；`mailbots_next/tests/test_import_excel_guards.py` 仍绿。
5. 🆕 **测试连带改动**：`tests/auth_gate_test.py` 的 `:545` / `:551` / `:561-562` 三处**必须**最小修正（删 `data:import` / 删两条路由断言），否则第 4 条必不成立。
6. 🆕 **启动无「反向残留」warning**：`_self_check` 对"表有、url_map 无"只 warning 不抛 ⇒ 测试抓不到，须靠第 1 条 grep 兜底。
7. 🆕 **`data:import` grep（代码/测试口径）**：`grep -rn "data:import" --include=*.py --include=*.js --include=*.html .` → 代码测试里**只允许 `auth/rbac.py` 与 `tests/auth_gate_test.py` 命中**（且均为"已删净/已修正"后的状态，实际应**零命中**）。**`docs/**` 与 `auth/README.md` 的命中属历史记录，预期存在、不改**；`templates/`、`static/` **已核实零命中**。新测试负向断言把"删干净"钉住。

---

## PR-D · 只渲染当前视图（用户明确"非常重要"）

> ✅ **PR-D 转发稿（自包含、锚点已按 A/B/C/E 合并后的 main 重新实读，直接用这份）**：`docs/mainsite/forwards/2026-10-17-转发OpenCode-PR-D.md`
> ⚠️ 本节行号已漂（`renderActive` 实为 `:1189-1197`、`renderAll` 实为 `:1202-1208`；调用点从 9 处变 **8 处**）；**以转发稿为准**。
> 🔴 **转发稿补了本节一处重大遗漏**：`renderAll()` **不只是渲染** —— 它还做 `renderTabs()` / `updateCompanyBtn()` / `refreshSortBadges()` **三件非渲染的事**；而 `renderActive()` 没有。⇒ **本节原写的方案二"令 `renderAll()` 直接 `return renderActive()`"是不完整的**，照写会丢这三个副作用（标签条不刷新 / 公司按钮不更新 / 排序徽标不更新）。转发稿给了改良版。
> 🔴 另：调用点里有 **1 处是回调形式**（`loadRows().then(renderAll)`，现 `:2435`，**无括号**）—— 只换 `renderAll()` 会漏它。

分支建议：`perf-render-active-view-only`

> ⛔ **范围红线（措辞 2026-10-12 修正；2026-10-17 再补一条）**
> - **不可以**改任何**子渲染函数**的函数体：`renderBody` / `renderBoard` / `renderDashboard` / `renderStats` / `renderTabs` / `renderMonthTabs` —— **内部一行都不动**。
> - **可以**：① 改**调用点**（把 `renderAll()` 的 **8 处**调用换成 `renderActive()` + 补三副作用）；② 改 **`renderAll()` 自身的委派体**（**推荐**，见转发稿改良版）。
> - 下面「改动」里的**两方案二选一**，不要混用（既换调用点、又把 `renderAll` 改成委派 = 重复做同一件事）。
> - `static/app.js:929`（`td.cell-edit` 构造处，**= 登记项 R1 的锚点**）在 `renderBody` 内 —— **一行都不碰**（R1 只登记）。
> - 🆕 ⚠️ **`renderDashboard()`（`:2130`）内含 async `fetch("api/train_summary")`** —— **不得增加**其调用次数。
> - 🆕 **登记项 R5**：`renderAll()` 里 `updateCompanyBtn()` 被**重复调用两次** —— **本 PR 逐句保留原样、不去重**（去重是语义变化，超出"只改什么时候干活"的边界），另开独立 PR。
> - 🆕 **登记项 R6**：启动那条 `:2459` 里 `setView(...)` 已调过 `renderActive()`、紧接着又 `renderAll()`（改造后会跑两遍）—— **只登记，不顺手去重**。
> - 🆕 采用改良版方案二时，`renderAll()` 的 **`dash` 提前 `return`、`filterRow` 条件判断、`updateCompanyBtn()` 两次调用全部逐句保留** —— 只把"四个渲染调用"收成 `renderActive()` 一个。
> - ℹ️ **"dash 下多执行三副作用也安全"的真正前提 = `renderTabs()` 是「清空重建」**（首行 `bar.innerHTML = ""`，`:1135`），**与 `el()` 用 `onclick` 还是 `addEventListener` 无关**（实测 `el()` 用 `addEventListener`，`:432`）—— 容器先清空、旧节点连同监听器一起丢弃，不会累积。落码时确认**没有去掉那行清空、没有改成增量 `appendChild`**。
> - 🆕 **`renderStats()` 已实读确认纯渲染**（`box.innerHTML = ""` 后只读 `DATA` 重建 KPI，不碰其它视图容器）。
> - 一句话：**可以改「什么时候调」和「`renderAll` 怎么转发」，不可以改「每个子渲染函数怎么干活」。**

### 现状
- `static/app.js:1189-1197 renderActive()`：**已实现**"按 `STATE.view` 只渲染当前块"**+ `renderMonthTabs()`**。
- `static/app.js:1202-1208 renderAll()`：**仍无条件全量渲染**，**外加三个非渲染副作用**：`renderTabs()` / `updateCompanyBtn()`（原本还被调了两次）/ `refreshSortBadges()`。
- `renderAll()` **8 处**调用（`1045` 清筛选、`1140` 切换看板标签、`1610` 新增行、`1639`/`1654` 删除、**`2435` 批量更新〔`.then(renderAll)` 回调形式〕**、`2459` 启动、`2593` 刷新）。
  > ⚠️ **行号与调用数按当时代码实况定位，不要死认数字。** PR-C 已删掉原 `2593`（导入）⇒ 由 9 处变 **8 处**；原 `1609/1638/1653/2434/2458/2630` → 现 `1610/1639/1654/2435/2459/2593`。
  > `renderDashboard`（`static/app.js:2130`）也是独立子渲染函数，**在红线保护范围内**。

### 改动（不引框架、不加构建、不改功能行为，只改"什么时候干活"）
- 让渲染收敛为"只渲染当前视图"：
  - ✅ **首选（方案二·改良）**：只改 `renderAll()` 函数体（**逐句保留原文的 `dash` 提前 `return`、`filterRow` 条件判断、`updateCompanyBtn()` 两次调用**，只把"四个渲染调用"收成 `renderActive()`）—— 8 处调用点**一行不改**（含 `.then(renderAll)` 回调），性能目标达成、三副作用保留；
  - 方案一：把 8 处调用换成 `renderActive()` 并**逐点补**三副作用 —— 可行但易漏（`2435` 是回调形式）。
- `renderMonthTabs()` 已在 `renderActive()` 内调用，无需额外处理。
- ⚠️ **不得**改变任何查询 / 筛选 / 分页语义（`loadRows` / `getView` 不动）。

### 验收
1. 打开默认表格视图（看板未打开）→ 用 DevTools Performance 或 `console.time`，记录改造前后一次刷新的耗时 / DOM 节点数，**附前后对比**（这是"非常重要"的核心价值）；
2. 切到看板 → 正常渲染；**标签条能切换/高亮**；
3. 四个视图（表格 / 看板 / 统计 / Dashboard）逐一人工核对，行为与改造前一致（含**公司按钮**、**排序徽标**）；
4. 🆕 **客观证据**：临时在 `renderBoard()` 顶部加 `console.count("renderBoard")`（**验完即撤、不进提交**）→ 表格视图下刷新时计数**不增长**。
   ⛔ **console 输出必须原样落进报告**（截图/文本）—— 因为"验完即撤"后**芙蕾雅复验时已看不到**；报告要附**改造前 / 改造后**两组对照数字。
5. 回归 `0 failed`（前端无自动化测试，以人工核对 + 截图为准）。

---

## 3. 交付方式
- 四件**串行**做：一件落码完成 → 停下报告 → 芙蕾雅提交并开 PR → 再开始下一件。
- 每个 PR 的报告含：改了哪些文件（`文件:行号`）、实跑命令与输出末几行、验收结论、以及"计划外发现"（**只登记，不顺手修**）。
- 报告里若与本文有出入，**以代码实况为准并立刻标出**（你可能看到我漏掉的第二处实现）。
- 全部完成后停在这里：**芙蕾雅验收 → 毛骁洋 merge**。

### 后续独立 PR 登记（本轮**不做**，勿顺手修）
| # | 事项 | 锚点 | 备注 |
|---|---|---|---|
| R1 | 退舱行「只放开备注」、其余单元格编辑限位 | `static/app.js:929`（`td.cell-edit` 构造处），配套 `static/style.css:504-510` | 2026-10-12 拍板：**从 PR-A 拆出、单独排期**。依据 `docs/mainsite/specs/退舱记录处理约定.md` §7 第 2 条 |
| R2 | 首次启动 `records` 为空时仍 `run_import(conn)` 读 `IMPORT_FILE` | `app.py:372-373` | 2026-10-12 登记：本轮保留现状（生产库不为空，实际不触发） |
| R3 | `/api/price/batch` 无退舱守卫，**且无 `is_deleted=0` 过滤**（`FROM records` 无条件）—— 会批量写退舱行与已软删行的单价 | ~~`app.py:922-945`~~ → **`app.py:935-960`** | → **转 PR-E**（见 3bis，**已拍板**：退舱 + 软删两个守卫都补） |
| R4 | `/api/admin/price/recalc` 无退舱守卫（已过滤 `is_deleted=0`） | ~~`admin_api.py:1109-1111`~~ → **`admin_api.py:1190`**（SELECT `:1203-1205`） | → **转 PR-E**（见 3bis，**已拍板**：只加退舱守卫） |

> R3/R4 属**新发现的既有缺口**（`退舱记录处理约定.md` §7 表未列价格类路径）。按铁律 4「任何涉及退舱的批量操作动手前先问毛骁洋」—— 登记时**只登记不修**。

---

## 3bis. 本期扩展 · PR-E（R3/R4 转正修 · **2026-10-12 已拍板**）

> 🔄 **2026-10-16 行号已重新锚定**（PR-A `4685719` 与 PR-B `1a9a90e` 合并后，本节原行号已漂）：
> `api_price_batch` = **`app.py:935`**（SELECT `:944-945`、循环 `:947-956`）；`api_price_recalc` = **`admin_api.py:1190`**（SELECT `:1203-1205`、`MACHINE` `:1206`、循环 `:1209-1231`）。
> 下方表格里的旧行号**仅作历史记录**，**一律以当时代码实况为准**（落码前先 `grep -n "def api_price_batch" app.py` 确认）。
> **PR-E 转发稿（自包含、行号已更新，直接用这份）**：`docs/mainsite/forwards/2026-10-16-转发OpenCode-PR-E.md`
> —— **计数口径、"既有字段只追加不改名"、守卫位置原则等细节也一律以转发稿为准**（本节表格是早期版本）。

建议分支：`fix-price-batch-cancel-guard`。

**主题**：给价格批量端点补**守卫** —— 与 PR-B「价格维护加固」是两件事，**必须独立成一个 PR**（混进 PR-B 会让 diff 变双主题，出问题难以定性是谁引入的）。

**顺序**：**A → B → E → C → D**。E 排 B 之后的理由是唯一的硬约束：**PR-B 已经打开并改过这两个函数**（B① 碰 `/api/price/batch`、B⑤ 碰 `api_price_recalc`），先把 B 做完、E 再在同一函数里补守卫，冲突最小。
> ℹ️ E 与 C 的先后**没有技术约束**：E 只碰 `app.py` 与 `admin_api.py`，**完全不碰 `static/app.js`**；而 C 在 `app.py` 删的是 `1360-1418`（**在 R3 锚点之下**），不会让 E 的行号漂移。
> 「行号会漂」这条担心的真正对象是 **PR-D**（C 删掉 `static/app.js:2568-2604` 后，`2593` 那处调用消失、`2630` 上移）—— PR-D 节已加「行号与调用数按当时代码实况定位」。**另：本条对 E 也已成真**（见上方锚点更新）。

### 改动（已定稿）
| 项 | 端点 | 锚点（**旧 → 现值**） | 改动 |
|---|---|---|---|
| R3 | `POST /api/price/batch` | ~~`app.py:922-945`~~ → **`app.py:935-960`** | 循环内加**两个**守卫（退舱 / 软删）→ `continue` + 计数；**SELECT 补取 `is_deleted` 列**（现在用的是 `config.ALL_FIELDS`，**不含** `is_deleted`） |
| R4 | `POST /api/admin/price/recalc` | ~~`admin_api.py:1109-1111`~~ → **`admin_api.py:1190`（SELECT `:1203-1205`）** | 循环内加**退舱**守卫 → `continue` + 计数；**SELECT 补取 `"状态"` 列**（现有 SELECT **不含** `状态`） |

> ⚠️ **守卫写在哪儿，是本 PR 最容易做错的一处**：
> **必须放在 Python 循环里 `continue` + 顺手计数，不要写进 SQL 的 `WHERE`。**
> 若写进 `WHERE`，被过滤掉的行**根本不会进循环** → `cancelled_skipped` / `deleted_skipped` **恒为 0**，与下面的计数要求自相矛盾（验收会红）。
> 参照：`/api/price/batch` 现有写法就是「全量取回 + 循环内 `continue`」（`app.py:927-936`），照它来即可。

### 三条已拍板口径
- **P-a = R3 把「已软删行」也纳入守卫**（`is_deleted=1` 的行同样跳过、并计入 `deleted_skipped`；**注意是循环内 `continue`，不是往 `WHERE` 加条件**，见上）。理由：同函数、同次改动、同一个 bug 面，分两次改反而多一次回归风险。且 `/api/price/batch` 不做任何过滤会往软删行盖 `updated_by`/`updated_at` → **污染 `退舱记录处理约定.md` §5「无 `updated_by`/`updated_at` 才算空壳重复」的判据**，这是必须补的硬理由。
- **P-b = R4 只加退舱守卫**，不夹带其它改动。
- **P-c = 退舱行「跳过重算、保留原单价」**（与 PR-A「不落点、不动原值」一致）。**不置空** —— 退舱是"退出"的历史记录，原价留着是给人看的，清了回不去。

### 执行细节（2026-10-12 定，两个端点**必须统一**）
- **计数口径分开报，不许混**（少一个，验收时"priced 少了但不知道为什么少"就说不清）：

| 字段 | 含义 | 备注 |
|---|---|---|
| `total` | 查询到的总行数（**加过滤前**） | = 取回的行数 |
| `priced` | 实际写入单价的行数 | |
| `cancelled_skipped` | 退舱跳过数 | |
| `deleted_skipped` | 软删跳过数 | **R4 恒为 0**，见下 |

- **字段名两个端点统一**：R3、R4 都用 `cancelled_skipped`，不许一个叫 `cancelled`、另一个叫 `skipped`（前端/测试会踩坑）。
- **重叠行归属（一行同时是「退舱」且「已软删」）**：
  归属**退舱优先** —— 命中退舱就记 `cancelled_skipped`，不再记 `deleted_skipped`。
  即两个桶**互斥**，保证 `priced + cancelled_skipped + deleted_skipped ≤ total` 且不重复计数。
  **任何情况下这种行都不得计入 `priced`。**
  （这不是"随便定一个"：退舱是本项目最需要显式报出的类别，且它是最容易被误伤的业务凭据。定死成规则是为了让两个端点必然一致 —— OpenCode 无状态、两端又是分两次写的，"必须一致"光靠要求不够，得给唯一解。）
- **R4 的 `deleted_skipped` 恒为 0**：R4 的既有查询已含 `COALESCE(is_deleted,0)=0`（`admin_api.py:1111`），软删行本来就不会被取回，所以**它恒为 0 是正常的，不是 bug，不要去"修"**（也**不要**为了让它非 0 而删掉那个过滤）。
- **两处 SELECT 都要补列**（否则守卫读不到字段）：R3 补 `is_deleted`、R4 补 `"状态"`。

### 与 PR-B 的关系
PR-B 稿已写明「R3/R4 只登记不修」；**已同步补一句**「R3/R4 已拍板本期修，将在 PR-B 之后的独立 PR-E 处理；本 PR 仍只登记不修」——否则 OpenCode 做 B 时会以为登记项被撤销。

### 验收
临时库造「正常 + 退舱 + 已软删 + **退舱且已软删**」四类行 → 跑两个端点 → 断言：
① 正常行照旧被算价/重算；
② 退舱行**单价与原值一字不变**且计入 `cancelled_skipped`；
③ 软删行（R3）单价与原值不变且计入 `deleted_skipped`；
④ **重叠行（退舱+软删）只计入 `cancelled_skipped`**，且**绝不出现在 `priced`**；
⑤ `total`/`priced`/两个 skipped 计数自洽：`priced + cancelled_skipped + deleted_skipped + 其它跳过 ≤ total`（**用 `≤`，不用 `=`** —— 真的还有"算不出价""无变化"等未列举的跳过原因）；
⑥ R4 的 `deleted_skipped == 0`（预期值，不是失败）；
⑦ **R4 的 `priced == updated + filled`**（实测 `admin_api.py:1239` 的既有审计就是 `priced={updated + filled}` ⇒ 该等式恒成立；**`priced` 计数点必须与 `updated`/`filled` 同一位置**，即 `UPDATE` 之后 —— 提前到"没变化检查"之前就会红）；
⑧ 回归 `0 failed`。

> 🔁 **芙蕾雅的复验（反向变异，各一条）**：① **把 `priced += 1` 挪到"没变化检查"（`:1224`）之前** → ⑦ 必须变红；② **把退舱守卫挪进 SQL `WHERE`** → ② / ④ 必须变红，且 `cancelled_skipped == 0` 会暴露"计数恒 0"。定性不明不放过。
