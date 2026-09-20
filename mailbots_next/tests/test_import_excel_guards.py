# -*- coding: utf-8 -*-
"""E 组（J/K：import_excel 退舱/软删守卫）验收测试：全部用临时库＋临时 Excel，勿碰真库。"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import config as root_config
from import_excel import run_import


def _make_db(path):
    # ALL_FIELDS 已含 状态/班列类型；is_deleted 与 manifest 夹具一样是独立列
    cols = ", ".join([f'"{c}" TEXT' for c in root_config.ALL_FIELDS])
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"""CREATE TABLE records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER, {cols},
            is_deleted INTEGER DEFAULT 0)"""
    )
    conn.commit()
    return conn


def _seed(conn, code, station, status="正常", deleted=0):
    conn.execute(
        'INSERT INTO records (seq, "客户编码", "目的站", "状态", is_deleted)'
        " VALUES (1, ?, ?, ?, ?)",
        (code, station, status, deleted),
    )
    conn.commit()


def _row_dict(conn, rid):
    return dict(conn.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())


def _make_xls(path, rows):
    # 目的站是 BASE_FIELDS（导入会刷新）；箱号是操作字段（导入保留），故用目的站断言更新语义
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["客户编码", "目的站"])
    for code, station in rows:
        ws.append([code, station])
    wb.save(path)


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "t.db"
    xls = tmp_path / "imp.xlsx"
    conn = _make_db(str(db))
    yield conn, str(xls)
    conn.close()


def test_retired_and_deleted_rows_untouched_plus_insert(env):
    """E.3①：同客户编码下退舱行 + 软删行都不被覆盖，改为新增 1 行。"""
    conn, xls = env
    _seed(conn, "KX001", "OLD-ST", status="退舱", deleted=0)
    _seed(conn, "KX001", "OLD-ST2", status="正常", deleted=1)
    retired_before = _row_dict(conn, 1)
    deleted_before = _row_dict(conn, 2)
    n_before = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    _make_xls(xls, [("KX001", "NEW-ST")])
    run_import(conn, xls)
    assert _row_dict(conn, 1) == retired_before  # 退舱行逐字段未动
    assert _row_dict(conn, 2) == deleted_before  # 软删行逐字段未动
    n_after = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n_after == n_before + 1  # 走 INSERT 新增分支
    new_row = conn.execute(
        'SELECT * FROM records WHERE "客户编码"=? AND "目的站"=?', ("KX001", "NEW-ST")).fetchone()
    assert new_row is not None


def test_normal_row_still_updated(env):
    """E.3②：正常行仍被更新（不回归），总数不变。"""
    conn, xls = env
    _seed(conn, "KN001", "OLD-ST", status="正常", deleted=0)
    _make_xls(xls, [("KN001", "NEW-ST")])
    run_import(conn, xls)
    row = conn.execute('SELECT * FROM records WHERE "客户编码"=?', ("KN001",)).fetchone()
    assert row["目的站"] == "NEW-ST"
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_same_code_normal_updated_retired_untouched(env):
    """同码一正常一退舱（如 CQWLJT260923001-D）：只更新正常行，退舱凭据不动。"""
    conn, xls = env
    _seed(conn, "KR001", "NORMAL-ST", status="正常", deleted=0)   # id=1 先入库
    _seed(conn, "KR001", "RETIRED-ST", status="退舱", deleted=0)  # id=2 退舱凭据
    retired_before = _row_dict(conn, 2)
    _make_xls(xls, [("KR001", "UPDATED-ST")])
    run_import(conn, xls)
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 2
    normal = _row_dict(conn, 1)  # ORDER BY id 取最早那条正常行
    assert normal["目的站"] == "UPDATED-ST"
    assert _row_dict(conn, 2) == retired_before
