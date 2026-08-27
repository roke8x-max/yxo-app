# -*- coding: utf-8 -*-
"""统一匹配器（spec §4）：客编优先、箱号辅助；active 铁律过滤；
退舱/软删仅命中→T4(cancelled_only)。判定顺序显式化（2026-08-26 拍板）。"""
import re
from core.models import MatchResult

CODE_RE = re.compile(r"^(?P<prefix>[A-Za-z]+)(?P<seq>\d+)(?:-(?P<suffix>.+))?$")
BOX_RE = re.compile(r"^[A-Z]{4}\d{7}$")

CANCELLED_STATUS = "退舱"


def parse_code(token):
    m = CODE_RE.match((token or "").strip())
    if not m:
        return None
    return {"prefix": m.group("prefix").upper(),
            "seq": m.group("seq"),
            "suffix": (m.group("suffix") or "").upper()}


def is_iso_box(token):
    return bool(BOX_RE.match((token or "").strip().upper()))


def norm_box(b):
    return (b or "").strip().upper()


def is_client_code_candidate(token, idx):
    """消歧（spec §4）：ISO 箱形态一律判箱号；否则需 prefix 在已知白名单或有后缀。"""
    tok = (token or "").strip()
    if not tok:
        return False
    if is_iso_box(tok):
        return False
    p = parse_code(tok)
    if p is None:
        return False
    return (p["prefix"] in idx.known_prefixes) or bool(p["suffix"])


class ActiveIndex:
    """内存三索引 + 全量兜底索引 + prefix 白名单。"""

    def __init__(self):
        self.by_full_code = {}     # 完整客编(upper) -> row
        self.by_seq = {}           # seq -> [row]           仅 active
        self._full_by_seq = {}     # seq -> [row]           含退舱/软删
        self.by_box = {}           # box(upper) -> [row]    仅 active
        self.known_prefixes = set()

    # -- 查询 --
    def get_active_by_full_code(self, code):
        return self.by_full_code.get((code or "").strip().upper())

    def active_by_seq(self, seq):
        return self.by_seq.get(seq or "", [])

    def full_by_seq(self, seq):
        return self._full_by_seq.get(seq or "", [])

    def active_by_box(self, box):
        return self.by_box.get(norm_box(box), [])


def build_index(rows):
    idx = ActiveIndex()
    for r in rows:
        p = parse_code(r.get("code", ""))
        active = (not r.get("deleted")) and (r.get("status", "") != CANCELLED_STATUS)
        if p:
            idx.known_prefixes.add(p["prefix"])
            key_full = (r.get("code") or "").strip().upper()
            if active and key_full and key_full not in idx.by_full_code:
                idx.by_full_code[key_full] = r
            idx._full_by_seq.setdefault(p["seq"], []).append(r)
            if active:
                idx.by_seq.setdefault(p["seq"], []).append(r)
        b = norm_box(r.get("box", ""))
        if b and active:
            idx.by_box.setdefault(b, []).append(r)
    return idx


def classify_match(code, box, idx):
    """spec §4 显式顺序：有码走 T1→T2/T7/T3→T4；无码走 T0/T5/T6。"""
    code = (code or "").strip()
    box_n = norm_box(box)

    if code:
        rec = idx.get_active_by_full_code(code)
        if rec:
            return MatchResult("T1", "exact", rec, ())
        p = parse_code(code)
        seq = p["seq"] if p else ""
        actives = idx.active_by_seq(seq) if seq else []
        if actives:
            in_suffix = p["suffix"]
            same_box_diff_suffix = [
                r for r in actives
                if box_n
                and norm_box(r.get("box", "")) == box_n
                and (parse_code(r.get("code", "")) or {}).get("suffix", "") != in_suffix
            ]
            if same_box_diff_suffix:
                return MatchResult("T2", "suffix_mismatch", same_box_diff_suffix[0],
                                   tuple(actives))
            suffixes = {(parse_code(r.get("code", "")) or {}).get("suffix", "")
                        for r in actives}
            if len(suffixes) >= 2:
                return MatchResult("T7", "multi_suffix", actives[0], tuple(actives))
            return MatchResult("T3", "box_mismatch", actives[0], tuple(actives))
        # active 未命中 → 查全量（含退舱/软删）区分 unknown / cancelled_only
        if seq and idx.full_by_seq(seq):
            return MatchResult("T4", "cancelled_only", None, ())
        return MatchResult("T4", "unknown", None, ())

    # 无客编分支（绝不进 T1–T3）
    hits = idx.active_by_box(box_n) if box_n else []
    if not hits:
        return MatchResult("T0", "nothing", None, ())
    if len(hits) == 1:
        return MatchResult("T5", "unique_box_only", hits[0], tuple(hits))
    return MatchResult("T6", "reused_box", hits[0], tuple(hits))
