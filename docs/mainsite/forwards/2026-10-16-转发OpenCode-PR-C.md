# 转发 OpenCode · PR-C 停用 Excel 台账导入（2026-10-16）

> 任务书：`docs/mainsite/taskbooks/2026-10-12-主站四件套-taskbook-OpenCode.md` **PR-C 节**（本文自包含，照此执行即可）
> 前置：PR-A（`4685719`）、PR-B（`1a9a90e`）、PR-E（`10e1bb2`）**均已合并进 main** —— **本文行号已按合并后的 main 重新锚定**
> ✅ **已拍板（2026-10-16）：`data:import` 权限**删干净**（含 `PERMISSIONS` 定义与 admin 角色项）** —— 路由没了它 gate 不到任何东西，留着是死权限、会误导。代价 = `tests/auth_gate_test.py` 多改 2 处，仍属"最小事实更新"。
> 芙蕾雅核定版 ｜ 落码：OpenCode ｜ 复验：芙蕾雅 ｜ merge：毛骁洋

---

OpenCode，**本轮只做 PR-C · 停用 Excel 台账导入**。

分支名 `chore-disable-excel-ledger-import` —— 仅供芙蕾雅建分支用，**你不用做任何 git 写操作**（含 `checkout -b`）。

## 为什么

Excel 台账整表导入（`/api/import`、`/api/import_upload`）已由新流程取代，现予**停用**：删掉两个路由与全部入口/引用，但**保留 `import_excel.py` 作为"退舱守卫"的参考实现**（它有测试在跑）。

## ⚠️ 行号已重新锚定（**不要再用任务书 PR-C 节里的旧行号**）

A/B/E 合并后行号已漂：

| 目标 | 任务书旧值 | **现状（以本文为准）** |
|---|---|---|
| `api_import` 路由 | `app.py:1360-1369` | **`app.py:1389-1398`** |
| `api_import_upload` 路由 | `app.py:1372-1418` | **`app.py:1401-1447`**（比旧值**长 29 行**） |
| `static/app.js` 导入段 | `2568-2604` | **`2569-2606`** |
| `auth/rbac.py` 各处 | `21-22` / `31` / `56` / `120-121` | **一致，未漂** |
| `templates/index.html:38-39` | — | **一致，未漂** |
| `config.py:24`、`import_excel.py:146` | — | **一致，未漂**（ℹ️ `import_excel.py` 那条在本 PR 加 3 行头注释后变为 **`:149`**） |

落码前请先 `grep -n "def api_import" app.py` 自行确认一次，**以实况为准**。

## 落码前先跑一遍这两条 grep（以实况为准）

**① 路由锚点**：`grep -n "def api_import" app.py` → 确认与本文一致。

**② `data:import` 消费方**：`grep -rn "data:import" --include=*.py --include=*.js --include=*.html .`

**芙蕾雅已预先核实（2026-10-16）**，预期命中如下 —— **代码与测试里只有这两处**：

| 文件 | 说明 |
|---|---|
| `auth/rbac.py`（`:22` / `:31` / `:56` / `:120-121`） | **本 PR 要删的**（`:22` 是注释） |
| `tests/auth_gate_test.py`（`:545` / `:551` / `:561-562`） | **本 PR 要最小修正的**（见第 7 条） |

✅ **已核实 `templates/` 与 `static/` 零命中** —— 前端**没有**消费 `data:import`（不存在"没权限就隐藏入口"的逻辑）。⇒ **不必为此停下报告**。

> ℹ️ **口径说明（免得你困惑）**：下面"预期存在但不改"那几类（`docs/**`、`auth/README.md`、`data/auth.db`）是**不带 `--include` 全仓 grep 时才会看到**的命中。按上面那条 `--include=*.py --include=*.js --include=*.html` 命令执行，**输出里不会出现 `.md` / `.db` / `docs/**`** —— 不要因为"命令输出里没看到、任务书却说有"而纠结，也别自己改成无 `--include` 的全仓 grep。

⚠️ **以下命中属"不该动的"，不在本 PR 范围**（出现它们是**正常**的，**不要修改**）：
- `docs/**` —— 全是**历史记录**（权限 spec、G 系列任务书/验收报告、本任务书）。**改它们会破坏审计链**，只登记不动。
- `auth/README.md:95` —— 记录 G 系列"当时必须新拆 4 项权限"的**历史设计说明**，同样**不改**（如需提示可只登记）。
- `*.db`（如 `data/auth.db`）—— **本地二进制的陈旧种子残留**，不是源码依赖，**不碰**。

⛔ **只有当"代码/测试"里出现上表两处之外的命中时**（例如某个 `.py` / `.js` / `.html` 也引用了 `data:import`）→ **停下报告、不要顺手删**。

## ⛔ 范围红线（五条，含 2026-10-16 新增三条）

1. **不要动 `app.py:371-373`**（首次启动 `records` 为空时仍 `run_import(conn)` = **登记项 R2**）—— `init_db` 里的补导入逻辑**不碰**。验收 grep 会查到它，**只登记、不修**。
2. **不要删 `app.py:23` 的 `from import_excel import run_import`** —— 删掉两个路由后它看着"没人用了"，但 **R2 路径（`:373`）仍在用**，删了直接 `ImportError`。**必须保留**。
3. 🆕 **不要把 `app.py:1450-1451` 的「舱单 / 箱号统一导入」注释头、以及 `app.py:1452` 起的 `_check_manifest_user` / `_check_manifest_apply` / `_manifest_forbid` 等模块级辅助函数删掉。**
   它们**紧跟在待删的 `api_import_upload` 后面**（`1447` 结束 → `1448-1449` 空行 → `1450` 注释 → `1452` def），是**舱单功能**，与 Excel 台账导入无关。⚠️ **按"范围"删、不要按"从这儿删到那儿"删** —— 这是本 PR 最容易多删的地方。
4. 🆕 **不要碰 `tests/` 里除下述三处之外的任何测试**；**不要为了让测试变绿去改被测代码的语义**（见下"测试连带改动"）。
5. 🆕 **不要修改 `docs/**` 或 `auth/README.md`** —— grep `data:import` 会在这些**历史记录**里命中，它们**不是**本 PR 的对象（改了会破坏审计链）。只登记、不修。

## 改动

### 1. `app.py`
- 删 `api_import`（`1389-1398`）与 `api_import_upload`（`1401-1447`）两段（含 `@app.route` 装饰器与其后的空行）；
  > `api_import_upload` 内部有一句 `from import_excel import analyze_import`（`:1407`）—— 它与路由同生共死，**随路由一起消失即可，不必单独处理**。
- 清 `app.py:9` 头注释里的 `config.IMPORT_FILE` 字样（现在是：`- 数据: 首次启动从 config.IMPORT_FILE 导入`）。改成不引用该符号的表述（例如「首次启动自动补导入参考台账」）。

### 2. `auth/rbac.py`（四处）
- 删 `:21-22` 那段 `POST /api/import` / `/api/import_upload` 的说明（文件头注释里的第 5 条）；
- 删 `:31` `"data:import": "Excel台账批量导入",`；
- 删 `:56` admin 角色清单里的 `"data:import",`；
- 删 `:120-121` 两条路由登记。
> ⚠️ 删除后**必须**同步改测试，见第 7 条 —— 否则回归必红（这是任务书漏掉的部分）。

### 3. `templates/index.html`
- 删 `:38-39`：`<button id="importBtn">导入</button>` 与 `<input id="importFile" ...>`；**`exportBtn` / `addRowBtn`（`:40-41`）保留**。

### 4. `static/app.js`
- 删 `:2569-2606`：`const importFile = ...` 起、到 `});` 止的整段（含 `importBtn` 绑定、`change` 监听、`doUpload`）。
  **`addRowBtn` 那段（`:2566-2568`）与 `helpBtn` 那段（`:2607` 起）保留** —— 前后各留一行边界，别多删。

### 5. `config.py:24`（**不删**，加注释）
```python
# 仅为保留参考实现（import_excel.py）而保留；/api/import 已停用，无业务入口读取。
IMPORT_FILE = r"D:\YXO_DATA\output\八月记录汇总.xlsx"
```

### 6. `import_excel.py`（**不删**，文件头加注释）
```python
# ⚠️ 已停用（2026-10-12）：/api/import 与 /api/import_upload 已删除。
# 保留本文件作为【退舱守卫】参考实现（见 :222-226），并有测试在跑。
# 彻底删除需毛骁洋单独确认。
```

### 7. 🆕 `tests/auth_gate_test.py`（**任务书漏掉、不删必红**）
删掉 `data:import` 后，这三处断言必然失败 —— **必须同步修正**（这是"事实变更"，不是放宽断言；**其余权限项一条都不能少**）：

| 行 | 现在 | 改为 |
|---|---|---|
| `:545` | `for p in ("data:import", "options:manage", "trash:purge", "train:manage"):` | 去掉 `"data:import"`，保留其余三项 |
| `:551` | `for p in ("manifest:apply", "price:manage", "config:manage", "data:import"):` | 去掉 `"data:import"`，保留其余三项 |
| `:561-562` | `assert rbac.ROUTE_PERMISSIONS[("POST","/api/import")] == "data:import"` 两行 | **整两行删除**（该键已不存在，留着必 `KeyError`） |

> ⛔ **不许**把 `assert` 改成 `pytest.skip` 或注释掉其它断言；只做上面这三处**最小事实更新**。

### 8. 🆕 新增 `tests/test_import_disabled.py`（2 个用例）
验"入口确实没了" + 把"删干净"钉住，防日后被误加回来：
```python
def test_import_routes_are_gone(client):
    """两路由已停用：直接 POST 应 404（不是 403、不是 500）。"""
    # 夹具照抄 tests/auth_gate_test.py 的 client（返回值是元组 (c, appmod)）
    ...
    assert c.post("/api/import").status_code == 404
    assert c.post("/api/import_upload").status_code == 404
    # 同时断言未登记：两个键都不在 ROUTE_PERMISSIONS
    from auth import rbac
    assert ("POST", "/api/import") not in rbac.ROUTE_PERMISSIONS
    assert ("POST", "/api/import_upload") not in rbac.ROUTE_PERMISSIONS


def test_data_import_permission_fully_removed():
    """'删干净'的守卫：权限与角色项都不该再存在（不靠 grep 兜）。"""
    from auth import rbac
    assert "data:import" not in rbac.PERMISSIONS
    assert "data:import" not in rbac.ROLE_PERMS["admin"]
```
> - 这两条负向断言是**"删干净"的护栏** —— 比 grep 硬：将来谁把 `data:import` 加回 `rbac.py`，测试会直接红。
> - 写请求**带不带 `X-CSRF-Token` 都要 404**（路由不存在时 Flask 在路由匹配阶段就返回 404）。若实测发现门禁先拦成 403，**以实况为准并在报告里说明**（那说明门禁在 404 之前生效，也不影响"停用"目的）。

> **⚠️ 关于 `client` fixture —— 两条硬要求**
> 1. **`client` fixture 定义在 `tests/auth_gate_test.py:56-57`（同文件内），`tests/` 下没有 `conftest.py`**（实测）⇒ 新文件里**必须复制一份同名 fixture**，**不要**试图 `from tests.auth_gate_test import client`（**pytest 不支持跨模块复用 fixture**，那样写不生效）。本仓库已有三个先例照此做：`tests/test_price_admin_hardening.py:62`、`tests/test_price_batch_guards.py:81`、`tests/test_sync_departure_cancel.py:53` —— **照它们抄**。
> 2. **`test_data_import_permission_fully_removed` 不要带 `client` 参数**。理由（已实测）：`auth/rbac.py` **全文零 `import` 语句、纯模块级字面量**（`PERMISSIONS` / `ROLES` / `ROLE_PERMS` / `ROUTE_PERMISSIONS` / `ROUTE_PERMISSIONS_SET`），**import 时没有任何 IO** ⇒ 不需要靠 fixture 的 monkeypatch 来"避开真实路径"。且同文件的 G7 同类权限测试（`auth_gate_test.py:541` / `:559`）**也都不带 fixture**，保持一致。
>    （不要加个用不到的 `client` 参数再配"勿删"注释 —— 那是注解噪音，且会给人一种"这条测试有 IO 风险"的错觉。）

## 验收

1. **grep（改后）**：
   `grep -rn "api/import\b\|import_upload\|IMPORT_FILE" --include=*.py --include=*.js --include=*.html .`
   → **只允许三个文件命中**：`config.py`（`:23` 注释 + `:24` 定义）、`import_excel.py`（`:2` 已停用注释 + `:149` 保留实现）、**以及新增的 `tests/test_import_disabled.py` 自身**（它必须写出被删的路由名才能断言"已消失"，**是护栏不是残留**）；
   **其余任何文件命中即不合格** —— 尤其 `app.py`（含 `:9` 注释）、`auth/rbac.py`、`static/app.js`、`templates/*.html` 必须**零命中**。
   > 📌 **2026-10-17 更正**：本条原写"只允许 `config.py` 与 `import_excel.py` 两个文件"，**漏了"新测试自身必然命中"**（落码方指出、核实成立）。
2. 主表页找不到「导入」入口（`index.html` / `app.js` 里无 `importBtn` / `importFile`）。
3. `POST /api/import`、`POST /api/import_upload` → **404**（附新测试输出）。
4. 回归 **`0 failed`**；`mailbots_next/tests/test_import_excel_guards.py` **仍绿**（`import_excel.py` 被保留，不应受影响）。
5. ⚠️ **启动无「反向残留」warning**：`_self_check` 对"表有、url_map 无"只 warning 不抛错 ⇒ **测试抓不到它**，必须靠上面第 1 条 grep 兜底（`rbac.py:120-121` 与两个路由必须**同时**删）。

## 报告必须含

- 改了哪些文件 + `文件:行号`（**用你自己 grep 出来的当前行号**）；
- 实跑命令 + **输出末几行**；
- 证据两步：① 新测试文件单跑绿；② **全量** `0 failed`；
  `& $PY -m pytest -q -p no:cacheprovider`（`$PY` = `D:\python.exe`，先 `& $PY -c "import sys;print(sys.executable)"` 自检；输出**不要**重定向到仓库内文件，要存就存 `%TEMP%`）；
- 上面第 1 条 grep 的**完整输出**（证明只剩两个文件命中）；
- **`data:import` 的 grep 输出**（证明代码/测试里只剩 `rbac.py`（已删净）与 `auth_gate_test.py`（已改），无第三个消费方）；
- 计划外发现：**只登记，不顺手修**。

不做任何 git 写操作（`add` / `commit` / `push` / 开 PR / 建分支都由芙蕾雅来）。
