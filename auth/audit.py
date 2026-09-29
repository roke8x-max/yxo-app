#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计写入（spec §11，落库 auth.db 内 auth_audit）。"""
from datetime import datetime

from flask import g, request


def log_event(event, username="", target="", detail="", ip="", req_id=""):
    from auth import dao
    try:
        ip = ip or (request.remote_addr if request else "")
    except Exception:
        pass
    try:
        req_id = req_id or (g.get("request_id", "") if g else "")
    except Exception:
        pass
    try:
        dao.write_audit(datetime.now().isoformat(timespec="seconds"),
                        username, ip, req_id, event, target, detail)
    except Exception:
        pass  # 审计绝不能打挂业务
