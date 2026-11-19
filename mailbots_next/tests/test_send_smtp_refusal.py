"""任务 22-A2：send_smtp 拒收口径固化（ADDENDUM-B §2.3）。

mock smtplib.SMTP_SSL，不碰真网络。核心口径：
  · 只有「被拒的全都是自检抄送地址」才判成功（信已发给业务收件人）；
  · 只要有一个业务地址被拒 ⇒ success=False，让 dedup 留 unresolved 等重判；
  · 计数/记录失败不许翻转业务判定（内部 try 兜住）。
"""
import smtplib
from unittest.mock import MagicMock

from email.mime.text import MIMEText

from mailbots_next.core import act
from mailbots_next.core.notify import flush_counters, get_counters

SELF = "3841559246@qq.com"          # = config.SELFCHECK_CC_ADDR 默认值
BUSINESS_TO = "client@example.com"
BUSINESS_CC = "nadya@cq-wood.com"


def _server(refused):
    """造一个 context-manager 形态的 SMTP server，sendmail 返回 refused。"""
    server = MagicMock()
    server.sendmail.return_value = refused
    ctx = MagicMock()
    ctx.__enter__.return_value = server
    ctx.__exit__.return_value = False
    return ctx


def _msg():
    msg = MIMEText("body")
    msg["Subject"] = "subj"
    return msg


def _call(monkeypatch, refused, to_list=None, cc_list=None):
    """在 mock 下跑一次 send_smtp；monkeypatch 的 SELFCHECK 由调用方设定。"""
    ctx = _server(refused)
    monkeypatch.setattr(act.smtplib, "SMTP_SSL", lambda *a, **kw: ctx)
    return act.send_smtp(
        _msg(), "sender@example.com", "password",
        to_list if to_list is not None else [BUSINESS_TO],
        cc_list if cc_list is not None else [],
    )


def test_a2_1_all_accepted(monkeypatch):
    """A2-1：sendmail 返回 {} ⇒ success True、refused {}。"""
    monkeypatch.setattr("mailbots_next.config.SELFCHECK_CC_ADDR", SELF)
    result = _call(monkeypatch, {})
    assert result == {"success": True, "refused": {}}


def test_a2_2_only_selfcheck_refused(monkeypatch):
    """A2-2：只拒自检抄送地址 ⇒ success True 且 selfcheck_cc_refused 计数 +1。"""
    monkeypatch.setattr("mailbots_next.config.SELFCHECK_CC_ADDR", SELF)
    flush_counters()
    result = _call(monkeypatch, {SELF: "550 no such user"})
    assert result["success"] is True
    assert get_counters().get("selfcheck_cc_refused") == 1


def test_a2_3_business_to_refused(monkeypatch):
    """A2-3：拒 to_list 里一个地址 ⇒ success False 且 error 含该地址。"""
    monkeypatch.setattr("mailbots_next.config.SELFCHECK_CC_ADDR", SELF)
    result = _call(monkeypatch, {BUSINESS_TO: "550 no such user"})
    assert result["success"] is False
    assert BUSINESS_TO in result["error"]


def test_a2_4_selfcheck_plus_business_cc_refused(monkeypatch):
    """A2-4：同时拒自检地址 + 业务 cc ⇒ success False（集合不等于 {自检}）。"""
    monkeypatch.setattr("mailbots_next.config.SELFCHECK_CC_ADDR", SELF)
    result = _call(
        monkeypatch,
        {SELF: "550 no", BUSINESS_CC: "550 no"},
        to_list=[BUSINESS_TO],
        cc_list=[SELF, BUSINESS_CC],
    )
    assert result["success"] is False


def test_a2_5_selfcheck_blank_then_refused(monkeypatch):
    """A2-5：反证——SELFCHECK_CC_ADDR 置空后只拒自检地址 ⇒ success False。"""
    monkeypatch.setattr("mailbots_next.config.SELFCHECK_CC_ADDR", "")
    result = _call(monkeypatch, {SELF: "550 no such user"})
    assert result["success"] is False


def test_a2_6_counter_failure_does_not_flip(monkeypatch):
    """A2-6 防回归：increment_counter 抛异常，仍是 success True。"""
    monkeypatch.setattr("mailbots_next.config.SELFCHECK_CC_ADDR", SELF)

    def boom(*a, **kw):
        raise RuntimeError("counter backend down")

    monkeypatch.setattr("mailbots_next.core.notify.increment_counter", boom)
    result = _call(monkeypatch, {SELF: "550 no such user"})
    assert result["success"] is True
