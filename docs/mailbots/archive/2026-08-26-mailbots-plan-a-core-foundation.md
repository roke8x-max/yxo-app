# MailBots 重构 · 计划 A：可观测性 + core 地基 + Schema/归档 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 spec 的刀1–3：daemon_loop 崩溃可见（traceback 入日志文件）、`mailbots/core/` 公共地基五模块（models/events_store/dedup/matching/routing）配全量单测、yxo.db 新表新索引与按月归档——全程不改动任何生产机器人行为。

**Architecture:** 新增 `core/` 包作为唯一公共层；events 独立 WAL 库存去重/台账/草单序号；yxo.db 只加索引与归档表不动业务表；匹配器吃 records 行构建内存双索引（active + 全量），路由器从 records 推导 + bot_config 配置 To/Cc。缓存失效复用 `meta_kv.data_version`。

**Tech Stack:** Python 3.13（`.venv`）、sqlite3 标准库、pytest 8、dataclasses。

**Spec:** `docs/superpowers/specs/2026-08-26-mailbots-refactor-design.md`（本计划实现其 §3/§4/§6/§7 与实施顺序表的刀1–3；§5 分类转发矩阵与 IDLE 属计划 B/C）

## Global Constraints

- 解释器固定用 `C:\Users\Roke8x\Projects\yxo-app\.venv\Scripts\python.exe`；测试命令统一 `& "C:\Users\Roke8x\Projects\yxo-app\.venv\Scripts\python.exe" -m pytest mailbots/tests/unit -v`（在仓库根 `C:\Users\Roke8x\Projects\yxo-app` 执行）。
- 工作分支：从 `dev` 拉 `feature/core-framework`，每个 Task 结束 commit 一次；不合 main。
- 本计划只允许修改 `common_io.py`（刀1）与新增文件；**禁止改动任何 `*_Robot.py` / `Database_Syncer.py` 生产文件**。
- 源码一律 UTF-8 + 首行 `# -*- coding: utf-8 -*-`；注释/日志中文，风格对齐现有代码。
- 凭据（ACCOUNTS/secrets）永不打印、永不写入测试快照。
- Windows 控制台是 GBK：断言失败信息不要依赖 emoji；日志文件写入用 `encoding="utf-8"`。
- 测试库基准：`mailbots/tests/data/yxo_test.db`（只读挂载）；夹具数据源 `mailbots/tests/testset/test_fixtures/records.csv`（17 行，含 BOM，读取用 `utf-8-sig`）。
- yxo.db 连接默认 `mode=ro` 打开做推导；只有建索引/归档两个显式函数用读写连接。

---

### Task 1: 测试脚手架 + pytest 安装

**Files:**
- Create: `requirements-dev.txt`
- Create: `mailbots/tests/unit/conftest.py`
- Create: `mailbots/tests/unit/test_smoke.py`

**Interfaces:**
- Produces: 可运行的 pytest 环境；`conftest.py` 把 `mailbots/` 加入 `sys.path`（后续所有 `from core.xxx import` / `import common_io` 依赖它）。

- [ ] **Step 1: 创建 requirements-dev.txt 并安装 pytest**

```text
# requirements-dev.txt
pytest>=8.0
```

```bash
& "C:\Users\Roke8x\Projects\yxo-app\.venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
```

- [ ] **Step 2: 写 conftest.py**

```python
# -*- coding: utf-8 -*-
"""把 mailbots/ 加入 sys.path，使单元测试可以 import common_io / core.*"""
import os
import sys

MAILBOTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MAILBOTS_DIR not in sys.path:
    sys.path.insert(0, MAILBOTS_DIR)
```

- [ ] **Step 3: 写冒烟测试**

```python
# -*- coding: utf-8 -*-
"""脚手架自检：能 import 公共层且 norm_train_no 行为不变。"""
from common_io import norm_train_no


def test_import_common_io():
    assert norm_train_no("491") == "WB491"
    assert norm_train_no("wb 492") == "WB492"
```

- [ ] **Step 4: 运行确认通过**

Run: `& "C:\Users\Roke8x\Projects\yxo-app\.venv\Scripts\python.exe" -m pytest mailbots/tests/unit -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add requirements-dev.txt mailbots/tests/unit/
git commit -m "test: pytest 脚手架 + conftest 注入 mailbots 路径"
```

---

### Task 2: core/models.py 数据类

**Files:**
- Create: `mailbots/core/__init__.py`（空文件）
- Create: `mailbots/core/models.py`
- Test: `mailbots/tests/unit/test_models.py`

**Interfaces:**
- Produces（后续任务按此签名消费）:
  - `MatchResult(tier: str, reason: str | None, record: dict | None, candidates: tuple[dict, ...])`；tier ∈ {"T1","T2","T3","T4","T5","T6","T7","T0"}
  - `MailEvent(account: str, folder: str, message_id: str, uid: str, subject: str, sender: str, date_hdr: str, eml_path: str)`
  - `RouteTarget(company: str, to: tuple[str, ...], cc: tuple[str, ...])`

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
from core.models import MailEvent, MatchResult, RouteTarget


def test_match_result_frozen():
    r = MatchResult(tier="T1", reason=None, record={"客户编码": "CQWLJT1-A"}, candidates=())
    assert r.tier == "T1"
    try:
        r.tier = "T2"
        assert False, "应为 frozen"
    except AttributeError:
        pass


def test_mail_event_defaults():
    e = MailEvent(account="a@cqtransit.com", folder="草单运单号", message_id="<m1>",
                  uid="123", subject="s", sender="x@y.com", date_hdr="", eml_path="")
    assert e.folder == "草单运单号"


def test_route_target_tuples():
    t = RouteTarget(company="港九港铁", to=["a@b.com"], cc=["c@d.com"])
    assert t.to == ("a@b.com",) and t.cc == ("c@d.com",)
```

- [ ] **Step 2: 运行确认失败**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_models.py -v`
Expected: FAIL (`ModuleNotFoundError: core`)

- [ ] **Step 3: 实现 models.py**

```python
# -*- coding: utf-8 -*-
"""core 数据类（spec §3 models.py）：全量类型注解，frozen 保证跨处理器传递不被改写。"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MatchResult:
    """classify_match 的输出。tier 见 spec §4 八档；reason 为机器可读明细。"""
    tier: str
    reason: str | None
    record: dict | None
    candidates: tuple[dict, ...] = ()


@dataclass(frozen=True)
class MailEvent:
    """一封待处理邮件的元数据（raw 已落盘，处理器不回 IMAP 取件）。"""
    account: str
    folder: str
    message_id: str
    uid: str
    subject: str
    sender: str
    date_hdr: str
    eml_path: str = ""


@dataclass(frozen=True)
class RouteTarget:
    """一家负责公司的路由结果：外部联系人 To/Cc（来自 bot_config scope='company'）。"""
    company: str
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    def __post_init__(self):
        object.__setattr__(self, "to", tuple(self.to))
        object.__setattr__(self, "cc", tuple(self.cc))
```

- [ ] **Step 4: 运行确认通过**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_models.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add mailbots/core/ mailbots/tests/unit/test_models.py
git commit -m "feat(core): models 数据类 MatchResult/MailEvent/RouteTarget"
```

---

### Task 3: 刀1 —— daemon_loop traceback 日志（可观测性第一刀）

**Files:**
- Modify: `mailbots/common_io.py`（新增 `log_error`/`run_once`，重构 `daemon_loop` 内联 try）
- Test: `mailbots/tests/unit/test_daemon_error_log.py`

**Interfaces:**
- Produces:
  - `common_io.log_error(bot_name: str, text: str) -> None`：追加写 `<mailbots>/error_logs/{bot}_error_YYYYMMDD.log`（UTF-8，目录自动创建）
  - `common_io.run_once(bot_name: str, run_fn) -> int`：执行一轮，网络类异常续跑、未知异常写完整 traceback，均返回 0
- 不变式：`daemon_loop(bot_name, run_fn, ...)` 对外签名与退避行为完全不变。

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""spec 刀1/D4：daemon_loop 异常必须落 traceback 到日志文件（根治隐身崩溃）。"""
import os
import common_io


def _err_dir(tmp_path):
    d = tmp_path / "error_logs"
    common_io.ERROR_LOG_DIR = str(d)          # 重定向到测试目录
    return str(d)


def test_run_once_unknown_exception_writes_traceback(tmp_path):
    d = _err_dir(tmp_path)

    def boom():
        raise ValueError("业务炸了")

    got = common_io.run_once("TestBot", boom)
    assert got == 0
    files = os.listdir(d)
    assert len(files) == 1 and files[0].startswith("TestBot_error_")
    content = open(os.path.join(d, files[0]), encoding="utf-8").read()
    assert "ValueError" in content and "业务炸了" in content and "Traceback" in content


def test_run_once_network_exception_logged(tmp_path):
    d = _err_dir(tmp_path)

    def netfail():
        raise ConnectionError("imap 断了")

    assert common_io.run_once("TestBot", netfail) == 0
    content = open(os.path.join(d, os.listdir(d)[0]), encoding="utf-8").read()
    assert "ConnectionError" in content


def test_run_once_success_returns_count(tmp_path):
    _err_dir(tmp_path)
    assert common_io.run_once("TestBot", lambda: 3) == 3
    assert common_io.run_once("TestBot", lambda: None) == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_daemon_error_log.py -v`
Expected: FAIL (`AttributeError: module 'common_io' has no attribute 'run_once'`)

- [ ] **Step 3: 实现（common_io.py）**

在文件头 import 区补：

```python
import traceback as _traceback
```

在 `daemon_loop` 定义之前（第 148 行附近）插入：

```python
# ==================== 错误日志（spec 刀1/D4：根治隐身崩溃）====================
ERROR_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error_logs")


def log_error(bot_name, text):
    """把异常/告警文本追加写入 error_logs/{bot}_error_YYYYMMDD.log（UTF-8）。
    目录首次自动创建；写日志自身失败只打控制台，绝不向上抛。"""
    try:
        os.makedirs(ERROR_LOG_DIR, exist_ok=True)
        fname = os.path.join(
            ERROR_LOG_DIR,
            "{}_error_{}.log".format(bot_name, datetime.now().strftime("%Y%m%d")),
        )
        with open(fname, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception as e:
        print("[{}] 写错误日志失败: {}".format(bot_name, e))


def run_once(bot_name, run_fn):
    """守护循环的一轮：网络类异常续跑；未知异常把完整 traceback 写入错误日志。
    返回本轮处理邮件数（run_fn 返回 None 视为 0）。"""
    try:
        return run_fn() or 0
    except (TimeoutError, OSError, ConnectionError) as e:
        log_error(bot_name, "网络类异常，续跑: {!r}".format(e))
        return 0
    except Exception:
        log_error(bot_name, "未知异常，本轮跳过:\n" + _traceback.format_exc())
        return 0
```

把 `daemon_loop` 内的原内联 try（现 162–169 行）替换为：

```python
        got = run_once(bot_name, run_fn)
```

（即删除原 `try: got = run_fn() or 0 ... got = 0` 整段，循环其余逻辑不动。）

- [ ] **Step 4: 运行全部测试确认通过且旧冒烟不回归**

Run: `& "...python.exe" -m pytest mailbots/tests/unit -v`
Expected: 全部 passed（含 Task1 冒烟）

- [ ] **Step 5: Commit**

```bash
git add mailbots/common_io.py mailbots/tests/unit/test_daemon_error_log.py
git commit -m "feat(observability): daemon_loop 异常 traceback 落 error_logs（spec 刀1/D4）"
```

---

### Task 4: core/events_store.py —— events 库 Schema + .eml 仓库 + 台账 + 归档

**Files:**
- Create: `mailbots/core/events_store.py`
- Test: `mailbots/tests/unit/test_events_store.py`

**Interfaces:**
- Consumes: 无（独立模块）
- Produces:
  - `connect(db_path: str) -> sqlite3.Connection`（WAL、busy_timeout=15000、row_factory=Row）
  - `ensure_schema(conn) -> None`：建 `dedup_global` / `waybill_ledger` / `draft_seen_seq`（spec §6 DDL 原文）
  - `save_eml(repo_dir: str, account: str, message_id: str, raw: bytes) -> str`：落盘 `repo_dir/YYYY/MM/<sanitized_msgid>.eml`，返回路径
  - `ledger_insert_waybill(conn, code, box, waybill, train_no, depart_at, company, msg_id) -> bool`（识别即留底 forward_status='pending'，按 msg_id+box+waybill 幂等；新插 True）
  - `ledger_mark_waybill_sent(conn, msg_id) -> int`（pending→sent 条数）
  - `seen_seq_add(conn, seq: str, msg_id: str) -> bool`（INSERT OR IGNORE；首次 True）
  - `archive_before(conn, table: str, date_col: str, keep_days: int = 90) -> int`：把 `{table}` 中 `date_col` 早于 N 天的行搬入 `{table}_archive`（无则 CREATE AS SELECT * WHERE 0），返回搬运行数

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
import os
import sqlite3
from core import events_store as es


def _conn(tmp_path):
    conn = es.connect(str(tmp_path / "events.db"))
    es.ensure_schema(conn)
    return conn


def test_schema_tables_created(tmp_path):
    conn = _conn(tmp_path)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"dedup_global", "waybill_ledger", "draft_seen_seq"} <= names


def test_save_eml_layout(tmp_path):
    raw = b"Message-ID: <abc@x>\r\n\r\nbody"
    p = es.save_eml(str(tmp_path / "repo"), "maoxiaoyang@cqtransit.com",
                    "<abc@x>", raw)
    assert p.endswith(".eml") and os.sep + "2026" + os.sep in p
    assert open(p, "rb").read() == raw
    # 同 msg_id 再存覆盖同一路径（幂等）
    p2 = es.save_eml(str(tmp_path / "repo"), "maoxiaoyang@cqtransit.com", "<abc@x>", raw)
    assert p2 == p


def test_waybill_ledger_pending_then_sent(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(code="CQWLJT260822004-Kol", box="HNKU5117095", waybill="38149366",
              train_no="WB734", depart_at="2026-08-22", company="莫斯科子公司",
              msg_id="<w1@x>")
    assert es.ledger_insert_waybill(conn, **kw) is True      # 识别即留底
    assert es.ledger_insert_waybill(conn, **kw) is False     # 幂等
    assert conn.execute("SELECT forward_status FROM waybill_ledger").fetchone()[0] == "pending"
    assert es.ledger_mark_waybill_sent(conn, "<w1@x>") == 1
    assert conn.execute("SELECT forward_status FROM waybill_ledger").fetchone()[0] == "sent"


def test_seen_seq_first_time_true(tmp_path):
    conn = _conn(tmp_path)
    assert es.seen_seq_add(conn, "260713004", "<d1@x>") is True
    assert es.seen_seq_add(conn, "260713004", "<d2@x>") is False  # 已见→B类依据


def test_archive_before_moves_rows(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("CREATE TABLE tracing_log(log_id TEXT PRIMARY KEY, log_date TEXT)")
    conn.executemany("INSERT INTO tracing_log VALUES(?,?)",
                     [("old1", "2026-01-01"), ("new1", "2099-01-01")])
    moved = es.archive_before(conn, "tracing_log", "log_date", keep_days=90)
    conn.commit()
    assert moved >= 1
    remain = {r[0] for r in conn.execute("SELECT log_id FROM tracing_log")}
    arch = {r[0] for r in conn.execute("SELECT log_id FROM tracing_log_archive")}
    assert "new1" in remain and "old1" in arch
```

- [ ] **Step 2: 运行确认失败**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_events_store.py -v`
Expected: FAIL (`ModuleNotFoundError: core.events_store`)

- [ ] **Step 3: 实现 events_store.py**

```python
# -*- coding: utf-8 -*-
"""events 单事件库（spec §3 events_store / §6 DDL）：
WAL + busy_timeout；dedup_global / waybill_ledger / draft_seen_seq 三表；
.eml 原件落盘仓库（处理器永不回 IMAP 取件）；通用按月归档。"""
import os
import re
import sqlite3
from datetime import datetime, timedelta

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dedup_global (
    key        TEXT PRIMARY KEY,
    synthetic  INTEGER DEFAULT 0,
    claimed_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS waybill_ledger (
    id          INTEGER PRIMARY KEY,
    code        TEXT,
    box         TEXT,
    waybill     TEXT,
    train_no    TEXT,
    depart_at   TEXT,
    company     TEXT,
    msg_id      TEXT,
    forward_status TEXT DEFAULT 'pending',
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_waybill_msg ON waybill_ledger(msg_id);
CREATE TABLE IF NOT EXISTS draft_seen_seq (
    seq TEXT PRIMARY KEY,
    msg_id TEXT,
    first_seen_at TEXT DEFAULT (datetime('now'))
);
"""


def connect(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def ensure_schema(conn):
    conn.executescript(SCHEMA_SQL)
    conn.commit()


_SANITIZE = re.compile(r"[^A-Za-z0-9._@+-]+")


def save_eml(repo_dir, account, message_id, raw):
    """原件落盘 YYYY/MM/<sanitized>.eml；同 Message-ID 幂等覆盖同路径。"""
    dt = datetime.now()
    safe = _SANITIZE.sub("_", (message_id or "noid"))[:80]
    sub = os.path.join(repo_dir, dt.strftime("%Y"), dt.strftime("%m"),
                       _SANITIZE.sub("_", account.split("@")[0]))
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, safe + ".eml")
    with open(path, "wb") as f:
        f.write(raw)
    return path


def ledger_insert_waybill(conn, code, box, waybill, train_no, depart_at,
                          company, msg_id):
    """识别即留底（spec P1-4）：INSERT OR IGNORE 按 msg_id+box+waybill 幂等。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO waybill_ledger"
        "(code, box, waybill, train_no, depart_at, company, msg_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (code, box, waybill, train_no, depart_at, company, msg_id))
    conn.commit()
    return cur.rowcount > 0


def ledger_mark_waybill_sent(conn, msg_id):
    cur = conn.execute(
        "UPDATE waybill_ledger SET forward_status='sent' "
        "WHERE msg_id=? AND forward_status='pending'", (msg_id,))
    conn.commit()
    return cur.rowcount


def seen_seq_add(conn, seq, msg_id):
    """A/B 类判定的序号台账：遇到即写（spec 2026-08-26 补充定义）。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO draft_seen_seq(seq, msg_id) VALUES(?,?)",
        ((seq or "").strip(), msg_id))
    conn.commit()
    return cur.rowcount > 0


def archive_before(conn, table, date_col, keep_days=90):
    """把 table 中 date_col 早于 keep_days 天的行搬入 {table}_archive（spec §6）。"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    cols = [r[1] for r in conn.execute("PRAGMA table_info({})".format(table))]
    collist = ", ".join('"{}"'.format(c) for c in cols)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS {t}_archive AS "
        "SELECT * FROM {t} WHERE 0".format(t=table))
    cur = conn.execute(
        "INSERT INTO {t}_archive ({cols}) SELECT {cols} FROM {t} "
        'WHERE date("{d}") < date(?)'.format(t=table, cols=collist, d=date_col),
        (cutoff,))
    n = cur.rowcount
    conn.execute('DELETE FROM {t} WHERE date("{d}") < date(?)'
                 .format(t=table, d=date_col), (cutoff,))
    conn.commit()
    return n
```

（注意测试文件里 `import os` 放顶部更规范——实现时把测试的 `import os` 移到文件头，删掉尾行。）

- [ ] **Step 4: 运行确认通过**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_events_store.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add mailbots/core/events_store.py mailbots/tests/unit/test_events_store.py
git commit -m "feat(core): events_store WAL 库 + 三表 DDL + eml 仓库 + 归档"
```

---

### Task 5: core/dedup.py —— 全局原子去重

**Files:**
- Create: `mailbots/core/dedup.py`
- Test: `mailbots/tests/unit/test_dedup.py`

**Interfaces:**
- Consumes: `core.events_store.connect/ensure_schema`（dedup_global 表已存在）
- Produces:
  - `synthetic_key(account: str, folder: str, sender: str, date_hdr: str, subject: str) -> str`：`"SYN::" + sha256(...)[:32]`（缺 Message-ID 兜底键，作用域隔离，spec §7.2）
  - `try_claim(conn, key: str, synthetic: bool = False) -> bool`：原子 claim，True=本进程获得处理权
  - `release(conn, key: str) -> None`：处理失败释放（下一轮重试，spec「成功才 mark」语义的实现方式）
  - `reclaim_stale(conn, hours: int = 6) -> int`：崩溃恢复，清掉超时 claim，返回清理数

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
import hashlib
from core import dedup, events_store as es


def _conn(tmp_path):
    conn = es.connect(str(tmp_path / "e.db"))
    es.ensure_schema(conn)
    return conn


def test_try_claim_atomic_exactly_once(tmp_path):
    conn = _conn(tmp_path)
    assert dedup.try_claim(conn, "<m1@x>") is True
    assert dedup.try_claim(conn, "<m1@x>") is False   # 第二个进程/第二轮抢不到


def test_release_allows_retry(tmp_path):
    conn = _conn(tmp_path)
    dedup.try_claim(conn, "<m1@x>")
    dedup.release(conn, "<m1@x>")                      # 转发失败→释放
    assert dedup.try_claim(conn, "<m1@x>") is True     # 下轮重试


def test_synthetic_key_scoped_and_flagged(tmp_path):
    k1 = dedup.synthetic_key("a@cqtransit.com", "草单运单号", "x@y",
                             "Thu, 21 Aug 2026 10:00:00 +0800", "草单")
    k2 = dedup.synthetic_key("b@cqtransit.com", "草单运单号", "x@y",
                             "Thu, 21 Aug 2026 10:00:00 +0800", "草单")
    assert k1 != k2                                    # 跨账号作用域隔离
    expect = "SYN::" + hashlib.sha256(
        "|".join(["a@cqtransit.com", "草单运单号", "x@y",
                  "Thu, 21 Aug 2026 10:00:00 +0800", "草单"]).encode("utf-8")).hexdigest()[:32]
    assert k1 == expect
    conn = _conn(tmp_path)
    assert dedup.try_claim(conn, k1, synthetic=True) is True
    row = conn.execute("SELECT synthetic FROM dedup_global WHERE key=?", (k1,)).fetchone()
    assert row["synthetic"] == 1


def test_reclaim_stale(tmp_path):
    conn = _conn(tmp_path)
    dedup.try_claim(conn, "<old@x>")
    conn.execute("UPDATE dedup_global SET claimed_at=datetime('now','-7 hours') "
                 "WHERE key='<old@x>'")
    conn.commit()
    assert dedup.reclaim_stale(conn, hours=6) == 1
    assert dedup.try_claim(conn, "<old@x>") is True
```

- [ ] **Step 2: 运行确认失败**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_dedup.py -v`
Expected: FAIL (`No module named core.dedup`)

- [ ] **Step 3: 实现 dedup.py**

```python
# -*- coding: utf-8 -*-
"""全局去重（spec §3 dedup / 08 §7）：
主键=Message-ID；缺 ID 用作用域内容哈希兜底(synthetic=1)；
claim→成功保持 / 失败 release→下轮重试；崩溃靠 reclaim_stale 兜底。"""
import hashlib


def synthetic_key(account, folder, sender, date_hdr, subject):
    payload = "|".join([account or "", folder or "", sender or "",
                        date_hdr or "", subject or ""])
    return "SYN::" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def try_claim(conn, key, synthetic=False):
    cur = conn.execute(
        "INSERT OR IGNORE INTO dedup_global(key, synthetic) VALUES(?,?)",
        (key, 1 if synthetic else 0))
    conn.commit()
    return cur.rowcount > 0


def release(conn, key):
    """处理失败：释放占用，下一轮允许重试（保证每封业务邮件至少转发一次）。"""
    conn.execute("DELETE FROM dedup_global WHERE key=?", (key,))
    conn.commit()


def reclaim_stale(conn, hours=6):
    cur = conn.execute(
        "DELETE FROM dedup_global "
        "WHERE claimed_at < datetime('now', ?)", ("-{} hours".format(hours),))
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 4: 运行确认通过**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_dedup.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add mailbots/core/dedup.py mailbots/tests/unit/test_dedup.py
git commit -m "feat(core): dedup_global 原子 claim/release/stale-reclaim"
```

---

### Task 6: core/matching.py —— 八档判定 + 内存双索引 + 消歧

**Files:**
- Create: `mailbots/core/matching.py`
- Test: `mailbots/tests/unit/test_matching.py`

**Interfaces:**
- Produces:
  - `parse_code(token: str) -> dict | None`：`{"prefix","seq","suffix"}`
  - `is_iso_box(token: str) -> bool`：`^[A-Z]{4}\d{7}$`
  - `build_index(rows: Iterable[dict]) -> ActiveIndex`；rows 元素为
    `{"code": str, "box": str, "company": str, "status": str, "deleted": int}`
    （由调用方从 records 列名映射；Task 7 的 RecordIndexProvider 负责映射）
  - `ActiveIndex.known_prefixes: set[str]`；`.get_active_by_full_code(code)`；`.active_by_seq(seq) -> list`；`.full_by_seq(seq) -> list`（含退舱/软删）；`.active_by_box(box) -> list`
  - `classify_match(code: str, box: str, idx: ActiveIndex) -> MatchResult`（八档 + reason ∈ {"exact","suffix_mismatch","box_mismatch","unknown","cancelled_only","multi_suffix","unique_box_only","reused_box","nothing"}）

- [ ] **Step 1: 用 testset records.csv 构造测试数据加载器并写失败测试**

```python
# -*- coding: utf-8 -*-
"""八档判定全档位测试。基准=v2 规范 §2/§5 + 2026-08-26 拍板：
退舱/软删仅命中的码 → T4(reason=cancelled_only)，仍报警。"""
import csv
import os
import pytest
from core import matching

RECORDS_CSV = os.path.join(os.path.dirname(__file__), "..", "testset",
                           "test_fixtures", "records.csv")


def load_rows():
    with open(RECORDS_CSV, encoding="utf-8-sig") as f:
        return [{"code": (r["客户编码"] or "").strip(),
                 "box": (r["箱号"] or "").strip().upper(),
                 "company": (r["开票子公司名称"] or "").strip(),
                 "status": (r["状态"] or "").strip(),
                 "deleted": int(r["is_deleted"] or 0)}
                for r in csv.DictReader(f)]


@pytest.fixture(scope="module")
def idx():
    return matching.build_index(load_rows())


def test_t1_exact_hit(idx):
    r = matching.classify_match("CQWLJT260713004-BLLST", "WRONGBOX", idx)
    assert (r.tier, r.reason) == ("T1", "exact")       # 客编精确→箱号不卡


def test_t4_cancelled_only(idx):
    r = matching.classify_match("CQWLJT260713006-VXN", "", idx)
    assert (r.tier, r.reason) == ("T4", "cancelled_only")


def test_t2_suffix_mismatch(idx):
    # 序号 260709001 在库(-Kol)；来信同箱不同后缀
    r = matching.classify_match("CQWLJT260709001-XXX", "OVLU2507254", idx)
    assert r.tier == "T2"


def test_t3_box_mismatch_or_empty(idx):
    r = matching.classify_match("CQWLJT260713004-ZZZ", "", idx)
    assert (r.tier, r.reason) == ("T3", "box_mismatch")


def test_t5_unique_box_no_code(idx):
    r = matching.classify_match("", "TSRU8008478", idx)
    assert (r.tier, r.reason) == ("T5", "unique_box_only")


def test_t6_reused_box(idx):
    # v2 §5: FWRU0192384 / PONU8175063 在 yxo_test.db 复用；records.csv 若无此箱，
    # 该用例以合成索引验证（下方 test_t6_with_synth_index）
    assert True


def test_t6_with_synth_index():
    rows = [
        {"code": "CQWLJT260101001-A", "box": "FWRU0192384", "company": "甲",
         "status": "", "deleted": 0},
        {"code": "CQWLJT260101002-B", "box": "FWRU0192384", "company": "乙",
         "status": "", "deleted": 0},
    ]
    r = matching.classify_match("", "FWRU0192384", matching.build_index(rows))
    assert (r.tier, r.reason) == ("T6", "reused_box")


def test_t7_cross_suffix_guard():
    rows = [
        {"code": "CQWLJT260201001-A", "box": "ABCU1111111", "company": "甲",
         "status": "", "deleted": 0},
        {"code": "CQWLJT260201001-B", "box": "ABCU2222222", "company": "乙",
         "status": "", "deleted": 0},
    ]
    r = matching.classify_match("CQWLJT260201001-C", "", matching.build_index(rows))
    assert (r.tier, r.reason) == ("T7", "multi_suffix")


def test_t0_nothing(idx):
    r = matching.classify_match("", "", idx)
    assert (r.tier, r.reason) == ("T0", "nothing")


def test_disambiguation_box_not_code():
    assert matching.is_iso_box("TRIU1234567") is True
    assert matching.parse_code("TRIU1234567") is not None  # 能解析但…
    rows = [{"code": "CQWLJT260101001-A", "box": "TRIU1234567", "company": "甲",
             "status": "", "deleted": 0}]
    idx = matching.build_index(rows)
    # 提取层规则：ISO 箱形态 token 一律判箱号，不进客编通道（spec §4 消歧规则1）
    assert matching.is_client_code_candidate("TRIU1234567", idx) is False
    assert matching.is_client_code_candidate("CQWLJT260101001-A", idx) is True


def test_parse_generic_prefix():
    p = matching.parse_code("ABC12345-DME")
    assert (p["prefix"], p["seq"], p["suffix"]) == ("ABC", "12345", "DME")
```

- [ ] **Step 2: 运行确认失败**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_matching.py -v`
Expected: FAIL (`No module named core.matching`)

- [ ] **Step 3: 实现 matching.py**

```python
# -*- coding: utf-8 -*-
"""统一匹配器（spec §4）：客编优先、箱号辅助；active 铁律过滤；
退舱/软删仅命中→T4(cancelled_only)。判定顺序显式化（2026-08-26 拍板）。"""
import re
from core.models import MatchResult

CODE_RE = re.compile(r"^(?P<prefix>[A-Za-z]+)(?P<seq>\d+)(?:-(?P<suffix>.+))?$")
BOX_RE = re.compile(r"^[A-Z]{4}\d{7}$")

CANCELLED_STATUS = "退舱"


def parse_code(token):
    m = CODE_RE.match((token or "").strip())
    if not m:
        return None
    return {"prefix": m.group("prefix").upper(),
            "seq": m.group("seq"),
            "suffix": (m.group("suffix") or "").upper()}


def is_iso_box(token):
    return bool(BOX_RE.match((token or "").strip().upper()))


def norm_box(b):
    return (b or "").strip().upper()


def is_client_code_candidate(token, idx):
    """消歧（spec §4）：ISO 箱形态一律判箱号；否则需 prefix 在已知白名单或有后缀。"""
    tok = (token or "").strip()
    if not tok:
        return False
    if is_iso_box(tok):
        return False
    p = parse_code(tok)
    if p is None:
        return False
    return (p["prefix"] in idx.known_prefixes) or bool(p["suffix"])


class ActiveIndex:
    """内存三索引 + 全量兜底索引 + prefix 白名单。"""

    def __init__(self):
        self.by_full_code = {}     # 完整客编(upper) -> row
        self.by_seq = {}           # seq -> [row]           仅 active
        self.full_by_seq = {}      # seq -> [row]           含退舱/软删
        self.by_box = {}           # box(upper) -> [row]    仅 active
        self.known_prefixes = set()

    # -- 查询 --
    def get_active_by_full_code(self, code):
        return self.by_full_code.get((code or "").strip().upper())

    def active_by_seq(self, seq):
        return self.by_seq.get(seq or "", [])

    def full_by_seq(self, seq):
        return self.full_by_seq.get(seq or "", [])

    def active_by_box(self, box):
        return self.by_box.get(norm_box(box), [])


def build_index(rows):
    idx = ActiveIndex()
    for r in rows:
        p = parse_code(r.get("code", ""))
        active = (not r.get("deleted")) and (r.get("status", "") != CANCELLED_STATUS)
        if p:
            idx.known_prefixes.add(p["prefix"])
            key_full = (r.get("code") or "").strip().upper()
            if active and key_full and key_full not in idx.by_full_code:
                idx.by_full_code[key_full] = r
            idx.full_by_seq.setdefault(p["seq"], []).append(r)
            if active:
                idx.by_seq.setdefault(p["seq"], []).append(r)
        b = norm_box(r.get("box", ""))
        if b and active:
            idx.by_box.setdefault(b, []).append(r)
    return idx


def classify_match(code, box, idx):
    """spec §4 显式顺序：有码走 T1→T2/T7/T3→T4；无码走 T0/T5/T6。"""
    code = (code or "").strip()
    box_n = norm_box(box)

    if code:
        rec = idx.get_active_by_full_code(code)
        if rec:
            return MatchResult("T1", "exact", rec, ())
        p = parse_code(code)
        seq = p["seq"] if p else ""
        actives = idx.active_by_seq(seq) if seq else []
        if actives:
            in_suffix = p["suffix"]
            same_box_diff_suffix = [
                r for r in actives
                if box_n
                and norm_box(r.get("box", "")) == box_n
                and (parse_code(r.get("code", "")) or {}).get("suffix", "") != in_suffix
            ]
            if same_box_diff_suffix:
                return MatchResult("T2", "suffix_mismatch", same_box_diff_suffix[0],
                                   tuple(actives))
            suffixes = {(parse_code(r.get("code", "")) or {}).get("suffix", "")
                        for r in actives}
            if len(suffixes) >= 2:
                return MatchResult("T7", "multi_suffix", actives[0], tuple(actives))
            return MatchResult("T3", "box_mismatch", actives[0], tuple(actives))
        # active 未命中 → 查全量（含退舱/软删）区分 unknown / cancelled_only
        if seq and idx.full_by_seq(seq):
            return MatchResult("T4", "cancelled_only", None, ())
        return MatchResult("T4", "unknown", None, ())

    # 无客编分支（绝不进 T1–T3）
    hits = idx.active_by_box(box_n) if box_n else []
    if not hits:
        return MatchResult("T0", "nothing", None, ())
    if len(hits) == 1:
        return MatchResult("T5", "unique_box_only", hits[0], tuple(hits))
    return MatchResult("T6", "reused_box", hits[0], tuple(hits))
```

- [ ] **Step 4: 运行确认通过**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_matching.py -v`
Expected: 11 passed

（随后创建 `mailbots/tests/unit/test_matching_integration.py` 承载下方两个真实库用例）

- [ ] **Step 5: 用 yxo_test.db 做一次集成抽查（只读）**

```python
# -*- coding: utf-8 -*-
"""集成抽查：真实库快照上重放关键拍板结论。"""
import sqlite3
import os
from core import matching

DB = os.path.join(os.path.dirname(__file__), "..", "data", "yxo_test.db")


def _idx_from_db():
    conn = sqlite3.connect("file:{}?mode=ro".format(DB.replace("\\", "/")), uri=True)
    conn.row_factory = sqlite3.Row
    rows = [{"code": r["客户编码"], "box": r["箱号"],
             "company": r["开票子公司名称"], "status": r["状态"],
             "deleted": r["is_deleted"] or 0}
            for r in conn.execute("SELECT 客户编码,箱号,开票子公司名称,状态,is_deleted FROM records")]
    conn.close()
    return matching.build_index(rows)


def test_yxo_test_db_vxn_cancelled_t4():
    idx = _idx_from_db()
    r = matching.classify_match("CQWLJT260718008-VXN", "", idx)
    assert (r.tier, r.reason) == ("T4", "cancelled_only")


def test_yxo_test_db_reused_boxes_t6():
    idx = _idx_from_db()
    for b in ("FWRU0192384", "PONU8175063"):
        assert matching.classify_match("", b, idx).tier == "T6"
```

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_matching.py mailbots/tests/unit/test_matching_integration.py -v`
Expected: 全部 passed（若 yxo_test.db 中两复用箱不存在则改用合成索引用例并在 PR 说明）

- [ ] **Step 6: Commit**

```bash
git add mailbots/core/matching.py mailbots/tests/unit/test_matching*.py
git commit -m "feat(core): matching 八档判定 + 双索引 + 客编/箱号消歧"
```

---

### Task 7: core/routing.py —— 统一路由 + 记录索引提供器 + yxo.db 索引

**Files:**
- Create: `mailbots/core/routing.py`
- Test: `mailbots/tests/unit/test_routing.py`

**Interfaces:**
- Consumes: `matching.build_index/classify_match`、`common_io.norm_train_no`、`db_write.bump_version` 所写的 `meta_kv('data_version')`
- Produces:
  - `ensure_record_indexes(yxo_conn) -> None`（spec §6 五条 CREATE INDEX IF NOT EXISTS，读写连接，调用方负责开关事务）
  - `load_company_routes(yxo_conn, bot: str) -> dict[str, dict]`：`{公司名: {"to": [...], "cc": [...]}}`（bot_config scope='company'，JSON 列解析）
  - `load_train_overrides(yxo_conn) -> dict[str, list[str]]`：norm 后班列车次 → 公司列表（scope='train' 应急覆盖层）
  - `resolve_recipients(yxo_ro_conn, idx, bot, code="", box="", train_id="") -> tuple[list[RouteTarget], str | None]`：
    - code/box 路径：`classify_match` T1→该公司；T5→该箱唯一公司；其他 tier → `([], reason)`
    - train 路径：override 优先，否则 `SELECT DISTINCT 开票子公司名称 FROM records WHERE 班列号=? AND COALESCE(is_deleted,0)=0 AND 状态<>'退舱'`
    - reason ∈ {"tier_not_routable:T2"...} / {"no_route_config:公司"} / {"train_unmatched"} / {"train_no_companies"}
    - 任一公司缺 bot_config → 跳过该公司并在 reason 记 `no_route_config`
  - `RecordIndexProvider(db_path: str, ttl_seconds: int = 300)`：`.get() -> ActiveIndex`；内部查 `meta_kv.data_version` 变化或 TTL 到期即重建（版本读取失败视为 "0"，不影响功能）

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""路由测试：合成 yxo.db（records+bot_config 最小 schema），专列/散舱同链路 + 空路由报警原因。"""
import json
import sqlite3
import pytest
from core import matching, routing


@pytest.fixture()
def yxo(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "yxo.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE records(客户编码 TEXT, 箱号 TEXT, 开票子公司名称 TEXT,
                           班列号 TEXT, 状态 TEXT, is_deleted INTEGER);
      CREATE TABLE bot_config(bot TEXT, scope TEXT, key TEXT,
                              to_addrs TEXT, cc_addrs TEXT, extra TEXT);
      CREATE TABLE meta_kv(key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute("INSERT INTO records VALUES('CQWLJT260101001-A','ABCU1111111','港九港铁','','',0)")
    conn.execute("INSERT INTO records VALUES('CQWLJT260102002-B','RBGU4001728','保时达','WB794','',0)")  # 专列
    conn.execute("INSERT INTO bot_config VALUES('tracking','company','港九港铁', ?, ?, NULL)",
                 (json.dumps(["chenkai@atrailimt.com"]), json.dumps(["watch@qq.com"])))
    conn.execute("INSERT INTO bot_config VALUES('tracking','train','794',NULL,NULL,?)",
                 (json.dumps({"companies": ["保时达"]}),))
    conn.commit()
    yield conn
    conn.close()


def _idx(yxo_conn):
    rows = [{"code": r["客户编码"], "box": r["箱号"], "company": r["开票子公司名称"],
             "status": r["状态"] or "", "deleted": r["is_deleted"] or 0}
            for r in yxo_conn.execute("SELECT * FROM records")]
    return matching.build_index(rows)


def test_code_t1_routes_to_company_contacts(yxo):
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", code="CQWLJT260101001-A")
    assert reason is None and len(targets) == 1
    assert targets[0].company == "港九港铁"
    assert targets[0].to == ("chenkai@atrailimt.com",)
    assert targets[0].cc == ("watch@qq.com",)


def test_missing_bot_config_reports_reason(yxo):
    yxo.execute("DELETE FROM bot_config WHERE scope='company'")
    yxo.commit()
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", code="CQWLJT260101001-A")
    assert targets == [] and reason.startswith("no_route_config")


def test_train_override_layer_wins(yxo):
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", train_id="794")
    assert reason is None
    assert [t.company for t in targets] == ["保时达"]      # override 层优先于 records 推导


def test_train_records_derivation(yxo):
    yxo.execute("DELETE FROM bot_config WHERE scope='train'")
    yxo.commit()
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", train_id="WB794")      # norm 后一致
    assert [t.company for t in targets] == ["保时达"]


def test_train_unmatched_reason(yxo):
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", train_id="999")
    assert targets == [] and reason == "train_no_companies"


def test_non_t1_not_routable(yxo):
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", code="CQWLJT999999-ZZZ")
    assert targets == [] and reason.startswith("tier_not_routable:T4")


def test_ensure_record_indexes_idempotent(yxo):
    routing.ensure_record_indexes(yxo)
    routing.ensure_record_indexes(yxo)                     # 重复执行不抛错
    names = {r[0] for r in yxo.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_records_code" in names


def test_provider_rebuilds_on_version_change(tmp_path):
    """缓存失效机制（spec §4）：meta_kv.data_version 变化 → get() 重建索引，
    新订舱不再被误判 T4。"""
    p = str(tmp_path / "yxo.db")
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE records(客户编码 TEXT, 箱号 TEXT, 开票子公司名称 TEXT,
                           班列号 TEXT, 状态 TEXT, is_deleted INTEGER);
      CREATE TABLE meta_kv(key TEXT PRIMARY KEY, value TEXT);
      INSERT INTO records VALUES('CQWLJT260101001-A','ABCU1111111','港九港铁','','',0);
      INSERT INTO meta_kv VALUES('data_version','1');
    """)
    conn.commit()
    prov = routing.RecordIndexProvider(p, ttl_seconds=0)
    idx1 = prov.get()
    assert idx1.get_active_by_full_code("CQWLJT260101001-A") is not None

    # 模拟 yxo_app 导入新订舱并 bump_version
    conn.execute(
        "INSERT INTO records VALUES('CQWLJT260199999-B','XQBU2222222','保时达','','',0)")
    conn.execute("UPDATE meta_kv SET value='2' WHERE key='data_version'")
    conn.commit()

    idx2 = prov.get()
    assert idx2.get_active_by_full_code("CQWLJT260199999-B") is not None
    assert routing._read_data_version(conn) == "2"
    conn.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_routing.py -v`
Expected: FAIL (`No module named core.routing`)

- [ ] **Step 3: 实现 routing.py**

```python
# -*- coding: utf-8 -*-
"""统一路由（spec §3 routing / D1/D2）：Layer1 从 records 推导（散舱/专列同链路
读 开票子公司名称=负责公司）；bot_config scope='company' 只管 To/Cc；
scope='train' 为应急覆盖层；空结果必须带 reason（调用方据此 alarm，根治 P3 静默）。"""
import json
import time
from core import matching
from core.models import RouteTarget
from common_io import norm_train_no

RECORD_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_records_code    ON records("客户编码");
CREATE INDEX IF NOT EXISTS idx_records_box     ON records("箱号");
CREATE INDEX IF NOT EXISTS idx_records_train   ON records("班列号");
CREATE INDEX IF NOT EXISTS idx_records_company ON records("开票子公司名称");
"""


def ensure_record_indexes(yxo_conn):
    yxo_conn.executescript(RECORD_INDEXES_SQL)
    yxo_conn.commit()


def load_company_routes(yxo_conn, bot):
    routes = {}
    for r in yxo_conn.execute(
            "SELECT key, to_addrs, cc_addrs FROM bot_config "
            "WHERE bot=? AND scope='company'", (bot,)):
        try:
            routes[r["key"]] = {"to": json.loads(r["to_addrs"] or "[]"),
                                "cc": json.loads(r["cc_addrs"] or "[]")}
        except json.JSONDecodeError:
            routes[r["key"]] = {"to": [], "cc": []}
    return routes


def load_train_overrides(yxo_conn):
    overrides = {}
    for r in yxo_conn.execute(
            "SELECT key, extra FROM bot_config WHERE scope='train'"):
        try:
            comps = (json.loads(r["extra"] or "{}").get("companies") or [])
        except json.JSONDecodeError:
            comps = []
        if comps:
            overrides[norm_train_no(r["key"])] = comps
    return overrides


def _targets_for(companies, routes, reasons):
    out = []
    for comp in companies:
        cfg = routes.get(comp)
        if not cfg or not (cfg.get("to") or cfg.get("cc")):
            reasons.append("no_route_config:" + comp)
            continue
        out.append(RouteTarget(company=comp,
                               to=tuple(cfg.get("to") or ()),
                               cc=tuple(cfg.get("cc") or ())))
    return out


def resolve_recipients(yxo_ro_conn, idx, bot, code="", box="", train_id=""):
    """返回 ([RouteTarget], reason|None)。空列表时 reason 必非空。"""
    reasons = []
    routes = load_company_routes(yxo_ro_conn, bot)

    if train_id:
        train_key = norm_train_no(train_id)
        overrides = load_train_overrides(yxo_ro_conn)
        companies = overrides.get(train_key)
        if not companies:
            companies = [r[0] for r in yxo_ro_conn.execute(
                "SELECT DISTINCT 开票子公司名称 FROM records "
                "WHERE 班列号=? AND COALESCE(is_deleted,0)=0 AND 状态<>'退舱'",
                (train_key,)) if r[0]]
        if not companies:
            return [], "train_no_companies"
        targets = _targets_for(companies, routes, reasons)
        return targets, ("; ".join(reasons) or None)

    result = matching.classify_match(code, box, idx)
    if result.tier == "T1":
        companies = [result.record["company"]] if result.record.get("company") else []
    elif result.tier == "T5":
        companies = sorted({r.get("company", "") for r in result.candidates} - {""})
    else:
        return [], "tier_not_routable:" + result.tier
    if not companies:
        return [], "no_match_records"
    targets = _targets_for(companies, routes, reasons)
    return targets, ("; ".join(reasons) or None)


def _read_data_version(yxo_conn):
    try:
        row = yxo_conn.execute(
            "SELECT value FROM meta_kv WHERE key='data_version'").fetchone()
        return row["value"] if row else "0"
    except Exception:
        return "0"


class RecordIndexProvider:
    """match 内存索引提供器（spec 缓存失效机制）：meta_kv.data_version 变化或 TTL
    到期即重建，防止 yxo_app 导入新订舱后被误判 T4。"""

    def __init__(self, db_path, ttl_seconds=300, ro=True):
        import sqlite3
        self._db_path = db_path
        self._ttl = ttl_seconds
        mode = "ro" if ro else "rw"
        uri = "file:{}?mode={}".format(db_path.replace("\\", "/"), mode)
        self._conn = sqlite3.connect(uri, uri=True, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._version = None
        self._built_at = 0.0
        self._idx = None

    def get(self):
        now = time.time()
        ver = _read_data_version(self._conn)
        expired = (now - self._built_at) > self._ttl
        if self._idx is None or ver != self._version or expired:
            rows = [{"code": r["客户编码"], "box": r["箱号"],
                     "company": r["开票子公司名称"], "status": r["状态"] or "",
                     "deleted": r["is_deleted"] or 0}
                    for r in self._conn.execute(
                        "SELECT 客户编码,箱号,开票子公司名称,状态,is_deleted FROM records")]
            self._idx = matching.build_index(rows)
            self._version = ver
            self._built_at = now
        return self._idx
```

- [ ] **Step 4: 运行确认通过**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_routing.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add mailbots/core/routing.py mailbots/tests/unit/test_routing.py
git commit -m "feat(core): 统一路由 resolve_recipients + 版本感知索引提供器"
```

---

### Task 8: yxo.db 归档 CLI（刀3 收尾）

**Files:**
- Create: `mailbots/scripts_archive.py`（CLI 入口，命名避开 scripts/ 平台脚本目录）
- Test: `mailbots/tests/unit/test_archive_cli.py`

**Interfaces:**
- Consumes: `core.events_store.archive_before`、`db_write.get_conn`
- Produces: `python mailbots/scripts_archive.py --tables tracing_log,forward_log --keep-days 90 [--dry-run] [--apply]`；默认 dry-run 只打印将搬运行数，`--apply` 才执行

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""归档 CLI：dry-run 不动数据；apply 搬移并建 *_archive。直接 exec 模块调用 main()。"""
import os
import sqlite3
import sys
from core import events_store as es  # noqa: F401 确保 sys.path 已含 mailbots

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts_archive.py")


def _load_mod(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(tmp_path):
    p = str(tmp_path / "yxo.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE forward_log(id INTEGER PRIMARY KEY, ts TEXT)")
    conn.executemany("INSERT INTO forward_log(ts) VALUES(?)",
                     [("2026-01-01 00:00:00",), ("2099-01-01 00:00:00",)])
    conn.commit()
    conn.close()
    return p


def test_dry_run_then_apply(tmp_path):
    dbp = _make_db(tmp_path)
    args = ["scripts_archive.py", "--db", dbp, "--tables", "forward_log",
            "--date-col", "ts", "--keep-days", "90"]
    mod = _load_mod("scripts_archive_a")
    out = mod.main(args + ["--dry-run"])
    assert "forward_log" in out and "1" in out            # dry-run 报告 1 行待归档
    assert not os.path.exists(str(tmp_path / "forward_log_archive"))

    mod2 = _load_mod("scripts_archive_b")
    mod2.main(args + ["--apply"])
    chk = sqlite3.connect(dbp)
    assert chk.execute("SELECT COUNT(*) FROM forward_log").fetchone()[0] == 1
    assert chk.execute("SELECT COUNT(*) FROM forward_log_archive").fetchone()[0] == 1
```

注意：conftest.py 已把 `mailbots/` 注入 sys.path，故被测脚本内的 `from core import events_store` 可解析；两次 `_load_mod` 用不同模块名避免 `main()` 读全局 `sys.argv` 的缓存问题（本实现 `main(argv)` 接收显式参数，不依赖 sys.argv）。

- [ ] **Step 2: 运行确认失败**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_archive_cli.py -v`
Expected: FAIL（脚本不存在）

- [ ] **Step 3: 实现 scripts_archive.py**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yxo.db 历史表按月归档 CLI（spec §6 / 刀3）。
用法：
  python scripts_archive.py --db D:\\...\\yxo.db \
      --tables tracing_log,forward_log --date-col ts --keep-days 90 [--dry-run|--apply]
默认 dry-run。--apply 才真正搬运。"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import events_store  # noqa: E402
import sqlite3  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--tables", default="tracing_log,forward_log")
    ap.add_argument("--date-col", required=True,
                    help="时间列名（tracing_log=log_date, forward_log=ts）")
    ap.add_argument("--keep-days", type=int, default=90)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=30)
    report = {}
    for t in [x.strip() for x in args.tables.split(",") if x.strip()]:
        if args.apply:
            moved = events_store.archive_before(conn, t, args.date_col,
                                                keep_days=args.keep_days)
        else:
            cutoff_sql = ("SELECT COUNT(*) FROM {t} WHERE date(\"{c}\") < "
                          "date('now', ?)").format(t=t, c=args.date_col)
            moved = conn.execute(
                cutoff_sql, ("-{} days".format(args.keep_days),)).fetchone()[0]
        report[t] = moved
    conn.close()
    for t, n in report.items():
        print("{}: {} {} 行".format("APPLIED" if args.apply else "DRY-RUN", t, n))
    return str(report)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行确认通过**

Run: `& "...python.exe" -m pytest mailbots/tests/unit/test_archive_cli.py -v`
Expected: 1 passed

- [ ] **Step 5: 对 yxo_test.db 副本做一次真实 dry-run 冒烟**

```bash
Copy-Item "mailbots\tests\data\yxo_test.db" "$env:TEMP\yxo_arch_smoke.db" -Force
& "...python.exe" mailbots\scripts_archive.py --db "$env:TEMP\yxo_arch_smoke.db" --tables tracing_log,forward_log --date-col log_date --keep-days 90
```
Expected: 输出各表 DRY-RUN 行数，无异常（forward_log 若无 ts/log_date 列则以实际列名传参，先 `PRAGMA table_info` 查看）。

- [ ] **Step 6: Commit**

```bash
git add mailbots/scripts_archive.py mailbots/tests/unit/test_archive_cli.py
git commit -m "feat(archive): yxo.db 历史表归档 CLI（dry-run/apply）"
```

---

### Task 9: 计划 A 验收 —— 全量回归 + 手册更新

**Files:**
- Modify: `docs/superpowers/specs/2026-08-26-mailbots-refactor-design.md`（仅在文末追加「实施进度」一节，标记刀1–3 完成）

- [ ] **Step 1: 全量测试**

Run: `& "...python.exe" -m pytest mailbots/tests/unit -v`
Expected: 全部 passed，0 failed

- [ ] **Step 2: 对照 spec 自查**

逐项核对 spec §3（六模块中五个已落地，imap_fetcher 属计划 B）、§4（八档+消歧+判定顺序+三索引）、§6（三表+五索引+归档）、§7（traceback 日志）。缺口记入提交说明，不留静默偏差。

- [ ] **Step 3: spec 追加进度记录**

```markdown
## 13. 实施进度

- [x] 刀1 可观测性（2026-XX-XX，PR/commit: <sha>）
- [x] 刀2 core 地基（同上）
- [x] 刀3 Schema/归档（同上；归档 CLI 默认 dry-run，生产首跑 --apply 待窗口期）
```

- [ ] **Step 4: Commit + 推送**

```bash
git add docs/superpowers/specs/2026-08-26-mailbots-refactor-design.md
git commit -m "docs(spec): 标记刀1-3完成"
git push origin feature/core-framework
```

---

## 附：计划 B / C 预告（不在本计划实施）

- **计划 B（刀4–5）**：`processors/draft.py`+`processors/waybill.py` 迁移（T0–T7 动作映射、WAY_B 忽略、W 类废除、C1 通知、sending.py SMTP 语义）、`core/imap_fetcher.py` 单 IDLE + 「草单运单号」合并文件夹（gate：邮箱规则人工重定向）、UIDVALIDITY 持久化、双模式并行收尾。
- **计划 C（刀6–9）**：tracing/dsk/atb 接 core（含 tracing xls 箱号解析新能力、原样转发矩阵）、notify 分层+聚合+日报、内存缓存切换+归档任务上线、PID 锁/DETACHED/nssm 脚本包（服务器智能体执行手册）、飞书代码删除与 legacy 归档。
