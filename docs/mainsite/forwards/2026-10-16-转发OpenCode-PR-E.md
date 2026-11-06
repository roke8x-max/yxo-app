# 转发 OpenCode · PR-E 价格批量端点补守卫（2026-10-16）

> 任务书：`docs/mainsite/taskbooks/2026-10-12-主站四件套-taskbook-OpenCode.md` **3bis 节**（本文自包含，照此执行即可）
> 前置：PR-A（`4685719`）、PR-B（`1a9a90e`）**均已合并进 main** —— **本文行号已按合并后的 main 重新锚定**
> 芙蕾雅核定版 ｜ 落码：OpenCode ｜ 复验：芙蕾雅 ｜ merge：毛骁洋

---

OpenCode，**本轮只做 PR-E · 给价格批量端点补守卫**。

分支名 `fix-price-batch-cancel-guard` —— 仅供芙蕾雅建分支用，**你不用做任何 git 写操作**（含 `checkout -b`）。

## 为什么

两个价格**批量**端点都没有退舱守卫，会把**退舱行**的单价一并改写：

- `POST /api/price/batch`（`app.py`）—— `SELECT … FROM records` **完全无条件**，连**已软删行**都会被写；
- `POST /api/admin/price/recalc`（`admin_api.py`）—— 已过滤软删，但**没有退舱守卫**。

依据 `AGENTS.md` 硬规则 12 与 `docs/mainsite/specs/退舱记录处理约定.md`：**退舱记录不得作为批量操作的落点**。另：`/api/price/batch` 往软删行写单价会盖 `updated_by`/`updated_at`，**污染 §5「无 `updated_by`/`updated_at` 才算空壳重复」的判据**。

## ⚠️ 行号已重新锚定（**不要再用任务书 3bis 里的旧行号**）

PR-A/PR-B 合并后行号已漂（任务书写 `app.py:922-945`、`admin_api.py:1109-1111`，实际是）：

| 端点 | 现状锚点 |
|---|---|
| `app.py` `api_price_batch` | **`def` 在 `:935`**；`SELECT` 在 `:944-945`；循环 `:947-956`；`return` 约 `:962` |
| `admin_api.py` `api_price_recalc` | **`def` 在 `:1190`**；`SELECT` 在 `:1203-1205`（**含 `WHERE COALESCE(is_deleted,0)=0`**）；`MACHINE` 在 `:1206`；循环 `:1209-1231` |

落码前请先 `grep -n "def api_price_batch" app.py` / `grep -n "def api_price_recalc" admin_api.py` **自行确认一次**，以实况为准。

## 改动（两个端点）

### R3 · `app.py` `api_price_batch`（**结构已实测，见下**）
1. **`SELECT` 补取 `is_deleted` 列** —— 现在用的是 `config.ALL_FIELDS`，**不含** `is_deleted`，不补就读不到软删状态。
2. **循环内加两个守卫**（退舱 / 软删）→ `continue` + 计数。
   **守卫位置：循环体第一步，早于任何读价 / 算价 / 写库动作。**（用语义描述，**不要**假设某一行变量名。）
   ✅ **此处放在最前面是正确的**（原因见下"守卫位置原则"）—— 本函数**没有任何 scanned 类计数器**，唯一计数器 `priced` 统计的是"实际写入行数"，退舱/软删行**本就不该计入**。
3. **查询语义除此之外不动**（不写 `WHERE`、不改"只补空价"的既有覆盖口径）。
4. 软删判空写 **`if (r["is_deleted"] or 0):`** —— 库里 `is_deleted` 允许 NULL（所以 SQL 里才用 `COALESCE`），显式 `or 0` 与查询口径一致，读代码的人不会怀疑"NULL 行会不会被误判"。（`if r["is_deleted"]:` 行为等价，但不如前者清楚。）

### R4 · `admin_api.py` `api_price_recalc`
1. **`SELECT` 补取 `"状态"` 列** —— 现有 SELECT（`:1204-1205`）**不含 `状态`**。
2. **循环内加退舱守卫** → `continue` + 计数。**只加这一条，不夹带其它改动**（P-b 已拍板）。
3. **守卫位置**：放在 `scanned += 1`（`:1213`）**之后**、`MACHINE` 判定之前 —— 这样**既有的 `scanned` 语义完全不变**（退舱行仍照旧计入 `scanned`），跳过的部分由新字段 `cancelled_skipped` 交代。
4. 该函数既有的 `MACHINE=("算价","批量算价","重算")`、"空价补算 / 手填不动"、`WHERE COALESCE(is_deleted,0)=0`、返回体里既有的 `scanned` / `updated` / `filled` / `failed` 字段 **全部保持原样**。

### 守卫位置的原则（**不是**"哪里都放最前面"，也**不是**两处不一致）
**守卫放在哪个位置，由"那个位置上的既有计数器该不该包含被跳过的行"决定**：

| 端点 | 该处既有计数器 | 该计数器**该不该**含退舱/软删行 | 守卫位置 |
|---|---|---|---|
| R4 | `scanned`（月份命中） | **该含**（`scanned` 语义 = 扫过多少行，不能变） | 放在 `scanned += 1` **之后** |
| R3 | `priced`（实际写入行数） | **不该含**（退舱行本就不该被算价 —— 正是本 PR 的目的） | 放在循环体**第一步** |

⚠️ **落码前仍请自己 `grep` 看一眼两个函数的循环体结构**，以**实况为准**（本节的"已实测"是 2026-10-16 的状态）。若发现 R3 里有与 `scanned` 同类的"扫过行数"计数器，**改成与 R4 一致的处理**（守卫放在它之后），别照字面硬放最前面。

### 既有返回体：**只追加，不改名、不合并**
- **R3 现状**（实测 `:962`）：`return jsonify({"ok": True, "priced": priced})`
  ⇒ **`priced` 是既有字段、语义已与本 PR 要求一致** —— **不要**把它当新字段重命名或包装；本次**只需追加** `total` / `cancelled_skipped` / `deleted_skipped` 三个。
  ⇒ 另外 `:961` 那条审计 `detail=f"批量算价 priced={priced}"` **保持不动**（它会随 `priced` 自然变小，这是**预期**，不是回归）。
- **R4 现状**：`{"ok": True, "scanned": …, "updated": …, "filled": …, "failed": …}` ⇒ 既有字段**全部保留、改名不动**，只**追加**四个新字段。
- **若发现既有字段里有与 `total` 同义的（如 `count`）**：**不合并、不改名**，只追加（给前端过渡期），并在报告里点出来。

### ⚠️ 守卫写在哪儿 —— 本 PR 最容易做错的一处
**必须放在 Python 循环里 `continue` + 顺手计数，不要写进 SQL 的 `WHERE`。**
若写进 `WHERE`，被过滤掉的行**根本不会进循环** → `cancelled_skipped` / `deleted_skipped` **恒为 0**，与下面的计数要求自相矛盾（验收会红）。
参照：`/api/price/batch` 现有写法本就是「全量取回（`SELECT` 在 `:944-945`）+ 循环内 `continue`（循环体 `:947-956`）」，照它来。

## 计数口径（两个端点**必须统一**）

响应里分开报，不许混（少一个，验收时"priced 少了但不知道为什么少"就说不清）：

| 字段 | 含义 | 备注 |
|---|---|---|
| `total` | **该端点 SELECT 实际取回的行数**（加守卫前） | = `len(rows)`；**两端含义不同**，见下 |
| `priced` | 实际写入单价的行数 | **R3 用的是既有 `priced`**；**R4 是新字段，且必须 == 既有的 `updated + filled`**（见下） |
| `cancelled_skipped` | 退舱跳过数 | 新字段 |
| `deleted_skipped` | 软删跳过数 | 新字段；**R4 存在但恒为 0**，见下 |

- ⛔ **两个端点的响应体都必须包含全部四个字段**（`total` / `priced` / `cancelled_skipped` / `deleted_skipped`）。
  **R4 的 `deleted_skipped` 是"存在但恒为 0"，不是"可以省掉"。** 字段缺一个，前端与测试就没法统一处理。
- **`priced` 两个端点的口径**：
  - **R3**：`priced` 是**既有字段**（`:946` 定义 / `:956` 累加 / `:962` 返回），语义已符合要求 ⇒ **直接沿用，不要重命名**。
  - **R4**：`priced` 是**新增字段**，含义 = 本次实际写入的行数 = **既有的 `updated` + 既有的 `filled`**
    （R4 循环里：空价补算记 `filled`、覆盖已有记 `updated`；代码里那条审计本来就是 `priced={updated + filled}`）。
    ⇒ 既有 `updated` / `filled` **保留不动**，与新的 `priced` **并存**，且必须满足 **`priced == updated + filled`**。
  - **⛔ `priced` 必须在"写库那一刻"计数，不能提前**：R4 循环里有**多个 `continue` 提前退出**（月份不符、**手填价格不动**、**算不出价 `failed`**、**算出来和现值相同 → 没变化跳过**（实测 `:1224-1225`））。
    这些行**都没有写库**，因此**都不能计入 `priced`**。
    ⇒ 最稳的实现：**直接令 `priced = updated + filled`**（不另设计数器），这样上述四类跳过**天然被排除**，等式**恒成立**。
    ⇒ 若坚持写独立计数器，**`priced += 1` 必须放在 `UPDATE` 之后**（与 `updated`/`filled` **同一位置**，即 `:1228-1231` 那一带），**绝不能**放在"没变化检查"（`:1224`）之前 —— 否则 `priced` 会把"没变化跳过"也算进去，`priced != updated + filled`。
- **`total` 两端含义不同，数值不需要相等、也不该相等**：
  `total` = "该端点 `SELECT` 实际取回的行数"——
  **R3 的是全表**（无 `WHERE`，**含退舱、含软删**）；**R4 的是软删过滤后的结果**（`WHERE COALESCE(is_deleted,0)=0`）。
  ⇒ **验收时不要把两个端点的 `total` 拿来互相比较**，那不是同一个量。
- ⚠️ **别把 `total` 和 R4 既有的 `scanned` 搞混**：`total` = 取回行数；`scanned` = **月份命中**的行数（既有字段，语义不动、也不改名）。计数器自洽要对着 **`total`** 算，不是对着 `scanned`。
- **字段名统一**：R3、R4 都用 `cancelled_skipped`（不许一个叫 `cancelled`、另一个叫 `skipped`）。
- **重叠行归属（一行同时是「退舱」且「已软删」）**：**退舱优先** —— 记 `cancelled_skipped`，不再记 `deleted_skipped`；两个桶**互斥**；**任何情况下都不得计入 `priced`**。
- **R4 的 `deleted_skipped` 恒为 0**：R4 的查询已含 `COALESCE(is_deleted,0)=0`（`:1205`），软删行本来就不会被取回 ⇒ **恒 0 是正常值，不是 bug，不要去"修"**，也**不要**为了让它非 0 而删掉那个过滤。
- **`priced` 与 skipped 的关系**用 `≤`（不要写成 `=`）：还有"算不出价""无变化"等未列举的跳过原因。

## 明确不做（⛔ 范围红线）

- **R1/R2 一行不碰**：退舱单元格编辑限位（`static/app.js:929` 附近）、首次启动补导入（`app.py:372-373`）—— 只登记。
- 不改退舱行/软删行的**原值**：**跳过 = 保留原单价**（与 PR-A「不落点、不动原值」一致）。**不置空。**
- 不改两个端点的既有覆盖口径、不加新端点、不动前端。

## 新增测试（`tests/test_price_batch_guards.py`）

临时库 + `app.test_client()`；夹具样板照抄 `tests/auth_gate_test.py`（**返回值是元组 `(c, appmod)`**；写请求必须带 `X-CSRF-Token`）。造**四类行**：正常 / 退舱 / 已软删 / **退舱且已软删**（同班列/同月，保证被端点扫到）：

1. 正常行照旧被算价 / 重算；
2. 退舱行**单价与原值一字不变**，且计入 `cancelled_skipped`；
3. 已软删行（R3）单价与原值不变，且计入 `deleted_skipped`；
4. **重叠行（退舱+软删）只计入 `cancelled_skipped`**，且**绝不出现在 `priced`**；
5. 计数自洽：**对着 `total`（不是 `scanned`）** 断言 `priced + cancelled_skipped + deleted_skipped ≤ total`；
6. **R4 的 `priced == updated + filled`**（与它既有的两个子计数一致）；
7. **两端点的响应体都含全部四个字段** —— 断言 `deleted_skipped` 在 R4 的响应里**存在且 == 0**（**预期值，不是失败**）；
8. 既有行为不回归：R4 仍然"手填价格不动"、R3 仍然"只补空价"；R4 的 `scanned/updated/filled/failed` 与 R3 的 `priced` 仍在且**语义未变**。
> ⚠️ **不要**写"两端点 `total` 应该相等"这类断言 —— 两者口径不同（R3 全表含软删、R4 已过滤软删），**天然不相等**。

> ⚠️ **第 6 条（`priced == updated + filled`）若红：先定性，不要动手改绿。**
> 这条等式**只要计数点正确就恒成立**：R4 循环里**成功写库**的行，必有且仅有 `updated` 或 `filled` 之一 +1（实测 `:1228-1231`）；而所有 `continue` 跳过的行（月份不符 / 手填不动 / 算不出价 / **没变化**）**都不写库** ⇒ **都不该进 `priced`**。
> ⇒ 若它红了，**最可能是 `priced` 的计数点被放到了某个 `continue` 之前**（实现问题 —— 改计数点即可）。
> ⛔ **不要**为了让断言变绿去改 R4 既有的 `scanned` / `updated` / `filled` / `failed` 语义，也**不要放宽断言**；定性不明就**停下报告 + 登记**。
> （注：R4 **确实存在"没变化跳过"分支** —— 实测 `:1224-1225`。它是**既有行为、不是缺陷**，且**不影响本等式**，因为那行不写库。）

## 报告必须含
- 改了哪些文件 + `文件:行号`（**用你自己 grep 出来的当前行号**）；
- 实跑命令 + **输出末几行**；
- 证据两步：① 新测试文件单跑绿；② **全量** `0 failed`；
  `& $PY -m pytest -q -p no:cacheprovider`（`$PY` = `D:\python.exe`，先 `& $PY -c "import sys;print(sys.executable)"` 自检；输出**不要**重定向到仓库内文件，要存就存 `%TEMP%`）；
- 计划外发现：**只登记，不顺手修**。

不做任何 git 写操作（`add` / `commit` / `push` / 开 PR / 建分支都由芙蕾雅来）。
