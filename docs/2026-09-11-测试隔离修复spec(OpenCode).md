# 测试隔离修复 spec（OpenCode）—— 消除 test_bounce.py 全局态泄漏

- 出具人：芙蕾雅 日期：2026-09-11 分支：dev
- 前置验收报告：`docs/2026-09-11-NDR增强-验收报告.md`
- 性质：**纯测试代码修复**。严禁改动任何生产代码。

---

## 0. 四条铁律（同既往）

1. 不瞎改已做好内容 —— 只动本 spec 指定的行，其余一律不碰。
2. 不自己 commit / push。
3. 做完先自测：全量套件必须 **0 failed**（当前 5 failed）。
4. 如实报告，不替拍板；实测数字原样贴出。

---

## 1. 问题现象（实测复现，非推测）

全量套件当前结果：**303 passed, 5 failed**。5 个失败：

```
FAILED mailbots_next/tests/test_p1_coverage.py::TestTypeIdentification::test_unclassified_goes_config_missing_only
FAILED mailbots_next/tests/test_p1_coverage.py::TestSweeper::test_max_retry_escalates_with_single_E1
FAILED mailbots_next/tests/test_p1_coverage.py::TestSweeper::test_flush_and_notify
FAILED mailbots_next/tests/test_p1_coverage.py::TestCredentials::test_missing_password_goes_E2
FAILED mailbots_next/tests/test_round5.py::TestNotifyFallback::test_notify_unknown_recipient_escalates_to_owner_email
```

**5 个失败的断言形态完全一致**，全部是同一个值错位：

```
E   AssertionError: assert 'ops@example.com' == 'maoxiaoyang@cqtransit.com'
E   AssertionError: assert ['ops@example.com'] == ['maoxiaoyang@cqtransit.com']
```

即：运行期 `OPS_OWNER_EMAIL` 变成了 `ops@example.com`，而用例期望默认值 `maoxiaoyang@cqtransit.com`。

## 2. 根因（唯一，已实证）

`OPS_OWNER_EMAIL` 在 `mailbots_next/config/settings.py:33` 于**模块导入时**读取一次：

```python
OPS_OWNER_EMAIL = os.environ.get("OPS_OWNER_EMAIL", "maoxiaoyang@cqtransit.com")
```

而 `mailbots_next/tests/test_bounce.py:156` 的 `test_poll_bounces_requeues_on_ndr` 内，**:162-168 直接给模块全局赋值、绕过 monkeypatch、跑完不还原**：

```python
def test_poll_bounces_requeues_on_ndr(monkeypatch):
    """NDR 到达时，重新入队 error_queue 并告警。"""
    from mailbots_next.config import settings as cfg
    from unittest.mock import MagicMock, patch

    # Setup settings
    cfg.BOUNCE_MONITOR_ENABLED = True      # ← 泄漏
    cfg.BOUNCE_IMAP_USER = "test@cqtransit.com"   # ← 泄漏
    cfg.BOUNCE_IMAP_PASSWORD = "password"         # ← 泄漏
    cfg.BOUNCE_IMAP_SERVER = "imap.example.com"   # ← 泄漏
    cfg.BOUNCE_IMAP_PORT = 993                    # ← 泄漏
    cfg.BOUNCE_FOLDER = "INBOX"                   # ← 泄漏
    cfg.OPS_OWNER_EMAIL = "ops@example.com"       # ← 致命：污染后续所有 OPS_OWNER_EMAIL 断言
```

**归因唯一性证据**：全仓 `ops@example.com` 只出现 3 处 —— `core/notify.py:76`（企微姓名静态映射表）、`tests/conftest.py:71`（假 secrets 的**字典值**）、`tests/test_bounce.py:168`（**唯一给 `OPS_OWNER_EMAIL` 赋值处**）。因此 `OPS_OWNER_EMAIL == "ops@example.com"` 只可能来自这一行。

**隔离性证据**：把这 5 个用例单独跑 → `5 passed in 0.73s`；把 `test_bounce.py` 放在前面一起跑 → 立刻复现 5 failed。

**性质判定**：这是**测试桩全局态泄漏**，不是生产功能缺陷。生产环境下 `OPS_OWNER_EMAIL` 只在进程启动时读取一次、由部署方按需设为真值，不存在测试这种运行时突变。

> 注：同文件的 `test_poll_bounces_dedupe_already_bounced`（:281-288）对**完全相同的 settings** 使用了正确的 `monkeypatch.setattr(settings, ...)`。本修复就是把 :162-168 改成与之写法一致。

## 3. 修复方案（P0 必做，唯一必改点）

**文件**：`mailbots_next/tests/test_bounce.py`
**范围**：仅函数 `test_poll_bounces_requeues_on_ndr`（约 :156-168）
**改法**：把 :162-168 的 7 行直接赋值，全部改为 `monkeypatch.setattr(cfg, <名>, <值>)`。该用例签名已声明 `monkeypatch` 参数，直接用即可。

改后应为：

```python
def test_poll_bounces_requeues_on_ndr(monkeypatch):
    """NDR 到达时，重新入队 error_queue 并告警。"""
    from mailbots_next.config import settings as cfg
    from unittest.mock import MagicMock, patch

    # Setup settings —— 必须经 monkeypatch，保证用例结束自动还原
    monkeypatch.setattr(cfg, "BOUNCE_MONITOR_ENABLED", True)
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_USER", "test@cqtransit.com")
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_PASSWORD", "password")
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_SERVER", "imap.example.com")
    monkeypatch.setattr(cfg, "BOUNCE_IMAP_PORT", 993)
    monkeypatch.setattr(cfg, "BOUNCE_FOLDER", "INBOX")
    monkeypatch.setattr(cfg, "OPS_OWNER_EMAIL", "ops@example.com")
```

其余代码（mock 装配、raw 报文、断言）**一律不动**。

### 明确不改（防扩大改动面）

以下是同类的"直改全局"写法，但**均无污染**，本次**不要动**（铁律 1）：

| 位置 | 写法 | 为何无害 |
|------|------|----------|
| `test_p1_coverage.py:385/391/400/404` | `sec_mod._SECRETS_CACHE = None` | 只是"重置为默认空缓存"，非设错值 |
| `test_p1_coverage.py:716/722` | `dedup_mod.DEDUP_DB_PATH = old_db` → finally 还原 | try/finally 已还原 |
| `test_core.py:912/915` | `store_mod.YXO_DB_PATH = db` → finally 还原 | try/finally 已还原 |

## 4. 加固（P1 建议，可选）

若希望**根治这一类**（不止本次一处），可在 `mailbots_next/tests/conftest.py` 增加一个 autouse fixture：每个用例开始前快照 `mailbots_next.config.settings` 模块的所有大写属性，用例结束后还原。这样任何未来对 settings 的直接赋值都会被自动兜回。

- 要求：加了之后全量套件必须仍 **0 failed**（不得引入新失败）。
- 若担心影响面，可只做 P0（单点修复）——P0 已足够让套件回到 0 failed。
- 不建议改动其它已通过的测试。

## 5. 验收标准（必须实测，贴原始输出）

1. **全量套件 0 failed**。命令（仓库根，dev 分支）：
   ```
   python -m pytest -q
   ```
   - 修复前：`303 passed, 5 failed`
   - 修复后期望：**`308 passed, 0 failed`**（若采纳 P1 且新增 fixture 用例，数字可略增，但 failed 必须为 0）
2. 单独复跑这 5 个用例也必须全过：
   ```
   python -m pytest "mailbots_next/tests/test_p1_coverage.py::TestTypeIdentification::test_unclassified_goes_config_missing_only" "mailbots_next/tests/test_p1_coverage.py::TestSweeper::test_max_retry_escalates_with_single_E1" "mailbots_next/tests/test_p1_coverage.py::TestSweeper::test_flush_and_notify" "mailbots_next/tests/test_p1_coverage.py::TestCredentials::test_missing_password_goes_E2" "mailbots_next/tests/test_round5.py::TestNotifyFallback::test_notify_unknown_recipient_escalates_to_owner_email" -q
   ```
3. **只改了测试代码**：`git status` / `git diff --name-only` 的输出中，**不得**出现 `mailbots_next/core/**`、`mailbots_next/serve.py`、`mailbots_next/config/**` 等生产文件（P1 若做，仅允许 `tests/conftest.py`）。
4. 报告里原样贴出：修复前后两次全量测试的汇总行 + `git diff --name-only` 结果。

> 环境提示：本机跑全量时请用 `--basetemp=<项目内目录>`（如 `--basetemp=./.pytest_tmp`），避免默认 `%TEMP%` 清理阶段卡住；跑完自行清理该目录。

## 6. 非目标（明确不做）

- 不改任何生产代码（`core/bounce.py`、`core/act.py`、`serve.py`、`config/*` 等）。
- 不改 `OPS_OWNER_EMAIL` 的默认值（生产默认 `maoxiaoyang@cqtransit.com` 正确，保持不动）。
- 不重构/不美化其它测试，不动 NDR 已验收通过的逻辑。
- 不 commit / push。

---
*本 spec 的根因与行号均来自 2026-09-11 实跑与读码，如有疑问先复跑 §1 命令再讨论。*
