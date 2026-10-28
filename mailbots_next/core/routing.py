from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import EmailType
from . import boxmatch
from .store import (
    load_records,
    get_responsible_person,
    get_recipients,
    get_train_companies,
    get_company_default_recipients,
    COMPANY_ALIAS,
)
from .log import get_logger

_log = get_logger(__name__)


@dataclass
class RoutingResult:
    company: str
    responsible_person: Optional[str]
    to_list: List[str]
    cc_list: List[str]
    route_source: str
    success: bool


def normalize_company(company: str) -> str:
    return COMPANY_ALIAS.get(company, company)


# 档位 → route_source。用 boxmatch 的常量做键，不写字面量：
# 契约是常量的值，不是 "multi"/"miss" 这几个字符串本身。
_SRC_BY_LEVEL = {
    boxmatch.LV_MISS: "no_route",
    boxmatch.LV_MULTI: "box_reuse",
}


def _result_from(level, rec) -> RoutingResult:
    """boxmatch 档位 → RoutingResult。

    ⚠️ 只接收 `match_atb` / `match_dsk` 的档位（code_box / train_box / box_only /
       multi / miss）。草单的 `num_unique` **不走这里**，走 `route_draft` 的
       单独分支（产出 `num_match`）——传进来会落到 `box_match` 兜底，与
       `decide_draft` 失配。

    🔴 box_reuse 必须保持 success=True 且 company 取自 rec（= hits[0]）：
       基线 route_dsk:96-110 就是这样（`company = matches[0]` + success=True），
       改成 False/空串会让 act 拿到的数据与基线不同。
    """
    src = _SRC_BY_LEVEL.get(level, "box_match")
    if rec is None:                                  # 只剩 miss 走这里
        return RoutingResult("", None, [], [], src, False)
    company = normalize_company(rec.get("company", ""))
    to_list, cc_list = get_recipients(company)
    return RoutingResult(company=company,
                         responsible_person=get_responsible_person(company),
                         to_list=to_list, cc_list=cc_list,
                         route_source=src,
                         success=bool(to_list))


def route_draft(row, records) -> RoutingResult:
    code = row.customer_code
    box = row.container_no
    company = None
    level = "none"

    for r in records:
        if code and r["客户编码"] == code:
            company = normalize_company(r.get("company", ""))
            level = "full"
            break

    if not company and row.customer_code_num:
        # #45 对齐：数字段命中且排除退舱后公司唯一，才认这一档。
        # 跨公司/全无公司时不再取第一个命中，交给后面的箱号分支或 no_route。
        num_level, num_rec, _c = boxmatch.match_draft_num(records, row.customer_code_num)
        if num_level == boxmatch.LV_NUM_UNIQUE and num_rec is not None:
            company = normalize_company(num_rec.get("company", ""))
            level = "num"

    if not company and box:
        matches = [r for r in records if r["箱号"] == box]
        if len(matches) == 1:
            company = normalize_company(matches[0].get("company", ""))
            level = "box"
        elif len(matches) > 1:
            company = normalize_company(matches[0].get("company", ""))
            level = "box_multi"

    if company:
        responsible = get_responsible_person(company)
        to_list, cc_list = get_recipients(company)
        if to_list:
            return RoutingResult(
                company=company,
                responsible_person=responsible,
                to_list=to_list,
                cc_list=cc_list,
                route_source=f"{level}_match",
                success=True,
            )

    return RoutingResult(
        company=company or "",
        responsible_person=None,
        to_list=[],
        cc_list=[],
        route_source="no_route",
        success=False,
    )


def route_waybill(row, records) -> RoutingResult:
    return route_draft(row, records)


def route_dsk(row, records) -> RoutingResult:
    box = row.container_no
    if not box:
        # 保留基线取值（no_box），不改成 no_route —— 见 REPORT「route_source 取值集合未变」
        return RoutingResult("", None, [], [], "no_box", False)

    level, rec, _c = boxmatch.match_dsk(records, box, row.train_no)
    return _result_from(level, rec)


def route_atb(row, records) -> RoutingResult:
    box = row.container_no
    if not box:
        # 基线 route_atb → route_dsk，箱号为空时同样是 no_box；此处等价保留
        return RoutingResult("", None, [], [], "no_box", False)

    level, rec, _c = boxmatch.match_atb(records, row.customer_code, box)
    return _result_from(level, rec)


def route_tracing(row, records) -> RoutingResult:
    train_no = row.train_no
    if not train_no:
        return RoutingResult("", None, [], [], "no_train_no", False)

    companies = get_train_companies(train_no)
    if not companies:
        seen = set()
        for r in records:
            if r["班列号"] in (f"WB{train_no}", train_no) or r["班列号"].endswith(train_no):
                comp = normalize_company(r.get("company", ""))
                if comp and comp not in seen:
                    seen.add(comp)
                    companies.append(comp)

    if not companies:
        return RoutingResult("", None, [], [], "no_company_match", False)

    results = []
    for comp in companies:
        to_list, cc_list = get_recipients(comp)
        if to_list:
            responsible = get_responsible_person(comp)
            results.append(RoutingResult(
                company=comp,
                responsible_person=responsible,
                to_list=to_list,
                cc_list=cc_list,
                route_source="train_match",
                success=True,
            ))

    if not results:
        return RoutingResult("", None, [], [], "no_route", False)

    return results


def route_row(email_type: EmailType, row, records):  # 可能返回 RoutingResult 或 list[RoutingResult]（tracing 多公司扇出）
    """可能返回 list[RoutingResult]（tracing 命中多家 → 每家一封副本）。"""
    if email_type == EmailType.DRAFT:
        return route_draft(row, records)
    elif email_type == EmailType.WAYBILL:
        return route_waybill(row, records)
    elif email_type == EmailType.DSK:
        return route_dsk(row, records)
    elif email_type == EmailType.ATB:
        return route_atb(row, records)
    elif email_type == EmailType.TRACING:
        return route_tracing(row, records)
    else:
        return RoutingResult("", None, [], [], "unknown_type", False)