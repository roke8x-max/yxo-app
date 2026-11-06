# 转发 OpenCode · PR-D 只渲染当前视图（2026-10-17）

> 任务书：`docs/mainsite/taskbooks/2026-10-12-主站四件套-taskbook-OpenCode.md` **PR-D 节**（本文自包含，照此执行即可）
> 前置：PR-A（`4685719`）、PR-B（`1a9a90e`）、PR-E（`10e1bb2`）、PR-C（`b801d26`）**均已合并进 main** —— **本文所有锚点均按合并后的 main 重新实读**
> 芙蕾雅核定版 ｜ 落码：OpenCode ｜ 复验：芙蕾雅 ｜ merge：毛骁洋

---

OpenCode，**本轮只做 PR-D · 让渲染收敛为"只渲染当前视图"**（用户明确说过"非常重要"）。

分支名 `perf-render-active-view-only` —— 仅供芙蕾雅建分支用，**你不用做任何 git 写操作**（含 `checkout -b`）。

## 为什么

`renderAll()` 每次刷屏都会**把四个视图全部重建**（`renderBody + renderBoard + renderTabs + renderStats`）—— 其中看板会把全部记录造成卡片，是"有点慢"的主因。

而 **`renderActive()` 早就写好**了"按 `STATE.view` 只渲染对应的那一块"，只是**调用点没换过去**。

## 锚点（2026-10-17 实读 `main = b801d26`，**不要再照抄任务书旧数字**）

| 目标 | 位置 |
|---|---|
| `renderActive()` | **`static/app.js:1189-1197`**（已实现，只渲染当前块 + `renderMonthTabs()`） |
| `renderAll()` | **`static/app.js:1202-1208`** |
| 子渲染函数 | `renderMonthTabs:568`、`renderBody:849`、`renderBoard:1070`、`renderTabs:1134`、`renderStats:1155`、`renderDashboard:2130` |
| `setView()` | `static/app.js:2085-2090`（**已在调 `renderActive()`**） |
| R1 锚点 | `static/app.js:929`（`td.cell-edit` 构造处，在 `renderBody` 内） |

### `renderAll()` 的 **8 处**调用（⚠️ 其中 1 处是"回调形式"，没有括号）

| # | 行 | 形态 |
|---|---|---|
| 1 | `1045` | `renderTabs(); renderAll();`（清筛选） |
| 2 | `1140` | `onclick` 里（切换看板标签） |
| 3 | `1610` | `renderAll();`（新增行） |
| 4 | `1639` | `DATA = ...filter(...); renderAll();`（软删） |
| 5 | `1654` | `renderAll();`（批量删除后） |
| 6 | **`2435`** | ⚠️ **`syncVersion(); loadRows().then(renderAll);`** —— **函数引用传递，无括号**（批量更新） |
| 7 | `2459` | 启动：`... setView(...); renderAll();` |
| 8 | `2593` | `loadRows().then(() => { renderAll(); ... });`（刷新） |

> ℹ️ 任务书写的"**9 处**"里，**导入那处（原 `2593`）已被 PR-C 删掉** ⇒ 现在 **8 处**；其余行号因 A/B/C/E 合并已整体位移（原 `1609/1638/1653/2434/2458/2630` → 现 `1610/1639/1654/2435/2459/2593`）。**以实况为准。**
> ⚠️ 第 6 处是 `.then(renderAll)` —— **改 `renderAll()` 而漏了它**，那处就静默不生效。

## 🔴 最关键的一条：`renderAll()` **不只是"渲染"**

实测 `renderAll()` 的函数体：

```js
function renderAll() {
  if (STATE.view === "dash") { renderDashboard(); return; }
  renderBody(); renderBoard(); renderTabs(); renderStats(); renderMonthTabs();
  document.getElementById("filterRow") && updateCompanyBtn();
  updateCompanyBtn();     // ← 注意：本来就被调了两次
  refreshSortBadges();
}
```

除渲染外，它还做**三件非渲染的事**：**`renderTabs()`**（看板标签条）、**`updateCompanyBtn()`**（公司筛选按钮）、**`refreshSortBadges()`**（排序徽标）。
而 `renderActive()`（`:1189-1197`）**只**做：按 view 渲染一块 + `renderMonthTabs()`。

> ⛔ **所以任务书原文的"方案二：令 `renderAll()` 直接 `return renderActive()`"是不完整的** —— 照那样写，**上面三件事全部丢失** ⇒ **标签条不刷新、公司按钮状态不更新、排序徽标不更新**，都是**肉眼可见的回归**。

## 实现（两方案二选一，**不要混用**）

### ✅ 推荐 · 方案二（改良）：只改 `renderAll()` 的函数体

```js
function renderAll() {
  renderActive();                       // 只渲染当前视图 —— 本次的性能目标
  if (STATE.view === "dash") return;    // 与原 renderAll 一致：dash 下不执行下面三样
  renderTabs();                         // 原 renderAll 一直在做，不能丢
  document.getElementById("filterRow") && updateCompanyBtn();   // 逐句保留原样
  updateCompanyBtn();                                           // 逐句保留原样（重复调用见 R5）
  refreshSortBadges();                  // 原 renderAll 一直在做，不能丢
}
```

**为什么推荐它**：
- **8 处调用点一行都不用改** —— 含 `2435` 的 `.then(renderAll)` 回调形式，**自动生效**；
- 性能目标达成（隐藏视图不再重建）；
- 三个副作用全部保留；**改动面最小、最不容易漏**；
- **逐句对应原文语义**：`dash` 提前 `return`、`filterRow` 条件判断、`updateCompanyBtn()` 两次调用 —— **全部原样保留**，本 PR **只把"四个渲染调用"收成 `renderActive()` 一个**。

> ⛔ **不要"顺手"去掉 `updateCompanyBtn()` 的重复调用** —— 那是**语义变化**，超出本 PR"只改什么时候干活"的边界。已登记为 **R5**（见下），另开独立 PR。
> ℹ️ **一处有意保留的差异（无害）**：原 `renderAll()` 在 dash 下**不调** `renderMonthTabs()`，而 `renderActive()` 会调。这是既有设计（`:1194` 注释写明"每次渲染同步全局月份条可见性（Dashboard/专列表格隐藏）"）—— `renderMonthTabs()` 自己会在 dash 隐藏，**这是采用 `renderActive()` 的本来目的，不是回归**。

### 方案一：把 8 处调用点换成 `renderActive()`

可行，但**不推荐**：每处要**逐点补** `renderTabs()` / `updateCompanyBtn()` / `refreshSortBadges()`，且 `2435` 是回调形式（`loadRows().then(renderAll)`）**极易漏改**。

> 既有先例：仓库里已有 `renderTabs(); renderActive();` 的写法（`:1151`、`:1569`），可参考。

## 📋 落码前请复核这三件事（芙蕾雅已预核，你确认一遍即可）

**① `renderTabs()` 是"清空重建"，不是"增量追加"** —— 这是"dash 下多执行也不出问题"的**真正前提**。
芙蕾雅已实读：`renderTabs()` 首行是 `bar.innerHTML = ""`（`:1135`），随后用 `el()` 新建节点。**即便 `el()` 内部用的是 `addEventListener`**（实测确实如此，`static/app.js:432`），因为容器**先被清空、旧节点连同其监听器一起被丢弃**，**不会累积重复监听**。
⇒ 你只需确认：**改造没有把 `bar.innerHTML = ""` 去掉、没有改成增量 `appendChild`**（本 PR 本来也不该动它）。

**② `renderStats()` 是纯渲染、无跨视图副作用** —— 实读确认：`box.innerHTML = ""`（`:1157`）后只读 `DATA` 并重建 KPI DOM，**不碰其它视图的容器** ✅

**③ `renderDashboard()`（`:2130`）有 async `fetch("api/train_summary")`** —— 不是纯渲染，**已列为红线 4**（不得增加其调用次数）。

> 说明：任务书原文只要求"扫一眼头部 10 行确认"。芙蕾雅已替你把这三处实读完，上面即结论；**你复核一遍即可，不必重新推导**。

## ⛔ 范围红线

1. **不可以**改任何**子渲染函数**的函数体：`renderBody` / `renderBoard` / `renderDashboard` / `renderStats` / `renderTabs` / `renderMonthTabs` —— **内部一行都不动**。只允许改**调用点**（方案一）或 **`renderAll()` 自身的委派体**（方案二）。
2. **`static/app.js:929`**（`td.cell-edit` 构造处，在 `renderBody` 内，**= 登记项 R1 的锚点**）—— **一行都不碰**。
3. **不得改变任何查询 / 筛选 / 分页语义**（`loadRows` / `getView` / `getView()` 不动）。
4. ⚠️ **`renderDashboard()`（`:2130`）内含 async `fetch("api/train_summary")`** —— 本 PR **不得增加**它的调用次数。方案二下 dash 视图仍是"一次渲染一次请求"，与现状**相同**；**不要**顺手把它改成同步或加缓存。
5. **不引框架、不加构建、不改功能行为** —— 只改"**什么时候干活**"。
6. **只登记、不顺手修**（本 PR 的登记项）：
   - **R5**：`renderAll()` 里 `updateCompanyBtn()` 被**重复调用两次**（`document.getElementById("filterRow") && updateCompanyBtn();` 之后又无条件调一次）—— **本 PR 逐句保留原样，不去重**，另开独立 PR 处理；
   - **R6**：启动那条 `:2459` 里 `setView(...)` 已经调过 `renderActive()`，紧接着又调 `renderAll()`（改造后会 `renderActive` 跑两遍）—— **登记即可，不要顺手去重**。

## 验收

1. **四个视图逐一人工核对**，行为与改造前一致 —— 尤其：**看板标签条**能切换/高亮、**公司筛选按钮**状态正确、**排序徽标**正确；**附截图**；
2. **性能前后对比**：默认表格视图（看板未打开）刷新一次，记录**耗时 / DOM 节点数**（DevTools Performance 或 `console.time`），**附改造前后对比数字**（这是"非常重要"的核心价值）；
3. **客观证据 · 隐藏视图确实不再重建**：在 `renderBoard()` 顶部**临时**加 `console.count("renderBoard")`（**验完即撤、不进提交**），在**表格视图**下刷新 → `renderBoard` 计数应**不增长**。
   ⛔ **这条的 console 输出必须原样落进报告**（截图或文本）—— 因为"临时加、验完即撤"之后**芙蕾雅复验时已经看不到了**。报告里要写清对照：**改造前**「表格视图刷新 N 次 → `renderBoard` 计数 = X」/ **改造后**「同样刷新 N 次 → 计数 = Y」，且 **Y 不随刷新增长**。
4. 回归 **`0 failed`**（前端无自动化测试，以人工核对 + 截图为准）。

## 报告必须含

- 改了哪些文件 + `文件:行号`（用你自己实读出来的行号）；
- 实跑命令 + **输出末几行**（全量 `& $PY -m pytest -q -p no:cacheprovider`，`$PY` = `D:\python.exe`，先自检 `sys.executable`；输出**不要**重定向到仓库内文件）；
- 采纳了哪个方案、为什么；
- **性能前后对比数字** + 四视图核对截图；
- **`console.count("renderBoard")` 的原始输出**（改造前 / 改造后两组对照数字，见验收 3）；
- 计划外发现：**只登记，不顺手修**。

不做任何 git 写操作（`add` / `commit` / `push` / 开 PR / 建分支都由芙蕾雅来）。
