# -*- coding: utf-8 -*-
"""`core/boxmatch.py` 判定对齐测试（任务 06）。

契约来源：#45/#46/#47 已验收的老系统测试表，同一张表喂新系统，结果必须一致。
    - match_atb   ← mailbots/tests/unit/test_atbroute.py RESOLVE_CASES
    - match_dsk   ← mailbots/tests/unit/test_dskroute.py RESOLVE_CASES / TRAIN_CASES
    - match_draft_num ← mailbots/tests/unit/test_nummatch.py NUM_CASES（8 个数字段）

⚠️ 与老系统唯一有意的差异：多条命中时本模块返回 hits[0]（老系统返回 None）。
   理由：基线 `routing.py:100` 是 `company = matches[0]`，box_reuse 的 company
   必须与基线一致，否则 act 拿到的数据会变。见 SPEC §九 意见 1。
"""
import json

import pytest

from mailbots_next.config import EmailType, CODE_NUM_RE
from mailbots_next.core import boxmatch
from mailbots_next.core.decide import decide_draft
from mailbots_next.core.extract import ExtractedRow, extract_email
from mailbots_next.core.routing import route_atb, route_dsk, route_draft
from mailbots_next.core.store import get_bot_config_connection, init_bot_config_db

# 集成用例要用到的公司（get_recipients 走 bot_config，必须有 DEFAULT 路由）
_RECIPIENTS = {
    "太平洋": ["tp@test.com"],
    "东盟": ["dm@test.com"],
    "同程配": ["tcp@test.com"],
    "中欧木业": ["zom@test.com"],
    "沙坪坝": ["spb@test.com"],
    "保时达": ["bsd@test.com"],
    "港九港铁": ["gj@test.com"],
    "环宇通": ["hyt@test.com"],
}


@pytest.fixture(scope="module", autouse=True)
def _seed_recipients():
    """给集成用例灌公司收件人（本文件自带，不依赖别的测试先跑）。"""
    init_bot_config_db()
    conn = get_bot_config_connection()
    try:
        for company, to in _RECIPIENTS.items():
            conn.execute(
                """INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra)
                   VALUES ('all', 'company', ?, ?, '[]', '{}')""",
                (company, json.dumps(to)),
            )
        conn.commit()
    finally:
        conn.close()
    yield


def _rec(rid, code, box, company, train=""):
    return {"id": rid, "客户编码": code, "箱号": box, "班列号": train, "company": company}


# ---------------------------------------------------------------- match_atb（8）
# (用例名, records, 来信客编, 箱号, 期望 level, 期望 record id)
# 多条命中期望 record id 是 hits[0]（本模块契约），不是 None。
ATB_CASES = [
    ("combo_unique",
     [_rec(11, "CQWLJT1-MA", "A", "保时达")],
     "CQWLJT1-MA", "A", boxmatch.LV_CODE_BOX, 11),
    # 有客编但组合查不到：判 miss，不静默回退到箱号
    ("combo_miss_but_box_has_other",
     [_rec(12, "CQWLJT9-ZZ", "A", "同程配")],
     "CQWLJT1-MA", "A", boxmatch.LV_MISS, None),
    ("box_only_unique",
     [_rec(13, "CQWLJT2-BB", "A", "东盟")],
     "", "A", boxmatch.LV_BOX_ONLY, 13),
    ("box_only_multi",
     [_rec(14, "CQWLJT2-BB", "A", "东盟"), _rec(15, "CQWLJT3-CC", "A", "保时达")],
     "", "A", boxmatch.LV_MULTI, 14),
    # 客编+箱号组合命中两条（同组合的重复行）⇒ multi，record = hits[0]
    ("combo_multi",
     [_rec(16, "CQWLJT1-MA", "A", "保时达"), _rec(17, "CQWLJT1-MA", "A", "中欧木业")],
     "CQWLJT1-MA", "A", boxmatch.LV_MULTI, 16),
    ("box_not_found",
     [],
     "", "ZZZZ", boxmatch.LV_MISS, None),
    # 纯箱号有 2 条会 multi，但带客编时组合唯一 ⇒ 客编把歧义消掉了（ATB 改造的核心）
    ("code_disambiguates_reused_box",
     [_rec(18, "CQWLJT1-MA", "A", "保时达"), _rec(19, "CQWLJT2-BB", "A", "东盟")],
     "CQWLJT1-MA", "A", boxmatch.LV_CODE_BOX, 18),
    ("empty_box",
     [],
     "", "", boxmatch.LV_MISS, None),
]


@pytest.mark.parametrize("name,records,code,box,exp_level,exp_id", ATB_CASES)
def test_match_atb(name, records, code, box, exp_level, exp_id):
    level, rec, cands = boxmatch.match_atb(records, code, box)
    assert level == exp_level, name
    assert (rec["id"] if rec else None) == exp_id, name
    if exp_level == boxmatch.LV_MULTI:
        # 多条时 record 必须落在既有候选里，且候选数 >= 2
        assert len(cands) >= 2, name
        assert rec in cands, name


# ---------------------------------------------------------------- match_dsk（8）
# (班列号, 箱号, records, 期望 level) —— 逐条抄老系统 RESOLVE_CASES
DSK_CASES = [
    ("WB223", "A", [_rec(1, "", "A", "中欧木业", "WB223")], boxmatch.LV_TRAIN_BOX),
    # 组合查不到、箱号唯一 ⇒ 兜底 box_only
    ("WB223", "A", [_rec(2, "", "A", "同程配", "WB881")], boxmatch.LV_BOX_ONLY),
    # 组合查不到、箱号两条 ⇒ multi
    ("WB223", "A", [_rec(3, "", "A", "同程配", "WB881"), _rec(4, "", "A", "保时达", "WB883")],
     boxmatch.LV_MULTI),
    # 组合键命中两条 ⇒ multi
    ("WB223", "A", [_rec(5, "", "A", "中欧木业", "WB223"), _rec(6, "", "A", "中欧木业", "WB223")],
     boxmatch.LV_MULTI),
    # 无班列号、箱号唯一
    ("", "A", [_rec(7, "", "A", "同程配", "WB881")], boxmatch.LV_BOX_ONLY),
    # 无班列号、箱号两条
    ("", "A", [_rec(8, "", "A", "同程配", "WB881"), _rec(9, "", "A", "保时达", "WB883")],
     boxmatch.LV_MULTI),
    # 箱号在库内查不到
    ("", "ZZZZ", [], boxmatch.LV_MISS),
    # 班列号不在组合里、箱号只有一条 ⇒ 兜底 box_only
    ("WB999", "A", [_rec(10, "", "A", "同程配", "WB777")], boxmatch.LV_BOX_ONLY),
]


@pytest.mark.parametrize("train,box,records,exp_level", DSK_CASES)
def test_match_dsk(train, box, records, exp_level):
    level, rec, _c = boxmatch.match_dsk(records, box, train)
    assert level == exp_level
    if exp_level in (boxmatch.LV_TRAIN_BOX, boxmatch.LV_BOX_ONLY):
        assert rec is not None and rec["箱号"] == box
    elif exp_level == boxmatch.LV_MULTI:
        # 多条时同样返回 hits[0]（不是 None）
        assert rec is not None and rec is records[0]
    else:
        assert rec is None


# ---------------------------------------------------------- match_draft_num（9）
# (数字段, [(公司, 状态)], 期望 level, 期望 company)
# 记录已按新系统口径预过滤（退舱行不进 records），所以"全退舱"组在此表现为空列表。
DRAFT_NUM_CASES = [
    ("260718008", [("保时达", "正常")], boxmatch.LV_NUM_UNIQUE, "保时达"),
    ("260718009", [("沙坪坝", "正常")], boxmatch.LV_NUM_UNIQUE, "沙坪坝"),
    ("260923001", [("太平洋", "正常")], boxmatch.LV_NUM_UNIQUE, "太平洋"),
    ("261001008", [("港九港铁", "正常")], boxmatch.LV_NUM_UNIQUE, "港九港铁"),
    ("261021001", [("东盟", "正常")], boxmatch.LV_NUM_UNIQUE, "东盟"),
    ("260618003", [("同程配", "正常")], boxmatch.LV_NUM_UNIQUE, "同程配"),
    ("260209002", [], boxmatch.LV_MISS, None),          # 原组全退舱 ⇒ 预过滤后无记录
    ("261001009", [], boxmatch.LV_MISS, None),          # 原组全退舱 ⇒ 预过滤后无记录
]


def _num_records(num, group):
    return [_rec(i + 1, "CQWLJT%s-%s" % (num, "ABCDEFG"[i]), "", comp)
            for i, (comp, _st) in enumerate(group)]


@pytest.mark.parametrize("num,group,exp_level,exp_company", DRAFT_NUM_CASES)
def test_match_draft_num(num, group, exp_level, exp_company):
    records = _num_records(num, group)
    level, rec, _c = boxmatch.match_draft_num(records, num)
    assert level == exp_level
    if exp_level == boxmatch.LV_NUM_UNIQUE:
        assert rec is not None and rec["company"] == exp_company
    else:
        assert rec is None


def test_match_draft_num_does_not_prefix_match_10_digits():
    """前缀反例：`CQWLJT2609120030`（10 位）不能被数字段 `260912003` 命中。

    用 startswith 写法会误匹配，所以口径必须是 CODE_NUM_RE.match().group(1) == num。
    """
    records = [_rec(1, "CQWLJT2609120030-A", "X", "同程配")]
    level, rec, _c = boxmatch.match_draft_num(records, "260912003")
    assert level == boxmatch.LV_MISS
    assert rec is None


# ------------------------------------------------------ extract_train_number（8）
# 与老系统 test_dskroute.TRAIN_CASES 同一张表，只把期望值 "" 换成 None。
TRAIN_CASES = [
    ("YXO-223 / DSK / AGROSTOP - 27.03.2026", "WB223"),
    ("DSK / AGROSTOP / YXO-219 - 13.04.2026 - FWRU0197560 x 1", "WB219"),
    ("YXO-219/DSK/AGROSTOP 13.04.2025", "WB219"),
    ("DSK YXO WB0808-ATD0809(773) / 1 x Ready for pick-up from Agrostop", "WB773"),
    # ID507 不是班列号 ⇒ 必须取不到，走箱号兜底
    ("DSK YXO WB0821 ATD0823 ID507 CICU2690998 x 1 / Ready for pick-up", None),
    ("YXO-2026-864 / DSK / AGROSTOP - 01.05.2026", "WB864"),
    ("DSK/AGROSTOP 01.01.2026", None),
    (None, None),
]


@pytest.mark.parametrize("subject,expected", TRAIN_CASES)
def test_extract_train_number(subject, expected):
    assert boxmatch.extract_train_number(subject) == expected


# ------------------------------------------------- _result_from / route_*（6）
def test_route_dsk_unique_is_box_match():
    recs = [_rec(1, "", "CICU1000001", "太平洋", "WB1")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU1000001",
                       train_no="WB1")
    r = route_dsk(row, recs)
    assert r.route_source == "box_match"
    assert r.success is True
    assert r.company == "太平洋"
    assert r.to_list == ["tp@test.com"]


def test_route_dsk_reuse_keeps_success_and_hits0_company():
    """box_reuse：success 仍为 True，company 取 hits[0]（与基线 route_dsk 一致）。"""
    recs = [
        _rec(1, "", "CICU1000001", "太平洋", "WB1"),
        _rec(2, "", "CICU1000001", "东盟", "WB2"),
    ]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU1000001")
    r = route_dsk(row, recs)
    assert r.route_source == "box_reuse"
    assert r.success is True
    assert r.company == recs[0]["company"]
    assert r.to_list == ["tp@test.com"]


def test_route_dsk_miss_is_no_route():
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU9999999")
    r = route_dsk(row, [])
    assert r.route_source == "no_route"
    assert r.success is False
    assert r.company == ""
    assert r.to_list == []


def test_route_atb_uses_code_not_box_only():
    """箱号被两家复用，但客编唯一 ⇒ 走 box_match（ATB 改造点）。"""
    recs = [
        _rec(1, "CQWLJT1-MA", "CICU1000001", "太平洋"),
        _rec(2, "CQWLJT2-BB", "CICU1000001", "东盟"),
    ]
    row = ExtractedRow(row_idx=0, email_type=EmailType.ATB,
                       customer_code="CQWLJT1-MA", container_no="CICU1000001")
    r = route_atb(row, recs)
    assert r.route_source == "box_match"
    assert r.success is True
    assert r.company == "太平洋"


def test_route_atb_code_miss_does_not_fall_back_to_box():
    recs = [_rec(1, "CQWLJT9-ZZ", "CICU1000001", "同程配")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.ATB,
                       customer_code="CQWLJT1-MA", container_no="CICU1000001")
    r = route_atb(row, recs)
    assert r.route_source == "no_route"
    assert r.success is False


def test_route_dsk_without_box_keeps_no_box_source():
    """空箱号保留基线取值 no_box（不是 no_route），取值集合因此不变。"""
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no=None)
    r = route_dsk(row, [])
    assert r.route_source == "no_box"
    assert r.success is False


# ------------------------------------------------------------ decide T1N（4）
def test_decide_draft_num_unique_forwards_t1n():
    """数字段命中 + 公司唯一 + 同数字段同箱不同后缀（会先撞 T2）⇒ T1N forward。"""
    recs = [
        _rec(1, "CQWLJT260718008-A", "CICU1000001", "保时达"),
        _rec(2, "CQWLJT260718008-B", "CICU1000001", "保时达"),
    ]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT260718008-XXX", customer_code_num="260718008",
                       container_no="CICU1000001")
    r = route_draft(row, recs)
    assert r.route_source == "num_match"
    d = decide_draft(row, r, recs)
    assert d.tier == "T1N" and d.action == "forward"


def test_decide_draft_num_cross_company_stays_pending():
    """数字段命中但跨公司 ⇒ 不转（T1N 不成立），仍走 stem 分析。"""
    recs = [
        _rec(1, "CQWLJT260101001-A", "", "太平洋"),
        _rec(2, "CQWLJT260101001-B", "", "保时达"),
    ]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT260101001-XXX", customer_code_num="260101001",
                       container_no="")
    r = route_draft(row, recs)
    assert r.route_source != "num_match"
    d = decide_draft(row, r, recs)
    assert d.action == "pending"


def test_decide_draft_exact_code_stays_t1():
    recs = [_rec(1, "CQWLJT260718008-A", "CICU1000001", "保时达")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT260718008-A", customer_code_num="260718008",
                       container_no="CICU1000001")
    r = route_draft(row, recs)
    assert r.route_source == "full_match"
    d = decide_draft(row, r, recs)
    assert d.tier == "T1" and d.action == "forward"


def test_decide_draft_no_num_hit_unchanged():
    """数字段无命中时 T1N 不得介入：仍是原来的 T3 pending。"""
    recs = [_rec(1, "CQWLJT260718008-A", "CICU9999999", "保时达")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT260718008-XXX", customer_code_num="260718099",
                       container_no="CICU1000001")
    r = route_draft(row, recs)
    assert r.route_source == "no_route"
    d = decide_draft(row, r, recs)
    assert d.tier == "T3" and d.action == "pending"


# ------------------------------------------------------------- extractors（8）
def _atb_raw(subject, box="CICU1912468"):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = "atb@yxologistics.com"
    msg.attach(MIMEText("test", "plain"))
    att = MIMEBase("application", "pdf")
    att.set_payload(b"test")
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", "attachment", filename="%s_ATB.pdf" % box)
    msg.attach(att)
    return msg.as_bytes()


ATB_CODE_CASES = [
    ("ATB for YXO-2026-864   CQWLJT260912003-D   FWRU0042785", "CQWLJT260912003-D"),
    ("ATB for YXO-2026-864   CQWLJT260912003   FWRU0042785", "CQWLJT260912003"),
    ("pickup reference for YXO-2026-134  FWRU0042785", None),
    ("cqwljt260912003-d", None),          # 小写不匹配：已知边界，按取不到处理
]


@pytest.mark.parametrize("subject,expected", ATB_CODE_CASES)
def test_atb_extractor_fills_full_customer_code(subject, expected):
    box = "FWRU0042785" if "FWRU" in subject else "CICU1912468"
    rows = extract_email(EmailType.ATB, _atb_raw(subject, box))
    assert len(rows) == 1
    assert rows[0].customer_code == expected


def _dsk_raw(subject, html=None, filename=None):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = "reex.mala@deutschebahn.com"   # 非 kasa：否则箱号改由 html 表格决定
    msg.attach(MIMEText(html or "<html><body>no table</body></html>", "html"))
    if filename:
        att = MIMEBase("application", "pdf")
        att.set_payload(b"test")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(att)
    return msg.as_bytes()


DSK_TRAIN_CASES = [
    ("ODP: YXO-223 / DSK / AGROSTOP 27.03.2026 CICU1234567", None, "WB223"),
    ("DSK YXO WB0808-ATD0809(773) / 1 x Ready CICU1234567", None, "WB773"),
    ("DSK YXO WB0821 ATD0823 ID507 CICU1234567 x 1 / Ready", None, None),
    ("DSK AGROSTOP 01.01.2026", "CICU1234567.PDF", None),   # 箱号只在附件名里
]


@pytest.mark.parametrize("subject,filename,expected", DSK_TRAIN_CASES)
def test_dsk_extractor_sets_train_no(subject, filename, expected):
    rows = extract_email(EmailType.DSK, _dsk_raw(subject, filename=filename))
    assert len(rows) == 1
    assert rows[0].train_no == expected
    assert rows[0].container_no == "CICU1234567"


# ---------------------------------------------------------------------------
def test_code_num_re_is_same_object_as_config():
    """自检命令 1b 的等价断言：不是两个同 pattern 的副本，是同一个对象。"""
    assert boxmatch.CODE_NUM_RE is CODE_NUM_RE
    assert boxmatch.CODE_NUM_RE.pattern == r"CQWLJT(\d+)"
