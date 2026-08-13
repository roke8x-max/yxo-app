# -*- coding: utf-8 -*-
"""
飞书多维表格双写：按客户编码定位记录并更新字段（并行期与本地库双写）。
"""
import os
import sys
import time
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, TABLE_MAIN

BASE = "https://open.feishu.cn/open-apis"
_token_cache = {"token": None, "expires_at": 0}


def _token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    r = requests.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                      timeout=10)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"feishu token failed: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200)
    return _token_cache["token"]


def _headers():
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def find_record_id(code, box=None):
    """按客户编码（可选箱号）搜索飞书主表记录，返回 (record_id, fields) 或 (None, None)。"""
    conditions = [{"field_name": "客户编码", "operator": "is", "value": [code]}]
    if box:
        conditions.append({"field_name": "箱号", "operator": "is", "value": [box]})
    payload = {"filter": {"conjunction": "and", "conditions": conditions},
               "automatic_fields": False}
    r = requests.post(
        f"{BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{TABLE_MAIN}/records/search",
        headers=_headers(), json=payload, timeout=15)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"feishu search failed: {data}")
    items = (data.get("data") or {}).get("items") or []
    if not items:
        return None, None
    item = items[0]
    # 飞书文本字段可能是 [{'text': ...}] 结构，简化为纯文本
    fields = {}
    for k, v in (item.get("fields") or {}).items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "text" in v[0]:
            fields[k] = "".join(seg.get("text", "") for seg in v)
        else:
            fields[k] = v
    return item.get("record_id"), fields


def update_fields(record_id, fields):
    """更新飞书主表一条记录，fields 为 {中文字段名: 值}。"""
    r = requests.put(
        f"{BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{TABLE_MAIN}/records/{record_id}",
        headers=_headers(), json={"fields": fields}, timeout=15)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"feishu update failed: {data}")
    return True


def dual_write(code, box, fields):
    """
    并行期双写飞书。返回 (ok, record_id, old_values, err_msg)。
    old_values 用于撤销。
    """
    try:
        record_id, old_fields = find_record_id(code, box=None)
        if not record_id:
            return False, None, None, f"飞书表中未找到客编 {code}"
        old_values = {k: old_fields.get(k, "") for k in fields}
        update_fields(record_id, fields)
        return True, record_id, old_values, ""
    except Exception as e:
        return False, None, None, str(e)


def revert(record_id, old_values):
    """撤销：把飞书记录字段恢复为旧值。"""
    try:
        # 飞书选项字段恢复空值需传空字符串
        fields = {k: (v if v is not None else "") for k, v in old_values.items()}
        update_fields(record_id, fields)
        return True
    except Exception:
        return False
