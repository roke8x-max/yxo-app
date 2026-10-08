# Spec：舱单导入 · 退舱/终态重现拦截 + 源端缺失建议退舱（A + C + D）

> 接 `spec_舱单导入_专列放行与箱属封号确认.md`（主 spec，下称「主 spec」）与 `spec_舱单导入_历史日志加班列号负责公司.md`（已合 PR #14）。
> 本 spec 修一个**主 spec 改动三（退舱剔除）的已知副作用**：退舱记录被整个踢出匹配池 → 源清单仍含该客编时，被当成「陌生客编」当新纪录插入，造成「退舱 + 新正常」两行重复计数。

## 状态 / 基线 / 分工

- **状态**：新需求，待实现。
- **基线**：`main`（当前 = `4df223f0…`，PR #14 已合入后的 tip）。**从 `main` 切新分支**（如 `fix/manifest-terminal-guard`），勿从任何本地未推分支切。
- **实现者**：opencode（用户指定）。**芙蕾雅负责需求讨论、spec 定稿与验收，不碰实现代码。**
- **⚠️ 流程铁律（针对 PR #14 违规自开）**：实现者**禁止 `commit`/`push`/开 PR**。代码完成后只告知芙蕾雅，由芙蕾雅验收通过后再开 PR / Squash 合入。
- **范围**：仅 `manifest_engine.py` 的 `build_diff` 匹配逻辑 + 1 个新增终态索引 + `load_records` 过滤扩展（D）+ 配套测试。**不涉及**前端交互（洋选 A 非 B：告警自然进现有 alerts 区，等其在前端处理，无需新确认框）、不涉及 `apply_diff` 写入逻辑、不涉及 `mailbots*`（`mailbots_next/core/store.py:85/178` 的 `状态<>'退舱'` 是邮件机器人解析范围，与本改动无关，不动）。

---

## 背景与根因（已实读代码确认）

- `manifest_engine.py:462-485` `load_records()`：`WHERE COALESCE("状态",'')<>'退舱' AND COALESCE(is_deleted,0)=0` → 退舱记录**不进** `by_core` 匹配索引。
- `manifest_engine.py:490` `build_diff(conn, parsed_rows)`：主循环 `cands = by_core.get(core, [])`（:528）；`if not cands:`（:530）把查不到匹配的客编按「陌生客编」当新纪录 `_add_import`（:542）→ `apply_diff` 插入新行。
- **副作用**：库内手动标退舱、但渝新欧源还没操作退舱 → 同客编在源重现 → `cands=[]` → 当新纪录插入 → 库里「退舱 + 新正常」两行重复。

本 spec 的目标：**退舱记录仍不参与匹配/更新（守住 `AGENTS.md` §6.1 铁律 2：退舱凭据永不被覆盖），但「源端重现已退舱/终态客编」要被检测并告警拦截，不再静默造重复。**

---

## 改动 D（必做配套，先于 A/C）：TERMINAL_STATUSES 终态集合

在 `manifest_engine.py` 顶部（邻近 `DEDICATED_THRESHOLD` 处）新增常量：

```python
# 已终结状态：这些状态的记录永不参与舱单导入的匹配/更新（铁律 2），
# 且其客编在源清单重现时需告警拦截（见 build_diff 方案 A）。
# 以后新增终态（如"作废"）在此集合加即可，勿硬编码散落各处。
TERMINAL_STATUSES = {"退舱", "延期"}
```

- **`load_records()` 过滤扩展**（:468）：从只排除退舱，改为排除全体终态 ——
  `WHERE COALESCE("状态",'') NOT IN (/* 占位 */) AND COALESCE(is_deleted,0)=0`。
  用参数化（`status_col IN (?)` 拼占位，或 Python 端先 fetch 再 filter，二选一，opencode 定；SQLite `IN` 对集合需动态占位，推荐 Python 端 `status not in TERMINAL_STATUSES` 过滤，避免拼 SQL）。
  **目的**：防「延期」等终态也漏进 active 匹配池（主 spec 场景 4）。
- **核查点**：`load_records` 仅被 `build_diff` 与其单测使用；确认无其它调用方依赖「只剔退舱、留延期」的旧行为。若有，逐处确认延期排除也是期望。

---

## 改动 A（核心）：终态重现 → 拦截 + 告警

在 `build_diff` 内：

### A.1 新增终态索引
在 `recs = load_records(conn)`（:492）之后，新增一次查询建 `by_core_term`（与 `by_core` 同 key=code_core）：

```python
# 方案 A：终态记录单独建索引，用于"源端重现已终结客编"的告警拦截。
term_rows = conn.execute(
    'SELECT id,"客户编码","箱号","班列号","状态" FROM records '
    'WHERE COALESCE(is_deleted,0)=0'
).fetchall()
by_core_term = {}
for r in term_rows:
    st = (r["状态"] or "").strip()
    if st in TERMINAL_STATUSES:
        by_core_term.setdefault(code_core(r["客户编码"] or ""), []).append({
            "id": r["id"], "code": r["客户编码"] or "",
            "box": norm_box(r["箱号"]), "train": (r["班列号"] or "").strip(),
            "status": st,
        })
```

> 注：`by_core_term` 包含**全部**终态记录（含已软删=0），与 `by_core`（active）互斥。

### A.2 主循环拦截（改 :530 `if not cands:` 分支）
当前：
```python
        if not cands:
            # 陌生客编一律放行，按该班列客编总数判专列/散舱
            tn = row["班列号"]
            ...
            _add_import(...)
            ...
            continue
```
改为：在进入「陌生客编放行」之前，先查终态索引；命中则**拦截 + 告警、不插入**，否则维持原「陌生客编放行」：

```python
        if not cands:
            # —— 方案 A：终态重现拦截 ——
            term = by_core_term.get(core)
            if term:
                alerts.append({
                    "type": "终态重现",
                    "key": str(row_idx),
                    "客户编码": row["客户编码"], "箱号": row["箱号"],
                    "说明": f"客编 {row['客户编码']} 本地已有「{term[0]['status']}」记录"
                            f"（id={term[0]['id']}），但源清单仍含该客编，"
                            f"未作为新纪录导入——请确认渝新欧是否已操作退舱/延期，"
                            f"再决定如何处理本地终态记录。",
                })
                continue  # 不放行进 imports，不写库，等前端人工处理
            # 陌生客编一律放行，按该班列客编总数判专列/散舱
            tn = row["班列号"]
            ...
            _add_import(...)
            ...
            continue
```

**语义要点**：
- `cands` 为空 = active 池无同 core → 才检查终态。若 active 有同 core（场景 3：1 退舱 + 1 正常同 core），走原匹配/更新逻辑，终态那条不被碰（正确，守铁律 2）。
- 拦截只 `continue`，**不 `_add_import`** → 该客编不进 `imports`、不膨胀专列阈值（`:509-512` `batch_codes` 不含它，正确）。
- 原退舱/终态记录**完全不被修改、不写 `update_log`**（铁律 2）。

---

## 改动 C（反向缺口）：源端缺失 → 建议退舱告警

在 `build_diff` 主循环结束后（return 之前），新增一段：扫描「本次导入覆盖的班列内、源清单已不含的 active 客编」，产「建议置退舱」告警（**不自动改**）。

```python
    # —— 方案 C：反向缺口。库内 active 记录，其班列在本批导入范围内、
    # 但客编未出现在本次源清单 → 源端可能已操作退舱，建议人工确认置退舱。
    # 仅限 train ∈ batch_codes 的班列（避免"只导入部分班列"误报其它班列的正常记录）。
    source_cores_by_train = batch_codes  # {train: set(core)}，已在 :509-512 构建
    for r in active:
        tn = r["train"]
        if not tn or tn not in source_cores_by_train:
            continue
        if r["core"] in source_cores_by_train[tn]:
            continue  # 源端仍有该客编，正常
        alerts.append({
            "type": "源端缺失建议退舱",
            "key": f"missing:{r['id']}",
            "客户编码": r["code"], "箱号": r["box"],
            "说明": f"库内记录（id={r['id']}，班列 {tn}）状态为「{r['status'] or '正常'}」，"
                    f"但本次导入源清单未含该客编——请确认渝新欧是否已操作退舱，"
                    f"若是则手动置退舱（本系统不自动改）。",
        })
```

**语义要点**：
- 仅在 `train ∈ batch_codes`（本批导入涉及的班列）内判断 → 部分班列导入不会误报其它班列。
- 不写库、不自动置退舱，仅告警交人工处理（洋拍板）。
- `active` 在 :493 已定义（`[r for r in recs if not r["deleted"]]`），但注意 `recs` 已被 D 改为「排除全体终态」→ `active` 天然不含终态，符合预期（不会对自己退舱的记录报"源端缺失"）。

---

## 验收标准（芙蕾雅验收用）

1. **方案 A 拦截**：构造库内 1 条 `状态=退舱`（core=X，无同 core active）+ 源 `parsed_rows` 含同 core X 的 active 行 → `build_diff` → 断言 `imports` 中**不含** X、`alerts` 含 1 条 `type="终态重现"`、原退舱记录逐字段未动（不写库）。
2. **方案 A 不影响正常新纪录**：库内无 X（无退舱无 active）+ 源含 X → 正常进 `imports`（陌生客编放行，专列/散舱阈值逻辑不变）。
3. **方案 A 不影响更新**：库内 1 条 active（core=X）+ 源含 X → 正常走 `updates`（匹配/字段差异），退舱逻辑不介入。
4. **方案 D 延期覆盖**：`TERMINAL_STATUSES` 含 `延期`；库内 1 条 `状态=延期`（core=Z）+ 源含 Z → 同验收 1 拦截（不进 imports、告警 `type="终态重现"`、status 显示"延期"）。
5. **方案 C 反向**：库内 1 条 active（train=T, core=X）+ 源只含 train=T 但 core=Y（不含 X）→ `alerts` 含 1 条 `type="源端缺失建议退舱"` for X。
6. **方案 C 部分导入保护**：库内 1 条 active（train=U, core=X）+ 源**完全不含** train U → **不**报"源端缺失"（避免部分导入误报）。
7. **回归**：主 spec 既有逻辑（专列阈值、箱属、后缀变更、字段冲突确认、退舱剔除旧义中的"不膨胀阈值"）不受影响；全套件（含历史日志加列测试）仍全绿。
8. **⚠️ 验收 11 改写（关键）**：现有 `mailbots_next/tests/test_manifest_spec.py` 验收 11（约 :77）原断言「退舱客编视同不存在 → 走陌生客编放行进 imports」**正是本 spec 要修的 bug 行为**。改 build_diff 时**必须同步改写该测试**：
   - 原：`seed 退舱(core) + 源含同 core → 断言进 imports`。
   - 新：`seed 退舱(core) + 源含同 core → 断言**不进 imports**、alerts 含 `type="终态重现"`、原退舱记录未动`。
   - 另一条验收 11（:86，41 客编 5 退舱 → 有效 36+1=37 ≤40 散舱、退舱不膨胀阈值）**保持不变**（D 后该语义仍成立）。
9. **新增测试**：补 方案 C 的验收 5/6 两条用例（临时库，勿碰真库）。

---

## 不在范围

- 前端交互改造（洋选 A 非 B：告警进现有 alerts 区，人工在前端处理，无新确认框）。
- `apply_diff` 写入逻辑改动。
- `mailbots*` 邮件机器人解析范围（独立系统，`状态<>'退舱'` 不动）。
- 自动置退舱 / 自动删除（C 只告警，不自动改）。
- `AGENTS.md` §6.1 措辞细化（可选：可在 spec 落地后补一句"退舱不参与匹配但参与终态重现告警"，非阻塞）。

---

## 实现顺序（依赖）

D → A → C。D 先把终态集合化，A/C 都基于 `TERMINAL_STATUSES` 与 `by_core_term`。同 PR 内按此顺序提交或合并提交均可。
