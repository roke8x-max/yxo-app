# -*- coding: utf-8 -*-
"""发送身份（spec §5.3）：负责公司 →(关键词映射)→ 负责同事 → 其 cqtransit 邮箱。
映射真相源 = WeComBot config.USER_COMPANIES/USER_EMAILS；找不到同事 → 调用方 alarm。"""
import json
import os

_override = None


def override_json(path):
    """测试/运维覆盖层：{"公司关键词": {"email":..., "name":...}} 或 {"kw": "邮箱"}"""
    global _override
    with open(path, encoding="utf-8") as f:
        _override = json.load(f)


def _wecombot_maps():
    """返回 (company_kw->email dict, company_kw->realname dict)；失败给空表。"""
    kw_email, kw_name = {}, {}
    try:
        from core import paths
        root = paths.detect_root()
        import sys
        wb = os.path.join(root, "WeComBot")
        if wb not in sys.path:
            sys.path.insert(0, wb)
        from config import COMPANY_TO_EMAIL, company_to_name  # type: ignore
        kw_email = dict(COMPANY_TO_EMAIL)
        for kw in kw_email:
            nm = company_to_name(kw)
            if nm:
                kw_name[kw] = nm
    except Exception:
        pass
    return kw_email, kw_name


def sender_for(company):
    c = str(company or "")
    if _override:
        for kw, val in _override.items():
            if kw in c:
                email = val["email"] if isinstance(val, dict) else val
                name = val.get("name") if isinstance(val, dict) else None
                return email, name
    kw_email, kw_name = _wecombot_maps()
    for kw, email in kw_email.items():
        if kw in c:
            return email, kw_name.get(kw)
    return None, None


def real_name_of(company):
    return sender_for(company)[1]
