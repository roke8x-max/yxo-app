# -*- coding: utf-8 -*-
"""路由种子迁移 CLI：dry-run 不写库；apply 写入且 INSERT OR IGNORE 幂等重跑不重复。
cache(default_map) 与 from-bot(dsk 现有 company 行) 两源各验一遍。直接 exec 模块调用 main()。
"""
import importlib.util
import json
import os
import sqlite3

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts_seed_routing.py")

# 生产同款表结构（init_bot_config.py）：UNIQUE(bot,scope,key) 是 INSERT OR IGNORE 幂等的依据
_DDL = """
CREATE TABLE IF NOT EXISTS bot_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    to_addrs TEXT NOT NULL DEFAULT '[]',
    cc_addrs TEXT NOT NULL DEFAULT '[]',
    extra TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'manual',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(bot, scope, key)
)
"""

_CACHE = {
    "default_map": {
        "港九港铁": {"to": ["chenkai@atrailimt.com"], "cc": ["watch@qq.com"]},
        "保时达": {"to": ["ops@baoshida.example"], "cc": []},
    }
}


def _load_mod(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(tmp_path):
    p = str(tmp_path / "yxo.db")
    conn = sqlite3.connect(p)
    conn.execute(_DDL)
    # 预置 dsk 的 2 条 company 行（draft/waybill 无任何 company 行——迁移闸门背景）
    conn.execute("INSERT INTO bot_config(bot,scope,key,to_addrs,cc_addrs,extra,source)"
                 " VALUES('dsk','company','港九港铁',?,?,?, 'feishu')",
                 (json.dumps(["old@atrailimt.com"]), json.dumps(["watch@qq.com"]),
                  json.dumps({})))
    conn.execute("INSERT INTO bot_config(bot,scope,key,to_addrs,cc_addrs,extra,source)"
                 " VALUES('dsk','company','保时达',?,?,?,'feishu')",
                 (json.dumps(["ops@baoshida.example"]), json.dumps([]), json.dumps({})))
    conn.commit()
    conn.close()
    return p


def _make_cache(tmp_path):
    p = str(tmp_path / "dsk_config_cache.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(_CACHE, f, ensure_ascii=False)
    return p


def _rows(dbp, bot):
    conn = sqlite3.connect(dbp)
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT key, to_addrs FROM bot_config WHERE bot=? AND scope='company'", (bot,))}
    conn.close()
    return rows


def test_dry_run_reports_without_writing(tmp_path):
    dbp = _make_db(tmp_path)
    cache = _make_cache(tmp_path)
    mod = _load_mod("scripts_seed_a")
    out = mod.main(["scripts_seed_routing.py", "--db", dbp,
                    "--from-cache", cache, "--into", "draft,waybill", "--dry-run"])
    assert out["draft"]["new"] == 2 and out["waybill"]["new"] == 2   # 各待写 2 条
    assert _rows(dbp, "draft") == {} and _rows(dbp, "waybill") == {}  # dry-run 不落库


def test_apply_from_cache_then_idempotent_rerun(tmp_path):
    dbp = _make_db(tmp_path)
    cache = _make_cache(tmp_path)
    mod = _load_mod("scripts_seed_b")
    args = ["scripts_seed_routing.py", "--db", dbp,
            "--from-cache", cache, "--into", "draft,waybill"]
    out = mod.main(args + ["--apply"])
    assert out["draft"]["new"] == 2 and out["waybill"]["new"] == 2
    rows = _rows(dbp, "draft")
    assert sorted(rows) == ["保时达", "港九港铁"]
    assert json.loads(rows["港九港铁"]) == ["chenkai@atrailimt.com"]

    out2 = mod.main(args + ["--apply"])                              # 幂等重跑
    assert out2["draft"]["new"] == 0 and out2["waybill"]["new"] == 0
    assert len(_rows(dbp, "waybill")) == 2                           # 不重复


def test_apply_from_bot_dsk_copies_rows_verbatim(tmp_path):
    dbp = _make_db(tmp_path)
    mod = _load_mod("scripts_seed_c")
    args = ["scripts_seed_routing.py", "--db", dbp,
            "--from-bot", "dsk", "--into", "waybill"]
    mod.main(args + ["--dry-run"])
    assert _rows(dbp, "waybill") == {}                               # dry-run 不落库
    out = mod.main(args + ["--apply"])
    assert out["waybill"]["new"] == 2
    dsk, wb = _rows(dbp, "dsk"), _rows(dbp, "waybill")
    assert wb["港九港铁"] == dsk["港九港铁"]                          # 原文照抄
    assert wb["保时达"] == dsk["保时达"]
    out2 = mod.main(args + ["--apply"])                              # 幂等重跑
    assert out2["waybill"]["new"] == 0 and len(_rows(dbp, "waybill")) == 2


def test_missing_default_map_errors(tmp_path):
    dbp = _make_db(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"box_record_map": {}}), encoding="utf-8")
    mod = _load_mod("scripts_seed_d")
    try:
        mod.main(["scripts_seed_routing.py", "--db", dbp,
                  "--from-cache", str(bad), "--into", "draft", "--apply"])
        assert False, "缺 default_map 应报错"
    except SystemExit:
        pass


def _apply_cache(tmp_path, capsys, default_map):
    """用给定 default_map 跑一次 apply，返回 (dbp, report, stdout)。"""
    dbp = _make_db(tmp_path)
    p = tmp_path / "cache_fix.json"
    p.write_text(json.dumps({"default_map": default_map}), encoding="utf-8")
    mod = _load_mod("scripts_seed_fix_{}".format(abs(hash(str(default_map))) % 10**8))
    out = mod.main(["scripts_seed_routing.py", "--db", dbp,
                    "--from-cache", str(p), "--into", "draft", "--apply"])
    return dbp, out, capsys.readouterr().out


def test_malformed_nondict_value_skipped_with_warning(tmp_path, capsys):
    default_map = {"坏公司": "oops",
                   "港九港铁": {"to": ["chenkai@atrailimt.com"], "cc": ["watch@qq.com"]}}
    dbp, out, stdout = _apply_cache(tmp_path, capsys, default_map)
    assert _rows(dbp, "draft") == {"港九港铁": json.dumps(["chenkai@atrailimt.com"])}
    assert out["draft"]["new"] == 1
    assert "警告" in stdout and "坏公司" in stdout


def test_malformed_str_to_skipped_valid_neighbor_kept(tmp_path, capsys):
    default_map = {"裸串公司": {"to": "ops@x.com", "cc": []},
                   "港九港铁": {"to": ["chenkai@atrailimt.com"], "cc": ["watch@qq.com"]}}
    dbp, out, stdout = _apply_cache(tmp_path, capsys, default_map)
    rows = _rows(dbp, "draft")
    assert "裸串公司" not in rows and "港九港铁" in rows   # 只跳过畸形键
    assert out["draft"]["new"] == 1 and "警告" in stdout and "裸串公司" in stdout


def test_empty_recipients_dead_row_skipped_with_warning(tmp_path, capsys):
    default_map = {"空壳公司": {"to": [], "cc": []},
                   "港九港铁": {"to": ["chenkai@atrailimt.com"], "cc": ["watch@qq.com"]}}
    dbp, out, stdout = _apply_cache(tmp_path, capsys, default_map)
    assert _rows(dbp, "draft") == {"港九港铁": json.dumps(["chenkai@atrailimt.com"])}
    assert out["draft"]["new"] == 1
    assert "警告" in stdout and "空壳公司" in stdout


def test_all_malformed_source_hard_fails_after_warnings(tmp_path, capsys):
    """全部畸形 → 逐条警告后硬失败：闸门不允许在零有效源时静默通过。"""
    try:
        _apply_cache(tmp_path, capsys, {"坏公司": "oops"})
        assert False, "零有效源应报错"
    except SystemExit:
        pass
    assert "警告" in capsys.readouterr().out
