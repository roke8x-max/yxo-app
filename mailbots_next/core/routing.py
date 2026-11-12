from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import EmailType, CODE_NUM_RE
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
    # 只按客编唯一命中（邮件没提取到箱号）。必须显式登记：
    # `.get(level, "box_match")` 的默认值会让漏登记的档位静默变成 box_match，
    # 事后从日志里看不出「这封是客编单判」。
    boxmatch.LV_CODE_ONLY: "code_only",
    # 本任务后 match_atb / match_dsk 都不再返回 multi，此映射保留仅为兜底
    # （decide_dsk 的 T6 因此端到端不可达，见 02_TASK 1.0 总表）。
    boxmatch.LV_MULTI: "box_reuse",
}


def _result_from(level, rec) -> RoutingResult:
    """boxmatch 档位 → RoutingResult。

    ⚠️ 只接收 `match_atb` / `match_dsk` 的档位（code_box / code_only / train_box /
       miss）。草单的 `num_unique` **不走这里**，走 `route_draft` 的单独分支
       （产出 `num_match`）——传进来会落到 `box_match` 兜底，与
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
    """草单/运单：客编（或序号段）**且**箱号，同一条记录命中才算路由成功。

    判定顺序（任务 17 写死）：
      ① 客编精确 —— 邮件**有箱号**时还须该记录箱号与邮件箱号一致（组合复核）；
      ② 序号段唯一 —— 同上，有箱号时必须与箱号同记录对上；
      ③ 都没有 ⇒ success=False。
    🔴 绝不按箱号定公司（原先「客编未命中 ⇒ 退化按箱号」的分支已删除）。
    """
    code = row.customer_code
    box = (row.container_no or "").strip()
    company = None
    level = "none"

    # ① 客编精确
    rec = next((r for r in records if code and r.get("客户编码") == code), None)
    if rec is not None:
        level = "full"
        # 有箱号 ⇒ 必须与同一条记录的箱号对上；对不上就失败，不退化、不改用别的记录
        if box and (rec.get("箱号") or "").strip() != box:
            return RoutingResult("", None, [], [], "no_route", False)
        company = normalize_company(rec.get("company", ""))

    if not company and row.customer_code_num:
        # #45 对齐：数字段命中且排除退舱后公司唯一，才认这一档。
        # 任务 17：邮件有箱号 ⇒ 序号段档也要与箱号同记录对上（序号段是客编派生键，
        # 不是组合之外的第二条放行路）。校验必须做在 routing 层：decide_draft 的
        # T1N 排在 T3 之前，留到 decide 兜是无效的。
        num_level, num_rec, num_hits = boxmatch.match_draft_num(records, row.customer_code_num)
        if num_level == boxmatch.LV_NUM_UNIQUE and num_rec is not None:
            if box:
                cand = [r for r in num_hits if (r.get("箱号") or "").strip() == box]
                comps = {normalize_company(r.get("company") or "") for r in cand
                         if (r.get("company") or "").strip()}
                num_rec = (next((r for r in cand if (r.get("company") or "").strip()), cand[0])
                           if cand and len(comps) == 1 else None)
            if num_rec is not None:
                company = normalize_company(num_rec.get("company", ""))
                level = "num"

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
    box = (row.container_no or "").strip()
    if not box:
        # 保留基线取值（no_box），不改成 no_route —— 见 REPORT「route_source 取值集合」
        return RoutingResult("", None, [], [], "no_box", False)

    train = (row.train_no or "").strip()
    if not train:
        # 任务 17：缺班列号不许退化成「只按箱号」。单独给 no_train_no，
        # 告警里才能分清「邮件没班列号」和「双键查无」——两者处理方式不同。
        return RoutingResult("", None, [], [], "no_train_no", False)

    level, rec, _c = boxmatch.match_dsk(records, box, train)
    return _result_from(level, rec)


def route_atb(row, records) -> RoutingResult:
    box = (row.container_no or "").strip()
    code = (row.customer_code or "").strip()
    if not box and not code:
        # 箱号与客编都没有：保留基线取值 no_box（等价于原先的「无箱号」语义）
        return RoutingResult("", None, [], [], "no_box", False)

    # 分支由 match_atb 判：无客编 ⇒ miss（不看箱号）；有客编无箱号 ⇒ 客编精确；
    # 有客编有箱号 ⇒ 双键唯一。箱号为空时不再提前失败（否则分支 B 永远走不到）。
    level, rec, _c = boxmatch.match_atb(records, code, box)
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


def _stem(code: str) -> str:
    """取 CQWLJT 后的数字段；不匹配返回空串。

    ⚠️ 与 decide.py 的 `_split_code(code)[0]` **不是一回事**：那边匹配不上时返回整个
    code，这里返回 ""。本文件的 ② 数字段档只认 CQWLJT<数字> 格式，所以必须用这个。
    """
    m = CODE_NUM_RE.match(code or "")
    return m.group(1) if m else ""


def route_release(row, records) -> RoutingResult:
    code = row.customer_code or ""
    box = (row.container_no or "").strip()
    rec, src = None, "no_route"

    # ① 精确
    rec = next((r for r in records if code and r["客户编码"] == code), None)
    if rec is not None:
        src = "code_exact"

    # ② 数字段唯一（公司唯一才算）。任务 17：邮件有箱号时，候选先按箱号过滤，
    #    序号段不许脱离箱号单独放行（与 route_draft 同一口径）。
    if rec is None and getattr(row, "customer_code_num", ""):
        hits = [r for r in records if _stem(r["客户编码"]) == row.customer_code_num]
        if box:
            hits = [r for r in hits if (r.get("箱号") or "").strip() == box]
        companies = {normalize_company(r.get("company", "")) for r in hits if r.get("company")}
        if len(companies) == 1:
            # ⚠️ 不能直接写 hits[0]。数字段相同、后缀不同的两行里，可能有一行
            # 没填公司；那时 companies 仍然只有 1 个非空值，但 hits[0] 恰好是
            # 那条空公司 —— get_recipients("") 返回 ([], [])，success 变 False，
            # 一封本该转发的信就这样变成 R0 告警。
            rec, src = next((r for r in hits if r.get("company")), hits[0]), "code_num"
        elif len(companies) > 1:
            return RoutingResult("", None, [], [], "num_multi_company", False)
        elif hits:
            # 数字段命中了行，但这些行的公司字段全是空 —— 数据异常。
            # 不能落到下面的 no_route，否则和「查无此编码」混为一谈、无从排查。
            return RoutingResult("", None, [], [], "num_no_company", False)

    if rec is None:
        return RoutingResult("", None, [], [], "no_route", False)

    # ③ 组合复核：邮件带箱号 ⇒ 命中记录必须是同一个箱号，否则判失败
    if box and (rec.get("箱号") or "").strip() != box:
        return RoutingResult("", None, [], [], "no_route", False)

    company = normalize_company(rec.get("company", ""))
    to_list, cc_list = get_recipients(company)

    # 回填班列信息（不改任何 dataclass，用既有字段 + raw_data）
    # ⚠️ 一律 strip：库里字段可能带尾随空格，不 strip 会让下游 == "专列" 比较失败
    row.train_no = rec.get("班列号", "") or ""
    row.raw_data["train_type"] = (rec.get("班列类型", "") or "").strip()
    row.raw_data["dest_station"] = rec.get("目的站", "") or ""

    return RoutingResult(
        company=company,
        responsible_person=get_responsible_person(company),
        to_list=to_list, cc_list=cc_list,
        route_source=src,
        success=bool(to_list),
    )


def route_retbox(row, records) -> RoutingResult:
    """与 route_release 同构。任务 17：第三档「箱号唯一兜底」已删除，不按箱号定公司。"""
    code = row.customer_code or ""
    box = (row.container_no or "").strip()
    rec, src = None, "no_route"

    # ① 精确
    rec = next((r for r in records if code and r["客户编码"] == code), None)
    if rec is not None:
        src = "code_exact"

    # ② 数字段唯一（公司唯一才算）；邮件有箱号时先按箱号过滤（组合复核）
    if rec is None and getattr(row, "customer_code_num", ""):
        hits = [r for r in records if _stem(r["客户编码"]) == row.customer_code_num]
        if box:
            hits = [r for r in hits if (r.get("箱号") or "").strip() == box]
        companies = {normalize_company(r.get("company", "")) for r in hits if r.get("company")}
        if len(companies) == 1:
            rec, src = next((r for r in hits if r.get("company")), hits[0]), "code_num"
        elif len(companies) > 1:
            # 客编指向多家公司 ⇒ 本来就该人工确认；不拿更弱的箱号去"救"。
            return RoutingResult("", None, [], [], "num_multi_company", False)
        elif hits:
            return RoutingResult("", None, [], [], "num_no_company", False)

    if rec is None:
        return RoutingResult("", None, [], [], "no_route", False)

    # ③ 组合复核：邮件带箱号 ⇒ 命中记录必须同箱号，否则判失败
    if box and (rec.get("箱号") or "").strip() != box:
        return RoutingResult("", None, [], [], "no_route", False)

    company = normalize_company(rec.get("company", ""))
    to_list, cc_list = get_recipients(company)

    row.train_no = rec.get("班列号", "") or ""
    row.raw_data["train_type"] = (rec.get("班列类型", "") or "").strip()
    row.raw_data["dest_station"] = rec.get("目的站", "") or ""

    return RoutingResult(
        company=company,
        responsible_person=get_responsible_person(company),
        to_list=to_list, cc_list=cc_list,
        route_source=src,
        success=bool(to_list),
    )


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
    elif email_type == EmailType.RELEASE:
        return route_release(row, records)
    elif email_type == EmailType.RELEASE_CANCEL:
        return route_release(row, records)
    elif email_type == EmailType.RETBOX:
        return route_retbox(row, records)
    else:
        return RoutingResult("", None, [], [], "unknown_type", False)