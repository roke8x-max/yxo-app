"""任务 19（B）：运踪按公司整封副本。

口径（旧系统 `mailbots/Tracing_Robot_IMAP.py`）：一封运踪邮件 = **命中公司数封
整封副本**，箱号完全不参与拆分。新系统提取层带 XLS 附件时会按箱号拆成 N 行
（TracingExtractor），serve 的行循环又是每行独立 claim、独立发送 ⇒ 20 个箱号
变成 20 封。本文件验「收敛为稳定的代表行」之后的行为。

夹具全部本机自建：tmp_path 下的库 + 现场用 xlwt 生成的 XLS 附件；
不连生产库 / 生产邮箱 / 生产 SMTP。班列号用 123（与线上事件无关，非事件标识）。
"""
import email
import email.policy
import io
import sqlite3
from email.message import EmailMessage
from unittest.mock import Mock

import pytest

from mailbots_next.config import (
    BOT_CONFIG_DB_PATH,
    DEDUP_DB_PATH,
    DAILY_COUNTERS_PATH,
    EmailType,
)
from mailbots_next.core.dedup import init_db, get_pending_errors
from mailbots_next.core.rowkey import serve_row_key
from mailbots_next.core.routing import RoutingResult
from mailbots_next.core.store import init_bot_config_db, init_forward_log, seed_owner_mapping

SENDER = "tracing-system@yxologistics.com"
SUBJECT = "Tracing info of YXO train 123"
DATE_HDR = "Tue, 16 Sep 2026 10:00:00 +0000"


@pytest.fixture(autouse=True)
def _dbs(tmp_path, monkeypatch):
    """每个用例一套干净的 dedup / bot_config / counters 库。"""
    monkeypatch.setenv("MAILBOT_MODE", "test")
    for p in (BOT_CONFIG_DB_PATH, DEDUP_DB_PATH, DAILY_COUNTERS_PATH):
        try:
            if p.exists():
                p.unlink()
        except PermissionError:
            pass
    init_db()
    init_bot_config_db()
    init_forward_log()
    seed_owner_mapping()
    try:
        yield
    finally:
        from mailbots_next.config.provider import invalidate_cache
        invalidate_cache()


@pytest.fixture
def proc(monkeypatch):
    """live 模式的 MailProcessor：forward_log 只在 live 下落库（store.write_forward_log）。

    SMTP 在用例里 patch，绝不真发信；mark_seen 也打桩 —— 它只是「已读」这项 UX
    标记，不属于本文件要验的业务，桩掉可让夹具彻底不碰 IMAP。
    """
    monkeypatch.setenv("MAILBOT_MODE", "live")
    from mailbots_next.config import snapshot
    from mailbots_next.serve import MailProcessor

    p = MailProcessor(snapshot())
    p.records = []
    p.accounts = {"t@t.com": "pw"}
    p.notifier = Mock()
    yield p


def _boxes(n):
    """n 个合法箱号（CONTAINER_RE = 4 字母 + 7 位数字）。"""
    return ["TCLU%07d" % (i + 1) for i in range(n)]


def _xls_bytes(boxes):
    xlwt = pytest.importorskip("xlwt")
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    ws.write(0, 0, "container_no")
    ws.write(0, 1, "destination")
    ws.write(0, 2, "status")
    for i, box in enumerate(boxes, start=1):
        ws.write(i, 0, box)
        ws.write(i, 1, "Moscow")
        ws.write(i, 2, "in_transit")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _raw_tracing(msgid="<tr-copy-1@x>", boxes=None):
    """一整封运踪原邮件：主题带班列车次 + 带箱号的 XLS 附件。"""
    m = EmailMessage()
    m["Message-ID"] = msgid
    m["Subject"] = SUBJECT
    m["From"] = "Tracing <%s>" % SENDER
    m["Date"] = DATE_HDR
    m.set_content("tracing body")
    if boxes is not None:
        m.add_attachment(_xls_bytes(boxes), maintype="application",
                         subtype="octet-stream", filename="tracing.xls")
    return m.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _route(companies):
    """每家公司一个 RoutingResult（route_tracing 的真实返回形态就是 list）。"""
    return [RoutingResult(c, None, [to], [], "train_match", True) for c, to in companies]


def _fl_rows(message_id):
    """按 message_id 读 forward_log（用 sqlite3 直读，不 import 写入模块）。"""
    from mailbots_next.core import store as store_mod
    conn = sqlite3.connect(str(store_mod.BOT_CONFIG_DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM forward_log WHERE message_id=? ORDER BY rowid", (message_id,))]
    finally:
        conn.close()


def _run(proc, raw, message_id, routes, uid=1):
    """跑一遍 process_email，返回 (execute_action Mock, send_smtp Mock)。

    execute_action 用 side_effect 包住真函数：既统计进入次数，又让 forward_log
    真的落库（不 mock 掉写入侧，否则「封数 == 行数」就成了自证）。
    """
    from unittest.mock import patch
    from mailbots_next import serve as serve_mod

    real_execute_action = serve_mod.execute_action
    with patch("mailbots_next.serve.route_row", return_value=routes), \
         patch("mailbots_next.serve.mark_seen", return_value=True), \
         patch("mailbots_next.serve.execute_action", side_effect=real_execute_action) as mock_act, \
         patch("mailbots_next.core.act.send_smtp", return_value={"success": True}) as mock_send:
        ok = proc.process_email("t@t.com", "Tracing", message_id, uid, SUBJECT,
                                "Tracing <%s>" % SENDER, DATE_HDR, raw)
    assert ok is True
    return mock_act, mock_send


# ---------------- 1. 20 行同 1 公司 → 1 封、1 行、代表行 key ----------------

def test_twenty_rows_one_company_sends_one_copy(proc):
    raw = _raw_tracing(boxes=_boxes(20))
    from mailbots_next.core.extract import extract_email
    from mailbots_next.core.notify import get_counters
    rows = extract_email(EmailType.TRACING, raw)
    assert len(rows) == 20, "夹具前提：提取层仍按箱号拆 20 行（本任务不改提取）"

    before = get_counters().get("action_forward", 0)
    mock_act, mock_send = _run(proc, raw, "tr-one-company",
                              _route([("太平洋", "a@x")]))
    after = get_counters().get("action_forward", 0)

    assert mock_act.call_count == 1, "20 箱同 1 公司 ⇒ 只许 1 次 execute_action"
    assert mock_send.call_count == 1, "20 箱同 1 公司 ⇒ 只许发 1 封"

    fl = _fl_rows("tr-one-company")
    assert len(fl) == 1
    assert mock_send.call_count == len(fl), "实发封数必须等于 forward_log 新增行数"
    # 计数口径同步核对：action_forward 与实际封数 / 落库行数必须是同一个数
    assert after - before == mock_send.call_count == len(fl)
    # 代表行来自 serve_row_key（不另写一份 key 公式），且是提取结果的第一行
    assert fl[0]["row_key"] == serve_row_key(rows[0])
    assert fl[0]["row_key"] == "0:%s" % rows[0].container_no
    # 任务 18 字段口径：robot/company/train_no 有值，code/box 留空
    assert fl[0]["robot"] == "运踪"
    assert fl[0]["company"] == "太平洋"
    assert fl[0]["train_no"] == "123"
    assert fl[0]["code"] == ""
    assert fl[0]["box"] == ""


# ---------------- 2. 20 行 × 2 公司 → 2 封、2 行 ----------------

def test_twenty_rows_two_companies_send_one_copy_each(proc):
    raw = _raw_tracing(msgid="<tr-copy-2@x>", boxes=_boxes(20))
    mock_act, mock_send = _run(proc, raw, "tr-two-companies",
                              _route([("太平洋", "a@x"), ("东盟", "b@x")]))

    assert mock_act.call_count == 2, "多公司仍由 route_tracing 扇出：N 家 N 封"
    assert mock_send.call_count == 2
    fl = _fl_rows("tr-two-companies")
    assert len(fl) == 2
    assert {r["company"] for r in fl} == {"太平洋", "东盟"}
    assert len({r["forward_id"] for r in fl}) == 2, "两封必须各有独立 forward_id"


# ---------------- 3. 无 XLS（1 行）→ 与基线一致：1 封 ----------------

def test_single_row_without_xls_behaves_as_before(proc):
    raw = _raw_tracing(msgid="<tr-copy-3@x>", boxes=None)
    from mailbots_next.core.extract import extract_email
    rows = extract_email(EmailType.TRACING, raw)
    assert len(rows) == 1, "夹具前提：无附件时提取层只给 1 行"

    mock_act, mock_send = _run(proc, raw, "tr-no-xls", _route([("太平洋", "a@x")]))

    assert mock_act.call_count == 1
    assert mock_send.call_count == 1
    fl = _fl_rows("tr-no-xls")
    assert len(fl) == 1
    assert fl[0]["row_key"] == serve_row_key(rows[0]) == "0:no_key"


# ---------------- 4. 同一 message_id 重扫 ⇒ 不再发（去重键稳定） ----------------

def test_rescan_same_message_does_not_send_again(proc):
    raw = _raw_tracing(msgid="<tr-copy-4@x>", boxes=_boxes(20))
    routes = _route([("太平洋", "a@x")])

    first_act, first_send = _run(proc, raw, "tr-rescan", routes, uid=7)
    assert first_send.call_count == 1
    fl_after_first = _fl_rows("tr-rescan")
    assert len(fl_after_first) == 1

    # 重扫：同一封、同一份 raw_bytes、同一个代表行 ⇒ claim 撞上已占用的
    # message_id|row_key，直接 duplicate，不发信、不新增行
    second_act, second_send = _run(proc, raw, "tr-rescan", routes, uid=7)

    assert second_act.call_count == 0, "重扫不许再进 execute_action"
    assert second_send.call_count == 0
    fl_after_second = _fl_rows("tr-rescan")
    assert len(fl_after_second) == 1
    assert fl_after_second[0]["forward_id"] == fl_after_first[0]["forward_id"]


# ---------------- 5. 整封副本：原文/主题/附件未裁剪 ----------------

def test_forward_is_whole_mail_copy(proc):
    raw = _raw_tracing(msgid="<tr-copy-5@x>", boxes=_boxes(20))
    from unittest.mock import patch

    crops = [
        "mailbots_next.core.act.split_waybill_by_company",
        "mailbots_next.core.act.split_dsk_by_box",
        "mailbots_next.core.act.rewrite_xls_filtered",
        "mailbots_next.core.act._forward_split",
    ]
    patchers = [patch(t) for t in crops]
    mocks = [p.start() for p in patchers]
    try:
        mock_act, mock_send = _run(proc, raw, "tr-whole-copy",
                                   _route([("太平洋", "a@x")]))
    finally:
        for p in patchers:
            p.stop()

    # execute_action 拿到的就是原邮件字节，没有「挑行后重拼」
    assert mock_act.call_args[0][3] == raw

    sent = mock_send.call_args[0][0]
    assert sent["Subject"] == SUBJECT, "主题必须原样（旧系统口径）"

    orig = email.message_from_bytes(raw, policy=email.policy.default)
    orig_xls = [p.get_payload(decode=True) for p in orig.walk()
                if p.get_filename() and p.get_filename().lower().endswith(".xls")]
    sent_xls = [p.get_payload(decode=True) for p in sent.walk()
                if p.get_filename() and p.get_filename().lower().endswith(".xls")]
    assert len(orig_xls) == 1 and sent_xls == orig_xls, "附件必须整份照搬，不按箱裁剪"

    for name, m in zip(crops, mocks):
        assert not m.called, "运踪不许走任何按箱/按公司拆分的函数：%s" % name
    assert get_pending_errors(100) == []
