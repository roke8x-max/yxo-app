# -*- coding: utf-8 -*-
"""draft 处理器：夹具→动作矩阵。fake ctx 注入，绝不真发 SMTP/企微。
处理序九步（spec §5.2/§5.4）逐字落地：
剥前缀→噪音前置→草单件判定（无发件人门槛）→提取→A/B 台账（遇到即写）
→classify_match→T1 转发 / T2·T3·T6·T7 待办 / T0·T4·T5 报警 / C2·内部回复链只记录。
"""
import email
import os
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from types import SimpleNamespace

from core import events_store as es
from core import identity
from core import matching
from core import sending
from core.models import MailEvent, RouteTarget
from processors.draft import ADMIN_MAILBOX, ADMIN_NAME, DraftProcessor

FIX = os.path.join(os.path.dirname(__file__), "..", "testset", "test_fixtures")


def _raw(rel):
    with open(os.path.join(FIX, rel), "rb") as f:
        return f.read()


def _fake_sender_for(company):
    return ("ops-moscow@cqtransit.com", "负责人甲") if "莫斯科" in (company or "") \
        else (None, None)


def _fake_real_name(company):
    return "负责人甲" if "莫斯科" in (company or "") else None


def _make_ctx(rows, monkeypatch):
    """内存 sqlite events 库 + 合成 idx + fake send/pending/alarm/notify 记录器。"""
    conn = es.connect(":memory:")
    es.ensure_schema(conn)
    idx = matching.build_index(rows)
    sent, pendings, alarms, notifies, sent_msgs = [], [], [], [], []

    def _send(m, fr, pw, to, cc=()):
        sent.append((fr, tuple(to)))
        sent_msgs.append(m)
        return sending.SendResult(True, tuple(to))

    def _add_pending(info, raw, test=True, simulated=False):
        pendings.append(info)
        return 1

    def _alarm(names, reason, text):
        alarms.append((tuple(names), reason))

    def _notify(name, text):
        notifies.append((name, text))
        return True

    ctx = SimpleNamespace(
        conn_events=conn, idx=idx,
        accounts={ADMIN_MAILBOX: "pwd"},
        live=False, smtp=("smtp.test", 465),
        resolve=lambda code, box, train_id="": (
            [RouteTarget(company="莫斯科子公司", to=("ops@moscow.example",))], None),
        send=_send, add_pending=_add_pending, alarm=_alarm, notify=_notify,
        release=None,
    )
    ctx.sent = sent
    ctx.sent_msgs = sent_msgs
    ctx.pendings = pendings
    ctx.alarms = alarms
    ctx.notifies = notifies
    if monkeypatch is not None:
        monkeypatch.setattr(identity, "sender_for", _fake_sender_for)
        monkeypatch.setattr(identity, "real_name_of", _fake_real_name)
    return ctx


def _ev(raw_bytes, subject=None, folder="草单运单号", eml_path=""):
    msg = email.message_from_bytes(raw_bytes)
    from email.utils import parseaddr
    sender = parseaddr(msg.get("From", ""))[1]
    return MailEvent("a@cqtransit.com", folder, msg["Message-ID"] or "<x>",
                     "1", subject if subject is not None else str(msg["Subject"] or ""),
                     sender, "", eml_path)


_ROWS = [{"code": "CQWLJT260713004-BLLST", "box": "TSRU8008478",
          "company": "莫斯科子公司", "status": "", "deleted": 0}]


def _proc_run(rel, rows, monkeypatch):
    raw = _raw(rel)
    ev = _ev(raw, eml_path=os.path.join(FIX, rel))
    ctx = _make_ctx(rows, monkeypatch)
    res = DraftProcessor(ctx).process(ev)
    return ev, ctx, res


# ---------- 骨架两例 ----------

def test_D01_full_forwards(monkeypatch):
    ev, ctx, res = _proc_run("draft/D01_A_full.eml", _ROWS, monkeypatch)
    assert res.action == "forward"
    assert len(ctx.sent_msgs) == 1 and not ctx.pendings and not ctx.alarms
    # A/B 台账「遇到即写」：首见 260713004 已入 draft_seen_seq
    row = ctx.conn_events.execute(
        "SELECT seq FROM draft_seen_seq WHERE seq='260713004'").fetchone()
    assert row is not None
    # TEST 门控：主题用 test_subject() 自设（build_forward 不设 A 类主题）
    subj = str(ctx.sent_msgs[0]["Subject"])
    assert "[测试·【转草单】→原收件人:ops@moscow.example]" in subj.rstrip()
    # 身份回退：映射邮箱不在 accounts → 回退 ADMIN 账号发送
    assert ctx.sent[0][0] == ADMIN_MAILBOX
    assert res.route and res.route[0].company == "莫斯科子公司"


def test_D07_cancelled_only_alarms(monkeypatch):
    rows = [dict(_ROWS[0]),
            {"code": "CQWLJT260713006-VXN", "box": "", "company": "",
             "status": "退舱", "deleted": 0}]
    ev, ctx, res = _proc_run("draft/D07_tc_excluded.eml", rows, monkeypatch)
    assert res.action == "alarm" and res.tier == "T4"


# ---------- 场景参数化展开（夹具→期望动作一一对应） ----------

def test_D03_update_seen_seq_is_B_forward(monkeypatch):
    # 预写台账：同数字段此前已转过草单 → B 类更新
    rows = [{"code": "CQWLJT260709001-Kol", "box": "OVLU2507254",
             "company": "科洛姆纳子公司", "status": "", "deleted": 0}]
    raw = _raw("draft/D03_B_update.eml")
    ev = _ev(raw, eml_path=os.path.join(FIX, "draft/D03_B_update.eml"))
    ctx = _make_ctx(rows, monkeypatch)
    assert es.seen_seq_add(ctx.conn_events, "260709001", "<seed>") is True
    res = DraftProcessor(ctx).process(ev)
    assert res.action == "forward"
    assert len(ctx.sent_msgs) == 1
    assert "【草单更新】" in str(ctx.sent_msgs[0]["Subject"])


def test_D04_C1_feedback_forwards_and_notifies(monkeypatch):
    ev, ctx, res = _proc_run("draft/D04_C1_feedback.eml", _ROWS, monkeypatch)
    assert res.action == "forward"
    assert len(ctx.sent_msgs) == 1
    # C1 也通知负责同事本人
    assert ctx.notifies and ctx.notifies[0][0] == "负责人甲"


def test_D05_C2_reply_records(monkeypatch):
    ev, ctx, res = _proc_run("draft/D05_C2_reply.eml", _ROWS, monkeypatch)
    assert res.action == "record"
    assert not ctx.sent_msgs and not ctx.pendings and not ctx.alarms


def test_D09_T2_suffix_pendings(monkeypatch):
    rows = [{"code": "CQWLJT260810001-VXN", "box": "ABCU1111111",
             "company": "莫斯科子公司", "status": "", "deleted": 0}]
    ev, ctx, res = _proc_run("draft/D09_T2_suffix.eml", rows, monkeypatch)
    assert res.action == "pending" and res.tier == "T2"
    assert len(ctx.pendings) == 1 and not ctx.sent_msgs
    info = ctx.pendings[0]
    for k in ("message_id", "subject", "sender", "date", "category", "code",
              "num", "box", "company", "owner", "reason", "candidates",
              "boxes_seen", "to", "cc"):
        assert k in info, k
    assert info["owner"] == "负责人甲"
    assert "完整客编不同" in info["reason"]
    assert info["candidates"]


def test_D10_T3_box_pendings(monkeypatch):
    rows = [{"code": "CQWLJT260810002-KOL", "box": "ABCU8888888",
             "company": "莫斯科子公司", "status": "", "deleted": 0}]
    ev, ctx, res = _proc_run("draft/D10_T3_box.eml", rows, monkeypatch)
    assert res.action == "pending" and res.tier == "T3"
    assert ctx.pendings[0]["owner"] == "负责人甲"
    assert "箱号" in ctx.pendings[0]["reason"]


def test_D11_T4_unknown_alarms(monkeypatch):
    ev, ctx, res = _proc_run("draft/D11_T4_unknown.eml", _ROWS, monkeypatch)
    assert res.action == "alarm" and res.tier == "T4"
    assert ctx.alarms == [((ADMIN_NAME,), "T4")]
    assert not ctx.pendings and not ctx.sent_msgs


def test_D12_T5_box_only_alarms(monkeypatch):
    rows = [{"code": "CQWLJT260810004-AAA", "box": "ABCU4444444",
             "company": "莫斯科子公司", "status": "", "deleted": 0}]
    ev, ctx, res = _proc_run("draft/D12_T5_box_only.eml", rows, monkeypatch)
    assert res.action == "alarm" and res.tier == "T5"
    assert ctx.alarms[0][0] == ("负责人甲", ADMIN_NAME)


def test_D13_T6_box_reuse_pendings(monkeypatch):
    rows = [{"code": "CQWLJT260810003-AAA", "box": "ABCU5555555",
             "company": "莫斯科子公司", "status": "", "deleted": 0},
            {"code": "CQWLJT260810003-BBB", "box": "ABCU5555555",
             "company": "布拉茨克子公司", "status": "", "deleted": 0}]
    ev, ctx, res = _proc_run("draft/D13_T6_box_reuse.eml", rows, monkeypatch)
    assert res.action == "pending" and res.tier == "T6"
    info = ctx.pendings[0]
    assert len(info["candidates"]) == 2
    assert "复用" in info["reason"]


def test_D14_T7_cross_suffix_pendings(monkeypatch):
    rows = [{"code": "CQWLJT260810005-AAA", "box": "",
             "company": "莫斯科子公司", "status": "", "deleted": 0},
            {"code": "CQWLJT260810005-BBB", "box": "",
             "company": "布拉茨克子公司", "status": "", "deleted": 0}]
    ev, ctx, res = _proc_run("draft/D14_T7_cross_suffix.eml", rows, monkeypatch)
    assert res.action == "pending" and res.tier == "T7"
    assert "后缀" in ctx.pendings[0]["reason"]


# ---------- 处理序补充覆盖（步骤 1 噪音前置 / 判据 2 兜底 / 文件夹门） ----------

def _synth_raw(subject, enc_pdf=False):
    m = MIMEMultipart()
    m["Subject"] = subject
    m["From"] = "liuting@yxologistics.com"
    m["Message-ID"] = "<synth-draft@x>"
    if enc_pdf:
        att = MIMEApplication(b"%PDF-1.4 test-fixture", _subtype="pdf")
        att.add_header("Content-Disposition", "attachment",
                       filename=("utf-8", "",
                                 "TSRU8008478-260711-150827已加密.pdf"))
        m.attach(att)
    return m.as_bytes()


def test_noise_hits_record_even_if_draft_looking(monkeypatch):
    """处理序第 3 步噪音清单前置：即使附件是标准加密草单 PDF，主题命中也只记录。"""
    raw = _synth_raw(
        "订舱草单_CQWLJT260713004-BLLST_TSRU8008478_报关单", enc_pdf=True)
    ev, ctx = _ev(raw), _make_ctx(_ROWS, monkeypatch)
    res = DraftProcessor(ctx).process(ev)
    assert res.action == "record" and res.detail == "noise"
    assert not ctx.sent_msgs and not ctx.pendings and not ctx.alarms


def test_D16_criterion2_box_pdf_with_update_keyword_forwards(monkeypatch):
    """判据 2 兜底：箱号.pdf + 正文含更新关键词 → 仍识别为草单件。"""
    ev, ctx, res = _proc_run("draft/D16_draft_criterion2.eml", _ROWS, monkeypatch)
    assert res.action == "forward" and res.tier == "T1"
    assert len(ctx.sent_msgs) == 1


def test_can_handle_folder_gate_and_waybill_whitelist():
    proc = DraftProcessor(SimpleNamespace())
    ok = MailEvent("a@cqtransit.com", "&j9BTVYNJU1U-", "<a>", "1",
                   "订舱草单_X", "f@d", "", "")
    ok2 = MailEvent("a@cqtransit.com", "运单草单", "<b>", "1",
                    "Re: 订舱草单_Y", "f@d", "", "")
    waybill = MailEvent("a@cqtransit.com", "草单运单号", "<c>", "1",
                        "2026-08-07 YXO-2026-781 CQWLJT运单号", "f@d", "", "")
    other_folder = MailEvent("a@cqtransit.com", "收件箱", "<d>", "1",
                             "订舱草单_Z", "f@d", "", "")
    assert proc.can_handle(ok) is True
    assert proc.can_handle(ok2) is True
    assert proc.can_handle(waybill) is False      # 运单号白名单件让位 waybill 处理器
    assert proc.can_handle(other_folder) is False
