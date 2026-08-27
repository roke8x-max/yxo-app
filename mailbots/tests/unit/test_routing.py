# -*- coding: utf-8 -*-
"""路由测试：合成 yxo.db（records+bot_config 最小 schema），专列/散舱同链路 + 空路由报警原因。"""
import json
import sqlite3
import pytest
from core import matching, routing


@pytest.fixture()
def yxo(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "yxo.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE records(客户编码 TEXT, 箱号 TEXT, 开票子公司名称 TEXT,
                           班列号 TEXT, 状态 TEXT, is_deleted INTEGER);
      CREATE TABLE bot_config(bot TEXT, scope TEXT, key TEXT,
                              to_addrs TEXT, cc_addrs TEXT, extra TEXT);
      CREATE TABLE meta_kv(key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute("INSERT INTO records VALUES('CQWLJT260101001-A','ABCU1111111','港九港铁','','',0)")
    conn.execute("INSERT INTO records VALUES('CQWLJT260102002-B','RBGU4001728','保时达','WB794','',0)")  # 专列
    conn.execute("INSERT INTO bot_config VALUES('tracking','company','港九港铁', ?, ?, NULL)",
                 (json.dumps(["chenkai@atrailimt.com"]), json.dumps(["watch@qq.com"])))
    # 简报夹具漏了保时达的 company 路由：train 用例断言 reason is None 且返回保时达，
    # 与"缺配置跳过+记 reason"语义矛盾，此处补齐（唯一最小修正）。
    conn.execute("INSERT INTO bot_config VALUES('tracking','company','保时达', ?, ?, NULL)",
                 (json.dumps(["ops@baoshida.example"]), json.dumps([])))
    conn.execute("INSERT INTO bot_config VALUES('tracking','train','794',NULL,NULL,?)",
                 (json.dumps({"companies": ["保时达"]}),))
    conn.commit()
    yield conn
    conn.close()


def _idx(yxo_conn):
    rows = [{"code": r["客户编码"], "box": r["箱号"], "company": r["开票子公司名称"],
             "status": r["状态"] or "", "deleted": r["is_deleted"] or 0}
            for r in yxo_conn.execute("SELECT * FROM records")]
    return matching.build_index(rows)


def test_code_t1_routes_to_company_contacts(yxo):
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", code="CQWLJT260101001-A")
    assert reason is None and len(targets) == 1
    assert targets[0].company == "港九港铁"
    assert targets[0].to == ("chenkai@atrailimt.com",)
    assert targets[0].cc == ("watch@qq.com",)


def test_missing_bot_config_reports_reason(yxo):
    yxo.execute("DELETE FROM bot_config WHERE scope='company'")
    yxo.commit()
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", code="CQWLJT260101001-A")
    assert targets == [] and reason.startswith("no_route_config")


def test_train_override_layer_wins(yxo):
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", train_id="794")
    assert reason is None
    assert [t.company for t in targets] == ["保时达"]      # override 层优先于 records 推导


def test_train_records_derivation(yxo):
    yxo.execute("DELETE FROM bot_config WHERE scope='train'")
    yxo.commit()
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", train_id="WB794")      # norm 后一致
    assert [t.company for t in targets] == ["保时达"]


def test_train_unmatched_reason(yxo):
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", train_id="999")
    assert targets == [] and reason == "train_no_companies"


def test_non_t1_not_routable(yxo):
    targets, reason = routing.resolve_recipients(
        yxo, _idx(yxo), "tracking", code="CQWLJT999999-ZZZ")
    assert targets == [] and reason.startswith("tier_not_routable:T4")


def test_ensure_record_indexes_idempotent(yxo):
    routing.ensure_record_indexes(yxo)
    routing.ensure_record_indexes(yxo)                     # 重复执行不抛错
    names = {r[0] for r in yxo.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_records_code" in names


def test_provider_rebuilds_on_version_change(tmp_path):
    """缓存失效机制（spec §4）：meta_kv.data_version 变化 → get() 重建索引，
    新订舱不再被误判 T4。"""
    p = str(tmp_path / "yxo.db")
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE records(客户编码 TEXT, 箱号 TEXT, 开票子公司名称 TEXT,
                           班列号 TEXT, 状态 TEXT, is_deleted INTEGER);
      CREATE TABLE meta_kv(key TEXT PRIMARY KEY, value TEXT);
      INSERT INTO records VALUES('CQWLJT260101001-A','ABCU1111111','港九港铁','','',0);
      INSERT INTO meta_kv VALUES('data_version','1');
    """)
    conn.commit()
    prov = routing.RecordIndexProvider(p, ttl_seconds=0)
    idx1 = prov.get()
    assert idx1.get_active_by_full_code("CQWLJT260101001-A") is not None

    # 模拟 yxo_app 导入新订舱并 bump_version
    conn.execute(
        "INSERT INTO records VALUES('CQWLJT260199999-B','XQBU2222222','保时达','','',0)")
    conn.execute("UPDATE meta_kv SET value='2' WHERE key='data_version'")
    conn.commit()

    idx2 = prov.get()
    assert idx2.get_active_by_full_code("CQWLJT260199999-B") is not None
    assert routing._read_data_version(conn) == "2"
    conn.close()
