# spec：草单 A/B 与台账移除 + 企微告警折叠 + schema 守卫

> 交办：芙蕾雅 ｜ 日期：2026-09-21 ｜ 执行：OpenCode ｜ 验收：芙蕾雅
> **改动分三组、互不牵连**，各自独立自测（交付后由洋按此分三个 commit）：
> - **commit 1** = 移除草单 A/B 判定与台账（§2）
> - **commit 2** = schema 守卫（§4）
> - **commit 3** = 企微通知折叠 + 日志前缀 X3（§3）—— **这一组独立，便于单独 revert**
> ⚠️ "三个 commit"是**界线**，不是让你去 commit —— **见 §0 第 2 条：你不 commit、不 push、不建分支。**

---

## 0. 交付纪律（四条铁律，先读）

1. **不瞎改已做好的内容** —— 只改本 spec 点到的位置。看到别的"顺手能改的"不要动，写进报告即可。
2. 🔴 **不要 commit、不要 push、不要建分支 —— 改完停在工作区。**
   - 与本项目此前几轮（A/B/C/D/E）的规矩一致：**你只负责落码**；
   - **分支由洋建**（用**扁平名**；本项目**建不了 `fix/xxx` 这类嵌套名**，会出 `.git` 层的事故 —— 那是他处理的事，**你不要碰 git 分支相关命令**）；
   - **提交也由洋做**（验收前的代码不进历史）；
   - **你要交付的东西**：① 改动文件清单（**按 commit 1 / commit 2 / commit 3 三组分别列**，三组分别见 §2 / §4 / §3）② 测试汇总行**原文** ③ 真锁往返的**原始输出** ④ §6 各条 grep 的原始输出；
   - 之后的顺序是：**芙蕾雅独立验收（含真锁复核）→ 洋建分支并提交**。
   - ⇒ 看到这条就不要再纠结"要不要提交"：**不提交**。若你已经提交了，**不要 revert**，直接在报告里说明（由芙蕾雅处理）。
3. **做完先自测**：跑测试 + 做"真锁"往返验证，**原始输出贴进报告**（汇总行原文，别只写"通过"）。
4. **如实报告**：偏差、不确定处、失败都写出来；**不要替人拍板**（拿不准就写"请示"）。

### 解释器与跑测试（必须照做）

- 唯一解释器：`C:\Users\Roke8x\.workbuddy\binaries\python\versions\3.13.12\python.exe`
  （PowerShell 里当命令用必须加 `&`；**不要用 `py -3.13`，它指向另一个解释器**。）
- 跑测试：`& $PY -m pytest -q -p no:cacheprovider mailbots_next/tests/`
  - **判据就是这个目录**（`mailbots_next/tests/`）。全仓合跑会带上旧 `mailbots/` 的 Windows 文件锁 warning、偶发很慢，**不作为判据**。
  - **不要传 `--basetemp`**（会触发 safe-delete 拦截导致假失败）。
- 若出现"跑到 100% 但没有汇总行、退出码 1"：先删 `%TEMP%\pytest-of-Roke8x` 再跑，**那不是测试失败**。

### 关于行号

下面的行号是**开工时的**（HEAD 见 §1）。编辑会让行号漂移 ⇒ **按代码原文定位**，别只信行号。

---

## 1. 基线（开工前先核，数字写进报告）

| 项 | 期望值 |
|---|---|
| 分支（由洋建好、你在上面落码） | **扁平名**的临时分支，从**最新 main** 拉出（不是 `dev`）。开工时记下 `git branch --show-current` 与 `git rev-parse --short HEAD` 贴进报告 |
| `mailbots_next/tests/` 用例数 | **298** |
| 代码自建的表（`mailbots_next/` 内 `CREATE TABLE`） | **5 张**：`dedup`、`error_queue`、`bot_config`、`forward_log`、`bounce_handled` |
| 代码引用的表（SQL 里出现的） | **9 个**：上面 5 张 + `forwarded_drafts`（本次要消灭）+ `records`、`tracing_log`、`tracing_snapshot`（**网页端的表**，见 §4） |

**预期改动文件**：

```
mailbots_next/core/extractors/draft.py      （组1）
mailbots_next/serve.py                      （组1）
mailbots_next/core/notify.py                （组1 删死方法 + 组2 折叠）
mailbots_next/config/settings.py            （组1 删常量 + 组2 加配置）
mailbots_next/config/__init__.py            （组1 删导出 + 组2 加导出）
mailbots_next/config/provider.py            （组1 删快照字段）
mailbots_next/README.md                     （组1 数据目录行 + 技术债登记）
mailbots_next/core/log.py                   （组2 顺带，见 §3.4）
mailbots_next/tests/conftest.py             （组1）
mailbots_next/tests/test_core.py            （组1）
mailbots_next/tests/test_inbound.py         （组1）
mailbots_next/tests/test_notify_email.py    （组1）
mailbots_next/tests/test_p1_coverage.py     （组1）
mailbots_next/tests/test_poll_uid.py        （组1）
mailbots_next/tests/test_round5.py          （组1）
mailbots_next/tests/test_bounce_e2e.py      （组1）
mailbots_next/tests/test_bounce_rev4.py     （组1）
mailbots_next/tests/test_bounce_rev42.py    （组1）
mailbots_next/tests/test_h2_h3.py           （组1）
mailbots_next/tests/test_notify_collapse.py （组2，新）
mailbots_next/tests/test_schema_guard.py    （组3，新）
```

⚠️ **不要改 `AGENTS.md`、不要改 `docs/` 下的任何文件**（那些由芙蕾雅负责）。

---

## 2. commit 1：草单 A/B 判定与 `forwarded_drafts` 台账**一起移除**

### 2.1 背景（供理解，不是让你判断对错）

草单分 A（新草单）/ B（更新草单）两类。B 的判据是"这个客编数字段是否出现在 `forwarded_drafts` 台账里"。
而这个台账 **生产代码从来没有建过表、也没有写过** —— 只有测试自己建 ⇒ `SELECT code_num FROM forwarded_drafts` **每封草单必抛 `no such table`** ⇒ 每封草单一条 ERROR + 一条企微程序告警（这就是停旧起新时告警刷屏的根因）。

**为什么要删而不是"建空表"或"补写入"**：
- 台账的语义本身不成立 —— 机器人**看不到人工转发**（不监控任何「已发送」文件夹），所以它永远无法知道"客户手上是什么版本"；
- 因此"恒判 A"本身就是**正确**的语义（既定方向：**宁可漏标，绝不误标**）；错的只是"它靠一张不存在的表来实现"；
- 删掉 ⇒ 刷屏源头消失、少一条读不存在表的路径、少一个死方法。

### 2.2 `mailbots_next/core/extractors/draft.py`

| 位置 | 改动 |
|---|---|
| `category = "B" if code_num and code_num in self._get_draft_nums() else "A"` | 改成 `category = "A"`，并**加一行注释**说明：台账已移除（2026-09-21）；恒 A = 永不误标；重新启用需按 README 技术债 X4 的三项前置单独立项 |
| `def _get_draft_nums(self)` 整个方法（含它的 `try/except`、`mkdir`、`sqlite3.connect`、`send_program_error`） | **整段删除** |
| import 里的 `OPS_OWNER_EMAIL` | 删除（**只被 `_get_draft_nums` 用**，已核） |
| import 里的 `from mailbots_next.core.notify import get_notifier` | 删除（**只被 `_get_draft_nums` 用**，已核） |

**必须保留**（别顺手删）：
- `_parse_ids()` 与它的返回值 —— `code_num` 仍要喂给 `draft_code_num=code_num`；
- `CODE_NUM_RE`、`DRAFT_CATEGORIES`、`UPDATE_KEYWORDS`、`ENC_PDF_RE`、`BOX_PDF_RE` 全部保留；
- `DRAFT_CATEGORIES`（`config/types.py`）**不动** —— 它是"合法取值白名单"，不是"会产生 B"的承诺，改它会影响 `draft.py` 的日志判定分支。**也不要删 `types.py` 里的 "B"**。

### 2.3 `mailbots_next/serve.py`

`if email_type == "draft" and row.draft_category == "B": ... else: send_forwarded(...)` —— **删掉 B 分支，一律 `send_forwarded`**。

⚠️ **外层的 `if detail == "forwarded" and decision.action == "forward":` 条件必须保留**（"组转发"的那行不额外发通知 —— 那是既有设计，别动）。改完 `elif decision.action == "alarm"` / `pending` 的链式结构要保持正确。

### 2.4 `mailbots_next/core/notify.py`：删 `send_draft_update`

删掉上面那处后它就是**死代码**（已核：生产调用点只有 `serve.py` 那一处）。
⚠️ **本次只删它**。`notify()` / `send_alarm` / `send_pending` / `send_forwarded` / `send_program_error` / `send_config_missing` / `send_digest` / `_fallback_owner_email` **全部不动**（§3 会动 `notify()`，但那是 **commit 3**，**本组不要顺手动**）。

### 2.5 配置清理（三处，必须一起清，否则导入报错）

| 文件 | 改动 |
|---|---|
| `mailbots_next/config/settings.py` | 删 `DRAFT_NUMS_DB_PATH = Path(...)` 那一行 |
| `mailbots_next/config/__init__.py` | 删 `from .settings import (...)` 里的 `DRAFT_NUMS_DB_PATH`；删 `__all__` 里的 `"DRAFT_NUMS_DB_PATH"` |
| `mailbots_next/config/provider.py` | 删顶部 import 里的它；删 `ConfigSnapshot` 的字段 `draft_nums_db_path: str`；删 `snapshot()` 里的 `draft_nums_db_path=str(DRAFT_NUMS_DB_PATH),` |

（已核：该字段**没有任何消费者**，`grep config.draft_nums_db_path` = 0；删掉不会影响启动日志或接口。）

### 2.6 测试适配（逐文件，别漏）

| 文件 | 改动 |
|---|---|
| `tests/conftest.py` | 删 `os.environ.setdefault("DRAFT_NUMS_DB_PATH", ...)` 那一行 |
| `tests/test_core.py` | ① 数据文件映射表里删 `("DRAFT_NUMS_DB_PATH", "draft_nums.db")` 项；② 删 `_seed_draft_nums()` 辅助函数；③ **按下节改造两条 B 用例**；④ `test_draft_A_sends_forward_notice` 里的 `send_draft_update.assert_not_called()` 删掉（方法已不存在，断言失去意义） |
| `tests/test_inbound.py` | 删 `DRAFT_NUMS_DB_PATH` 的 import 与两处清理列表里的它；删 `notifier.send_draft_update.return_value = True` 那一行 |
| `tests/test_notify_email.py` | 删 `test_send_draft_update` 用例 |
| `tests/test_p1_coverage.py` | 删 import 与**两处**清理列表里的它 |
| `tests/test_poll_uid.py` | 删 import 与清理列表里的它 |
| `tests/test_round5.py` | 删 import 与清理列表里的它 |
| `tests/test_bounce_e2e.py` / `test_bounce_rev4.py` / `test_bounce_rev42.py` / `test_h2_h3.py` | 各删数据文件映射元组里的 `("DRAFT_NUMS_DB_PATH", "draft_nums.db")` 项 |

⚠️ 删 import 时注意：这些文件里 `from mailbots_next.config import (...)` 是**多行括号列表**，删一项别留下悬空逗号导致语法错误；删完后每行都要跑一遍 import 冒烟。

### 2.7 🔴 两条 B 用例要**改造**，不要整条删

**(a) `test_draft_category_B_update_detected` → 改造成"台账存在也仍判 A"的真锁**

原用例是"seed 台账 ⇒ 期望 B"。**新用例要做相反的事**：在临时目录里**真的建出一张 `forwarded_drafts` 表并塞入该客编数字段**，然后断言草单**仍然被判为 `A`**。
用例名建议 `test_draft_category_stays_A_even_if_ledger_exists`。
> 这条锁的价值：防止将来有人"把台账加回来"——加回来这条必挂。

**(b) `test_draft_B_forward_has_no_extra_note_and_sends_update_notice` → 改造成 A 场景，**保留**它的两条关键断言**

这条用例里真正有价值的不是 B，而是它顺手锁住了**客户可见文案的纯净铁律**：
```python
assert str(sent_msg["Subject"]) == subject      # 主题一字不改（没加【草单更新】之类前缀）
assert "作废" not in sent_msg.as_string()        # 正文没有"此版本作废"这类自撰文案
```
⇒ **这两条断言必须保留**（洋 2026-09-17 定的规则：客户可见的正文/主题不得有自撰文案）。把用例改成 A 场景（不 seed 台账），把 `send_draft_update.assert_called_once()` 改成 `send_forwarded.assert_called_once()`，其余保持。用例名建议 `test_draft_forward_subject_and_body_have_no_added_text`。

### 2.8 新增一条锁：**草单分类不碰任何 DB**

新用例（放 `test_core.py` 的 `TestDraftCategories` 里）`test_draft_extraction_never_opens_a_db`：
- 在调用 `DraftExtractor().extract(msg, parsed)`（或等价的 `extract_email(EmailType.DRAFT, raw)`）**期间**，把 `sqlite3.connect` 换成一个"一被调用就 `AssertionError`"的函数；
- 断言：**没有异常**、且分类结果为 `"A"`。
- 目的：锁死"草单提取路径不再触碰任何数据库"这个不变量（比"断言某张表不存在"更本质）。

### 2.9 `mailbots_next/README.md`（commit 1 的文档部分）

1. `data/` 目录说明里**去掉 `draft_nums.db`**；
2. 技术债表**追加一行 `X4`**：

> **「草单更新/作废」（B 类）重新启用需要三项前置，缺一不可**：① 台账（记录"机器人转发过什么"）② **内容指纹**（判断"和上一版是否真的不同"——注意草单文件名自带**秒级时间戳**，同票重发文件名必然不同，而 md5 单向可靠：相同⇒绝对没变，不同⇒可能只是重新导出，故需单独设计）③ **人工转发的可见性**（机器人不监控「已发送」文件夹 ⇒ 这是语义不成立的根因）。
> **现状 = 恒判 A（新草单）**，这是**有意保留的正确语义**（既定方向：宁可漏标，绝不误标），**不要当缺陷修**。

---

## 3. commit 3：企微通知**折叠**（防同类错误刷屏）

### 3.1 目标与硬约束

- 目标：**短时间内的同类错误只发首条 + 一条汇总**，把"每秒数条"压成"每分钟一条"；
- 🔴 **不得丢信息、不得掩盖问题**：**日志仍逐条记录**、**计数逻辑完全不受影响**、**不同种类的错误绝不合并**；
- 🔴 **只折叠 `program_error`（程序错误）**。`alarm` / `pending` / `forwarded` / `draft_update` / `digest` / `config_missing` **一律不折叠**（`alarm`/`pending` 每条对应不同业务对象，折叠会丢信息）。

### 3.2 配置（`mailbots_next/config/settings.py` + `config/__init__.py`）

```python
# 同类程序错误的通知折叠窗口（秒）：窗口内同类只发首条，窗口结束时补发一条"共 N 次"。
# 0 = 关闭折叠（逐条发送）。<0 或非法值 ⇒ WARN + 回落 60。
NOTIFY_COLLAPSE_WINDOW_SEC = int(os.environ.get("NOTIFY_COLLAPSE_WINDOW_SEC", "60"))
```
- 照 `INGEST_POLL_SEC` 的既有写法处理非法值（WARN + 回落），**不要抛异常**；
- 在 `config/__init__.py` 的 import 列表与 `__all__` 里**各加一行**；
- **不要**加进 `provider.py` 的 `ConfigSnapshot`（那是运行期快照，本项不是运行期开关）。

### 3.3 实现位置与结构（`mailbots_next/core/notify.py`）

**目的：最小改动**。把 `notify()` 的**原有函数体原样搬**到新方法 `_send_now(notify_type, recipients, content, error_id)`（**一行逻辑都不改**），`notify()` 变成一个薄薄的"折叠闸门"。

```
notify(notify_type, recipients, content, error_id):
    if notify_type not in _COLLAPSIBLE_TYPES or window <= 0:
        return self._send_now(notify_type, recipients, content, error_id)   # 行为与现在完全一致

    _flush_expired_windows()                    # 先把过期的汇总补发出去
    fp = _fingerprint(notify_type, content)
    now = _now()
    with _collapse_lock:
        st = _collapse_state.get(fp)
        if st is None:
            _collapse_state[fp] = {"window_start": now, "count": 1, "recipients": list(recipients),
                                   "notify_type": notify_type, "content": content, "error_id": error_id}
            send_now = True
        else:
            st["count"] += 1                    # 窗口内同类 ⇒ 只累加
            send_now = False
    if send_now:
        return self._send_now(notify_type, recipients, content, error_id)
    _log.info(f"Notify collapsed | type={notify_type} | fp={fp} | count={st['count']}")
    return True                                 # 折叠 ≠ 失败：首条已立即发出
```

**硬性要求**：

1. 🔴 **首条必须立即发出，绝不延迟**（告警不能等窗口结束才发）。
2. 🔴 **`_flush_expired_windows()` 补发汇总**：对每个"已过期（`now - window_start >= window`）且 `count > 1`"的指纹，**用当初的收件人**发一条汇总，正文 = 原正文 + 追加一行：
   `（同类型错误最近 <window> 秒共发生 <count> 次，已折叠 <count-1> 条）`
   然后从 state 里删除该指纹。**汇总走 `_send_now()` 直发**（不进折叠判定 ⇒ 防递归）。
3. 🔴 **指纹必须把 `[error_id=xxxx]` 归一化掉** —— **这条最容易写错**：`log.error()` 每次返回**随机 uuid**（`core/log.py:78` `str(uuid.uuid4())[:8]`），`send_program_error` 会把它写进正文；**如果指纹直接用原文，就永远不会折叠**（实测事故里每条 error_id 都不同）。
   `_normalize_content(content)` 只做两件事：① 去掉 `[error_id=…]` 片段 ② 把连续数字替换成 `#`（时间戳/计数/箱号段会变）。
   `_fingerprint(notify_type, content) = f"{notify_type}|{_normalize_content(content)}"`。
   ⚠️ **不要归一化到"只看类型"**：不同来源的错误文本不同（例：`no such table: xxx` vs `SMTP timeout`），归一化后天然不同 ⇒ **不同问题不会被合并**。归一化只针对"每次必然变化的 token"。
4. **锁的作用域**：`with _collapse_lock` **只包住 state 的读写**；**网络发送必须在锁外**（不能持锁做 IO，否则 12 条轮询线程会互相阻塞）。
5. **并发正确性**：多线程同时首见同一指纹时，**只能有一个线程发出**（其余按"窗口内"处理）。
6. **state 有上界**：`_collapse_state` 最大条目数设上限（如 500）；超出时把"最早窗口"的汇总补发掉并清理。防内存无界。
7. **时钟可注入**：窗口判断用模块级 `_now()`（内部 `return time.monotonic()`），**测试靠 monkeypatch `_now` 推进时间，不许用 `sleep` 写测试**。
8. **`reset_notifier()` 里一并清空 `_collapse_state`**（测试隔离；注释说明这是为测试）。
9. **返回语义**：折叠（未发送）时返回 `True`；`_send_now` 的返回值语义**完全不变**。
10. **日志**：折叠时打一条 `info`（含 type / 指纹 / 计数）；**不调用 `log_notify`**（没发生真实发送，记 success 会失真、记 failure 会误报）。
11. **`notify_failed` 计数不受折叠影响**：真实发送失败照旧 `+1`；折叠本身**不计数**。

### 3.4 `mailbots_next/core/log.py`（commit 3 顺带，一处小改）

`get_logger(name)` 目前是**单例**且忽略传入的 `name` ⇒ 所有模块的日志前缀都挂着"第一个调用者"的名字（例：`wecom_notify` 的报错显示成 `core.dedup:`），排查时容易误认来源。
本次**顺手修掉**（技术债 `X3`）：
- 改成**按 `name` 缓存的多实例**（`dict` + 锁），同一个 `name` 仍复用同一实例（保持"不重复建 handler"的初衷）；
- ⚠️ **不许改变日志格式、输出目标、级别**（只让前缀反映真实调用模块）；
- 改完复核既有日志相关断言（若有用例断言了某个固定前缀，按键改；**不许删断言**）。
- 顺带把 README 技术债表那行 `X3` 标成"已修（2026-09-21）"，别留"下批修"的过期说法。

> 如果这一处改完发现影响面比预期大（例如有大量用例依赖固定前缀），**停下来写进报告**，不要硬改。

### 3.5 用例（新文件 `tests/test_notify_collapse.py`，全部用假 HTTP 桩，**不打真企微**）

| # | 用例 | 断言要点 |
|---|---|---|
| 1 | 首条立即发 | 1 次 HTTP 请求，发生在窗口内（没等窗口结束） |
| 2 | 窗口内同类 10 条 | **只发 1 次**；`_log.info` 记了 10 次 |
| 3 | 窗口过期后再来一条 | **补发 1 条汇总 + 当前 1 条** ⇒ 共 2 次请求 |
| 4 | 汇总正文内容 | 含"共发生 10 次"与"已折叠 9 条"，且**原正文仍在** |
| 5 | **不同 detail 不合并** | 两种不同错误各发 1 次（共 2 次请求） |
| 6 | 🔴 **`error_id` 不同、detail 相同 ⇒ 仍合并** | 10 条只发 1 次（**专锁 §3.3 第 3 条的归一化**） |
| 7 | 非折叠类型 | `alarm` / `pending` / `forwarded` / `digest` 各发 N 次，**一次都不折叠** |
| 8 | 关闭折叠 | `NOTIFY_COLLAPSE_WINDOW_SEC=0` ⇒ 10 条发 10 次 |
| 9 | 计数不受影响 | 折叠场景下 `notify_failed` 不增；真实发送失败（桩返回 500）时照常 `+1` |
| 10 | 多线程 | 10 线程并发同一指纹 ⇒ **总请求数 = 1**（锁正确） |
| 11 | state 上界 | 造 600 个不同指纹 ⇒ 条目数 ≤ 500，且没抛异常 |

### 3.6 真锁（必须自己做，把原始输出贴报告）

1. 把 `_normalize_content` 改成**不清理** `[error_id=…]` ⇒ **用例 6 必挂**；
2. 把 `_COLLAPSIBLE_TYPES` 改成含 `"alarm"` ⇒ **用例 7 必挂**；
3. 把 `_flush_expired_windows()` 改成空函数 ⇒ **用例 3 / 4 必挂**。

每次撤改后**还原并核对 md5 一致**（⚠️ 用**二进制**方式读写信文件：Python 文本模式会把 LF 转 CRLF，会让"还原后 md5 不一致"变成假警报）。

---

## 4. commit 2：schema 守卫（防"代码读一张没人建的表"复发）

### 4.1 背景（供理解）

本次事故的**真正帮凶**是测试盲区：`test_core.py` **自己建了** `forwarded_drafts` 表（`test_manifest_spec.py` 也自己建 `import_batch`）⇒ 于是"生产从未建这张表"这种缺陷，测试全绿也照样上线。要新增两类"照不出假绿"的守卫。

### 4.2 新文件 `tests/test_schema_guard.py`

**测试 A：`test_every_referenced_table_is_created`**

- 扫 `mailbots_next/**/*.py`（**排除 `tests/`**）的源码文本，抽取：
  - 引用的表名：正则 `(?:FROM|INTO|JOIN)\s+([A-Za-z_][A-Za-z_0-9]*)` 与 `UPDATE\s+([A-Za-z_][A-Za-z_0-9]*)\s+SET`（**大小写不敏感**，**多行字符串也要能命中** ⇒ 对整份文件文本做 `finditer`，不要逐行）；
  - 建的表名：`CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z_0-9]*)`。
- 断言：`referenced - created_in_mailbots_next - _EXTERNAL_TABLES == set()`（失败时把差集打进断言消息）。
- `_EXTERNAL_TABLES` **必须写明"谁建的"**，且**测试要验证这个豁免本身**：对每张外部表，断言它所声明的文件里确实存在 `CREATE TABLE IF NOT EXISTS <表名>`：
  ```python
  _EXTERNAL_TABLES = {
      "records": "app.py",              # 网页端 app.py:247
      "tracing_log": "app.py",          # app.py:297
      "tracing_snapshot": "app.py",     # app.py:314
  }
  ```
  > 这样"外部表"这个口子不能用来藏一张谁都没建的表。
- **专锁本次修复**：断言 `"forwarded_drafts"` **不在** referenced 集合里（并且 `grep` 全 `mailbots_next/`（含 tests）**0 命中**这个表名）。

**测试 B：`test_fresh_production_init_creates_all_tables`（生产初始化冒烟）**

- **必须用 `subprocess` 起新进程**（同进程内模块已缓存 ⇒ 不能代表首次启动）；用 pytest 的 `tmp_path`（**Windows 原生路径**，别用 shell 的 `/tmp`）。
- 子进程环境：`MAILBOT_MODE=test`，并把 `BOT_CONFIG_DB_PATH` / `DEDUP_DB_PATH` / `DAILY_COUNTERS_PATH` / `IMAP_STATE_PATH` / `YXO_DB_PATH` **全部指到 `tmp_path`**。
- 子进程只做一件事：`import mailbots_next.core.dedup, mailbots_next.core.store`
  （**这就是真实的生产初始化路径** —— 建表发生在模块导入时：`dedup.py` 尾部 `init_db()`、`store.py` 尾部 `init_bot_config_db()` / `init_forward_log()` / `init_bounce_handled()`。**不要**在测试里去调这些 `init_*`，那又会变成"测试自己建表"。）
- 断言：
  1. 子进程退出码 = 0；
  2. 用 `sqlite3` 打开 `tmp_path` 下的库，收集**实际存在的表**（含 `sqlite_sequence`，所以用**包含**而不是相等），断言它**包含** `{dedup, error_queue, bot_config, forward_log, bounce_handled}`；
  3. 断言"代码会读、但既不在本地建表清单、也不是外部表"的表**为空**（把测试 A 的判据在**真实初始化结果**上再跑一遍）。

> ✅ 这套配方我已实测过：子进程 import 后产出 `bot_config.db -> [bot_config, bounce_handled, forward_log, sqlite_sequence]`、`dedup.db -> [dedup, error_queue, sqlite_sequence]`，退出码 0。

### 4.3 真锁

1. **把 `_get_draft_nums` 的读取逻辑加回来**（在 `draft.py` 里加一行 `sqlite3.connect(...).execute("SELECT code_num FROM forwarded_drafts")`）⇒ **测试 A 必挂**（`forwarded_drafts` 既不在建表清单、也不在外部表）；
2. **把 `init_bounce_handled()` 那行模块级调用注释掉** ⇒ **测试 B 必挂**（`bounce_handled` 被引用但实际没建出来）。

---

## 5. 不需要做的事（红线）

- ❌ **不要"补写入"台账**（`INSERT INTO forwarded_drafts`）—— 语义不成立（机器人看不到人工转发），已明确否决；
- ❌ **不要"建一张空表让它别报错"**；
- ❌ 不要新增/修改任何**客户可见**的邮件主题或正文文案（铁律：客户可见内容不得有自撰文案）；
- ❌ 不要动 `mailbots/`（旧系统）任何文件；
- ❌ 不要动 `AGENTS.md`、`docs/`；不要动 `scripts/`；
- ❌ 不要动 `mailbots_next/data/` 下的运行期文件（gitignored，本机那个 0 字节的 `draft_nums.db` 由芙蕾雅/运维清理）；
- ❌ 不要改 `settings.py` 里 `INGEST_POLL_SEC` 的上限逻辑（`serve.py` 的 `POLL_SECS_MAX` 是本轮之前刚验收过的，别碰）；
- ❌ 不要为了让测试通过而**删断言 / 改判据**；如果某条既有用例因本改动必须调整，**在报告里逐条列出"原断言 → 新断言 + 为什么"**。

---

## 6. 验收判据（芙蕾雅按这个验）

1. `& $PY -m pytest -q -p no:cacheprovider mailbots_next/tests/` ⇒ **0 failed**，并把**汇总行原文**贴进报告；用例数应为 `298 + 新增`（贴出的数字要与实际新增对得上 —— 我会核对"新增数 == 我数的增量"，**不许用删旧用例凑绿**）。
2. `grep -rn "forwarded_drafts" mailbots_next/` ⇒ **0 命中**（含 tests）。
3. `grep -rn "DRAFT_NUMS_DB_PATH\|draft_nums" mailbots_next/ --include=*.py` ⇒ **0 命中**。
4. `grep -rn "wecombot" mailbots_next/ --include=*.py` ⇒ **0 命中**（回归锁：C 组的成果不许回退）。
5. `grep -rn "_get_draft_nums\|send_draft_update" mailbots_next/ --include=*.py` ⇒ **0 命中**。
6. §3.6 三条真锁 + §4.3 两条真锁：**每条都贴出"撤改后的 pytest 输出行 + 还原后 md5 一致"**。
7. **改动文件归属清晰、无多余文件**：`git status` 里所有改动/新增文件都必须在 §1 清单内，且**能干净地归到 commit 1 / 2 / 3 三组之一**（报告里按三组分列；**不要产生"横跨多组"的文件** —— 唯一允许的例外是 `mailbots_next/README.md`，它同时含 commit 1 的登记行与 commit 3 的 X3 标记，**在报告里说明即可**）。
   > ⚠️ 注意：这一条**只要求"改动清爽、可分组"**，**不要求你 commit** —— 提交由洋做（§0 第 2 条）。

---

## 7. 报告格式（照这个写，别省）

```
一、改动文件（按 commit 1 / commit 2 / commit 3 三组分列；每行：文件 + 改了什么 + 属于哪一组）
二、测试
    - 基线（开工前 `git branch --show-current` / HEAD / 用例数）→ 现在（用例数 / 汇总行原文 / 退出码）
    - 新增用例逐条列名
三、真锁往返（5 条，每条：撤改动作 → pytest 原始输出 → 还原后 md5）
四、grep 验收（§6 第 2–5 条，贴原始输出）
五、`git status --short` 原始输出（证明没有多余文件）
六、偏差与请示（有就写，没有就写"无"）
七、不确定 / 我没做的事
```

> 交付=**报告 + 停在工作区的改动**。**不要 commit、不要 push、不要建分支**（详见 §0 第 2 条）。

---

*本 spec 的背景与行号均经芙蕾雅独立核实（2026-09-21）。有任何与代码不符处，**先停下来问**，不要按 spec 猜着改。*
