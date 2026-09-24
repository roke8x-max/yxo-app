# 第二批 spec：waybill_rejected 单证审核驳回补实现 + 测试基础设施两处（OpenCode）

> ⚠️ 更名说明（2026-09-24 命名清理批）：本文件原写的新系统 tier 名 `WAY_B` 已统一改为
> `waybill_rejected`，文中引用的代码行/日志文案均同步更名 ⇒ 在 9-24 之前的提交里看到的
> 实际字样是 `WAY_B`（映射表见 `docs/2026-09-23-命名清理(tier代号)-前置调查与小spec.md` §4）。
> 用例名与其它代号**未**同步更名。

> 交办：芙蕾雅 ｜ 日期：2026-09-22 ｜ 执行：OpenCode ｜ 验收：芙蕾雅
> **洋 2026-09-22 拍板三条**：**A** 单证审核驳回 → **按生产行为补实现告警** ／ **B** 修那条环境耦合断言 ／ **C** 日志目录隔离（**上线自检的命门**）。
> 依据：`docs/2026-09-22-单证驳回缺口与测试耦合-分析与推荐.md`（我的独立核实全文）

---

## 0. 交付纪律（四条铁律，先读）

1. **不瞎改已做好的内容** —— 只改本 spec 点到的位置。看到别的"顺手能改的"**不要动**，写进报告即可。
2. **不要 commit、不要 push、不要建分支**（分支由洋建；**这台机器建嵌套分支名会被静默抹掉**）。
   **改完停在工作区**。若已提交，**不要 revert**，在报告里说明。
3. **做完先自测**：跑测试 + 做**真锁**往返验证，**原始输出贴进报告**（贴汇总行原文，别只写"通过"）。
4. **如实报告**：偏差、不确定处、失败都写出来；**不要替人拍板**（拿不准就写"请示"）。

**分组**：本轮 = **三组互不相干的改动（A / B / C）**，交付后由洋按此分 **三个 commit**。
⚠️ 这三组是给你划边界用的（每组各自自测、互不牵连），**不是让你去 commit**。

---

## 1. 改动文件总览

| 组 | 文件 | 改动 |
|---|---|---|
| **A** | `mailbots_next/config/types.py` | 新增常量 `WAYBILL_REJECT_KEYWORD` |
| **A** | `mailbots_next/config/__init__.py` | 按现有 `CODE_RE` 等方式**同步导出**该常量（+ `__all__`） |
| **A** | `mailbots_next/core/extract.py` | `ExtractedRow` 新增字段 `waybill_rejected: bool = False` |
| **A** | `mailbots_next/core/extractors/waybill.py` | 新增 waybill_rejected 分支（**从正文取码**） |
| **A** | `mailbots_next/core/decide.py` | `decide_waybill` 首行加**守卫**（防误判成 T1 转发给客户） |
| **A** | `mailbots_next/core/notify.py` | 新增 `send_waybill_rejected()` |
| **A** | `mailbots_next/serve.py` | `_process_row` 加**早分流**（在路由之前） |
| **A** | `mailbots_next/tests/test_waybill_rejected.py`（新） | A 组用例 |
| **B** | `mailbots_next/tests/test_p1_coverage.py` | 修那条环境耦合断言（`TestCredentials`） |
| **C** | `mailbots_next/config/settings.py` | `LOGS_DIR` 改为可被环境变量覆盖 |
| **C** | `mailbots_next/tests/conftest.py` | 重定向 `LOGS_DIR` 到测试临时目录 |

---

## 2. A 组：waybill_rejected「单证审核驳回」补实现（**本轮核心**）

### 2.1 现状（我已核到行号，你不要再猜）

这封邮件（主题 `单证审核驳回`、发件人 `docwbfb@yxologistics.com`、**无附件**、编码在**正文**）：

```
分类：命中 TYPE_ROUTES 的 WAYBILL（发件人白名单 docwbfb@）      ← 分类本来就对
提取：waybill.py:45-69 —— 无 xls 时【只从主题取码】⇒ code=None
      （waybill.py:16 把 body 读出来了，此后从未使用）
⇒ row_key = "0:no_key"（rowkey.py:14）
⇒ 路由 no_route（routing.py:177）
⇒ serve.py:375-385：记 ERROR + 入 error_queue + routing_failed 计数
  ⚠️ decide() 在路由【之后】才调用（serve.py:389）⇒ 路由失败时它根本不执行
⇒ 实际行为 = ERROR 日志 + 错误队列，【零业务通知】
```

**目标行为**（等价于生产在跑的旧系统 `MailBots/Waybill_Robot.py`，也即洋 7-30 拍板）：
> **从正文取编码 → 找负责同事 → 只给他发一条告警；不转发给任何客户、不进待办队列、不进错误队列。**

### 2.2 实现要求（逐文件）

**① `config/types.py`** —— 新增模块级常量（与既有 `UPDATE_KEYWORDS` 同一处）：

```python
WAYBILL_REJECT_KEYWORD = "单证审核驳回"
```

并在 `config/__init__.py` 按 `CODE_RE` / `UPDATE_KEYWORDS` **现成的方式**导出（含 `__all__`）。

**② `core/extract.py`** —— `ExtractedRow`（`dataclass`）新增一个字段，**必须有默认值**（不影响既有构造点）：

```python
    waybill_rejected: bool = False
```

**③ `core/extractors/waybill.py`** —— 在 `extract()` 里，取到 `subject`/`body` 之后、**附件分流之前**，加 waybill_rejected 分支：

- 判据：`WAYBILL_REJECT_KEYWORD in subject`
- 编码**从正文取**，照旧系统口径（`MailBots/Waybill_Robot.py:extract_code_from_body`）：
  `CODE_RE.search(body)` 命中后取 `.group(0).split("-")[0]`
  （例：正文 `您的编码为:CQWLJT260917002-Kol单证审核被拒.` → 取到 `CQWLJT260917002`）
- `code_num` 用 `CODE_NUM_RE.match(code)` 取（取不到就 None）；`container_no=None`
- 返回**单行**，`waybill_rejected=True`，`email_type="waybill"`
- **不要动既有的 xls 路径与"主题取码"路径**

**④ `core/decide.py::decide_waybill`** —— **首行**加守卫：

```python
def decide_waybill(row, routing, records) -> Decision:
    # 🔴 必须在委托 decide_draft 之前：否则带编码的 waybill_rejected 会命中 T1
    #    ⇒ action=forward ⇒ 把「单证驳回」邮件转发给【客户】。
    if getattr(row, "waybill_rejected", False):
        return Decision("waybill_rejected", "notify_rejected",
                        "Rejected document: notify responsible colleague only")
    return decide_draft(row, routing, records)
```

**⑤ `core/notify.py`** —— 照 `send_alarm`（`:299`）的写法新增：

```python
    def send_waybill_rejected(self, responsible_person: str, admin_person: str,
                              detail: str, error_id: Optional[str] = None) -> bool:
        content = f"🚨 单证审核驳回告警\n{detail}"
        return self.notify("waybill_rejected", [responsible_person, admin_person], content, error_id)
```

- `detail` 由调用方拼：`f"客户编码: {row.customer_code or '-'} | {subject[:100]}"`
  ⇒ 最终正文与旧系统一致（`🚨 单证审核驳回告警` + `客户编码: X` + 摘要）。
- ⚠️ **不得带"回复确认/跳过"** —— 那条链路依赖 8765 回调、**生产不通**（已知缺口）。**在方法 docstring 里写明这个降级**（"单向告知；一键操作需 8765 回调恢复后另议"）。
- `notify_type="waybill_rejected"` 不在 `_COLLAPSIBLE_TYPES` 内 ⇒ **不折叠**（符合预期：每条对应不同客户）。

**⑥ `serve.py::_process_row`** —— 紧接既有 `draft_category in ("C2","W","OTHER")` 早分流（`:299-308`）**之后**、`routing = route_row(...)`（`:309`）**之前**，加早分流：

```python
        # waybill_rejected 单证审核驳回：只通知负责同事。
        # 🔴 必须在路由【之前】：路由失败会走 :375-385 的错误队列分支，
        #    那样这封"已知且预期会来"的邮件又变成 ERROR 噪音。
        if getattr(row, "waybill_rejected", False):
            # 仅用于解析负责同事，无副作用（不发信、不转发）
            r = route_row(email_type, row, self.records)
            responsible = ""
            if not isinstance(r, list):          # waybill 只会返回单个 RoutingResult，这里防御性写法
                responsible = r.responsible_person or ""
            _log.log_decide(message_id, row.row_idx, "waybill_rejected", "notify_rejected",
                            f"Rejected document; responsible={responsible or 'OPS fallback'}")
            ok = self.notifier.send_waybill_rejected(
                responsible, OPS_OWNER_EMAIL,
                f"客户编码: {row.customer_code or '-'} | {subject[:100]}")
            _log.log_action(message_id, row.row_idx, "notify_rejected", "notify_rejected")
            increment_counter("action_notify_rejected" if ok else "notify_rejected_failed")
            return "rejected"
```

- **路由失败（取不到负责同事）时**：`responsible` 为空 ⇒ 按上面的写法**兜底发 `OPS_OWNER_EMAIL`**，**绝不静默丢弃、绝不进错误队列**。
- outcome 词 `"rejected"` **无需改** `:169-170` 的计数表（`:182` 用 `.get(..., 0) + 1`，新词自动纳入）；**也不要改** `:316` 的终态优先级元组（那是多公司扇出路径专用，本分支直接 `return`）。
- ⚠️ **不要动标已读逻辑**（`:201-207`）：`"rejected"` 不置 `has_manual` ⇒ 邮件会被标已读 —— 这与旧系统**一致**（旧系统 `mark_seen_everywhere` 在 A/B 两类共用的尾部，`Waybill_Robot.py:995`，**B 类也标已读**）。我已核过，别再改。

### 2.3 红线（**不许做的事**）

- ❌ **不转发给任何客户**（waybill_rejected 只发企微通知，**不发 SMTP**）；`execute_action` **不需要**新增动作分支（本组不走它）。
- ❌ **不进待办队列**（不调 `send_pending`、不写 pending 相关表）。
- ❌ **不进错误队列**、**不记 ERROR**（它不是程序错误）。
- ❌ 不去改旧系统 `mailbots/`；不去改 `docs/superpowers/` 里那两份 8-26 设计文档。
- ❌ 不碰 `draft.py` / 草单相关任何逻辑（那批刚验收完）。

### 2.4 A 组验收判据

1. 用例（**全部用假数据，不打真企微、不发真信**）：
   - **① 正文取码**：正文含 `您的编码为:CQWLJT260917002-Kol单证审核被拒.` + 主题 `单证审核驳回` + 无附件 ⇒ `waybill_rejected is True`、`customer_code == "CQWLJT260917002"`、`code_num` 正确；
   - **② 不转发**：走完整 `_process_row`（mock notifier）⇒ `execute_action` **未被调用**（或断言没有任何 SMTP 调用），且 `add_error` **未被调用**；
   - **③ 通知到负责同事**：mock 掉 `route_row` 让负责同事为某人 ⇒ 断言 `send_waybill_rejected` 收到的**第一个参数是该负责同事**；
   - **④ 取不到负责同事时兜底**：mock 路由失败 ⇒ 断言**仍然发**、且收件人含 `OPS_OWNER_EMAIL`；**断言 `add_error` 未被调用**（= 不进错误队列）；
   - **⑤ 守卫在 decide 里独立生效**：直接调 `decide_waybill(row_with_code, routing_success, records)` ⇒ 必须返回 `action == "notify_rejected"`（**不得**是 `forward`）—— 这条是防"有人把早分流挪走/去掉"的第二道闸；
   - **⑥ 回归**：普通运单号邮件（有 xls / 主题含编码）行为**不变**。
2. **真锁（必须做，贴原始输出）**：
   - 撤掉 `serve.py` 的早分流 ⇒ **至少 1 条挂**；
   - 删掉 `waybill.py` 的正文取码（改回 `code=None`）⇒ **至少 1 条挂**；
   - 删掉 `decide_waybill` 的守卫 ⇒ 用例⑤ **必挂**（证明它独立有效）。
   - 每条：撤改 → pytest 原始输出 → 还原 → **md5 一致**。
3. grep：`grep -rn "WAYBILL_REJECT_KEYWORD\|waybill_rejected\|send_waybill_rejected" mailbots_next/ --include=*.py` 只应出现在上述 6 个生产文件 + 新测试里。

---

## 3. B 组：修那条环境耦合断言

**文件**：`mailbots_next/tests/test_p1_coverage.py`（`TestCredentials::test_fake_secrets_effective_and_real_untouched`，约 `:384-393`）

**现在的写法**（第 391 行）：

```python
assert not (BASE_DIR / "secrets.json").exists()      # 🔴 部署机【必须】有这个文件 ⇒ 在部署目录跑测试必红
```

**改成**（保留"能失败"的属性，改成环境无关的等价写法）：

```python
    def test_fake_secrets_effective_and_real_untouched(self):
        import mailbots_next.config.secrets as sec_mod
        from mailbots_next.config import BASE_DIR
        real = BASE_DIR / "secrets.json"
        def snap():
            return (real.stat().st_mtime_ns, real.stat().st_size) if real.exists() else None
        before = snap()
        sec_mod._SECRETS_CACHE = None
        try:
            accounts = sec_mod.get_accounts()
            assert accounts.get("maoxiaoyang@cqtransit.com") == "fake-pwd-mao"   # 假凭据生效（不变）
            assert sec_mod.SECRETS_PATH != real                                  # 确实没指向真文件
        finally:
            sec_mod._SECRETS_CACHE = None
        assert snap() == before          # 真文件既没被创建、也没被改动
```

**要求**：**这不是"把断言删掉让它绿"** —— 替换后两条断言仍**都能失败**（若 `SECRETS_PATH` 被改回真文件、或测试真写了真文件，都会挂）。请在报告里说明这一点。

---

## 4. C 组：日志目录隔离（**上线自检的命门**）

**问题**：`config/settings.py:6-7`

```python
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"        # 🔴 硬编码，没有环境变量覆盖
```

而 `conftest.py:57-59,78` 只重定向了 `BOT_CONFIG_DB_PATH` / `DEDUP_DB_PATH` / `DAILY_COUNTERS_PATH` / `MAILBOT_SECRETS_PATH` —— **`LOGS_DIR` 没被重定向** ⇒ **测试的 ERROR 写进生产日志目录**（实测：某日日志 10,516 行里含 `ghost@example.com`×10、`boom`×16、`fatal-id`×8）。

**后果**：直接**架空**执行清单 §8 的 **[7]「程序错误类告警应为 0 条」** —— 只要先跑测试再看日志，[7] 就报非 0，把正常系统误判成"代码有问题"。

**改成**（各 1 行，与既有做法一致）：

```python
# config/settings.py（第 7 行）
LOGS_DIR = Path(os.environ.get("LOGS_DIR", str(BASE_DIR / "logs")))
```

```python
# tests/conftest.py（与第 57-59 行同一处）
os.environ.setdefault("LOGS_DIR", str(_TEST_DATA_DIR / "logs"))
```

**验收**：① 跑一遍全量后，`mailbots_next/logs/` 下**不再新增**含测试数据的行（给一条可复核的命令，贴原始输出）；
② 全量用例数**不减少**；③ `LOGS_DIR` 仍能被环境变量覆盖。

---

## 5. 报告格式（照这个结构交）

```
一、改动文件（按 A/B/C 三组分别列：文件 + 改了什么）
二、测试
    - 基线（开工前 HEAD / 用例数）→ 现在（用例数 / 汇总行原文 / 退出码）
    - 新增用例逐条列名
三、真锁往返（A 组 3 条，每条：撤改动作 → pytest 原始输出 → 还原后 md5）
四、grep 验收（§2.4 第 3 条 + §4 第 1 条，贴原始输出）
五、偏差与请示（有就写，没有就写"无"）
六、不确定 / 我没做的事
```

---

## 6. 背景（供理解，不必回应）

- 这是**上线阻断项**：不修的话，停旧起新后**单证驳回再也没有人收到通知**（业务功能缺失），且每封产生一条 ERROR 噪音。
- 为什么不自己实现一套：`mailbots_next` 已有统一的通知咽喉（`notify.py`）与动作管线（`decide` / `execute_action`），本组**最大化复用**它们，不新增机制。
- 为什么不"按 8-26 设计静默忽略"：洋 2026-09-22 拍板 —— 8-26 那条与**你 7-30 亲自拍板 + 生产在跑**的行为冲突，且无留言记录、Plan B 从未上线，判定为重构期的技术简化。细节见 `docs/2026-09-22-单证驳回缺口与测试耦合-分析与推荐.md`。
