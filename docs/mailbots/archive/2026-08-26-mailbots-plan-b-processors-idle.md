# MailBots 重构 · 计划 B：处理器迁移 + IDLE 长驻（刀4–5）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把草单/运单号两个机器人迁入新框架——`processors/draft|waybill` 按 spec §5 分类转发矩阵处理「草单运单号」合并文件夹邮件，`core/imap_fetcher` 以 IDLE（含轮询降级）实现秒级触达，`mailbot_serve.py` 单长驻进程托管；全程双模式并行、零重复转发。

**Architecture:** 新增 `paths`(启动自检+凭据网关) / `sending`(SMTP 返回值语义) / `identity`(公司→同事发件身份) / `notify`(企微最小通道) 四个公共模块与 `Processor` 协议；两个处理器消费计划 A 的 matching/routing/dedup/events_store；xls 解析与转发构造从旧机器人**复制移植**为纯函数模块（旧机器人本计划不改动）；`mailbot_serve.py` 组装 fetcher→dispatch。

**Tech Stack:** Python 3.13 `.venv`、sqlite3、pytest 8、imaplib(含自实现 raw-IDLE)、email/mime 标准库。

**Spec:** `docs/superpowers/specs/2026-08-26-mailbots-refactor-design.md`（§3 paths/sending/notify/接口、§5 分类与转发矩阵、刀4–5 行项）

## Global Constraints

- 解释器 `C:\Users\Roke8x\Projects\yxo-app\.venv\Scripts\python.exe`；测试 `& "...python.exe" -m pytest mailbots/tests/unit -v`（当前基线 39 passed，每任务后递增）。
- 分支 `feature/plan-b-processors-idle`（自 dev 拉）。每 Task 一 commit。
- **禁止修改任何现有生产文件**：`*_Robot.py`、`Database_Syncer.py`、`common_io.py`、`draft_pending.py`、`dedup_store.py`、`forward_log.py`、`db_write.py`、`bot_config.py`、WeComBot/**。允许创建：`mailbots/core/*`、`mailbots/processors/*`、`mailbots/mailbot_serve.py`、`mailbots/scripts_seed_routing.py`、`mailbots/tests/unit/test_planb_*.py`、`docs` 进度。
- 凭据（ACCOUNTS/secrets）永不打印、永不入测试快照。
- 源码 UTF-8 + coding 头；中文注释对齐现有风格；Windows GBK 控制台下断言不依赖 emoji。
- LIVE 门控铁律：所有真实发送/标已读代码必须经 `settings.live()` 为真才执行；默认（无环境变量、无配置）一律 TEST 干跑。
- 双模式并行不变量：新框架与旧短进程可能同时扫描同一邮箱，一切去重以 `dedup_global` claim 为准；claim 成功但处理失败必须 `release`。

---

### Task 1: core/paths.py + Processor 协议（前置三小件）

**Files:**
- Create: `mailbots/core/paths.py`
- Modify: `mailbots/core/models.py`（追加 ProcessResult 与 Processor 协议，不动已有类）
- Test: `mailbots/tests/unit/test_planb_paths.py`

**Interfaces:**
- Produces:
  - `paths.detect_root() -> str`（存在 `<root>\WeComBot\config.py` 者；候选顺序 `D:\YXO_DATA` → `\\10.0.199.184\yxo_data` → 抛 `StartupError`）
  - `paths.yxo_db_path() / events_db_path() / eml_repo_dir() -> str`
  - `paths.load_accounts() -> dict[str, str]`（优先 WeComBot config.ACCOUNTS，回退 `config_local.ACCOUNTS`，都无→空 dict 不抛）
  - `paths.smtp_endpoint() -> tuple[str, int]`
  - `paths.ensure_startup_checks() -> None`（yxo.db 只读打开能查 records 计数 + events 目录可创建；失败抛 `StartupError`）
  - `models.ProcessResult(event, action, tier=None, route=(), detail="")`；`action ∈ {"forward","pending","alarm","record","skip","ignored"}`
  - `models.Processor`（`@runtime_checkable` Protocol：`can_handle(event) -> bool`；`process(event) -> ProcessResult`）

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""paths 网关与 Processor 协议。"""
import os
import pytest
from core import paths
from core.models import MailEvent, ProcessResult, Processor


def test_detect_root_picks_candidate_with_wecombot(tmp_path):
    # 候选1 无 WeComBot\config.py；候选2 有 → 返回候选2
    cand2 = tmp_path / "root2"
    (cand2 / "WeComBot").mkdir(parents=True)
    (cand2 / "WeComBot" / "config.py").write_text("", encoding="utf-8")
    got = paths.detect_root(candidates=[str(tmp_path / "root1"), str(cand2)])
    assert got == str(cand2)


def test_detect_root_raises_when_none(tmp_path):
    with pytest.raises(paths.StartupError):
        paths.detect_root(candidates=[str(tmp_path)])


def test_load_accounts_never_raises():
    assert isinstance(paths.load_accounts(), dict)


def test_process_result_and_protocol():
    ev = MailEvent("a@cqtransit.com", "草单运单号", "<m@x>", "1", "s", "f@d", "", "")
    pr = ProcessResult(event=ev, action="record")
    assert pr.tier is None and pr.route == ()
    assert {"can_handle", "process"} <= set(Processor.__protocol_attrs__)
```

> 实现注意：`detect_root` 真实候选顺序 `D:\YXO_DATA → \\10.0.199.184\yxo_data`，测试经 `candidates=` 注入以隔离本机环境。

- [ ] **Step 2: 运行确认失败**（ModuleNotFoundError: core.paths）

- [ ] **Step 3: 实现**

```python
# -*- coding: utf-8 -*-
"""启动自检 + 凭据/端点网关（spec §3 paths.py）。
裁定：yxo.db 自检为『只读可查 records』而非写测——写测会在业务高峰制造锁竞争。"""
import os


class StartupError(RuntimeError):
    pass


_CANDIDATE_ROOTS = [r"D:\YXO_DATA", r"\\10.0.199.184\yxo_data"]
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # mailbots/


def detect_root(candidates=None):
    """返回首个含 WeComBot\\config.py 的根目录；显式传 candidates 供测试注入。"""
    for cand in (candidates or _CANDIDATE_ROOTS):
        if os.path.isfile(os.path.join(cand, "WeComBot", "config.py")):
            return cand
    raise StartupError(
        "未找到运行根目录（候选 {} 均无 WeComBot\\config.py）".format(_CANDIDATE_ROOTS))


def yxo_db_path():
    return os.path.join(detect_root(), "yxo_app", "data", "yxo.db")


def events_db_path():
    return os.path.join(_HERE, "data", "events.db")


def eml_repo_dir():
    return os.path.join(_HERE, "data", "eml_repo")


def load_accounts():
    """ACCOUNTS 网关：WeComBot config 优先（与现役机器人同源），回退 config_local。
    两者皆缺 → 空 dict（调用方据此跳过对应账号并告警，绝不抛出凭据内容）。"""
    root = None
    try:
        root = detect_root()
    except StartupError:
        root = None
    if root:
        try:
            import sys
            if root + "\\WeComBot" not in sys.path:
                sys.path.insert(0, root + "\\WeComBot")
            from config import ACCOUNTS  # type: ignore
            return dict(ACCOUNTS)
        except Exception:
            pass
    try:
        sys_path = os.path.dirname(os.path.abspath(__file__))
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from config_local import ACCOUNTS  # type: ignore
        return dict(ACCOUNTS)
    except Exception:
        return {}


def smtp_endpoint():
    root = detect_root()
    import sys
    if root + "\\WeComBot" not in sys.path:
        sys.path.insert(0, root + "\\WeComBot")
    from config import SMTP_SERVER, SMTP_PORT  # type: ignore
    return str(SMTP_SERVER), int(SMTP_PORT)


def ensure_startup_checks():
    db = yxo_db_path()
    if not os.path.isfile(db):
        raise StartupError("yxo.db 不存在: {}".format(db))
    import sqlite3
    conn = sqlite3.connect("file:{}/?mode=ro".format(db.replace("\\", "/")), uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        conn.close()
    if not n:
        raise StartupError("records 表为空，拒绝启动（疑似连错库）")
    os.makedirs(eml_repo_dir(), exist_ok=True)
```

models.py 文件头部 import 行改为：

```python
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
```

models.py 末尾追加：

```python
@dataclass(frozen=True)
class ProcessResult:
    """处理器一次处理的结果。action 见 Global Constraints 枚举。"""
    event: MailEvent
    action: str
    tier: str | None = None
    route: tuple = ()
    detail: str = ""
    def __post_init__(self):
        object.__setattr__(self, "route", tuple(self.route))


@runtime_checkable
class Processor(Protocol):
    def can_handle(self, event: MailEvent) -> bool: ...
    def process(self, event: MailEvent) -> ProcessResult: ...
```

- [ ] **Step 4: 测试通过 + 全量回归（39+3=42 passed）**

- [ ] **Step 5: Commit**

```bash
git add mailbots/core/paths.py mailbots/core/models.py mailbots/tests/unit/test_planb_paths.py
git commit -m "feat(core): paths 网关 + ProcessResult/Processor 协议（计划B前置）"
```

---

### Task 2: core/sending.py —— SMTP 发送语义

**Files:**
- Create: `mailbots/core/sending.py`
- Test: `mailbots/tests/unit/test_planb_sending.py`

**Interfaces:**
- Consumes: `paths.smtp_endpoint()`
- Produces:
  - `SendResult(ok: bool, delivered: tuple[str,...], refused: tuple[str,...], error: str)`
  - `send_smtp(msg, sender_email, sender_pwd, to_list, cc_list=()) -> SendResult`
  - 语义（spec §3 sending.py）：空收件人→ok=False,error="no_recipients"；连接/登录异常→ok=False,error=repr(e)；`sendmail` 返回**空 dict**→ok=True 全送达；非空 dict→部分拒收（delivered=已受理者，refused=被拒者，ok=False）——调用方规则：`ok→mark`；`delivered 且 refused→alarm(reason=smtp_failed)+mark`；`not delivered→release 重试`

- [ ] **Step 1: 失败测试**（monkeypatch smtplib.SMTP_SSL 为 FakeServer 类：记录 login/sendmail 调用；三个场景——全送达返回 `{}`、部分拒收 `{"bad@x": (550,b"...")}`、连接抛 OSError）

```python
# -*- coding: utf-8 -*-
"""sending.py 三场景：全送达 / 部分拒收 / 连接失败。"""
from email.message import EmailMessage
from core import sending


def _msg():
    m = EmailMessage()
    m["Subject"] = "t"
    m.set_content("body")
    return m


def test_all_delivered(monkeypatch):
    calls = {}
    monkeypatch.setattr(sending.smtplib, "SMTP_SSL", _fake(calls, refused={}))
    r = sending.send_smtp(_msg(), "s@cqtransit.com", "pwd",
                          ["a@b.com"], ["c@d.com"])
    assert r.ok and r.delivered == ("a@b.com", "c@d.com") and not r.refused
    assert calls["login"] == ("s@cqtransit.com", "pwd")


def test_partial_refusal(monkeypatch):
    monkeypatch.setattr(sending.smtplib, "SMTP_SSL",
                        _fake({}, refused={"bad@x": (550, b"no mailbox")}))
    r = sending.send_smtp(_msg(), "s@cqtransit.com", "p", ["good@x", "bad@x"])
    assert not r.ok
    assert r.delivered == ("good@x",) and r.refused == ("bad@x",)


def test_connect_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("conn reset")
    monkeypatch.setattr(sending.smtplib, "SMTP_SSL", boom)
    r = sending.send_smtp(_msg(), "s@x", "p", ["a@b.com"])
    assert not r.ok and "conn reset" in r.error


def _fake(calls, refused):
    class Fake:
        def __init__(self, host, port, timeout=None):
            calls["endpoint"] = (host, port)
        def login(self, u, p):
            calls["login"] = (u, p)
        def sendmail(self, frm, tos, payload):
            calls["tos"] = tos
            return dict(refused)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return Fake
```

- [ ] **Step 2: FAIL 确认**

- [ ] **Step 3: 实现**

```python
# -*- coding: utf-8 -*-
"""SMTP 发送语义（spec §3 sending.py）：
成功定义 = smtplib.sendmail() 返回空 dict。部分拒收不抛异常，必须检查返回值。"""
import smtplib
from dataclasses import dataclass
from core import paths


@dataclass(frozen=True)
class SendResult:
    ok: bool
    delivered: tuple = ()
    refused: tuple = ()
    error: str = ""


def send_smtp(msg, sender_email, sender_pwd, to_list, cc_list=()):
    tos = [a for a in list(to_list) + list(cc_list) if a]
    if not tos:
        return SendResult(ok=False, error="no_recipients")
    host, port = paths.smtp_endpoint()
    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(sender_email, sender_pwd)
            refused = s.sendmail(sender_email, tos, msg.as_string())
    except Exception as e:
        return SendResult(ok=False, error=repr(e))
    delivered = tuple(t for t in tos if t not in refused)
    return SendResult(ok=not refused, delivered=delivered,
                      refused=tuple(refused.keys()))
```

- [ ] **Step 4: PASS + 回归（45 passed）**

- [ ] **Step 5: Commit** `feat(core): sending SMTP 返回值语义 SendResult`

---

### Task 3: core/identity.py —— 公司→负责同事→发件身份

**Files:**
- Create: `mailbots/core/identity.py`
- Test: `mailbots/tests/unit/test_planb_identity.py`

**Interfaces:**
- Produces:
  - `sender_for(company: str) -> tuple[str | None, str | None]`：`(发件邮箱, 负责同事真名)`；映射来自 WeComBot config 的 `COMPANY_TO_EMAIL`/`company_to_name`（关键词子串匹配，与现役行为一致）；导入失败→`(None, None)`；提供 `override_json(path)` 供测试注入 `{"公司关键词": "邮箱"}` 形状的覆盖层（优先生效）
  - `real_name_of(company: str) -> str | None`

- [ ] **Step 1: 失败测试**（override_json 注入 `{"港九港铁": "maoxiaoyang@cqtransit.com"}` → `sender_for("港九港铁子公司")` 返回该邮箱；未匹配公司返回 `(None,None)`；不依赖生产 WeComBot 环境——实现里 import 失败静默降级）

- [ ] **Step 2: FAIL**

- [ ] **Step 3: 实现**

```python
# -*- coding: utf-8 -*-
"""发送身份（spec §5.3）：负责公司 →(关键词映射)→ 负责同事 → 其 cqtransit 邮箱。
映射真相源 = WeComBot config.USER_COMPANIES/USER_EMAILS；找不到同事 → 调用方 alarm。"""
import json
import os

_override = None


def override_json(path):
    """测试/运维覆盖层：{"公司关键词": {"email":..., "name":...}} 或 {"kw": "邮箱"}"""
    global _override
    with open(path, encoding="utf-8") as f:
        _override = json.load(f)


def _wecombot_maps():
    """返回 (company_kw->email dict, company_kw->realname dict)；失败给空表。"""
    kw_email, kw_name = {}, {}
    try:
        from core import paths
        root = paths.detect_root()
        import sys
        wb = os.path.join(root, "WeComBot")
        if wb not in sys.path:
            sys.path.insert(0, wb)
        from config import COMPANY_TO_EMAIL, company_to_name  # type: ignore
        kw_email = dict(COMPANY_TO_EMAIL)
        for kw in kw_email:
            nm = company_to_name(kw)
            if nm:
                kw_name[kw] = nm
    except Exception:
        pass
    return kw_email, kw_name


def sender_for(company):
    c = str(company or "")
    if _override:
        for kw, val in _override.items():
            if kw in c:
                email = val["email"] if isinstance(val, dict) else val
                name = val.get("name") if isinstance(val, dict) else None
                return email, name
    kw_email, kw_name = _wecombot_maps()
    for kw, email in kw_email.items():
        if kw in c:
            return email, kw_name.get(kw)
    return None, None


def real_name_of(company):
    return sender_for(company)[1]
```

- [ ] **Step 4: PASS + 回归**

- [ ] **Step 5: Commit** `feat(core): identity 发送身份映射（WeComBot 同源+覆盖层）`

---

### Task 4: core/notify.py —— 企微通知最小通道

**Files:**
- Create: `mailbots/core/notify.py`
- Test: `mailbots/tests/unit/test_planb_notify.py`

**Interfaces:**
- Produces:
  - `notify_realtime(real_name: str | None, text: str) -> bool`（通道=`cs_bot/wecom_api.notify_by_name`，延迟导入+失败吞掉返 False；`real_name` 为 None 直接 False）
  - `notify_alarm(names: list[str], reason: str, text: str) -> None`：reason 空则抛 `ValueError`（spec：alarm 强制 reason）；逐个 name 走 realtime 通道；5min 聚合属刀7 在本模块内扩展，本期不做
  - `set_channel(fn)`：测试注入 `(name,text)->(ok,channel)`

- [ ] **Step 1: 失败测试**（set_channel 注入假通道：realtime 正常转发文本；real_name=None 不调用；notify_alarm 空 reason 抛 ValueError、多 names 逐一送达）

- [ ] **Step 2: FAIL**

- [ ] **Step 3: 实现**

```python
# -*- coding: utf-8 -*-
"""企微通知最小通道（spec §3 notify.py 的 Plan-B 子集；
digest/聚合在刀7 于本模块内扩展）。"""
_channel = None
try:
    from core import paths
    _root = paths.detect_root()
    import sys
    _cs = _root + "\\WeComBot\\cs_bot"
    if _cs not in sys.path:
        sys.path.insert(0, _cs)
    from cs_bot.wecom_api import notify_by_name as _real_channel  # noqa
    _channel = _real_channel
except Exception:
    _channel = None


def set_channel(fn):
    global _channel
    _channel = fn


def notify_realtime(real_name, text):
    if not real_name or _channel is None:
        return False
    try:
        ok, _ch = _channel(real_name, text)
        return bool(ok)
    except Exception:
        return False


def notify_alarm(names, reason, text):
    if not reason:
        raise ValueError("alarm 必须携带 reason（spec §8 杜绝不知为何而报警）")
    for n in names or []:
        notify_realtime(n, "[alarm:{}]\n{}".format(reason, text))
```

- [ ] **Step 4: PASS + 回归**

- [ ] **Step 5: Commit** `feat(core): notify 最小企微通道（realtime/alarm，reason 强制）`

---

### Task 5: processors/xlsio.py —— 运单号 xls 纯函数移植

**Files:**
- Create: `mailbots/processors/__init__.py`（空）、`mailbots/processors/xlsio.py`
- Test: `mailbots/tests/unit/test_planb_xlsio.py`

**Interfaces:**
- Produces（从 `Waybill_Robot.py:397-611` 复制移植的纯函数，签名保持一致以便对照验收）：
  - `parse_waybill_xls(raw_bytes) -> list[dict]`（键 `客户编码/箱号/运单号`；xlrd/openpyxl/latin-1 文本三分支照搬）
  - `xls_all_rows(raw_bytes, name) -> (headers, rows) | (None, None)`
  - `rewrite_xls_filtered(raw_bytes, name, keep_rows) -> (new_bytes, new_name) | (None, None)`
- 依赖注意：xlrd 未装（计划 A 曾临时装到 Temp libs）——本任务把 `xlrd` 写进 `requirements-dev.txt` 并安装；openpyxl 若缺同样补。

- [ ] **Step 1: 失败测试**（用 testset waybill 夹具 W06_multirow_mixed.eml 提取 .xlsx 附件字节 → parse 出 ≥3 行且列键正确；rewrite 过滤后行数=keep_rows 数、其余行消失）

```python
# -*- coding: utf-8 -*-
"""xlsio 用真实夹具做端到端解析/重写验证。"""
import email
import os
from email import policy
from processors import xlsio

FIX = os.path.join(os.path.dirname(__file__), "..", "testset", "test_fixtures")


def _att_bytes(eml_name, needle=".xls"):
    with open(os.path.join(FIX, "waybill", eml_name), "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    for part in msg.walk():
        fn = part.get_filename() or ""
        if needle in fn.lower():
            return part.get_payload(decode=True), fn
    return None, None


def test_parse_w06_rows():
    raw, name = _att_bytes("W06_multirow_mixed.eml")
    assert raw, "夹具应含 xls 附件"
    rows = xlsio.parse_waybill_xls(raw)
    assert len(rows) >= 2
    assert set(rows[0]) == {"客户编码", "箱号", "运单号"}


def test_rewrite_keeps_only_requested(tmp_path):
    raw, name = _att_bytes("W06_multirow_mixed.eml")
    rows = xlsio.parse_waybill_xls(raw)
    keep = rows[:1]
    out, out_name = xlsio.rewrite_xls_filtered(raw, name, keep)
    assert out and out_name
    back = xlsio.parse_waybill_xls(out)
    assert len(back) == 1
    assert back[0]["客户编码"] == keep[0]["客户编码"]
```

- [ ] **Step 2: FAIL → Step 3: 实现**（把 Waybill_Robot.py:397-611 五个函数原样移植：`_cell_str/_col_index/get_attachment_bytes 不需要——get_attachment_bytes 留在旧处，xlsio 只收 raw bytes；parse_waybill_xls/_xls_all_rows/rewrite_xls_filtered 及其私有助手`。xlrd 分支缺库时抛 ImportError 由调用方降级）→ **Step 4: PASS + 回归** → **Step 5: Commit** `feat(processors): xlsio 纯函数移植（parse/all_rows/rewrite_filtered）`

---

### Task 6: processors/forward_builder.py —— 草单转发构造移植

**Files:**
- Create: `mailbots/processors/forward_builder.py`
- Test: `mailbots/tests/unit/test_planb_forward_builder.py`

**Interfaces:**
- Produces（从 `Draft_Forward_Robot.py:163-166,527-570` 移植）：
  - `CATEGORY_LABEL = {"A": "【转草单】", "B": "【草单更新】", "C1": "【反馈问题】", "C2": "确认回复(不转发)", "WAY_A": "运单号确认", "WAY_B": "驳回告警"}`
  - `build_forward(raw_bytes, category, extra_note=None) -> tuple[MIMEMultipart, str]`（重组 mixed：HTML 优先/B 类前置红色提示/extra_note 拼接/附件重挂；主题前缀仅 B 类加 CATEGORY_LABEL）
  - `test_subject(orig_subject, orig_to, label) -> str`（`[测试·{label}→原收件人:{orig_to}] {subj}`，LIVE 门控时由处理器调用）

- [ ] **Step 1: 失败测试**（用 real_imap_samples 的 draft eml 原始字节：build_forward 后 subject 含【草单更新】当 category=B；附件数与原件一致；HTML 正文存在时保留 HTML 部分；extra_note 出现在正文首部）

- [ ] **Step 2: FAIL → Step 3: 实现**（移植 :527-570 主体，label 逻辑内聚）→ **Step 4: PASS + 回归** → **Step 5: Commit** `feat(processors): forward_builder 移植（label/重组/附件）`

---

### Task 7: scripts_seed_routing.py —— bot_config 路由种子迁移

**Files:**
- Create: `mailbots/scripts_seed_routing.py`
- Test: `mailbots/tests/unit/test_planb_seed.py`

**背景（必读）**：现役草单/运单号路由读 `dsk_config_cache.json`（default_map: 公司→{to,cc}; box_record_map: 箱→公司），而新架构读 `bot_config(bot, scope='company')`。实测测试库里 draft/waybill 两 bot 无任何 company 行。本脚本是数据迁移闸门。

**Interfaces:**
- Produces: CLI `--db <yxo.db> [--from-cache <dsk_config_cache.json>] [--from-bot dsk] --into draft,waybill [--dry-run|--apply]`
- 行为：取源映射（cache 的 default_map 优先，否则从 bot=dsk 的 company 行复制）；对每个 `--into` bot `INSERT OR IGNORE INTO bot_config(bot,scope,key,to_addrs,cc_addrs,extra) VALUES(?, 'company', 公司, json(to), json(cc), NULL)`；dry-run 打印将写入条数；apply 后打印实际新增数

- [ ] **Step 1: 失败测试**（tmp sqlite 建 bot_config 表 + 预置 dsk 的 2 条 company 行 + 一个 cache JSON 文件两用例各跑 dry-run/apply；断言 dry-run 不写、apply 幂等重跑不重复）

- [ ] **Step 2: FAIL → Step 3: 实现**（argparse；json.dumps ensure_ascii=False）→ **Step 4: PASS + 回归** → **Step 5: Commit** `feat(seed): bot_config 草单/运单号 company 路由种子迁移脚本`

---

### Task 8: processors/draft.py —— 草单处理器

**Files:**
- Create: `mailbots/processors/draft.py`
- Test: `mailbots/tests/unit/test_planb_draft_proc.py`

**Interfaces:**
- Consumes: matching.classify_match/build_index/is_iso_box/is_client_code_candidate、events_store(seen_seq_add/save_eml)、identity.sender_for、notify、forward_builder、sending.send_smtp、paths.load_accounts。**路由经 `ctx.resolve(code, box, train_id="") -> ([RouteTarget], reason|None)` 注入**（serve 接线 core/routing；测试注入 fake）——处理器自身不直接持有 yxo 连接。dedup 的 claim/release 由 serve 层统一负责，处理器不触碰。
- Produces: `DraftProcessor(ctx)` 实现 Processor 协议；`ctx` 为 `SimpleNamespace(conn_events, idx, accounts, live, smtp, resolve, send, add_pending, alarm, notify)`（由 serve 注入；测试全 fake）

**处理序（spec §5.2/§5.4 逐字实现）：**
1. `can_handle`: folder ∈ {"草单运单号","&j9BTVYNJU1U-","运单草单"}（过渡期兼容旧夹具名）且非运单号白名单件
2. 剥回复前缀（`Re:`/`回复:`/`答复:`/`【草单更新】`循环剥离至稳定）→ 得 clean_subject
3. 噪音清单前置：附件名或主题命中 `出区放行|报关单|INVOICE|clipboard|货协运单` 等 → `record`+detail=noise
4. 草单件判定（无发件人门槛）：ENC_PDF_RE 命中，或 BOX_PDF_RE+正文更新关键词（关键词沿用 UPDATE_KEYWORDS 四词）
5. 提取：clean_subject 先 CODE_RE 全匹配→按 `is_client_code_candidate` 过滤得 code；CONTAINER_RE 取 box
6. A/B：`is_new = events_store.seen_seq_add(conn_events, seq, message_id)`（**遇到即写**，返回值 True=首见）；`category = "A" if is_new else "B"`
7. `classify_match(code, box, idx)`：
   - T1→resolve→有 target：`forward`（identity.sender_for(company)，回退 ADMIN 账号；send_smtp；ok→events save_eml+dedup 保持+forward_log.record+notify_realtime(owner本人,C1 时也通知)；refused 部分拒收→alarm(smtp_failed)+仍 mark；全拒→release+下轮重试）
   - T2/T3/T6/T7→`pending`（draft_pending.add_pending，info 键集与旧版一致：message_id/subject/sender/date/category/code/num/box/company/owner=identity.real_name_of(company) or ADMIN_NAME/reason=tier 中文文案/candidates/boxes_seen/to/cc）
   - T4/T5/T0→`alarm`（notify_alarm([owner or ADMIN_NAME, ADMIN_NAME], reason=tier, text=主题+提取物摘要）；不进待办）
8. C2/内部回复链→`record`；退舱（rec.status=='退舱'）→T4 cancelled_only 路径已报警，不再单独 skip
9. 返回 ProcessResult(action, tier, route, detail)

**测试（夹具驱动，全部 fake ctx：内存 sqlite events 库 + 合成 idx + fake send/pending/notify 记录器）。** 骨架（其余场景按同构展开，夹具→期望动作一一对应）：

```python
# -*- coding: utf-8 -*-
"""draft 处理器：夹具→动作矩阵。fake ctx 注入，绝不真发 SMTP/企微。"""
import email
import os
from types import SimpleNamespace
from core import events_store as es
from core import matching
from processors.draft import DraftProcessor

FIX = os.path.join(os.path.dirname(__file__), "..", "testset", "test_fixtures")


def _raw(rel):
    with open(os.path.join(FIX, rel), "rb") as f:
        return f.read()


def _ev_and_ctx(rel, rows, monkeypatch):
    raw = _raw(rel)
    msg = email.message_from_bytes(raw)
    ev = MailEvent("a@cqtransit.com", "草单运单号", msg["Message-ID"] or "<x>",
                   "1", str(msg["Subject"] or ""), "s@d", "", "")
    conn = es.connect(":memory:")
    es.ensure_schema(conn)
    idx = matching.build_index(rows)
    sent, pendings, alarms = [], [], []
    from core.models import RouteTarget
    ctx = SimpleNamespace(
        conn_events=conn, idx=idx, accounts={"maoxiaoyang@cqtransit.com": "pwd"},
        live=False, smtp=("smtp.test", 465),
        resolve=lambda code, box, train_id="": (
            [RouteTarget(company="莫斯科子公司", to=("ops@moscow.example",))], None),
        send=lambda m, fr, pw, to, cc=(): sent.append((fr, tuple(to))) or sending.SendResult(True, tuple(to)),
        add_pending=lambda info, raw, test=True, simulated=False: pendings.append(info) or 1,
        alarm=lambda names, reason, text: alarms.append((tuple(names), reason)),
        notify=lambda name, text: True,
    )
    return ev, ctx, sent, pendings, alarms


_ROWS = [{"code": "CQWLJT260713004-BLLST", "box": "TSRU8008478",
          "company": "莫斯科子公司", "status": "", "deleted": 0}]


def test_D01_full_forwards(monkeypatch):
    ev, ctx, sent, pendings, alarms = _ev_and_ctx(
        "draft/D01_A_full.eml", _ROWS, monkeypatch)
    res = DraftProcessor(ctx).process(ev)
    assert res.action == "forward"
    assert len(sent) == 1 and not pendings and not alarms


def test_D07_cancelled_only_alarms():
    rows = [dict(_ROWS[0]),
            {"code": "CQWLJT260713006-VXN", "box": "", "company": "",
             "status": "退舱", "deleted": 0}]
    ev, ctx, sent, pendings, alarms = _ev_and_ctx("draft/D07_tc_excluded.eml", rows, None)
    res = DraftProcessor(ctx).process(ev)
    assert res.action == "alarm" and res.tier == "T4"
```

其余场景参数化：D03→forward(category=B)；D04→forward+notify 被调；D05→record；D11/D12→alarm；D09/D10/D13/D14→pending 且 info 键含 owner/reason/candidates。

- [ ] Steps: ①失败测试 ②FAIL ③实现 ④PASS+回归（预计 +9）⑤Commit `feat(processors): draft 处理器（八档动作矩阵+AB台账+C1通知）`

---

### Task 9: processors/waybill.py —— 运单号处理器

**Files:**
- Create: `mailbots/processors/waybill.py`
- Test: `mailbots/tests/unit/test_planb_waybill_proc.py`

**Interfaces:**
- Consumes: 同 Draft + xlsio
- Produces: `WaybillProcessor(ctx)`；`can_handle`: 白名单（默认 `docwbfb@yxologistics.com`，cfg 可覆）且（folder∈运单类 或 有 .xls 附件）

**处理序：**
1. 主题含「单证审核驳回」→ **ignored**（audit 日志一行，零通知零待办——2026-08-26 拍板 WAY_B 不做任何处理）
2. WAY_A：`xlsio.parse_waybill_xls` → 行级独立处理（拍板：报警行不阻断其他行）：
   - 每行 classify_match：T1→resolve→分组键(to,cc)；T2/T3/T6/T7→pending_rows；T4/T5/T0→alarm_rows（notify_alarm 即时逐行，含 owner 若识别）
   - 退舱行→T4(cancelled_only) alarm（统一规则）
   - 分组发送：`rewrite_xls_filtered` 生成该公司小 xls（失败保守附原始）→ `build_forward(raw,"WAY_A")` 重组 → identity.sender_for(company) 发信 → `ledger_insert_waybill`（识别即留底 pending）→ 成功 `ledger_mark_waybill_sent` + forward_log.record(note="WAY_A 拆分转发(N行)")
   - 无公司/无路由/发送失败行→unresolved→pending（info 带 rows 子集）
3. 全部行 resolved 且发送成功→mark seen；存在 unresolved→pending 队列（复用 add_pending, category="WAY_A"）

**测试**（W01_single_external→forward+留底 sent；W06_multirow_mixed→拆 N 封各行归公司；W08_multirow_with_cancel→VXN 行 alarm、其余行照常拆分转发（2026-08-26 拍板行间独立）；W09_multirow_with_unknown→unknown 行 alarm；W05_unknown→ignored；W04_no_xls→ignored）

- [ ] Steps: ①失败测试 ②FAIL ③实现 ④PASS+回归（预计 +5）⑤Commit `feat(processors): waybill 处理器（WAY_A 行级拆分+WAY_B忽略+行间独立告警）`

---

### Task 10: core/imap_fetcher.py —— IDLE 抓取器（raw-IDLE + UIDVALIDITY + 轮询降级）

**Files:**
- Create: `mailbots/core/imap_fetcher.py`
- Test: `mailbots/tests/unit/test_planb_fetcher.py`

**Interfaces:**
- Produces:
  - `utf7_encode(folder: str) -> str`（modified UTF-7，「草单运单号」→ `&Xn9BTVP3-` 形态；实现 base64 utf-16-be 变体）
  - `MailState(state_path)`：`get(account,folder)->(uidvalidity,last_uid)` / `update(account,folder,uv,last_uid)`（JSON 原子写）
  - `fetch_new(conn, folder_srv, state, account) -> list[tuple[int, bytes]]`：select readonly → 校验 UIDVALIDITY（变化→清 last_uid 全量重扫）→ `UID SEARCH UID {last+1}:*` → 逐封 `UID FETCH (BODY.PEEK[])` → 更新 state → 返回 [(uid, raw)]
  - `Idler(account, password, folders_srv: list[str], on_raw: Callable[[str,str,int,bytes],None], state_path, max_idle=1740, poll_fallback_secs=0)`：线程；循环 = 对每个 folder `fetch_new` → `send IDLE` 起 29 分钟窗口（socket 层读 untagged 行）→ 收到 EXISTS/超时 → `DONE` → 再 fetch_new；任何异常→指数退避重连（base 30s cap 300s）；`poll_fallback_secs>0` 时不用 IDLE 改定时轮询（服务端不支持时的开关）
  - raw-IDLE 实现：`typ, data = conn.select(...)` 后 `sock = conn.socket(); sock.send(("X IDLE\r\n").encode())`——注意 imaplib 允许未知命令直接走 `conn.send()`+`conn.readline()`？**裁定：使用 `conn.send(command)` 与 `conn.readline()` 原语拼装**（imaplib 公开这两个底层方法，无需碰私有 socket 细节），收到 `+ idling` 进入等待，读到含 `EXISTS` 的 untagged 行或超时后发 `DONE\r\n` 收完成行。服务器若拒绝 IDLE（无 `+` 响应）→ 该账号自动降级轮询并记日志。

- [ ] **Step 1: 失败测试**（FakeConn/sock 内存实现：①utf7_encode("草单运单号") 结果再经探针已验证的解码函数还原相等；②fetch_new 首次返回全部、第二次返回空、插入新 uid 后返回增量；③uidvalidity 变化触发全量；④Idler 用 fake conn 驱动一轮 fetch→IDLE→EXISTS→DONE→fetch 循环后 stop()）

- [ ] **Step 2: FAIL → Step 3: 实现 → Step 4: PASS + 回归 → Step 5: Commit** `feat(core): imap_fetcher raw-IDLE+UIDVALIDITY 增量+轮询降级`

---

### Task 11: mailbot_serve.py —— 单长驻入口 + 双模式并行

**Files:**
- Create: `mailbots/mailbot_serve.py`
- Test: `mailbots/tests/unit/test_planb_serve.py`

**Interfaces:**
- Produces:
  - `build_context(live: bool|None) -> SimpleNamespace`（paths.ensure_startup_checks、events connect+ensure_schema、yxo ro conn、RecordIndexProvider、accounts、smtp endpoint、ADMIN_NAME="毛骁洋"、folders 配置读取）
  - `FOLDERS_DEFAULT = [("草单运单号", utf7)]`；`FOLDERS_TRANSITION` 追加 `[("&j9BTVVP3-","运单号"),("&j9BTVYNJU1U-","运单草单")]`（env `MERGED_FOLDER_ONLY=1` 时剔除旧文件夹——邮箱规则重定向完成后的收尾开关）
  - `main(argv=None)`：`--live`（缺省 TEST 干跑：process 结果只打印不发送不标读）、`--poll-secs N`（轮询降级）、`--once`（单轮扫描退出，供冒烟）；组装 `DraftProcessor(ctx)`+`WaybillProcessor(ctx)` 注册表 → `on_raw` 回调：MailEvent 构造 → dedup.try_claim(message_id 或 synthetic_key) → 首个 can_handle 的处理器.process → action==forward/pending/alarm 成功路径保持 claim，skip/ignored/异常→release
  - `--once` 冒烟模式供灰度前人工演练

- [ ] **Step 1: 失败测试**（fake fetcher 注入两封 eml：一封草单一封运单号 → 断言分发到正确处理器、TEST 模式下无 SMTP 调用、dedup 二次投递被 claim 挡下、异常路径 release）
- [ ] **Step 2–5:** FAIL→实现→PASS+全量回归→Commit `feat(serve): mailbot_serve 单进程组装（IDLE+双模式并行+TEST/LIVE 门控）`

---

### Task 12: 计划 B 验收 —— 全量回归 + spec 进度

- [ ] Step 1: `pytest mailbots/tests/unit -v` 全绿（预计累计 80±5 passed）
- [ ] Step 2: 对照 spec §5 转发矩阵逐格打勾（draft 不拆/waybill 拆/WAY_B ignored/C1 通知/两档呈现+alarm 第三态）
- [ ] Step 3: spec 文末「13. 实施进度」追加刀4–5 行（注明：上线 gate=骁洋改邮箱规则；并行期三文件夹同 watch）
- [ ] Step 4: Commit + push（push 由控制者在 finishing 阶段执行）

---

## 附：计划 C 边界（不在本计划）

刀6 tracing/dsk/atb 接 core（tracing xls 箱号解析、原样转发矩阵核对）；刀7 notify digest+5min 聚合+每日日报；刀8 内存缓存切换生产+归档任务排程；刀9 PID 锁/DETACHED/nssm 脚本包（服务器智能体执行手册）、飞书代码删除、legacy 归档、Database_Syncer 下线。
