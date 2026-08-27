# -*- coding: utf-8 -*-
import os
import sqlite3
from core import events_store as es


def _conn(tmp_path):
    conn = es.connect(str(tmp_path / "events.db"))
    es.ensure_schema(conn)
    return conn


def test_schema_tables_created(tmp_path):
    conn = _conn(tmp_path)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"dedup_global", "waybill_ledger", "draft_seen_seq"} <= names


def test_save_eml_layout(tmp_path):
    raw = b"Message-ID: <abc@x>\r\n\r\nbody"
    p = es.save_eml(str(tmp_path / "repo"), "maoxiaoyang@cqtransit.com",
                    "<abc@x>", raw)
    assert p.endswith(".eml") and os.sep + "2026" + os.sep in p
    assert open(p, "rb").read() == raw
    # 同 msg_id 再存覆盖同一路径（幂等）
    p2 = es.save_eml(str(tmp_path / "repo"), "maoxiaoyang@cqtransit.com", "<abc@x>", raw)
    assert p2 == p


def test_waybill_ledger_pending_then_sent(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(code="CQWLJT260822004-Kol", box="HNKU5117095", waybill="38149366",
              train_no="WB734", depart_at="2026-08-22", company="莫斯科子公司",
              msg_id="<w1@x>")
    assert es.ledger_insert_waybill(conn, **kw) is True      # 识别即留底
    assert es.ledger_insert_waybill(conn, **kw) is False     # 幂等
    assert conn.execute("SELECT forward_status FROM waybill_ledger").fetchone()[0] == "pending"
    assert es.ledger_mark_waybill_sent(conn, "<w1@x>") == 1
    assert conn.execute("SELECT forward_status FROM waybill_ledger").fetchone()[0] == "sent"


def test_seen_seq_first_time_true(tmp_path):
    conn = _conn(tmp_path)
    assert es.seen_seq_add(conn, "260713004", "<d1@x>") is True
    assert es.seen_seq_add(conn, "260713004", "<d2@x>") is False  # 已见→B类依据


def test_archive_before_moves_rows(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("CREATE TABLE tracing_log(log_id TEXT PRIMARY KEY, log_date TEXT)")
    conn.executemany("INSERT INTO tracing_log VALUES(?,?)",
                     [("old1", "2026-01-01"), ("new1", "2099-01-01")])
    moved = es.archive_before(conn, "tracing_log", "log_date", keep_days=90)
    conn.commit()
    assert moved >= 1
    remain = {r[0] for r in conn.execute("SELECT log_id FROM tracing_log")}
    arch = {r[0] for r in conn.execute("SELECT log_id FROM tracing_log_archive")}
    assert "new1" in remain and "old1" in arch
