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
import json
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
        "bot_config.db": tmp_path / "bot_config.db",
    }
    monkeypatch.setattr(notify, "DAILY_ISSUES_PATH", paths["daily_issues.json"])
    monkeypatch.setattr(notify, "DAILY_DIGEST_PATH", paths["daily_digest.json"])
    monkeypatch.setattr(notify, "DAILY_COUNTERS_PATH", paths["daily_counters.json"])
    monkeypatch.setattr(daily_report, "ARCHIVE_STATS_PATH", paths["archive_stats.json"])
    monkeypatch.setattr(daily_report, "DAILY_REPORT_SENT_PATH", paths["daily_report_sent.json"])
    monkeypatch.setattr(daily_report, "LEGACY_FORWARD_LOG_PATH", paths["legacy_forward_log.db"])
    monkeypatch.setattr(daily_report, "YXO_DB_PATH", str(paths["yxo.db"]))
    # 任务 18：新系统 forward_log（读侧用模块级常量，必须 patch 在 daily_report 上）
    monkeypatch.setattr(daily_report, "BOT_CONFIG_DB_PATH", paths["bot_config.db"])
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


# 新系统 forward_log 的 23 列：基线 18 列原样 + 任务 18 新增的 5 列。
_NEXT_COLS = (
    "forward_id", "message_id", "row_key", "account", "folder", "uid",
    "subject", "sender", "date_hdr", "raw_hex", "to_list", "cc_list",
    "sent_by", "sent_at", "bounced", "company", "checked_at", "check_result",
    "robot", "sent_local", "code", "box", "train_no",
)

_NEXT_DDL = (
    "CREATE TABLE forward_log ("
    "forward_id TEXT PRIMARY KEY, message_id TEXT, row_key TEXT,"
    " account TEXT, folder TEXT, uid INTEGER, subject TEXT, sender TEXT,"
    " date_hdr TEXT, raw_hex TEXT, to_list TEXT, cc_list TEXT,"
    " sent_by TEXT, sent_at TEXT, bounced INTEGER DEFAULT 0,"
    " company TEXT, checked_at TEXT, check_result TEXT,"
    " robot TEXT DEFAULT '', sent_local TEXT DEFAULT '', code TEXT DEFAULT '',"
    " box TEXT DEFAULT '', train_no TEXT DEFAULT '')"
)


def _next_db(path, rows):
    """造新系统 forward_log（23 列）。rows = dict 列表，键取自 _NEXT_COLS。

    `to_list`/`cc_list` 传 list，这里 `json.dumps`（生产写入侧就是 json.dumps）。
    """
    conn = sqlite3.connect(path)
    conn.execute(_NEXT_DDL)
    for r in rows:
        d = {k: r.get(k, "") for k in _NEXT_COLS}
        d["uid"] = int(r.get("uid", 0) or 0)
        d["bounced"] = int(r.get("bounced", 0) or 0)
        d["to_list"] = json.dumps(r.get("to_list", []), ensure_ascii=False)
        d["cc_list"] = json.dumps(r.get("cc_list", []), ensure_ascii=False)
        conn.execute(
            "INSERT INTO forward_log (" + ",".join(_NEXT_COLS) + ") VALUES ("
            + ",".join("?" * len(_NEXT_COLS)) + ")",
            [d[k] for k in _NEXT_COLS],
        )
    conn.commit()
    conn.close()


def _next_db_unmigrated(path):
    """未迁移的老库：只有基线 18 列，**没有 sent_local** —— 用来测读侧容错。"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE forward_log ("
        "forward_id TEXT PRIMARY KEY, message_id TEXT, row_key TEXT,"
        " account TEXT, folder TEXT, uid INTEGER, subject TEXT, sender TEXT,"
        " date_hdr TEXT, raw_hex TEXT, to_list TEXT, cc_list TEXT,"
        " sent_by TEXT, sent_at TEXT, bounced INTEGER DEFAULT 0,"
        " company TEXT, checked_at TEXT, check_result TEXT)"
    )
    conn.execute(
        "INSERT INTO forward_log (forward_id, message_id, company, subject,"
        " sent_at, to_list) VALUES (?,?,?,?,?,?)",
        ("old-1", "msg-old", "东盟", "旧库行", "2026-11-09 03:20:00", "[]"),
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

# ---------------------------------------------------------------- 任务 18：新系统 forward_log

def _row(fid, **kw):
    """新表一行的最小骨架（其余列由 _next_db 补默认值）。"""
    d = {"forward_id": fid, "message_id": "msg-" + fid, "row_key": "0"}
    d.update(kw)
    return d


def test_next_db_only_five_robots(env):
    """判据 2：只有新表有数据（模拟切换后）。

    五类机器人齐全、第二节每类票数 = 该类行数、第五节 time 取自 sent_local[11:16]、
    客编/箱号逐项对上、`发至` 显示 `a@x.com, b@y.com`（不带方括号与引号）。
    """
    _next_db(env["bot_config.db"], [
        _row("n-draft", subject="草单", company="东盟", robot="草单",
             sent_local="2026-11-09 11:20:00", code="CQWLJT261109001-MA",
             box="CICU1000001", to_list=["hgn@cail56.com", "ops@cail56.com"]),
        _row("n-waybill", subject="运单号下发", company="港九港铁", robot="运单号",
             sent_local="2026-11-09 09:12:00", code="CQWLJT261109003-D",
             box="CICU1234567", to_list=["chenkai@atrailimt.com"]),
        _row("n-atb", subject="ATB通知", company="太平洋", robot="ATB",
             sent_local="2026-11-09 10:05:00", code="", box="CICU2222222",
             to_list=["a@x.com"]),
        _row("n-dsk", subject="DSK通知", company="保时达", robot="DSK",
             sent_local="2026-11-09 10:40:00", code="CQWLJT261109009",
             box="CICU3333333", to_list=["b@x.com"]),
        _row("n-tracing", subject="原始运踪主题", company="东盟", robot="运踪",
             sent_local="2026-11-09 11:05:00", train_no="968",
             to_list=["hgn@cail56.com"]),
    ])
    data = daily_report.collect(now=NOON)
    assert data["total"] == 5
    assert data["failed"] == 0
    for robot in ("草单", "运单号", "ATB", "DSK", "运踪"):
        assert data["by_robot"].get(robot) == 1, robot

    body = daily_report.build_body(data)

    # 第二节：每类 1 票（列宽用实现里的 _pad，避免把空格数抄死在测试里）
    for robot in ("草单", "运单号", "ATB", "DSK", "运踪"):
        assert f"  {daily_report._pad(robot, 10)}1 票" in body, robot

    # 第五节：时分 / 箱号 / 客编 / 公司 / 发至 逐项对上
    assert ("  11:20 [草单] 箱CICU1000001 CQWLJT261109001-MA 东盟"
            " 发至 hgn@cail56.com, ops@cail56.com") in body
    assert ("  09:12 [运单号] 箱CICU1234567 CQWLJT261109003-D 港九港铁"
            " 发至 chenkai@atrailimt.com") in body
    assert "  10:05 [ATB] 箱CICU2222222 太平洋 发至 a@x.com" in body
    assert "  10:40 [DSK] 箱CICU3333333 CQWLJT261109009 保时达 发至 b@x.com" in body
    # 运踪行：subject 位显示 班列{train_no}（与 tracing_log 那条同一个格式）
    assert "  11:05 [运踪] 班列968 东盟 发至 hgn@cail56.com" in body

    # 按负责人分组：东盟→杨雅雯（草单+运踪），港九港铁/太平洋→毛骁洋，保时达→冯茜
    assert "【杨雅雯】2 票" in body
    assert "【毛骁洋】2 票" in body
    assert "【冯茜】1 票" in body

    # 发至行不许出现方括号与引号（JSON 残留的典型症状）
    for line in body.splitlines():
        if "发至 " in line:
            tail = line.split("发至 ", 1)[1]
            assert "[" not in tail and "]" not in tail and '"' not in tail, line


def test_three_sources_and_message_id_not_deduped(env):
    """判据 3：三源同时有数据（切换当天）⇒ 总数 = 三者之和；同 message_id 的 3 行 = 3 票。

    专门防「按 message_id 去重」回潮：一封邮件拆 3 家公司就是 3 票。
    """
    _legacy_db(env["legacy_forward_log.db"], [
        ("草单", "2026-11-09", "2026-11-09 08:00:00", "杨雅雯", "东盟",
         "LEGACY-CODE", "CICU0000001", "旧库行", "old@x.com"),
    ])
    _tracing_db(env["yxo.db"], [
        ("968_东盟_080500", "968", "东盟", "<m-tr@x>",
         "发至:trace@x.com | 由:yangyawen@cqtransit.com", "2026-11-09 08:05:00"),
    ])
    _next_db(env["bot_config.db"], [
        _row("n-a", message_id="same-msg", company="东盟", robot="草单",
             sent_local="2026-11-09 09:00:00", code="SPLIT-A", to_list=["a@x.com"]),
        _row("n-b", message_id="same-msg", company="港九港铁", robot="草单",
             sent_local="2026-11-09 09:00:20", code="SPLIT-B", to_list=["b@x.com"]),
        _row("n-c", message_id="same-msg", company="太平洋", robot="草单",
             sent_local="2026-11-09 09:00:40", code="SPLIT-C", to_list=["c@x.com"]),
    ])

    data = daily_report.collect(now=NOON)
    assert data["total"] == 5                       # 1 旧库 + 3 新库 + 1 运踪
    assert len(data["rows"]) == 5
    assert data["by_robot"].get("草单") == 4        # 1 旧库 + 3 新库，没有被压成 2
    assert sum(1 for r in data["rows"] if r["code"].startswith("SPLIT-")) == 3

    body = daily_report.build_body(data)
    for code in ("LEGACY-CODE", "SPLIT-A", "SPLIT-B", "SPLIT-C"):
        assert code in body, code
    assert "班列968" in body
    assert "[每日汇总] 2026-11-09 转发5" in daily_report.build_subject(data)


def test_next_rows_use_sent_local_not_utc(env):
    """判据 4：分天看 sent_local（本地时间）。换成 sent_at（UTC）这条就会挂。"""
    _next_db(env["bot_config.db"], [
        # 本地 11-13 00:30 发出 = UTC 11-12 16:30
        _row("n-mid", company="东盟", robot="草单", sent_local="2026-11-13 00:30:00",
             sent_at="2026-11-12 16:30:00", code="LOCAL-0030", to_list=["a@x.com"]),
        # 本地 11-12 23:30 发出 = UTC 11-12 15:30（截 sent_at 会被误算进 11-12，
        # 反过来本地凌晨那批会被算进前一天 —— 这条只是对照组）
        _row("n-before", company="东盟", robot="草单", sent_local="2026-11-12 23:30:00",
             sent_at="2026-11-12 15:30:00", code="LOCAL-2330", to_list=["a@x.com"]),
    ])
    rows = daily_report._next_forward_rows("2026-11-13")
    assert [r["code"] for r in rows] == ["LOCAL-0030"]
    assert rows[0]["time"] == "00:30"


def test_to_list_normalized_and_failed_counted(env):
    """判据 8：JSON 数组 → `a, b`；空数组 → 空串**且计入「失败 1」**（改造前恒为 0）。"""
    assert daily_report._to_list_text('["a@x.com","b@y.com"]') == "a@x.com, b@y.com"
    assert daily_report._to_list_text("[]") == ""
    assert daily_report._to_list_text("") == ""
    # 旧表逗号串原样（不抛、不改）
    assert daily_report._to_list_text("a@x.com,b@y.com") == "a@x.com,b@y.com"
    # 坏串不抛；JSON null 不许渲染成 "None"
    assert daily_report._to_list_text('["a@x.com", null]') == "a@x.com"
    assert daily_report._to_list_text("[broken") == "[broken"

    _next_db(env["bot_config.db"], [
        _row("n-ok", company="东盟", robot="草单", sent_local="2026-11-09 10:00:00",
             code="OK-CODE", to_list=["a@x.com", "b@y.com"]),
        _row("n-norecip", company="东盟", robot="草单", sent_local="2026-11-09 10:10:00",
             code="NORECIP-CODE", to_list=[]),
    ])
    data = daily_report.collect(now=NOON)
    assert data["total"] == 2
    assert data["failed"] == 1 and data["success"] == 1

    body = daily_report.build_body(data)
    assert "发至 a@x.com, b@y.com" in body
    assert "（成功 1 / 失败 1）" in body
    norecip = next(line for line in body.splitlines() if "NORECIP-CODE" in line)
    assert "发至" not in norecip


def test_next_db_empty_fields(env):
    """判据 6 ①：robot/code/box/train_no 全空的新表行照常渲染，正文不出现 None / []。"""
    _next_db(env["bot_config.db"], [
        _row("n-empty", company="东盟", subject="原始主题",
             sent_local="2026-11-09 10:00:00", to_list=["a@x.com"],
             robot="", code="", box="", train_no=""),
    ])
    data = daily_report.collect(now=NOON)
    assert data["total"] == 1
    body = daily_report.build_body(data)
    assert "None" not in body
    assert "[]" not in body
    # 该行仍在第五节明细里（robot 空 → "?"，非运踪不显示 subject）
    assert "  10:00 [?] 东盟 发至 a@x.com" in body


def test_next_db_unmigrated(env):
    """判据 6 ②：新表未迁移（无 sent_local 列）⇒ 该源返回 []、collect 不抛、总数只算旧库+运踪。"""
    _next_db_unmigrated(env["bot_config.db"])
    _legacy_db(env["legacy_forward_log.db"], [
        ("草单", "2026-11-09", "2026-11-09 08:00:00", "杨雅雯", "东盟",
         "LEGACY-ONLY", "CICU0000001", "旧库行", "old@x.com"),
    ])
    _tracing_db(env["yxo.db"], [
        ("968_东盟_080500", "968", "东盟", "<m-tr@x>",
         "发至:trace@x.com | 由:yangyawen@cqtransit.com", "2026-11-09 08:05:00"),
    ])
    assert daily_report._next_forward_rows("2026-11-09") == []
    data = daily_report.collect(now=NOON)          # 绝不抛
    assert data["total"] == 2
    assert data["by_robot"] == {"草单": 1, "运踪": 1}


def test_dry_returns_full_body(env):
    """判据 6 ③：三个数据源文件都不存在（env 里一个库都没建）⇒ dry 仍返回完整五节正文。

    三个路径由 env 夹具全部指到 tmp_path，所以这里挂的只可能是断言、不是 I/O。
    """
    res = daily_report.send_daily_report(now=NOON, dry=True)
    assert res["reason"] == "dry"
    body = res["body"]
    assert body and body.strip()
    for title in ("■ 一、", "■ 二、", "■ 三、", "■ 四、", "■ 五、"):
        assert title in body, title
    assert res["subject"].startswith("[每日汇总]")   # 半角方括号（正文首行是全角【】）
    assert "转发 0 票" in body


def test_tracing_from_next_db_not_double_counted(env):
    """判据 9：新表 robot='运踪' 的行只出现 1 次；同日 tracing_log 也有 1 行 ⇒ 两行并列、互不吞。"""
    _next_db(env["bot_config.db"], [
        _row("n-tr", company="东盟", robot="运踪", sent_local="2026-11-09 11:05:00",
             train_no="968", subject="原始主题", to_list=["hgn@cail56.com"]),
    ])
    _tracing_db(env["yxo.db"], [
        ("968_东盟_110500", "968", "东盟", "<m1@x>",
         "发至:hgn@cail56.com | 由:yangyawen@cqtransit.com", "2026-11-09 11:05:00"),
    ])
    data = daily_report.collect(now=NOON)
    assert data["total"] == 2
    assert data["by_robot"].get("运踪") == 2
    body = daily_report.build_body(data)
    assert body.count("[运踪] 班列968") == 2
