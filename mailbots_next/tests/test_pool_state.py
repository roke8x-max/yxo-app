"""任务 20 验收用例：已发永不重发（D1）+ 未发出池自动重判（D2）+ 告警折叠（D3）
+ 遗留对账（G1）+ 人工出口（G4）。逐条对应 05_ACCEPTANCE.md 判据 1–19。

约定
----
· 只碰 conftest 建的临时库；repool 的两份 JSON 状态文件在本文件里 monkeypatch
  到 pytest 的临时目录，**绝不**写进 mailbots_next/data/。
· live 发送一律 patch 掉 SMTP / 标已读 / 归档复制 —— 用例绝不连真服务器。
· 「首次分发」和「重判」都走真实 route + decide（不 mock 它们），只有需要构造
  扇出 / multi_company 的用例才替换 route_row / decide。
"""
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from mailbots_next import serve
from mailbots_next.config import (
    BOT_CONFIG_DB_PATH,
    DAILY_COUNTERS_PATH,
    DEDUP_DB_PATH,
    LOGS_DIR,
    OPS_OWNER_EMAIL,
    EmailType,
    snapshot,
)
from mailbots_next.core import Decision
from mailbots_next.core.daily_report import build_body, collect
from mailbots_next.core.dedup import (
    add_error,
    count_unresolved,
    get_alarm,
    get_alarm_state,
    get_connection as dedup_conn,
    get_status,
    init_db,
    insert_if_absent,
    item_age_days,
    list_unresolved,
    reopen as dedup_reopen,
    set_status,
    try_claim,
)
from mailbots_next.core.extract import ExtractedRow
from mailbots_next.core.routing import RoutingResult
from mailbots_next.core.store import (
    get_bot_config_connection,
    init_bot_config_db,
    init_forward_log,
    seed_owner_mapping,
)

# ---------------------------------------------------------------- 常量与夹具

_CODE = "CQWLJT260810001"
_BOX = "CICU1000001"
_OTHER_BOX = "CICU9999999"
_SENDER = "docwbfb@yxologistics.com"
_RAW = b"Subject: subj\r\nFrom: docwbfb@yxologistics.com\r\n\r\nbody\r\n"

_RECIPIENTS = {
    "太平洋": (["tp@test.com"], ["tp_cc@test.com"]),
    "东盟": (["dm@test.com"], []),
    "港九港铁": (["gj@test.com"], []),
}

# 05_ACCEPTANCE 判据 12 ④：dedup 新增列必须正好这 9 个（任务 22-A1 增 lease_until 租约列）
_DEDUP_NEW_COLS = {
    "status", "last_try_at", "source", "close_reason",
    "account", "folder", "uid", "retry_count", "lease_until",
}


def _seed_recipients():
    conn = get_bot_config_connection()
    try:
        import json

        for company, (to, cc) in _RECIPIENTS.items():
            conn.execute(
                """INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra)
                   VALUES (?, 'company', ?, ?, ?, '{}')""",
                ("all", company, json.dumps(to), json.dumps(cc)),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _pool_env(tmp_path, monkeypatch):
    """每个用例一份干净的 dedup / bot_config / forward_log + 临时 repool 状态文件。"""
    for p in (BOT_CONFIG_DB_PATH, DEDUP_DB_PATH, DAILY_COUNTERS_PATH):
        path = Path(p)
        if path.exists():
            try:
                path.unlink()
            except PermissionError:
                pass
    init_db()
    init_bot_config_db()
    init_forward_log()
    seed_owner_mapping()
    _seed_recipients()

    monkeypatch.setattr(serve, "REPOOL_REQUEST_PATH", tmp_path / "repool_request.json")
    monkeypatch.setattr(serve, "REPOOL_STATE_PATH", tmp_path / "repool_state.json")
    monkeypatch.setenv("MAILBOT_MODE", "live")
    # 副作用一律打桩：标已读 / 归档复制都是真 IMAP 操作
    monkeypatch.setattr(serve, "mark_seen", Mock(return_value=True))
    monkeypatch.setattr(serve, "enqueue_mark_seen", Mock(return_value=True))
    monkeypatch.setattr("mailbots_next.core.archive.copy_after_forward",
                        Mock(return_value=True))
    try:
        yield
    finally:
        from mailbots_next.config.provider import invalidate_cache

        invalidate_cache()
        for p in (BOT_CONFIG_DB_PATH, DEDUP_DB_PATH, DAILY_COUNTERS_PATH):
            path = Path(p)
            if path.exists():
                try:
                    path.unlink()
                except PermissionError:
                    pass
        # 删完**立刻建回空表**：后续模块（test_release_aggregate）的夹具直接
        # DELETE FROM forward_log，假定表已存在；只删文件会让它 21 条全 error。
        init_db()
        init_bot_config_db()
        init_forward_log()


# ---------------------------------------------------------------- 小工具


def _rec(code=_CODE, box=_BOX, company="太平洋", train_no="WB943", train_type=""):
    return {"id": 1, "客户编码": code, "箱号": box, "company": company,
            "状态": "正常", "is_deleted": 0,
            "班列号": train_no, "班列类型": train_type,
            "目的站": "测试站"}


def _rows(*pairs, email_type=EmailType.DRAFT):
    return [ExtractedRow(row_idx=i, email_type=email_type, customer_code=c,
                         customer_code_num=(c or "")[6:15] if c else None,
                         container_no=b)
            for i, (c, b) in enumerate(pairs)]


def _make_proc(records=None, email_type=EmailType.DRAFT):
    proc = serve.MailProcessor(snapshot())
    proc.records = list(records or [])
    proc.accounts = {"t@t.com": "pw-t"}
    proc.notifier = Mock()
    proc._identify_type = Mock(return_value=email_type)
    return proc


def _seed_forward_log(message_id, row_key="", company="", bounced=0,
                      sender="", sent_by=""):
    conn = get_bot_config_connection()
    try:
        conn.execute(
            "INSERT INTO forward_log (forward_id, message_id, row_key, company, bounced, "
            " sent_at, sender, sent_by, to_list, cc_list) "
            "VALUES (?,?,?,?,?, datetime('now'), ?, ?, '[]', '[]')",
            ("fid-" + uuid.uuid4().hex, message_id, row_key, company, bounced,
             sender, sent_by),
        )
        conn.commit()
    finally:
        conn.close()


def _forward_rows(message_id=None):
    conn = get_bot_config_connection()
    try:
        if message_id:
            cur = conn.execute("SELECT * FROM forward_log WHERE message_id = ?",
                               (message_id,))
        else:
            cur = conn.execute("SELECT * FROM forward_log")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _error_rows(message_id):
    conn = dedup_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM error_queue WHERE message_id = ? ORDER BY id", (message_id,))]
    finally:
        conn.close()


def _dedup_rows(message_id=None):
    conn = dedup_conn()
    try:
        if message_id:
            cur = conn.execute(
                "SELECT * FROM dedup WHERE message_id = ? OR message_id LIKE ?",
                (message_id, message_id + "|%"))
        else:
            cur = conn.execute("SELECT * FROM dedup")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _pragma(table):
    conn = dedup_conn()
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    finally:
        conn.close()


def _sqlite_master():
    conn = dedup_conn()
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def _log_text():
    out = []
    for p in sorted(Path(LOGS_DIR).glob("*.log")):
        out.append(p.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(out)


def _alarms(proc):
    return [c for c in proc.notifier.notify.call_args_list
            if c.args and c.args[0] == "alarm"]


def _flush_deferred(proc, mid=None):
    """任务 33：失败告警延后 180s。测试用老化 pending + 到期扫描触发真实告警。"""
    conn = dedup_conn()
    try:
        sql = ("UPDATE alarm_pending SET first_failed_at=datetime('now','-200 seconds') "
               "WHERE status='pending'")
        params = ()
        if mid:
            sql += " AND msg_id=?"
            params = (mid,)
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
    return serve.flush_due_alarms(proc)


def _smtp_ok(msg, sender_email, sender_password, to_list, cc_list=None, mail_from=None):
    return {"success": True, "refused": {}}


def _two_companies():
    """一封拆两家：太平洋（已发）+ 东盟（未发）。"""
    return [
        RoutingResult("太平洋", "maoxiaoyang@cqtransit.com", ["tp@test.com"], [],
                      "code_exact", True),
        RoutingResult("东盟", "yangyawen@cqtransit.com", ["dm@test.com"], [],
                      "code_exact", True),
    ]


def _seed_pool_item(message_id, row_key, *, account="t@t.com", folder="草单",
                    uid=11, stage="routing", error_type="NO_ROUTE",
                    error_detail="no_route", raw=_RAW, subject="subj",
                    sender=_SENDER, days_ago=0):
    """造一件「认领了但没发出去」的池子件（dedup=unresolved + error_queue 带原文）。"""
    assert try_claim(message_id, row_key, account=account, folder=folder, uid=uid)
    add_error(message_id, row_key, stage, error_type, error_detail,
              "eid-" + uuid.uuid4().hex[:8], account=account, folder=folder,
              uid=uid, subject=subject, sender=sender, raw_bytes=raw)
    set_status(message_id, row_key, "unresolved")
    if days_ago:
        conn = dedup_conn()
        try:
            conn.execute(
                "UPDATE dedup SET claimed_at = datetime('now', ?), "
                "  last_try_at = datetime('now', ?) WHERE message_id = ?",
                ("-%d days" % days_ago, "-%d days" % days_ago,
                 "%s|%s" % (message_id, row_key)),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- 判据 1


class TestC01SentNeverResent:
    def test_forward_log_hit_blocks_resend(self):
        """forward_log 已有该发送单元 ⇒ 重放不发信、落 sent、错误条目 resolved、日志有原因。"""
        mid, key = "p20-c01", "0:" + _BOX
        _seed_forward_log(mid, key, "太平洋")
        _seed_pool_item(mid, key, stage="act", error_type="ACTION_FAILED",
                        error_detail="SMTP failed: boom")

        proc = _make_proc([_rec()])
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            serve.sweep_once(proc)

        assert smtp.call_count == 0, "已发出的发送单元不许重发"
        assert get_status(mid, key) == "sent"
        assert [e["status"] for e in _error_rows(mid)] == ["resolved"]
        assert "forward_log hit" in _log_text()


# ---------------------------------------------------------------- 判据 2 / 2b


class TestC02Fanout:
    def test_only_failed_company_resent_then_sent(self):
        """一封拆两家，C1 已发、C2 失败：只重发 C2，两家都齐了才 sent。"""
        mid, key = "p20-c02", "0:" + _BOX
        _seed_forward_log(mid, key, "太平洋")
        _seed_pool_item(mid, key, stage="act", error_type="ACTION_FAILED",
                        error_detail="SMTP failed")

        proc = _make_proc([_rec()])
        with patch.object(serve, "route_row", return_value=_two_companies()), \
                patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            serve.sweep_once(proc)

        assert smtp.call_count == 1, "已发的那家一次都不许再发"
        assert smtp.call_args.args[3] == ["dm@test.com"]
        rows = _forward_rows(mid)
        assert len(rows) == 2
        assert {r["company"] for r in rows} == {"太平洋", "东盟"}
        assert get_status(mid, key) == "sent"

    def test_partial_failure_stays_unresolved(self):
        """C1 已发、C2 重放后仍失败 ⇒ 必须留 unresolved（不得因 C1 成功就标 sent）。"""
        mid, key = "p20-c02b", "0:" + _BOX
        _seed_forward_log(mid, key, "太平洋")
        _seed_pool_item(mid, key, stage="act", error_type="ACTION_FAILED",
                        error_detail="SMTP failed")

        def _send(msg, sender_email, sender_password, to_list, cc_list=None,
                  mail_from=None):
            if "dm@test.com" in to_list:
                return {"success": False, "error": "refused by mx"}
            return {"success": True, "refused": {}}

        proc = _make_proc([_rec()])
        with patch.object(serve, "route_row", return_value=_two_companies()), \
                patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _send
            serve.sweep_once(proc)

        assert smtp.call_count == 1
        assert len(_forward_rows(mid)) == 1, "C1 不许新增 forward_log 行"
        assert get_status(mid, key) == "unresolved"
        assert len(_alarms(proc)) == 1


# ---------------------------------------------------------------- 判据 2c


class TestC02cKeySemantics:
    def test_no_row_key_column_and_joined_key(self):
        cols = _pragma("dedup")
        assert "row_key" not in cols, "拼接键，不许加 row_key 列"
        assert set(cols) - {"message_id", "claimed_at", "claimed_by"} == _DEDUP_NEW_COLS

        mid = "p20-c02c"
        key0, key1 = "0:" + _BOX, "1:" + _OTHER_BOX
        assert try_claim(mid, key0) is True
        assert try_claim(mid, key1) is True
        keys = {r["message_id"] for r in _dedup_rows(mid)}
        assert keys == {"%s|%s" % (mid, key0), "%s|%s" % (mid, key1)}

        items = [i for i in list_unresolved() if i["message_id"] == mid]
        assert len(items) == 2
        for it in items:
            assert tuple(it["key"].split("|", 1)) == (mid, it["row_key"])
            assert it["row_key"] in (key0, key1)


# ---------------------------------------------------------------- 判据 3


class TestC03RepoolForwards:
    def test_repool_forwards_after_records_fixed(self):
        """数据补全后自动发出：一次 send、落 sent、forward_log 新增 1 行、无人工确认。"""
        mid, key = "p20-c03", "0:" + _BOX
        _seed_pool_item(mid, key)

        proc = _make_proc([_rec(box=_BOX)])
        serve.mark_repool_requested()
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            stats = serve.repool_once(proc)

        assert stats["sent"] == 1
        assert smtp.call_count == 1
        assert get_status(mid, key) == "sent"
        assert len(_forward_rows(mid)) == 1
        proc.notifier.send_pending.assert_not_called()


# ---------------------------------------------------------------- 判据 4


class TestC04RepoolStillFails:
    def test_repool_twice_keeps_unresolved_and_folds_alarm(self):
        mid, key = "p20-c04", "0:" + _BOX
        _seed_pool_item(mid, key)

        # 库里箱号对不上 ⇒ route_draft 走 no_route，两次都不会发出去
        proc = _make_proc([_rec(box=_OTHER_BOX)])
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            serve.mark_repool_requested()
            serve.repool_once(proc)
            serve.mark_repool_requested()
            serve.repool_once(proc)

        assert smtp.call_count == 0
        assert get_status(mid, key) == "unresolved"
        # 真的重判过（而不是被 try_claim 挡成 duplicate）—— 否则「池子里的件
        # 根本进不到 _process_row」这种退化也会让本判据变绿（反证 2 就是冲这条）。
        assert (proc._get_last_outcome() or {}).get("queued") == 1
        assert len(_alarms(proc)) == 1, "同 key 第二次只累加计数，不重复发"
        st = get_alarm_state("pool|%s|%s" % (mid, key))
        assert st is not None and st["count"] == 2


# ---------------------------------------------------------------- 判据 5


class TestC05MultiCompanyConsistent:
    def test_multi_then_unique_same_path(self, monkeypatch):
        """首发 multi_company → alarm + unresolved；补全后重判 → send + sent。"""
        mid, key = "p20-c05", "0:" + _BOX
        mail_code = "CQWLJT260810001-ABC"   # 不精确命中，走数字段
        records = [
            _rec(code="CQWLJT260810001-DMZ", box=_BOX, company="太平洋"),
            _rec(code="CQWLJT260810001-XLYJN", box=_BOX, company="东盟"),
        ]
        proc = _make_proc(records, email_type=EmailType.RELEASE)
        # RELEASE 默认停用（TYPE_ROUTES enabled=False）——本判据考的是 route+decide
        # 的一致性，与类型开关无关，这里显式打开。
        monkeypatch.setattr(serve, "is_type_enabled", lambda *a, **k: True)

        real_route, real_decide = serve.route_row, serve.decide
        with patch.object(serve, "route_row", side_effect=real_route) as m_route, \
                patch.object(serve, "decide", side_effect=real_decide) as m_decide, \
                patch.object(serve, "extract_email",
                             return_value=_rows((mail_code, _BOX),
                                                email_type=EmailType.RELEASE)), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            # ① 首次分发：数字段命中两家
            proc.process_email("t@t.com", "放箱令", mid, 21, "subj", _SENDER, "",
                               _RAW)
            assert smtp.call_count == 0
            # 任务 33：首次失败只挂起 pending，不发企微。
            assert len(_alarms(proc)) == 0
            assert (get_alarm(mid, key) or {}).get("status") == "pending"
            assert get_status(mid, key) == "unresolved"
            assert m_route.call_count == 1
            # 🔴 故意不断言 route_source 相同：首次是 num_multi_company，
            #    补全后必然是 num_match，两者本来就不同（05_ACCEPTANCE 判据 5）。

            # ② 补全（只剩一家）后重判
            proc.records = [records[0]]
            serve.mark_repool_requested()
            stats = serve.repool_once(proc)

        assert stats["sent"] == 1
        assert smtp.call_count == 1
        assert get_status(mid, key) == "sent"
        assert (get_alarm(mid, key) or {}).get("status") == "cancelled"
        # 同一入口：两次都过 route_row，第二次还进了 decide（首次 routing 失败进不去）
        assert m_route.call_count == 2
        assert m_decide.call_count == 1


# ---------------------------------------------------------------- 判据 6


class TestC06AlarmNotOnlyInReport:
    def test_alarm_calls_notify_and_record_issue(self):
        proc = _make_proc([])          # 库里查无此编码 ⇒ 路由失败 ⇒ alarm
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch.object(serve, "record_issue") as rec, \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            proc.process_email("t@t.com", "草单", "p20-c06", 31, "subj", _SENDER, "",
                              _RAW)
            # 任务 33：宽限期内不发；到期复查后照常告警到人和当日汇总（不是只进日报）。
            assert len(_alarms(proc)) == 0
            _flush_deferred(proc, "p20-c06")

        assert len(_alarms(proc)) >= 1, "到期告警必须到人"
        assert rec.call_count >= 1, "同时要有当日汇总项"
        assert any(c.args and c.args[0] == "pool_alarm" for c in rec.call_args_list)
        assert smtp.call_count == 0


# ---------------------------------------------------------------- 判据 7


class TestC07StaleAlarm:
    def test_over_two_days_alarm_mentions_days(self):
        mid, key = "p20-c07", "0:" + _BOX
        _seed_pool_item(mid, key, days_ago=3)
        assert item_age_days(mid, key) >= 3

        proc = _make_proc([_rec(box=_OTHER_BOX)])
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            serve.mark_repool_requested()
            serve.repool_once(proc)

        alarms = _alarms(proc)
        assert len(alarms) == 1
        text = str(alarms[0].args[2])
        assert "已在池 3 天" in text, text
        assert "超 2 天未发出" in text, text
        assert get_status(mid, key) == "unresolved", "超期不许被丢弃"


# ---------------------------------------------------------------- 判据 8


class TestC08MailGoneAfterThree:
    def test_three_empty_payloads_then_closed(self):
        mid, key = "p20-c08", "0:" + _BOX
        # 无 error_queue 原文、account 有但 uid=0 ⇒ 两条取原文路径都拿不到
        assert try_claim(mid, key, account="t@t.com", folder="草单", uid=0)
        set_status(mid, key, "unresolved")

        proc = _make_proc([_rec()])
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            serve.mark_repool_requested()
            serve.repool_once(proc)
            assert get_status(mid, key) == "unresolved"
            assert _dedup_rows(mid)[0]["retry_count"] == 1

            serve.mark_repool_requested()
            serve.repool_once(proc)
            assert get_status(mid, key) == "unresolved"
            assert _dedup_rows(mid)[0]["retry_count"] == 2

            serve.mark_repool_requested()
            serve.repool_once(proc)

        row = _dedup_rows(mid)[0]
        assert row["status"] == "closed"
        assert row["close_reason"] == "mail_gone"
        assert smtp.call_count == 0
        assert len(_alarms(proc)) >= 1, "转 closed 也不许静默"


# ---------------------------------------------------------------- 判据 9


class TestC09PoolCli:
    def test_list_close_retry(self, capsys):
        mid, key = "p20-c09", "0:" + _BOX
        _seed_pool_item(mid, key)

        assert serve.pool_cli(["list"]) == 0
        out = capsys.readouterr().out
        assert "未发出池：1 件" in out
        assert mid in out

        assert serve.pool_cli(["close", mid], row_key=key) == 0
        row = _dedup_rows(mid)[0]
        assert row["status"] == "sent"
        assert row["source"] == "manual"

        # retry：closed → reopen → 立刻重判一次（这里让路由失败，只要求发生告警）
        _seed_pool_item("p20-c09b", key)
        set_status("p20-c09b", key, "closed", source="auto", close_reason="manual")
        proc = _make_proc([_rec(box=_OTHER_BOX)])
        with patch.object(serve, "MailProcessor", return_value=proc), \
                patch.object(serve, "extract_email",
                             return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            assert serve.pool_cli(["retry", "p20-c09b"], row_key=key) == 0
        assert len(_alarms(proc)) == 1
        assert count_unresolved() >= 1


# ---------------------------------------------------------------- 判据 10


class TestC10ReconcileLegacy:
    @pytest.fixture()
    def old_dbs(self, tmp_path):
        pq = tmp_path / "pending_queue.db"
        conn = sqlite3.connect(pq)
        conn.execute("CREATE TABLE pending_queue (message_id TEXT, status TEXT, "
                     "subject TEXT, code TEXT, box TEXT)")
        conn.executemany(
            "INSERT INTO pending_queue VALUES (?,?,?,?,?)",
            [("old-pq-confirmed", "confirmed", "确认过", "C1", "B1"),
             ("old-pq-skipped", "skipped", "跳过", "C2", "B2")],
        )
        conn.commit()
        conn.close()

        ledger = tmp_path / "draft_forward_ledger.db"
        conn = sqlite3.connect(ledger)
        conn.execute("CREATE TABLE processed_mails (message_id TEXT, action TEXT, "
                     "subject TEXT, code TEXT, box TEXT)")
        conn.executemany(
            "INSERT INTO processed_mails VALUES (?,?,?,?,?)",
            [("old-auto-split", "auto_forwarded_split", "自动拆", "C3", "B3"),
             ("old-notified", "notified", "只通知", "C4", "B4"),
             ("old-record", "record", "只记录", "C5", "B5"),
             ("old-pending", "pending", "等人工", "C6", "B6")],
        )
        conn.commit()
        conn.close()

        fwd = tmp_path / "forward_log.db"
        conn = sqlite3.connect(fwd)
        conn.execute("CREATE TABLE forward_log (message_id TEXT, subject TEXT, "
                     "code TEXT, box TEXT, test INTEGER)")
        conn.executemany(
            "INSERT INTO forward_log VALUES (?,?,?,?,?)",
            [("old-fwd-1", "发过1", "C7", "B7", 0),
             ("old-fwd-2", "发过2", "C8", "B8", 0)],
        )
        conn.commit()
        conn.close()
        return pq, ledger, fwd

    def _run(self, old_dbs, apply=False):
        import subprocess
        import sys

        script = Path(serve.__file__).resolve().parent.parent / "scripts" / "reconcile_legacy.py"
        cmd = [sys.executable, str(script),
               "--pending-queue", str(old_dbs[0]),
               "--ledger", str(old_dbs[1]),
               "--forward-log", str(old_dbs[2]),
               "--dedup-db", str(DEDUP_DB_PATH)]
        if apply:
            cmd.append("--apply")
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        return out.returncode, (out.stdout or "") + (out.stderr or "")

    def test_dry_run_writes_nothing(self, old_dbs):
        rc, out = self._run(old_dbs)
        assert rc == 0, out
        assert "DRY-RUN" in out
        assert _dedup_rows() == []

    def test_apply_maps_by_action(self, old_dbs):
        # 已存在的键不许被覆盖（先塞一条 unresolved）
        insert_if_absent("old-auto-split", "unresolved", source="manual",
                         claimed_by="test")
        rc, out = self._run(old_dbs, apply=True)
        assert rc == 0, out

        got = {r["message_id"]: r["status"] for r in _dedup_rows()}
        assert got["old-auto-split"] == "unresolved", "已存在的键不许覆盖"
        assert got["old-notified"] == "closed"
        assert got["old-record"] == "closed"
        assert got["old-pending"] == "closed", "旧 pending 是「没发」，绝不能标 sent"
        assert got["old-fwd-1"] == "sent"
        assert got["old-fwd-2"] == "sent"
        assert got["old-pq-confirmed"] == "sent"
        assert got["old-pq-skipped"] == "closed"
        for r in _dedup_rows():
            if r["message_id"] != "old-auto-split":
                assert r["source"] == "legacy_manual"
                assert r["claimed_by"] == "reconcile"

        # ⑤ 输出里含那份 pending 清单（4 列）
        assert "旧 processed_mails action='pending'" in out
        assert "message_id | subject | code | box" in out
        assert "old-pending | 等人工 | C6 | B6" in out

        # ④ 幂等：再跑一次结果不变
        before = {r["message_id"]: r["status"] for r in _dedup_rows()}
        rc2, _ = self._run(old_dbs, apply=True)
        assert rc2 == 0
        assert {r["message_id"]: r["status"] for r in _dedup_rows()} == before

    def test_forward_log_without_message_id_is_skipped(self, tmp_path):
        """实测旧表没有 message_id 列 ⇒ 本源跳过并打印说明（不猜、不静默）。"""
        fwd = tmp_path / "old_forward_log.db"
        conn = sqlite3.connect(fwd)
        conn.execute("CREATE TABLE forward_log (robot TEXT, day TEXT, company TEXT, "
                     "test INTEGER)")
        conn.execute("INSERT INTO forward_log VALUES ('draft','2026-11-13','太平洋',0)")
        conn.commit()
        conn.close()
        rc, out = self._run((tmp_path / "none1.db", tmp_path / "none2.db", fwd))
        assert rc == 0
        assert "无 message_id 列" in out


# ---------------------------------------------------------------- 判据 11


class TestC11RowKey:
    def test_row_key_has_no_customer_code(self):
        from mailbots_next.core.rowkey import serve_row_key

        no_box = ExtractedRow(row_idx=3, email_type=EmailType.DRAFT,
                              customer_code=_CODE, container_no=None)
        assert serve_row_key(no_box) == "3:-"
        with_box = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                                customer_code=_CODE, container_no=" " + _BOX + " ")
        assert serve_row_key(with_box) == "0:" + _BOX
        assert "CQWLJT" not in serve_row_key(with_box)

    def test_new_rows_never_contain_customer_code_in_key(self):
        proc = _make_proc([_rec()])
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            proc.process_email("t@t.com", "草单", "p20-c11", 41, "subj", _SENDER,
                               "", _RAW)

        for row in _forward_rows("p20-c11"):
            assert "CQWLJT" not in (row["row_key"] or "")
        for row in _dedup_rows("p20-c11"):
            mid, _, row_key = row["message_id"].partition("|")
            assert "CQWLJT" not in row_key


# ---------------------------------------------------------------- 判据 12


class TestC12NoRegression:
    def test_purge_old_dedup_has_no_call_site(self):
        root = Path(serve.__file__).resolve().parent
        pat = re.compile(r"purge_old_dedup\s*\(")
        hits = []
        for p in sorted(root.rglob("*.py")):
            if "tests" in p.parts:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                s = line.strip()
                if s.startswith("#") or s.startswith("def "):
                    continue
                if pat.search(s):
                    hits.append("%s:%d: %s" % (p.relative_to(root), i, s))
        assert hits == [], "purge_old_dedup 必须保持零调用点"

    def test_status_values_and_columns(self):
        assert {"alarm_state", "dedup", "error_queue"} <= _sqlite_master()
        assert "retry_count" in _pragma("dedup")
        cols = set(_pragma("dedup"))
        assert cols - {"message_id", "claimed_at", "claimed_by"} == _DEDUP_NEW_COLS

        # 运行后数据抽查：status 只许三值
        _seed_pool_item("p20-c12", "0:" + _BOX)
        assert {r["status"] for r in _dedup_rows()} <= {"sent", "closed", "unresolved"}

    def test_bad_status_rejected(self):
        with pytest.raises(ValueError):
            set_status("p20-c12", "0:" + _BOX, "resent")
        with pytest.raises(ValueError):
            insert_if_absent("p20-c12", "queued")

    def test_daily_report_pool_section(self):
        _seed_pool_item("p20-c12-d", "0:" + _BOX)
        body = build_body(collect())
        assert "未发出池" in body
        # 原有节次与四类计数口径不变
        for title in ("一、", "二、", "三、", "四、", "五、转发明细"):
            assert title in body, title


# ---------------------------------------------------------------- 判据 13


class TestC13TwoTriggers:
    def test_request_marker_created_consumed_and_reruns(self, tmp_path):
        marker = tmp_path / "repool_request.json"
        assert serve.mark_repool_requested() is True
        assert marker.exists()

        proc = _make_proc([])
        with patch.object(serve, "_repool_item", return_value="unresolved") as item:
            serve.repool_once(proc)
            assert not marker.exists(), "脏标记必须被消费掉"
            assert item.call_count == 0, "池子里没有件，不该重判"

        _seed_pool_item("p20-c13", "0:" + _BOX)
        assert serve.mark_repool_requested() is True
        with patch.object(serve, "_repool_item", return_value="unresolved") as item:
            serve.repool_once(proc)
            assert item.call_count == 1

    def test_daily_trigger_only_once_per_day(self):
        proc = _make_proc([])
        _seed_pool_item("p20-c13b", "0:" + _BOX)
        assert serve._daily_repool_due() is True
        with patch.object(serve, "_repool_item", return_value="unresolved") as item:
            serve.repool_once(proc)
            assert item.call_count == 1
            # ③ 同一天第二次：不再执行
            serve.repool_once(proc)
            assert item.call_count == 1
        assert serve._daily_repool_due() is False
        # ④ 跨天才再来
        assert serve._daily_repool_due(datetime.now() + timedelta(days=1)) is True


# ---------------------------------------------------------------- 判据 14


class TestC14OwnerLookup:
    def _fail_route(self, proc, mid, row, sender=_SENDER):
        with patch.object(serve, "extract_email", return_value=[row]), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            proc.process_email("t@t.com", "草单", mid, 51, "subj", sender, "", _RAW)
        # 任务 33：反查收件人落进 pending，到期发出后仍可断言收件人列表。
        _flush_deferred(proc, mid)
        return smtp

    def test_by_train_no(self):
        row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                           customer_code="CQWLJT000000000-ZZZ",
                           container_no="NOBOX", train_no="WB943")
        proc = _make_proc([_rec(company="东盟", train_no="WB943")])
        self._fail_route(proc, "p20-c14a", row)
        alarms = _alarms(proc)
        assert len(alarms) == 1
        recips = alarms[0].args[1]
        assert "yangyawen@cqtransit.com" in recips
        assert OPS_OWNER_EMAIL in recips

    def test_by_sender_history(self):
        _seed_forward_log("old-1", "0:X", "港九港铁", sender=_SENDER)
        _seed_forward_log("old-2", "0:Y", "港九港铁", sender=_SENDER)
        row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                           customer_code="CQWLJT000000000-ZZZ",
                           container_no="NOBOX", train_no="")
        proc = _make_proc([])          # 没有 records ⇒ 班列号反查必空
        self._fail_route(proc, "p20-c14b", row)
        recips = _alarms(proc)[0].args[1]
        assert "maoxiaoyang@cqtransit.com" in recips    # 港九港铁 → 毛骁洋
        assert OPS_OWNER_EMAIL in recips

    def test_box_only_falls_back_to_ops(self):
        """只给箱号（records 里能按箱号找到公司）也必须落洋：禁止按箱号反查。"""
        row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT,
                           customer_code="CQWLJT000000000-ZZZ",
                           container_no=_BOX, train_no="")
        proc = _make_proc([_rec(box=_BOX, company="东盟", train_no="WB943")])
        self._fail_route(proc, "p20-c14c", row, sender="unknown@nowhere.com")
        recips = _alarms(proc)[0].args[1]
        assert recips == [OPS_OWNER_EMAIL]

    def test_no_box_reverse_lookup_in_source(self):
        src = Path(serve.__file__).read_text(encoding="utf-8")
        body = src.split("def _failure_recipients", 1)[1].split("def _pool_round_active", 1)[0]
        assert "箱号" not in body or "禁止" in body
        assert "_owner_by_box" not in src


# ---------------------------------------------------------------- 判据 15


class TestC15SentWithoutForwardLog:
    def test_sent_status_alone_blocks_send(self):
        mid, key = "p20-c15", "0:" + _BOX
        assert try_claim(mid, key)
        set_status(mid, key, "sent", source="manual")
        assert _forward_rows(mid) == []

        proc = _make_proc([_rec()])
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            proc.process_email("t@t.com", "草单", mid, 61, "subj", _SENDER, "", _RAW)

        assert try_claim(mid, key) is False
        assert smtp.call_count == 0
        assert get_status(mid, key) == "sent"
        assert proc._get_last_outcome()["duplicate"] == 1


# ---------------------------------------------------------------- 判据 16


class TestC16AggregationDecoupled:
    def test_forwarded_plus_alarm_is_unresolved(self):
        mid, key = "p20-c16", "0:" + _BOX
        decisions = [Decision("T1", "forward", "ok"),
                     Decision("R0", "alarm", "no recipients")]

        proc = _make_proc([_rec()])
        with patch.object(serve, "route_row", return_value=_two_companies()), \
                patch.object(serve, "decide", side_effect=decisions), \
                patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            proc.process_email("t@t.com", "草单", mid, 71, "subj", _SENDER, "", _RAW)

        # 行返回值仍按优先级算成 forwarded（旧调用方的统计口径不变）
        assert proc._get_last_outcome()["forwarded"] == 1
        # 但 dedup 必须按逐单元聚合 ⇒ 有 alarm 就不许 sent
        assert get_status(mid, key) == "unresolved"


# ---------------------------------------------------------------- 判据 17


class TestC17BareKeyFromReconcile:
    def test_bare_sent_key_blocks_new_row_key(self):
        mid = "p20-c17"
        key = "0:" + _BOX
        assert insert_if_absent(mid, "sent", source="legacy_manual",
                                claimed_by="reconcile") is True

        assert try_claim(mid, key) is False
        proc = _make_proc([_rec()])
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            proc.process_email("t@t.com", "草单", mid, 81, "subj", _SENDER, "", _RAW)

        assert smtp.call_count == 0
        assert proc._get_last_outcome()["duplicate"] == 1
        assert [r["message_id"] for r in _dedup_rows(mid)] == [mid]


# ---------------------------------------------------------------- 判据 18


class TestC18Reopen:
    def test_reopen_closed_then_repool(self):
        mid, key = "p20-c18", "0:" + _BOX
        _seed_pool_item(mid, key)
        set_status(mid, key, "closed", source="auto", close_reason="manual")
        # retry_count 造 2，验证 reopen 会清零
        conn = dedup_conn()
        try:
            conn.execute("UPDATE dedup SET retry_count = 2 WHERE message_id = ?",
                         ("%s|%s" % (mid, key),))
            conn.commit()
        finally:
            conn.close()

        proc = _make_proc([_rec(box=_OTHER_BOX)])
        with patch.object(serve, "MailProcessor", return_value=proc), \
                patch.object(serve, "extract_email",
                             return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            assert serve.pool_cli(["retry", mid], row_key=key) == 0

        row = _dedup_rows(mid)[0]
        assert row["status"] == "unresolved"
        assert row["source"] == "manual"
        assert row["close_reason"] == ""
        assert row["retry_count"] == 0
        assert len(_alarms(proc)) >= 1, "reopen 后必须紧接着重判一次"

    def test_reopen_sent_refused(self):
        mid, key = "p20-c18b", "0:" + _BOX
        assert try_claim(mid, key)
        set_status(mid, key, "sent", source="manual")

        assert dedup_reopen(mid, key, "tester") is False
        assert get_status(mid, key) == "sent"

        proc = _make_proc([_rec()])
        with patch.object(serve, "MailProcessor", return_value=proc), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            rc = serve.pool_cli(["retry", mid], row_key=key)
        assert rc == 1
        assert get_status(mid, key) == "sent"
        assert smtp.call_count == 0

    def test_close_idempotent(self):
        mid, key = "p20-c18c", "0:" + _BOX
        _seed_pool_item(mid, key)
        assert serve.pool_cli(["close", mid], row_key=key) == 0
        assert get_status(mid, key) == "sent"
        assert serve.pool_cli(["close", mid], row_key=key) == 0
        assert get_status(mid, key) == "sent"
        assert len(_dedup_rows(mid)) == 1


# ---------------------------------------------------------------- 判据 19


class TestC19StateSurvivesRestart:
    def test_retry_count_and_alarm_state_persist(self):
        assert "retry_count" in _pragma("dedup")
        assert "alarm_state" in _sqlite_master()

        mid, key = "p20-c19", "0:" + _BOX
        assert try_claim(mid, key, account="t@t.com", folder="草单", uid=0)
        set_status(mid, key, "unresolved")

        proc = _make_proc([])
        with patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            serve.mark_repool_requested()
            serve.repool_once(proc)
            serve.mark_repool_requested()
            serve.repool_once(proc)
            assert _dedup_rows(mid)[0]["retry_count"] == 2

            # 「重启」：清掉内存态的 notifier / 配置缓存，重新构造 processor
            from mailbots_next.core.notify import reset_notifier
            from mailbots_next.config.provider import invalidate_cache

            reset_notifier()
            invalidate_cache()
            proc2 = _make_proc([])

            serve.mark_repool_requested()
            serve.repool_once(proc2)

        row = _dedup_rows(mid)[0]
        assert row["retry_count"] == 3, "重启不许把连续计数清零"
        assert row["status"] == "closed"
        assert row["close_reason"] == "mail_gone"

    def test_alarm_fold_survives_restart(self):
        mid, key = "p20-c19b", "0:" + _BOX
        _seed_pool_item(mid, key)

        proc = _make_proc([_rec(box=_OTHER_BOX)])
        with patch.object(serve, "extract_email", return_value=_rows((_CODE, _BOX))), \
                patch("mailbots_next.core.act.send_smtp") as smtp:
            smtp.side_effect = _smtp_ok
            serve.mark_repool_requested()
            serve.repool_once(proc)
            assert len(_alarms(proc)) == 1

            from mailbots_next.core.notify import reset_notifier

            reset_notifier()
            proc2 = _make_proc([_rec(box=_OTHER_BOX)])
            serve.mark_repool_requested()
            serve.repool_once(proc2)

        assert _alarms(proc2) == [], "同 key 24h 内重启后也不许再发"
        st = get_alarm_state("pool|%s|%s" % (mid, key))
        assert st["count"] == 2

    def test_repool_state_file(self):
        proc = _make_proc([])
        with patch.object(serve, "_repool_item", return_value="unresolved"):
            serve.repool_once(proc)     # 首次：daily 到期 ⇒ 跑并落盘
        state = Path(serve.REPOOL_STATE_PATH)
        assert state.exists()
        import json

        data = json.loads(state.read_text(encoding="utf-8"))
        assert data["last_daily_date"] == datetime.now().strftime("%Y-%m-%d")
        assert serve._daily_repool_due() is False
