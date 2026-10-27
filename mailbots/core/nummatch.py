# -*- coding: utf-8 -*-
"""草单匹配的纯函数集合：records 读取 + 数字段放宽 + 动作决策。

从 Draft_Forward_Robot.py 搬出来的判定逻辑（2026-10-26，任务 03）。

⚠️ 本模块必须保持零副作用：不读环境变量、不连网、不写库、不 import 项目配置。
   原因：Draft_Forward_Robot 在 import 时会定位 ROOT、拉生产 config（含 SMTP 账号），
   且 import draft_pending 会触发其模块级 self_test() 以 rwc 打开真实 pending_queue.db。
   把判定逻辑放在这里，单元测试才能在不碰生产环境的前提下覆盖它。
"""
import re
import sqlite3

CODE_NUM_RE = re.compile(r'CQWLJT(\d+)')

CANCELLED_STATUS = "退舱"
NUM_AUTO_LEVEL = "num_auto"
NUM_AUTO_NO_ROUTE_MSG = "数字段唯一命中, 但该公司没有配置转发路由"

# 软删过滤写在 SQL 里（不是 Python 层 if 过滤）。
# 与 db_write.is_box_cancelled() 的口径对齐；不改这条，软删行会污染客编唯一性与数字段分组。
RECORDS_SQL = ('SELECT id, "客户编码", "箱号", "开票子公司名称", "状态" '
               'FROM records WHERE COALESCE(is_deleted, 0) = 0')


def load_records(db_path):
    """只读打开 yxo.db → [{id, code, box, company, status}]。

    打开/查询失败直接抛异常，由调用方决定怎么记（机器人侧 catch 后返回空列表）。
    """
    rows = []
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for r in conn.execute(RECORDS_SQL):
            rows.append({
                "id": r["id"],
                "code": str(r["客户编码"] or "").strip(),
                "box": str(r["箱号"] or "").strip(),
                "company": str(r["开票子公司名称"] or "").strip(),
                "status": str(r["状态"] or "").strip(),
            })
    finally:
        conn.close()
    return rows


def num_group_company(hits):
    """数字段分组 → (company 或 None, 理由)。

    理由 ∈ {"unique", "all_cancelled", "cross_company", "no_company"}。
    排除退舱后按公司去重：唯一才返回公司名。空公司必须过滤掉，
    否则空串会被当成一家"公司"，污染唯一性判断。
    """
    cand = [r for r in hits if r.get("status") != CANCELLED_STATUS]
    if not cand:
        return None, "all_cancelled"
    comps = {r["company"] for r in cand if r["company"]}
    if len(comps) == 1:
        return comps.pop(), "unique"
    return None, ("cross_company" if len(comps) > 1 else "no_company")


def resolve_num_match(hits, relax_enabled):
    """数字段命中后的判定（hits 非空由调用方保证）。

    返回 (level, rec)，level ∈ {NUM_AUTO_LEVEL, "num"}。
    放宽时 rec 必须是**非退舱且有公司**的那条 —— 不能返回 hits[0]，
    因为 hits[0] 可能是退舱行（id 更小），会让 decide_action 判成 cancel。
    """
    if relax_enabled:
        company, _why = num_group_company(hits)
        if company:
            rec = next(r for r in hits
                       if r.get("status") != CANCELLED_STATUS and r.get("company") == company)
            return NUM_AUTO_LEVEL, rec
    return "num", hits[0]


def match_record(records, code, num, box, relax_enabled=False):
    """返回 (level, rec)。level: full / num_auto / num / box / box_multi / none。

    ⚠️ relax_enabled 默认 False —— 防御性默认值：本函数是通用工具，
       不显式传参时行为与改动前一致。草单调用点显式传 NUM_RELAX_ENABLED。
    """
    if code:
        hits = [r for r in records if r["code"] == code]
        if hits:
            return "full", hits[0]
    if num:
        hits = [r for r in records if r["code"].startswith("CQWLJT") and
                CODE_NUM_RE.match(r["code"]) and CODE_NUM_RE.match(r["code"]).group(1) == num]
        if hits:
            return resolve_num_match(hits, relax_enabled)
    if box:
        hits = [r for r in records if r["box"] == box]
        if len(hits) == 1:
            return "box", hits[0]
        if len(hits) > 1:
            return "box_multi", hits[0]
    return "none", None


def decide_action(category, level, to_list, rec_status, box_cancelled):
    """等价于现网 Draft_Forward_Robot.py:692-700，谓词一字不改，只是搬进来便于单测。

    rec_status: rec["status"]，无 rec 时传 ""。box_cancelled: 已查好的 is_box_cancelled 结果。
    """
    if rec_status == CANCELLED_STATUS or box_cancelled:
        return "cancel"
    if category in ("C2", "W"):
        return "record"
    if category in ("A", "B", "C1"):
        return "forward" if (level in ("full", NUM_AUTO_LEVEL) and to_list) else "pending"
    return "pending"      # 不可达：classify 只产出 A/B/C1/C2/W
