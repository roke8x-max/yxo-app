# -*- coding: utf-8 -*-
"""组合键路由判定（纯函数，无 IO）。

records 每条形如：
    {"id": 1, "客户编码": "CQWLJT260912003-D", "箱号": "FWRU0042785",
     "班列号": "WB864", "company": "保时达", ...}
（store.load_records(active_only=True) 已在 SQL 排除 状态='退舱' 与 is_deleted=1）

⚠️ 本模块与老系统 `mailbots/core/atbroute.py` / `dskroute.py` / `nummatch.py`
   是**两份同源实现**（老系统最终要删，所以不跨包 import）。改一处要改另一处，
   并用同一张测试表对齐结果。
"""
import re

from ..config import CODE_NUM_RE      # settings.py:147 = r'CQWLJT(\d+)'，不本地定义

# 班列号三种写法，统一归一到 "WB<3位>"（与老系统 mailbots/core/dskroute.py 逐字相同）
TRAIN_PATTERNS = (
    re.compile(r"YXO[- ]?(?:20\d{2}[- ]?)?(\d{3})(?!\d)"),       # YXO-223 / YXO 2026-223
    re.compile(r"WB\d{4}\s*[-/]?\s*ATD\d{4}\s*\((\d{3})\)"),     # WB0808-ATD0809(773)
    re.compile(r"\((\d{3})\)"),                                   # 兜底 (773)
)

LV_CODE_BOX = "code_box"      # 客编+箱号唯一命中
LV_TRAIN_BOX = "train_box"    # 箱号+班列号唯一命中
LV_BOX_ONLY = "box_only"      # 只能按箱号唯一命中（兜底）
LV_NUM_UNIQUE = "num_unique"  # 草单：数字段命中且公司唯一
LV_MULTI = "multi"            # 命中多条
LV_MISS = "miss"              # 查不到


def extract_train_number(subject):
    """从主题取班列号，返回 'WB773'；取不到返回 None。

    ⚠️ 放在本模块（不拆到 config/settings.py）：它是纯函数，与判定同源，
       且任务书 §五的自检命令按 `boxmatch.extract_train_number` 调用。
    """
    s = subject or ""
    for pat in TRAIN_PATTERNS:
        m = pat.search(s)
        if m:
            return "WB" + m.group(1)
    return None


def _box(r):
    return (r.get("箱号") or "").strip()


def _code(r):
    return (r.get("客户编码") or "").strip()


def _train(r):
    return (r.get("班列号") or "").strip()


def by_box(records, box):
    return [r for r in records if box and _box(r) == box]


def match_atb(records, code, box):
    """ATB：客编为权威，箱号必需。返回 (level, record, candidates)。

    🔴 多条时 record 返回 hits[0]（**不是 None**）——与基线 route_dsk 的
       `company = matches[0]` 一致，否则 box_reuse 的 company 会变成空串。
    """
    code = (code or "").strip()
    box = (box or "").strip()
    if code:
        hits = [r for r in records if _box(r) == box and _code(r) == code]
        if len(hits) == 1:
            return LV_CODE_BOX, hits[0], hits
        if hits:
            return LV_MULTI, hits[0], hits
        # 有客编但组合查不到 → miss，不静默回退到箱号
        return LV_MISS, None, by_box(records, box)
    hits = by_box(records, box)
    if len(hits) == 1:
        return LV_BOX_ONLY, hits[0], hits
    if hits:
        return LV_MULTI, hits[0], hits
    return LV_MISS, None, []


def match_dsk(records, box, train):
    """DSK：班列号为权威标识，箱号必需。多条时同样返回 hits[0]。"""
    box = (box or "").strip()
    train = (train or "").strip()
    if train:
        hits = [r for r in records if _box(r) == box and _train(r) == train]
        if len(hits) == 1:
            return LV_TRAIN_BOX, hits[0], hits
        if hits:
            return LV_MULTI, hits[0], hits
        only = by_box(records, box)
        if len(only) == 1:
            return LV_BOX_ONLY, only[0], only
        if only:
            return LV_MULTI, only[0], only
        return LV_MISS, None, []
    only = by_box(records, box)
    if len(only) == 1:
        return LV_BOX_ONLY, only[0], only
    if only:
        return LV_MULTI, only[0], only
    return LV_MISS, None, []


def match_draft_num(records, code_num):
    """草单：数字段命中。排除退舱后公司唯一才成立（records 已排除退舱）。

    🔴 匹配口径**照抄老系统** `mailbots/core/nummatch.py:91-92`：
       用 `CODE_NUM_RE.match(code).group(1) == num` 精确相等，
       **不要**写 `code.startswith("CQWLJT" + num)` —— 后者会把
       `CQWLJT2609120030`（10 位）误当成 `260912003`（9 位）。
    """
    num = (code_num or "").strip()
    if not num:
        return LV_MISS, None, []
    hits = []
    for r in records:
        c = _code(r)
        m = CODE_NUM_RE.match(c)
        if m and m.group(1) == num:
            hits.append(r)
    if not hits:
        return LV_MISS, None, []
    comps = {(r.get("company") or "").strip() for r in hits
             if (r.get("company") or "").strip()}
    if len(comps) == 1:
        return LV_NUM_UNIQUE, hits[0], hits
    return LV_MULTI, hits[0], hits
