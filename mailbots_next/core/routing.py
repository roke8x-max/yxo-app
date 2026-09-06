from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import EmailType
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
        for r in records:
            if r["客户编码"].startswith("CQWLJT"):
                import re
                m = re.match(r'CQWLJT(\d+)', r["客户编码"])
                if m and m.group(1) == row.customer_code_num:
                    company = normalize_company(r.get("company", ""))
                    level = "num"
                    break

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
        return RoutingResult("", None, [], [], "no_box", False)

    matches = [r for r in records if r["箱号"] == box]
    if len(matches) >= 2:
        # Box reused across bookings: stay on the success path with an
        # explicit marker so decide_dsk can land the T6 alarm instead of
        # auto-forwarding to the first company.
        company = normalize_company(matches[0].get("company", ""))
        responsible = get_responsible_person(company)
        to_list, cc_list = get_recipients(company)
        return RoutingResult(
            company=company,
            responsible_person=responsible,
            to_list=to_list,
            cc_list=cc_list,
            route_source="box_reuse",
            success=True,
        )

    company = None
    for r in matches:
        company = normalize_company(r.get("company", ""))
        break

    if company:
        responsible = get_responsible_person(company)
        to_list, cc_list = get_recipients(company)
        if to_list:
            return RoutingResult(
                company=company,
                responsible_person=responsible,
                to_list=to_list,
                cc_list=cc_list,
                route_source="box_match",
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


def route_atb(row, records) -> RoutingResult:
    return route_dsk(row, records)


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


def route_row(email_type: EmailType, row, records) -> RoutingResult:
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