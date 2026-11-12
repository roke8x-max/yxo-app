"""放箱令 / 取消放箱令 / 还箱令 —— 路由与档位（任务 10 第二阶段）。

不依赖 IMAP、不发信、不读真实库：records 全部在内存里构造，用 `02_TASK.md §6.1`
的 REAL 实测数据（客编 / 箱号 / 班列 / 公司 / 班列类型 都来自 yxo.db 真实行）。

⚠️ §6.0 的收件人桩是必须的：`get_recipients` / `get_responsible_person` 查 bot_config
表，测试环境是刚建出来的空表 ⇒ 不打桩则恒返回 [] ⇒ success 恒 False ⇒ 正例全红。
桩必须打在 `mailbots_next.core.routing` 上（routing.py 是 `from .store import
get_recipients`，导入时就把函数对象绑死了），打在 store 上无效。
"""
import importlib
import logging

import pytest

from mailbots_next.config import EmailType
from mailbots_next.core import routing
from mailbots_next.core.extract import ExtractedRow

# decide 的名字在 core/__init__.py 里被函数对象占住了
# （`from .decide import decide`），`import mailbots_next.core.decide as m` 拿到的是
# 函数而不是模块，所以这里走 importlib。
decide_mod = importlib.import_module("mailbots_next.core.decide")
decide = decide_mod.decide
decide_release = decide_mod.decide_release
decide_retbox = decide_mod.decide_retbox
_train_type = decide_mod._train_type

DECIDE_LOGGER = "mailbots_next.core.decide"


# ---------------------------------------------------------------- 夹具

@pytest.fixture(autouse=True)
def _stub_recipients(monkeypatch):
    """收件人打桩：bot_config 在测试环境是空表，不打桩则 success 恒为 False。"""

    def fake_recipients(company, scope="company"):
        if not company:
            return [], []
        return ([f"{company}@example.test"], ["cc@example.test"])

    def fake_person(company):
        return f"{company}-owner" if company else None

    monkeypatch.setattr(routing, "get_recipients", fake_recipients)
    monkeypatch.setattr(routing, "get_responsible_person", fake_person)


# ---------------------------------------------------------------- 数据

REAL = [
    {"客户编码": "CQWLJT261108051-VXN", "箱号": "", "班列号": "WB943",
     "目的站": "沃尔西诺", "company": "保时达", "班列类型": "专列"},
    {"客户编码": "CQWLJT261111005-MA", "箱号": "", "班列号": "WB1055",
     "目的站": "马拉", "company": "港九港铁", "班列类型": "散舱"},
    {"客户编码": "CQWLJT261015003-DMZ", "箱号": "XHCU5271865", "班列号": "WB957",
     "目的站": "电煤", "company": "同程配", "班列类型": "专列"},
    {"客户编码": "CQWLJT261015004-DMZ", "箱号": "XHCU5030934", "班列号": "WB957",
     "目的站": "电煤", "company": "联运", "班列类型": "专列"},
    {"客户编码": "CQWLJT261005003-VXN", "箱号": "", "班列号": "WB845",
     "目的站": "沃尔西诺", "company": "中欧木业", "班列类型": "散舱"},
]


def _row(email_type=EmailType.RELEASE, code=None, num=None, box=None, **raw):
    """ExtractedRow：row_idx / email_type 没有默认值，必须显式传。

    raw_data 默认是 {}（default_factory=dict），不是 None。
    """
    return ExtractedRow(
        row_idx=0,
        email_type=email_type,
        customer_code=code,
        customer_code_num=num,
        container_no=box,
        raw_data=dict(raw),
    )


def _rec(code, company, train, box="", train_type="散舱", station="测试站"):
    return {"客户编码": code, "箱号": box, "班列号": train, "目的站": station,
            "company": company, "班列类型": train_type}


# ---------------------------------------------------------------- 1~2 精确命中 + 分流

def test_01_release_exact_zhuanlie_pool():
    """放箱令精确命中专列：R1 + pool，班列信息回填到 row。"""
    row = _row(EmailType.RELEASE, code="CQWLJT261108051-VXN", num="261108051")
    r = routing.route_row(EmailType.RELEASE, row, REAL)

    assert r.company == "保时达"
    assert r.route_source == "code_exact"
    assert r.success is True
    assert row.train_no == "WB943"
    assert row.raw_data["train_type"] == "专列"
    assert row.raw_data["dest_station"] == "沃尔西诺"

    d = decide(EmailType.RELEASE, row, r, REAL)
    assert (d.tier, d.action) == ("R1", "pool")


def test_02_release_exact_sancang_forward():
    """放箱令精确命中散舱：R1 + forward。"""
    row = _row(EmailType.RELEASE, code="CQWLJT261111005-MA", num="261111005")
    r = routing.route_row(EmailType.RELEASE, row, REAL)

    assert r.company == "港九港铁"
    assert row.raw_data["train_type"] == "散舱"

    d = decide(EmailType.RELEASE, row, r, REAL)
    assert (d.tier, d.action) == ("R1", "forward")


# ---------------------------------------------------------------- 3 取消件 R1N

def test_03_cancel_stem_match_r1n_warns(caplog):
    """取消件只靠数字段命中：R1N + forward，且必须打 WARN（不许静默）。"""
    row = _row(EmailType.RELEASE_CANCEL, code="CQWLJT261005003-Kol", num="261005003")
    r = routing.route_row(EmailType.RELEASE_CANCEL, row, REAL)

    assert r.route_source == "code_num"
    assert r.company == "中欧木业"
    assert r.success is True

    with caplog.at_level(logging.WARNING, logger=DECIDE_LOGGER):
        d = decide(EmailType.RELEASE_CANCEL, row, r, REAL)

    # WB845 是散舱
    assert (d.tier, d.action) == ("R1N", "forward")
    assert any("R1N stem match" in rec.message for rec in caplog.records)


# ------------------------------------------------- 4~5 还箱令：箱号不再单判

def test_04_retbox_without_code_is_no_route():
    """还箱令无客编：箱号即使唯一也不许定位公司（任务 17 删掉 box_unique 兜底）。

    基线名 test_04_retbox_box_unique_pool_warns（箱号唯一 ⇒ B2 pool）。
    """
    row = _row(EmailType.RETBOX, box="XHCU5271865")
    assert row.customer_code is None and row.customer_code_num is None

    r = routing.route_row(EmailType.RETBOX, row, REAL)
    assert r.success is False
    assert r.company == ""
    assert r.route_source == "no_route"

    # 失败在 routing 层就 return "queued"（serve 报警分支），decide 侧只做兜底：
    # B2 已删除，落到 B0 告警。
    d = decide_retbox(row, r, REAL)
    assert (d.tier, d.action) == ("B0", "alarm")


def test_05_retbox_same_train_other_company():
    """同班列 WB957 的另一个箱：同样不按箱号定公司（不是「联运」，也不是「同程配」）。"""
    row = _row(EmailType.RETBOX, box="XHCU5030934")
    r = routing.route_row(EmailType.RETBOX, row, REAL)

    assert r.success is False
    assert r.company == ""
    assert r.route_source == "no_route"


# ---------------------------------------------------------------- 6 查无 + R0

def test_06_no_route_alarms():
    """不存在的客编：no_route / success False / 空 company，decide_release → R0。"""
    row = _row(EmailType.RELEASE, code="CQWLJT299999999-ZZZ", num="299999999")
    r = routing.route_row(EmailType.RELEASE, row, REAL)

    assert r.success is False
    assert r.route_source == "no_route"
    assert r.company == ""

    # 真实流程里 success=False 在 _process_routing_result 就 return 了，
    # 走不到 decide()，所以这里直接调 decide_release。
    d = decide_release(row, r, REAL)
    assert (d.tier, d.action) == ("R0", "alarm")
    assert "No route" in d.reason


# ---------------------------------------------------------------- 7 专列/散舱分流

def test_07_train_type_splits_action():
    """同一条链路上，专列 → pool，散舱 → forward。"""
    pool_row = _row(EmailType.RELEASE, code="CQWLJT261108051-VXN", num="261108051")
    fwd_row = _row(EmailType.RELEASE, code="CQWLJT261111005-MA", num="261111005")

    r_pool = routing.route_row(EmailType.RELEASE, pool_row, REAL)
    r_fwd = routing.route_row(EmailType.RELEASE, fwd_row, REAL)

    assert decide(EmailType.RELEASE, pool_row, r_pool, REAL).action == "pool"
    assert decide(EmailType.RELEASE, fwd_row, r_fwd, REAL).action == "forward"


# ------------------------------------------------- 8 箱号复用：不再 B6，直接失败

def test_08_box_reuse_without_code_is_no_route():
    """同一箱号命中多票且邮件没有客编：routing 层直接失败，decide 侧落 B0。

    基线名 test_08_box_reuse_b6（box_reuse ⇒ B6）；任务 17 起 route_retbox
    不再返回 box_reuse，B6 分支因此端到端不可达（代码保留，见 02_TASK 1.0）。
    """
    records = [
        _rec("CQWLJT261015003-DMZ", "同程配", "WB957", box="XHCU9000001", train_type="专列"),
        _rec("CQWLJT261015004-DMZ", "联运", "WB957", box="XHCU9000001", train_type="专列"),
    ]
    row = _row(EmailType.RETBOX, box="XHCU9000001")
    r = routing.route_row(EmailType.RETBOX, row, records)

    assert r.route_source == "no_route"
    assert r.success is False
    assert r.company == ""

    d = decide_retbox(row, r, records)
    assert (d.tier, d.action) == ("B0", "alarm")


# ---------------------------------------------------------------- 9 数字段跨公司

def test_09_stem_multi_company_r0():
    """同数字段命中两家公司：num_multi_company，reason 是 Stem ambiguous 不是 No route。"""
    records = [
        _rec("CQWLJT261201001-VXN", "保时达", "WB943", train_type="专列"),
        _rec("CQWLJT261201001-MA", "港九港铁", "WB1055", train_type="散舱"),
    ]
    row = _row(EmailType.RELEASE, code="CQWLJT261201001-XXX", num="261201001")
    r = routing.route_release(row, records)

    assert r.route_source == "num_multi_company"
    assert r.success is False
    assert r.company == ""

    d = decide_release(row, r, records)
    assert (d.tier, d.action) == ("R0", "alarm")
    assert "Stem ambiguous" in d.reason
    assert "No route" not in d.reason


# ---------------------------------------------------------------- 10 数字段命中但公司全空

def test_10_stem_no_company_r0():
    """数字段命中了行但公司字段全空：num_no_company，不能混成 no_route。"""
    records = [_rec("CQWLJT261202001-VXN", "", "WB845", train_type="散舱")]
    row = _row(EmailType.RELEASE, code="CQWLJT261202001-XXX", num="261202001")
    r = routing.route_release(row, records)

    assert r.route_source == "num_no_company"
    assert r.success is False

    d = decide_release(row, r, records)
    assert (d.tier, d.action) == ("R0", "alarm")


# ---------------------------------------------------------------- 11 兜底写回 raw_data

def _train_records(n, train="WB9000", train_type=""):
    """同班列 n 个不同客编（班列类型故意留空，逼 _train_type 走兜底）。"""
    return [
        {"客户编码": f"CQWLJT2612{i:05d}-VXN", "箱号": "", "班列号": train,
         "目的站": "沃尔西诺", "company": "保时达", "班列类型": train_type}
        for i in range(n)
    ]


def test_11_fallback_written_back_to_raw_data():
    """班列类型为空、同班列 50 个客编：decide 后 raw_data 必须落成 专列（不是 ""）。"""
    records = _train_records(50)
    row = _row(EmailType.RELEASE, code="CQWLJT261200000-VXN", num="261200000")
    r = routing.route_release(row, records)

    # route 阶段字段为空 ⇒ raw_data 里还是空串
    assert row.raw_data["train_type"] == ""

    d = decide_release(row, r, records)
    assert d.action == "pool"
    # 🔴 兜底算出的值必须写回，否则下游聚合器读到空值
    assert row.raw_data["train_type"] == "专列"


# ---------------------------------------------------------------- 12 40/41 边界

def test_12_train_type_boundary_40_vs_41():
    """同班列客编去重数 40 → 散舱，41 → 专列（判据是 > 40）。"""
    records40 = _train_records(40)
    row40 = _row(EmailType.RELEASE, code="CQWLJT261200000-VXN", num="261200000")
    r40 = routing.route_release(row40, records40)
    assert _train_type(records40, row40) == "散舱"
    assert row40.raw_data["train_type"] == "散舱"
    assert decide_release(row40, r40, records40).action == "forward"

    records41 = _train_records(41)
    row41 = _row(EmailType.RELEASE, code="CQWLJT261200000-VXN", num="261200000")
    r41 = routing.route_release(row41, records41)
    assert _train_type(records41, row41) == "专列"
    assert row41.raw_data["train_type"] == "专列"
    assert decide_release(row41, r41, records41).action == "pool"


# ---------------------------------------------------------------- 13 数字段两行、首行为空公司

def test_13_stem_picks_row_with_company():
    """同数字段两行、第一行 company 为空：必须取非空那家，不能裸取 hits[0]。"""
    records = [
        _rec("CQWLJT261203001-VXN", "", "WB845", train_type="散舱"),
        _rec("CQWLJT261203001-DMZ", "中欧木业", "WB845", train_type="散舱"),
    ]
    row = _row(EmailType.RELEASE_CANCEL, code="CQWLJT261203001-Kol", num="261203001")
    r = routing.route_release(row, records)

    assert r.route_source == "code_num"
    assert r.success is True
    assert r.company == "中欧木业"


# ---------------------------------------------------------------- 14 尾随空格 strip

def test_14_train_type_trailing_space_stripped():
    """"专列 "（尾随空格）进 decide：写回必须是 strip 后的 "专列"，action=pool。

    ⚠️ 这里**不走 route**：route_release 已经把值 strip 过了，走 route 就只能证明
    "route 会 strip"，证明不了 `_train_type` 的「字段优先」分支会不会写回。
    直接构造 raw_data 里带空格的 row，才能验到那一行赋值。
    """
    records = [_rec("CQWLJT261204001-VXN", "保时达", "WB943", train_type="专列")]
    row = _row(EmailType.RELEASE, code="CQWLJT261204001-VXN", num="261204001",
               train_type="专列 ")
    routing_result = routing.RoutingResult(
        company="保时达",
        responsible_person="保时达-owner",
        to_list=["保时达@example.test"],
        cc_list=["cc@example.test"],
        route_source="code_exact",
        success=True,
    )

    d = decide(EmailType.RELEASE, row, routing_result, records)
    assert row.raw_data["train_type"] == "专列"
    assert d.action == "pool"


# ---------------------------------------------------------------- 15 B0 区分 No recipients

def test_15_retbox_no_recipients_b0(monkeypatch):
    """还箱令 company 非空但收件人为空：B0 + reason 含 No recipients（不是 No route）。

    任务 17：路由入口从「只给箱号」改成「客编+箱号」组合键（箱号单判已删除）。
    """
    row = _row(EmailType.RETBOX, code="CQWLJT261015003-DMZ", num="261015003",
               box="XHCU5271865")
    r = routing.route_retbox(row, REAL)
    assert r.company == "同程配"

    # 覆盖掉 autouse 桩：模拟 bot_config 里这家公司没配 to_addrs
    monkeypatch.setattr(routing, "get_recipients", lambda *a, **k: ([], []))
    r = routing.route_retbox(row, REAL)
    assert r.success is False
    assert r.company == "同程配"

    d = decide_retbox(row, r, REAL)
    assert (d.tier, d.action) == ("B0", "alarm")
    assert "No recipients" in d.reason
    assert "No route" not in d.reason


# ---------------------------------------------------------------- 16 num_multi_company reason

def test_16_stem_ambiguous_reason_not_no_route():
    """num_multi_company 的 reason 必须是 Stem ambiguous，不能和「查无」混成一条。"""
    records = [
        _rec("CQWLJT261205001-VXN", "保时达", "WB943", train_type="专列"),
        _rec("CQWLJT261205001-MA", "港九港铁", "WB1055", train_type="散舱"),
    ]
    row = _row(EmailType.RETBOX, code="CQWLJT261205001-XXX", num="261205001")
    r = routing.route_retbox(row, records)
    assert r.route_source == "num_multi_company"

    d = decide_release(row, r, records)
    assert d.tier == "R0"
    assert d.action == "alarm"
    assert "Stem ambiguous" in d.reason
    assert "No route" not in d.reason


# ------------------------------------------------- 17~18 放箱令/还箱令：箱号组合复核

def test_17_release_code_hit_but_box_mismatch_fails():
    """放箱令：客编精确命中，但邮件箱号与该记录不一致 ⇒ success=False（判据 6）。"""
    records = [_rec("CQWLJT261108051-VXN", "保时达", "WB943", box="XHCU1111111")]
    row = _row(EmailType.RELEASE, code="CQWLJT261108051-VXN", num="261108051",
               box="XHCU9999999")
    r = routing.route_release(row, records)

    assert r.success is False
    assert r.company == ""
    assert r.route_source == "no_route"


def test_18_retbox_code_hit_but_box_mismatch_fails():
    """还箱令：同上，序号段档有箱号时也必须与箱号同记录命中（判据 6）。"""
    records = [_rec("CQWLJT261108051-VXN", "保时达", "WB943", box="XHCU1111111")]
    # 序号段命中但后缀不同 ⇒ 走 ② 档；箱号不一致 ⇒ 失败
    row = _row(EmailType.RETBOX, code="CQWLJT261108051-Kol", num="261108051",
               box="XHCU9999999")
    r = routing.route_retbox(row, records)

    assert r.success is False
    assert r.company == ""
    assert r.route_source == "no_route"


def test_19_release_code_and_box_both_match_succeeds():
    """正例：客编+箱号同记录命中 ⇒ 照常路由（组合复核不是一律拒）。"""
    records = [_rec("CQWLJT261108051-VXN", "保时达", "WB943", box="XHCU1111111")]
    row = _row(EmailType.RELEASE, code="CQWLJT261108051-VXN", num="261108051",
               box="XHCU1111111")
    r = routing.route_release(row, records)

    assert r.success is True
    assert r.company == "保时达"
    assert r.route_source == "code_exact"
