# -*- coding: utf-8 -*-
"""core/nummatch.py 的单元测试：数字段放宽 + 动作决策 + records 软删过滤。

⚠️ 只 `from core import nummatch`。
   绝对不要 import Draft_Forward_Robot —— 它在导入时会定位生产 ROOT、拉 config
   （含 SMTP 账号），并触发 draft_pending 模块级 self_test() 以 rwc 打开真实
   pending_queue.db 并 INSERT/DELETE 一行。spec 2026-10-26 §四.0 有实测记录。

fixture 数据来源：`02_input/2026-10-26-理清_匹配键与箱号复用.md` §3 的 8 组判定表
（#1~#8 逐条抄，#9/#10 是 spec §六 6.1 标注的「构造」行）。
"""
import sqlite3

import pytest

from core import nummatch


# ---------- 6.1 数字段判定表（11 行） ----------
# (数字段, [(公司, 状态), ...] 组内记录, 来信客编, relax, 期望 level, 期望 company)
NUM_CASES = [
    # —— 理清稿 §3 的 8 组，逐条一致 ——
    ("260718008", [("保时达", "正常"), ("同程配", "退舱")],
     "CQWLJT260718008-XXX", True, "num_auto", "保时达"),
    ("260718009", [("沙坪坝", "正常"), ("同程配", "退舱")],
     "CQWLJT260718009-XXX", True, "num_auto", "沙坪坝"),
    ("260923001", [("太平洋", "正常"), ("中欧木业", "退舱")],
     "CQWLJT260923001-XYZ", True, "num_auto", "太平洋"),
    ("261001008", [("东盟", "退舱"), ("港九港铁", "正常")],
     "CQWLJT261001008-XXX", True, "num_auto", "港九港铁"),
    ("261021001", [("中欧木业", "退舱"), ("东盟", "正常")],
     "CQWLJT261021001-XXX", True, "num_auto", "东盟"),
    ("260618003", [("同程配", "退舱"), ("同程配", "正常")],
     "CQWLJT260618003-XXX", True, "num_auto", "同程配"),
    ("260209002", [("中欧木业", "退舱"), ("中欧木业", "退舱")],
     "CQWLJT260209002-XXX", True, "num", None),          # 全退舱：保持现状
    ("261001009", [("东盟", "退舱"), ("港九港铁", "退舱")],
     "CQWLJT261001009-XXX", True, "num", None),          # 全退舱：保持现状
    # —— 构造行（spec §六 6.1 #9 / #10）——
    ("260101001", [("太平洋", "正常"), ("保时达", "正常")],
     "CQWLJT260101001-XXX", True, "num", None),          # 排除退舱后仍跨公司
    ("260101002", [("环宇通", "正常")],
     "CQWLJT260101002-XXX", True, "num_auto", "环宇通"),  # 唯一公司
    # —— 同 #1，关掉放宽 ——
    ("260718008", [("保时达", "正常"), ("同程配", "退舱")],
     "CQWLJT260718008-XXX", False, "num", None),
]


def _records(num, group):
    """把 (公司, 状态) 列表变成 load_records 口径的记录（键为英文 id/code/box/company/status）。"""
    return [{"id": i + 1,
             "code": "CQWLJT%s-%s" % (num, "ABCDEFG"[i]),
             "box": "",
             "company": comp,
             "status": status}
            for i, (comp, status) in enumerate(group)]


@pytest.mark.parametrize("num,group,incoming,relax,exp_level,exp_company", NUM_CASES)
def test_resolve_num_match(num, group, incoming, relax, exp_level, exp_company):
    """数字段命中后的档位与公司判定。也走一遍 match_record，确认调用链一致。"""
    hits = _records(num, group)

    level, rec = nummatch.resolve_num_match(hits, relax)
    assert level == exp_level
    if exp_level == nummatch.NUM_AUTO_LEVEL:
        # 放宽命中时返回的必须是非退舱且有公司的那条，不能是 hits[0]（可能是退舱行）
        assert rec["status"] != nummatch.CANCELLED_STATUS
        assert rec["company"] == exp_company
    else:
        assert rec == hits[0]

    # 同一份数据从 match_record 进（来信客编带不同后缀，不会被 full 截走），档位必须一致
    level2, rec2 = nummatch.match_record(hits, incoming, num, "", relax)
    assert level2 == exp_level
    assert rec2 == rec


# ---------- 6.2 动作判定表（8 行） ----------
# (category, level, to_list, rec_status, box_cancelled, 期望 action)
ACTION_CASES = [
    ("A", "full", ["to@example.com"], "正常", False, "forward"),
    ("A", nummatch.NUM_AUTO_LEVEL, ["to@example.com"], "正常", False, "forward"),
    ("A", "num", ["to@example.com"], "正常", False, "pending"),
    ("A", nummatch.NUM_AUTO_LEVEL, [], "正常", False, "pending"),
    ("A", "full", ["to@example.com"], "退舱", False, "cancel"),
    ("A", "full", ["to@example.com"], "正常", True, "cancel"),
    ("C2", "num", ["to@example.com"], "正常", False, "record"),
    ("W", "num", ["to@example.com"], "正常", False, "record"),
]


@pytest.mark.parametrize("category,level,to_list,rec_status,box_cancelled,exp_action", ACTION_CASES)
def test_decide_action(category, level, to_list, rec_status, box_cancelled, exp_action):
    assert nummatch.decide_action(category, level, to_list, rec_status, box_cancelled) == exp_action


# ---------- 6.3 档位回归表（4 行，每行 relax True/False 同一断言） ----------
# (用例名, records, code, num, box, 期望 level, 期望 rec 的 id)
LEVEL_CASES = [
    ("full",
     [{"id": 11, "code": "CQWLJT260713004-BLLST", "box": "TSTU1111111", "company": "保时达", "status": "正常"}],
     "CQWLJT260713004-BLLST", "260713004", "TSTU1111111", "full", 11),
    ("box",
     [{"id": 21, "code": "CQWLJT260714001-AAA", "box": "TSTU2222222", "company": "东盟", "status": "正常"}],
     "", None, "TSTU2222222", "box", 21),
    ("box_multi",
     [{"id": 31, "code": "CQWLJT260714002-AAA", "box": "TSTU3333333", "company": "东盟", "status": "正常"},
      {"id": 32, "code": "CQWLJT260714003-BBB", "box": "TSTU3333333", "company": "港九港铁", "status": "正常"}],
     "", None, "TSTU3333333", "box_multi", 31),
    ("none",
     [{"id": 41, "code": "CQWLJT260714004-AAA", "box": "TSTU4444444", "company": "东盟", "status": "正常"}],
     "CQWLJT260714099-ZZZ", "260714099", "TSTU9999999", "none", None),
]


@pytest.mark.parametrize("name,records,code,num,box,exp_level,exp_id", LEVEL_CASES)
def test_match_record_level_regression(name, records, code, num, box, exp_level, exp_id):
    """full / box / box_multi / none 四个分支在 relax 开关两侧都必须与改动前一致。"""
    for relax in (True, False):
        level, rec = nummatch.match_record(records, code, num, box, relax)
        assert level == exp_level, "%s (relax=%s)" % (name, relax)
        assert (rec["id"] if rec else None) == exp_id, "%s (relax=%s)" % (name, relax)


# ---------- 6.4 / 6.5 load_records 软删过滤（真 sqlite，tmp_path 自建） ----------
def _make_db(path, rows):
    """建一张至少七列的 records 表并插行。rows: [(编码, 箱号, 公司, 状态, is_deleted)]"""
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE records ('
                 'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                 '"客户编码" TEXT, "箱号" TEXT, "开票子公司名称" TEXT, '
                 '"状态" TEXT, is_deleted INTEGER, "发班时间" TEXT)')
    for code, box, comp, status, deleted in rows:
        conn.execute('INSERT INTO records ("客户编码","箱号","开票子公司名称","状态",is_deleted,"发班时间") '
                     'VALUES (?,?,?,?,?,?)', (code, box, comp, status, deleted, "2026-10-01"))
    conn.commit()
    conn.close()


def test_load_records_ignores_soft_deleted(tmp_path):
    """同客编两条：软删那条（id 更小）不得被读入，只返回有效那条。"""
    db = str(tmp_path / "yxo.db")
    _make_db(db, [
        ("CQWLJT260718008-OLD", "TSTU5555555", "", "正常", 1),          # 软删、公司为空
        ("CQWLJT260718008-NEW", "TSTU5555556", "保时达", "正常", 0),
    ])
    rows = nummatch.load_records(db)
    assert len(rows) == 1
    assert rows[0]["code"] == "CQWLJT260718008-NEW"
    assert rows[0]["company"] == "保时达"


def test_load_records_soft_deleted_three_groups(tmp_path):
    """3 组「1 删 1 留」⇒ 只应剩 3 条。"""
    db = str(tmp_path / "yxo.db")
    rows = []
    for i, comp in enumerate(("保时达", "东盟", "太平洋")):
        rows.append(("CQWLJT26071900%d-DEL" % i, "TSTU600000%d" % i, comp, "正常", 1))
        rows.append(("CQWLJT26071900%d-KEEP" % i, "TSTU610000%d" % i, comp, "正常", 0))
    _make_db(db, rows)
    loaded = nummatch.load_records(db)
    assert len(loaded) == 3
    assert all(r["code"].endswith("-KEEP") for r in loaded)
