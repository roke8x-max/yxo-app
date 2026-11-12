# -*- coding: utf-8 -*-
"""组合键路由判定（纯函数，无 IO）。

records 每条形如：
    {"id": 1, "客户编码": "CQWLJT260912003-D", "箱号": "FWRU0042785",
     "班列号": "WB864", "company": "保时达", ...}
（store.load_records(active_only=True) 已在 SQL 排除 状态='退舱' 与 is_deleted=1）

⚠️ 本模块与老系统 `mailbots/core/atbroute.py` / `dskroute.py` / `nummatch.py`
   是**两份同源实现**（老系统最终要删，所以不跨包 import）。改一处要改另一处，
   并用同一张测试表对齐结果。
   ⚠️ 唯一例外（任务 17）：班列号 3/4 位支持只改新系统，老系统仍只认 3 位，
      两边**刻意不一致**，详见下方 `TRAIN_PATTERNS` 上方注释。
"""
import re

from ..config import CODE_NUM_RE      # settings.py:147 = r'CQWLJT(\d+)'，不本地定义

# 班列号三种写法，统一归一到 "WB<3 或 4 位>"（与 records.班列号 精确相等）。
#
# 🔴 刻意与老系统不一致：新系统已支持 3 位与 4 位班列号（生产库确有 WB10xx 四位），
#    老系统 `mailbots/core/dskroute.py` 仍只认 3 位。本任务只改新系统，老系统一行不动，
#    所以这条差异**只写在这里**，不要回写到老系统文件里。
#
# 🔴 匹配优先级写死（顺序即优先级）：① 括号老格式 (773) → ② YXO-... → ③ 裸 WB...
#    ① 必须最先，否则 `WB0808-ATD0809(773)` 会被 ③ 的 4 位规则抢成 WB0808。
#    ③ 的负向断言 `(?!\s*[-/]?\s*ATD)`：`WB0821 ATD0823 ...` 这种「箱号+ATD 日期」对
#    在基线里就**不是**班列号（老规则只有带括号时才认），保持取不到，避免放宽。
TRAIN_PATTERNS = (
    re.compile(r"\((\d{3})\)"),                                   # ① 老格式 (773)
    re.compile(r"YXO[- ]?(?:20\d{2}[- ]?)?(\d{3,4})(?!\d)"),      # ② YXO-919 / YXO-2026-1032
    re.compile(r"WB(\d{3,4})(?!\d)(?!\s*[-/]?\s*ATD)"),           # ③ 裸 WB1032 / WB919
)

LV_CODE_BOX = "code_box"      # 客编+箱号唯一命中
LV_CODE_ONLY = "code_only"    # 只按客编唯一命中（邮件没提取到箱号时才有此档）
LV_TRAIN_BOX = "train_box"    # 箱号+班列号唯一命中
LV_NUM_UNIQUE = "num_unique"  # 草单：数字段命中且公司唯一
LV_MULTI = "multi"            # 命中多条（现存唯一用途：match_draft_num 跨公司）
LV_MISS = "miss"              # 查不到 / 组合键不成立


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


def match_atb(records, code, box):
    """ATB：客编+箱号 组合键复核。返回 (level, record, candidates)。

    三个分支（任务 17 写死，禁止任何「只按箱号」的放行）：
      A 无客编               ⇒ LV_MISS，不看箱号；
      B 有客编、邮件无箱号   ⇒ 客编精确唯一命中 ⇒ LV_CODE_ONLY；
      C 有客编、邮件有箱号   ⇒ 客编+箱号 同一条记录唯一命中 ⇒ LV_CODE_BOX，否则 LV_MISS。

    🔴 分支 C 优先于「客编可单独判定」：邮件提取到箱号就必须双键对上，
       否则「复核判定」形同虚设。
    🔴 不再有多条时「猜第一条」的档位：多命中按失败处理。
    """
    code = (code or "").strip()
    box = (box or "").strip()
    if not code:
        return LV_MISS, None, []                     # 分支 A
    if not box:
        hits = [r for r in records if _code(r) == code]
        if len(hits) == 1:
            return LV_CODE_ONLY, hits[0], hits       # 分支 B
        return LV_MISS, None, hits
    hits = [r for r in records if _code(r) == code and _box(r) == box]
    if len(hits) == 1:
        return LV_CODE_BOX, hits[0], hits            # 分支 C
    return LV_MISS, None, hits


def match_dsk(records, box, train):
    """DSK：箱号+班列号 双键唯一命中，只返回 LV_TRAIN_BOX / LV_MISS。

    🔴 不许退化成只按箱号（任务 17）：缺班列号、双键 0 条、双键多条
       一律 LV_MISS ⇒ 上层 success=False ⇒ 报警，不猜、不取第一条。
    """
    box = (box or "").strip()
    train = (train or "").strip()
    if not box or not train:
        return LV_MISS, None, []
    hits = [r for r in records if _box(r) == box and _train(r) == train]
    if len(hits) == 1:
        return LV_TRAIN_BOX, hits[0], hits
    return LV_MISS, None, hits


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
