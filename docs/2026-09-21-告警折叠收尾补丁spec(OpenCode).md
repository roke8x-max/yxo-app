# spec：告警折叠收尾补丁（3 处小修 + 1 处行为修正）

> 交办：芙蕾雅 ｜ 日期：2026-09-21 ｜ 执行：OpenCode ｜ 验收：芙蕾雅
> 上一轮：`docs/2026-09-21-草单A-B移除与告警折叠spec(OpenCode).md`（已交付、我已验收通过）；本文件是它的**收尾补丁**。

---

## 0. 交付纪律（同上一轮，先读）

1. **只改本文件点到的 4 个位置**，别顺手改别的；看到别的要改的地方写进报告即可。
2. 🔴 **不要 commit、不要 push、不要建分支**（分支由洋建，他用扁平名；**你不要碰任何 git 分支命令**）。**改完停在工作区**。
3. **做完先自测**：跑测试 + 做真锁（见 §3），**原始输出贴进报告**。
4. **如实报告**：偏差与不确定处都写出来，不要替人拍板。
5. 解释器与跑测试命令同上一轮：唯一解释器 `…\3.13.12\python.exe`；`& $PY -m pytest -q -p no:cacheprovider mailbots_next/tests/`；**不要传 `--basetemp`**。

### 🗂 关于提交分组（这条很重要，别搞乱）

**本轮 3 处改动全部属于"折叠组"（上一轮的 commit 3）**，不新增组、不动 commit 1 / commit 2 的任何内容。
⇒ 你要交付的清单按老样子分三组列，但**变更只出现在 commit 3 那一组**；`git status` 里除 `notify.py` / `README.md` / `tests/test_notify_collapse.py` 外**不应出现其它文件**（除非你在报告里说明原因）。

---

## 1. 修正 1：过期汇总要能被"任何一条通知"触发（行为修正）

**问题**：上一轮 `_flush_expired_windows()` 被放在 `notify()` 的**早退之后** ⇒ 只有 `program_error` 到来时才补发过期汇总。实测：突发 10 条 → 只发首条；窗口过期后来一条 `forwarded` ⇒ **汇总不发**；要等下一条 `program_error`（可能几天后，届时文案里的"最近 60 秒"已经失真）。
（这个位置是**芙蕾雅 spec 伪代码写错**的，不是你的实现问题。）

**改法**：在 `notify()` 里把 `self._flush_expired_windows()` **提到那个早退之前**，即：

```python
def notify(self, notify_type, recipients, content, error_id=None):
    # 先把过期窗口的汇总补发掉（对非折叠类型同样执行：它只清理"已过期"的窗口，
    # 不会为当前这条建立任何 state，也不会递归）
    self._flush_expired_windows()
    if notify_type not in _COLLAPSIBLE_TYPES or _get_collapse_window() <= 0:
        return self._send_now(notify_type, recipients, content, error_id)
    ...
```

**约束**：
- `_flush_expired_windows()` 内部**已有 window<=0 直接返回的保护**，**不要动它的内部逻辑**；
- **不得递归**：汇总必须继续走 `_send_now()` 直发；
- **不得为当前这条建立 state**（flush 只读/删过期项）；
- 窗口为 0（关闭折叠）时行为**必须与现在完全一致**（照样全发、无汇总）。

## 2. 修正 2：汇总文案去掉"最近 N 秒"（防迟到失真）

现在两处（**注意是两处**：`_flush_expired_windows()` 里、以及 `notify()` 里**驱逐**（超上界）那处）都拼：

```python
f"\n（同类型错误最近 {window} 秒共发生 {est['count']} 次，已折叠 {est['count'] - 1} 条）"
```

**改成不含时间断言的措辞**（两处统一用同一句、建议抽成一个模块级小函数避免再次不同步）：

```python
f"\n（同一批同类错误共发生 {n} 次，已折叠 {n - 1} 条）"
```

## 3. 修正 3：用例 11（state 上界）改成**真的能挂**

现在的写法 `f"unique detail number {i} xyz"` **只差数字** ⇒ 归一化后**全部落到同一个指纹** ⇒ state 恒为 1 ⇒ `assert len(state) <= 500` **永远成立**（实测：把 `_COLLAPSE_MAX_ENTRIES` 改成 `500000`，该用例照样通过）。

**改法**：
- detail 用**字母区分**（保证归一化后仍是 600 个不同指纹），例如：

```python
def _unique_kind(i):
    a = chr(ord("a") + i % 26)
    b = chr(ord("a") + (i // 26) % 26)
    c = chr(ord("a") + (i // 26 // 26) % 26)
    return f"kind-{a}{b}{c}"

for i in range(600):
    n.send_program_error(OPS_OWNER_EMAIL, f"e{i}", _unique_kind(i))
assert len(notify_mod._collapse_state) == 500      # ← 用 ==，不是 <=
```
- ⚠️ 注意 error_id 也别只差数字（同理），用 `f"e{i}"` 里含字母/数字都行，但 **detail 必须是关键**；
- 断言用 **`== 500`**（不是 `<=`）：这样"上界被删掉"会变成 `600` ⇒ **必挂**。
- 顺带：**在用例里说明**为什么必须是字母区分（否则将来有人"简化"回去，锁又失效了）。

## 4. 修正 4：两处零风险小瑕疵

| 位置 | 改动 |
|---|---|
| `tests/test_notify_collapse.py` 里 `test_collapse_does_not_touch_failure_counter` 内的残留变量 | **删掉** `reset_notifier_state_only = False` 那一行（无用变量） |
| `mailbots_next/README.md` 技术债 **X3** 行的"现象"列 | 该列仍写着"修它要动…（`log.py` 的 `_logger_instance`）"，而标识符已改名 ⇒ 在句末补一句：**"（该单例已改名为 `_logger_instances`，本条已修，见右栏）"**。**只动这一句，别改别的字** |

---

## 5. 必须新增/更新的用例与真锁

### 5.1 新用例（`tests/test_notify_collapse.py`）

**`test_expired_window_flushed_by_any_notify_type`**（这是修正 1 的锁，上一轮没有）：
- 同类错误 10 条（只发 1 条）→ 时钟推进 > 窗口 → **发一条 `forwarded`**（非折叠类型）
- 断言：**`forwarded` 那条照发**，**且**补发的汇总也发了 ⇒ 总请求数 = 3（首条 + forwarded + 汇总）
- 断言汇总正文字符串里 **不含** "最近"、**含** "同一批同类错误共发生 10 次"

**更新 `test_collapse_state_bounded`**：按 §3 改成字母区分 + `== 500`。

**更新 `test_summary_body_keeps_original_and_counts`**（若它断言了旧文案）：文案断言改成新版措辞。

### 5.2 真锁（必须自己做，贴原始输出 + 还原后 md5）

1. **把 `_flush_expired_windows()` 挪回早退之后** ⇒ **`test_expired_window_flushed_by_any_notify_type` 必挂**；
2. **把上界改成 500000** ⇒ **`test_collapse_state_bounded` 必挂**（这正是它以前拦不住的情况）。

> ⚠️ **每个文件各建一份备份**（别共用一份），且**还原后必须核 md5**（文本模式写文件会把 LF 变 CRLF，用二进制读写）。

---

## 6. 不需要做的事

- ❌ 不要动 `AGENTS.md` / `docs/` / `scripts/` / `mailbots/` / `mailbots_next/data/`；
- ❌ 不要改上一轮已验收的任何其它内容（`draft.py` / `serve.py` / `store.py` / `provider.py` / 其它测试文件）；
- ❌ 不要顺手动 `_COLLAPSE_MAX_ENTRIES` 的**取值**（仍是 500）；
- ❌ 不要改折叠的判定口径（只折叠 `program_error`、不同指纹不合并、日志逐条、计数不变）。

---

## 7. 判据与报告

1. `& $PY -m pytest -q -p no:cacheprovider mailbots_next/tests/` ⇒ **0 failed**，贴**汇总行原文**；用例数应为 `311 + 1`（新增 1 条）——**若数字不对，按实际贴出并说明**。
2. 两条真锁的原始输出 + 还原后 md5。
3. `git status --short` 原始输出（确认只有 `notify.py` / `README.md` / `tests/test_notify_collapse.py` 被动过）。
4. 报告格式同上一轮（改动文件按组列 / 测试 / 真锁 / grep / 偏差与请示 / 不确定项）。

---

*本补丁只有 4 处改动，但其中 2 处是"锁的锁"（让测试真能拦住回归），请照 §5.2 老实做真锁。*
