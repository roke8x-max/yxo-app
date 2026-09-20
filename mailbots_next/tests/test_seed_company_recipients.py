# -*- coding: utf-8 -*-
"""A 组（T2 公司收件人初始化）验收测试：全部用临时库，勿碰真库/生产库。"""
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import scripts.seed_company_recipients as seed_mod
import mailbots_next.core.store as store_mod


COMPANIES_7 = ["东盟", "中欧木业", "保时达", "同程配", "太平洋", "沙坪坝", "港九港铁"]


def _make_source_db(path):
    """仿生产 yxo.db.bot_config：7 家 company 行（含沙坪坝跨两个 bot），JSON 数组文本。"""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE bot_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT, scope TEXT, key TEXT,
            to_addrs TEXT, cc_addrs TEXT, extra TEXT, source TEXT, updated_at TEXT)"""
    )
    rows = []
    for i, name in enumerate(COMPANIES_7):
        rows.append(("shared", "company", name,
                     json.dumps([f"to-{i}@example.test"]),
                     json.dumps([f"cc-{i}@example.test"])))
    # 沙坪坝多一个 bot 的行：合并后 to/cc 取并集
    rows.append(("draft", "company", "沙坪坝",
                 json.dumps(["extra-to@example.test"]), json.dumps([])))
    conn.executemany(
        "INSERT INTO bot_config (bot, scope, key, to_addrs, cc_addrs) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture()
def dbs(tmp_path, monkeypatch):
    src = tmp_path / "yxo_src.db"
    dst = tmp_path / "bot_config.db"
    _make_source_db(str(src))
    monkeypatch.setattr(store_mod, "BOT_CONFIG_DB_PATH", dst)
    store_mod.init_bot_config_db()
    return src, dst


def test_mirror_8_homes_nonempty_to(dbs):
    src, dst = dbs
    summary = seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    assert summary["written"] == 8 and summary["skipped"] == 0
    for name in COMPANIES_7 + ["联运"]:
        to, _cc = store_mod.get_recipients(name)
        assert to, name


def test_lianyun_exact_values(dbs):
    """联运是洋 2026-09-09 确认真值（非占位）：断言精确内容。"""
    src, dst = dbs
    seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    to, cc = store_mod.get_recipients("联运")
    assert to == ["gongqilin@cqjzxly.cn", "yuyanling@cqjzxly.cn",
                  "jjb@cqjzxly.cn", "wanglu@cqjzxly.cn"]
    assert cc == ["3841559246@qq.com"]


def test_merge_union_dedup_keep_order(dbs):
    src, dst = dbs
    seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    to, cc = store_mod.get_recipients("沙坪坝")
    assert to == ["to-5@example.test", "extra-to@example.test"]
    assert cc == ["cc-5@example.test"]


def _dump_rows(dst):
    conn = sqlite3.connect(dst)
    try:
        return conn.execute(
            "SELECT bot, scope, key, to_addrs, cc_addrs, extra FROM bot_config ORDER BY bot, scope, key"
        ).fetchall()
    finally:
        conn.close()


def test_idempotent_second_run_all_skipped(dbs):
    src, dst = dbs
    first = seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    before = _dump_rows(str(dst))
    second = seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    assert second["written"] == 0 and second["skipped"] == 8
    assert _dump_rows(str(dst)) == before  # 逻辑内容完全一致（文件字节可能因页元数据不同）
    assert first["written"] == 8


def test_no_force_never_overwrites_manual_edit(dbs, capsys):
    src, dst = dbs
    seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    conn = sqlite3.connect(dst)
    conn.execute("UPDATE bot_config SET to_addrs=? WHERE bot='all' AND scope='company' AND key='沙坪坝'",
                 (json.dumps(["manual@example.test"]),))
    conn.commit()
    conn.close()
    seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    to, _ = store_mod.get_recipients("沙坪坝")
    assert to == ["manual@example.test"]
    out = capsys.readouterr().out
    assert "manual@example.test" not in out  # 未打印真实手工值？只看跳过计数
    assert "跳过" in out


def test_force_overwrites_and_prints_old(dbs, capsys):
    src, dst = dbs
    seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    conn = sqlite3.connect(dst)
    conn.execute("UPDATE bot_config SET to_addrs=? WHERE bot='all' AND scope='company' AND key='沙坪坝'",
                 (json.dumps(["manual@example.test"]),))
    conn.commit()
    conn.close()
    seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False, force=True)
    to, _ = store_mod.get_recipients("沙坪坝")
    assert to == ["to-5@example.test", "extra-to@example.test"]
    out = capsys.readouterr().out
    assert "m***@example.test" in out  # 覆盖前打印旧值（脱敏）


def test_dry_run_default_writes_nothing(dbs):
    src, dst = dbs
    summary = seed_mod.seed_company_recipients(str(src), str(dst), dry_run=True)
    assert summary["written"] == 0
    conn = sqlite3.connect(dst)
    n = conn.execute("SELECT COUNT(*) FROM bot_config WHERE scope='company'").fetchone()[0]
    conn.close()
    assert n == 0


def test_local_table_has_no_source_col(dbs):
    src, dst = dbs
    seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    conn = sqlite3.connect(dst)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bot_config)").fetchall()]
    conn.close()
    assert "source" not in cols


def test_check_empty_db_logs_error(dbs):
    with patch.object(store_mod, "_log") as mock_log:
        assert store_mod.check_company_recipients_configured() is False
    mock_log.error.assert_called_once()
    assert "seed_company_recipients.py --apply" in mock_log.error.call_args[0][0]


def test_check_with_rows_no_error(dbs):
    src, dst = dbs
    seed_mod.seed_company_recipients(str(src), str(dst), dry_run=False)
    with patch.object(store_mod, "_log") as mock_log:
        assert store_mod.check_company_recipients_configured() is True
    mock_log.error.assert_not_called()


def test_zero_source_rows_warns_before_injecting_lianyun(tmp_path, capsys):
    """源库可读但 scope='company' 为 0 行 ⇒ 必须显式 WARN。

    否则脚本会"看起来成功"地只灌常量「联运」1 家，其余公司收件人全空 ⇒ 邮件判 no_route。
    （不可读走 return {} 提前退出、EXIT=2，是另一条路径，此处专测"可读但为空"。）
    """
    src = tmp_path / "empty_but_readable.db"
    conn = sqlite3.connect(src)
    conn.execute(
        "CREATE TABLE bot_config (id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT, "
        "scope TEXT, key TEXT, to_addrs TEXT, cc_addrs TEXT)"
    )
    conn.commit()
    conn.close()

    summary = seed_mod.seed_company_recipients(
        str(src), str(tmp_path / "dst.db"), dry_run=True)
    out = capsys.readouterr().out
    assert "⚠️" in out and "0 行" in out, out
    # 只剩常量注入的联运 1 家 —— 这正是必须喊出来的原因
    assert summary["total"] == 1
    assert "联运" in out
