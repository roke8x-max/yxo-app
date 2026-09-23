# 命名清理 spec（OpenCode）：草单分类死值 + `WAY_B` tier 改名

> 交办：芙蕾雅 ｜ 日期：2026-09-23 ｜ 执行：OpenCode ｜ 验收：芙蕾雅
> **洋 2026-09-23 拍板**：范围扩大到**草单分类代号**；**分两个动作**（动作 1 零风险先做、动作 2 先查副作用）；**排在第三批之后、单独一批**。
> **洋 2026-09-23 第二轮拍板**：① `W`/`OTHER` 分支**连一起删，不留退路**；② 走**保守方案**（枚举 + **值不变**）；③ 三处硬编码收敛**采纳**；④ **`DRAFT_CATEGORIES` 删掉**，改名为 `NON_AUTO_DRAFT_CATEGORIES = ("C2",)`（保留原名会造成"名字说所有分类、实际只剩 C2"的新误解）；⑤ **术语映射表落 `docs/`、不进 `AGENTS.md`**；⑥ `WAY_A` 在新系统不存在 ⇒ WAY 侧实际只有 `WAY_B` 一处 tier。
> **一个分支、两个 commit**（动作 1 / 动作 2），便于单独 revert。
> 前置调查全文：`docs/2026-09-23-命名清理(tier代号)-前置调查与小spec.md`（本 spec **取代**其中 §3 的小 spec；该文档的 §1 调查结论与 §4 映射表**仍然有效**）。
> **开工基线**：**第三批（`WAY_B` 改为"识别 + 静默跳过"）已落在工作区**（能看到 `serve.py` 的 `WAY_B skipped by design` 日志文案、`send_waybill_rejected` 已删）。本批就在它之上继续改，**不要回退第三批的任何内容**。

---

## 0. 交付纪律（四条铁律，先读）

1. **不瞎改已做好的内容** —— 只改本 spec 点到的位置；别的"顺手能改的"写进报告不要动。
2. **不要 commit、不要 push、不要建分支**（分支由洋建）。**改完停在工作区**；若已提交不要 revert，在报告里说明。
3. **做完先自测**：跑测试 + 真锁往返，**原始输出贴进报告**。
4. **如实报告**：偏差、不确定处、失败都写出来；**不要替人拍板**。

---

## 1. 两个动作的顺序与边界

| 动作 | 内容 | 风险 | 顺序 |
|---|---|---|---|
| **动作 1** | 删**死值**（`B`/`W`/`OTHER` **及其分支**）+ **删 `DRAFT_CATEGORIES`** + 消掉三处硬编码重复 + 删死条件 | 零（都不再产生） | **先做** |
| **动作 2** | **保守方案**：加 `DraftCategory` 枚举（**字符串值一律不变**）+ `WAY_B` tier → `waybill_rejected` | 低 | 后做 |

⛔ **本批不做**：老系统 `mailbots/`（还在生产跑）、历史归档文档（`docs/superpowers/**`、老系统 testset、历史日志）、`T0`–`T7` 等其它 tier（另立一项）。

✅ **本批顺带项（洋 2026-09-23 定；1 处，属"名字与实际不符"的同类问题）**：
`mailbots_next/tests/test_waybill_rejected.py` 的用例 ④（`test_rejected_silent_even_when_unroutable`）
里 `patch("mailbots_next.serve.route_row", ...)` 已成**死桩** —— 静默早分流位于 `route_row`
**之前** ⇒ 该 patch 在当前代码下**永不生效**，两条断言变成平凡为真。
⚠️ **它不是空测试**（撤掉早分流时它就会挂，仍有区分力），**只是名字/说明的前提已不存在**。
⇒ 本批**只改名字与注释**（如 `test_rejected_silent_regardless_of_routing`）或删掉那个 patch，
**不要改任何断言、不要动 `serve.py` 的早分流逻辑**（那一批刚验收过）。

---

## 2. 动作 1：删死值 + 消重复

### 2.1 我核实的前提（**结论：三个值都已死，但 `W` 要删得干净得连分支一起删**）

| 值 | 核实结论 | 证据 |
|---|---|---|
| `B` | ✅ **已死**（9-21 起恒判 A） | `draft.py` 里 `category = "A"` 恒定；全仓无 `="B"` 赋值 |
| `W` | ⚠️ **经验上已死，但静态仍有一条窄路径** | **经验**：344 封真实样本中 `W` = **0**；生产老库 `category` 取值分布 = `A/C2/C1/B/空`，**无 `W`**；全部 67 封"主题含运单号"的样本**都来自 `docwbfb@`** ⇒ 被 WAYBILL 类型截走 ✓<br>**静态**：`W` 的产生条件是"**被类型判为 DRAFT**（附件名含 `已加密`/`箱号` 且发件人不在排除表，这是 `TYPE_ROUTES` 里的**宽松子串**规则）**且** 主题含 `运单号` **且** 不匹配严格草单附件正则" ⇒ **仍可达**（例如客户回一封带 `…箱号.xlsx` 的邮件、主题写了"运单号"） |
| `OTHER` | ✅ **永不产生** | `draft.py` 只会给 `category` 赋 `A`/`W`/`C1`/`C2` 四个值之一 |

⇒ **因此**：只删白名单条目、保留 `W` 分支，会造出一种新的不一致 ——「**代码能产出白名单里不存在的值**」（正好是"名字承诺了不存在的事"的反面版本）。
✅ **洋 2026-09-23 拍板：`W` 分支与 `OTHER` 一起删，不留退路。** 下面的改动**按"分支与清单条目一起清"执行**，不要再保留"W 作兜底"的写法。

### 2.2 改动

**① `mailbots_next/core/extractors/draft.py`**

- 删掉 `elif "运单号" in subject: category = "W"` 整段；`else` 分支的 `C1`/`C2` 判定保持不变。
- 第 41 行现在这句：

  ```python
  if category in ("C2", "W", "OTHER") or category not in DRAFT_CATEGORIES:
  ```

  **简化**为单一常量判定（见 ②）。⚠️ **删 `W`/`OTHER` 后，原来那个 `or category not in DRAFT_CATEGORIES` 子句恒为 `False`**（`A`/`C1`/`C2` 都在表里）⇒ **该子句是死条件，一并删掉**。

**② `mailbots_next/config/types.py`：收敛三处硬编码 + **删掉 `DRAFT_CATEGORIES`****

现在同一个集合在**三个地方各写了一遍**：`draft.py:41` 的 `("C2","W","OTHER")`、`decide.py:34` 的 `("C2","W","OTHER")`、`serve.py:301` 的 `("C2","W","OTHER")`。

🔴 **`DRAFT_CATEGORIES` 本批删除，且不要用什么别名把它留下来。** 两条理由：
1. 删掉 §① 那个死条件后，它**零消费者**（现值为 `["A", "B", "C1", "C2", "OTHER"]`，**`W` 从来就不在其中**）；
2. **留着这个名字会造出一种新的误解**：它字面意思是"草单的**所有**分类"，而删完死值后实际只剩 `C2` 一档在用 —— 这正是本批要治的那类病（名字承诺了不存在的事）。

改成（**删 `DRAFT_CATEGORIES`、新增下面这一个**）：

```python
# 需要"留人工"的草单分类：不自动转发、不进错误队列、不标已读。
# 2026-09-23 收敛：原 `DRAFT_CATEGORIES = ["A","B","C1","C2","OTHER"]` 已删
# （删掉死值后零消费者，且名字会让人误以为它是"所有分类"）；
# 原三处各写一遍的 ("C2","W","OTHER") 收敛到这里。
NON_AUTO_DRAFT_CATEGORIES = ("C2",)
```

- 同步 `config/__init__.py` 的 import 与 `__all__`：**删 `DRAFT_CATEGORIES`、加 `NON_AUTO_DRAFT_CATEGORIES`**。

**③ `decide.py:34` / `serve.py:301`**：`in ("C2","W","OTHER")` ⇒ `in NON_AUTO_DRAFT_CATEGORIES`；`serve.py:304` 的日志文案保持不变。

**④ `AGENTS.md:100`**：分类描述现在写的是「`A原始 / C1问题 / C2确认 / W运单号` 分类」⇒ **去掉 `W运单号`**（改为 `A原始 / C1问题 / C2确认`），并与已有的「B『更新草单』已移除」那句保持一致口径。

**⑤ 必须改动的既有测试（两处，我逐个读过原文）**

- 🔴 **`mailbots_next/tests/test_p1_coverage.py:626`** —— 用例 `test_manual_rows_exempt_from_mark_seen` 里：
  ```python
  for i, cat in enumerate(("C2", "W", "OTHER"))     # ← 删掉 W/OTHER 后会造出"白名单外"的分类值
  ```
  ⇒ 改成 `for i, cat in enumerate(("C2",))`，**docstring 同步**（现在写的是「C2/W/OTHER 留人工行」）。**用例本身保留**（它锁的是"留人工行不标已读"，仍然有效）。
- 🔴 **`mailbots_next/tests/test_core.py:1057-1062`** —— 用例 `test_draft_OTHER_unknown_category_skips`：它手工构造 `category="OTHER"` 的行、断言 `("MANUAL","skip")`。
  ⇒ **本批删除该用例**（`OTHER` 已不可能产生，这条状态不可达），并**在报告里单独列出这次删除 + 原因**。
  > ⚠️ 这是本动作**唯一一处删测试**，所以要求显式申报。若你认为应当保留一道"**未知分类不进自动流程**"的防御（那会**改变 `decide.py` 的行为**、超出本批范围）⇒ **不要自作主张实现**，写进报告请示即可。
  > 参考：`("MANUAL","skip")` 这条行为**仍由 C2 的用例覆盖**（`test_core.py:1048-1055` 断言 `log_manual_category` 收到 `"C2"`），所以删掉这条不会造成覆盖缺口。

### 2.3 验收判据 + 真锁

**判据**

⚠️ **下面几条 grep 都是我亲手跑过的口径**。原稿前两条写错了（一条**永假**、一条**恒真**），已按实测修正 —— **不要"优化"回更宽/更窄的模式**（这两类错法我们这一批已经反复踩过）。

1. 用例全绿；新增用例：**"主题含运单号 + 非白名单发件人 + 附件名含『箱号』" ⇒ `draft_category` 落 `C1`/`C2`（不再有 `W`）**；
2. `grep -rnw 'DRAFT_CATEGORIES' mailbots_next/ --include=*.py` = **0**。
   🔴 **必须带 `-w`**：新常量 `NON_AUTO_DRAFT_CATEGORIES` **含有子串** `DRAFT_CATEGORIES`，不用 `-w` 就永远命中 1 条（我实测过：普通 grep = 2、`-w` = 1）
3. 分类字面量的**精确**判据：
   - `grep -rnE 'category\s*=\s*"W"|"C2",\s*"W"|"W",\s*"OTHER"' mailbots_next/ --include=*.py` = **0**；
   - `grep -rn '"W"' mailbots_next/ --include=*.py` 的命中**只允许**剩下面这 2 处 —— 它们是 `unicodedata.east_asian_width` 的**显示宽度**取值，**与草单分类无关、不许改**：
     - `core/act.py:206`、`tests/test_forward_layer.py:25`（均为 `east_asian_width(ch) in ("W", "F")`）
   > 🔴 原稿写的是「`grep '"W"'` = **0**」—— **那条永远不可能成立**（被上面 2 处合法命中挡住）。
4. `NON_AUTO_DRAFT_CATEGORIES` 在全仓**只允许这 6 处**：`config/types.py` 定义 1 处、`config/__init__.py` 2 处（import + `__all__`）、`core/extractors/draft.py` / `core/decide.py` / `serve.py` 各 1 处使用；且**不得再有内联元组**（已由第 3 条锁住）。

**真锁**
- 把 `draft.py` 的 `W` 分支**加回去** ⇒ 新增用例 **必挂**；
- 把 `NON_AUTO_DRAFT_CATEGORIES` 里的 `("C2",)` 改成 `()` ⇒ **至少 1 条挂**（证明它真的在被用）。
> 撤改要**把行首缩进一起纳入匹配**；改完**先 `ast.parse()` 自检**再跑测试（我们踩过两次）。

---

## 3. 动作 2：保守方案（枚举 + **值不变**）+ `WAY_B` 改名

### 3.1 前置调查结论（决定走保守方案）

| 三问 | 结论 |
|---|---|
| **① DB 里有没有历史字符串** | **有（在老系统）**：`draft_forward_ledger.processed_mails.category` **271 行** = `A 121 / C2 61 / C1 51 / B 36 / 空 2`；`pending_queue.category` **11 行** = `B 4 / A 4 / C1 3`。**新系统：草单分类与 tier 都不入 DB**（`error_queue` 无 category 列、`forward_log` 无 tier/category 列） |
| **② 巡检/脚本按字符串匹配** | **没有**：新系统 `scripts/` 零引用；给小叽的执行清单 §8 的 `Select-String` 不含这些值；老系统 `daily_forward_report.py`、`Database_Syncer.py` = 0。唯一按值判定的是**老系统自己**（`draft_pending.py:601` `in ("A","B")`）——**本批不动** |
| **③ 文档引用清单** | **要改**：我写的 4 份 spec/报告 + `AGENTS.md:100`。**不改**：`docs/superpowers/**`、老系统 testset、历史日志 |

⇒ **判定：改字符串会造成「新旧词汇不一致」的对照摩擦（老库 282 行历史值 + 老系统展示层 `CATEGORY_LABEL` 也按老值写），但不造成技术故障**（新系统既不写也不读这些值）。
⇒ **按洋的预设走保守方案：加枚举、值不变。**

### 3.2 改动

**① `config/types.py` 新增 `DraftCategory`（用 `StrEnum`，不是 `(str, Enum)`）**

```python
class DraftCategory(StrEnum):
    """草单分类。⚠️ 值一律保持历史字符串（A/B/C1/C2/OTHER），不要改值 ——
    老系统台账 (draft_forward_ledger.processed_mails.category 271 行) 与
    pending_queue.category (11 行) 里存的就是这些字符串，改值会造成跨系统对照困难。"""
    NEW = "A"                     # 新草单（原 A）
    UPDATE = "B"                  # ⚠️ 历史值：新系统不再产生（恒判 A），保留仅为识别老库历史数据
    UPSTREAM_FEEDBACK = "C1"      # 上游问题反馈（原 C1）
    EXTERNAL_REPLY = "C2"         # 外部回复/确认（原 C2）
    OTHER = "OTHER"               # ⚠️ 历史值：新系统不再产生
```

🔴 **为什么必须是 `StrEnum` 而不是 `(str, Enum)`** —— 我实测了两种写法在 Python 3.13 下的差别（不是推测）：

| 表达式 | `class X(str, Enum)` | `class X(StrEnum)` |
|---|---|---|
| `f"{X.NEW}"` / `str()` / `format()` / `"%s" %` | **`'X.NEW'`** 🔴 | `'A'` ✅ |
| `.value` | `'A'` | `'A'` |
| `== "A"` / `hash == hash("A")` / `{"A":1}[X.NEW]` / `json.dumps` / `in ("C2","A")` | 全部相同 ✅ | 全部相同 ✅ |

**本仓有 3 处 f-string 会直接打印分类值**，用 `(str, Enum)` 会让日志文案悄悄变成 `DraftCategory.EXTERNAL_REPLY`：
`core/decide.py:35`、`core/serve.py:304`、`core/log.py:150`（`log_manual_category` 内部）。
⇒ 用 `StrEnum` 后这 3 处**一个字都不用改**；若坚持 `(str, Enum)`，就必须在这 3 处（以及**以后每一处新加的**）写 `.value` —— 那是给后人埋雷，不采用。

⚠️ **两条必读的前置事实（我已核实）**：
- `StrEnum` 需要 **Python 3.11+**。**生产部署 venv `D:\YXO_DATA\yxo_app\venv` 实测 = 3.13.14** ✓、本机唯一解释器 3.13.14 ✓、网页端 `app.py` **不 import `mailbots_next`**（0 命中）⇒ 无兼容风险。
- 🔴 **但 `mailbots_next/README.md:54` 现在写的是「Python 3.10+」** ⇒ 采纳 `StrEnum` 后**必须同步改成「Python 3.11+」**（见 ④）。否则就是"文档允许 3.10、代码却用了 3.11+ 特性"——正是本批要治的那类不一致。
- 记住 `from enum import StrEnum`（`config/types.py:2` 现在是 `from enum import Enum`，需一并调整）。

> 关于 `UPDATE`/`OTHER` 这两个"历史值"成员：**本批保留**（它们让这个枚举成为一份**完整的分类码本**，替代老系统的 `CATEGORY_LABEL`；且 §3.3 的不变性断言正好逐条覆盖这 5 个值）。**不要新增 `W` 成员** —— `W` 从来不在 `DRAFT_CATEGORIES` 里、也从未进过任何库（老库取值只有 `A/C2/C1/B/空`），给它一个成员等于凭空造一个"存在的分类"。
> ⚠️ 若你（交付方）认为该只留新系统真正产生的 3 个（`NEW`/`UPSTREAM_FEEDBACK`/`EXTERNAL_REPLY`），**不要自行改**，在报告里写请示。

**② 代码改用成员**（`StrEnum` 本身就是 `str` 子类 ⇒ 与既有字符串比较天然兼容，**不需要大改**）：
- `draft.py` 的赋值：`category = DraftCategory.NEW` 等；
- `decide.py:34`、`serve.py:301` 的判定：用 `NON_AUTO_DRAFT_CATEGORIES`（动作 1 已收敛）；
- `ExtractedRow.draft_category` 的**类型标注保持 `Optional[str]`**（不做类型收紧，避免牵动面过大；成员是 `str` 子类，运行时兼容）；
- ✅ **三处日志/文案不用动**（`decide.py:35`、`serve.py:304`、`log.py:150`）—— `StrEnum` 的 f-string 天然输出 `A`/`C1`/`C2`，这正是选它的原因；
- 日志里可在值后面附人话，例如 `category=A(new_draft)`（可选，若做请保持格式统一）。

**③ `WAY_B` tier ⇒ `waybill_rejected`**（**这条字符串可以改**：新系统里 tier **不入 DB**，只进日志与测试；且新系统 grep `WAY_A` = 0）
- `core/decide.py` 守卫里的 tier、`core/serve.py` 早分流的 `log_decide` tier 与日志文案（`WAY_B skipped by design` ⇒ `waybill_rejected skipped by design`）；
- `waybill.py` 与 `decide.py` 的注释里的 `WAY_B` 字样 ⇒ 改描述性表述（**保留**"否则会命中 T1 转发给客户"这条警告）；
- `tests/test_waybill_rejected.py` 的 tier 断言与 docstring。

**④ 文档**
- 更新我写的 4 份"活文档"里指向**新系统**的 `WAY_B`；（涉及老系统代号的地方**保留原样**并注明"老系统代号"）
- 🔴 **`mailbots_next/README.md:54` 的「Python 3.10+」改成「Python 3.11+」** —— 采纳 `StrEnum` 的**必要配套**（3.11+ 才有 `enum.StrEnum`）。实测生产 venv 与开发机均为 3.13.14，改这一行即可；不改就成了"文档允许 3.10、代码用了 3.11+ 特性"。
- **术语映射表**（`WAY_A`/`WAY_B` 含义 + 新系统对应名 + 历史数据位置）：**落 `docs/`，不进 `AGENTS.md`** —— `AGENTS.md` 是规则手册，术语映射属文档。该表**由芙蕾雅维护**（内容已在 `docs/2026-09-23-命名清理(tier代号)-前置调查与小spec.md` §4），**不在你的改动范围内**；你只需保证"活文档里指向新系统的 `WAY_B`"与新代码一致。

### 3.3 验收判据 + 真锁

**判据**
1. 用例全绿；`grep -rn 'WAY_B' mailbots_next/ --include=*.py` = **0**（**不限带引号的** —— 注释、docstring、日志文案里也不许再出现；改名前它共 8 处命中：`decide.py`×2、`waybill.py`×1、`serve.py`×2、`tests/test_waybill_rejected.py`×3）；
2. **行为不变性断言（保守方案的命门）**：新增用例断言
   `DraftCategory.NEW.value == "A"`、`UPDATE.value == "B"`、`UPSTREAM_FEEDBACK.value == "C1"`、`EXTERNAL_REPLY.value == "C2"`、`OTHER.value == "OTHER"` —— **防止后人"顺手把值也改了"**；
3. ✅ **`StrEnum` 的打印语义也要断言**（这才是"选它"的实质）：新增/追加断言
   `f"{DraftCategory.NEW}" == "A"`、`str(DraftCategory.EXTERNAL_REPLY) == "C2"`
   ⇒ 若后人把 `StrEnum` 换回 `(str, Enum)`，这两条**必挂**（实测该写法会打印 `DraftCategory.NEW`）；
4. `grep -rn 'draft_category ==' mailbots_next/tests/` 的断言仍全绿（值没变的直接证据）；
5. 🔴 **`is` 比较检查（实测本仓今天 = 0 处，改完必须仍为 0）**：
   `grep -rnE '\bis\s+["\x27]' mailbots_next/ --include=*.py` = **0**。
   说明：枚举成员与**字符串字面量**之间**不存在 `is` 恒等**（实测两种写法 `m is "A"` 都是 `False`）⇒ 一旦有人写 `x is "C2"`，换成枚举后**永远为假且不报错**，属静默失效。本仓现在一处都没有，所以本批**不需要改任何 `is`**；这条是**改完后回归用**的判据，也是给未来加的一道闸。

**真锁**
- 把 `NEW = "A"` 改成 `"new_draft"` ⇒ **行为不变性用例必挂**；
- 把 `decide.py` 的 tier 改回 `"WAY_B"` ⇒ tier 断言用例**必挂**；
- （新）把 `StrEnum` 换回 `(str, Enum)` ⇒ 判据 3 的 f-string 断言**必挂**。

---

## 4. 报告格式

```
一、改动文件（按 动作1 / 动作2 两组分别列：文件 + 改了什么）
二、测试（基线 HEAD/用例数 → 现在用例数/汇总行原文/退出码；被改用例与新用例逐条列名）
三、真锁往返（动作 1 两条 + 动作 2 两条：撤改动作 → pytest 原始输出 → 还原后 md5）
四、grep 验收（§2.3 第 2/3 条 + §3.3 第 1 条，贴原始输出）
五、偏差与请示
六、不确定 / 我没做的事
```

---

## 5. 背景（供理解，不必回应）

- 这批的动机是**可读性**：`A`/`C1`/`C2`/`WAY_B` 光看名字不知道是什么；而**老系统的做法是"存代号、展示时翻译"**（`mailbots/draft_pending.py:67` 的 `CATEGORY_LABEL = {"A": "转草单", "B": "草单更新", "C1": "反馈问题", "WAY_A": "运单号下发", "WAY_B": "单证驳回"}`）——**新系统直接在源头把意图写清楚**，就不用再养一层翻译。
- 为什么不直接改字符串值：老系统台账里有 **282 行**历史值（`processed_mails.category` + `pending_queue.category`），且老系统**仍在生产运行**、其 `draft_pending.py:601` 还用 `in ("A", "B")` 做活逻辑判定 ⇒ 值保持一致，跨系统对照和人工排查都最省事。
- 为什么 `WAY_B` 可以改字符串：新系统里 tier **不进任何 DB**，只在日志/测试里出现，且新系统**根本没有 `WAY_A`**（`grep WAY_A mailbots_next/` = 0；带 `WAY_A` 的全是老系统）⇒ **Way 侧本批实际只改 `WAY_B` 一处 tier**，比原以为的"两个都改"小得多。
