# 第三批 spec：WAY_B 改为「静默跳过」（撤掉通知）+ 通知收件人去重（OpenCode）

> 交办：芙蕾雅 ｜ 日期：2026-09-23 ｜ 执行：OpenCode ｜ 验收：芙蕾雅
> **洋 2026-09-23 拍板（两条）**：
> **① 单证审核被拒邮件不再进企微提示** —— 业务理由：**渝新欧在操作时会直接与同事沟通**，这件事不会因为少了企微提醒就没人做，**提醒反而是累赘**。
> **② 通知收件人重复要修**（1 行去重 + 真锁）。
> 前提：上一批（A/B/C 三组）已落码并经我独立验收通过，**本轮是在其之上做方向调整**。

---

## 0. 交付纪律（四条铁律，先读）

1. **不瞎改已做好的内容** —— 只改本 spec 点到的位置；看到别的"顺手能改的"不要动，写进报告。
2. **不要 commit、不要 push、不要建分支**（分支由洋建）。**改完停在工作区**；若已提交不要 revert，在报告里说明。
3. **做完先自测**：跑测试 + 做**真锁**往返验证，**原始输出贴进报告**。
4. **如实报告**：偏差、不确定处、失败都写出来；**不要替人拍板**。

**分组**：本轮 = **两组（A 改造 / B 去重）**，交付后由洋按此分 **两个 commit**。
⚠️ 这两组是给你划边界用的，**不是让你去 commit**。

---

## 1. 背景：上一批落了什么（现状，你据此改）

上一批把「WAY_B 单证审核驳回」实现成了**发企微告警给负责同事**。**本批要把它改成"什么都不发"**。
现状（我已核到行号）：

| 文件 | 现状 |
|---|---|
| `config/types.py:78` | `WAYBILL_REJECT_KEYWORD = "单证审核驳回"` |
| `config/__init__.py` | 已导出该常量 |
| `core/extract.py:25` | `ExtractedRow.waybill_rejected: bool = False` |
| `core/extractors/waybill.py:19-38` | WAY_B 分支：主题含关键词 ⇒ **从正文** `CODE_RE.search(body).group(0).split("-")[0]` 取码 ⇒ 单行 `waybill_rejected=True` |
| `core/decide.py:85-87` | 守卫：`waybill_rejected` ⇒ `Decision("WAY_B", "notify_rejected", ...)` |
| `core/notify.py:311-316` | `send_waybill_rejected()`（`notify_type="waybill_rejected"`） |
| `serve.py:308-324` | 早分流：`route_row` 取负责同事 ⇒ `send_waybill_rejected(...)` ⇒ `return "rejected"` |
| `tests/test_waybill_rejected.py` | 6 条用例（其中 ③④ 断言"发了通知"） |

---

## 2. A 组：WAY_B 改为**静默跳过**（撤掉通知）

### 2.1 目标行为（**一句话**）

> 主题含「单证审核驳回」的邮件：**识别出来、记一条 info 日志、什么也不发** ——
> **不通知任何人（企微/邮件都不发）、不转发、不进待办、不进错误队列、不记 ERROR。**

⚠️ **"不进错误队列"仍然是硬要求**（这正是上一批要根治的噪音）——**不要**因为"不发通知了"就顺手把它变回普通流程：那样这封邮件会落回 `no_route` ⇒ `ERROR` + 错误队列。

### 2.2 逐文件改造

**① `serve.py:308-324`：早分流整块替换为**

```python
        # WAY_B 单证审核驳回：业务上不需要通知（渝新欧会直接与同事沟通），
        # 故静默跳过 —— 不通知、不转发、不进错误队列、不记 ERROR。
        # 🔴 仍必须在路由【之前】：路由失败会走错误队列分支，又变回噪音。
        if getattr(row, "waybill_rejected", False):
            _log.info(
                f"WAY_B skipped by design (no notify) | msg_id={message_id[:50]} | "
                f"row={row.row_idx} | code={row.customer_code or '-'} | subject={subject[:100]}"
            )
            increment_counter("action_wayb_rejected_skipped")
            return "rejected"
```

- **不再需要 `route_row`**（没人要通知，不需要解析负责同事）⇒ 整块里对 `route_row` 的调用一并删掉。
- **仍保留 `return "rejected"`**：作为独立可观测的终态词。它不置 `has_manual` ⇒ 邮件照旧被标已读（**与旧系统一致**，旧系统 `mark_seen_everywhere` 在 A/B 两类共用尾部；**不要动 `serve.py:201-207`**）。

**② `core/decide.py:85-87`：守卫保留，但动作改成 `skip`**

```python
    # 🔴 必须在委托 decide_draft 之前：否则带编码的 WAY_B 会命中 T1
    #    ⇒ action=forward ⇒ 把「单证驳回」邮件转发给【客户】。
    if getattr(row, "waybill_rejected", False):
        return Decision("WAY_B", "skip", "Rejected document: no action by design")
```

> 这条守卫是**第二道闸**（防有人把早分流挪走/去掉）。`"skip"` 在 `execute_action`（`act.py:560-561`）里**已有分支** ⇒ 返回 `(True, "skipped")`、不发信、不通知 ⇒ **不需要新增任何动作分支**。

**③ `core/notify.py`：删掉 `send_waybill_rejected()`（311-316）**

它现在**没有任何调用者** ⇒ 死代码。
**为什么必须删而不是留着**：本项目的规矩是"**名字不得承诺代码里不存在/不再做的事**"（上一批刚因同类问题删过 `send_draft_update` 与草单台账）。留着它，将来会有人以为"单证驳回是会通知的"。

**④ `core/extractors/waybill.py`：保持不动** ✓
（仍要**识别** WAY_B，也要**从正文取码** —— 码进 info 日志，便于将来排查"哪一单被拒"。这是本轮唯一保留"多余信息"的地方，理由就是可追溯。）

**⑤ `config/types.py` / `config/__init__.py` / `core/extract.py`：保持不动** ✓

### 2.3 测试改造（`tests/test_waybill_rejected.py`）

**必须改造现有 4 条**（③④ 现在断言的"发了通知"已不成立）：

| 用例 | 改成 |
|---|---|
| ① 正文取码 | **保持不变** |
| ② 不转发、不入队 | **保持不变** |
| ③ 原「通知到负责同事」 | 改为 **`test_rejected_sends_no_notification`**：走完整 `process_email` ⇒ 断言 `proc.notifier` 的**任何 send_\* 方法都没被调用**（如断言 `proc.notifier.mock_calls == []`）—— **这是本组最关键的断言** |
| ④ 原「兜底发 OPS」 | 改为 **`test_rejected_silent_even_when_unroutable`**：路由拿不到负责同事时**同样零通知**，且 `add_error` 未被调用（仍不进错误队列） |
| ⑤ 守卫 | 改为断言 `d.action == "skip"`、`d.tier == "WAY_B"`（**绝不是 `forward`**） |
| ⑥ 普通运单号回归 | **保持不变** |

**新增 1 条**：`test_rejected_records_info_counter` —— 断言计数 `action_wayb_rejected_skipped` 有增长（**"静默"不等于"没痕迹"：必须能在计数和日志里看见**）。

### 2.4 红线（**不许做的事**）

- ❌ **不发任何通知**（企微/邮件都不发）；**不留**新的待办项。
- ❌ **不进错误队列、不记 ERROR**（这是上一批的成果，不能回退）。
- ❌ **不转发给任何客户**。
- ❌ 不删 `waybill_rejected` 字段、不删 `WAYBILL_REJECT_KEYWORD`、不删 `waybill.py` 的 WAY_B 分支（**识别能力要留**）。
- ❌ 不动 `serve.py:201-207`（标已读）、不动 `draft.py`/草单逻辑、不动旧系统 `mailbots/`。

### 2.5 A 组验收判据 + 真锁

**判据**
1. 用例全绿；`grep -rn "send_waybill_rejected" mailbots_next/ --include=*.py` = **0 命中**（方法已删、调用已删、测试已不再引用）；
2. 走完整 `process_email` 的用例断言 **notifier 零调用**（见 ③）；
3. `grep -rn "action_wayb_rejected_skipped" mailbots_next/ --include=*.py` 恰在 `serve.py` 出现 1 次（`increment_counter`）+ 测试内 1 次。

**真锁（3 条，每条：撤改 → pytest 原始输出 → 还原 → md5 一致）**
- **锁 1**：删掉 `serve.py` 的 WAY_B 早分流整块 ⇒ **至少 1 条挂**（会落回 `no_route` 错误队列路径）；
- **锁 2**：把 `decide_waybill` 的守卫改成直接 `return decide_draft(...)` ⇒ 用例⑤ **必挂**；
- **锁 3**：把早分流里的 `return "rejected"` 换成 `return "manual"` ⇒ **至少 1 条挂**（若你认为这条锁不住，**说明原因**，不要静默跳过）。

> ⚠️ 撤改时**把行首缩进一起纳入匹配**（只搜 `return ...` 会吞掉 4 个空格 ⇒ 产出语法错误 ⇒ pytest 报 `1 error` 而不是干净失败）。**撤改后先 `ast.parse()` 自检语法，再跑测试。**

---

## 3. B 组：通知收件人**去重**（1 行）

### 3.1 问题（实测证据）

`OPS_OWNER_EMAIL = maoxiaoyang@cqtransit.com`（`settings.py:32`），而 `core/store.py::seed_owner_mapping` 里**「太平洋」「港九港铁」的负责同事正是他** ⇒ 通知收件人列表变成 `[mao, mao]`，而 `_send_now`（`notify.py:236`）**逐个收件人循环、不去重** ⇒ **同一个人收到两条相同消息**。

实测（假 client 计 `_notify_by_name` 调用次数）：`responsible == OPS` 与兜底 `(OPS, OPS)` 两种场景**都调用 2 次**。
影响 `send_forwarded` / `send_alarm` / `send_pending` 等**所有**通知类型（**本批之前就存在**）；8 家客户里有 2 家归运维本人 ⇒ 会实际发生。

### 3.2 改法（`notify.py:236`）

```python
        for recipient in dict.fromkeys(recipients):      # 去重：同一人只发一次
```

**只改这一行**，不动 `unmapped` 逻辑、不动 `_fallback_owner_email`、不动返回值语义。

### 3.3 用例 + 真锁

**新增 2 条**（建议放 `tests/test_notify_email.py`，那里已有假 client 设施；用假 `_notify_by_name` 记录调用即可，**不打真企微**）：
- `test_duplicate_recipients_send_once`：同一邮箱传两次 ⇒ `_notify_by_name` **只调用 1 次**；
- `test_distinct_recipients_still_each_get_one`：两个**不同**邮箱 ⇒ **各调用 1 次（共 2 次）**。
  ⚠️ 第二条是**防过度修复**的守卫：别把去重写成"只发第一个"。

**真锁**：把 `dict.fromkeys(recipients)` 改回 `recipients` ⇒ `test_duplicate_recipients_send_once` **必挂**；还原后 md5 一致。

---

## 4. 报告格式

```
一、改动文件（按 A / B 两组分别列：文件 + 改了什么）
二、测试
    - 基线（开工前 HEAD / 用例数）→ 现在（用例数 / 汇总行原文 / 退出码）
    - 被改造的用例逐条列名（旧的 → 新的）；新增用例逐条列名
三、真锁往返（A 组 3 条 + B 组 1 条，每条：撤改动作 → pytest 原始输出 → 还原后 md5）
四、grep 验收（§2.5 第 1、3 条，贴原始输出）
五、偏差与请示（有就写，没有就写"无"）
六、不确定 / 我没做的事
```

---

## 5. 背景（供理解，不必回应）

- 上一批把 WAY_B 做成了告警，依据是"**你 2026-07-30 拍板过『单证驳回单独告警』、且生产旧系统一直这么做**"（当时在同一份设计文档里还查到 8-26 写过"完全忽略"，两者冲突）。
- **洋 2026-09-23 从业务角度重新判定**：渝新欧操作时会**直接与同事沟通**，所以企微提醒没有业务价值、反而是累赘 ⇒ **撤掉通知**。
- 因此本批 = **"识别 + 静默跳过"**：既不再发通知，**也仍然不产生错误队列噪音**（后者是本轮真正的技术目标，不能回退）。
