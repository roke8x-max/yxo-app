# -*- coding: utf-8 -*-
"""sending.py 三场景：全送达 / 部分拒收 / 连接失败。"""
from email.message import EmailMessage
import pytest
from core import sending


@pytest.fixture(autouse=True)
def _fake_endpoint(monkeypatch):
    """smtp_endpoint 注入假端点：单测不触碰真实 detect_root（含 UNC 探测）。"""
    monkeypatch.setattr(sending.paths, "smtp_endpoint",
                        lambda: ("smtp.test", 465))


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
    assert calls["endpoint"] == ("smtp.test", 465)
    assert calls["tos"] == ["a@b.com", "c@d.com"]


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


def test_no_recipients(monkeypatch):
    # 空收件人早退：绝不发起 SMTP 连接（连接即视为违约）
    def boom(*a, **k):
        raise AssertionError("no_recipients 时不应建立连接")
    monkeypatch.setattr(sending.smtplib, "SMTP_SSL", boom)
    r = sending.send_smtp(_msg(), "s@x", "p", [], [])
    assert not r.ok and r.error == "no_recipients"


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
