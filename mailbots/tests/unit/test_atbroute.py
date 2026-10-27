# -*- coding: utf-8 -*-
"""core/atbroute.py 的单元测试：客编/班列号解析 + 组合键路由判定 + 索引软删过滤 + 告警收件人。

⚠️ 只 `from core import atbroute`（照 test_nummatch.py 的先例）。
   绝对不要 import Atb_Robot —— 它在导入时会 sys.path.insert(0, r'D:\\YXO_DATA\\WeComBot')、
   `from config import ...`（读生产配置，含 SMTP 账号），并在文件末尾
   `from config_local import STAMP_TOKEN`（import 时执行）。spec §四.0 有记录。

判定表来源：`02_SPEC-ATB客编加箱号路由.md` §六（6.1 ~ 6.7）。
"""
import sqlite3

import pytest

from core import atbroute


# 临时库建表语句（spec §六 6.5，六列齐全）
CREATE_SQL = ('CREATE TABLE records('
              'id INTEGER PRIMARY KEY, '
              '"客户编码" TEXT, "箱号" TEXT, "班列号" TEXT, '
              '"开票子公司名称" TEXT, "状态" TEXT, '
              'is_deleted INTEGER DEFAULT 0)')


def _make_db(path, rows):
    """建临时 records 表。rows: [(id, 客户编码, 箱号, 班列号, 公司, 状态, is_deleted)]"""
    conn = sqlite3.connect(path)
    conn.execute(CREATE_SQL)
    conn.executemany(
        'INSERT INTO records (id, "客户编码", "箱号", "班列号", "开票子公司名称", "状态", is_deleted) '
        'VALUES (?,?,?,?,?,?,?)', rows)
    conn.commit()
    conn.close()


def _idx(rows):
    """按 load_index 的口径把 [(id, code, box, company, status)] 造成内存索引，不读库。"""
    by_code_box = {}
    by_box = {}
    for rid, code, box, company, status in rows:
        rec = {"id": rid, "code": code, "box": box, "train": "",
               "company": company, "status": status}
        by_box.setdefault(box, []).append(rec)
        if code:
            by_code_box.setdefault((code, box), []).append(rec)
    return by_code_box, by_box


# ---------- 6.1 extract_code（6 行） ----------
CODE_CASES = [
    ("pickup reference for YXO-2026-134   CQWLJT260201008-MA\nFWRU0197236",
     "CQWLJT260201008-MA"),
    ("ATB for YXO-2026-864   CQWLJT260912003-D   FWRU0042785",
     "CQWLJT260912003-D"),
    ("ATB for YXO-2026-864   CQWLJT260912003", "CQWLJT260912003"),
    ("pickup reference for YXO-2026-134  FWRU0197236", ""),
    ("cqwljt260912003-d", ""),          # 小写不匹配：已知边界，当期按取不到处理
    (None, ""),
]


@pytest.mark.parametrize("subject,expected", CODE_CASES)
def test_extract_code(subject, expected):
    assert atbroute.extract_code(subject) == expected


# ---------- 6.2 extract_train（3 行） ----------
TRAIN_CASES = [
    ("pickup reference for YXO-2026-134  CQWLJT260201008-MA", "WB134"),
    ("YXO-134 / ATB", "WB134"),
    ("ATB for CQWLJT260912003-D  FWRU0042785", ""),
]


@pytest.mark.parametrize("subject,expected", TRAIN_CASES)
def test_extract_train(subject, expected):
    assert atbroute.extract_train(subject) == expected


# ---------- 6.3 resolve（8 行） ----------
# (用例名, 索引行 [(id, code, box, company, status)], 来信客编, 箱号, 期望 level, 期望 record id)
RESOLVE_CASES = [
    ("combo_unique",
     [(11, "CQWLJT1-MA", "A", "保时达", "正常")],
     "CQWLJT1-MA", "A", "code_box", 11),
    ("combo_miss_but_box_has_other",
     [(12, "CQWLJT9-ZZ", "A", "同程配", "正常")],
     "CQWLJT1-MA", "A", "miss", None),
    ("box_only_unique",
     [(13, "CQWLJT2-BB", "A", "东盟", "正常")],
     "", "A", "box_only", 13),
    ("box_only_multi",
     [(14, "CQWLJT2-BB", "A", "东盟", "正常"),
      (15, "CQWLJT3-CC", "A", "保时达", "正常")],
     "", "A", "multi", None),
    ("combo_multi",
     [(16, "CQWLJT1-MA", "A", "保时达", "正常"),
      (17, "CQWLJT1-MA", "A", "中欧木业", "退舱")],
     "CQWLJT1-MA", "A", "multi", None),
    ("box_not_found",
     [],
     "", "ZZZZ", "miss", None),
    ("combo_unique_cancelled_record",
     [(18, "CQWLJT1-MA", "A", "保时达", "退舱")],
     "CQWLJT1-MA", "A", "code_box", 18),
    ("empty_box",
     [],
     "", "", "miss", None),
]


@pytest.mark.parametrize("name,rows,code,box,exp_level,exp_id", RESOLVE_CASES)
def test_resolve(name, rows, code, box, exp_level, exp_id):
    by_code_box, by_box = _idx(rows)
    level, rec, cands = atbroute.resolve(by_code_box, by_box, code, box)
    assert level == exp_level, name
    assert (rec["id"] if rec else None) == exp_id, name
    # candidates 只在 multi / miss 时被用于告警文案，必须与索引里的候选集合一致
    if exp_level == atbroute.LV_MULTI:
        assert len(cands) == 2, name
    if exp_level == atbroute.LV_MISS:
        assert isinstance(cands, list), name


# ---------- 6.4 decide（4 行） ----------
DECIDE_CASES = [
    (atbroute.LV_CODE_BOX, atbroute.ACTION_FORWARD),
    (atbroute.LV_BOX_ONLY, atbroute.ACTION_FORWARD),
    (atbroute.LV_MULTI, atbroute.ACTION_ALERT),
    (atbroute.LV_MISS, atbroute.ACTION_ALERT),
]


@pytest.mark.parametrize("level,exp_action", DECIDE_CASES)
def test_decide(level, exp_action):
    assert atbroute.decide(level) == exp_action


# ---------- 6.5 load_index（3 条，真 sqlite + tmp_path） ----------
def test_load_index_ignores_soft_deleted(tmp_path):
    """同客编同箱号两条，一条 is_deleted=1 ⇒ 索引里只剩 1 条。"""
    db = str(tmp_path / "yxo.db")
    _make_db(db, [
        (101, "CQWLJT260912003-D", "FWRU0042785", "WB864", "保时达", "正常", 1),
        (102, "CQWLJT260912003-D", "FWRU0042785", "WB864", "保时达", "正常", 0),
    ])
    by_code_box, by_box = atbroute.load_index(db)
    assert len(by_code_box[("CQWLJT260912003-D", "FWRU0042785")]) == 1
    assert by_code_box[("CQWLJT260912003-D", "FWRU0042785")][0]["id"] == 102
    assert len(by_box["FWRU0042785"]) == 1


def test_load_index_skips_empty_box(tmp_path):
    """箱号为空串的记录不进任何索引。"""
    db = str(tmp_path / "yxo.db")
    _make_db(db, [
        (103, "CQWLJT260912004-D", "", "WB864", "同程配", "正常", 0),
        (104, "CQWLJT260912005-D", "FWRU0042786", "WB864", "同程配", "正常", 0),
    ])
    by_code_box, by_box = atbroute.load_index(db)
    assert "" not in by_box
    assert ("CQWLJT260912004-D", "") not in by_code_box
    assert len(by_box) == 1
    assert len(by_code_box) == 1


def test_load_index_missing_db_raises(tmp_path):
    """库文件不存在 ⇒ 抛异常，由机器人侧兜住并本轮 return 0。"""
    missing = str(tmp_path / "not_exists.db")
    with pytest.raises(Exception):
        atbroute.load_index(missing)


# ---------- 6.6 组合键回放（1 条） ----------
# 生产库实测的 3 个复用箱号（spec §六 6.6）。实施方手里没有生产库，
# 这里用临时库按生产 id 顺序造同样的两组：较早那条是正确公司，较晚那条是箱号
# 最后覆盖赢到的公司（基线 box_company.get(box_no) 取到的就是它）。
REPLAY_ROWS = [
    (276,  "CQWLJT260315005-MA", "CICU1912935", "WB223", "中欧木业", "正常", 0),
    (571,  "CQWLJT260510001-MA", "FWRU0042527", "WB433", "保时达", "正常", 0),
    (627,  "CQWLJT260614001-MA", "CHTU4100456", "WB524", "港九港铁", "正常", 0),
    (2320, "CQWLJT260301001-AA", "CICU1912935", "WB881", "同程配", "正常", 0),
    (2415, "CQWLJT260302001-AA", "FWRU0042527", "WB883", "同程配", "正常", 0),
    (2362, "CQWLJT260303001-AA", "CHTU4100456", "WB884", "同程配", "正常", 0),
]

REPLAY_CASES = [
    ("CQWLJT260315005-MA", "CICU1912935", "中欧木业", "同程配"),
    ("CQWLJT260510001-MA", "FWRU0042527", "保时达", "同程配"),
    ("CQWLJT260614001-MA", "CHTU4100456", "港九港铁", "同程配"),
]


def test_replay_reused_boxes(tmp_path):
    """组合键取到较早（正确）那条；箱号的「最后一条赢」取到较晚（会转错）那条。"""
    db = str(tmp_path / "yxo.db")
    _make_db(db, REPLAY_ROWS)
    by_code_box, by_box = atbroute.load_index(db)

    for code, box, exp_company, old_company in REPLAY_CASES:
        level, rec, _cands = atbroute.resolve(by_code_box, by_box, code, box)
        assert level == atbroute.LV_CODE_BOX, (code, box)
        assert rec["company"] == exp_company, (code, box)

        # 基线口径的复现：箱号有多条，逐行覆盖 ⇒ 最后一条（id 最大）赢
        last = max(by_box[box], key=lambda r: r["id"])
        assert last["company"] == old_company, (code, box)
        assert len(by_box[box]) == 2, (code, box)


# ---------- 6.7 alert_names（4 行） ----------
# 桩 lookup：不 import config（公司 → 负责人）
_LOOKUP = {"甲": "杨雅雯", "乙": "韩文豪", "丙": "冯茜", "太平": "毛骁洋"}


def _stub_lookup(company):
    return _LOOKUP.get(company)


ALERT_CASES = [
    (["甲", "乙"], ["杨雅雯", "韩文豪", "毛骁洋"]),
    (None, ["毛骁洋"]),                        # spec 的「[] / None」：本条同时覆盖 []，见下
    (["太平"], ["毛骁洋"]),                    # 洋自己负责的公司：去重后只发一次
    (["不存在的公司"], ["毛骁洋"]),             # lookup 返回 None：不报错
]


@pytest.mark.parametrize("companies,expected", ALERT_CASES)
def test_alert_names(companies, expected):
    assert atbroute.alert_names(companies, _stub_lookup, ("毛骁洋",)) == expected
    if companies is None:
        # spec §六 6.7 第 2 行写的是「[] / None」，空列表必须与 None 同结果
        assert atbroute.alert_names([], _stub_lookup, ("毛骁洋",)) == expected
