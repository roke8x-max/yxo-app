# 任务书：退舱行「单格编辑」守卫（OpenCode 执行）

- 日期：2026-10-19
- 派单：芙蕾雅（spec 见 `docs/mainsite/specs/2026-10-19-退舱单格编辑守卫-spec.md`）
- 业务拍板：毛骁洋 2026-10-19（批量改备注允许；R2+R7 本期一起做）
- **代码基线：main `0572fd1`**（= `b614056` + README，代码与 `b614056` 完全一致）
- 目标分支：从最新 main 拉**扁平命名**临时分支（连字符，不带斜杠），如 `fix-cancel-cell-guard`

---

## 〇、开工前必读（5 分钟，别跳）

1. `AGENTS.md` **硬规则 12**（碰退舱记录先停）+ 第 3 节分支约定。
2. `docs/mainsite/specs/退舱记录处理约定.md` **§3 五条铁律 / §4 人能做什么**。
3. 跑一次测试基线，判据 **`0 failed`**（命令见 `README.md` §跑测试）：
   ```powershell
   $PY = "<这台机器的 python.exe 绝对路径>"
   & $PY -m pytest -q -p no:cacheprovider
   ```
   芙蕾雅侧实测基线：**581 passed, 0 failed**（2026-10-19）。用例数会漂，**只认 `0 failed`**。

### 硬红线（违反任一条即停止并报告）

- ❌ **不要 `git commit` / `git push` / 开 PR** —— 落码后停在工作区，报告即可。
- ❌ **不要动** `sync_departure()`（`app.py:765-787`，PR#34 成果）、`/api/price/batch`（`app.py:934-975`）、`/api/admin/price/recalc`（`admin_api.py:1218-1219`）、`manifest_engine.py` 的比对池、`import_excel.py` 的导入守卫 —— 本次一律保持原样。
- ❌ **不要**为了"统一"把两个白名单合并成一个（见 §一）。
- ❌ **不要**改 `yxo.db` 表结构 / 加字段 / 加接口。
- ❌ **不要**给软删（`is_deleted=1`）的退舱行做特殊放行 —— 它照样拒。
- ❌ **不要**在 `api_update` 之外再造一条"合法的写单元格"入口。

---

## 一、要实现的业务口径（一张表就够）

| 字段 | 单格 `PATCH /api/row/<id>` | 批量 `POST /api/cells` |
|---|---|---|
| **备注** | ✅ 允许 | ✅ **允许**（洋 17:12 拍板，是对工单"批量不退让"的**唯一豁免**） |
| **状态** | ✅ 允许（本次新增） | ⛔ **跳过**（洋原话："只要不被**批量**修改就行"） |
| 其余字段 | ⛔ 403 | ⛔ 跳过 + 计数 |

⇒ **必须是两个白名单常量**，不能共用一个：

```python
CANCEL_EDITABLE_FIELDS = ("备注", "状态")        # 单格
CANCEL_BATCH_EDITABLE_FIELDS = ("备注",)          # 批量
```

> 工单原文把 `("备注","状态")` 同时塞进两个端点，那会让批量也能刷状态 —— **不要照抄**。

---

## 二、后端改动（`app.py`，4 处）

### 2.1 新增常量 + 判定函数

放在现有 `DATE_FIELDS` 定义（`app.py:1030`）旁边，或紧邻 `api_update` 之前，**只定义一处**：

```python
# 退舱行「单格人工编辑」白名单（毛骁洋 2026-10-19 拍板）：
# 状态可人工改（只要不被批量改），备注沿用 §4 原有规则，其余字段防误改。
# 依据 AGENTS.md 硬规则 12 + docs/mainsite/specs/退舱记录处理约定.md §4
CANCEL_EDITABLE_FIELDS = ("备注", "状态")
# 退舱行「批量落点」白名单（洋 2026-10-19 17:12：批量改备注允许）：只有备注。
CANCEL_BATCH_EDITABLE_FIELDS = ("备注",)


def _is_cancel_row(conn, rid):
    """该行当前是否为退舱记录（状态为 NULL / 空串一律视为非退舱）。"""
    r = conn.execute('SELECT COALESCE("状态",\'\') AS s FROM records WHERE id=?',
                     (rid,)).fetchone()
    return bool(r and str(r["s"]).strip() == "退舱")
```

- `get_db()`（`app.py:161-164`）已设 `row_factory = sqlite3.Row`，故 `r["s"]` 可用。
- `get_db()` **每次请求新建连接、不挂 `g`**（已核），所以不存在"复用连接被 close"的问题；`sqlite3` 的 `close()` 重复调用是幂等的。
- 判定口径必须 `str(...).strip() == "退舱"`，与 `app.py:783`（PR#34）、`app.py:952`（PR#36）一致。

### 2.2 `api_update()`（`app.py:790-813`）

位置：`if field not in config.ALL_FIELDS:`（返回 400）**之后**，`conn = get_db()` 之后、`old_year = departure_year(...)` **之前**。

```python
    conn = get_db()
    # 退舱记录：只允许改「备注」「状态」，其余字段拒绝（洋 2026-10-19 拍板）
    if field not in CANCEL_EDITABLE_FIELDS and _is_cancel_row(conn, rid):
        conn.close()          # 早退必须关连接
        return jsonify({"ok": False, "msg": "退舱记录只允许修改「备注」和「状态」"}), 403
```

- 非法字段仍是 400（顺序不能反）。
- `rid` 不存在 → `_is_cancel_row` 返 `False` → 走原逻辑（影响 0 行、返回 `ok:True`）。**既有语义不动**。
- 判断在 `sync_departure()` 之前 ⇒ 发班时间被拒时不会产生任何联动。

### 2.3 `api_cells()`（`app.py:816-847`）

1. 循环前初始化计数器（与现有 `saved = 0` 并列）：

```python
    saved = 0
    cancelled_skipped = 0          # 被跳过的退舱单元格数（不得静默处理）
```

2. 循环内，`if field not in config.ALL_FIELDS or not isinstance(rid, int): continue` **之后**、`old_year = ...` **之前**：

```python
        # 退舱记录：批量只允许写「备注」；状态与其他字段一律跳过并计数（同 api_update 的口径）
        if field not in CANCEL_BATCH_EDITABLE_FIELDS and _is_cancel_row(conn, rid):
            cancelled_skipped += 1
            continue
```

3. 返回体加一个键（**不改既有键语义**）：

```python
    return jsonify({"ok": True, "saved": saved, "cancelled_skipped": cancelled_skipped})
```

- 计数点：**在 `continue` 之前 `+1`**，与"未写入"严格一一对应（`saved` 仍只在 `UPDATE` 之后 `+1`，位置不动）。
- 命名用 `cancelled_skipped`（对齐 PR#36 `app.py:973`），**不用** `skipped_cancel`。
- 不预批量查状态：900 条最多 900 次主键点查，本地 SQLite 毫秒级，保持简单。

### 2.4 `api_price()` 单行算价加守卫（R2）

`app.py:900-931`。该方法已把整行（含 `状态`）取出（`app.py:903-905`），**不要再查一次**。
位置：`if not r: return jsonify({"ok": False}), 404` **之后**、`load_price_config()` **之前**：

```python
    # 退舱行不允许算价（洋 2026-10-19）：状态只放开单格人工改，价格不属于此列
    if str(r["状态"] or "").strip() == "退舱":
        return jsonify({"ok": False, "msg": "退舱记录不允许算价"}), 403
```

- 此处 `conn` 已在 `app.py:906` 关闭，直接 return 即可；`r` 是 `sqlite3.Row`（已取值的内存对象），**关连接后照样能读字段**，不用担心。
- 放在 `load_price_config()` 之前 ⇒ 被拒请求不读价表，测试也不需要价格配置。

---

## 三、前端改动（`static/app.js` + `templates/index.html`）

> 前端**必须做**（洋："不要被随意不小心改掉了"）。只做后端会出现：屏幕上留着被拒的值、批量提示虚报行数、关页兜底静默丢弃。

### 3.1 两个常量 + 三个 helper（放在 `app.js:877` 附近）

```js
const CANCEL_EDITABLE = ["备注", "状态"];         // 对齐后端 CANCEL_EDITABLE_FIELDS（单格）
const CANCEL_BATCH_EDITABLE = ["备注"];           // 对齐后端 CANCEL_BATCH_EDITABLE_FIELDS（批量）
function isCancelRow(r) { return !!(r && String(r["状态"] || "").trim() === "退舱"); }
function isCancelLocked(r, field) { return isCancelRow(r) && CANCEL_EDITABLE.indexOf(field) < 0; }
function isCancelBatchLocked(r, field) { return isCancelRow(r) && CANCEL_BATCH_EDITABLE.indexOf(field) < 0; }
```

顺手把 `app.js:877` 的 `if (String(r["状态"] || "").trim() === "退舱") tr.classList.add("row-cancel");`
改成 `if (isCancelRow(r)) tr.classList.add("row-cancel");`（纯等价替换，避免两套判据漂移）。

### 3.2 `openCellEditor()`（`app.js:202`）第一行拦住

```js
  if (isCancelLocked(r, c.name)) { toast("退舱记录只允许修改「备注」和「状态」"); return; }
```

覆盖「双击」（`app.js:971`）和「键入即编辑」（`app.js:2747`）两条入口。

### 3.3 `saveCell()`（`app.js:1234`）兜底 + 失败回滚

开头（`rec` 已取到，判空后再判）：

```js
  if (isCancelLocked(rec, field)) { toast("退舱记录只允许修改「备注」和「状态」"); return; }
```

失败分支（现在是 `} else toast("保存失败: " + j.msg);`，`app.js:1265`）改成：

```js
    } else {
      clearPending(id, field);
      if (rec) rec[field] = oldValue;   // DATA 本来没改，重绘即还原显示
      renderActive();
      toast("保存失败: " + j.msg);
    }
```

并把 `pushUndo(...)`（`app.js:1237`）**移到成功分支**（`oldValue` 变量保留在原位）——被拒的请求不该在撤销栈里留一条不可回放的记录。

### 3.4 批量三个调用方（**关键，照 §3.4.1 的代码骨架写**）

**接口定死**：`applyEdits(edits, msg, extraCancelSkipped = 0)` —— 第 3 个参数本轮新增，默认 `0` ⇒ `pasteToRow()` 的整行粘贴调用（`app.js:1814`）**不用改**。

| 函数 | 改法 |
|---|---|
| `applyEdits()` `app.js:1752` | 见下方 3.4.1 完整骨架（过滤 → 全跳过则不发请求 → 否则发 `kept` → 回调里算 `totalSkipped`） |
| `doGridPaste()` `app.js:1885-1886` | 在 `edits.push({...}); target[tc.name] = nv;` **之前**：`if (isCancelBatchLocked(target, tc.name)) { skippedCancel++; continue; }`（`target` 是既有变量，别另查）。`skippedCancel` 是**本函数的局部变量**，用**第 3 个参数**传给 `applyEdits`：`applyEdits(edits, msg, skippedCancel)` |
| `applyBulkSet()` `app.js:2410-2438` | **它直接 `fetch("api/cells")`，不走 `applyEdits`** ⇒ 不要重复 toast，也不要把它改成调 `applyEdits`。`let skipped = 0;` 与 `const edits = []` 一起初始化；`targets.forEach` 内按 `isCancelBatchLocked(r, field)` 跳过并计数。**空 `edits` 时按原因给不同提示、不发请求** —— 见 §3.4.2 骨架 |

#### 3.4.1 `applyEdits` 参考实现（**`j` 只在 `.then` 回调里出现，别在请求前碰它**）

```js
function applyEdits(edits, msg, extraCancelSkipped = 0) {
  // 退舱行批量只允许写「备注」（与后端 CANCEL_BATCH_EDITABLE_FIELDS 一致）
  let selfSkipped = 0;                    // ← 初始化必须在过滤循环之前
  const kept = [];
  edits.forEach((e) => {
    const target = DATA.find((x) => x.id === e.id);
    if (isCancelBatchLocked(target, e.field)) { selfSkipped++; return; }
    kept.push(e);
  });
  const skippedNoReq = extraCancelSkipped + selfSkipped;   // 不发请求时也能算出来的跳过数

  if (!kept.length) {                     // 全被跳过：不发空 POST，但必须报出来
    if (skippedNoReq > 0) {
      toast("退舱记录不可修改，已跳过 " + skippedNoReq + " 个单元格");
      loadRows(true).then(renderActive);   // 本地 DATA 可能比库旧，以服务端为准重取
    } else toast("没有需要粘贴的内容");
    return;
  }

  kept.forEach((e) => { const rec = DATA.find((x) => x.id === e.id); if (rec) rec[e.field] = e.value; });
  fetch("api/cells", {
    // ⚠️ headers 保持原样（只有 Content-Type）。CSRF 头由全局 fetch 包装器自动加
    //    （app.js:14-23：auth 模式下对非 GET 的同源请求补 X-CSRF-Token）。
    //    不要自己手写 token，也不要删这项 headers。
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: USER, edits: kept }),
  }).then((r) => r.json()).then((j) => {
    const totalSkipped = skippedNoReq + (j.cancelled_skipped || 0);   // j 只在这里存在
    if (j.ok) {
      toast(msg + (totalSkipped > 0 ? "（已跳过 " + totalSkipped + " 个退舱单元格）" : ""));
      // pushUndo 的参数形状照原 applyEdits 写法（type + edits），别改字段名
      pushUndo({ type: "bulk-edit", edits: JSON.parse(JSON.stringify(kept)) });
      // 只重绘一次：跳过过就重取真值，否则就地重绘
      if (totalSkipped > 0) loadRows(true).then(renderActive);
      else renderActive();
    } else toast("保存失败: " + (j.msg || ""));
  }).catch(() => toast("保存失败(网络)"));
}
```

> ⚠️ **不要**写成 `totalSkipped = extra + self + (j.cancelled_skipped || 0)` 然后放在请求之前 ——
> 「全被跳过」那条路径根本没有 `j`（它只在 `.then((j) => ...)` 回调作用域里），会直接 `ReferenceError`。
> 上面用 `skippedNoReq`（请求前）+ `j.cancelled_skipped`（回调内）两段拼接，就是为了避开这个坑。
>
> ℹ️ **CSRF 已核实**：原 `applyEdits`（`app.js:1755-1758`）的 headers **本来就只有 Content-Type**，
> 没有手写 CSRF —— 是 `app.js:14-23` 的全局 `fetch` 包装器自动补 `X-CSRF-Token`（`credentials` 也由它统一设为 `same-origin`）。
> 所以照抄骨架**不会**掉 CSRF；反过来说，**别"贴心地"自己加一个 header**（手写的值不会随登录刷新）。
>
> ℹ️ **`loadRows()` 返回 Promise —— 已核实现，不是猜**：`app.js:1579-1582` 是
> `return fetch("api/rows").then(r => r.json()).then(rows => { DATA = rows; })`，
> 即**数据写进 `DATA` 之后**才 resolve；原码已有 8 处 `.then` 用法
> （`app.js:1253 / 1305 / 1511 / 1602 / 2436 / 2522 / 2533 / 2599`）。
> 所以 `loadRows(true).then(renderActive)` = **先重取再重绘**，语义正确，照抄即可。
> （`node --check` 只验语法，验不出"返回值不是 Promise"这种运行时报错 —— 这条是读实现确认的。）
>
> ℹ️ **这两处 `loadRows(...).then(...)` 不要加 `.catch`**：全仓 8 处 `loadRows(...).then(...)` **一处都没有 `.catch`**（已 grep 确认）。
> 重取失败（网络抖动 / 500）时 `.then(renderActive)` 不执行、屏幕停在乐观值、无提示 —— 这是**既有行为**（原 `applyEdits` 成功后压根不重取），
> **不在本次范围**。要加就得连那 8 处一起改，属于另一个独立改进，别在本次单点加（会和其余调用分叉）。

原则：乐观更新本地 `DATA` 只能对"确定会写成功"的格子做。备注是例外（批量允许），所以不会被过滤 —— 这是拍板结果，不是漏判。

#### 3.4.2 `applyBulkSet` 参考实现（**它的 `edits` 也可能被过滤成空**）

现状（`app.js:2422-2438`）：`targets.forEach` 生成 `edits` → `if (!edits.length) { toast("没有需要补写的空值行"); return; }` → 直接 `fetch`。

改造要点：**在生成 `edits` 时就跳过**，并且**空 `edits` 那句提示要按原因分开**（否则"全是退舱行"会显示成"没有需要补写的空值行"，误导）。

> ⚠️ 骨架**只示意插入位置**。`targets.forEach` 里的**原有逻辑**（`mode === "fill"` 的补空判断、`edits.push` 的字段形状）
> **以原码 `app.js:2422-2427` 为准** —— 变量名（`field` / `mode` / `value` / `cur` / `targets` / `edits`）已核对与原码一致；
> 若你读到的原码与此不同（说明文件被改过），**按原码，不要按骨架**。

```js
  const edits = [];
  let skipped = 0;                        // ← 与 edits 一起初始化
  targets.forEach((r) => {
    const cur = r[field] == null ? "" : String(r[field]);
    if (mode === "fill" && cur.trim() !== "") return;   // 仅补空：跳过非空（原有逻辑）
    if (isCancelBatchLocked(r, field)) { skipped++; return; }   // 退舱行：备注放行，其余跳过
    edits.push({ id: r.id, field, value });
  });
  if (!edits.length) {
    // 不发空 POST（与 applyEdits 语义一致）；按原因给不同提示
    if (skipped > 0) toast("退舱记录不可修改，已跳过 " + skipped + " 个单元格");
    else toast("没有需要补写的空值行");
    return;
  }
```

发请求那段（保持它自己的 `fetch`，**不改成调 `applyEdits`**），提示改成——**这两行放在原 `fetch(...).then((j) => { if (j.ok) { ... } })` 的 `j.ok` 分支顶部**，替掉原来那句 `toast("已批量更新 " + edits.length + " 行…")`：

```js
      const totalSkipped = skipped + (j.cancelled_skipped || 0);
      toast("已更新 " + j.saved + " 行「" + field + "」" + (totalSkipped > 0 ? "（跳过 " + totalSkipped + " 个退舱单元格）" : ""));
```

> `j.ok` 分支里原有的 `syncVersion(); loadRows().then(renderAll);` **保留不动**（它会以服务端为准重取整表，跳过提示已由上面这行给出）。

> ⚠️ `skipped` 和 `j` 的关系与 `applyEdits` 相同：**`skipped` 请求前就有，`j.saved`/`j.cancelled_skipped` 只在 `.then` 里**。
> 空 `edits` 分支里**不许出现 `j`**。
>
> 这条路径对应验收第 14 条：筛出 3 条退舱行改「目的站」→ `targets` 全是退舱 → `edits` 为空 → 必须**不发请求**、
> 报「已跳过 3 个单元格」，而不是原来那句「没有需要补写的空值行」（会让人以为这批没有空值，实际是被拦了）。

#### 3.4.3 `doBulkPaste` 是死代码（与本任务无关）

`doBulkPaste()`（`app.js:1818`）经核查**全仓无调用点**（`static/` 与 `templates/` 都搜过），是死代码。
**不改、不删、别当漏项。** 若用 Find Usages 后仍不放心，确认一下没有动态拼接调用即可，不要"顺手升级"它。
（真正在用的批量粘贴入口是 `doGridPaste`：右键 `app.js:1778`、单元格 Ctrl+V / 全局 `paste` 事件 `app.js:2714`。）

### 3.5 退舱行不渲染「算价」按钮（R2 前端一半）

两处都加 `&& !isCancelRow(r)`：
- 列表重绘 `app.js:940-943`（条件 `c.name === "单箱价格" && !String(r[c.name] || "").trim()`）
- 编辑态重绘 `app.js:210-212`（见 3.6，改完判据后同样加）

### 3.6 修 R7 笔误（影响所有行，PR 里单独写一条）

`app.js:210`：`if (c.name === "备注" && !String(r[c.name] || "").trim()) {` → 把 `"备注"` 改成 `"单箱价格"`（与 `app.js:942` 对齐）。

> 现状后果：编辑一次**空备注**格里失焦后，备注格里冒出一个「算价」按钮，点一下就写「单箱价格」——而备注正是退舱行唯一放开的格子，等于后门。

### 3.7 静态资源版本号（漏了前端等于白做）

`templates/index.html:222`：`<script src="static/app.js?v=20260805copy"></script>`
把 `?v=` 改成一个新值，如 `v=20261019cancelguard`。

### 3.8 不要动的地方 / 已知不对齐的点

- `flushPending()`（`app.js:1218`）：`keepalive fetch` 无 UI，被跳过时前端无法提示 —— **接受静默**（前置拦截 + 批量过滤已覆盖到它）。不要为了"提示"去改 beforeunload 流程。
- **批量失败不回滚**：`applyEdits` / `applyBulkSet` 失败时只 toast，本地 `DATA` 已被乐观更新、不回滚。这是**既有行为**（本次不修，与 R1 同属"批量撤销/回滚"的既有缺陷），PR 评审时别当成本次引入的问题 —— 任务书 §五 有提醒项。
  只有 `saveCell()`（单格）本次要补失败回滚，见 §3.3。

---

## 四、测试（新增 `tests/test_cancel_cell_guard.py`）

**项目没有 `conftest.py`** —— 每文件自带 `client` fixture。直接照抄 `tests/test_sync_departure_cancel.py` 的夹具（返回 `(c, appmod)` 元组；写请求必须带 `X-CSRF-Token`）。

造数据要点：改成三行 —— **正常 + 退舱 + 「延期」**，前两行**必须同班列**（照抄该文件的 `TRAIN = "WB9999"`，正常行 `发班时间=2026-08-01`、退舱行 `2026-08-02`）。
不同班列的话验收第 5 条「同班列未被联动」就失去意义（联动本来就只认同班列，会**假通过**）。

必须覆盖：

| # | 用例 | 断言 |
|---|---|---|
| 1 | PATCH 退舱行「备注」 | 200，值写入 |
| 2 | PATCH 退舱行「状态」退舱→正常 | 200，值写入 |
| 3 | PATCH 退舱行「箱号」 | 403，值不变 |
| 4 | PATCH 退舱行「客户编码」 | 403 |
| 5 | PATCH 退舱行「发班时间」 | 403，且同班列正常行**未被联动** |
| 6 | PATCH 退舱行「目的站」 | 403 |
| 7 | `/api/cells` 退舱行「箱号」 | 200，`saved==0`，`cancelled_skipped==1`，值不变 |
| 8 | `/api/cells` 退舱行「**备注**」 | 200，`saved==1`，`cancelled_skipped==0`，值写入 |
| 8b | `/api/cells` 退舱行「**状态**」 | 200，`saved==0`，`cancelled_skipped==1`，值不变 |
| 9 | PATCH 正常行任意字段 | 200 |
| 10 | PATCH 正常行「发班时间」 | `synced` 语义不变（退舱行不被联动） |
| 11 | `DELETE /api/row/<退舱id>` | 200（人工删除仍允许） |
| 17 | `POST /api/price/<退舱id>` | 403，`单箱价格` 不变（无需价格配置） |

> 第 8 与 8b 是本次拍板的分水岭，必须两条都在 —— 防止后续有人把两个白名单合并。

前端源码断言（沿用既有做法：正则取函数体，别全局搜子串，否则会假通过）：

```python
m = re.search(r"function openCellEditor\(.*?\n\}", src, re.S)
assert m, "未找到 openCellEditor（前端被重构过？）"
assert "isCancelLocked" in m.group(0)
# 同理：saveCell → isCancelLocked；applyEdits → isCancelBatchLocked
# templates/index.html 里 app.js?v= 不等于 20260805copy
```

> 这个 `\n\}` 写法**保持与 `tests/test_sync_departure_cancel.py:181` 同款**（该文件已验证跑绿）：
> 项目里函数收尾 `}` 一律顶格，非贪婪匹配不会误吞下一个函数。
> 有人建议改成 `\n\}\n` 更「稳」——**不采纳**：既有先例已证明可用，多改一处只是增加与先例分叉的风险。

---

## 五、交付时请在报告里给

1. 改了哪些文件、每个函数的**新行号**。
2. pytest 结果截图/文本：**`0 failed`**（含新增用例名）。
3. **实机截图/录屏**（子串断言覆盖不到的）：验收 12–17 —— 双击退舱行箱号被拦、改状态成功、专列批量设置条提示不虚报、空备注格不再冒算价按钮、退舱行无算价按钮。
4. 一句确认：`sync_departure` / `price/batch` / `admin recalc` 的退舱守卫**未被本次改动影响**。
5. 一句确认：两处 `fetch("api/cells")` 的 `headers` **保持原样未被改动**（CSRF 由全局包装器加，`app.js:14-23`），`pushUndo` 参数形状照原写法。
6. 一句说明：**R1（批量粘贴撤销会把字段清空）本次未修**，需在 PR 描述里显式提醒。
   附带一句（本次的附带改善）：`pushUndo` 现在只压**过滤后的 `kept`**，退舱行不再进撤销栈 ⇒ **R1 的影响面缩小了，但 R1 本身没修**。
7. 一句说明：**批量路径失败时不回滚本地 `DATA`**（`applyEdits` / `applyBulkSet`）属既有行为，本次未修，PR 描述里一并提一句，避免被当成新引入的问题。
8. 一句说明：两处 `loadRows(true).then(renderActive)` **没有加 `.catch`**（与全仓 8 处既有调用一致），失败时静默属既有行为，别当 bug 报。
9. ⚠️ 部署提示照抄：**改了 `app.py` ⇒ 测试环境(:5012) 与生产(:5011) 都要重启 Flask**；`static/app.js` 不需重启但要 bump `?v=` + 浏览器强刷。
