# -*- coding: utf-8 -*-
"""任务 16 · 每日一封汇总邮件（`06_EMAIL-DESIGN.md` 是唯一文本真相源）。

覆盖判据：
  1  运踪成功通知不抄送洋（send_forwarded 的 cc_admin）
  4  09:00 digest / 12:00 archive_summary 不再发企微
  5  未分类 / 配置缺失不逐封弹企微，但在邮件第三节列出
  6  每天恰好 1 封、只发洋、无数据也发
  7  6 类来源逐项可见 + 五节结构（非旧文本追加）
  8  待办仍实时发企微，并在邮件第三节汇总
  9  归档 stats 读**当天**，不拿昨天的充今天
"""
import sqlite3
from datetime import datetime
from unittest.mock import Mock

import pytest

from mailbots_next.config import OPS_OWNER_EMAIL
from mailbots_next.core import daily_report, notify


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def env(tmp_path, monkeypatch):
    """把每日邮件用到的 5 个路径全部指到 tmp（互不干扰、绝不碰真实库）。"""
    paths = {
        "daily_issues.json": tmp_path / "daily_issues.json",
        "daily_digest.json": tmp_path / "daily_digest.json",
        "daily_counters.json": tmp_path / "daily_counters.json",
        "archive_stats.json": tmp_path / "archive_stats.json",
        "daily_report_sent.json": tmp_path / "daily_report_sent.json",
        "legacy_forward_log.db": tmp_path / "legacy_forward_log.db",
        "yxo.db": tmp_path / "yxo.db",
    }
    monkeypatch.setattr(notify, "DAILY_ISSUES_PATH", paths["daily_issues.json"])
    monkeypatch.setattr(notify, "DAILY_DIGEST_PATH", paths["daily_digest.json"])
    monkeypatch.setattr(notify, "DAILY_COUNTERS_PATH", paths["daily_counters.json"])
    monkeypatch.setattr(daily_report, "ARCHIVE_STATS_PATH", paths["archive_stats.json"])
    monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_PATH", paths["daily_report_sent.json"])
    monkeypatch.setattr(daily_report, "LEGACY_FORWARD_LOG_PATH", paths["legacy_forward_log.db"])
    monkeypatch.setattr(daily_report, "YXO_DB_PATH", str(paths["yxo.db"]))
    notify.clear_issues()
    return paths


def _legacy_db(path, rows):
    """rows: (robot, day, ts, owner, company, code, box, subject, to_list)"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE forward_log (id INTEGER PRIMARY KEY AUTOINCREMENT, robot TEXT,"
        " day TEXT, ts TEXT, owner TEXT, company TEXT, code TEXT, box TEXT,"
        " subject TEXT, to_list TEXT, sender TEXT, note TEXT, test INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO forward_log (robot,day,ts,owner,company,code,box,subject,"
        "to_list,sender,note,test) VALUES (?,?,?,?,?,?,?,?,?,'s@x.com','',0)",
        rows,
    )
    conn.commit()
    conn.close()


def _tracing_db(path, rows):
    """rows: (log_id, train_no, company, mail_msg_id, forward_detail, log_date)"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tracing_log (log_id TEXT, train_no TEXT, company TEXT,"
        " mail_msg_id TEXT, forward_detail TEXT, log_date TEXT, train_key TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracing_log (log_id,train_no,company,mail_msg_id,"
        "forward_detail,log_date,train_key) VALUES (?,?,?,?,?,?,'')",
        rows,
    )
    conn.commit()
    conn.close()


def _seed_full_day(env):
    """一天里有：2 条旧库转发 + 1 条运踪 + digest 计数 + 归档 stats + 4 类待汇总项。"""
    _legacy_db(env["legacy_forward_log.db"], [
        ("草单", "2026-11-09", "2026-11-09 11:20:00", "杨雅雯", "东盟",
         "CQWLJT261109001-MA", "CICU1000001", "运单草单", "hgn@cail56.com"),
        ("运单号", "2026-11-09", "2026-11-09 09:12:00", "毛骁洋", "港九港铁",
         "CQWLJT261109003-D", "CICU1234567", "运单号下发", "chenkai@atrailimt.com"),
    ])
    _tracing_db(env["yxo.db"], [
        ("968_东盟_110500", "968", "东盟", "<m1@x>",
         "发至:hgn@cail56.com | 由:yangyawen@cqtransit.com", "2026-11-09 11:05:00"),
    ])
    notify.write_digest_snapshot(
        {"post_send_bookkeeping_failed": 1, "send_check_skip_no_id": 2},
        date="2026-11-10",
    )
    daily_report.write_archive_stats(
        {"verified": 45, "deleted": 45, "failed": 0, "recovered_deleted": 0,
         "terminated": 0}, now=datetime(2026, 11, 10, 12, 0, 0),
    )
    notify.record_issue("unclassified", "来自 shidong@yxologistics.com「渝新欧放箱」")
    notify.record_issue("config_missing", "无收件人：公司 太平洋 未配置收件人",
                        code="no_recipients")
    notify.record_issue("pending", "T3 命中客编但箱不符 | 草单 客编CQWLJT261109007 箱FWRU456")
    notify.record_issue("program_error", "Max retries reached for <m9@x>")


def _capture_smtp(monkeypatch):
    sent = []

    def _fake(subject, body, to_list):
        sent.append({"subject": subject, "body": body, "to": list(to_list)})

    monkeypatch.setattr(daily_report, "_smtp_send", _fake)
    return sent


NOON = datetime(2026, 11, 10, 12, 30, 0)


# ---------------------------------------------------------------- 五节结构 / 6 类来源

def test_body_is_five_sections(env):
    data = daily_report.collect(now=NOON)
    body = daily_report.build_body(data)
    for title in ("一、今天一句话（先看这里）", "二、转发概览（粗）",
                  "三、待办与异常（细 · 需你处理的都在这）",
                  "四、发送与归档自检（细 · 只报异常数，正常为 0）",
                  "五、转发明细（细 · 全量，按需看）"):
        assert f"■ {title}" in body, title
    assert body.startswith("【每日汇总】2026-11-09（周一）")
    assert "本邮件每天 12:30 自动发送" in body
    # 不是把新内容追加在旧日报后面：旧日报的两句特征文案都不该出现
    assert "全公司自动转发汇总" not in body
    assert "按同事分组：" not in body


def test_body_covers_six_sources(env):
    _seed_full_day(env)
    data = daily_report.collect(now=NOON)
    body = daily_report.build_body(data)

    # ① 转发日报总表（按负责人 + 逐条明细）
    assert "【杨雅雯】" in body and "【毛骁洋】" in body
    assert "CQWLJT261109001-MA" in body
    assert "按负责人：" in body
    # ② digest 计数（含记账失败）→ 第四节
    assert "发信后记账失败 1 次" in body
    assert "跳过(无匹配键) 2" in body
    # ③ 归档 stats → 第四节
    assert "核过 45 / 已删 45" in body
    # ④ 未分类 / 配置缺失 → 第三节
    assert "未分类邮件 来自 shidong@yxologistics.com" in body
    assert "无收件人：公司 太平洋" in body
    # ⑤ 待办 → 第三节
    assert "【待办 1 条】" in body and "箱FWRU456" in body
    # ⑥ 程序错误 → 第三节
    assert "【程序错误 1 条】" in body
    # 概览与合计
    assert "运踪" in body and "合计" in body


def test_subject_format(env):
    _seed_full_day(env)
    data = daily_report.collect(now=NOON)
    assert daily_report.build_subject(data) == "[每日汇总] 2026-11-09 转发3 异常4"


# ---------------------------------------------------------------- 发送规则

def test_empty_day_still_sends_short_report(env, monkeypatch):
    """0 转发 0 异常也要发（「每天一封」要可预期）。"""
    sent = _capture_smtp(monkeypatch)
    res = daily_report.send_daily_report(now=NOON)
    assert res["sent"] is True and res["reason"] == "sent"
    assert len(sent) == 1
    assert "今天全部正常，无需处理" in sent[0]["body"]
    assert "转发 0 票" in sent[0]["body"]
    assert "■ 五、转发明细" in sent[0]["body"]


def test_only_one_email_per_day(env, monkeypatch):
    sent = _capture_smtp(monkeypatch)
    assert daily_report.send_daily_report(now=NOON)["reason"] == "sent"
    second = daily_report.send_daily_report(now=NOON)
    assert second["sent"] is False and second["reason"] == "already_sent"
    assert len(sent) == 1


def test_recipient_is_ops_owner_only(env, monkeypatch):
    sent = _capture_smtp(monkeypatch)
    daily_report.send_daily_report(now=NOON)
    assert sent[0]["to"] == [OPS_OWNER_EMAIL]


def test_not_due_before_1230(env, monkeypatch):
    sent = _capture_smtp(monkeypatch)
    res = daily_report.send_daily_report(now=datetime(2026, 11, 10, 8, 0, 0))
    assert res["sent"] is False and res["reason"] == "not_due"
    assert sent == []


def test_force_sends_before_1230(env, monkeypatch):
    sent = _capture_smtp(monkeypatch)
    res = daily_report.send_daily_report(now=datetime(2026, 11, 10, 8, 0, 0), force=True)
    assert res["sent"] is True and len(sent) == 1


def test_dry_does_not_send_or_mark(env, monkeypatch):
    sent = _capture_smtp(monkeypatch)
    res = daily_report.send_daily_report(now=NOON, dry=True)
    assert res["reason"] == "dry" and sent == []
    assert daily_report.send_daily_report(now=NOON)["reason"] == "sent"


# ---------------------------------------------------------------- 日期口径

def test_archive_stats_must_be_today(env):
    """昨天的归档 stats 绝不能冒充今天（判据 9）。"""
    daily_report.write_archive_stats({"verified": 9, "deleted": 9},
                                     now=datetime(2026, 11, 9, 12, 0, 0))
    data = daily_report.collect(now=NOON)
    assert data["archive"] == {}
    body = daily_report.build_body(data)
    assert "核过 0 / 已删 0" in body
    assert "归档核对（2026-11-10 12:00）" in body


def test_forward_rows_are_yesterday(env):
    """转发明细取昨天；今天发生的转发不进这封邮件。"""
    _legacy_db(env["legacy_forward_log.db"], [
        ("草单", "2026-11-09", "2026-11-09 11:20:00", "杨雅雯", "东盟",
         "YESTERDAY-CODE", "CICU1000001", "昨天", "a@x.com"),
        ("草单", "2026-11-10", "2026-11-10 09:00:00", "杨雅雯", "东盟",
         "TODAY-CODE", "CICU1000002", "今天", "a@x.com"),
    ])
    data = daily_report.collect(now=NOON)
    body = daily_report.build_body(data)
    assert "YESTERDAY-CODE" in body and "TODAY-CODE" not in body


def test_ro_connect_survives_network_error(env, monkeypatch):
    """数据盘掉线时只读连接必须退化成 None，不能把整封邮件打掉。

    Windows 上网络盘不可达时 `Path.exists()` 抛
    `OSError: [WinError 64] 指定的网络名不再可用`（不是返回 False），
    若让它冒出去，当天那封邮件就发不出来了。这里把 exists 打成恒抛，
    断言两个取数函数都安静返回空列表。
    """
    def _boom(self, *a, **k):
        raise OSError(64, "指定的网络名不再可用")

    monkeypatch.setattr("pathlib.Path.exists", _boom)
    assert daily_report._ro_connect(r"\\10.0.199.184\yxo_data\yxo_app\data\yxo.db") is None
    assert daily_report._legacy_forward_rows("2026-11-09") == []
    assert daily_report._tracing_rows("2026-11-09") == []


def test_tracing_forward_counted(env):
    """运踪不写 forward_log 而写 tracing_log —— 邮件必须能数出来（否则运踪消失）。"""
    _tracing_db(env["yxo.db"], [
        ("968_东盟_110500", "968", "东盟", "<m1@x>",
         "发至:hgn@cail56.com | 由:yangyawen@cqtransit.com", "2026-11-09 11:05:00"),
    ])
    data = daily_report.collect(now=NOON)
    assert data["by_robot"].get("运踪") == 1
    assert data["by_owner"].get("杨雅雯") == 1


# ---------------------------------------------------------------- 企微侧（判据 1 / 4 / 8）

def test_tracing_forwarded_does_not_cc_admin(env):
    """判据 1：运踪成功通知只给负责人，不给洋。"""
    from unittest.mock import patch
    from mailbots_next.config import EmailType
    from mailbots_next.core.extract import ExtractedRow
    from mailbots_next.core.routing import RoutingResult
    import mailbots_next.serve as serve_mod

    processor = serve_mod.MailProcessor.__new__(serve_mod.MailProcessor)
    processor.notifier = Mock()
    processor.records = []
    processor.accounts = {'a@x.com': 'pw'}
    row = ExtractedRow(row_idx=0, email_type=EmailType.TRACING, train_no="1032")
    routing = RoutingResult("港九港铁", "maoxiaoyang@cqtransit.com",
                            ["chenkai@atrailimt.com"], [], "train_hit", True)
    with patch.object(serve_mod, "execute_action", return_value=(True, "forwarded")):
        processor._process_routing_result(
            routing, row, EmailType.TRACING, "t-msg-1", "0:1032", b"raw",
            "a@x.com", "Tracing", 7, "s", "subj", "",
        )
    kwargs = processor.notifier.send_forwarded.call_args.kwargs
    assert kwargs.get("cc_admin") is False


def test_non_tracing_forwarded_still_cc_admin(env):
    """草单/运单/ATB/DSK 保持现状：负责人 + 洋（任务书 1.4）。"""
    from unittest.mock import patch
    from mailbots_next.config import EmailType
    from mailbots_next.core.extract import ExtractedRow
    from mailbots_next.core.routing import RoutingResult
    import mailbots_next.serve as serve_mod

    processor = serve_mod.MailProcessor.__new__(serve_mod.MailProcessor)
    processor.notifier = Mock()
    processor.records = []
    processor.accounts = {'a@x.com': 'pw'}
    row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU1000001")
    routing = RoutingResult("太平洋", "maoxiaoyang@cqtransit.com",
                            ["a@x.com"], [], "full_match", True)
    with patch.object(serve_mod, "execute_action", return_value=(True, "forwarded")):
        processor._process_routing_result(
            routing, row, EmailType.DSK, "d-msg-1", "0:CICU1000001", b"raw",
            "a@x.com", "DSK", 7, "s", "subj", "",
        )
    kwargs = processor.notifier.send_forwarded.call_args.kwargs
    assert kwargs.get("cc_admin") is True


def test_send_forwarded_cc_admin_false_sends_one_recipient():
    n = notify.WeComNotifier.__new__(notify.WeComNotifier)
    n._client = None
    n._notify_by_name = Mock(return_value=(True, "wxwork"))
    assert n.send_forwarded("yangyawen@cqtransit.com", OPS_OWNER_EMAIL,
                            "d", cc_admin=False) is True
    assert n._notify_by_name.call_count == 1
    assert n._notify_by_name.call_args.args[0] == "杨雅雯"


def test_send_forwarded_default_still_two_recipients():
    n = notify.WeComNotifier.__new__(notify.WeComNotifier)
    n._client = None
    n._notify_by_name = Mock(return_value=(True, "wxwork"))
    assert n.send_forwarded("yangyawen@cqtransit.com", OPS_OWNER_EMAIL, "d") is True
    assert n._notify_by_name.call_count == 2


def test_archive_summary_not_sent_to_wecom(env, monkeypatch):
    """判据 4：12:00 归档汇总不再发企微，改为落地 stats。"""
    from mailbots_next.core import archive

    fake = Mock()
    monkeypatch.setattr(archive, "get_notifier", lambda: fake)
    archive._notify_summary({"verified": 3, "deleted": 3, "failed": 1})
    fake.notify.assert_not_called()
    assert daily_report.get_archive_stats(datetime.now().strftime("%Y-%m-%d")).get("verified") == 3


def test_pending_still_notifies_wecom(env):
    """判据 8：待办仍实时发企微给洋，同时进邮件第三节汇总。"""
    from unittest.mock import patch
    from mailbots_next.config import EmailType
    from mailbots_next.core.extract import ExtractedRow
    from mailbots_next.core.routing import RoutingResult
    import mailbots_next.serve as serve_mod

    processor = serve_mod.MailProcessor.__new__(serve_mod.MailProcessor)
    processor.notifier = Mock()
    processor.records = []
    processor.accounts = {'a@x.com': 'pw'}
    row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                       customer_code="CQWLJT261109007", container_no="FWRU456")
    routing = RoutingResult("东盟", "yangyawen@cqtransit.com", ["a@x.com"], [],
                            "full_match", True)
    from mailbots_next.core.decide import Decision
    with patch.object(serve_mod, "decide",
                      return_value=Decision("T3", "pending", "Stem hit but box mismatch")), \
         patch.object(serve_mod, "execute_action", return_value=(True, "pending")):
        processor._process_routing_result(
            routing, row, EmailType.DRAFT, "p-msg-1", "0:CQWLJT261109007", b"raw",
            "a@x.com", "草单", 7, "s", "subj", "",
        )
    processor.notifier.send_pending.assert_called_once()
    items = [i for i in notify.get_issues() if i["kind"] == "pending"]
    assert len(items) == 1 and "FWRU456" in items[0]["detail"]
