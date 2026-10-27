# -*- coding: utf-8 -*-
"""core/dskroute.py 的单元测试：班列号解析 + 组合键判定 + 整封语义 + 软删过滤 + 告警收件人。

⚠️ 只 `from core import dskroute`。
   绝对不要 import Dsk_Robot —— 它在导入时会
     :36  sys.path.insert(0, r'D:\\YXO_DATA\\WeComBot')
     :41  from config import ...（读生产配置，含 SMTP 账号）
     :740 from config_local import STAMP_TOKEN（模块级执行）
   spec 2026-10-27 §四.0 有说明。

数据来源：02_SPEC-DSK箱号加班列号路由.md §六 6.1~6.6，逐条对齐。
"""
import sqlite3

import pytest

from core import dskroute


# ---------- 测试用索引构造（与 load_index 产出的结构一致） ----------

def _rec(box, train, company, rid=1, code="", status="正常"):
    return {"id": rid, "code": code, "box": box, "train": train,
            "company": company, "status": status}


def _index(records):
    by_box_train = {}
    by_box = {}
    for r in records:
        by_box.setdefault(r["box"], []).append(r)
        if r["train"]:
            by_box_train.setdefault((r["box"], r["train"]), []).append(r)
    return by_box_train, by_box


# ---------- 6.1 extract_train 三种主题写法（8 行） ----------

TRAIN_CASES = [
    ("YXO-223 / DSK / AGROSTOP - 27.03.2026", "WB223"),
    ("DSK / AGROSTOP / YXO-219 - 13.04.2026 - FWRU0197560 x 1", "WB219"),
    ("YXO-219/DSK/AGROSTOP 13.04.2025", "WB219"),
    ("DSK YXO WB0808-ATD0809(773) / 1 x Ready for pick-up from Agrostop", "WB773"),
    # 已知边界：ID507 不是班列号，库内 WB507 与该箱无关 ⇒ 取不到，走箱号兜底
    ("DSK YXO WB0821 ATD0823 ID507 CICU2690998 x 1 / Ready for pick-up", ""),
    ("YXO-2026-864 / DSK / AGROSTOP - 01.05.2026", "WB864"),
    ("DSK/AGROSTOP 01.01.2026", ""),
    (None, ""),
]


@pytest.mark.parametrize("subject,expected", TRAIN_CASES)
def test_extract_train(subject, expected):
    assert dskroute.extract_train(subject) == expected


# ---------- 6.2 resolve 判定表（8 行） ----------

RESOLVE_CASES = [
    # 1 组合键唯一命中
    ("WB223", "A", [_rec("A", "WB223", "中欧木业")], dskroute.LV_TRAIN_BOX),
    # 2 组合查不到，箱号唯一（WB881）⇒ 兜底转发
    ("WB223", "A", [_rec("A", "WB881", "同程配")], dskroute.LV_BOX_ONLY),
    # 3 组合查不到，箱号两条 ⇒ 告警
    ("WB223", "A", [_rec("A", "WB881", "同程配"), _rec("A", "WB883", "保时达")],
     dskroute.LV_MULTI),
    # 4 组合键命中两条 ⇒ 告警
    ("WB223", "A", [_rec("A", "WB223", "中欧木业"), _rec("A", "WB223", "中欧木业")],
     dskroute.LV_MULTI),
    # 5 无班列号，箱号唯一
    ("", "A", [_rec("A", "WB881", "同程配")], dskroute.LV_BOX_ONLY),
    # 6 无班列号，箱号两条
    ("", "A", [_rec("A", "WB881", "同程配"), _rec("A", "WB883", "保时达")],
     dskroute.LV_MULTI),
    # 7 箱号在库内查不到
    ("", "ZZZZ", [], dskroute.LV_MISS),
    # 8 班列号不在组合里，箱号只有一条（WB777）⇒ 兜底转发
    ("WB999", "A", [_rec("A", "WB777", "同程配")], dskroute.LV_BOX_ONLY),
]


@pytest.mark.parametrize("train,box,records,expected", RESOLVE_CASES)
def test_resolve_level(train, box, records, expected):
    by_box_train, by_box = _index(records)
    level, rec, cands = dskroute.resolve(by_box_train, by_box, box, train)
    assert level == expected
    if expected in (dskroute.LV_TRAIN_BOX, dskroute.LV_BOX_ONLY):
        assert rec is not None and rec["box"] == box
        assert cands == [rec]
    else:
        assert rec is None


# ---------- 6.3 动作级（4 行 parametrize + 1 条整封语义） ----------

LEVEL_ACTION_CASES = [
    (dskroute.LV_TRAIN_BOX, dskroute.ACTION_FORWARD),
    (dskroute.LV_BOX_ONLY, dskroute.ACTION_FORWARD),
    (dskroute.LV_MULTI, dskroute.ACTION_ALERT),
    (dskroute.LV_MISS, dskroute.ACTION_ALERT),
]


@pytest.mark.parametrize("level,expected", LEVEL_ACTION_CASES)
def test_decide_action(level, expected):
    assert dskroute.decide(level) == expected


def test_plan_email_keeps_whole_mail_semantics():
    """一封 4 箱、第 3 箱 multi ⇒ blocked 非空、plans 只含前 2 箱。

    证明「整封」语义没被改成逐箱部分转发：预检一旦不通过，
    后面的箱一个都不进 plans，调用方拿不到任何可发送项。
    """
    records = [
        _rec("CICU1000001", "WB223", "中欧木业", 1),
        _rec("CICU1000002", "WB223", "中欧木业", 2),
        _rec("CICU1000003", "WB881", "同程配", 3),
        _rec("CICU1000003", "WB883", "保时达", 4),
        _rec("CICU1000004", "WB223", "中欧木业", 5),
    ]
    by_box_train, by_box = _index(records)
    rows = [(b, "ASSOC") for b in
            ("CICU1000001", "CICU1000002", "CICU1000003", "CICU1000004")]

    plans, blocked = dskroute.plan_email(
        by_box_train, by_box, rows, "WB223", lambda b, c: ["to@example.com"])

    assert blocked is not None
    assert blocked[0] == "CICU1000003"
    assert blocked[1] == dskroute.LV_MULTI
    assert [p["box"] for p in plans] == ["CICU1000001", "CICU1000002"]


# ---------- 6.4 load_index 软删过滤（3 条，临时 sqlite） ----------

_DDL = (
    'CREATE TABLE records('
    '  id INTEGER PRIMARY KEY,'
    '  "客户编码" TEXT, "箱号" TEXT, "班列号" TEXT,'
    '  "开票子公司名称" TEXT, "状态" TEXT,'
    '  is_deleted INTEGER DEFAULT 0'
    ')'
)


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    try:
        conn.execute(_DDL)
        conn.executemany(
            'INSERT INTO records (id, "客户编码", "箱号", "班列号",'
            ' "开票子公司名称", "状态", is_deleted) VALUES (?,?,?,?,?,?,?)',
            rows)
        conn.commit()
    finally:
        conn.close()


def test_load_index_filters_soft_deleted(tmp_path):
    """两条同箱号同班列号，一条 is_deleted=1 ⇒ 组合键只剩 1 条。"""
    db = str(tmp_path / "a.db")
    _make_db(db, [
        (1, "C1", "CICU1000001", "WB223", "中欧木业", "正常", 0),
        (2, "C2", "CICU1000001", "WB223", "同程配", "正常", 1),
    ])
    by_box_train, by_box = dskroute.load_index(db)
    assert len(by_box_train[("CICU1000001", "WB223")]) == 1
    assert by_box_train[("CICU1000001", "WB223")][0]["company"] == "中欧木业"
    assert len(by_box["CICU1000001"]) == 1


def test_load_index_skips_empty_box(tmp_path):
    """箱号为空串的记录不进任何索引。"""
    db = str(tmp_path / "b.db")
    _make_db(db, [
        (1, "C1", "", "WB223", "中欧木业", "正常", 0),
        (2, "C2", "CICU1000002", "WB223", "中欧木业", "正常", 0),
    ])
    by_box_train, by_box = dskroute.load_index(db)
    assert "" not in by_box
    assert list(by_box) == ["CICU1000002"]
    assert list(by_box_train) == [("CICU1000002", "WB223")]


def test_load_index_empty_train_stays_in_by_box_only(tmp_path):
    """班列号为空 ⇒ 进 by_box，不进 by_box_train。"""
    db = str(tmp_path / "c.db")
    _make_db(db, [
        (1, "C1", "CICU1000003", "", "同程配", "正常", 0),
        (2, "C2", "CICU1000003", "WB777", "同程配", "正常", 0),
    ])
    by_box_train, by_box = dskroute.load_index(db)
    assert len(by_box["CICU1000003"]) == 2
    assert list(by_box_train) == [("CICU1000003", "WB777")]


# ---------- 6.5 组合键回放：3 例取错公司全部纠正（1 条） ----------

REPLAY_ROWS = [
    # 箱号, 班列, 正确公司(id 较小), 干扰公司(id 较大)
    ("CICU1912935", "WB223", "中欧木业", "WB881", "同程配"),
    ("FWRU0042527", "WB433", "保时达", "WB883", "同程配"),
    ("XHCU5033213", "WB555", "港九港铁", "WB884", "同程配"),
]


def test_replay_reused_boxes_resolve_to_earlier_record(tmp_path):
    """箱号复用场景：组合键必须取到该班列那条，而不是 id 最大的那条。

    ⚠️ 真库回放需生产库只读副本，由验收方执行；这里用临时库造同构数据。
    """
    db = str(tmp_path / "replay.db")
    rows = []
    rid = 1
    for box, train, right_company, wrong_train, wrong_company in REPLAY_ROWS:
        rows.append((rid, "C-%s" % rid, box, train, right_company, "正常", 0))
        rid += 1
        rows.append((rid, "C-%s" % rid, box, wrong_train, wrong_company, "正常", 0))
        rid += 1
    _make_db(db, rows)

    by_box_train, by_box = dskroute.load_index(db)
    # 每组都是「箱号两条」——按箱号取必错，组合键取才对
    assert all(len(recs) == 2 for recs in by_box.values())

    for box, train, right_company, _, _ in REPLAY_ROWS:
        level, rec, _ = dskroute.resolve(by_box_train, by_box, box, train)
        assert level == dskroute.LV_TRAIN_BOX
        assert rec["company"] == right_company
        # 取到的是 id 较小的那条（「最后一条赢」的相反面）
        assert rec["id"] == min(r["id"] for r in by_box[box])


# ---------- 6.6 alert_names 收件人（4 条） ----------

_LOOKUP = {"甲": "杨雅雯", "乙": "韩文豪", "丙": "冯茜", "太平": "毛骁洋"}


def _lookup(company):
    return _LOOKUP.get(company)


ALERT_CASES = [
    # 1 两个候选公司 ⇒ 两个负责人 + 洋
    (["甲", "乙"], ("毛骁洋",), ["杨雅雯", "韩文豪", "毛骁洋"]),
    # 2 拿不到公司 ⇒ 只发洋
    (None, ("毛骁洋",), ["毛骁洋"]),
    # 3 候选是洋自己负责的公司 ⇒ 去重，只发一次
    (["太平"], ("毛骁洋",), ["毛骁洋"]),
    # 4 公司查不到负责人 ⇒ 只发洋
    (["不存在的公司"], ("毛骁洋",), ["毛骁洋"]),
]


@pytest.mark.parametrize("companies,always,expected", ALERT_CASES)
def test_alert_names(companies, always, expected):
    assert dskroute.alert_names(companies, _lookup, always) == expected
