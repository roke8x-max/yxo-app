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
