#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""records 查询统一入口（spec §8）：所有 records 读操作经此注入 scope。

scope ∈ { all, companies:[...], none }（spec §3.2/§3.3 铁律）：
  all → 不过滤；companies → 按「开票子公司名称」过滤（真实生效）；
  none/空/缺失/配置丢失 → 空结果，绝不全量。

公司名归一化：去首尾空格、全角→半角（NFKC）、大小写折叠，两端统一后再比对。
"""
import unicodedata

import config

COMPANY_FIELD = "开票子公司名称"


def normalize_company(name):
    return unicodedata.normalize("NFKC", str(name or "")).strip().casefold()


def resolve_scope(identity):
    """identity 为 None / scope 未设定 → ('none', [])，绝不全量。"""
    if identity is None:
        return "none", []
    st = (getattr(identity, "scope_type", "") or "").strip().lower()
    if st == "all":
        return "all", []
    if st == "companies":
        raw = getattr(identity, "companies", []) or []
        normed = {normalize_company(c) for c in raw if str(c).strip()}
        normed.discard("")
        if not normed:
            return "none", []
        return "companies", normed
    return "none", []


def _in_scope(company_value, scope):
    st, payload = scope
    if st == "all":
        return True
    if st == "companies":
        return normalize_company(company_value) in payload
    return False


def _filter_rows(rows, scope, company_key=COMPANY_FIELD):
    if scope[0] == "all":
        return rows
    return [r for r in rows if _in_scope(dict(r).get(company_key, ""), scope)]


def _base_rows(conn, extra=""):
    flds = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    return conn.execute(
        f'SELECT id, seq, order_idx, {flds}, updated_at, updated_by FROM records '
        f'WHERE COALESCE(is_deleted,0)=0 {extra} ORDER BY order_idx, id').fetchall()


def list_records(conn, identity, train_type=""):
    """主数据读取（/api/rows）：scope 过滤 + 可选班列类型。"""
    scope = resolve_scope(identity)
    if scope[0] == "none":
        return []
    rows = _base_rows(conn)
    if train_type in ("散舱", "专列"):
        rows = [r for r in rows if (dict(r).get("班列类型") or "") == train_type]
    return _filter_rows(rows, scope)


def list_trash(conn, identity):
    flds = ", ".join([f'"{f}"' for f in config.ALL_FIELDS])
    scope = resolve_scope(identity)
    if scope[0] == "none":
        return []
    rows = conn.execute(
        f'SELECT id, seq, {flds}, deleted_at, deleted_by FROM records '
        f'WHERE COALESCE(is_deleted,0)=1 ORDER BY deleted_at DESC').fetchall()
    return _filter_rows(rows, scope)


def filter_export_rows(rows, identity):
    """导出：前端传来的行在服务端按 scope 再过滤（防伪造 rows 偷数据）。"""
    scope = resolve_scope(identity)
    if scope[0] == "all":
        return rows
    if scope[0] == "none":
        return []
    return [r for r in (rows or [])
            if normalize_company((r or {}).get(COMPANY_FIELD, "")) in scope[1]]


def train_summary_rows(all_rows, identity):
    """专列聚合的 scope 过滤（Python 侧归一化比对）。"""
    scope = resolve_scope(identity)
    if scope[0] == "none":
        return []
    return _filter_rows(all_rows, scope)
