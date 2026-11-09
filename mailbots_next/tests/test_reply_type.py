# -*- coding: utf-8 -*-
"""任务 13｜回复类邮件：识别入口 + 发件人域规范化（用例 #1–#32）。

编号口径：用例 #1–#32，编号与 02_TASK.md §六 一致（A 组共用函数 / B 组入口规则 /
C 组费用邮件正反 / D 组 C2 边界与本周 7 条回归 / E 组顺序·缓存·夹具）。

🔴 夹具约定（02_TASK.md §六 抬头）：**所有 From 一律真实完整头**
（`"邮箱" <邮箱>` 或 `姓名 <邮箱>`），不许只写纯邮箱。
理由：parse_email() 的 sender 是 `msg.get("From")` 的 str()，生产里就是完整
From 头；旧实现 `sender.split("@")[-1]` 拿到的是 `yxologistics.com>`（带尖括号）
⇒ 内部回复被误判成 C2 留人工。**用纯邮箱夹具会全绿、上线才炸** —— 本文件的
每条用例都按完整头构造，就是为了让这个缺陷在测试里必然暴露。
"""
import email
import email.policy
import glob
import inspect
import io
import json
import os
import re
import sqlite3
from email.message import EmailMessage
from unittest.mock import Mock, patch

import pytest

import mailbots_next.config as cfg_mod
import mailbots_next.core.dedup as dedup_mod
import mailbots_next.core.store as store_mod
import mailbots_next.serve as serve_mod
from mailbots_next.config import (
    TYPE_ROUTES,
    EmailType,
    NON_AUTO_DRAFT_CATEGORIES,
    get_enabled_types,
    invalidate_cache,
    snapshot,
)
from mailbots_next.core.decide import decide, decide_draft
from mailbots_next.core.extract import ExtractedRow, extract_email
from mailbots_next.core.routing import RoutingResult, route_row
from mailbots_next.core.sender_addr import normalize_sender_domain
from mailbots_next.serve import MailProcessor

# ---------------------------------------------------------------------------
# 夹具：真实完整 From 头（两个形态都覆盖："邮箱" <邮箱> / 姓名 <邮箱>）
# ---------------------------------------------------------------------------
FROM_YXO_CHENLONG = '"chenlong@yxologistics.com" <chenlong@yxologistics.com>'
FROM_YXO_LIUJIANGLAN = '"liujianglan@yxologistics.com" <liujianglan@yxologistics.com>'
FROM_YXO_LUOHUA = '"luohua@yxologistics.com" <luohua@yxologistics.com>'
FROM_YXO_DOCWBFB = "Docwbfb <docwbfb@yxologistics.com>"
FROM_YXO_ATB = "ATB <atb@yxologistics.com>"
FROM_YXO_TRACING = "Tracing System <tracing-system@yxologistics.com>"
FROM_YXO_SHIDONG = "石东 <shidong@yxologistics.com>"
FROM_RTSB_KASA = "Kasa <kasa@rtsb.de>"
FROM_CQTRANSIT_YANG = "杨雅雯 <yangyawen@cqtransit.com>"
FROM_CAIL56_EVELYN = "胡益 <evelyn.hu@cail56.com>"
FROM_CAIL56_EVELYN_QUOTED = '"evelyn.hu@cail56.com" <evelyn.hu@cail56.com>'
FROM_CQGYTCP_ZHOU = "周继阳 <zhoujiyang@cqgytcp.com>"
FROM_YUXINOU_OPS = "Operations <operations@yuxinoulogistics.com>"
FROM_GMAIL_CUSTOMER = "Customer <customer@gmail.com>"


def _mail(subject, from_hdr, attachments=(), body="正文（任务 13 用例夹具）"):
    """构造一封真实邮件：From 用完整头，附件名逐个挂上（EmailMessage + policy.default）。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_hdr
    msg["To"] = "maoxiaoyang@cqtransit.com"
    msg.set_content(body)
    for fn in attachments:
        msg.add_attachment(b"%PDF-1.4 stub", maintype="application",
                           subtype="octet-stream", filename=fn)
    return msg


def _identify(subject, from_hdr, attachments=()):
    """走真实 _identify_type（只用入参、不读实例状态 ⇒ 可绕过 __init__）。"""
    raw = _mail(subject, from_hdr, attachments).as_bytes()
    proc = object.__new__(MailProcessor)
    return proc._identify_type(from_hdr, subject, raw)


def _pipeline(subject, from_hdr, attachments=()):
    """识别 → 提取 → 路由 → 决策，全用生产函数（C 组 / D 组主件回归用）。"""
    raw = _mail(subject, from_hdr, attachments).as_bytes()
    proc = object.__new__(MailProcessor)
    email_type = proc._identify_type(from_hdr, subject, raw)
    assert email_type == "draft", "用例前提：该封应被识别为 draft"
    rows = extract_email(EmailType.DRAFT, raw)
    assert len(rows) == 1
    row = rows[0]
    records = store_mod.load_records()
    routing = route_row(EmailType.DRAFT, row, records)
    decision = decide(EmailType.DRAFT, row, routing, records)
    return row, routing, decision


# 费用邮件两条正例命中的客编，以及退舱客编（#28）
RECORDS = [
    ("CQWLJT260810001-SPB", "CICU1000001", "WB1", "测试站", "太平洋", "", "正常", 0, "", ""),
    ("CQWLJT261022003-Kol", "FWRU0194073", "WB10768", "杜伊斯堡", "保时达", "", "正常", 0, "", ""),
    ("CQWLJT260924005-BLLST", "TEFU4001589", "WB10768", "杜伊斯堡", "保时达", "", "正常", 0, "", ""),
    # 退舱：load_records() 会过滤掉，用于 #28「不退化成静默取消」
    ("CQWLJT260810009-SPB", "CICU1000009", "WB1", "测试站", "太平洋", "", "退舱", 0, "", ""),
]

RECIPIENTS = {
    "太平洋": ["tp@test.com"],
    "保时达": ["pyx@cqbestar.cn", "xyli@cqbestar.cn"],
}


@pytest.fixture(autouse=True)
def _reset_route_cache():
    """#31：用例可能临时改 TYPE_ROUTES / enabled_types，收尾必须清 _RUNTIME_CACHE。

    provider 的缓存 TTL 60s，不清会按文件顺序飘（同一份代码跑两次结果不同）。
    """
    yield
    invalidate_cache()


@pytest.fixture
def biz_dbs(monkeypatch, tmp_path):
    """隔离业务库：records + 收件人 + 去重库，全部落在 tmp_path。"""
    for name, fname in (
        ("YXO_DB_PATH", "yxo.db"),
        ("BOT_CONFIG_DB_PATH", "bot_config.db"),
        ("DEDUP_DB_PATH", "dedup.db"),
    ):
        target = tmp_path / fname
        for mod in (cfg_mod, store_mod, dedup_mod):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, target)

    conn = sqlite3.connect(tmp_path / "yxo.db")
    try:
        conn.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY, 客户编码 TEXT, 箱号 TEXT,"
            " 班列号 TEXT, 目的站 TEXT, 开票子公司名称 TEXT, 本地货源公司 TEXT,"
            " 状态 TEXT, is_deleted INTEGER, dsk TEXT, ATB TEXT, 班列类型 TEXT)"
        )
        conn.executemany(
            "INSERT INTO records (客户编码, 箱号, 班列号, 目的站, 开票子公司名称,"
            " 本地货源公司, 状态, is_deleted, dsk, ATB) VALUES (?,?,?,?,?,?,?,?,?,?)",
            RECORDS,
        )
        conn.commit()
    finally:
        conn.close()

    store_mod.init_bot_config_db()
    store_mod.init_forward_log()
    conn = store_mod.get_bot_config_connection()
    try:
        for company, to_addrs in RECIPIENTS.items():
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra)"
                " VALUES ('all', 'company', ?, ?, '[]', '{}')",
                (company, json.dumps(to_addrs)),
            )
        conn.commit()
    finally:
        conn.close()

    dedup_mod.init_db()
    yield


# ===========================================================================
# A 组｜共用函数 normalize_sender_domain（#1–#6）
# ===========================================================================
class TestNormalizeSenderDomain:
    """入口规则与 DraftExtractor 共用的唯一实现（洋定的红线：只写一份）。"""

    def test_01_plain_address(self):
        """#1 纯地址 → 域"""
        assert normalize_sender_domain("liujianglan@yxologistics.com") == "yxologistics.com"

    def test_02_quoted_address_as_display_name(self):
        """#2 真实 From 头（显示名就是邮箱本身）→ 域，不带 `>`"""
        assert normalize_sender_domain(
            '"liujianglan@yxologistics.com" <liujianglan@yxologistics.com>'
        ) == "yxologistics.com"

    def test_03_chinese_display_name(self):
        """#3 中文显示名 → 域"""
        assert normalize_sender_domain("黄光楠 <hgn@cail56.com>") == "cail56.com"

    def test_04_uppercase_domain(self):
        """#4 大写 → 小写"""
        assert normalize_sender_domain("LiuJiangLan@YXOLOGISTICS.COM") == "yxologistics.com"

    def test_05_header_object(self):
        """#5 _UniqueAddressHeader（msg.get("From") 的返回）→ 不抛异常、域正确"""
        raw = (
            'From: "liujianglan@yxologistics.com" <liujianglan@yxologistics.com>\r\n'
            "Subject: t\r\n\r\nbody"
        ).encode("ascii")
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        hdr = msg["From"]
        # 先确认真的是头对象（不是 str）：这才是生产里传进来的形态
        assert type(hdr).__name__ == "_UniqueAddressHeader"
        assert normalize_sender_domain(hdr) == "yxologistics.com"

    def test_06_empty_and_garbage_return_empty(self):
        """#6 ""/None/无 @/只有显示名 → ""，且不抛异常"""
        for bad in ("", None, "无at符", "只有显示名", "a@", "  "):
            assert normalize_sender_domain(bad) == "", bad


# ===========================================================================
# B 组｜入口规则（无附件，除 #9 外）（#7–#13）
# ===========================================================================
class TestEntryRule:
    """新增的 DRAFT 入口规则：完整客编 + sender_domain_whitelist 精确相等。"""

    def test_07_reply_with_full_code_from_yxo_domain(self):
        """#7 Re: 回复：运单草单…CQWLJT… → draft（回复类邮件原来一律 None）"""
        subject = "Re: 回复：运单草单_YXO-2026-927_CQWLJT261009003-BK_SMGU8818410"
        assert _identify(subject, FROM_YXO_CHENLONG) == "draft"

    def test_08_code_in_middle_of_subject(self):
        """#8 客编在主题中间（search 语义，不是 match）→ draft"""
        subject = ("Re: 11.5号：YXO-2026-1032  PO:CQWLJT261105001-MA,"
                   "柜号：FWRU0042825_随车资料")
        assert _identify(subject, FROM_YXO_LIUJIANGLAN) == "draft"

    def test_09_real_draft_with_attachment_still_category_A(self, biz_dbs):
        """#9 草单原件（已加密.pdf）仍由原 DRAFT 规则命中，行为不变（恒 A + 公司正确）"""
        # ① 有客编 + 附件：识别仍是 draft，且分类恒 A（不走域判定）
        row, routing, decision = _pipeline(
            "运单草单 CQWLJT260810001-SPB CICU1000001",
            FROM_YXO_CHENLONG,
            ["CICU1000001-260810-123456已加密.pdf"],
        )
        assert row.draft_category == "A"
        assert row.is_draft_attachment is True
        assert routing.company == "太平洋"
        assert (decision.tier, decision.action) == ("T1", "forward")

        # ② 主题里没有客编：新规则无从命中，仍由原附件规则识别为 draft
        assert _identify(
            "运单草单（无客编主题）", FROM_YXO_CHENLONG,
            ["CICU1000001-260810-123456已加密.pdf"],
        ) == "draft"

    def test_10_waybill_not_stolen_by_new_rule(self):
        """#10 docwbfb 的运单号邮件仍 waybill —— 新规则在列表最后，抢不走白名单类型"""
        assert _identify("运单号下发 CQWLJT260810001", FROM_YXO_DOCWBFB) == "waybill"

    def test_11_other_whitelisted_types_unchanged(self):
        """#11 atb / kasa / tracing-system 仍各自类型"""
        assert _identify("ATB for YXO-2026-638 CQWLJT260722001-D CICU1912468",
                         FROM_YXO_ATB) == "atb"
        assert _identify("DSK notification CQWLJT260810001", FROM_RTSB_KASA) == "dsk"
        assert _identify("train 793 tracking CQWLJT260810001",
                         FROM_YXO_TRACING) == "tracing"

    def test_12_release_notices_not_matched(self):
        """#12 渝新欧放箱 / 取消放箱令（无客编）→ 不命中任何规则"""
        assert _identify("渝新欧放箱", FROM_YXO_SHIDONG) is None
        assert _identify("渝新欧取消放箱令", FROM_YXO_SHIDONG) is None

    def test_13_robot_digest_not_matched(self):
        """#13 机器人日报 → 不命中（不设 subject_exclude 也不会吸进来）"""
        assert _identify("[正式] 草单转发机器人 定时汇总 12:00（3 轮）",
                         FROM_YXO_SHIDONG) is None


# ===========================================================================
# C 组｜费用邮件正反（洋 11-06 指定）（#14–#20）
# ===========================================================================
class TestFeeMail:
    """正例：完整客编 + yxo 域 ⇒ C1 → T1 forward → 保时达；反例：不命中。"""

    def test_14_lock_fee_forward_goes_to_baoshida(self, biz_dbs):
        """#14 正：境外加装电子锁费用 YXO-2026-924 CQWLJT261022003-Kol"""
        row, routing, decision = _pipeline(
            "Fw: 境外加装电子锁费用 YXO-2026-924  CQWLJT261022003-Kol  FWRU0194073",
            FROM_YXO_LUOHUA,
        )
        assert row.draft_category == "C1"
        assert routing.company == "保时达"
        assert (decision.tier, decision.action) == ("T1", "forward")
        assert "pyx@cqbestar.cn" in routing.to_list
        assert "xyli@cqbestar.cn" in routing.to_list

    def test_15_document_fee_forward_goes_to_baoshida(self, biz_dbs):
        """#15 正：【单据问题滞留产生的费用】YXO-2026-875 CQWLJT260924005-BLLST"""
        row, routing, decision = _pipeline(
            "Fw: 【单据问题滞留产生的费用】 YXO-2026-875  "
            "CQWLJT260924005-BLLST TEFU4001589",
            FROM_YXO_LUOHUA,
        )
        assert row.draft_category == "C1"
        assert routing.company == "保时达"
        assert (decision.tier, decision.action) == ("T1", "forward")
        assert "pyx@cqbestar.cn" in routing.to_list
        assert "xyli@cqbestar.cn" in routing.to_list

    def test_16_bare_cqwljt_with_suffix_word_not_matched(self):
        """#16 反：CQWLJT 后无数字/字母（`CQWLJT 6柜`）→ 不命中"""
        assert _identify("Fw: 境外加装电子锁费用 YXO-2026-993 CQWLJT 6柜",
                         FROM_YXO_LUOHUA) is None

    def test_17_no_code_one_box_only_not_matched(self):
        """#17 反：只有「1柜」，无客编 → 不命中"""
        assert _identify("Fw: 境外加装电子锁费用 YXO-2026-933 1柜", FROM_YXO_LUOHUA) is None

    def test_18_no_code_six_box_only_not_matched(self):
        """#18 反：只有「6柜」，无客编 → 不命中"""
        assert _identify("Fw: 境外加装电子锁费用 YXO-2026-995 6柜", FROM_YXO_LUOHUA) is None

    def test_19_company_domain_with_bare_code_not_matched(self):
        """#19 反：完整客编但发件人是公司域（cqgytcp.com）→ 不命中（域不在白名单）"""
        assert _identify("Re: 转发：境外加装电子锁费用 YXO-2026-993 CQWLJT 6柜",
                         FROM_CQGYTCP_ZHOU) is None

    def test_20_quotation_mail_not_matched(self):
        """#20 反：报价单主题（无完整客编）→ 不命中"""
        assert _identify("杜伊斯堡COC费用更新: Re: 渝新欧11月报价单",
                         FROM_YUXINOU_OPS) is None


# ===========================================================================
# D 组｜C2 边界与本周 7 条回归（#21–#28）
# ===========================================================================
class TestC2BoundaryAndRegression:
    """C2 边界原样：公司域 / cqtransit.com 的回复仍留人工、不转发。"""

    def test_21_cqtransit_reply_not_matched(self):
        """#21 回复：运单草单_927 / 杨雅雯 <yangyawen@cqtransit.com> → 不命中"""
        assert _identify("回复：运单草单_927", FROM_CQTRANSIT_YANG) is None

    def test_22_cail56_reply_not_matched(self):
        """#22 回复:Re: 回复：运单草单_927 / 胡益 <evelyn.hu@cail56.com> → 不命中"""
        assert _identify("回复:Re: 回复：运单草单_927", FROM_CAIL56_EVELYN) is None

    def test_23_cail56_reply_926_not_matched(self):
        """#23 回复:运单草单_926 / "evelyn.hu@cail56.com" <…> → 不命中"""
        assert _identify("回复:运单草单_926", FROM_CAIL56_EVELYN_QUOTED) is None

    def test_23b_domain_gate_blocks_company_domain_with_full_code(self):
        """#21–#23 的加强版：**完整客编** + 非白名单域 ⇒ 仍然不命中。

        只靠「无客编」解释不了 21–23；这一条单独证明域闸真的在起作用。
        """
        subject = "回复：运单草单_YXO-2026-927_CQWLJT261009003-BK_SMGU8818410"
        assert _identify(subject, FROM_CQTRANSIT_YANG) is None
        assert _identify(subject, FROM_CAIL56_EVELYN) is None
        # 同一封主题，发件人换成 yxo 域 ⇒ 命中（对照组）
        assert _identify(subject, FROM_YXO_CHENLONG) == "draft"

    def test_24_manual_category_constant_and_process_row_untouched(self, biz_dbs, monkeypatch):
        """#24 C2 仍是唯一留人工分类；_process_row 在 route_row 之前 return manual"""
        assert NON_AUTO_DRAFT_CATEGORIES == ("C2",)

        row = ExtractedRow(
            row_idx=0, email_type=EmailType.DRAFT,
            customer_code="CQWLJT261022003-Kol", container_no="FWRU0194073",
            draft_category="C2",
        )
        proc = object.__new__(MailProcessor)
        with patch.object(serve_mod, "try_claim", return_value=True), \
             patch.object(serve_mod, "route_row") as mock_route, \
             patch.object(serve_mod, "add_error") as mock_err:
            outcome = proc._process_row(
                row, "0:CQWLJT261022003-Kol", EmailType.DRAFT, "c2-case-24", b"",
                "maoxiaoyang@cqtransit.com", "INBOX", 1, FROM_CAIL56_EVELYN,
                "回复:运单草单_926", "",
            )
        assert outcome == "manual"
        mock_route.assert_not_called()   # 路由之前就返回
        mock_err.assert_not_called()     # 不进错误队列

        # 整封级：C2 行 ⇒ 不标已读（live 下也不标，邮件留未读给人工）
        monkeypatch.setenv("MAILBOT_MODE", "live")
        full = MailProcessor(snapshot())
        full.records = store_mod.load_records()
        full.accounts = {"maoxiaoyang@cqtransit.com": "fake-pw"}
        full.notifier = Mock()
        rows = [ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                             customer_code="CQWLJT261022003-Kol",
                             container_no="FWRU0194073", draft_category="C2")]
        raw = _mail("Re: 确认", FROM_CAIL56_EVELYN, ["箱号说明.txt"]).as_bytes()
        with patch.object(serve_mod, "extract_email", return_value=rows) as mock_extract, \
             patch.object(serve_mod, "mark_seen") as mock_mark, \
             patch.object(serve_mod, "enqueue_mark_seen") as mock_enq, \
             patch.object(serve_mod, "add_error") as mock_err2, \
             patch.object(serve_mod, "execute_action") as mock_act:
            ok = full.process_email("maoxiaoyang@cqtransit.com", "草单", "c2-case-24-full",
                                    1, "Re: 确认", FROM_CAIL56_EVELYN, "", raw)
        assert ok is True
        # 前提自检：这封真的被识别成 draft 并走到了 C2 留人工（不是「未分类」早退）
        assert mock_extract.call_args[0][0] == EmailType.DRAFT
        assert full._get_last_outcome().get("manual") == 1
        mock_mark.assert_not_called()
        mock_enq.assert_not_called()
        mock_act.assert_not_called()     # 不转发
        mock_err2.assert_not_called()    # 不进错误队列

    def test_25_decide_draft_first_line_still_blocks_c2(self):
        """#25 decide_draft 首行仍返回 (MANUAL, skip)"""
        row = ExtractedRow(
            row_idx=0, email_type=EmailType.DRAFT,
            customer_code="CQWLJT261022003-Kol", container_no="FWRU0194073",
            draft_category="C2",
        )
        routing = RoutingResult("保时达", "m@x.com", ["pyx@cqbestar.cn"], [],
                                "full_match", True)
        records = [{"客户编码": "CQWLJT261022003-Kol", "箱号": "FWRU0194073",
                    "company": "保时达"}]
        decision = decide_draft(row, routing, records)
        assert (decision.tier, decision.action) == ("MANUAL", "skip")

    def test_26_process_row_returns_manual_before_route_row(self):
        """#26 源码顺序：_process_row 里 C2 拦截必须在 route_row 调用之前"""
        src = inspect.getsource(MailProcessor._process_row)
        i_manual = src.index("NON_AUTO_DRAFT_CATEGORIES")
        i_route = src.index("route_row(")
        assert i_manual < i_route, "C2 拦截被挪到路由之后 ⇒ 会进错误队列"

    def test_27_main_draft_path_unchanged(self, biz_dbs):
        """#27 主件回归（本地可验的部分）：草单原件 ⇒ A / T1 forward / 公司正确。

        真实的「本周 22 条主件」只能在测试环境 F:\\YXO_TEST 由验收方复核
        （见 05_ACCEPTANCE.md B4），本地没有那 22 封原始邮件。
        """
        row, routing, decision = _pipeline(
            "运单草单 CQWLJT261022003-Kol FWRU0194073",
            FROM_YXO_LUOHUA,
            ["FWRU0194073-261022-101010已加密.pdf"],
        )
        assert row.draft_category == "A"
        assert routing.company == "保时达"
        assert (decision.tier, decision.action) == ("T1", "forward")

    def test_28_retired_record_not_silently_dropped(self, biz_dbs):
        """#28 退舱客编：load_records 过滤掉 ⇒ T4 alarm（不退化成静默跳过）"""
        row, routing, decision = _pipeline(
            "运单草单_YXO-2026-909_CQWLJT260810009-SPB_CICU1000009",
            FROM_YXO_LUOHUA,
        )
        assert row.draft_category == "C1"
        assert routing.success is False
        assert decision.action == "alarm"
        assert decision.tier == "T4"


# ===========================================================================
# E 组｜顺序 / 缓存 / 夹具（#29–#32）
# ===========================================================================
class TestRoutesOrderAndFixtures:
    """规则位置、priority 升序、缓存清理、夹具形态。"""

    def test_29_priority_strictly_ascending(self):
        """#29 priority 全表升序（tests/test_core.py 也断言这一条）"""
        priorities = [r["priority"] for r in TYPE_ROUTES]
        assert priorities == sorted(priorities)

    def test_30_new_rule_is_last_and_others_untouched(self):
        """#30 新规则在最后、enabled True；前 8 条的 enabled/priority/match 一字未改"""
        assert len(TYPE_ROUTES) == 9
        last = TYPE_ROUTES[-1]
        assert last["type"] == EmailType.DRAFT
        assert last["priority"] == 60
        assert last["enabled"] is True
        assert sorted(last["match"].keys()) == ["sender_domain_whitelist", "subject_pattern"]
        assert last["match"]["sender_domain_whitelist"] == ["yxologistics.com"]
        assert last["match"]["subject_pattern"] == r"CQWLJT[0-9A-Za-z\-]+"

        head = TYPE_ROUTES[:8]
        assert [r["priority"] for r in head] == [5, 6, 7, 10, 20, 30, 40, 50]
        assert [r["enabled"] for r in head] == [False, False, False, True, True, True, True, True]
        assert [r["type"].value for r in head] == [
            "release_cancel", "release", "retbox",
            "waybill", "atb", "dsk", "tracing", "draft",
        ]
        # 前 8 条的 match 键集合原样（现有 5 条的不许被动过）
        assert [sorted(r["match"].keys()) for r in head[3:]] == [
            ["sender_whitelist"],
            ["sender_whitelist"],
            ["sender_whitelist"],
            ["sender_whitelist"],
            ["attachment_pattern", "sender_exclude"],
        ]

    def test_31_cache_cleared_after_mutation(self):
        """#31 临时改规则/类型后必须 invalidate_cache()，否则测试按文件顺序飘"""
        from mailbots_next.config.provider import _RUNTIME_CACHE

        get_enabled_types()
        assert "enabled_types" in _RUNTIME_CACHE
        invalidate_cache()
        assert _RUNTIME_CACHE == {}

    def test_32_all_test_fixtures_use_full_from_header(self):
        """#32 全量扫描 tests/：没有「裸邮箱直接赋给 From」的夹具。

        这是 02_TASK.md 红线 ④ 的机器判据。用纯邮箱夹具会全绿、上线才炸：
        生产里 sender 是完整 From 头，旧实现 split("@")[-1] 会拿到
        `yxologistics.com>`（带尖括号）⇒ 内部回复误判 C2。
        """
        pattern = re.compile(
            r"From.*=.*[\"'][A-Za-z0-9_.-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}[\"']\s*$"
        )
        here = os.path.dirname(os.path.abspath(__file__))
        hits = []
        for path in glob.glob(os.path.join(here, "*.py")):
            with io.open(path, encoding="utf-8") as f:
                for i, line in enumerate(f.read().splitlines(), start=1):
                    if pattern.search(line):
                        hits.append((os.path.basename(path), i, line.strip()))
        assert hits == [], "发现裸邮箱 From 夹具：%r" % (hits,)

        # 反向自检：本文件构造的必须是完整头（能被解析回同一个域）
        assert normalize_sender_domain(FROM_CAIL56_EVELYN) == "cail56.com"
        assert normalize_sender_domain(FROM_GMAIL_CUSTOMER) == "gmail.com"
