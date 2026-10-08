# 转发层修复 spec（运单号正文表格 + 附件后缀口径 + 重写回退命名）—— 可独立落码

> 日期：2026-09-17 ｜ **v3.2**（新增 **§10：删除正文自撰前言**，洋 2026-09-17 拍板；`F1`~`F4` 已落码并验收通过，**§10 由芙蕾雅直接改**）｜ 编写：芙蕾雅 ｜ 交付对象：OpenCode
> **v3.2 变更（洋的决定）**：正文里那句 `本邮件仅包含贵公司相关的运单号信息：`（纯文本前言 + HTML 的 `<p>` 说明段）**是机器人自己加的，要删掉** —— 下游公司只需收到"按规则拆分出来的内容本身"。⇒ 详见 **§10**；**§2（F1 正文格式）里带该前言的片段已被 §10 取代**。
>
> **v3.1**（新增 **F4：深色模式兼容**，洋 2026-09-17 拍板"修"）
> **v3 修正（v2.2 落码验收时发现，错在 spec 不在代码）**：
> ① 🔴 **§4.1 第 10 条自相矛盾**：v2.2 同时要求"其中一行含中文"与"第 2 列**字符 index** 相等"，而中文的**字符数 ≠ 显示宽度** ⇒ **正确实现也会挂**。已改为"**中文必须放末列、前序列纯 ASCII**"，并附实测推演。（OpenCode 落码时自行绕过并说明了原因，处理正确。）
> ② 🔴 **§4.4 基线数字改掉**：v2.2 写的"基线 372"是**过时值**（实测改动前 **373**，期间 PR#12/#14 多次增删用例）⇒ 判据改为**相对量**"0 failed + 增量恰等于新增用例数"，**不引用会漂移的历史绝对值**。
>
> **v2.2 新增 §3.1**「`.xlsm` 丢宏」的**认可与登记**；**v2.1** 把 F3 断言改为精确等值；**v2** 采纳 ds 交付前反馈（F3 改名公式、F2 循环导入、F1 对齐算法）。
> **v2 相对 v1 的 4 处修正**：
> ① 🔴 **F3 的改名公式改掉**：v1 写的"沿用旧系统 `name[:-4]`"**是错的**（对 `.xlsx` 会得到 `report.xlsx.xlsx`、对 `.xlsm` 会得到 `report.xlsm.xlsx`）→ 改为 **`os.path.splitext`**，并附推演表；
> ② **F2 补"循环导入"结论**：已**实测**排除（`core/__init__.py` 先导 `.extractors` 后导 `.act`），并保留 3 秒冒烟命令与备选方案；
> ③ **F1 补纯文本对齐算法**：明确"**先全表算列宽、再逐行填充**，不要每行独立算宽"（否则各行错位），并把它变成一条**可测**的断言；
> ④ 测试从 18 条扩到 **21 条**：新增"对齐必须全表统一列宽"、"重写命名一律用**精确等值**断言"、"双后缀输入名（`x.xlsx.xlsx`）必须原名保留"三条硬锁；`<td>` 也补 `white-space: nowrap`。**v2.1** 追加：F3 的断言方式从"数 `.xlsx` 出现次数"改为 **`assert new_name == splitext(input_name)[0] + ".xlsx"` 精确等值**（ds 2026-09-17 指出计数法在双后缀输入名上会误挂，等值法既避免误挂又更强）。**v2.2** 追加：新增 **§3.1**——`.xlsm` 丢宏**明确认可**（"名字与字节一致"优先于"保住宏"），并**只登记不实现**：要求在 `mailbots_next/README.md` 技术债表加一行，写清"若客户真依赖宏，正确做法是整封原件不过滤地转发，且这属业务决策须问洋"。
>
> 洋的决定（2026-09-17）：① **旧系统不修**（不在仓库里、改动风险大、业务可接受，新系统上线后自然退役）② **正文表格走方案 A**（`multipart/alternative`）③ **附件后缀口径一并修**（"静默丢附件"是正确性问题，必须修）④ **本 spec 单独一份、单独 commit**，与收信通路那批（`docs/2026-09-16-收信通路修复spec(OpenCode)-方案A轮询.md`）**分开** ⑤ 生产运行目录纠正见 §5（同步进部署清单）。
>
> 🔴 **本 spec 只动「转发层」**（邮件怎么发出去：正文 + 附件）。**不要**顺手改收信层（`ingest.py` / 轮询 / UID 水位线 / IDLE 删除）——那是另一份 spec 的范围，两批分开提交。

---

## 0. 背景：这是什么问题（附实读证据）

### 0.1 正文表格"崩溃"（客户可见，当前就在发生）

只读实读 `maoxiaoyang@cqtransit.com` 的「已发送」，主题 `2026-09-17 YXO-2026-872 CQWLJT运单号`（uid=2951 / 2992 / 2869 三封同构）：

```
部件树：multipart/mixed
  ├─ [正文] text/plain  148~152 字节
  └─ [附件] .xls  5,632 字节（过滤版）

正文原文（逐字）：
  本邮件仅包含贵公司相关的运单号信息：

  客户编码 | 箱号 | 运单号
  --- | --- | ---
  CQWLJT260914003-XLYJN | FWRU0243394 | 38273225
```

🔴 **根因：把 Markdown 表格语法当成 `text/plain` 发出。** `--- | --- | ---` 是 Markdown 的**表格分隔行**，纯文本邮件不会渲染它 —— 客户看到的就是一堆竖线加破折号（"表格崩溃、文字还在"）。

**代码位置**：`mailbots_next/core/act.py:215-218`

```python
lines = ["本邮件仅包含贵公司相关的运单号信息：", "", "客户编码 | 箱号 | 运单号", "--- | --- | ---"]
for r in g["rows"]:
    lines.append(f"{r.get('客户编码') or '-'} | {r.get('箱号') or '-'} | {r.get('运单号') or '-'}")
msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))
```

> 生产旧系统 `MailBots\Waybill_Robot.py:609-619` 是同一个写法。**按洋的决定，旧系统不改**（此处仅作溯源，`MailBots\` 不是 git 仓、不属本仓库改动范围）。

### 0.2 附件只认 `.xls`（静默丢附件）

`mailbots_next/core/act.py:224`：

```python
if fn and fn.lower().endswith(".xls"):
```

而 extractor 侧的口径是（`core/extractors/waybill.py:10`）：

```python
XLS_SUFFIXES = (".xls", ".xlsx", ".xlsm")
```

⇒ **两侧口径不一致**。若上游改发 `.xlsx`/`.xlsm`：**抽取照旧成功（能读出箱号、能路由、能发信），但转发出去的信里一个附件都没有** —— 客户收到"正文列了箱号、却没有表格附件"的邮件。**这是静默丢数据，不是体验问题。**

### 0.3 🔴 `rewrite_xls_filtered` 回退时"字节格式与文件名不一致"（**新系统相对旧系统的回退**）

`core/act.py:270-358` 的结构是 `try: xlrd … except Exception: try: openpyxl …`：

| 路径 | 写出的字节 | 返回的文件名 |
|---|---|---|
| xlrd 成功 → **xlwt** 写 | **`.xls`（BIFF）** | 原 `name` |
| 回退 → **openpyxl** 写 | **`.xlsx`（ZIP）** | **原 `name`** ← 🔴 问题在这 |

`return buf.getvalue(), name`（`:356`）**把 xlsx 字节顶着原来的扩展名发出去**。后果：

- 输入是 `.xls`（或名字是 `.xls` 而内容其实是 xlsx —— 代码注释里明确提到过这种情况：`# Fallback: try openpyxl (in case .xls is actually xlsx)`）→ 回退路径产出 **xlsx 字节 + `.xls` 文件名** ⇒ 客户 Excel 打开时**弹"文件格式与扩展名不一致"警告**，严重的直接打不开。
- 输入是 `.xlsm` → openpyxl 只会写出普通 `.xlsx`（**宏会丢**），而文件名仍叫 `.xlsm` ⇒ 同一个不一致问题 + 宏静默丢失。

**旧系统处理过这件事**（`mailbots/Waybill_Robot.py:570-571`，8/14 那批修复里）：

```python
base = name[:-4] if (name or "").lower().endswith(".xls") else name
return buf.getvalue(), base + ".xlsx"      # ← 回退时把扩展名改成 .xlsx
```

⇒ **新系统把这段丢了。** 这是回退，必须补回。

---

## 1. F1【P0 · 客户可见】正文改 `multipart/alternative`（方案 A）

**目标**：客户在**任何**客户端（富文本 / 纯文本）看到的都是一张像样的表。

**做法**：把 `split_waybill_by_company()`（`act.py:191-…`）里 `msg.attach(MIMEText(..., "plain", "utf-8"))` 一处，换成 `multipart/alternative` 容器 + 两个部件：

```
MIMEMultipart("mixed")                     ← 外层不变（继续挂附件）
  └─ MIMEMultipart("alternative")
       ├─ MIMEText(text_plain, "plain", "utf-8")     ← 对齐的纯文本（不是 markdown）
       └─ MIMEText(html, "html", "utf-8")            ← 真 <table>
```

> ⚠️ 顺序：`alternative` 里**纯文本在前、HTML 在后**（RFC 2046 约定：越靠后越"富"）。

### 1.1 `text/html` 部件的硬要求

- 用**真正的 `<table>`**（`<tr>`/`<th>`/`<td>`），不要用 `<div>` 拼。
- **样式全部内联**（`style="..."` 写在元素上）。**禁止依赖 `<style>` 块 / class** —— 多数邮件客户端（含阿里邮箱 Web、Outlook）会剥掉 `<style>`，剥完就散架。
- 建议骨架（可微调，但要素不能少）：

```html
<div style="font-family: Tahoma, Arial, '微软雅黑', SimSun; font-size: 13px; color: #000;">
  <p style="margin: 0 0 10px 0;">本邮件仅包含贵公司相关的运单号信息：</p>
  <table style="border-collapse: collapse; font-size: 13px;">
    <thead>
      <tr>
        <th style="border: 1px solid #999; padding: 4px 10px; background: #f2f2f2; text-align: left; white-space: nowrap;">客户编码</th>
        <th style="border: 1px solid #999; padding: 4px 10px; background: #f2f2f2; text-align: left; white-space: nowrap;">箱号</th>
        <th style="border: 1px solid #999; padding: 4px 10px; background: #f2f2f2; text-align: left; white-space: nowrap;">运单号</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td style="border: 1px solid #999; padding: 4px 10px; white-space: nowrap;">CQWLJT260914003-XLYJN</td>
        <td style="border: 1px solid #999; padding: 4px 10px; white-space: nowrap;">FWRU0243394</td>
        <td style="border: 1px solid #999; padding: 4px 10px; white-space: nowrap;">38273225</td>
      </tr>
    </tbody>
  </table>
</div>
```

> `<th>` 与 `<td>` **都加 `white-space: nowrap`** —— 保证"一行一箱号"不折行（值都短，不加也不会崩，但加上更稳）。

- **必须做 HTML 转义**：单元格值走 `html.escape(str(v))`（客户编码/箱号/运单号来自 Excel，理论上可能含 `&`/`<`）。

### 1.2 `text/plain` 部件的硬要求

- 🔴 **禁止出现 Markdown 表格语法**：不得输出 `--- | --- | ---`，也不要用 `|` 当分隔符。
- **要真的对齐**（等宽字体下看起来就是表）。**对齐算法必须按下面三步做，不要每行独立算宽度**（那样各行会错位）：

  1. **全表扫一遍，先算列宽**：对每一列，取**所有单元格**（含表头）显示宽度的最大值。
     显示宽度：`中文/全角字符 = 2`，`ASCII/半角 = 1`（可用 `unicodedata.east_asian_width(ch) in ("W", "F")` 判定）。
  2. **再逐行填充**：按第 1 步算出的**固定列宽**左对齐补空格（不足补空、超出不截断——超出说明第 1 步算错了）。
  3. **列间固定 2 个空格**；表头也用同一套列宽（保证列对齐）。
- 基线形态（列宽按实际数据算，下面是示意）：

```
本邮件仅包含贵公司相关的运单号信息（详见附件）：

客户编码                箱号          运单号
CQWLJT260914003-XLYJN   FWRU0243394   38273225
```

- 空值仍用 `-` 占位（保持与现状一致）。
- 建议把这段做成**独立小函数**（如 `_format_plain_table(headers, rows) -> str`），这样测试可以单独断言它 —— 比在整封邮件里断言好测。

### 1.3 不要做的事

- ❌ 不要给正文加"更新/作废/【草单更新】"之类字样（那是草单线的事，且新系统当前**有意**不标）。
- ❌ 不要改主题（`msg["Subject"]` 保持不变）。
- ❌ 不要改分组逻辑（`groups` / `company_routes` / 收件人来源一律不动）。

---

## 2. F2【P0 · 静默丢附件】附件后缀口径对齐

**要求**：把 `act.py:224` 的 `.xls` 判断改为与 extractor **同一个常量**，**单一来源**，避免以后再漂移。

```python
# 从 extractor 侧导入，不要在 act.py 里再写一份字面量
from mailbots_next.core.extractors.waybill import XLS_SUFFIXES   # (".xls", ".xlsx", ".xlsm")
...
if fn and fn.lower().endswith(XLS_SUFFIXES):
```

**✅ 循环导入风险已实测排除**（ds 2026-09-17 提出，芙蕾雅实跑验证）：`core/__init__.py` 里 `.extractors`（含 `waybill`）在**第 ~46 行**导入，而 `.act` 在**第 58 行** —— 也就是说 `act.py` 执行到那行 import 时，`waybill` 已经完整加载在 `sys.modules` 里，**不成环**。三条冒烟实测均通过：

```
python -c "import mailbots_next.serve; import mailbots_next.core.ingest"      → OK
python -c "from mailbots_next.core.act import XLS_SUFFIXES"                    → OK ('.xls','.xlsx','.xlsm')
python -c "import mailbots_next.core.extractors.waybill; import mailbots_next.core.act as a; print(a.XLS_SUFFIXES)"   → OK
```

**但落码后请仍按下面这条自查一次**（万一将来导入顺序被人改动，这条 3 秒就能抓到）：

```powershell
python -c "import mailbots_next.serve; from mailbots_next.core.act import XLS_SUFFIXES; print('import ok', XLS_SUFFIXES)"
```

> 若报 `ImportError` / `cannot import name`，**改用替代方案**：把 `XLS_SUFFIXES` 提到 `mailbots_next/config/types.py`（或新建 `core/consts.py`），**`act.py` 与 `extractors/waybill.py` 两处都从那里导入**。
> **不接受**在 `act.py` 里再写一份 `(".xls", ".xlsx", ".xlsm")` 字面量——本次问题就是两份口径各写各的造成的。
> 报告里请写明你选了哪种、以及为什么。

**同时**：`act.py:224` 那段是 `for part in original_msg.walk(): … break`（只处理**第一个**匹配附件）。加了后缀后仍保持"只取第一个"的语义**不变**（不要顺手改成循环多个，那是另一个话题）。

---

## 3. F3【P0 · 补回回退】重写后的**字节格式必须与返回的文件名一致**

**要求**：`rewrite_xls_filtered()` 的返回值必须满足不变量：

> **返回的 `name` 的扩展名，必须与返回字节的真实格式一致。**

实现口径（按输入后缀决定输出格式）：

| 输入后缀 | 写出方式 | 返回的 `name` |
|---|---|---|
| `.xls` | xlwt → `.xls` | 原 `name`（不变） |
| `.xlsx` | openpyxl → `.xlsx` | 原 `name`（不变） |
| `.xlsm` | openpyxl → 只会产出 `.xlsx`（**宏不保留**） | **改名成 `.xlsx`**（用 `splitext` 去掉原后缀再拼，见下方 🔴） ，并 `WARN` 一行说明宏未保留 |
| `.xls` 走**回退**（xlrd 失败） | openpyxl → `.xlsx` | **改名成 `.xlsx`**（同上，用 `splitext`） |

- 🔴 **「改名」必须用 `os.path.splitext`，不要用旧系统的 `name[:-4]` 写法**（ds 2026-09-17 指出，已推演验证）：

  ```python
  base, _ = os.path.splitext(name)          # 正确：三种后缀都能切
  return buf.getvalue(), base + ".xlsx"
  ```

  为什么旧写法不能照抄 —— 旧系统的输入**只有 `.xls` 一种**，`[:-4]` 恰好够用；新系统要覆盖三种后缀，`[:-4]` 会失效：

  | 输入 | `name.lower().endswith(".xls")` | `name[:-4]` 写法得到 | 结果 |
  |---|---|---|---|
  | `report.xls` | True | `report` + `.xlsx` = `report.xlsx` | ✅ |
  | `report.xlsx` | **False**（结尾是 `.xlsx`） | `report.xlsx` + `.xlsx` = **`report.xlsx.xlsx`** | ❌ |
  | `report.xlsm` | **False**（结尾是 `.xlsm`） | `report.xlsm` + `.xlsx` = **`report.xlsm.xlsx`** | ❌ |

  ⇒ 用 `[:-4]` 会让 `.xlsx` 与 `.xlsm` 两条路径都产出畸形文件名，**与上表的 F3 要求自相矛盾**。**一律 `os.path.splitext`。**

  > ✅ **一个有用的性质（已推演验证）：`os.path.splitext(name)[0] + ".xlsx"` 对 `.xlsx` 输入是幂等的** ——
  > `x.xlsx → x.xlsx`；连双后缀名 `x.xlsx.xlsx → x.xlsx.xlsx`（**保持原名不变**，正确）。
  > ⇒ 所以代码**可以无条件套用这个公式**（不必先判断"要不要改名"），两种写法都不会错。
  >
  > 反例对照（帮助理解旧写法为什么越改越糟）：
  > `x.xlsx` → `x.xlsx.xlsx`；`x.xlsx.xlsx` → `x.xlsx.xlsx.xlsx`。

- **调用方不受影响**：`act.py:227-232` 已经把 `rewrite_xls_filtered` 返回的 `new_name` 用于 `Content-Disposition filename=`，所以**只要这里返回对的名字，附件名就自动正确**。
- ⚠️ **`except Exception:` 的裸兜底**：现状 `:315` 与 `:357` 两处都是**静默 `return None, None`**。本次不要求改造错误处理结构，但**必须**保证：重写失败时**不抛异常打断发信**（现有行为），且**回退链上任何一次"写成 xlsx 字节"都必然走到改名分支**（别只改一条路径漏另一条）。

**为什么这条必须做**：`.xls` 走回退是**真实可能发生**的——`extractors/waybill.py:95` 的注释就写着 `# Fallback: try openpyxl (in case .xls is actually xlsx)`。上游发个"扩展名 .xls、内容其实是 xlsx"的文件，就会踩中。

### 3.1 关于"`.xlsm` 丢宏"——**认可，且不预留任何代码**（洋 + ds 2026-09-17 确认）

**结论：「改名 `.xlsx` + WARN」是当前约束下的最优解。** 理由三条，从硬到软：

1. 🔴 **"名字与字节一致"是更高优先级的不变量。** 若为了保宏而保留 `.xlsm` 扩展名、实际却写的是**无宏**的 xlsx 字节，客户打开会看到"**扩展名与内容不符**"的警告 —— **这比丢宏更糟**（丢宏是静默的、且这个业务链路本来就不用宏；格式警告是显式的、且会让客户怀疑文件被改坏）。
2. **代码路径上宏必然丢，且没有退路。** `rewrite_xls_filtered()` 的目的就是"**过滤出本公司的行、重写成干净表格**"，这个"重写"本身即放弃原工作簿的**全部结构**（宏、图表、条件格式）。openpyxl 的 `keep_vba=True` **只对"`.xlsm` 读进来、再存回 `.xlsm`"有效**；我们是"存成 `.xlsx`"，用不上。
3. **业务场景里根本没有宏。** 上游发的是运单号**数据表**（`…CQWLJT运单号.xls`），宏工作簿不是这个链路的产物。真出现 `.xlsm`，大概率是同事用 Excel 另存时**误选了格式** —— 宏不是有意携带的。

**🔴 登记要求（不实现代码，只写文档）**：请在 `mailbots_next/README.md` 的"技术债（登记，未实现）"表里**新增一行**。该表现有 4 列（`编号 | 内容 | 为什么现在不做 | 将来什么条件下再做`），照格式填：

| 列 | 填什么 |
|---|---|
| **编号** | `X1`（或按现有风格起编号；现有的是 `F5-1`/`F5-2`/`G4`） |
| **内容** | `rewrite_xls_filtered()` 重写时**丢弃 .xlsm 的宏**（改名为 `.xlsx` 并打 WARN） |
| **为什么现在不做** | ① **"名字与字节一致"优先级更高**——保 `.xlsm` 扩展名却写**无宏**字节，客户会看到"格式与扩展名不符"警告，**比丢宏更糟**；② 该函数本就"**过滤出本公司行、重写成干净表格**"，**必然放弃原工作簿全部结构**（宏/图表/条件格式）；openpyxl 的 `keep_vba=True` **只在"`.xlsm` 读入再存回 `.xlsm`"时有效**，我们存的是 `.xlsx`，用不上；③ 业务链路里上游发的是**运单号数据表**，**根本不含宏**；真出现 `.xlsm` 大概率是同事另存时误选格式 |
| **将来什么条件下再做** | **不是"再做"的问题** —— 若真出现客户依赖宏：正确做法是**整封原件不带过滤地转发**（放弃"只保留本公司行"），**而非"保留宏 + 改内容"**。**这是业务决策，遇到时停下来问洋**；代码层**不预留任何开关** |

> 请注意措辞：这条**不是 TODO**，是**已知且已接受的取舍**。表里 G4 那行就是这个风格，照它写。（表末那句"本节仅登记，不写代码、不加配置、不加用例"仍然适用。）

---

## 4. 测试要求（`mailbots_next/tests/`）

新建 `test_forward_layer.py`（**不要改现有测试文件**；若确实需要共用夹具，用新文件内的局部 helper）。

**4.1 正文结构（F1）**
1. `split_waybill_by_company()` 产出的 msg：顶层是 `multipart/mixed`，且**含一个 `multipart/alternative` 子容器**。
2. `alternative` 里**恰好两个**正文部件，顺序为 `text/plain` → `text/html`。
3. 🔴 **`text/plain` 部件里不含 `"--- | ---"`** —— 这是本 bug 的**硬回归锁**（改前必挂，因为那行就是被打出来的 markdown 分隔行）。
4. 🔴 **`text/plain` 里竖线 `"|"` 出现次数为 0**（新格式完全不用竖线当分隔符）。
   > 若担心"将来某条数据本身含竖线"导致误挂：把这条改成"**除数据字段自身携带的竖线外**，不含作为分隔符的竖线"也可以；但**第 3 条的硬锁不能放宽**。真遇到数据含竖线，**停下来报告**（那说明纯文本对齐方案要重新评估），**不要**直接把断言删掉让它变绿。
5. `text/html` 部件里含 `<table`、`<tr`、`<th`，且**至少 3 个 `<th>`**（表头三列）。
6. `text/html` 里**不含 `<style`**、**不含 `class=`**（断言"样式全内联"这条约束）。
7. 行数据正确：N 行 `rows` ⇒ HTML 里 `<tbody>` 下恰 N 个 `<tr>`，且每行三个 `<td>` 值等于 `客户编码/箱号/运单号`。
8. **转义**：构造一条 `箱号 = "A&<B>"` 的数据，断言 HTML 里出现 `A&amp;&lt;B&gt;` 而**不出现**裸 `A&<B>`。
9. 空值：`运单号 = ""` ⇒ 单元格/纯文本里显示 `-`。
10. 🔴 **纯文本对齐是"全表统一列宽"，不是逐行独立算宽**（ds 2026-09-17 指出这个坑）：
    - 构造**至少 3 行、且各列长度差异明显**的数据（例如 `客户编码` 一列分别 6 / 16 / 12 个字符），**其中一行含中文**。
    - 断言一（抓"每行独立算宽"）：**所有数据行的第 2 列（箱号）起始字符位置完全相同**（每行按 `\n` 拆开、取箱号子串的 `index`，三者必须相等）。
    - 断言二（抓"列宽算错/末列没对齐"）：所有行的**总显示宽度**相同（口径：`east_asian_width` 为 `W`/`F` 算 2，其余算 1），**表头行也计入**。
    - 🔴 **⚠️ 约束：中文必须放在"第 2 列之后"（即末列），前序列必须纯 ASCII。**
      原因（**v3 修正了 v2.2 的一处自相矛盾**，2026-09-17 OpenCode 落码时指出、芙蕾雅实算确认）：
      **"前列含中文"与"第 2 列字符 index 相等"数学上互斥** —— 中文的**字符数 ≠ 显示宽度**（1 个中文字符占 2 列）。实测：

      ```
      列宽(按显示宽度) = 6
      值       显示宽度   补齐后      字符长度   箱号起始字符 index
      ABC123   6         'ABC123'    6          8
      重       2         '重    '     5          7      ← 不一致！
      ```

      ⇒ 若中文放在第 1 列，**正确实现也会得到不同的字符 index**，断言会在正确代码上失败。
      **把中文放末列即可**：断言一由"前列长度 6/16/12 的差异"驱动（照样能抓逐行算宽），断言二覆盖含中文那一行。
    - 这条用例**改前必挂**（现状根本没这逻辑）；若实现成"每行自己算列宽"，它也会挂 —— 正是要防的那种发挥。

**4.2 附件后缀（F2）**
11. 造一封原件，附件名分别为 `x.xls` / `x.xlsx` / `x.xlsm`（内容用真能解析的最小表），断言**三种都被挂上**（改前 `.xlsx`/`.xlsm` 会**完全不带附件** ⇒ 改前必挂）。
12. 断言 `act.py` 用的是**共享常量**（如 `from … import XLS_SUFFIXES`），不是自有字面量 —— 可用源码级断言或直接断言 `XLS_SUFFIXES == (".xls", ".xlsx", ".xlsm")` 且 `act` 模块里能取到该名字。
13. 断言"只取第一个匹配附件"的语义未变（附两个 xls ⇒ 只挂 1 个）。

**4.3 重写命名（F3）**

> 🔴 **断言方式统一用"精确等值"，不要用"数 `.xlsx` 出现次数"**（ds 2026-09-17 指出）：
> ```python
> base, _ = os.path.splitext(input_name)      # 期望值 = 输入名去后缀 + ".xlsx"
> assert new_name == base + ".xlsx"           # 精确等值，覆盖所有边缘情况
> ```
> 为什么不用 `count(".xlsx") == 1`：输入名本身可能就带双后缀（如 `x.xlsx.xlsx`），此时**正确结果本就是 `x.xlsx.xlsx`**，计数断言会**误挂**。精确等值既避免误挂，**又比计数更强**（能抓出任何异常变换，不只是"多一个后缀"）。

14. `.xls` 正常路径（xlrd 成功）⇒ **返回名精确等于输入名**（仍 `.xls`），且字节是 BIFF（`b"\xd0\xcf\x11\xe0"` 开头）。
15. `.xlsx`（输入 `x.xlsx`）⇒ 🔴 `assert new_name == "x.xlsx"`（精确等值；`[:-4]` 写法会得到 `x.xlsx.xlsx` ⇒ **改前/写错必挂**）。
16. 🔴 **边缘情况：输入名本身是双后缀**（输入 `x.xlsx.xlsx`）⇒ `assert new_name == "x.xlsx.xlsx"`（**原名保留、不得继续叠加**）。这条专门钉住 ds 提的误挂风险，同时证明实现没把名字改坏。
17. `.xlsm`（输入 `x.xlsm`）⇒ 🔴 `assert new_name == "x.xlsx"`（精确等值；不是 `x.xlsm.xlsx`），字节是 ZIP。
18. 🔴 **回退路径**：喂一个"名字是 `x.xls`、内容其实是 xlsx"的文件 ⇒ 🔴 `assert new_name == "x.xlsx"`、字节是 ZIP（**改前必挂**：会返回 `x.xls` + ZIP 字节 —— 名字与字节不一致）。
19. `keep_rows` 为空 ⇒ 返回 `(None, None)`；重写失败 ⇒ 返回 `(None, None)` 且**不抛异常**。
20. 断言"重写失败时调用方仍附原始附件"（`act.py` 的 else 分支行为未被破坏）。

**4.4 基线**
21. 全量套件 **0 failed**，且**增量恰等于新增用例数**（即 `改动后总数 − 改动前总数 == 新文件用例数`）。请贴原始汇总行 + 改动前后两次的收集数。
    > 🔴 **不要引用历史绝对值**（v3 修正）：本 spec 早先写的"基线 372"是**过时数字**（实测改动前为 **373**，期间 PR#12/#14 多次增删用例）。**判据用相对量，不用会漂移的绝对值。**

---

## 5. 生产运行目录纠正（**不影响本 spec 的代码**，但请勿再引用错的地方）

🔴 实测（2026-09-17）：**机器人真正在跑的是 `\\10.0.199.184\yxo_data\MailBots\`**（= 服务器 `D:\YXO_DATA\MailBots\`，**不是 git 仓**，纯部署目录）。证据：该目录下 `backoff_state.json` 与 `data/` **当日 09:05 被写**、`logs/` 当日 00:10。

| 目录 | 是什么 |
|---|---|
| `\\10.0.199.184\yxo_data\**MailBots**\` | 🔴 **机器人运行目录**（`Waybill_Robot.py` 9-03 / `Draft_Forward_Robot.py` 9-04 / `Atb_Robot.py`、`Dsk_Robot.py` 8-31；启动脚本 `start_waybill_loop.bat` 等） |
| `\\10.0.199.184\yxo_data\yxo_app\` | **网页端**（`app.py` / :5011 / manifest / `yxo.db`），HEAD `9c4f7da` |

⇒ **"停旧起新"要停的是 `MailBots\` 下的机器人**，不是 `yxo_app\`。已同步进部署清单。

---

## 6. 非目标（**明确不要做**）

- ❌ **不改旧系统**（`MailBots\` 下的机器人、仓内 `mailbots/` 目录）—— 洋 2026-09-17 明确：不在仓库里、改动风险大、业务可接受，新系统上线后自然退役。
- ❌ **不改收信层**（`ingest.py` / 轮询 / UID 水位线 / 删 IDLE / `serve.py` 的收信接线）—— 那批走 `docs/2026-09-16-收信通路修复spec(OpenCode)-方案A轮询.md`，**分开提交**。
- ❌ 不改 `split_dsk_by_box()`（DSK 拆分的 HTML 注入）。它的 `<table>` 处理是**原样保留原件 HTML**，不属于本次问题。
- ❌ 不在 `text/html` 里加"更新/作废"类字样。
- ❌ 不 commit / 不 push（交我复核后再由洋决定）。

---

## 7. 验收（请贴**原始输出**）

1. **正文形态**：
   - `grep -n '"--- | ---"' mailbots_next/core/act.py` → **0 条**
   - `grep -n "multipart/alternative\|\"alternative\"" mailbots_next/core/act.py` → **≥1 条**
2. **后缀口径**：
   - `grep -rn "XLS_SUFFIXES" mailbots_next/core/` → **≥2 条**（定义处 + act.py 使用处）
   - `grep -n 'endswith(".xls")' mailbots_next/core/act.py` → **0 条**
3. **重写命名**：`grep -n '".xlsx"' mailbots_next/core/act.py` → 出现在 `rewrite_xls_filtered` 的改名分支里；且 `grep -n "splitext" mailbots_next/core/act.py` → **≥1 条**；`grep -n "\[:-4\]" mailbots_next/core/act.py` → **0 条**
4. **登记（§3.1）**：`grep -n "keep_vba\|宏" mailbots_next/README.md` → **≥1 条**（技术债表新增的那一行）
5. **测试**：新文件用例逐条列出；全量套件 **0 failed** 且**增量恰等于新增用例数**（**不要引用历史绝对值**）；**必须用默认 basetemp**（`python -m pytest -q -p no:cacheprovider > out.txt 2>&1`），**禁止 `--basetemp=<项目内目录>`**（WorkBuddy safe-delete shim 会让测试在断言前假失败）
6. **改动清单**：逐条列出所有改动文件（**动了非本 spec 点名的文件必须写明原因**）
7. **未回退确认**：`make_forward_id` 纯 hex / `_forward_split` 三处同一 `sub_fid` / `send_ctx["_meta"]` / `_row_key` / `poll_bounces` 先于 `sweep_once` —— 均不得回退。

---

## 8. 提交要求（洋 2026-09-17 明确）

- **单独一份、单独 commit**，与收信通路那批**分开**（转发层 ≠ 收信层）。
- commit message 建议：`fix(mailbots_next): 运单号转发正文改 multipart/alternative（真表格）+ 附件后缀对齐 extractor + 修复重写回退的扩展名不一致`
- **预期改动文件**（请对照报告；多出的要写明原因）：`core/act.py`、`tests/test_forward_layer.py`（新建）、`README.md`（§3.1 的那行登记）。若你选了 F2 的备选方案，还会多 `config/types.py` 或 `core/consts.py`。
- **不 commit / push 由我复核后再定**（按既有流程）。

---

## 9. F4【客户可见 · 深色模式】不要"只声明前景色、不声明背景色"

> **状态：F1~F3 已落码并验收通过（实跑 393 passed / 0 failed）。本节是 F4，单独一轮，只改这一件事，不要顺手动 F1~F3 的其它内容。**

### 9.1 问题（为什么这是客户可见的缺陷）

现在生成的 HTML 里声明了**前景色但没声明背景色**：

| 位置 | 现状 |
|---|---|
| 外层 `<div>`（`act.py:247`） | `font-size: 13px; **color: #000;**` ← 强制黑字，**没有背景色** |
| `<th>`（`act.py:197` 的 `_TH_STYLE`） | `border: 1px solid #999; padding: 4px 10px; **background: #f2f2f2;**` ← 浅灰底，**没有前景色** |

🔴 **危险组合就是"声明了 color、没声明 background"**：邮件客户端在**深色模式**下会用自己的深色背景，但**保留我们写死的 `color:#000`** ⇒ **黑字压深底，客户看不清**。

（另外这结构本身也不自洽：`<th>` 是浅底、`<td>` 是跟随客户端的底 —— 一旦客户端走深色，同一张表里表头是浅底黑字、数据格是深底浅字，观感也不是我们要的。）

### 9.2 修法（**方案 A，洋已选**）：不声明任何前景色/背景色，表格结构靠边框维持

**要改的就 2 处**（全仓 `grep -n "color:\|background" act.py` 确认只有这两处）：

1. **`act.py:247`**：外层 `<div>` 的 style 里**删掉 `color: #000;`**，保留 `font-family` / `font-size`（字体不影响对比度）。
   ```python
   # 改后
   '<div style="font-family: Tahoma, Arial, \'微软雅黑\', SimSun; '
   'font-size: 13px;">'
   ```
2. **`act.py:197` 的 `_TH_STYLE`**：**删掉 `background: #f2f2f2; `**，其余（border / padding / text-align / white-space）**全部保留**。
   ```python
   _TH_STYLE = ("border: 1px solid #999; padding: 4px 10px; "
                "text-align: left; white-space: nowrap;")
   ```

改完后：HTML 里**一个 `color:` 都没有、一个 `background` 都没有** ⇒ 客户端在**任何**主题下都会自己配对前景/背景（黑字白底 / 白字黑底都成立），**不可能出现"看不清"**。表格结构**靠 `border: 1px solid #999`（中灰，浅底深底都可见）**维持；表头靠 `<th>` **默认加粗**区分（不再靠灰底）。

**保持不动**：`border: 1px solid #999`、`padding: 4px 10px`、`white-space: nowrap`、`text-align: left`、`border-collapse: collapse`、`html.escape`、`<table>/<thead>/<tbody>/<th>/<td>` 结构、纯文本部件（F1 的对齐逻辑一律不动）。

> **备选方案 B（不采用，仅备注）**：反过来**两个都声明**（外层加 `background:#ffffff;`）——"白卡片"确定性更强，但深色模式下会出现一块刺眼白块，且与"跟随客户端主题"的常规做法相悖。**除非洋改口，一律按 A 做。**

### 9.3 F4 的测试要求（加到 `test_forward_layer.py`，**不要改动已有 20 条**）

> 📌 **改前基线（我实测，单行数据时）**：`color:` **1 次**、`background` **3 次**（`<th>` 有 3 个，每个都带了一遍 `_TH_STYLE`）、`border: 1px solid` **6 次**（3×th + 3×td）。改后应为 `color:` **0** / `background` **0** / `border` 仍 **6**。

22. 🔴 **不声明任何颜色**：`text/html` 部件里 **`"color:"` 出现 0 次**（注意也要排除 `background-color` 之类 —— 可用 `re.search(r"(?<!-)color\s*:", html)`，或最简：断言 `"color:" not in html.lower()`）。
23. 🔴 **不声明任何背景**：`text/html` 里 **`"background"` 出现 0 次**（这条**改前必挂**：现状 **3 次**）。
24. **表格结构仍在**：`text/html` 仍含 `<table`、`<th`、`<td`，且 `"border: 1px solid"` **出现 6 次**（单行数据：3×th + 3×td；若你用了别的行数，按实际列数 vs 行数算清并写明）。
25. **已有的约束不回退**：仍无 `<style`、无 `class=`；`<th>`/`<td>` **仍带 `white-space: nowrap`**（可顺手补上这条断言 —— 见 §6 观察 3）。
26. **纯文本部件零影响**：`text/plain` 仍不含 `"|"`、不含 `"--- | ---"`，且对齐断言（原 `test_10`）仍通过。

### 9.4 F4 的验收 grep

- `grep -c "color:" mailbots_next/core/act.py` → **0**
- `grep -c "background" mailbots_next/core/act.py` → **0**
- `grep -n "border: 1px solid" mailbots_next/core/act.py` → **≥1**（`_TH_STYLE`/`_TD_STYLE` 两行里都在）
- 全量套件 **0 failed**，**增量恰等于新增用例数**（原 20 → 现按你实际条数报）

---

## 10. 【v3.2 新增 · 洋 2026-09-17 决定】删除正文自撰前言

> **本节由芙蕾雅直接改并落码**（洋："这个芙蕾雅你直接改，改完告诉我"）。

### 10.1 洋的要求（原话）

> "「本邮件仅包含贵公司相关的运单号信息」这句话有吗？在转发邮件的时候不要擅自加这些内容，下面公司只要收到按规则拆分的邮件就行了。"

⇒ 规则：**正文只放"按规则拆分出来的内容本身"（= 表格），不加任何机器人自撰的说明性文字。**

### 10.2 改什么（2 处，`mailbots_next/core/act.py`）

| 位置 | 改前 | 改后 |
|---|---|---|
| `_build_waybill_text_plain()` | `return "本邮件仅包含贵公司相关的运单号信息：\n\n" + _format_plain_table(...)` | `return _format_plain_table(...)` |
| `_build_waybill_html()` | `<div …><p style="margin: 0 0 10px 0;">本邮件仅包含…：</p><table …>` | `<div …><table …>`（**删掉整个 `<p>`**） |

**保持不动**（都不是"内容"）：`<div>` 的 `font-family`/`font-size`、`<table>` 的 `border-collapse`、`border: 1px solid #999`、`padding`、`text-align`、`white-space: nowrap`、`html.escape`，以及 F4 的**零颜色声明**。

> ⚠️ 本 spec §2（F1）里那些**带前言的正文片段已被本节取代** —— 那是按当时生产原样保留的证据，不是目标形态。

### 10.3 改后的正文（实测原文）

`text/plain`（第一行就是表头；无前言、无前导空行）：

```
客户编码               箱号         运单号
CQWLJT260914003-XLYJN  FWRU0243394  38273225
CQWLJT260914007-ZQ     TCLU5599881  38273301
CQWLJT260915012-LY     HNKU5117095  重庆转关99
```

`text/html`：`<div style="font-family:…; font-size: 13px;"><table style="border-collapse: collapse; font-size: 13px;"><thead>…</thead><tbody>…</tbody></table></div>`（无 `<p>`、无颜色声明）

### 10.4 测试

- 原 `test_10` / `test_25` 里的 `split("本邮件仅包含…：\n\n")[1]` 改为 **`split("\n")`**（正文即表格，第一行是表头）。
- **新增 `test_26_no_selfauthored_preamble_in_body`**：断言纯文本**首行是表头**、无前导空行、不含 `本邮件` / `详见附件`；HTML **不含 `<p`**、不含 `本邮件` / `详见附件`。⇒ **锁住"别再把它加回来"**。
- ✅ **真锁已验证**：在**内存里**把两个构造函数换回带前言版本 ⇒ `test_26` **必挂**；换回当前实现 ⇒ 通过（**源文件未改动**，md5 校验一致）。

### 10.5 验收

| 项 | 结果 |
|---|---|
| `tests/test_forward_layer.py` | **26 passed**（25 → 26） |
| `mailbots_next/tests/` | **0 failed**（增量**恰为 1** 条新用例） |
| 肉眼渲染 | `tmp/waybill_body_preview.html` + 截图：改前（带前言）/ 改后（只有表格）；浅色与暗色均正常 |

### 10.6 后续注意

- 其余转发类型（草单 / DSK / ATB / 运踪）**本就零自撰文案**（"更新/作废/【草单更新】"grep = 0）；`act.py:95`/`:111` 是**原封转发上游正文**，不含我们添加的文字。
- 旧系统 `mailbots/Waybill_Robot.py:582` 同样有这句，但**按洋此前决定旧系统不修**。
