# -*- coding: utf-8 -*-
"""ATB 邮件路由判定（纯函数模块，零副作用）。

为什么单独成模块：
    Atb_Robot.py 在 import 期就会 sys.path.insert + from config import（读生产配置）、
    末尾 from config_local import STAMP_TOKEN，单元测试无法直接 import 它。
    本模块不读环境变量、不建库、不发网络请求，只接收数据、返回判定。

判定顺序（客编为权威，箱号必需）：
    1. 有客编 + (客编, 箱号) 唯一命中   → level=code_box   → forward
    2. 有客编 + 组合查不到              → level=miss       → alert（不静默回退箱号）
    3. 无客编 + 箱号唯一命中            → level=box_only   → forward（记 warn）
    4. 任一环节命中多条                 → level=multi      → alert
    5. 箱号在库内查不到                 → level=miss       → alert
"""
import re
import sqlite3

# 客编：CQWLJT + 9 位数字，可带 -后缀
CODE_RE = re.compile(r"CQWLJT\d{9}(?:-[A-Za-z0-9]+)?")

# 班列号：主题里的 YXO-2026-134 / YXO 134 / YXO-134
TRAIN_RE = re.compile(r"YXO[- ]?(?:20\d{2}[- ]?)?(\d{3})(?!\d)")

LV_CODE_BOX = "code_box"    # 客编+箱号唯一命中
LV_BOX_ONLY = "box_only"    # 无客编，箱号唯一命中（回退）
LV_MULTI = "multi"          # 命中多条
LV_MISS = "miss"            # 查不到

ACTION_FORWARD = "forward"
ACTION_ALERT = "alert"


def extract_code(subject):
    """从主题取客编全码；取不到返回 ''。"""
    m = CODE_RE.search(subject or "")
    return m.group(0) if m else ""


def extract_train(subject):
    """从主题取班列号，返回 'WB773' 形式；取不到返回 ''。仅用于日志与校验，不参与路由。"""
    m = TRAIN_RE.search(subject or "")
    return ("WB" + m.group(1)) if m else ""


def load_index(db_path):
    """读 records（过滤软删），返回 (by_code_box, by_box)。

    by_code_box: {(客编, 箱号): [record, ...]}
    by_box:      {箱号: [record, ...]}
    record: dict(id, code, box, train, company, status)
    """
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT id, "客户编码", "箱号", "班列号", "开票子公司名称", "状态" '
            "FROM records WHERE COALESCE(is_deleted, 0) = 0"
        ).fetchall()
    finally:
        conn.close()

    by_code_box = {}
    by_box = {}
    for r in rows:
        box = str(r["箱号"] or "").strip()
        if not box:
            continue
        code = str(r["客户编码"] or "").strip()
        rec = {
            "id": r["id"],
            "code": code,
            "box": box,
            "train": str(r["班列号"] or "").strip(),
            "company": str(r["开票子公司名称"] or "").strip(),
            "status": str(r["状态"] or "").strip(),
        }
        by_box.setdefault(box, []).append(rec)
        if code:
            by_code_box.setdefault((code, box), []).append(rec)
    return by_code_box, by_box


def resolve(by_code_box, by_box, code, box):
    """返回 (level, record, candidates)。

    record 只在 level 为 code_box / box_only 时非 None。
    candidates 在 multiple / miss 时给出全部候选，供告警文案使用。
    """
    if code:
        hits = by_code_box.get((code, box), [])
        if len(hits) == 1:
            return LV_CODE_BOX, hits[0], hits
        if hits:
            return LV_MULTI, None, hits
        # 客编给了但组合查不到 → 判 miss，不静默回退到箱号
        return LV_MISS, None, by_box.get(box, [])

    hits = by_box.get(box, [])
    if len(hits) == 1:
        return LV_BOX_ONLY, hits[0], hits
    if hits:
        return LV_MULTI, None, hits
    return LV_MISS, None, []


def decide(level):
    """档位 → 动作。"""
    if level in (LV_CODE_BOX, LV_BOX_ONLY):
        return ACTION_FORWARD
    return ACTION_ALERT


def alert_names(companies, lookup, always=()):
    """候选公司 → 告警收件人名单（去重，末尾补 always）。

    lookup: 公司名 → 真名的函数（生产传 config.company_to_name，测试传桩）。
    always: 始终抄送的姓名（生产传 ("毛骁洋",)）。
    公司查不到负责人时不报错，跳过即可 —— 至少要留下 always 里的人。
    """
    names = []
    for c in companies or []:
        n = lookup(c) if c else None
        if n and n not in names:
            names.append(n)
    for n in always:
        if n not in names:
            names.append(n)
    return names
