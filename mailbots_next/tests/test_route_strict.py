# -*- coding: utf-8 -*-
"""任务 17 · 路由判定收紧（禁止只用箱号）+ DSK 班列号修复 —— 验收夹具。

本文件逐条对应 `02_TASK.md` 第 5 节的判据（1~8），验的是**端到端形态**：
    识别/提取给出 row → route_* 判 → serve._process_routing_result 处置
    → 企微 alarm / 错误队列 / routing_failed 计数 / 有没有真发信。

为什么要单独一个文件：基线里各测试只验「route_* 的返回值」，本任务的关键改动
是「失败一律 success=False 且在 serve 的 not routing.success 分支补报警」，
必须连着 serve 一起验，否则「改了但不报警」和「报警但多发一封 SMTP 降级邮件」
这两个错都发现不了。

🔴 不写生产库、不发真邮件：records 全部是内存构造，SMTP 路径一律打桩，
   企微收件人映射用 `_notify_by_name` 的 Mock（真 notifier 的降级路径也在验）。
"""
import json
from unittest.mock import Mock, patch

import pytest

from mailbots_next.config import (
    BOT_CONFIG_DB_PATH,
    DAILY_COUNTERS_PATH,
    DEDUP_DB_PATH,
    EmailType,
    OPS_OWNER_EMAIL,
    snapshot,
)
from mailbots_next.core import boxmatch, routing
from mailbots_next.core.decide import decide, decide_draft, decide_retbox
from mailbots_next.core.dedup import get_pending_errors, init_db
from mailbots_next.core.extract import ExtractedRow
from mailbots_next.core.notify import (
    WeComNotifier,
    get_counters_snapshot,
    reset_notifier,
)
from mailbots_next.core.store import (
    get_bot_config_connection,
    init_bot_config_db,
    init_forward_log,
    seed_owner_mapping,
)

# 判据里要求 alarm 恰好 1 个收件人（洋）——这里用 config 的常量，不写字面量
OWNER = OPS_OWNER_EMAIL

_RECIPIENTS = {
    "太平洋": ["tp@test.com"],
    "东盟": ["dm@test.com"],
    "同程配": ["tcp@test.com"],
    "保时达": ["bsd@test.com"],
}


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例一套空库：bot_config / dedup / 计数器，避免相互串味。"""
    reset_notifier()
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
    try:
        yield
    finally:
        from mailbots_next.config.provider import invalidate_cache
        invalidate_cache()


def _rec(code, box, company, train=""):
    return {"id": 1, "客户编码": code, "箱号": box, "班列号": train,
            "company": company, "状态": "正常", "is_deleted": 0}


def _proc(records):
    """真 MailProcessor + 真 notifier（只把企微网络与 SMTP 打桩）。"""
    from mailbots_next.serve import MailProcessor

    proc = MailProcessor(snapshot())
    proc.records = records
    proc.accounts = {OWNER: "pw"}
    notifier = WeComNotifier()
    notifier._client = None
    notifier._notify_by_name = Mock(return_value=(True, "wxwork"))
    notifier._fallback_owner_email = Mock(return_value=False)
    # 记录调用但照常执行：既能看到收件人列表，又能验「不发 SMTP 降级邮件」
    notifier.notify = Mock(wraps=notifier.notify)
    proc.notifier = notifier
    return proc, notifier


def _run(proc, routing_result, row, email_type, tag):
    """走 serve 的失败/成功分支；返回 outcome（失败路径恒为 "queued"）。"""
    with patch("mailbots_next.core.act.send_smtp") as mock_smtp, \
         patch("mailbots_next.serve.execute_action") as mock_action:
        mock_action.return_value = (True, "forwarded")
        outcome = proc._process_routing_result(
            routing_result, row, email_type, f"t17-{tag}", f"0:{row.row_idx}", b"raw",
            "maoxiaoyang@cqtransit.com", "INBOX", 7, "s@x.com", "subj", "",
        )
    return outcome, mock_smtp, mock_action


def _alarm_calls(notifier):
    return [c for c in notifier.notify.call_args_list
            if c.args and c.args[0] == "alarm"]


def _failed_count():
    return get_counters_snapshot().get("routing_failed", 0)


# ----------------------------------------------------------------- 判据 1 · DSK 缺键

def test_dsk_no_train_no_alarms_to_owner_only():
    """DSK 有箱号、主题没班列号：不转发 + alarm（只洋）+ 计数 +1。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋", train="WB1")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU1000001")
    r = routing.route_dsk(row, recs)
    assert r.success is False and r.route_source == "no_train_no"

    proc, notifier = _proc(recs)
    # 真实生产路径传的是字符串（`_identify_type` 返回 route["type"].value），
    # 这里刻意用 "dsk" 而不是 EmailType.DSK，验花名册判断对 str 也成立。
    outcome, mock_smtp, mock_action = _run(proc, r, row, "dsk", "dsk-notrain")

    assert outcome == "queued"
    mock_action.assert_not_called()
    mock_smtp.assert_not_called()
    alarms = _alarm_calls(notifier)
    assert len(alarms) == 1
    assert alarms[0].args[1] == [OWNER]                       # 恰好 1 人：洋
    assert "no_train_no" in alarms[0].args[2]                 # 原因可区分
    assert notifier._notify_by_name.call_count == 1
    notifier._fallback_owner_email.assert_not_called()
    assert _failed_count() == 1


def test_dsk_no_box_alarms_and_source_is_no_box():
    """DSK 没有箱号：success=False、route_source=no_box、alarm、计数 +1。"""
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no=None,
                       train_no="WB1")
    r = routing.route_dsk(row, [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋", "WB1")])
    assert r.success is False and r.route_source == "no_box"

    proc, notifier = _proc([])
    outcome, mock_smtp, _ = _run(proc, r, row, EmailType.DSK, "dsk-nobox")
    assert outcome == "queued"
    mock_smtp.assert_not_called()
    assert len(_alarm_calls(notifier)) == 1
    assert _failed_count() == 1


# ----------------------------------------------------------------- 判据 2 · DSK 双键

def test_match_dsk_only_two_levels():
    """静态契约：match_dsk 只可能返回 train_box / miss。"""
    recs = [_rec("", "CICU1000001", "太平洋", "WB1")]
    levels = {
        boxmatch.match_dsk(recs, "CICU1000001", "WB1")[0],
        boxmatch.match_dsk(recs, "CICU1000001", "WB2")[0],   # 箱号命中、班列不同
        boxmatch.match_dsk(recs, "CICU9999999", "WB1")[0],   # 班列命中、箱号不同
        boxmatch.match_dsk(recs, "CICU1000001", "")[0],      # 无班列
    }
    assert levels <= {boxmatch.LV_TRAIN_BOX, boxmatch.LV_MISS}


@pytest.mark.parametrize("train,box", [("WB2", "CICU1000001"), ("WB1", "CICU9999999")])
def test_dsk_single_key_hit_is_not_routed(train, box):
    """只有单边对上（箱号或班列号）⇒ 不转发、alarm、计数 +1。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋", train="WB1")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no=box,
                       train_no=train)
    r = routing.route_dsk(row, recs)
    assert r.success is False and r.route_source == "no_route"

    proc, notifier = _proc(recs)
    outcome, mock_smtp, _ = _run(proc, r, row, EmailType.DSK, f"dsk-single-{train}")
    assert outcome == "queued"
    mock_smtp.assert_not_called()
    assert len(_alarm_calls(notifier)) == 1
    assert _failed_count() == 1


def test_dsk_combo_multi_hit_is_not_routed():
    """双键命中两条（同箱同班列重复行）⇒ 不猜第一条，失败 + alarm。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋", "WB1"),
            _rec("CQWLJT260810002-VXN", "CICU1000001", "东盟", "WB1")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU1000001",
                       train_no="WB1")
    r = routing.route_dsk(row, recs)
    assert r.success is False and r.route_source == "no_route"

    proc, notifier = _proc(recs)
    outcome, _, _ = _run(proc, r, row, EmailType.DSK, "dsk-multi")
    assert outcome == "queued"
    assert len(_alarm_calls(notifier)) == 1
    assert _failed_count() == 1


def test_dsk_combo_unique_still_forwards():
    """正例兜底：双键唯一命中照常转发（收紧不能把正常件也拦掉）。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋", "WB1")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU1000001",
                       train_no="WB1")
    r = routing.route_dsk(row, recs)
    assert r.success is True and r.company == "太平洋"


# ----------------------------------------------------------------- 判据 3/4 · ATB

def test_atb_no_code_with_unique_box_is_not_routed():
    """ATB 无客编 + 箱号在库里唯一命中 ⇒ 仍然失败（by_box 兜底已删）。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.ATB, customer_code=None,
                       container_no="CICU1000001")
    r = routing.route_atb(row, recs)
    assert r.success is False and r.route_source == "no_route"

    proc, notifier = _proc(recs)
    outcome, mock_smtp, mock_action = _run(proc, r, row, EmailType.ATB, "atb-nocode")
    assert outcome == "queued"
    mock_action.assert_not_called()
    mock_smtp.assert_not_called()
    alarms = _alarm_calls(notifier)
    assert len(alarms) == 1 and alarms[0].args[1] == [OWNER]
    notifier._fallback_owner_email.assert_not_called()
    assert _failed_count() == 1


def test_atb_combo_miss_is_not_routed():
    """ATB 有客编有箱号但组合查无：LV_MISS、不退回箱号、不转发。"""
    recs = [_rec("CQWLJT260810009-ZZZ", "CICU1000001", "同程配")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.ATB,
                       customer_code="CQWLJT260810001-SPB", container_no="CICU1000001")
    level, rec, _ = boxmatch.match_atb(recs, row.customer_code, row.container_no)
    assert level == boxmatch.LV_MISS and rec is None

    r = routing.route_atb(row, recs)
    assert r.success is False and r.company == ""
    proc, notifier = _proc(recs)
    outcome, mock_smtp, _ = _run(proc, r, row, EmailType.ATB, "atb-combo-miss")
    assert outcome == "queued"
    mock_smtp.assert_not_called()
    assert len(_alarm_calls(notifier)) == 1
    assert _failed_count() == 1


def test_atb_combo_multi_hit_is_miss():
    """ATB 组合键命中两条 ⇒ LV_MISS（不再多命中取 first）。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋"),
            _rec("CQWLJT260810001-SPB", "CICU1000001", "东盟")]
    level, rec, _ = boxmatch.match_atb(recs, "CQWLJT260810001-SPB", "CICU1000001")
    assert level == boxmatch.LV_MISS and rec is None


def test_atb_code_without_box_forwards_via_code_only():
    """例外条款：邮件没箱号 ⇒ 客编精确命中即可转发，档位是 code_only。"""
    recs = [_rec("CQWLJT260810001-SPB", "", "太平洋")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.ATB,
                       customer_code="CQWLJT260810001-SPB", container_no=None)
    r = routing.route_atb(row, recs)
    assert r.success is True and r.company == "太平洋"
    assert r.route_source == "code_only"
    # 档位必须有显式映射：漏映射会静默变成 "box_match"，排查时分不清
    assert routing._SRC_BY_LEVEL[boxmatch.LV_CODE_ONLY] == "code_only"


# ----------------------------------------------------------------- 判据 5/8 · 草单运单

def test_draft_box_unique_but_no_code_no_num_is_not_routed():
    """草单：客编与序号段都查无，箱号在库里唯一 ⇒ 也不许定公司（防回归核心）。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT299999999-ZZZ", customer_code_num="299999999",
                       container_no="CICU1000001")
    r = routing.route_draft(row, recs)
    assert r.success is False and r.company == "" and r.route_source == "no_route"

    proc, notifier = _proc(recs)
    outcome, mock_smtp, _ = _run(proc, r, row, EmailType.DRAFT, "draft-boxuniq")
    assert outcome == "queued"
    mock_smtp.assert_not_called()
    assert len(_alarm_calls(notifier)) == 1
    assert _failed_count() == 1


def test_draft_code_hit_but_box_mismatch_fails_at_routing():
    """草单：客编命中但邮件箱号对不上 ⇒ routing 层就失败（不进 T1N/T1）。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT260810001-SPB", customer_code_num="260810001",
                       container_no="CICU9999999")
    r = routing.route_draft(row, recs)
    assert r.success is False and r.route_source == "no_route"

    proc, notifier = _proc(recs)
    outcome, mock_smtp, _ = _run(proc, r, row, EmailType.DRAFT, "draft-boxmis")
    assert outcome == "queued"
    mock_smtp.assert_not_called()
    assert len(_alarm_calls(notifier)) == 1
    assert _failed_count() == 1


def test_draft_num_hit_but_box_mismatch_fails_at_routing():
    """草单：序号段唯一命中，但箱号与本记录不一致 ⇒ 失败（不许靠 T3 兜）。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT260810001-XXX", customer_code_num="260810001",
                       container_no="CICU9999999")
    r = routing.route_draft(row, recs)
    assert r.success is False and r.company == ""


def test_draft_num_hit_and_box_match_forwards_t1n():
    """正例：序号段唯一 + 箱号同记录 ⇒ num_match，decide 走 T1N 转发。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT260810001-XXX", customer_code_num="260810001",
                       container_no="CICU1000001")
    r = routing.route_draft(row, recs)
    assert r.success is True and r.route_source == "num_match"
    d = decide_draft(row, r, recs)
    assert (d.tier, d.action) == ("T1N", "forward")


def test_draft_no_box_code_exact_forwards():
    """例外条款（判据 8）：邮件没箱号时，客编精确命中允许单独转发。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT260810001-SPB", customer_code_num="260810001")
    r = routing.route_draft(row, recs)
    assert r.success is True and r.route_source == "full_match"
    assert decide_draft(row, r, recs).action == "forward"


def test_waybill_shares_draft_rules():
    """运单号与草单同引擎：箱号对不上同样在 routing 层失败。"""
    recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋")]
    row = ExtractedRow(row_idx=0, email_type=EmailType.WAYBILL,
                       customer_code="CQWLJT260810001-SPB", customer_code_num="260810001",
                       container_no="CICU9999999")
    r = routing.route_waybill(row, recs)
    assert r.success is False and r.route_source == "no_route"


def test_t2_and_t7_keep_pending():
    """判据 5③/8：T2（同序号段同箱不同后缀）与 T7（同序号段多后缀）仍是 pending。"""
    t2_row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                          customer_code="CQWLJT260810001-VXN", container_no="CICU1000001")
    t2_recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋")]
    no_route = routing.RoutingResult("", None, [], [], "no_route", False)
    d2 = decide_draft(t2_row, no_route, t2_recs)
    assert (d2.tier, d2.action) == ("T2", "pending")

    t7_row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                          customer_code="CQWLJT260810001-XXX")
    t7_recs = [_rec("CQWLJT260810001-SPB", "CICU1000001", "太平洋"),
               _rec("CQWLJT260810001-VXN", "CICU1000002", "东盟")]
    d7 = decide_draft(t7_row, no_route, t7_recs)
    assert (d7.tier, d7.action) == ("T7", "pending")


def test_no_box_only_determination_in_production_code():
    """判据 9 静态契约：业务代码里不许出现「仅以箱号为条件」的判定行。

    只允许箱号出现在**组合键**里（同一记录已由客编 / 序号段 / 班列号筛过），
    因此逐函数检查时只要求：`route_dsk` / `route_atb` / `route_draft` /
    `route_release` / `route_retbox` / `decide_dsk` 内不得有「直接遍历全量
    records 拿箱号比较」的写法（即 `for r in records` 后紧跟箱号条件）。
    """
    import importlib
    import inspect
    import re

    def _src(fn):
        """取函数源码并剥掉 docstring：注释里引用旧写法不算违规。

        ⚠️ 不能用 `src.replace(fn.__doc__, "")`：Python 会把 docstring 的各行
        前导缩进去掉，`__doc__` 与源码里那段文本不逐字节相同。按三引号切片才稳。
        """
        src = inspect.getsource(fn)
        i = src.find('"""')
        if i != -1:
            j = src.find('"""', i + 3)
            if j != -1:
                src = src[:i] + src[j + 3:]
        return src

    # ① 路由层：箱号比较的右侧必须是「邮件箱号」，左侧必须是已筛过的 rec / hits
    for fn in (routing.route_dsk, routing.route_atb, routing.route_draft,
               routing.route_release, routing.route_retbox):
        src = _src(fn)
        # 「for r in records ... 箱号 == 」这种先按箱号筛全表的写法必须为 0
        assert not re.search(r"for r in records[^\n]*箱号", src), fn.__name__

    # ② 判定层：decide_dsk 不再按箱号重扫 records
    decide_mod = importlib.import_module("mailbots_next.core.decide")
    src_dsk = _src(decide_mod.decide_dsk)
    assert 'r["箱号"] ==' not in src_dsk          # 不再按箱号重扫 records
    assert "box_matches" not in src_dsk

    # ③ boxmatch：两条组合键都要求两个字段同时命中
    src_atb = _src(boxmatch.match_atb)
    src_dsk_m = _src(boxmatch.match_dsk)
    assert "_code(r) == code and _box(r) == box" in src_atb
    assert "_box(r) == box and _train(r) == train" in src_dsk_m
    # ④ DSK 只有两档
    assert "LV_TRAIN_BOX" in src_dsk_m and "LV_MISS" in src_dsk_m
    assert "LV_BOX_ONLY" not in src_dsk_m and "LV_MULTI" not in src_dsk_m


# ----------------------------------------------------------------- 判据 6 · 放箱令系列

def test_retbox_has_no_box_only_branch():
    """静态契约：route_retbox 不再有 box_unique / box_reuse，decide_retbox 不再有 B2。"""
    import importlib
    import inspect

    src_route = inspect.getsource(routing.route_retbox)
    assert "box_unique" not in src_route and "box_reuse" not in src_route

    decide_mod = importlib.import_module("mailbots_next.core.decide")
    src_decide = inspect.getsource(decide_mod.decide_retbox)
    assert '"B2"' not in src_decide and "Box-only" not in src_decide


def test_retbox_combination_key_required():
    """还箱令：客编命中但箱号不一致 ⇒ 失败（组合复核）；箱号单独存在也不放行。"""
    recs = [_rec("CQWLJT261015003-DMZ", "XHCU5271865", "同程配")]
    mismatch = ExtractedRow(row_idx=0, email_type=EmailType.RETBOX,
                            customer_code="CQWLJT261015003-DMZ", customer_code_num="261015003",
                            container_no="XHCU9999999")
    assert routing.route_retbox(mismatch, recs).success is False

    box_only = ExtractedRow(row_idx=0, email_type=EmailType.RETBOX,
                            container_no="XHCU5271865")
    r = routing.route_retbox(box_only, recs)
    assert r.success is False and r.company == ""
    assert (decide_retbox(box_only, r, recs).tier,
            decide_retbox(box_only, r, recs).action) == ("B0", "alarm")


def test_release_combination_key_required():
    """放箱令（RELEASE / RELEASE_CANCEL 同函数）：有箱号时须与记录箱号一致。"""
    recs = [_rec("CQWLJT261108051-VXN", "XHCU1111111", "保时达")]
    ok = ExtractedRow(row_idx=0, email_type=EmailType.RELEASE,
                      customer_code="CQWLJT261108051-VXN", customer_code_num="261108051",
                      container_no="XHCU1111111")
    assert routing.route_release(ok, recs).success is True

    bad = ExtractedRow(row_idx=0, email_type=EmailType.RELEASE_CANCEL,
                       customer_code="CQWLJT261108051-VXN", customer_code_num="261108051",
                       container_no="XHCU2222222")
    assert routing.route_release(bad, recs).success is False


# ----------------------------------------------------------------- 报警边界 · 运踪静默

def test_tracing_zero_hit_stays_silent_without_alarm():
    """运踪零命中仍是静默跳过：不新增 alarm、不计数 routing_failed。"""
    row = ExtractedRow(row_idx=0, email_type=EmailType.TRACING, train_no="9999")
    r = routing.RoutingResult("", None, [], [], "no_company_match", False)
    proc, notifier = _proc([])
    outcome, mock_smtp, _ = _run(proc, r, row, EmailType.TRACING, "tracing-zero")
    assert outcome == "skipped"
    mock_smtp.assert_not_called()
    assert _alarm_calls(notifier) == []
    assert _failed_count() == 0
    assert [e for e in get_pending_errors(100) if e["message_id"] == "t17-tracing-zero"] == []


def test_tracing_other_failure_does_not_alarm():
    """非零命中的 TRACING 失败（如无班列号）保持原样：入队但不发 alarm。"""
    row = ExtractedRow(row_idx=0, email_type=EmailType.TRACING, train_no=None)
    r = routing.route_tracing(row, [])
    assert r.success is False
    proc, notifier = _proc([])
    outcome, _, _ = _run(proc, r, row, EmailType.TRACING, "tracing-notrain")
    assert outcome == "queued"
    assert _alarm_calls(notifier) == []
    assert _failed_count() == 1
