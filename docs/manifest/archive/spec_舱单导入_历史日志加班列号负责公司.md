# spec：导入历史日志（update_log）补充「班列号」「负责公司」列

> **v2 修订**：依 DS 反馈修正——改动 2 的 INSERT 示例改为以生产 `apply_diff` 真实结构为准（去掉虚构的 `lg` dict、补回 `created_at`）；`_rec_meta` 签名改为 `def _rec_meta(conn, rid)`（消除 `conn` 自由变量 NameError）；迁移段写死「模块级初始化、只跑一次 + 窄异常捕获（仅 `duplicate column` 才忽略，其余 OperationalError 原样抛出）」；新增 PR 策略与当前工作树说明（基于 dev，不另开 PR，并入 PR #12）；取值写库内原值不 norm；copyTSV 明确加上；验收1 幂等验法写死。
>
> **背景**：生产环境「差异清单」已显示 `班列号`+`负责公司`（洋截图确认）。仓库 dev（HEAD `8f6a8f4`）的「差异清单」`renderUpdates` 已对齐生产列顺序（已改 `templates/manifest.html`，commit `8f6a8f4`）。
> 但**「③ 导入历史 / 回退」展开后的明细日志表（`renderLogs`，数据来自 `update_log`）目前只有 `客户编码/箱号/字段/旧值/新值/动作/操作`，没有 `班列号` 和 `负责公司`**。本 spec 补齐这两列，使审计/回退时也能一眼看到归属班列与负责公司。
> **范围**：仅 `update_log` 的写入、读取、展示 + 表结构迁移。不涉及差异清单（已处理）、导入预览页（待导入）。
>
> **⚠ 实现纪律（重要）**：实现者（opencode）**禁止 `commit`/`push`/开 PR**——完成实现后通知芙蕾雅验收，由芙蕾雅或洋开 PR（Squash 合 `main`）。PR #14 系 opencode 违规自开，已存在；本次验收后由洋 Squash 合入或退回重开。

---

## ⚠️ 前置说明（PR 策略 + 当前工作树，opencode 必读）

- **PR #12 已合并，本 spec 是新 PR（与 修正A 非同一 PR）**：PR #12（dev→main）已于 **2026-09-15 Squash 合入 main**（main 现 = `a8eccb22`，含主功能 + 修正A 修复 + html A 文案）。修正A 已在 main；**本 spec 是独立的新 PR**，不是「并入 PR #12」（PR 已合，无法再追加 commit）。
- **基线 = `main`（`a8eccb22`）**：从 `main` 切新分支（如 `fix/manifest-log-columns`）。**不要从本地 dev（`8f6a8f4`）切**——本地 dev 比 main 多一个未推、未评审的「差异清单列顺序对齐」提交（`git diff a8eccb22..本地dev` 仅 `manifest.html` 4 行），从它切会把未评审改动带进本 PR。
  - 注：`origin/dev`（`aeee989`）= main 内容 + 一次 `Merge main into dev`，与 main 内容一致；从 `origin/dev` 切等价于从 main 切，但统一用 `main` 更清晰。
- **列顺序对齐（本地 `8f6a8f4`）已拍板：并入本 PR**（洋 2026-09-16 依 DS 建议确认）。**经核验无需重排**：main `a8eccb22` 当前 `renderUpdates` 为**未对齐版本**（表头 `班列号/负责公司/客户编码/箱号/变更明细`）；本地未推 commit `8f6a8f4` 的**父版本**结构与之逐行一致（即 `8f6a8f4` 是在该未对齐父版本上做了「→对齐生产」那 4 行）。故改动 6 的「前→后」在 main 上**原样套用即可**——DS 担心的「重排到不同位置」未发生（PR #12 的 html 改动未动 renderUpdates 列序）。opencode 在新分支**按「改动 6」直接重做这 4 行**（勿依赖未推本地 commit `8f6a8f4`，opencode 看不到它），与历史日志增强一起合入 main，避免该本地提交被遗忘。改动 6 与 改动 4 同文件不同函数，互不冲突。
- **文件集**：本 spec 改动 4 文件 —— `app.py`（新增迁移 + 读取接口）、`manifest_engine.py`（apply_diff INSERT×5：① import ② alerts field_fix ③ suffix 客户编码 ④ suffix 目的站 ⑤ updates 通道；PR #14 暂含 ①②③④，⑤ 待洋拍板）、`templates/manifest.html`（改动4 `renderLogs`+`copyTSV`；改动6 `renderUpdates` 顺序对齐）、`mailbots_next/tests/test_manifest_spec.py`（回归）。
- **合入方式**：开新 PR 合 `main`。本仓库 main 受 `required_linear_history` 保护、禁用普通 merge commit；沿用 PR #12 的 **Squash merge**（或 Rebase，dev 历史已对齐 main 亦可——但 Squash 最稳，与 #12 一致）。

---

## 改动 1（必须）— `update_log` 表结构加两列（兼容已存在库）

位置：`app.py` 舱单初始化函数内（`CREATE TABLE IF NOT EXISTS update_log` 之后，约 311–329 行，与建表同一函数、随应用启动执行一次）。

**要点（写死，防 opencode 即兴）**：
- 该初始化函数在应用启动时跑一次（单进程/多 worker 各自启动各跑一次，属正常）。
- 生产库已存在 `update_log` 表，必须用 `PRAGMA table_info` 判存在，避免重复加列报错。
- 多 worker 并发启动时，两个 worker 可能同时判「列不存在」→ 同时 `ALTER` → 一个报 `duplicate column name`。故 **每个 `ALTER` 外层包窄异常捕获**：仅当错误是「重复列」(`duplicate column`) 时才忽略（并发幂等），其余 `sqlite3.OperationalError`（如 `database is locked` / `database is busy` / `disk I/O error` / `attempt to write a readonly database`）**原样抛出**，让启动故障显式暴露，而非埋到运行期 INSERT 才以误导性方式报错。

```python
# 在 CREATE TABLE update_log 与其索引之后追加（同一初始化函数内）：
cur_cols = {r[1] for r in conn.execute("PRAGMA table_info(update_log)").fetchall()}
for col in ("班列号", "负责公司"):
    if col not in cur_cols:
        try:
            conn.execute(f'ALTER TABLE update_log ADD COLUMN "{col}" TEXT')
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise  # 其余 OperationalError(锁/只读/IO)原样抛出，暴露真实启动故障
            pass        # 仅并发重复加列才忽略（幂等）
```

（`sqlite3` 已在 `app.py` 顶部导入，无需再 import。）

---

## 改动 2（必须）— 写库时填充这两列

位置：`manifest_engine.py` 的 `apply_diff`（签名 `def apply_diff(conn, diff, operator, source_files)`，故函数内 `conn` 已可用）。所有 `conn.execute("INSERT INTO update_log ...")` 处共有 **4 处**：① `import` 新增专列箱（约 790 行）；② `field_fix` 字段冲突（约 820 行）；③ `suffix_change` 客户编码（约 849 行）；④ `suffix_change` 目的站（约 857 行）。

**取值来源（写死）**：
- `班列号`：按 `record_id` 从 `records` 取 `"班列号"`（经 `norm_train` 已归一化的值，见下「取值不 norm」说明）。
- `负责公司`：按 `record_id` 从 `records` 取 `"开票子公司名称"`。
- 查不到（`record_id` 为空或记录不存在）→ 写空串 `""`。

新增模块级 helper（定义在 `manifest_engine.py`，与 `apply_diff` 同文件）：

```python
def _rec_meta(conn, rid):
    """按 record_id 取 records 的 班列号 + 开票子公司名称（负责公司）。"""
    if not rid:
        return ("", "")
    r = conn.execute(
        'SELECT "班列号","开票子公司名称" FROM records WHERE id=?', (rid,)
    ).fetchone()
    return (r[0] if r else "", r[1] if r else "")
```

⚠ **取值不 norm**：`records` 表里存的 `班列号` 已是归一化后的值（`norm_train` 在导入时已应用）。本 spec 写库内原值、**不再做 norm**（避免 opencode 多此一举或二次改动语义）。

**INSERT 改动（以生产真实结构为准，不是凭空示例）**：在每处 INSERT 的列清单补 `班列号,负责公司`，VALUES 补两个 `?`，参数末补 `_rec_meta(conn, rid)` 的两个返回值。**务必保留原有 `created_at` 列与 `now` 参数**（原代码末尾即 `,now)`），不要漏。

> 变量名说明（防 DS 误导向）：示例中的 `f` / `old[f]` / `a["new_value"]` **就是生产 `apply_diff` field_fix 分支的真实写法**（`f = a.get("field")` → `old = conn.execute(SELECT ..."{f}"...)` → `old[f]`、`a["new_value"]`），已与真实代码对齐，无需改名。示例仅示意「新增两列」的位置与取值，opencode 按真实代码结构套用即可。

### ① import 新增专列箱（原 ~790 行）
```python
train_no, company = _rec_meta(conn, new_id)
conn.execute(
    "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,"
    "班列号,负责公司,field,old_value,new_value,action,source_file,operator,created_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (batch_id, "import", new_id, r.get("客户编码", ""), r.get("箱号", ""),
     train_no, company,
     "(新增专列箱)", "", f"{r.get('客户编码','')}/{r.get('箱号','')}",
     "增", ",".join(source_files), operator, now))
```

### ② field_fix 字段冲突（原 ~820 行，原 `old` 已含 客户编码/箱号/字段值）
```python
train_no, company = _rec_meta(conn, rid)
conn.execute(
    "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,"
    "班列号,负责公司,field,old_value,new_value,action,source_file,operator,created_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (batch_id, "update", rid, old["客户编码"] or "", old["箱号"] or "",
     train_no, company,
     f, old[f] or "", a["new_value"],
     "改", ",".join(source_files), operator, now))
```

> ⚠ **③/④ 复用修正 A 的既有变量（勿重复定义）**：`old_code` / `old_station` / `nc` / `st` 由修正 A（已随 PR #12 合入 main `a8eccb22`）在 `suffix_change` 分支定义——`old_code = old["客户编码"] if old else ""`、`old_station = old["目的站"] if old else ""`、`nc = a.get("new_code")`、`st = a.get("目的站")`。本 spec 的 ③/④ 直接复用它们写 `update_log`，**勿重复定义、勿改名**（①/② 用到的 `r` / `old["客户编码"]` / `old["箱号"]` 同为 `apply_diff` 既有变量）。
>
### ③ suffix_change 客户编码（原 ~849 行，原 客户编码列写 `old_code`、箱号列写 `""`）
```python
train_no, company = _rec_meta(conn, rid)
conn.execute(
    "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,"
    "班列号,负责公司,field,old_value,new_value,action,source_file,operator,created_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (batch_id, "update", rid, old_code, "",
     train_no, company,
     "客户编码", old_code, nc,
     "改", ",".join(source_files), operator, now))
```

### ④ suffix_change 目的站（原 ~857 行）
```python
train_no, company = _rec_meta(conn, rid)
conn.execute(
    "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,"
    "班列号,负责公司,field,old_value,new_value,action,source_file,operator,created_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (batch_id, "update", rid, old["客户编码"] if old else "", "",
     train_no, company,
     "目的站", old_station, st,
     "改", ",".join(source_files), operator, now))
```

> **关于性能（可选，不强制）**：suffix_change 拆两行日志后，同一 `rid` 会查 `records` 两次（③、④ 各一次 `_rec_meta`）。功能正确、开销极小，可接受现状。若想优化，可在 `apply_diff` 上层对每个 `rid` 取一次 `rec_meta` 缓存后传下，但本 spec 不要求。

### ⑤ updates 通道（确认更新，原 ~768 行，spec 补遗）

`apply_diff` 在 imports 循环**之前**，还有一个 `for ch in diff.get("updates", [])` 循环处理「确认更新」（前端 import 预览里用户直接改字段值后确认的那条路径）。该路径也 `INSERT INTO update_log`，但 **v2 原只数了 4 处 INSERT、漏列了它**——其新日志两列为空，会导致审计行不一致。本 spec 补上：与 ② 同模式补 `班列号,负责公司`。

```python
train_no, company = _rec_meta(conn, rid)
conn.execute(
    "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,"
    "班列号,负责公司,field,old_value,new_value,action,source_file,operator,created_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (batch_id, "update", rid, u.get("客户编码", ""), u.get("箱号", ""),
     train_no, company,
     f, old_val, new_val, ch.get("action", "改"),
     ",".join(source_files), operator, now))
```

⚠ **本处是 v2 的疏漏**（实际 `apply_diff` 有 5 处 INSERT，非 4 处）。PR #14 实现时未含此 ⑤（opencode 严格按 spec 的 4 处改，并正确上报）。

✅ **已拍板纳入（洋 2026-09-16）**：opencode 在 PR #14 补一笔 commit 实现本 ⑤（_rec_meta 已定义，改动极小）。纳入后 `update_log` 全部 5 条审计路径均带班列号/负责公司，与本次 spec 审计完整性目标一致。

---

## 改动 3（必须）— 读取接口返回这两列

位置：`app.py` `api_manifest_batch_detail`（约 1505–1510 行）。SELECT 列表追加 `班列号,负责公司`（**保持原 `created_at` 末尾不变**）：

```python
logs = conn.execute(
    "SELECT id,batch_id,batch_type,record_id,客户编码,箱号,"
    "班列号,负责公司,field,"
    "old_value,new_value,action,source_file,operator,COALESCE(reverted,0) reverted,"
    "created_at FROM update_log WHERE batch_id=? ORDER BY id",
    (batch_id,)
).fetchall()
```

---

## 改动 4（必须）— 前端日志表（renderLogs）加两列 + copyTSV

位置：`templates/manifest.html` `renderLogs`（main `a8eccb22` 位于 **747–779 行**）。**以下为 main 当前真实代码，opencode 在「箱号」列后插入「班列号」「负责公司」两列即可，勿改其余。**

thead：在 `el("th",{text:"箱号"})` 之后、`el("th",{text:"字段"})` 之前插入两列：
```javascript
// 原
    el("th", { text:"客户编码" }), el("th", { text:"箱号" }), el("th", { text:"字段" }),
// 改后
    el("th", { text:"客户编码" }), el("th", { text:"箱号" }),
    el("th", { text:"班列号" }), el("th", { text:"负责公司" }),
    el("th", { text:"字段" }),
```

tbody 行：在 `el("td",{text:l["箱号"] || ""})` 之后、`el("td",{text:l.field || ""})` 之前插入两列：
```javascript
// 原
      el("td", { text:l["客户编码"] || "" }), el("td", { text:l["箱号"] || "" }),
      el("td", { text:l.field || "" }),
// 改后
      el("td", { text:l["客户编码"] || "" }), el("td", { text:l["箱号"] || "" }),
      el("td", { text:l["班列号"] || "" }), el("td", { text:l["负责公司"] || "" }),
      el("td", { text:l.field || "" }),
```

`copyTSV(l)`（main 位于 **780–783 行**）：在数组 `l["箱号"]` 之后插入两列（保持 `record_id` 最前不变）：
```javascript
// 原
  const line = [l.record_id, l["客户编码"], l["箱号"], l.field, l.old_value || "", l.new_value || "", l.action].join("\t");
// 改后
  const line = [l.record_id, l["客户编码"], l["箱号"], l["班列号"] || "", l["负责公司"] || "", l.field, l.old_value || "", l.new_value || "", l.action].join("\t");
```


---

## 改动 5（建议）— 回归测试

在 `mailbots_next/tests/test_manifest_spec.py` 追加断言：构造一次字段冲突确认 + 一次后缀变更确认 → 查 `update_log` → 对应行的 `班列号`、`负责公司` 均非空且等于 `records` 中该 `record_id` 的实际值。

---

## 改动 6（必须，已拍板并入）— 差异清单（renderUpdates）列顺序对齐生产

位置：`templates/manifest.html` `renderUpdates`（main `a8eccb22` 位于 **414–444 行**）。源 = 本地未推 commit `8f6a8f4`（洋 2026-09-15 已写，但仅本地、未评审、未推）；依洋拍板并入本 PR，由 opencode 在新分支**重做**这 4 行，**勿 cherry-pick 那个本地 commit**（opencode 看不到它）。

**经核验**：main 当前 `renderUpdates` 为**未对齐版本**（`班列号/负责公司/客户编码/箱号/变更明细`）；本地未推 commit `8f6a8f4` 的**父版本**结构与之逐行一致（即 `8f6a8f4` 是在该未对齐父版本上把列序改为对齐生产）。故本改动在 main 上**原样套用其「前→后」即可、无需重排位置**。

目标（对齐生产截图）：表头与每行顺序改为 `客户编码 / 箱号 / 班列号 / 负责公司 / 变更明细`。

thead（原 → 改后）：
```javascript
// 原
    el("th", { text:"班列号" }), el("th", { text:"负责公司" }),
    el("th", { text:"客户编码" }), el("th", { text:"箱号" }), el("th", { text:"变更明细" })
// 改后
    el("th", { text:"客户编码" }), el("th", { text:"箱号" }),
    el("th", { text:"班列号" }), el("th", { text:"负责公司" }),
    el("th", { text:"变更明细" })
```

tbody 行（原 → 改后）：
```javascript
// 原
      el("td", { text:u["班列号"] || "" }),
      el("td", { text:u["负责公司"] || "" }),
      el("td", { text:u["客户编码"] || "" }),
      el("td", { text:u["箱号"] || "" }),
      el("td", null, ch),
// 改后
      el("td", { text:u["客户编码"] || "" }),
      el("td", { text:u["箱号"] || "" }),
      el("td", { text:u["班列号"] || "" }),
      el("td", { text:u["负责公司"] || "" }),
      el("td", null, ch),
```

> 注：本改动与 改动 4（renderLogs）同文件不同函数，无冲突。合入后，本地未推 commit `8f6a8f4` 即被本 PR 内容取代，洋合入后可删除该本地 commit（仓库卫生，待办）。

---

## 验收标准（芙蕾雅验收用）

1. **迁移幂等**：在已存在 `update_log` 表的库上**连续执行两次**迁移段不报错（第二次因 `PRAGMA table_info` 已判列存在而跳过）；并发/重复 `ALTER` 的 `duplicate column` 由**窄 `except`** 忽略（仅该错误被吞，其余 `OperationalError` 原样抛出）。并发 ALTER 的竞态路径难以在单测里稳定复现，本项以「连续执行两次不报错 + 表结构正确（两列已加上）」为准，不必强求测出并发那一支。
2. 对一条既有记录做箱号冲突确认 → `update_log` 新行 `班列号`=`该记录班列号`、`负责公司`=`该记录开票子公司名称`。
3. 对一条做后缀变更确认（客户编码变 + 目的站变）→ 两条单列日志各自带正确的 `班列号`/`负责公司`。
4. 展开「导入历史 / 回退」某批次 → 日志表显示 `班列号`、`负责公司` 两列且值正确；`copyTSV` 导出含这两列。
5. 整批回退 / 单条撤销后，`update_log` 的这两列保留（回退只动 `reverted` 标记，不改这两列）。
6. 老库（迁移前无这两列）升级后，历史日志行这两列显示为空白，不报错。
7. **差异清单列顺序（改动 6）**：展开差异清单 → 表头与每行顺序为 `客户编码 / 箱号 / 班列号 / 负责公司 / 变更明细`，对齐生产截图；与 `renderLogs`（改动 4）互不冲突、两处均正确显示。

---

## 不在本次范围

- 差异清单列顺序：已并入本 PR（见 改动 6），不再单独处理。
- 待导入预览页（renderImports）加负责公司：该页是陌生客编新箱，库内无记录，需从「客户编码→子公司」映射反查，属另一需求，另议。
- revert_batch / revert_item 回滚语义调整（见修正A spec，已定不实现）。
