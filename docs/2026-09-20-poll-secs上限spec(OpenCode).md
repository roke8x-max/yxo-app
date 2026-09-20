# spec：`INGEST_POLL_SEC` / `--poll-secs` 加"上限夹取"（D 组补丁）

> 交付对象：OpenCode ｜ 验收：芙蕾雅 ｜ 决策人：洋（2026-09-20 拍板）
> **背景（供理解，你不需要知道历史）**：收信通路是"短周期轮询 + UID 水位线"，轮询周期由
> `INGEST_POLL_SEC`（默认 30 秒）控制。目前它**只有下限校验、没有上限**：如果有人在环境变量或
> 命令行里把它设成 3600（1 小时），连接会长时间空闲，可能被 IMAP 服务端的空闲超时断开。
> 断开只会由"下一条命令失败"发现 ⇒ **告警最长延迟一个完整周期**。
> 为此加一个**上限夹取**，让延迟有界。

---

## 0. 一句话任务

在 `mailbots_next/serve.py` 里，对**两条入口合流之后**的最终轮询周期做 `> 1500 秒 ⇒ WARN + 夹到 1500`，
**不要**把这个校验放进 `mailbots_next/config/settings.py`（那会被 `--poll-secs` 绕过）。

---

## 1. 现状事实（都已核实，直接按此实现）

| 位置 | 现状 |
|---|---|
| `mailbots_next/config/settings.py:52-58` | `_ingest_poll_raw = int(os.environ.get("INGEST_POLL_SEC", "30"))`；`<= 0` 时 `warnings.warn(...)` **回落 30**；最终 `INGEST_POLL_SEC = _ingest_poll_raw if > 0 else 30` ⇒ **只有下限，无上限** |
| `mailbots_next/serve.py:812` | `parser.add_argument("--poll-secs", type=int, default=0, ...)` ⇒ **第二条入口**，`0` 表示"未指定" |
| `mailbots_next/serve.py:857-863` | A 组接线：`check_company_recipients_configured()`（**本任务不要动它**） |
| `mailbots_next/serve.py:865-869` | `--once` 分支：`_once_secs = args.poll_secs if (args.poll_secs and args.poll_secs > 0) else _DEF_POLL` → `poll_once(processor, poll_secs=max(1, int(_once_secs)))` |
| `mailbots_next/serve.py:872-875` | 主循环：`poll_secs = args.poll_secs if (args.poll_secs and args.poll_secs > 0) else _DEF_POLL` → `poll_secs = max(1, int(poll_secs))` → `start_pollers(...)` |
| `mailbots_next/serve.py:59` | 模块日志对象是 `_log = EmailLogger("mailbots_next.serve")` ⇒ **用 `_log.warning(...)`** |
| `mailbots_next/serve.py:474` | `_build_pollers(processor, poll_secs=0)` 被 `start_pollers` / `poll_once` 共用 ⇒ **不要改它的签名或语义** |

**为什么必须卡在"合流之后"**：`--poll-secs` 不经过 `settings.py`。只有在**两处 `args.poll_secs ... else _DEF_POLL` 算完之后**夹取，才能同时覆盖"环境变量"和"命令行"两条入口。若只在 `settings.py` 里改，`--poll-secs 3600` 会原样通过。

**为什么这不是"防漏信"**：`_poll_loop` 的连接异常分支是 `_drop` → `sleep(10)` → 回到循环顶重连**并重跑本轮**，且 UID 水位只在"处理成功 / 已入队"时推进 ⇒ 断开只会**延迟**，不会丢邮件。上限的价值是**让延迟有界**（防配置错误导致告警长时间不响）。不要在注释里写成"防漏信"。

---

## 2. 要做的改动（3 处，都在 `mailbots_next/serve.py` + 1 处注释）

### 2.1 新增常量与夹取函数（放在 `_build_pollers` 定义**之前**，约 `serve.py:474` 上方）

```python
# 轮询周期上限：25 分钟，相对常见的 30 分钟服务端空闲超时留 5 分钟余量（洋 2026-09-20 拍板）。
# 必须在两条入口（INGEST_POLL_SEC / --poll-secs）合流之后夹取 —— 放在 settings.py 会被 --poll-secs 绕过。
POLL_SECS_MAX = 1500


def _clamp_poll_secs(secs, source: str) -> int:
    """轮询周期上限夹取：超限 ⇒ WARN + 夹到 POLL_SECS_MAX。

    周期越大，连接空闲越久，越可能被服务端空闲超时断开；而断开只能由"下一条命令失败"
    发现 ⇒ 告警延迟最长一个周期。夹取让这个延迟有上界。
    （不会漏信：重连后会重跑本轮，且水位不推进，邮件仍在。）
    """
    secs = int(secs)
    if secs > POLL_SECS_MAX:
        _log.warning(
            f"poll_secs={secs} exceeds POLL_SECS_MAX={POLL_SECS_MAX} ({source}),"
            f" clamped to {POLL_SECS_MAX}"
        )
        return POLL_SECS_MAX
    return secs
```

### 2.2 `--once` 分支（`serve.py:867`）

```python
        _once_secs = args.poll_secs if (args.poll_secs and args.poll_secs > 0) else _DEF_POLL
        poll_once(processor, poll_secs=max(1, _clamp_poll_secs(_once_secs, "--once")))
```

### 2.3 主循环分支（`serve.py:874`）

```python
    poll_secs = args.poll_secs if (args.poll_secs and args.poll_secs > 0) else _DEF_POLL
    poll_secs = max(1, _clamp_poll_secs(poll_secs, "--poll-secs/INGEST_POLL_SEC"))
    start_pollers(processor, poll_secs=poll_secs)
```

> 下限 `max(1, ...)` 的语义**保持不变**；不要改动 `settings.py` 里 `<=0 ⇒ 回落 30` 的既有逻辑。

### 2.4 `settings.py` 的注释补一行（**只改注释，不动任何代码/取值**）

在 `INGEST_POLL_SEC` 上面那段注释末尾追加：

```
# ⚠️ 上限不在这里校验：--poll-secs 会绕过本模块，两条入口的夹取统一在 serve.py 合流之后
#    （POLL_SECS_MAX / _clamp_poll_secs）。
```

理由：防止将来有人"顺手在 settings 里加上限"，那会造出"env 生效、CLI 不生效"的隐性不一致。

---

## 3. 测试要求（新建 `mailbots_next/tests/test_poll_secs_clamp.py`）

**必须包含下面 6 条行为用例**（用 `monkeypatch` 捕获真实传入值，别只断言函数返回值）：

| # | 场景 | 期望 |
|---|---|---|
| 1 | `_clamp_poll_secs(3600, ...)` | 返回 `1500` 且 `_log.warning` **被调用一次**（消息含 `3600` 与 `1500`） |
| 2 | `_clamp_poll_secs(1500, ...)` / `(1501, ...)` | `1500`（不触发，边界不越界）/ `1501` ⇒ `1500` 且 WARN |
| 3 | 正常值 `30` / `60` | 原样返回、**不产生 WARN**（防"改出误报"） |
| 4 | 环境变量入口：`INGEST_POLL_SEC=3600` 经 `serve.main()` 路径 | `start_pollers` 收到的 `poll_secs` 为 **1500** |
| 5 | 命令行入口：`--poll-secs 3600` | 同上为 **1500**（**这是本任务的核心目的**） |
| 6 | `--poll-secs 0`（未指定）⇒ 用默认值；以及 `--once` 分支同样受夹取 | 默认值原样（30，若被夹说明有 bug）；`--once` 传出的值 ≤ 1500 |

**做法提示（不必照抄）**：用例 4/5 可以 `monkeypatch.setattr(serve, "start_pollers", fake)` 与 `monkeypatch.setattr(serve, "poll_once", fake)` 捕获实参，再 `monkeypatch.setattr(sys, "argv", [...])` 调 `serve.main()`；注意 `serve.main()` 会先跑 A 组的 `check_company_recipients_configured()`，测试里一并 `monkeypatch` 掉，别让它真去读库。

### 🔴 真锁要求（硬要求）

改完后，请自己**把夹取临时撤掉**（让 `_clamp_poll_secs` 直接 `return int(secs)`）跑一遍：**至少 1 条用例必须挂**。
然后还原，并核对文件 `md5` 与改动前一致。把这一次往返的**原始输出**贴在报告里。

---

## 4. 不得做的事

- ❌ 不改 `settings.py` 的任何**逻辑/取值**（只在 §2.4 指定的注释处追加两行）。
- ❌ 不改默认值 `30`、不改 `<=0 ⇒ 回落 30` 的既有语义。
- ❌ 不改 `_build_pollers` / `start_pollers` / `poll_once` 的签名或语义。
- ❌ 不把上限写成 1500 以外的值（**洋已拍板 1500**，不要"优化"成 600/900）。
- ❌ 不加新依赖、不改别的文件、不"顺手"重构。
- ❌ **不要 `git commit`、不要 `git push`**（改完停在工作区，由洋决定提交）。
- ❌ 不要在注释或日志里写"防止漏信"（事实是"限制延迟上界"，见 §1 末段）。

---

## 5. 验收判据（芙蕾雅会逐条自己跑）

1. `grep -n "POLL_SECS_MAX" mailbots_next/serve.py` 命中常量与函数；
2. `grep -n "_clamp_poll_secs" mailbots_next/serve.py` 命中定义 + **2 处调用**（`--once` 与主循环各一处）；
3. `grep -rn "POLL_SECS_MAX" mailbots_next/config/` —— **只允许命中 §2.4 要求追加的那两行注释，不得出现常量定义或调用**；`grep -rn "_clamp_poll_secs" mailbots_next/config/` 同理（只允许注释提及亦然）。本判据的本意是"**夹取逻辑不在 `config/` 里**"，**不是"字面零命中"**。
   > 📌 **2026-09-20 修正**：本条原文写的是"`POLL_SECS_MAX` **0 命中**"，而 §2.4 又要求追加含该词的两行注释 —— **两条自相矛盾，是 spec 自身的缺陷**。交付方如实申报了这处偏差、未擅自删注释（处置：**保留注释**，它正是为防"将来有人在 settings 里加上限"而写；改为按本条新表述验收）。
4. 全量 `& $PY -m pytest -q -p no:cacheprovider` **0 failed**（以你开工时采集的用例数为基线，报告里写清"基线 N → 现在 N+新增加"，**不要写死绝对值**）；
5. 真锁往返的原始输出；
6. `--poll-secs 3600` / `INGEST_POLL_SEC=3600` 两条入口都夹到 1500（用例 4/5 覆盖）。

---

## 6. 另附一件小事：README 技术债登记一行（**只登记，不实现**）

在 `mailbots_next/README.md` 的「技术债（登记，未实现）」表（表头 `| 编号 | 内容 | 为什么现在不做 | 将来什么条件下再做 |`，现有行含 `F5-1` / `X1` / `T1` / `C1` / `T2`）末尾追加一行：

```markdown
| X2 | 企微连续故障时，每次失败都记一条 ERROR（`notify_failed` 计数同时增长） | 计数与现象都在，运维照样看得见；**降频的代价是可能漏看"首次异常"**，而现在正是最需要它的时候 | 已登记不实现（洋 2026-09-20）。若将来日志被刷屏到**影响其他问题定位**，再评估"每小时最多 N 条完整 ERROR、其余降为 WARN" |
```

编号用 `X2`（与 `X1` 同类：登记的是"某个既有实现细节"，不是待做功能）。**只加这一行，别改表里其他内容。**

---

## 7. 交付要求

- 改动文件清单 + 每个文件改了什么（一行一句）；
- 新增用例数 + 全量结果（**基线 → 现在**，别写死绝对值）；
- 真锁往返的原始输出（撤掉 → 哪条挂 → 还原 → md5 一致）；
- 与 spec 的**任何偏差都要单独列出**（不要自己拍板，也不要用"基本一致"含糊过去）；
- 报告里**不要**写 `git commit` / `git push` 的记录（本任务不提交）。

---

*本 spec 的历史背景、决策由来不必猜测；需要额外信息请在报告里直接问，不要自行假设。*
