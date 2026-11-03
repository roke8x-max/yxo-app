from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import EmailType, CODE_NUM_RE, NON_AUTO_DRAFT_CATEGORIES
from .log import get_logger

_log = get_logger(__name__)


def _split_code(code: str) -> Tuple[str, str]:
    """Split 客户编码 into (stem_digits, dest_suffix).

    stem = digits after CQWLJT (the 序号); suffix = part after '-' (到站后缀),
    '' when the code has no '-'. Codes not matching CQWLJT<digits> fall back
    to (code, '') so they only match themselves.
    """
    if not code:
        return "", ""
    m = CODE_NUM_RE.match(code)
    stem = m.group(1) if m else code
    suffix = code.rsplit("-", 1)[1] if "-" in code else ""
    return stem, suffix


@dataclass
class Decision:
    tier: str
    action: str
    reason: str
    notify_type: Optional[str] = None


def decide_draft(row, routing, records) -> Decision:
    if row.draft_category in NON_AUTO_DRAFT_CATEGORIES:
        return Decision("MANUAL", "skip", f"Non-auto category: {row.draft_category}")

    code = row.customer_code or ""
    box = row.container_no or ""

    if code:
        # T1: exact customer-code hit (recipients resolved)
        if routing.success and any(r.get("客户编码") == code for r in records):
            return Decision("T1", "forward", "Exact customer code match")

        # T1N: 数字段命中且排除退舱后公司唯一（route_draft 的 num 分支已判过唯一）
        # 🔴 必须留在 T2/T7 之前：同数字段同箱不同后缀的邮件会先被 T2 截成 pending，
        #    这正是对齐前 #45 放宽失效的根因。
        if getattr(routing, "route_source", "") == "num_match" and routing.success:
            return Decision("T1N", "forward", "Customer code stem match with unique company")

        # Stem analysis on the active subset (records are pre-filtered by the
        # scope rule: 状态<>'退舱' AND is_deleted=0)
        stem, suffix = _split_code(code)
        stem_rows = [
            r for r in records
            if stem and _split_code(r.get("客户编码", ""))[0] == stem
        ]
        if not stem_rows:
            return Decision("T4", "alarm", "Customer code stem not in active records")

        # T2: same stem + same box, different destination suffix
        if box:
            same_box = [r for r in stem_rows if (r.get("箱号") or "") == box]
            if same_box and any(
                _split_code(r.get("客户编码", ""))[1] != suffix for r in same_box
            ):
                return Decision("T2", "pending", "Same stem and box, different destination suffix")

        # T7: defensive guard — one stem spans >=2 active suffixes
        suffixes = {_split_code(r.get("客户编码", ""))[1] for r in stem_rows}
        if len(suffixes) >= 2:
            return Decision("T7", "pending", "Stem spans multiple active suffixes")

        # T3: stem hit but box mismatch/empty
        return Decision("T3", "pending", "Stem hit but box mismatch or empty")

    # Codeless branch: never enters T1-T3
    if not box:
        return Decision("T0", "alarm", "No customer code and no box")
    box_matches = [r for r in records if (r.get("箱号") or "") == box]
    if len(box_matches) == 0:
        return Decision("T0", "alarm", "No customer code and box has no hit")
    if len(box_matches) == 1:
        return Decision("T5", "alarm", "No customer code but unique box match")
    return Decision("T6", "pending", "Box number reused by multiple records")


def decide_waybill(row, routing, records) -> Decision:
    # 🔴 必须在委托 decide_draft 之前：否则带编码的 waybill_rejected 会命中 T1
    #    ⇒ action=forward ⇒ 把「单证驳回」邮件转发给【客户】。
    if getattr(row, "waybill_rejected", False):
        return Decision("waybill_rejected", "skip", "Rejected document: no action by design")
    # Waybill shares the draft eight-tier engine (客编+箱号 keys).
    return decide_draft(row, routing, records)


def decide_dsk(row, routing, records) -> Decision:
    # Reused box (route_dsk marks box_reuse) alarms instead of forwarding
    # to the first company it happens to hit.
    if getattr(routing, "route_source", "") == "box_reuse":
        return Decision("T6", "alarm", "Box number reused by multiple records (>=2)")

    if routing.success:
        return Decision("T1", "forward", "Unique box match")

    if row.container_no:
        box_matches = [r for r in records if r["箱号"] == row.container_no]
        if len(box_matches) >= 2:
            return Decision("T6", "alarm", "Box number reused by multiple records (>=2)")

    return Decision("T0", "alarm", "Box not found in active records")


def decide_atb(row, routing, records) -> Decision:
    return decide_dsk(row, routing, records)


def decide_tracing(row, routing, records) -> Decision:
    # 保守修：接受两种形态（serve 对 list 做多公司扇出、单个 RoutingResult 直接下发）。
    # 不得破坏多公司扇出（命中 N 家 → N 次转发）。
    if isinstance(routing, list):
        if routing:
            return Decision("TRACE_HIT", "forward", f"Matched {len(routing)} companies via train")
        else:
            return Decision("TRACE_MISS", "skip", "No company matched for this train")
    if getattr(routing, "success", False):
        return Decision("TRACE_HIT", "forward", f"Matched {getattr(routing, 'company', '')} via train")
    return Decision("TRACE_MISS", "skip", "No company matched for this train")


_TRAIN_TYPE_POOL = "专列"


def _train_type(records, row) -> str:
    """专列/散舱：字段优先，客编数 >40 兜底，都拿不到按散舱（保守）。

    🔴 兜底算出结果后**必须写回 row.raw_data["train_type"]**。
    下游 spec 03 的聚合器直接读 raw_data["train_type"] 决定「进池还是直发」，
    不写回的话兜底路径等于白算：decide 里发了 pool，聚合器却按散舱处理。
    """
    t = ((row.raw_data or {}).get("train_type") or "").strip()
    if t in ("专列", "散舱"):
        # 走「字段优先」这一支也要把 strip 后的值写回。
        # route 阶段若带进 "专列 "（尾随空格），这里 return "专列" 而 raw_data
        # 仍是 "专列 "，下游 spec03 用 == "专列" 比较会 False ⇒ 专列当散舱。
        row.raw_data["train_type"] = t
        return t

    tn = getattr(row, "train_no", "") or ""
    if tn:
        # 🔴 用 _split_code（非标准编码各自当独立客编），不能用 routing._stem：
        #    后者对非 CQWLJT 编码一律返回 ""，会把不同客编塌成同一个、去重数偏小
        #    ⇒ 专列被判成散舱 ⇒ 该合的不合。
        n = len({_split_code(r.get("客户编码", ""))[0]
                 for r in records if r.get("班列号") == tn})
        result = "专列" if n > 40 else "散舱"
    else:
        result = "散舱"

    row.raw_data["train_type"] = result      # ← 关键：落回 raw_data
    return result


def decide_release(row, routing, records) -> Decision:
    src = getattr(routing, "route_source", "")

    if not routing.success:
        # 分三种，排查方向完全不同：
        #   ① company 非空 ⇒ bot_config 没配收件人
        #   ② 数字段命中了行但有歧义 ⇒ 编码/数据问题，不是"查无"
        #   ③ 真的查无此编码
        if routing.company:
            return Decision("R0", "alarm",
                            f"No recipients: company={routing.company} src={src}")
        if src in ("num_multi_company", "num_no_company"):
            return Decision("R0", "alarm", f"Stem ambiguous: {src}")
        return Decision("R0", "alarm", f"No route: {src}")

    # 业务上"重复到达照常发"，所以这里不拦——去重交给 spec 03 的批次内去重
    train_type = _train_type(records, row)
    action = "pool" if train_type == _TRAIN_TYPE_POOL else "forward"

    if src == "code_exact":
        return Decision("R1", action, f"Exact code match | train_type={train_type}")

    # R1N：只靠数字段命中，有串到「同数字段、不同后缀」那张票的风险。
    # log_decide() 只打 INFO，必须额外 WARN 一次，否则取消件静默串号没人发现。
    _log.warning(
        f"R1N stem match | code={row.customer_code} code_num={row.customer_code_num} "
        f"company={routing.company} src={src} train={getattr(row, 'train_no', '')}"
    )
    return Decision("R1N", action, f"Stem match | train_type={train_type}")


def decide_retbox(row, routing, records) -> Decision:
    src = getattr(routing, "route_source", "")

    if not routing.success:
        # 和 decide_release 同样的三分法（B6 是箱号复用，单独一档）
        if src == "box_reuse":
            return Decision("B6", "alarm", "Box reused")
        if routing.company:
            return Decision("B0", "alarm",
                            f"No recipients: company={routing.company} src={src}")
        if src in ("num_multi_company", "num_no_company"):
            return Decision("B0", "alarm", f"Stem ambiguous: {src}")
        return Decision("B0", "alarm", f"No route: {src}")

    train_type = _train_type(records, row)
    action = "pool" if train_type == _TRAIN_TYPE_POOL else "forward"

    if src == "code_exact":
        return Decision("B1", action, f"Exact code match | train_type={train_type}")
    if src == "code_num":
        _log.warning(
            f"B1N stem match | code={row.customer_code} code_num={row.customer_code_num} "
            f"company={routing.company} src={src} train={getattr(row, 'train_no', '')}"
        )
        return Decision("B1N", action, f"Stem match | train_type={train_type}")

    # B2：老模板没有客编，只能靠箱号唯一定位。箱号的语义比客编弱（同一箱会跑多趟），
    # 单看箱子就对上了必须留痕，方便事后核对。
    _log.warning(
        f"B2 box-only match | box={row.container_no} "
        f"company={routing.company} src={src} train={getattr(row, 'train_no', '')}"
    )
    return Decision("B2", action, f"Box-only match | train_type={train_type}")


def decide(email_type, row, routing, records) -> Decision:
    # Accept both EmailType enum and string
    if isinstance(email_type, str):
        try:
            email_type = EmailType(email_type)
        except ValueError:
            return Decision("UNKNOWN", "skip", f"Unknown email type: {email_type}")
    
    if email_type == EmailType.DRAFT:
        return decide_draft(row, routing, records)
    elif email_type == EmailType.WAYBILL:
        return decide_waybill(row, routing, records)
    elif email_type == EmailType.DSK:
        return decide_dsk(row, routing, records)
    elif email_type == EmailType.ATB:
        return decide_atb(row, routing, records)
    elif email_type == EmailType.TRACING:
        return decide_tracing(row, routing, records)
    elif email_type == EmailType.RELEASE:
        return decide_release(row, routing, records)
    elif email_type == EmailType.RELEASE_CANCEL:
        return decide_release(row, routing, records)
    elif email_type == EmailType.RETBOX:
        return decide_retbox(row, routing, records)
    else:
        return Decision("UNKNOWN", "skip", f"Unknown email type: {email_type}")