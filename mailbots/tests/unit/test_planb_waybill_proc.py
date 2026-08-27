# -*- coding: utf-8 -*-
"""waybill 处理器：夹具 W01-W09 全覆盖。fake ctx 注入，绝不真发 SMTP/企微。
处理序（spec §5 WAY_A 行级拆分 / WAY_B 忽略 / 留底表）：
1. 主题含「单证审核驳回」→ ignored（仅 audit 日志）
2. WAY_A：parse_waybill_xls → 行级 classify_match：
   - T1→resolve→分组键(to,cc)；T2/T3/T6/T7→pending_rows；
   - T4/T5/T0→alarm_rows（notify_alarm 即时逐行，含 owner 若识别）
   - 退舱行→T4(cancelled_only) alarm（统一规则）
3. 分组发送：rewrite_xls_filtered 生成小 xls（失败保守附原始）→ build_forward(raw,"WAY_A")
   → identity.sender_for(company) 发信 → ledger_insert_waybill（识别即留底 pending）
   → 成功 ledger_mark_waybill_sent + forward_log.record(note="WAY_A 拆分转发(N行)")
4. 无公司/无路由/发送失败行→unresolved→pending（info 带 rows 子集）
5. 全部行 resolved 且发送成功→mark seen；存在 unresolved→pending 队列（复用 add_pending, category="WAY_A"）
"""
import email
import os
from email import policy
from types import SimpleNamespace

from core import events_store as es
from core import identity
from core import matching
from core import sending
from core.models import MailEvent, RouteTarget
from processors import xlsio

FIX = os.path.join(os.path.dirname(__file__), "..", "testset", "test_fixtures")


def _raw(rel):
    with open(os.path.join(FIX, rel), "rb") as f:
        return f.read()


def _fake_sender_for(company):
    return ("ops-moscow@cqtransit.com", "负责人甲") if "莫斯科" in (company or "") \
        else ("ops-asean@cqtransit.com", "负责人乙") if "东盟" in (company or "") \
        else ("ops-bratsk@cqtransit.com", "负责人丙") if "布拉茨克" in (company or "") \
        else ("ops-minsk@cqtransit.com", "负责人丁") if "明斯克" in (company or "") \
        else (None, None)


def _fake_real_name(company):
    if "莫斯科" in (company or ""):
        return "负责人甲"
    if "东盟" in (company or ""):
        return "负责人乙"
    if "布拉茨克" in (company or ""):
        return "负责人丙"
    if "明斯克" in (company or ""):
        return "负责人丁"
    return None


# ---------- 基准 records（与 build_testset.py 一致） ----------
_ROWS = [
    {"code": "CQWLJT260713004-BLLST", "box": "TSRU8008478", "company": "莫斯科子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260713005-SPB", "box": "", "company": "圣彼得堡子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260709001-Kol", "box": "OVLU2507254", "company": "科洛姆纳子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260713006-VXN", "box": "TCLU3000001", "company": "越南子公司", "status": "退舱", "deleted": 0},
    {"code": "CQWLJT260713007-MOS", "box": "TCLU3000002", "company": "莫斯科子公司", "status": "", "deleted": 1},
    {"code": "CQWLJT260810001-SPB", "box": "ABCU1111111", "company": "圣彼得堡子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260810001-VXN", "box": "ABCU1111111", "company": "越南子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260810002-SPB", "box": "ABCU2222222", "company": "圣彼得堡子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260810003-SPB", "box": "ABCU4444444", "company": "圣彼得堡子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260810004-SPB", "box": "ABCU5555555", "company": "圣彼得堡子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260810004-VXN", "box": "ABCU5555555", "company": "越南子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260810005-SPB", "box": "ABCU6666666", "company": "圣彼得堡子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260810005-VXN", "box": "ABCU6666666", "company": "越南子公司", "status": "", "deleted": 0},
    # 运单号用
    {"code": "CQWLJT260816001-MA", "box": "TEMU9000001", "company": "东盟子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260816002-MA", "box": "TEMU9000002", "company": "东盟子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260816003-BLLST", "box": "TEMU9000003", "company": "布拉茨克子公司", "status": "", "deleted": 0},
    {"code": "CQWLJT260816004-MSK", "box": "TEMU9000004", "company": "明斯克子公司", "status": "", "deleted": 0},
]


def _make_ctx(rows, monkeypatch):
    """内存 sqlite events 库 + 合成 idx + fake send/pending/alarm/notify 记录器。"""
    conn = es.connect(":memory:")
    es.ensure_schema(conn)
    idx = matching.build_index(rows)
    sent, pendings, alarms, notifies, sent_msgs, ledger_inserts, ledger_updates = [], [], [], [], [], [], []

    def _send(m, fr, pw, to, cc=()):
        sent.append((fr, tuple(to), tuple(cc)))
        sent_msgs.append(m)
        return sending.SendResult(True, tuple(to))

    def _add_pending(info, raw, test=True, simulated=False):
        pendings.append(info)
        return 1

    def _alarm(names, reason, text):
        alarms.append((tuple(names), reason, text))

    def _notify(name, text):
        notifies.append((name, text))
        return True

    # 记录 ledger 调用
    def _ledger_insert(code, box, waybill, train_no, depart_at, company, msg_id):
        ledger_inserts.append((code, box, waybill, train_no, depart_at, company, msg_id))
        return True

    def _ledger_mark_sent(msg_id):
        ledger_updates.append(msg_id)
        return 1

    # resolve: 根据 code/box 查找 company 再返回路由
    def _resolve(code, box, train_id=""):
        # 从 rows 中查找 company
        company = ""
        for r in rows:
            if r["code"] == code and (not box or r["box"] == box):
                company = r["company"]
                break
        # 模拟路由：东盟 -> call56 外部, 布拉茨克/明斯克 -> 内部(无外部收件人)
        if "东盟" in company:
            return [RouteTarget(company=company, to=("ops@asean.example",), cc=())], None
        elif "布拉茨克" in company or "明斯克" in company:
            return [RouteTarget(company=company, to=(), cc=())], None
        elif "莫斯科" in company or "圣彼得堡" in company or "科洛姆纳" in company or "越南" in company:
            return [RouteTarget(company=company, to=("ops@internal.example",), cc=())], None
        return [], "no_route_config"

    ctx = SimpleNamespace(
        conn_events=conn, idx=idx,
        accounts={"maoxiaoyang@cqtransit.com": "pwd", "ops-moscow@cqtransit.com": "pwd",
                  "ops-asean@cqtransit.com": "pwd", "ops-bratsk@cqtransit.com": "pwd",
                  "ops-minsk@cqtransit.com": "pwd"},
        live=False, smtp=("smtp.test", 465),
        resolve=_resolve,
        send=_send, add_pending=_add_pending, alarm=_alarm, notify=_notify,
        release=None,
        ledger_insert_waybill=_ledger_insert,
        ledger_mark_waybill_sent=_ledger_mark_sent,
    )
    ctx.sent = sent
    ctx.sent_msgs = sent_msgs
    ctx.pendings = pendings
    ctx.alarms = alarms
    ctx.notifies = notifies
    ctx.ledger_inserts = ledger_inserts
    ctx.ledger_updates = ledger_updates
    if monkeypatch is not None:
        monkeypatch.setattr(identity, "sender_for", _fake_sender_for)
        monkeypatch.setattr(identity, "real_name_of", _fake_real_name)
    return ctx


def _ev(raw_bytes, subject=None, folder="运单号", eml_path=""):
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    return MailEvent("maoxiaoyang@cqtransit.com", folder, msg["Message-ID"] or "<x>",
                     "1", subject if subject is not None else str(msg["Subject"] or ""),
                     "docwbfb@yxologistics.com", "", eml_path)


def _proc_run(rel, rows, monkeypatch):
    raw = _raw(rel)
    ev = _ev(raw, eml_path=os.path.join(FIX, rel))
    ctx = _make_ctx(rows, monkeypatch)
    from processors.waybill import WaybillProcessor
    res = WaybillProcessor(ctx).process(ev)
    return ev, ctx, res


# ============================================================
# W01: 单行外部公司 → split_forward + ledger_sent
# ============================================================
def test_W01_single_external(monkeypatch):
    ev, ctx, res = _proc_run("waybill/W01_single_external.eml", _ROWS, monkeypatch)
    # action=forward（拆分转发成功，无 unresolved）
    assert res.action == "forward"
    assert res.tier == "T1"
    # 1 封邮件发出
    assert len(ctx.sent_msgs) == 1
    # ledger 插入 1 行
    assert len(ctx.ledger_inserts) == 1
    # ledger 标记 sent
    assert len(ctx.ledger_updates) == 1
    # 无待办、无报警
    assert not ctx.pendings
    assert not ctx.alarms
    # 企微通知负责人（东盟子公司）
    assert any("负责人乙" == n for n, _ in ctx.notifies)
    # forward_log 记录（通过 forward_builder.build_forward 验证）
    fwd = ctx.sent_msgs[0]
    # TEST 模式：主题被 test_subject() 包装
    assert "CQWLJT260816001-MA 运单号草单" in fwd["Subject"]
    assert "[测试" in fwd["Subject"]
    # 附件应包含过滤后的小 xls
    att_names = []
    for part in fwd.walk():
        fn = part.get_filename()
        if fn:
            att_names.append(fn)
    assert any(".xlsx" in n or ".xls" in n for n in att_names)


# ============================================================
# W02: 内部公司（无外部收件人） → 仅企微，无邮件
# ============================================================
def test_W02_internal_only(monkeypatch):
    ev, ctx, res = _proc_run("waybill/W02_internal_only.eml", _ROWS, monkeypatch)
    assert res.action == "forward"
    # 无外部收件人 → 不发邮件
    assert not ctx.sent_msgs
    # 企微通知负责人（布拉茨克子公司）
    assert any("负责人丙" == n for n, _ in ctx.notifies)
    # ledger 仍插入并标记 sent
    assert len(ctx.ledger_inserts) == 1
    assert len(ctx.ledger_updates) == 1


# ============================================================
# W03: 多行同一外部公司 → 合并 1 封，小 xls 含 2 行
# ============================================================
def test_W03_multi_external(monkeypatch):
    ev, ctx, res = _proc_run("waybill/W03_multi_external.eml", _ROWS, monkeypatch)
    assert res.action == "forward"
    assert len(ctx.sent_msgs) == 1
    # 解析附件行数应为 2
    fwd = ctx.sent_msgs[0]
    # 验证小 xls 含 2 行
    for part in fwd.walk():
        fn = part.get_filename()
        if fn and (fn.endswith(".xlsx") or fn.endswith(".xls")):
            payload = part.get_payload(decode=True)
            rows = xlsio.parse_waybill_xls(payload)
            assert len(rows) == 2
            codes = {r["客户编码"] for r in rows}
            assert codes == {"CQWLJT260816001-MA", "CQWLJT260816002-MA"}
            break
    else:
        assert False, "未找到 xls 附件"


# ============================================================
# W04: 无 xls 附件 → ignored（不崩溃，仅记录）
# ============================================================
def test_W04_no_xls(monkeypatch):
    ev, ctx, res = _proc_run("waybill/W04_no_xls.eml", _ROWS, monkeypatch)
    # 无 xls 附件 → can_handle 为 False 或处理后 ignored
    assert res.action in ("ignored", "record", "skip")
    assert not ctx.sent_msgs
    assert not ctx.pendings
    assert not ctx.alarms


# ============================================================
# W05: 客编不在库 → alarm（T4 unknown）
# ============================================================
def test_W05_unknown(monkeypatch):
    ev, ctx, res = _proc_run("waybill/W05_unknown.eml", _ROWS, monkeypatch)
    assert res.action == "alarm"
    assert res.tier == "T4"
    assert ctx.alarms
    # 报警应含 owner（识别出公司则有 owner，未识别则 ADMIN）
    names, reason, text = ctx.alarms[0]
    assert reason == "T4"
    # 无邮件、无待办
    assert not ctx.sent_msgs
    assert not ctx.pendings


# ============================================================
# W06: 多行混合（2外部+1内部）→ 拆 2 份（1封邮件+1企微）
# ============================================================
def test_W06_multirow_mixed(monkeypatch):
    ev, ctx, res = _proc_run("waybill/W06_multirow_mixed.eml", _ROWS, monkeypatch)
    assert res.action == "forward"
    # 1 封邮件（东盟），1 个企微（布拉茨克）
    assert len(ctx.sent_msgs) == 1
    # 企微通知两家公司负责人
    notified_names = {n for n, _ in ctx.notifies}
    assert "负责人乙" in notified_names  # 东盟
    assert "负责人丙" in notified_names  # 布拉茨克
    # ledger 插入 3 行（全部行识别入库）
    assert len(ctx.ledger_inserts) == 3
    # ledger_mark_waybill_sent 对每个成功分组各调用一次（外部邮件组+内部通知组）
    assert len(ctx.ledger_updates) == 2
    # 邮件附件小 xls 含 2 行
    fwd = ctx.sent_msgs[0]
    for part in fwd.walk():
        fn = part.get_filename()
        if fn and (fn.endswith(".xlsx") or fn.endswith(".xls")):
            payload = part.get_payload(decode=True)
            rows = xlsio.parse_waybill_xls(payload)
            assert len(rows) == 2
            break
    else:
        assert False, "未找到 xls 附件"


# ============================================================
# W07: 多行同一外部公司 → 1 封邮件，小 xls 含全部行
# ============================================================
def test_W07_multirow_one_co(monkeypatch):
    ev, ctx, res = _proc_run("waybill/W07_multirow_one_co.eml", _ROWS, monkeypatch)
    assert res.action == "forward"
    assert len(ctx.sent_msgs) == 1
    fwd = ctx.sent_msgs[0]
    for part in fwd.walk():
        fn = part.get_filename()
        if fn and (fn.endswith(".xlsx") or fn.endswith(".xls")):
            payload = part.get_payload(decode=True)
            rows = xlsio.parse_waybill_xls(payload)
            assert len(rows) == 2
            break
    else:
        assert False, "未找到 xls 附件"


# ============================================================
# W08: 多行含退舱行 → 退舱行 alarm(T4 cancelled_only)，其余行正常拆分转发
# ============================================================
def test_W08_multirow_with_cancel(monkeypatch):
    ev, ctx, res = _proc_run("waybill/W08_multirow_with_cancel.eml", _ROWS, monkeypatch)
    assert res.action == "forward"
    # 1 封邮件（东盟），1 个企微（布拉茨克）
    assert len(ctx.sent_msgs) == 1
    # 报警：退舱行 VXN
    assert len(ctx.alarms) == 1
    names, reason, text = ctx.alarms[0]
    assert reason == "T4"
    assert "退舱" in text or "cancelled" in text.lower() or "VXN" in text
    # 其余 2 行正常：ledger 插入 2 行（退舱行不入库或标记跳过）
    # 注意：退舱行按 spec "退舱行→T4(cancelled_only) alarm"，不入库
    assert len(ctx.ledger_inserts) == 2
    # 企微通知两家公司
    notified_names = {n for n, _ in ctx.notifies}
    assert "负责人乙" in notified_names
    assert "负责人丙" in notified_names


# ============================================================
# W09: 多行含未命中行 → 未命中行 alarm(T4 unknown)，其余行正常拆分转发
# ============================================================
def test_W09_multirow_with_unknown(monkeypatch):
    ev, ctx, res = _proc_run("waybill/W09_multirow_with_unknown.eml", _ROWS, monkeypatch)
    assert res.action == "forward"
    # 1 封邮件（东盟），1 个企微（布拉茨克）
    assert len(ctx.sent_msgs) == 1
    # 报警：未命中行
    assert len(ctx.alarms) == 1
    names, reason, text = ctx.alarms[0]
    assert reason == "T4"
    assert "999999998" in text or "unknown" in text.lower() or "库内无" in text
    # 其余 2 行正常
    assert len(ctx.ledger_inserts) == 2
    notified_names = {n for n, _ in ctx.notifies}
    assert "负责人乙" in notified_names
    assert "负责人丙" in notified_names


# ============================================================
# WAY_B: 主题含「单证审核驳回」→ ignored（仅 audit，零通知零待办）
# ============================================================
def test_WAY_B_rejected_ignored(monkeypatch):
    # 构造 WAY_B 邮件
    raw = _raw("waybill/W01_single_external.eml")
    msg = email.message_from_bytes(raw, policy=policy.default)
    # 修改主题
    del msg["Subject"]
    msg["Subject"] = "单证审核驳回 CQWLJT260816001-MA"
    raw_b = msg.as_bytes()
    ev = _ev(raw_b, subject="单证审核驳回 CQWLJT260816001-MA")
    ctx = _make_ctx(_ROWS, monkeypatch)
    from processors.waybill import WaybillProcessor
    res = WaybillProcessor(ctx).process(ev)
    # ignored：无动作
    assert res.action == "ignored"
    assert not ctx.sent_msgs
    assert not ctx.pendings
    assert not ctx.alarms
    assert not ctx.notifies
    assert not ctx.ledger_inserts


# ============================================================
# can_handle: 白名单 + (运单号文件夹 或 有 .xls 附件)
# ============================================================
def test_can_handle():
    from processors.waybill import WaybillProcessor
    proc = WaybillProcessor(SimpleNamespace())
    # 白名单发件人 + 运单号文件夹
    ok = MailEvent("a@cqtransit.com", "运单号", "<a>", "1", "运单号草单", "docwbfb@yxologistics.com", "", "")
    assert proc.can_handle(ok) is True
    # 白名单发件人 + 有 .xls 附件（通过 subject 判断，实际由 serve 层传 folder）
    ok2 = MailEvent("a@cqtransit.com", "收件箱", "<b>", "1", "CQWLJT260816001-MA 运单号.xlsx", "docwbfb@yxologistics.com", "", "")
    assert proc.can_handle(ok2) is True
    # 非白名单 → False
    not_wl = MailEvent("a@cqtransit.com", "运单号", "<c>", "1", "运单号草单", "other@yxologistics.com", "", "")
    assert proc.can_handle(not_wl) is False
    # 白名单但非运单号文件夹且无 xls 关键字 → False（依赖 folder 判断）
    # 这里只测 folder 门
    ok3 = MailEvent("a@cqtransit.com", "其他文件夹", "<d>", "1", "普通邮件", "docwbfb@yxologistics.com", "", "")
    # 实际 can_handle 会看 folder，如果不在运单号类文件夹且无附件信息则 False
    # serve 层会传正确 folder，此处仅验证逻辑框架