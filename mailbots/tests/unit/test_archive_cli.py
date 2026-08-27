# -*- coding: utf-8 -*-
"""归档 CLI：dry-run 不动数据；apply 搬移并建 *_archive。直接 exec 模块调用 main()。"""
import os
import sqlite3
import sys
from core import events_store as es  # noqa: F401 确保 sys.path 已含 mailbots

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts_archive.py")


def _load_mod(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(tmp_path):
    p = str(tmp_path / "yxo.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE forward_log(id INTEGER PRIMARY KEY, ts TEXT)")
    conn.executemany("INSERT INTO forward_log(ts) VALUES(?)",
                     [("2026-01-01 00:00:00",), ("2099-01-01 00:00:00",)])
    conn.commit()
    conn.close()
    return p


def test_dry_run_then_apply(tmp_path):
    dbp = _make_db(tmp_path)
    args = ["scripts_archive.py", "--db", dbp, "--tables", "forward_log",
            "--date-col", "ts", "--keep-days", "90"]
    mod = _load_mod("scripts_archive_a")
    out = mod.main(args + ["--dry-run"])
    assert "forward_log" in out and "1" in out            # dry-run 报告 1 行待归档
    assert not os.path.exists(str(tmp_path / "forward_log_archive"))

    mod2 = _load_mod("scripts_archive_b")
    mod2.main(args + ["--apply"])
    chk = sqlite3.connect(dbp)
    assert chk.execute("SELECT COUNT(*) FROM forward_log").fetchone()[0] == 1
    assert chk.execute("SELECT COUNT(*) FROM forward_log_archive").fetchone()[0] == 1
