# 修正 Spec：舱单导入 · 后缀变更审计漏记 + revert_batch 潜在崩溃（验收 14 缺陷 A + 历史遗留崩溃）

> **状态：已落地归档（历史记录）** —— 本 spec 描述的修复（改动 1 / 1b / 2）已随 **PR #12（2026-09-15 Squash 合入 main `a8eccb22`）** 落地并验收通过，**本文件不再执行、无需再开 PR**。§5 的 revert 回滚语义增强（真正还原客户编码/目的站）**仍未实现，属独立增强，待洋单独拍板后另行立项**，不在本归档 spec 范围。

> 接 `spec_舱单导入_专列放行与箱属封号确认.md`（以下简称「主 spec」）。
> 本文是 PR #12（commit 2882af3）验收后发现的 **必改缺陷 + 防御性加固 + 可选项 + 合并纪律**。
> 实现方：opencode；验收方：芙蕾雅（按 dev→PR→main）。
> **v4 修订**：DS 再反馈两必须修——(1) 改动 1b 的 `except sqlite3.OperationalError` 捕获过宽（会静默吞 database is locked 等真实错误）→ 改窄异常，仅 `no such column` 跳过、其余 `raise`；(2) §3「两单列日志均标记 reverted=1」与 revert_batch 白名单 `continue`（不标记）不符，且改动 1b「捕获后仍标记 reverted=1」会制造「假回退」→ 捕获分支统一不标记 reverted。另标注 revert_item 白名单 `pass` 与 revert_batch 不一致的历史遗留。（v3 已补写死 5 点；v2 已纠正 v1 对 revert 反向写回的反向描述。）

## 0. 背景与定位

主 spec §5 验收标准 **第 14 条「留痕」** 要求：确认类操作须把修改前后值写入 `update_log`，便于审计回查。

PR #12 后端 19 项测试全绿、前端交互（箱号二次确认、报警消失）逻辑到位，但 `apply_diff` 的 `suffix_change` 分支有两处问题：

1. **审计截断**（验收 14 违反）：`new_value` 被 `[:0]` 截空，目的站新值丢失。
2. **revert_batch 潜在崩溃**（历史遗留）：`field` 字面量写作 `"客户编码/目的站"`，而 `revert_batch`/`revert_item` 对不在跳过集合的 `field` 会执行 `UPDATE records SET "{field}"=?` → 该列不存在 → **SQLite 报错崩溃**。

库内 `records` 两列（`客户编码`/`目的站`）实际都写对了（UPDATE 正确），**仅审计日志漏记 + 回滚路径有雷**。客户运单数据留痕不完整属硬伤，必须修；回滚崩溃属线上隐患，必须一并排掉（含历史库里已有的合体日志）。

## 1. 缺陷现象（精确，已读生产代码核实）

### 1.1 审计截断（验收 14）
`manifest_engine.py`，`apply_diff` 内 `source == "suffix_change"` 分支，约 **第 845–846 行**：

```python
conn.execute(
    "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,field,"
    "old_value,new_value,action,source_file,operator,created_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
    (batch_id, "update", rid, old["客户编码"] if old else "", "",
     "客户编码/目的站", "", a.get("new_code", "") + ("|" + a.get("目的站", ""))[:0],
     "改", ",".join(source_files), operator, now))
```

- `new_value` 被 `("|" + a.get("目的站", ""))[:0]` 截空 → 只剩 `new_code`，**「目的站」新值丢失**（验收 14 违反）。
- `old_value` 传 `""` → 连旧值也没记，审计无从对比。
- `[:0]` 是笔误（疑似想写 `[:N]` 截断却写成 `0`）。

### 1.2 revert_batch 潜在崩溃（历史遗留）
真实 `revert_batch` 代码（第 862–880 行），update 类日志处理：

```python
else:
    if lg["field"] in ("客户编码", "目的站") or lg["field"] == "(新增专列箱)":
        continue                              # ← 命中跳过，不回写
    conn.execute(f'UPDATE records SET "{lg["field"]}"=? WHERE id=?',
                 (lg["old_value"], lg["record_id"]))
```

`revert_item`（第 883–906 行）同构：`if lg["field"] in ("客户编码","目的站"): pass`。

**问题**：`field="客户编码/目的站"` 这个字符串 **不在** `("客户编码","目的站")` 集合里（8 字符 ≠ 4 字符、≠ 3 字符）→ 不命中 `continue` → 落到第 876 行执行 `UPDATE records SET "客户编码/目的站"=?` → **records 表无此列 → `SQLITE_ERROR: no such column` 崩溃**。

> ⚠ **DS 反馈核实结论**：原 v1 spec「要点」写"revert_batch 按 field in (...) 反向写回，保持字面量不变才不会破坏回滚"——**完全反了**。实际是 `continue` 跳过；且保持 `"客户编码/目的站"` 恰恰会触发崩溃。此崩溃是**历史遗留**（旧生产代码 `tmp/prod_manifest_engine.py:756` 也是合体 field），被 `[:0]` 截断 + 实际无人回滚过 suffix_change 批次 一起掩盖，此前未暴露。本 spec 用两处手段消除：①改动 1 让**新**日志不再产生合体 field；②改动 1b 在 revert 侧加防御，**历史**合体日志回退也不再崩。

## 2. 修改要求（精准、零歧义，关键判断已写死）

### 改动 1（必须改）— suffix_change 按列拆两条 update_log（审计 + 消崩溃）

**拆的是日志，不是 UPDATE**：`records` 的写入保持原样（第 832–835 行，按 `sets` 列表一次写两列），本次对它**零改动**。只把 `update_log` 插入从「一条合体」改为「按实际变更列各一条单列」。

**日志写入判断条件（写死，opencode 勿即兴发挥）：**

```python
old_code    = old["客户编码"] if old else ""
old_station = old["目的站"]   if old else ""
nc = a.get("new_code")     # 后缀变更 alert 顶层携带，见主 spec §8.1
st = a.get("目的站")

if nc and nc != old_code:                 # 客户编码 真变了 → 写一条 field="客户编码"
    conn.execute(
        "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,field,"
        "old_value,new_value,action,source_file,operator,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, "update", rid, old_code, "", "客户编码",
         old_code, nc, "改", ",".join(source_files), operator, now))
    n_alert += 1
if st and st != old_station:             # 目的站 真变了 → 写一条 field="目的站"
    conn.execute(
        "INSERT INTO update_log(batch_id,batch_type,record_id,客户编码,箱号,field,"
        "old_value,new_value,action,source_file,operator,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, "update", rid, old["客户编码"] if old else "", "", "目的站",
         old_station, st, "改", ",".join(source_files), operator, now))
    n_alert += 1
# 两列都没真变 → 不写任何日志行（不产生空日志）
```

**判断条件写死（逐条）：**
- `nc and nc != old_code` → 写一条 `field="客户编码"`（**非空 且 ≠ 旧值**才写）。
- `st and st != old_station` → 写一条 `field="目的站"`（同上）。
- 两列都未变 → **不写日志行**（不产生空/无变化日志）。
- 只变一列 → 只写一条。

**要点（opencode 必读）：**
- **UPDATE 保持一次、不拆**：`records` 写入仍走第 832–835 行原有 `sets` 列表（只要 `nc`/`st` 非空即更新对应列，**不**判断是否等于旧值），本次对它零改动。**禁止把 UPDATE 也拆成两条**——拆的是 `update_log`，不是 `UPDATE`。
- **≠ old 判断只用在日志写入上**，勿挪到 UPDATE（UPDATE 维持原样）。
- `old` 在第 837 行 UPDATE（838–840 行）**之前**已 `SELECT "客户编码","目的站" FROM records WHERE id=?` 取出，直接复用，**勿再查**；`old` 可能 `None`，用 `if old else ""` 兜底。
- `field` 用单列 `"客户编码"` / `"目的站"`（**严禁**合体 `"客户编码/目的站"`）—— 命中 revert 跳过集合 → 不执行 `UPDATE records SET "客户编码/目的站"=?`，消除 §1.2 崩溃。
- **n_alert 按日志条数计（写死）**：每写一条 `update_log` +1；两列都变 → +2，只变一列 → +1，都不变 → +0。与现有 `n_alert` 语义（按 `update_log` 实际条数走）一致，**勿改为「一次确认操作 +1」**。
- 第 5 个参数（箱号列）保持 `""`（后缀变更不涉及箱号）。

> 注：主 spec §8.2 描述 suffix_change「一条覆盖两列」指 `records` 的 UPDATE（sets 列表一次覆盖两列），本文仅调整 `update_log` 审计粒度（拆两行单列），**不改动 records 写入、不改动 alerts_applied 结构**。

### 改动 1b（必须，防御性）— revert_batch / revert_item 未知 field 不裸拼 SQL（窄异常：不吞真实错误、不制造假回退）

历史库里可能已有 `field="客户编码/目的站"` 的合体日志（PR #12 之前产生）。若有人回退老批次，仍会因 `UPDATE records SET "客户编码/目的站"=?` 崩（`no such column`）。本次**顺手消除该历史风险**（成本极低）。

**采用方案 A（窄异常捕获，改动最小）**：DS 另提方案 B（白名单校验 `field not in KNOWN_COLUMNS` 则跳过）——亦可，但本 spec 采用 A，diff 最小、不引入新常量。

1. 文件顶部 `import sqlite3`（第 19 行附近）补一行：`import logging`。
2. 在 `revert_batch`（第 876 行）与 `revert_item`（第 898 行）的 `conn.execute(f'UPDATE records SET "{lg["field"]}"=? ...')` 外包 `try/except`，**仅当 `"no such column" in str(e)` 时 warning 跳过，其余 `OperationalError` 一律 `raise`**：

```python
try:
    conn.execute(f'UPDATE records SET "{lg["field"]}"=? WHERE id=?',
                 (lg["old_value"], lg["record_id"]))
except sqlite3.OperationalError as e:
    if "no such column" not in str(e):
        raise  # 其余 OperationalError（database is locked / busy / no such table / disk I/O error / readonly）原样抛出，绝不停默吞
    logging.getLogger(__name__).warning(
        "revert 跳过未知列 field=%r record=%s: %s", lg["field"], lg["record_id"], e)
    continue  # 仅 revert_batch 用（revert_item 改用 return True，见下）；跳过写回，且不标记 reverted=1
```

**revert_batch 适配**：`except` 内的 `continue` 会跳过原第 878 行 `UPDATE update_log SET reverted=1`（该标记在 `else` 块外、循环末尾）→ 合体 field 行**不标记 reverted=1**，与白名单 `continue` 分支语义一致（都不回写、都不标记）。正常路径（无异常）仍落到该行标记 reverted=1；白名单 `continue` 仍跳过标记（不变）。

**revert_item 适配**：`revert_item` 是单条非循环、无 `continue`。`except` 内 `if "no such column"...: raise` 之后改为 `return True`（warning 已记）——即「不回写、不标记 reverted、函数正常返回」，reverted 保持 0。这会跳过末行 `UPDATE update_log SET reverted=1`（line 900）与批次完成检查（902–905）；历史合体 field 本就非真回退，reverted=0 是诚实状态，可接受。

**要点（opencode 必读）：**
- **绝不停默吞错误（核心）**：`sqlite3.OperationalError` 涵盖 `database is locked` / `database is busy` / `no such table` / `disk I/O error` / `attempt to write a readonly database` 等。**只放行 `no such column`**，其余必须 `raise`——否则回滚时遇锁错误会被静默忽略、用户误以为整批已回退、实际部分行没写回，比崩溃更糟。
- **不制造「假回退」**：捕获跳过的行**不标记 reverted=1**（reverted 保持 0），运维看 `update_log` 不会被「已回退」误导（它确实没回写 records）。
- 跳过集合（`客户编码`/`目的站`/`(新增专列箱)` 的 `continue`/`pass`）与正常回写逻辑**不变**。
- ⚠ **已知既有不一致（本次不修，仅标注）**：当前 `revert_item` 白名单 `pass` 分支会落到 line 900 **标记 reverted=1**（而 `revert_batch` 白名单 `continue` 不标记）——两函数对白名单字段语义已预先相反。本 spec 的 1b 捕获分支统一选择「不标记」，与新合体 field 行为一致、不制造假回退；`revert_item` 白名单「标记 reverted=1 但不回写」属历史遗留 wart，不在本次范围，后续仓库卫生议题收口。

### 改动 2（非阻塞，可选）— 前端按钮文案中性化
`templates/manifest.html` 中 `applyImportsBtn` 文案若为「确认导入专列」：因现在散舱也走此确认通道（主 spec 改动一），建议改为 **「确认导入所选」**。改不改都不阻塞合并。

### 改动 3（建议）— 回归测试
`mailbots_next/tests/test_manifest_spec.py` 新增 **2 项**：
- **(a) 审计完整 + 计数**：构造同时变更 `客户编码`+`目的站` 的 suffix_change 确认 → 断言 `update_log` 有**两条**（`field='客户编码'` / `field='目的站'`），`new_value` 分别=新 code / 新 station，`old_value` 分别=旧值；且 `n_alert` 较基线 +2。再构造「只变目的站」→ 断言仅 1 条 `field='目的站'`、`n_alert` +1。目的：把验收 14 变机器可验，锁死 `[:0]` 类笔误与计数口径。
- **(b) revert 不崩溃（核心）**：见 §3 验收项。

## 3. 验收（芙蕾雅复验清单）

- [ ] 改动 1 落地，`pytest mailbots_next/tests/test_manifest_spec.py` 全绿（含改动 3 新增 a/b）。
- [ ] 手工：`SELECT field,old_value,new_value FROM update_log WHERE batch_id=? AND field IN ('客户编码','目的站')` → 两列新旧值齐全（**不再有合体 `客户编码/目的站` 行**）。
- [ ] **revert 不崩溃（核心，对应 §1.2 / 改动 1b）**：
  - **新批次**：构造 suffix_change 确认（两列均变）→ 跑 `revert_batch` → **不抛 `SQLITE_ERROR`**；两条单列日志（`field=客户编码`/`field=目的站`）按既有白名单 `continue` 设计**不回写 records、reverted 保持 0**（与改动前 `客户编码/目的站` 类字段的回退行为一致，非回归）；`records` 两列保持新值。
  - **历史批次**：在库内置入一条 `field="客户编码/目的站"` 的老日志 → 跑 `revert_batch` → 因 改动 1b 的窄 `try/except` 仅捕获 `no such column`，**记 warning + `continue` 跳过该行、不崩、该行 reverted 保持 0**；其余行正常回退（标记 reverted=1）。（亦可构造同结构老日志跑 `revert_item` 验证 `return True` 且不标记 reverted。）
- [ ] **n_alert 口径**：两列都变 → +2；只变一列 → +1；都不变 → +0（与「日志条数」一致，非「一次确认 +1」）。
- [ ] 前端交互（验收 8/13/14/15）仍正常：箱号二次确认、部分勾选消失、失败保留——本次仅动 `update_log` 写入与 revert 防御，**不影响**（`manifest.html` 读的是 alert，不读 `update_log.field`）。
- [ ] 整库回滚（验收 15 事务）不受影响：`app.py` 调用方 `except` 内 `conn.rollback()` 仍接住；`revert_batch` 仅被 try/except 防御包裹，正常回写路径未改。

## 4. 合并纪律（重要，勿忽略）

**本 spec 的修复已随 PR #12 合入 main，此节原指令作废。**

PR #12（dev→main）已于 **2026-09-15 以 Squash 方式合入 main**（main 现 = `a8eccb22`，commit message `feat(manifest): 专列按客编数放行+箱属更新/冲突可确认+退舱剔除+已确认报警消失（含修正A 审计按列拆分）`）。故：

- 修正A 的改动（改动 1 / 1b / 2）**已落地 main、已验收通过**（6 项测试绿见 PR #13 验证记录；PR #12 合入即含此修复）。
- 本 spec **无需再开 PR、无需从 main 切分支**。
- 原 §4 写的「禁止在 PR #12 上追加、从 main 切 `fix/manifest-audit-log` 分支、PR #12 关闭/不合并」是针对早期「PR #12 是 77 文件大杂烩、应关掉另开」的判断，**该判断后被证实基于过期本机引用（火绒拦 `.git/packed-refs` 致 `origin/main` 停旧值）属误判**——PR #12 在 GitHub 上始终是 3 文件干净 PR，并已按「reopen + 把修复 cherry-pick 到 dev + Squash 合入 main」路径完成。此段已删除。

**后续舱单类 spec 的基线约定（供 `spec_舱单导入_历史日志加班列号负责公司.md` 等复用）：**
- 凡改 `manifest_engine.py` / `templates/manifest.html` / `test_manifest_spec.py` 中**已在 main 的内容**，新改动**从 `main`（`a8eccb22`）切干净分支**，开新 PR 合 main。
- **不要从本地未推的 dev 切分支**——本地 dev（`8f6a8f4`）比 main 多一个未推、未评审的「差异清单列顺序对齐」提交（`git diff a8eccb22..本地dev` 仅 `manifest.html` 4 行），从它切会把未评审改动带进 PR。
- 合入后按 WORKFLOW §5.4 情况 B：把 main 合回 dev（保持 dev 与 main 内容对齐）。PR #12 合入后已执行（dev `aeee989` = main + `Merge main into dev`；`git diff a8eccb22..aeee989` 为空，内容已对齐）。

## 5. 设计决策提示（洋已拍板，本次不实现）

洋已确认：当前设计 `客户编码`/`目的站` 在 `revert` 跳过集合内，**suffix_change 整批回退时不还原这两列**（与改动前一致，非回归）。本止血 spec 保持该行为，只排雷 + 补审计 + 加防御。

若未来要求「整批回退真正还原客户编码/目的站」，需另改 `revert_batch`/`revert_item`：识别合体 field（或按两条日志）分别 `UPDATE 客户编码`/`UPDATE 目的站` 回写 old——属独立增强，风险在回滚路径（验收 15），不在本止血范围，待洋单独拍板。

## 6. 不做的事（边界）

- 不动主 spec 四项改动本身逻辑（专列阈值、箱属、退舱剔除、报警消失均已通过）。
- 不动 `revert_batch`/`revert_item` 的**跳过集合与正常回写逻辑**；仅 改动 1b 在其 `UPDATE` 外包裹 `try/except`（防御未知列，不影响正常路径）。
- 不扩大改动范围去收拾 dev/main 分叉（单独仓库卫生任务）。
