# -*- coding: utf-8 -*-
"""DSK 邮件路由判定（纯函数模块，零副作用）。

为什么单独成模块：
    Dsk_Robot.py 在 import 期就会 sys.path.insert + from config import（读生产配置）、
    末尾 from config_local import STAMP_TOKEN，单元测试无法直接 import 它。
    本模块不读环境变量、不建库、不发网络请求。

判定顺序（班列号为权威标识，箱号为必需）：
    1. 有班列号 + (箱号, 班列号) 唯一命中      → level=train_box → forward
    2. 有班列号 + 组合查不到 + 箱号唯一        → level=box_only  → forward（warn）
    3. 有班列号 + 组合查不到 + 箱号多条        → level=multi     → alert
    4. 无班列号 + 箱号唯一                     → level=box_only  → forward（warn）
    5. 无班列号 + 箱号多条                     → level=multi     → alert
    6. 箱号在库内查不到                        → level=miss      → alert
"""
import re
import sqlite3

# 班列号三种写法（按优先级），统一归一到 "WB<3位数字>"
TRAIN_PATTERNS = (
    re.compile(r"YXO[- ]?(?:20\d{2}[- ]?)?(\d{3})(?!\d)"),          # YXO-223 / YXO 2026-223
    re.compile(r"WB\d{4}\s*[-/]?\s*ATD\d{4}\s*\((\d{3})\)"),        # WB0808-ATD0809(773)
    re.compile(r"\((\d{3})\)"),                                     # 兜底：(773)
)

# 箱号：4 大写字母 + 7 数字（与 Dsk_Robot.CONTAINER_RE 一致）
BOX_RE = re.compile(r"\b[A-Z]{4}\d{7}\b")

LV_TRAIN_BOX = "train_box"   # 箱号+班列号唯一命中
LV_BOX_ONLY = "box_only"     # 只能按箱号唯一命中（兜底）
LV_MULTI = "multi"           # 命中多条
LV_MISS = "miss"             # 查不到

ACTION_FORWARD = "forward"
ACTION_ALERT = "alert"


def extract_train(subject):
    """从主题取班列号，返回 'WB773'；取不到返回 ''。"""
    s = subject or ""
    for pat in TRAIN_PATTERNS:
        m = pat.search(s)
        if m:
            return "WB" + m.group(1)
    return ""


def extract_boxes_from_text(text):
    """从任意文本（附件名等）提取箱号，去重保序。"""
    seen = []
    for b in BOX_RE.findall(text or ""):
        if b not in seen:
            seen.append(b)
    return seen


def load_index(db_path):
    """读 records（过滤软删），返回 (by_box_train, by_box)。

    by_box_train: {(箱号, 班列号): [record, ...]}
    by_box:       {箱号: [record, ...]}
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

    by_box_train = {}
    by_box = {}
    for r in rows:
        box = str(r["箱号"] or "").strip()
        if not box:
            continue
        rec = {
            "id": r["id"],
            "code": str(r["客户编码"] or "").strip(),
            "box": box,
            "train": str(r["班列号"] or "").strip(),
            "company": str(r["开票子公司名称"] or "").strip(),
            "status": str(r["状态"] or "").strip(),
        }
        by_box.setdefault(box, []).append(rec)
        if rec["train"]:
            by_box_train.setdefault((box, rec["train"]), []).append(rec)
    return by_box_train, by_box


def resolve(by_box_train, by_box, box, train):
    """返回 (level, record, candidates)。"""
    if train:
        hits = by_box_train.get((box, train), [])
        if len(hits) == 1:
            return LV_TRAIN_BOX, hits[0], hits
        if hits:
            return LV_MULTI, None, hits
        # 组合查不到：看箱号本身是否唯一，唯一才兜底
        only = by_box.get(box, [])
        if len(only) == 1:
            return LV_BOX_ONLY, only[0], only
        if only:
            return LV_MULTI, None, only
        return LV_MISS, None, []

    only = by_box.get(box, [])
    if len(only) == 1:
        return LV_BOX_ONLY, only[0], only
    if only:
        return LV_MULTI, None, only
    return LV_MISS, None, []


def decide(level):
    """档位 → 动作。"""
    if level in (LV_TRAIN_BOX, LV_BOX_ONLY):
        return ACTION_FORWARD
    return ACTION_ALERT


def alert_names(companies, lookup, always=()):
    """候选公司 → 告警收件人名单（去重，末尾补 always）。

    lookup: 公司名 → 真名的函数（生产传 config.company_to_name，测试传桩）。
    always: 始终抄送的姓名（生产传 ("毛骁洋",)）。
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


def plan_email(by_box_train, by_box, container_rows, train, route_lookup):
    """整封预检。返回 (plans, blocked)。

    container_rows: [(box_no, assoc_code), ...]（只需前两列）
    route_lookup:   (box_no, company) -> to_list，用于校验 DEFAULT 路由是否存在
    blocked:        (box_no, level, msg, candidates) 或 None
    plans 元素:     dict(box, assoc, level, rec)

    ⚠️ 预检必须同时校验「组合键唯一」和「有 DEFAULT 路由」两件事 ——
       后者原本在 Dsk_Robot.py:600-617，漏了会让"能定位但没配路由"的邮件
       漏到发送阶段，变成发送失败而不是整封跳过。
    """
    plans = []
    for box_no, assoc_code in container_rows:
        level, rec, cands = resolve(by_box_train, by_box, box_no, train)
        if decide(level) == ACTION_ALERT:
            return plans, (box_no, level, "候选项 %d 条" % len(cands), cands)
        if not route_lookup(box_no, rec["company"]):
            return plans, (box_no, "no_default_route",
                           "公司 '%s' 无 DEFAULT 路由" % (rec["company"] or "未知"),
                           [rec])
        plans.append({"box": box_no, "assoc": assoc_code, "level": level, "rec": rec})
    return plans, None
