# -*- coding: utf-8 -*-
"""任务 24｜下游回复草单 → 人工待处理（T1–T11）。

判据唯一定义处：`mailbots_next/core/draft_reply.py`（本文件只调用，不重写判据）。

🔴 新符号一律**函数内延迟 import**：基线代码没有 `core/draft_reply.py`，dedup 也没有
`manual_queue` 四个函数 ⇒ 顶层 import 会让基线直接 collection error，「先在基线上跑红」
就没有意义了（02_TASK.md §五）。

🔴 夹具约定：所有 From 一律**真实完整头**（`姓名 <邮箱>` / `"邮箱" <邮箱>`），与
`tests/test_reply_type.py` 抬头一致。纯邮箱会让 `normalize_sender_domain` 的边界
（尾随 `>`、大小写）在测试里失效。
"""
import sys
from email.message import EmailMessage
from unittest.mock import Mock, patch

import pytest

import mailbots_next.config as cfg_mod
import mailbots_next.core.dedup as dedup_mod
import mailbots_next.serve as serve_mod

# ---------------------------------------------------------------------------
# 工单三封真实邮件（02_TASK.md §1.1 原文，主题一字不改）
# ---------------------------------------------------------------------------
FROM_ID5_ZHOU = "周继阳 <zhoujiyang@cqgytcp.com>"
FROM_ID3_PACIFIC = "张雨 <pacific-opt5@p-shipping.com>"
FROM_ID4_BINGO = "leeht <bingo.leeht@qq.com>"

SUBJ_ID5 = "Fw: 回复: 运单草单_YXO-2026-1047_CQWLJT261123001-BLLST_TEFU4005959"
SUBJ_ID3 = "Re: 运单草单_YXO-2026-929_CQWLJT261116003-VXN_SMGU8855482"
SUBJ_ID4 = "回复：转发：运单草单_YXO-2026-1045_CQWLJT261116002-XLYJN_GESU6019245"

# 元组：（主题，From，客编，箱号）
REAL_MAILS = (
    (SUBJ_ID5, FROM_ID5_ZHOU, "CQWLJT261123001-BLLST", "TEFU4005959"),
    (SUBJ_ID3, FROM_ID3_PACIFIC, "CQWLJT261116003-VXN", "SMGU8855482"),
    (SUBJ_ID4, FROM_ID4_BINGO, "CQWLJT261116002-XLYJN", "GESU6019245"),
)

# 上游 yxo 域（洋澄清：yxo 是上游，带前缀也走自动转发）
FROM_YXO_CHENLONG = '"chenlong@yxologistics.com" <chenlong@yxologistics.com>'
FROM_YXO_LUOHUA = "罗华 <luohua@yxologistics.com>"
# 域边界：notyxologistics.com 含子串 yxologistics.com，但**不是** yxo 域
FROM_FAKE_YXO = "冒充上游 <ops@notyxologistics.com>"


def _mail(subject, from_hdr, attachments=(), body="正文（任务 24 用例夹具）"):
    """构造一封真实邮件：From 用完整头（EmailMessage + policy.default），照 test_reply_type。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_hdr
    msg["To"] = "maoxiaoyang@cqtransit.com"
    msg.set_content(body)
    for fn in attachments:
        msg.add_attachment(b"%PDF-1.4 stub", maintype="application",
                           subtype="octet-stream", filename=fn)
    return msg


@pytest.fixture
def iso_dedup(monkeypatch, tmp_path):
    """隔离 dedup 库（manual_queue 建在 tmp），并建表。"""
    target = tmp_path / "dedup.db"
    monkeypatch.setattr(dedup_mod, "DEDUP_DB_PATH", str(target))
    monkeypatch.setattr(cfg_mod, "DEDUP_DB_PATH", str(target), raising=False)
    dedup_mod.init_db()
    yield target


def _processor():
    """真构造一个 MailProcessor（不走 __init__ 的副作用裁剪），notifier 换成 Mock。"""
    proc = serve_mod.MailProcessor(serve_mod.snapshot())
    proc.notifier = Mock()
    return proc


# ===========================================================================
# T1｜工单三封真实主题 + 非 yxo From
# ===========================================================================
def test_t1_three_real_downstream_replies_detected():
    from mailbots_next.core.draft_reply import (
        is_downstream_draft_reply,
        parse_draft_reply_ids,
    )

    for subject, from_hdr, code, box in REAL_MAILS:
        assert is_downstream_draft_reply(from_hdr, subject) is True, subject
        assert parse_draft_reply_ids(subject) == (code, box), subject


# ===========================================================================
# T2｜多重前缀必须循环剥（只剥一次的伪实现会漏）
# ===========================================================================
def test_t2_multi_prefix_stripped_in_loop():
    from mailbots_next.core.draft_reply import (
        is_downstream_draft_reply,
        strip_reply_prefixes,
    )

    subjects = (
        # 工单里最深的一封：全角冒号 + 中文前缀
        SUBJ_ID4,
        # 半角冒号混用
        "Fw: 回复: 运单草单_YXO-2026-1047_CQWLJT261123001-BLLST_TEFU4005959",
        # 三层大小写混写
        "RE: FW: Fwd: 答复: 运单草单_YXO-2026-929_CQWLJT261116003-VXN_SMGU8855482",
    )
    for subject in subjects:
        stripped = strip_reply_prefixes(subject)
        assert stripped.startswith("运单草单"), (subject, stripped)
        assert is_downstream_draft_reply(FROM_ID5_ZHOU, subject) is True, subject


# ===========================================================================
# T3｜无前缀的完整骨架 + 非 yxo From ⇒ 仍判下游（本函数与前缀无关）
# ===========================================================================
def test_t3_bare_skeleton_from_non_yxo_is_downstream():
    from mailbots_next.core.draft_reply import is_downstream_draft_reply

    bare = "运单草单_YXO-2026-1047_CQWLJT261123001-BLLST_TEFU4005959"
    assert is_downstream_draft_reply(FROM_ID5_ZHOU, bare) is True


# ===========================================================================
# T4｜yxo 域带前缀 ⇒ False（上游，走自动转发）
# ===========================================================================
def test_t4_yxo_domain_is_upstream_not_downstream():
    from mailbots_next.core.draft_reply import is_downstream_draft_reply

    for subject in (SUBJ_ID3, SUBJ_ID5):
        for from_hdr in (FROM_YXO_CHENLONG, FROM_YXO_LUOHUA):
            assert is_downstream_draft_reply(from_hdr, subject) is False, (subject, from_hdr)


# ===========================================================================
# T5｜域边界：notyxologistics.com 必须判 True（抓「误用 in 子串」的假绿）
# ===========================================================================
def test_t5_domain_lookalike_is_downstream():
    """期望值写死 True：精确相等 ⇒ 非 yxo ⇒ 下游进人工。

    若实现误用 `YXO_DOMAIN in domain`（子串），本条会返回 False —— 这条用例存在的
    唯一目的就是让那个错误实现在这里变红。
    """
    from mailbots_next.core.draft_reply import is_downstream_draft_reply
    from mailbots_next.core.sender_addr import normalize_sender_domain

    # 前提：域本身确实是 notyxologistics.com（不是 yxo），且字面上含 yxo 子串
    assert normalize_sender_domain(FROM_FAKE_YXO) == "notyxologistics.com"
    assert "yxologistics.com" in "notyxologistics.com"

    assert is_downstream_draft_reply(FROM_FAKE_YXO, SUBJ_ID3) is True


# ===========================================================================
# T6｜骨架三要素缺一不可
# ===========================================================================
def test_t6_skeleton_requires_all_three_parts():
    from mailbots_next.core.draft_reply import is_downstream_draft_reply

    # 只有客编：缺「运单草单」与 YXO-订单号
    assert is_downstream_draft_reply(FROM_ID3_PACIFIC, "回复：CQWLJT261116003-VXN") is False
    # 缺 YXO-订单号
    assert is_downstream_draft_reply(
        FROM_ID3_PACIFIC, "回复：运单草单_CQWLJT261116003-VXN") is False
    # 缺完整客编（只有裸 CQWLJT）
    assert is_downstream_draft_reply(
        FROM_ID3_PACIFIC, "回复：运单草单_YXO-2026-929_CQWLJT") is False
    # 缺「运单草单」
    assert is_downstream_draft_reply(
        FROM_ID3_PACIFIC, "回复：YXO-2026-929_CQWLJT261116003-VXN") is False
    # 取不到域（From 头残缺）⇒ False，不抛异常
    assert is_downstream_draft_reply("只有显示名", SUBJ_ID3) is False


# ===========================================================================
# T7｜端到端：命中下游回复 ⇒ 落人工表，不 add_error / 不告警 / 不标已读
# ===========================================================================
def test_t7_process_email_routes_to_manual_queue(iso_dedup, monkeypatch):
    from mailbots_next.core.dedup import list_manual

    monkeypatch.setenv("MAILBOT_MODE", "live")
    proc = _processor()
    raw = _mail(SUBJ_ID5, FROM_ID5_ZHOU).as_bytes()
    date_hdr = "Wed, 20 Nov 2026 10:41:00 +0800"

    # 前提自检：这封真的识别不出来（否则走不到新分支，下面全是假绿）
    assert proc._identify_type(FROM_ID5_ZHOU, SUBJ_ID5, raw) is None

    with patch.object(serve_mod, "add_manual", wraps=dedup_mod.add_manual) as m_add, \
            patch.object(serve_mod, "dedup_has_manual", wraps=dedup_mod.has_manual) as m_has, \
            patch.object(serve_mod, "add_error") as m_err, \
            patch.object(serve_mod, "try_claim") as m_claim, \
            patch.object(serve_mod, "record_issue") as m_issue, \
            patch.object(serve_mod, "mark_seen") as m_seen, \
            patch.object(serve_mod, "enqueue_mark_seen") as m_enq, \
            patch.object(serve_mod, "increment_counter") as m_cnt:
        ok = proc.process_email("maoxiaoyang@cqtransit.com", "草单", "msg-t7", 11,
                                SUBJ_ID5, FROM_ID5_ZHOU, date_hdr, raw)

    assert ok is True
    # 落人工表：一次插入、一次查重
    assert m_add.call_count == 1
    assert m_has.call_count == 1
    # 告警链源头截断（判据 9）
    m_err.assert_not_called()
    m_claim.assert_not_called()
    m_issue.assert_not_called()
    # 不标已读（判据 7）
    m_seen.assert_not_called()
    m_enq.assert_not_called()
    # 计数只记一次
    m_cnt.assert_called_once_with("draft_reply_manual")
    assert proc._get_last_outcome() == {"manual": 1}
    # 不发任何程序错误告警
    proc.notifier.send_program_error.assert_not_called()

    items = list_manual(10, status="pending")
    assert len(items) == 1
    it = items[0]
    assert it["message_id"] == "msg-t7"
    assert it["account"] == "maoxiaoyang@cqtransit.com"
    assert it["folder"] == "草单"
    assert it["uid"] == 11
    assert it["subject"] == SUBJ_ID5
    assert it["sender"] == FROM_ID5_ZHOU
    assert it["date_hdr"] == date_hdr
    assert it["code"] == "CQWLJT261123001-BLLST"
    assert it["box"] == "TEFU4005959"
    assert it["status"] == "pending"
    assert it["arrived_at"]


# ===========================================================================
# T8｜同一 message_id 落两次：仍 1 行、arrived_at 不刷新、计数不重复
# ===========================================================================
def test_t8_same_message_id_is_idempotent(iso_dedup, monkeypatch):
    from mailbots_next.core.dedup import add_manual, get_connection, list_manual

    monkeypatch.setenv("MAILBOT_MODE", "live")
    proc = _processor()
    raw = _mail(SUBJ_ID4, FROM_ID4_BINGO).as_bytes()

    with patch.object(serve_mod, "add_manual", wraps=dedup_mod.add_manual) as m_add, \
            patch.object(serve_mod, "dedup_has_manual", wraps=dedup_mod.has_manual) as m_has, \
            patch.object(serve_mod, "increment_counter") as m_cnt:
        for _ in range(2):
            assert proc.process_email("maoxiaoyang@cqtransit.com", "草单", "msg-t8", 12,
                                      SUBJ_ID4, FROM_ID4_BINGO, "", raw) is True

    # 第一次插入、第二次被 has_manual 挡住 ⇒ add_manual 只进 1 次 DB 写；
    # 计数已移进 `if not has_manual:` 块内 ⇒ 第二封不重复计数（判据 9 第二条）。
    assert m_add.call_count == 1
    assert m_has.call_count == 2
    assert m_cnt.call_count == 1

    rows = list_manual(10, status="pending")
    assert len(rows) == 1
    assert rows[0]["message_id"] == "msg-t8"
    assert rows[0]["code"] == "CQWLJT261116002-XLYJN"
    assert rows[0]["box"] == "GESU6019245"

    # 判据 8：重落不刷新 arrived_at、不改内容（直接打 dedup 层，用不同字段再落一次）
    arrived_before = rows[0]["arrived_at"]
    again_id = add_manual("msg-t8", subject="改过的主题", code="别的客编", box="")
    assert again_id == rows[0]["id"]
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT arrived_at, subject, code FROM manual_queue WHERE message_id=?",
            ("msg-t8",)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM manual_queue").fetchone()[0]
    finally:
        conn.close()
    assert total == 1
    assert row["arrived_at"] == arrived_before
    assert row["subject"] == SUBJ_ID4
    assert row["code"] == "CQWLJT261116002-XLYJN"


# ===========================================================================
# T9｜list_manual / resolve_manual
# ===========================================================================
def test_t9_list_and_resolve(iso_dedup):
    from mailbots_next.core.dedup import add_manual, list_manual, resolve_manual

    add_manual("m-a", subject="主题 A", sender=FROM_ID5_ZHOU, code="CQWLJT261116003-VXN")
    add_manual("m-b", subject="主题 B")

    pending = list_manual(10, status="pending")
    assert [i["message_id"] for i in pending] == ["m-a", "m-b"]
    assert list_manual(10, status="resolved") == []
    assert len(list_manual(10, status="")) == 2   # status='' ⇒ 不过滤

    assert resolve_manual(pending[0]["id"]) is True
    assert [i["message_id"] for i in list_manual(10, status="pending")] == ["m-b"]
    resolved = list_manual(10, status="resolved")
    assert [i["message_id"] for i in resolved] == ["m-a"]
    assert resolved[0]["resolved_at"]

    assert resolve_manual(999999) is False   # 不存在的 id


# ===========================================================================
# T10｜CLI：--manual list / resolve，缺 id 退出码 2
# ===========================================================================
def test_t10_cli_list_and_resolve(iso_dedup, monkeypatch, capsys):
    from mailbots_next.core.dedup import add_manual, list_manual

    mid = add_manual("m-cli", account="maoxiaoyang@cqtransit.com", folder="草单", uid=7,
                     subject=SUBJ_ID5, sender=FROM_ID5_ZHOU,
                     code="CQWLJT261123001-BLLST", box="TEFU4005959")

    monkeypatch.setattr(sys, "argv", ["serve", "--manual", "list"])
    assert serve_mod.main() == 0
    out = capsys.readouterr().out
    assert "待人工处理：1 条" in out
    assert "#%d" % mid in out
    assert "from=%s" % FROM_ID5_ZHOU in out
    assert "code=CQWLJT261123001-BLLST box=TEFU4005959" in out

    monkeypatch.setattr(sys, "argv",
                        ["serve", "--manual", "resolve", "--manual-id", str(mid)])
    assert serve_mod.main() == 0
    assert "已标记已处理" in capsys.readouterr().out
    assert list_manual(10, status="pending") == []

    # 缺 --manual-id ⇒ 退出码 2（main() 的返回值，__main__ 里原样给 sys.exit）
    monkeypatch.setattr(sys, "argv", ["serve", "--manual", "resolve"])
    assert serve_mod.main() == 2
    assert "用法" in capsys.readouterr().out

    # 不存在的 id ⇒ 仍 0，但明确说没找到
    monkeypatch.setattr(sys, "argv",
                        ["serve", "--manual", "resolve", "--manual-id", "999999"])
    assert serve_mod.main() == 0
    assert "未找到该 id" in capsys.readouterr().out


# ===========================================================================
# T11｜回归：正常上游草单仍识别为 draft，且不被新分流吃掉
# ===========================================================================
def test_t11_upstream_draft_unchanged():
    from mailbots_next.core.draft_reply import is_downstream_draft_reply

    proc = object.__new__(serve_mod.MailProcessor)   # 只用入参，绕过 __init__
    subject = "运单草单_YXO-2026-1047_CQWLJT261123001-BLLST_TEFU4005959"
    attachments = ("TEFU4005959-261123-101010已加密.pdf",)

    for from_hdr in (FROM_YXO_CHENLONG, FROM_YXO_LUOHUA):
        raw = _mail(subject, from_hdr, attachments).as_bytes()
        assert proc._identify_type(from_hdr, subject, raw) == "draft"
        assert is_downstream_draft_reply(from_hdr, subject) is False

    # 带前缀的 yxo 域草单同样命中 DRAFT（洋澄清：yxo 是上游，自动转发正常）
    subject_re = "Re: " + subject
    raw = _mail(subject_re, FROM_YXO_CHENLONG, attachments).as_bytes()
    assert proc._identify_type(FROM_YXO_CHENLONG, subject_re, raw) == "draft"
    assert is_downstream_draft_reply(FROM_YXO_CHENLONG, subject_re) is False
