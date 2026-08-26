# -*- coding: utf-8 -*-
"""统一路由（spec §3 routing / D1/D2）：Layer1 从 records 推导（散舱/专列同链路
读 开票子公司名称=负责公司）；bot_config scope='company' 只管 To/Cc；
scope='train' 为应急覆盖层；空结果必须带 reason（调用方据此 alarm，根治 P3 静默）。"""
import json
import time
from core import matching
from core.models import RouteTarget
from common_io import norm_train_no

RECORD_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_records_code    ON records("客户编码");
CREATE INDEX IF NOT EXISTS idx_records_box     ON records("箱号");
CREATE INDEX IF NOT EXISTS idx_records_train   ON records("班列号");
CREATE INDEX IF NOT EXISTS idx_records_company ON records("开票子公司名称");
"""


def ensure_record_indexes(yxo_conn):
    yxo_conn.executescript(RECORD_INDEXES_SQL)
    yxo_conn.commit()


def load_company_routes(yxo_conn, bot):
    routes = {}
    for r in yxo_conn.execute(
            "SELECT key, to_addrs, cc_addrs FROM bot_config "
            "WHERE bot=? AND scope='company'", (bot,)):
        try:
            routes[r["key"]] = {"to": json.loads(r["to_addrs"] or "[]"),
                                "cc": json.loads(r["cc_addrs"] or "[]")}
        except json.JSONDecodeError:
            routes[r["key"]] = {"to": [], "cc": []}
    return routes


def load_train_overrides(yxo_conn):
    overrides = {}
    for r in yxo_conn.execute(
            "SELECT key, extra FROM bot_config WHERE scope='train'"):
        try:
            comps = (json.loads(r["extra"] or "{}").get("companies") or [])
        except json.JSONDecodeError:
            comps = []
        if comps:
            overrides[norm_train_no(r["key"])] = comps
    return overrides


def _targets_for(companies, routes, reasons):
    out = []
    for comp in companies:
        cfg = routes.get(comp)
        if not cfg or not (cfg.get("to") or cfg.get("cc")):
            reasons.append("no_route_config:" + comp)
            continue
        out.append(RouteTarget(company=comp,
                               to=tuple(cfg.get("to") or ()),
                               cc=tuple(cfg.get("cc") or ())))
    return out


def resolve_recipients(yxo_ro_conn, idx, bot, code="", box="", train_id=""):
    """返回 ([RouteTarget], reason|None)。空列表时 reason 必非空。"""
    reasons = []
    routes = load_company_routes(yxo_ro_conn, bot)

    if train_id:
        train_key = norm_train_no(train_id)
        overrides = load_train_overrides(yxo_ro_conn)
        companies = overrides.get(train_key)
        if not companies:
            companies = [r[0] for r in yxo_ro_conn.execute(
                "SELECT DISTINCT 开票子公司名称 FROM records "
                "WHERE 班列号=? AND COALESCE(is_deleted,0)=0 AND 状态<>'退舱'",
                (train_key,)) if r[0]]
        if not companies:
            return [], "train_no_companies"
        targets = _targets_for(companies, routes, reasons)
        return targets, ("; ".join(reasons) or None)

    result = matching.classify_match(code, box, idx)
    if result.tier == "T1":
        companies = [result.record["company"]] if result.record.get("company") else []
    elif result.tier == "T5":
        companies = sorted({r.get("company", "") for r in result.candidates} - {""})
    else:
        return [], "tier_not_routable:" + result.tier
    if not companies:
        return [], "no_match_records"
    targets = _targets_for(companies, routes, reasons)
    return targets, ("; ".join(reasons) or None)


def _read_data_version(yxo_conn):
    try:
        row = yxo_conn.execute(
            "SELECT value FROM meta_kv WHERE key='data_version'").fetchone()
        return row["value"] if row else "0"
    except Exception:
        return "0"


class RecordIndexProvider:
    """match 内存索引提供器（spec 缓存失效机制）：meta_kv.data_version 变化或 TTL
    到期即重建，防止 yxo_app 导入新订舱后被误判 T4。"""

    def __init__(self, db_path, ttl_seconds=300, ro=True):
        import sqlite3
        self._db_path = db_path
        self._ttl = ttl_seconds
        mode = "ro" if ro else "rw"
        uri = "file:{}?mode={}".format(db_path.replace("\\", "/"), mode)
        self._conn = sqlite3.connect(uri, uri=True, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._version = None
        self._built_at = 0.0
        self._idx = None

    def get(self):
        now = time.time()
        ver = _read_data_version(self._conn)
        expired = (now - self._built_at) > self._ttl
        if self._idx is None or ver != self._version or expired:
            # 正式映射层：全字段 NULL 兜底；状态 strip 防 '退舱 '（尾随空格）误判 active
            rows = [{"code": (r["客户编码"] or ""), "box": (r["箱号"] or ""),
                     "company": (r["开票子公司名称"] or ""),
                     "status": (r["状态"] or "").strip(),
                     "deleted": r["is_deleted"] or 0}
                    for r in self._conn.execute(
                        "SELECT 客户编码,箱号,开票子公司名称,状态,is_deleted FROM records")]
            self._idx = matching.build_index(rows)
            self._version = ver
            self._built_at = now
        return self._idx
