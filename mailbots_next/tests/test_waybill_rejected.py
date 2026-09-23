"""WAY_B 单证审核驳回（2026-09-22）：全部用假数据，不打真企微、不发真信。"""
import pytest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import Mock, patch

from mailbots_next.config import (
    BOT_CONFIG_DB_PATH,
    DEDUP_DB_PATH,
    DAILY_COUNTERS_PATH,
    EmailType,
)
from mailbots_next.core.dedup import init_db
from mailbots_next.core.store import (
    init_bot_config_db,
    init_forward_log,
    seed_owner_mapping,
)
from mailbots_next.core.extract import extract_email, ExtractedRow
from mailbots_next.core.routing import RoutingResult

REJECT_SUBJECT = "单证审核驳回"
REJECT_BODY = "您的编码为:CQWLJT260917002-Kol单证审核被拒."
REJECT_FROM = "docwbfb@yxologistics.com"


@pytest.fixture(autouse=True)
def _wayb_dbs():
    for p in (BOT_CONFIG_DB_PATH, DEDUP_DB_PATH, DAILY_COUNTERS_PATH):
        if p.exists():
            try:
                p.unlink()
            except PermissionError:
                pass
    init_db()
    init_bot_config_db()
    init_forward_log()
    seed_owner_mapping()
    yield


def _reject_raw(subject=REJECT_SUBJECT, body=REJECT_BODY, sender=REJECT_FROM):
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = sender
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg.as_bytes()


def _processor():
    from mailbots_next.serve import MailProcessor
    from mailbots_next.config import snapshot
    proc = MailProcessor(snapshot())
    proc.accounts = {"t@t.com": "pw"}
    proc.notifier = Mock()
    return proc


def test_reject_body_code_extraction():
    """① 正文取码：主题含驳回关键词 + 无附件 ⇒ 单行 waybill_rejected，码从正文取。"""
    rows = extract_email(EmailType.WAYBILL, _reject_raw())
    assert len(rows) == 1
    row = rows[0]
    assert row.waybill_rejected is True
    assert row.email_type == "waybill"
    assert row.customer_code == "CQWLJT260917002"
    assert row.customer_code_num == "260917002"
    assert row.container_no is None


def test_rejected_row_never_forwards_nor_queues():
    """② 不转发：完整 _process_row ⇒ execute_action 未调用，add_error 未调用。"""
    proc = _processor()
    with patch("mailbots_next.serve.execute_action") as mock_exec, \
         patch("mailbots_next.serve.add_error") as mock_err:
        proc.process_email("t@t.com", "运单草单", "wayb-msg-1", 1,
                           REJECT_SUBJECT, REJECT_FROM, "", _reject_raw())
    mock_exec.assert_not_called()
    mock_err.assert_not_called()


def test_rejected_sends_no_notification():
    """③ 零通知：走完整 process_email ⇒ notifier 的任何 send_* 都没被调用。"""
    proc = _processor()
    proc.process_email("t@t.com", "运单草单", "wayb-msg-2", 2,
                       REJECT_SUBJECT, REJECT_FROM, "", _reject_raw())
    assert proc.notifier.mock_calls == []


def test_rejected_silent_even_when_unroutable():
    """④ 路由拿不到负责同事时同样零通知，且不进错误队列。"""
    proc = _processor()
    bad_route = RoutingResult(company="", responsible_person=None,
                              to_list=[], cc_list=[], route_source="no_route", success=False)
    with patch("mailbots_next.serve.route_row", return_value=bad_route), \
         patch("mailbots_next.serve.add_error") as mock_err:
        proc.process_email("t@t.com", "运单草单", "wayb-msg-3", 3,
                           REJECT_SUBJECT, REJECT_FROM, "", _reject_raw())
    assert proc.notifier.mock_calls == []
    mock_err.assert_not_called()


def test_decide_guard_blocks_forward():
    """⑤ 守卫独立生效：带编码 WAY_B 直接调 decide_waybill ⇒ skip（绝非 forward）。"""
    from mailbots_next.core.decide import decide_waybill
    row = ExtractedRow(row_idx=0, email_type="waybill",
                       customer_code="CQWLJT260917002", customer_code_num="260917002",
                       waybill_rejected=True)
    ok_route = RoutingResult(company="太平洋", responsible_person="m@x.com",
                             to_list=["t@x.com"], cc_list=[], route_source="full_match", success=True)
    recs = [{"客户编码": "CQWLJT260917002", "箱号": "", "company": "太平洋",
             "状态": "正常", "is_deleted": 0}]
    d = decide_waybill(row, ok_route, recs)
    assert d.action == "skip"
    assert d.tier == "WAY_B"


def test_rejected_records_info_counter():
    """⑦ "静默"不等于"没痕迹"：计数 action_wayb_rejected_skipped 有增长。"""
    from mailbots_next.core.notify import get_counters
    proc = _processor()
    before = get_counters().get("action_wayb_rejected_skipped", 0)
    proc.process_email("t@t.com", "运单草单", "wayb-msg-4", 4,
                       REJECT_SUBJECT, REJECT_FROM, "", _reject_raw())
    assert get_counters().get("action_wayb_rejected_skipped", 0) == before + 1
    # 终态词必须是 "rejected"（不是 "manual"）："manual" 会置 has_manual ⇒
    # live 下邮件不标已读。outcome 经线程本地槽可观测。
    assert proc._get_last_outcome().get("rejected", 0) == 1


def test_normal_waybill_unchanged():
    """⑥ 回归：普通运单号（主题含编码、无驳回词）行为不变。"""
    msg = MIMEMultipart()
    msg["Subject"] = "运单号 CQWLJT260810001"
    msg["From"] = REJECT_FROM
    msg.attach(MIMEText("normal body", "plain", "utf-8"))
    rows = extract_email(EmailType.WAYBILL, msg.as_bytes())
    assert len(rows) == 1
    assert rows[0].waybill_rejected is False
    assert rows[0].customer_code == "CQWLJT260810001"
    from mailbots_next.core.decide import decide_waybill
    ok_route = RoutingResult(company="太平洋", responsible_person="m@x.com",
                             to_list=["t@x.com"], cc_list=[], route_source="full_match", success=True)
    recs = [{"客户编码": "CQWLJT260810001", "箱号": "", "company": "太平洋",
             "状态": "正常", "is_deleted": 0}]
    d = decide_waybill(rows[0], ok_route, recs)
    assert (d.tier, d.action) == ("T1", "forward")
