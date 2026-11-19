# -*- coding: utf-8 -*-
"""任务 22-C：运踪转发成功不推企微（T14–T18，判据 17–19）。

只静默「成功」这一类；告警 / 待办 / 失败三条路必须照常通知或入库。
"""
from unittest.mock import Mock, patch

import pytest

from mailbots_next.config import EmailType, OPS_OWNER_EMAIL, snapshot
from mailbots_next.core import Decision
from mailbots_next.core.extract import ExtractedRow
from mailbots_next.core.routing import RoutingResult
import mailbots_next.serve as serve_mod


def _processor():
    p = serve_mod.MailProcessor.__new__(serve_mod.MailProcessor)
    p.notifier = Mock()
    p.records = []
    p.accounts = {"a@x.com": "pw"}
    return p


def _run(processor, email_type, row, routing, *, action="forward", detail="forwarded",
         success=True, decide_action=None):
    """走 _process_routing_result，patch decide/execute_action/归档；返回 processor。"""
    with patch.object(serve_mod, "execute_action", return_value=(success, detail)), \
            patch("mailbots_next.core.archive.copy_after_forward", return_value=True), \
            patch.object(serve_mod.MailProcessor, "_forward_log_hit", return_value=False):
        decide_ctx = patch.object(
            serve_mod, "decide",
            return_value=Decision("T1", decide_action or action, "reason")) \
            if decide_action else patch.object(serve_mod, "decide",
                                               return_value=Decision("T1", action, "reason"))
        with decide_ctx:
            processor._process_routing_result(
                routing, row, email_type, "t-msg-1", "0:BOX", b"raw",
                "a@x.com", "INBOX", 7, "s", "subj", "",
            )
    return processor


def _tracing():
    return (EmailType.TRACING,
            ExtractedRow(row_idx=0, email_type=EmailType.TRACING, train_no="1032"),
            RoutingResult("港九港铁", "maoxiaoyang@cqtransit.com",
                          ["chenkai@atrailimt.com"], [], "train_hit", True))


def test_tracing_forwarded_no_notification_and_counter():
    """T14：运踪 forwarded ⇒ send_forwarded 未调用 + notify_skip_tracing_forwarded +1。"""
    et, row, routing = _tracing()
    p = _processor()
    with patch.object(serve_mod, "increment_counter") as cnt:
        _run(p, et, row, routing)
    p.notifier.send_forwarded.assert_not_called()
    cnt.assert_any_call("notify_skip_tracing_forwarded")


def test_non_tracing_forwarded_still_notifies_owner_and_ops():
    """T15：DSK/草单/运单号/ATB 同条件仍通知，收件人含负责人 + OPS。"""
    for et, row, routing in (
        (EmailType.DSK,
         ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU1000001"),
         RoutingResult("太平洋", "maoxiaoyang@cqtransit.com", ["a@x.com"], [], "full_match", True)),
        (EmailType.DRAFT,
         ExtractedRow(row_idx=0, email_type=EmailType.DRAFT, customer_code="CQWLJT261109007"),
         RoutingResult("东盟", "yangyawen@cqtransit.com", ["a@x.com"], [], "full_match", True)),
    ):
        p = _processor()
        _run(p, et, row, routing)
        p.notifier.send_forwarded.assert_called_once()
        args = p.notifier.send_forwarded.call_args.args
        assert args[0] == routing.responsible_person
        assert args[1] == OPS_OWNER_EMAIL


def test_tracing_alarm_still_notifies():
    """T16：运踪 alarm ⇒ send_alarm 仍调用（失败类不许静默）。"""
    et, row, routing = _tracing()
    p = _processor()
    _run(p, et, row, routing, decide_action="alarm", detail="alarm_logged")
    p.notifier.send_alarm.assert_called_once()
    p.notifier.send_forwarded.assert_not_called()


def test_tracing_pending_still_notifies():
    """T17：运踪 pending ⇒ send_pending 仍调用。"""
    et, row, routing = _tracing()
    p = _processor()
    _run(p, et, row, routing, decide_action="pending", detail="pending_queued")
    p.notifier.send_pending.assert_called_once()
    p.notifier.send_forwarded.assert_not_called()


def test_tracing_failed_goes_to_error_queue():
    """T18：运踪 execute_action 失败 ⇒ add_error 有记录，send_forwarded 未调用。"""
    et, row, routing = _tracing()
    p = _processor()
    with patch.object(serve_mod, "add_error") as mock_add:
        _run(p, et, row, routing, success=False, detail="SMTP failed")
    mock_add.assert_called_once()
    p.notifier.send_forwarded.assert_not_called()
