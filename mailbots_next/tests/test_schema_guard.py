"""Schema 守卫（2026-09-21）：防"代码读一张没人建的表"复发。

背景：草单台账那张表生产从未建过，只有测试自己建 ⇒ 全绿也照样上线。
两类守卫都"照不出假绿"：测试自己不建任何业务表。

测试 A：静态扫描源码，referenced - created - 外部 == 空。
测试 B：子进程做真实生产初始化（import 即建表），断言 5 张表真实落盘。
"""
import ast
import io
import re
import sqlite3
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent  # mailbots_next/
REPO_ROOT = BASE_DIR.parent

_REF_RE = re.compile(r"\b(?:FROM|INTO|JOIN)\s+([A-Za-z_][A-Za-z_0-9]*)", re.IGNORECASE)
_UPD_RE = re.compile(r"\bUPDATE\s+([A-Za-z_][A-Za-z_0-9]*)\s+SET", re.IGNORECASE)
_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z_0-9]*)", re.IGNORECASE
)

# 外部表：必须写明"谁建的"，且测试验证该文件里真有 CREATE TABLE。
_EXTERNAL_TABLES = {
    "records": "app.py",              # 网页端 app.py:247
    "tracing_log": "app.py",          # app.py:297
    "tracing_snapshot": "app.py",     # app.py:314
}

# 代码自建表（生产初始化路径建出）：dedup.py 尾部 init_db()、
# store.py 尾部 init_bot_config_db() / init_forward_log() / init_bounce_handled()。
_CREATED_TABLES = {"dedup", "error_queue", "bot_config", "forward_log", "bounce_handled"}


def _clean_source(text: str) -> str:
    """把 import 语句行、docstring、注释替换为空格（保偏移），再做 finditer。

    偏离说明：spec 原正则直接扫全文会在本仓产生约 40 个误命中（全部来自
    `from X import Y` 导入行，如 FROM datetime / FROM typing / FROM mailbots_next，
    外加注释与 docstring 里的英文 prose 如 "into a 500"、"from active"、"from spec"，
    以及 `mail_from or sender`（FROM 嵌在标识符里）。本函数只剔除"语法上不可能是
    SQL"的位置（import 语句/注释/docstring）+ 给关键字加 \\b；SQL 一律写在字符串
    字面量里，不受影响；多行字符串照样整块命中（finditer 对全文，非逐行）。
    """
    lines = text.splitlines(keepends=True)

    def blank(lineno_start, lineno_end):
        for n in range(lineno_start, lineno_end + 1):
            if 1 <= n <= len(lines):
                lines[n - 1] = " " * len(lines[n - 1])

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                blank(node.lineno, node.end_lineno or node.lineno)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                # docstring（独立字符串语句）：英文 prose 重灾区，不可能是 SQL。
                blank(node.lineno, node.end_lineno or node.lineno)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                (sl, sc), (el, ec) = tok.start, tok.end
                if sl == el and 1 <= sl <= len(lines):
                    line = lines[sl - 1]
                    lines[sl - 1] = line[:sc] + " " * (ec - sc) + line[ec:]
    except (tokenize.TokenError, SyntaxError):
        pass
    return "".join(lines)


def _scan_mailbots_next():
    referenced: set = set()
    created: set = set()
    for py in sorted(BASE_DIR.rglob("*.py")):
        if "tests" in py.parts:
            continue
        cleaned = _clean_source(py.read_text(encoding="utf-8"))
        referenced.update(m.group(1) for m in _REF_RE.finditer(cleaned))
        referenced.update(m.group(1) for m in _UPD_RE.finditer(cleaned))
        created.update(m.group(1) for m in _CREATE_RE.finditer(cleaned))
    return referenced, created


def test_every_referenced_table_is_created():
    # 专锁本次修复的表名：拆写以免本文件自身成为文本命中。
    ledger = "forwarded" + "_drafts"
    referenced, created = _scan_mailbots_next()
    # 专锁本次修复：该台账不得再被引用（含 tests 全文 0 命中）。
    assert ledger not in referenced, "ledger table still referenced in source"
    hits = []
    for py in sorted(BASE_DIR.rglob("*.py")):
        if ledger in py.read_text(encoding="utf-8"):
            hits.append(str(py))
    assert hits == [], f"ledger table textual hits: {hits}"
    # 外部表豁免本身要被验证：声明的文件里真有 CREATE TABLE <表名>。
    for table, owner in _EXTERNAL_TABLES.items():
        owner_text = (REPO_ROOT / owner).read_text(encoding="utf-8")
        assert re.search(
            rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{table}\b", owner_text, re.IGNORECASE
        ), f"external table {table!r} not actually created in {owner}"
    diff = referenced - created - set(_EXTERNAL_TABLES)
    assert diff == set(), f"tables read but never created: {sorted(diff)}"


def test_fresh_production_init_creates_all_tables(tmp_path):
    """生产初始化冒烟：子进程 import 即建表（模块级 init），不调任何 init_*。"""
    import os

    env = dict(os.environ)
    env["MAILBOT_MODE"] = "test"
    env["BOT_CONFIG_DB_PATH"] = str(tmp_path / "bot_config.db")
    env["DEDUP_DB_PATH"] = str(tmp_path / "dedup.db")
    env["DAILY_COUNTERS_PATH"] = str(tmp_path / "daily_counters.json")
    env["IMAP_STATE_PATH"] = str(tmp_path / "imap_state.json")
    env["YXO_DB_PATH"] = str(tmp_path / "yxo.db")
    # 凭证：沿用父进程 conftest 的假 secrets（MAILBOT_SECRETS_PATH 已指向 tmp 假文件，
    # 经 env 继承）；import 路径只做建表，不发信。
    proc = subprocess.run(
        [sys.executable, "-c", "import mailbots_next.core.dedup, mailbots_next.core.store"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"fresh init failed: {proc.stderr[-2000:]}"
    actual: set = set()
    for db in ("bot_config.db", "dedup.db"):
        p = tmp_path / db
        assert p.exists(), f"{db} not created by fresh init"
        conn = sqlite3.connect(p)
        try:
            actual.update(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
        finally:
            conn.close()
    assert _CREATED_TABLES <= actual, \
        f"missing tables after fresh init: {sorted(_CREATED_TABLES - actual)}"
    # 测试 A 的判据在真实初始化结果上再跑一遍：代码会读、但既不在本地建表清单、
    # 也不是外部表的表必须为空。
    referenced, created = _scan_mailbots_next()
    assert created == _CREATED_TABLES, \
        f"created-table drift: {sorted(created ^ _CREATED_TABLES)}"
    assert (referenced - created - set(_EXTERNAL_TABLES)) == set()
