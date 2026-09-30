#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""权限门禁入口（spec §4 / §6）。

init_auth(app)：设 secret_key、注册 auth_bp、挂 _auth_gate、建表+种子、启动自检。
硬约束（spec §9）：本包内不得 import 任何业务模块（records/manifest/tuoshu/price…），
只对外暴露 init_auth / g.identity / require_permission。
"""
from types import SimpleNamespace

from flask import g, jsonify, request, current_app

import config
from auth import audit, dao, service
from auth.rbac import (AUTHENTICATED, PUBLIC, ROUTE_PERMISSIONS,
                       ROUTE_PERMISSIONS_SET, PERMISSIONS)


def _deny(code, msg, status, event="越权访问"):
    try:
        req_id = g.get("request_id", "-")
    except Exception:
        req_id = "-"
    ident = getattr(g, "identity", None)
    audit.log_event(event, getattr(ident, "username", "") if ident else "",
                    target=f"{request.method} {request.path}")
    return jsonify({"ok": False, "error": code, "msg": msg, "req_id": req_id}), status


def _load_identity():
    """解析 session → 注入 g.identity。失败返回 None（禁用即时失效，spec §5.2）。"""
    user = service.get_session(request.cookies.get(service.SESSION_COOKIE, ""))
    if not user:
        return None
    from auth.rbac import role_permissions
    import json
    try:
        companies = json.loads(user["scope_companies"] or "[]")
    except Exception:
        companies = []
    return SimpleNamespace(
        username=user["username"], role=user["role"],
        permissions=role_permissions(user["role"]),
        scope_type=user["scope_type"] or "none",
        companies=companies if isinstance(companies, list) else [])


def _system_identity():
    """disabled 期兜底身份（A 工单）：与 _load_identity 同形状 5 字段。
    permissions 必须全权限（list(PERMISSIONS.keys())），否则视图层
    @require_permission/内联 ident.permissions 判定会把整站 403 挡死。"""
    return SimpleNamespace(
        username="system",
        role="system",                              # 仅供形状对齐；disabled 期不参与任何 role 判定
        permissions=list(PERMISSIONS.keys()),
        scope_type="all",                           # records_dao.resolve_scope 全量放行
        companies=[])


def _dry_run_log(req):
    """disabled 期观察日志（A 工单）：每非噪声请求一行 INFO，前缀 AUTH_DRY_RUN。"""
    if req.url_rule is None:
        matched = "未登记"
    else:
        key = (req.method, req.url_rule.rule)
        matched = "未登记" if key not in ROUTE_PERMISSIONS_SET else ROUTE_PERMISSIONS[key]
    current_app.logger.info("AUTH_DRY_RUN path=%s method=%s ip=%s matched=%s has_session=False",
                            req.path, req.method, req.remote_addr, matched)


def _auth_gate():
    # 噪声过滤（disabled 与 enabled 都要跳）：未匹配路由交回 Flask 原生 404；静态资源放行。
    # ⚠️ 不跳 OPTIONS / HEAD：HEAD 会实际执行 GET 视图，若 return 不注入身份，
    #    视图内裸 g.identity.username 会 AttributeError。
    if request.url_rule is None:
        return None
    if request.endpoint == "static":
        return None

    # —— A 工单：灰度开关 ——
    if not config.AUTH_ENABLED:
        g.identity = _system_identity()   # username="system"，全权限 + scope=all
        _dry_run_log(request)
        return None                        # 不拒任何请求

    # 以下为 enabled 正常逻辑（CSRF + 登录/权限），保持现有实现不变
    # 第一层 · CSRF（所有非 GET；/api/stamp 靠 X-Stamp-Token 豁免）
    if request.method != "GET":
        if request.path not in service.CSRF_EXEMPT_PATHS and not service.verify_csrf(request):
            return _deny("HTTP_403", "CSRF 校验失败", 403, event="CSRF拒绝")

    # 第二层 · 登录与权限（先判未登记 → 默认拒绝 403）
    key = (request.method, request.url_rule.rule)
    if key not in ROUTE_PERMISSIONS_SET:
        return _deny("HTTP_403", "无权限", 403)
    perm = ROUTE_PERMISSIONS[key]
    if perm == PUBLIC:
        g.identity = _load_identity()  # 公开路由也尽力挂身份（供审计），无则不挡
        return None
    ident = _load_identity()
    if ident is None:
        try:
            req_id = g.get("request_id", "-")
        except Exception:
            req_id = "-"
        return jsonify({"ok": False, "error": "HTTP_401",
                        "msg": "未登录", "req_id": req_id}), 401
    g.identity = ident
    if perm == AUTHENTICATED:
        return None
    if perm not in (ident.permissions or []):
        return _deny("HTTP_403", "无权限", 403)
    return None


def _self_check(app):
    """启动自检（spec §6.3）：url_map 与 ROUTE_PERMISSIONS 双向比对。
    未登记路由：DEBUG 下直接抛错退出；生产 warning（仍按默认拒绝 403）。
    反向残留：warning。static endpoint 跳过。"""
    missing, stale = [], []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for m in (rule.methods or set()) - {"HEAD", "OPTIONS"}:
            if (m, rule.rule) not in ROUTE_PERMISSIONS_SET:
                missing.append(f"{m} {rule.rule}")
    registered = {(m, r) for (m, r) in ROUTE_PERMISSIONS_SET}
    live = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for m in (rule.methods or set()) - {"HEAD", "OPTIONS"}:
            live.add((m, rule.rule))
    stale = sorted(f"{m} {r}" for (m, r) in registered - live)
    if missing:
        msg = "[auth] 未登记路由: " + "; ".join(sorted(missing))
        if app.debug:
            raise RuntimeError(msg + "（DEBUG 下拒绝带漏登启动）")
        app.logger.warning(msg)
    if stale:
        app.logger.warning("[auth] 反向残留（表有、url_map 无）: " + "; ".join(stale))
    return missing, stale


def init_auth(app):
    """接线：secret_key → auth_bp → _auth_gate → 建表种子 → 启动自检。"""
    import config
    from auth import schema
    from auth.common import is_production
    from auth.routes import auth_bp
    # B 工单：生产缺 YXSTAMP_TOKEN 拒绝启动，绝不带弱默认上线。
    # 导入期不 raise（app.debug/testing 此时未赋值），此处才有 app 对象可判。
    if is_production(app) and config.STAMP_TOKEN is None:
        raise RuntimeError("YXSTAMP_TOKEN 未设置，拒绝启动 /api/stamp")
    if not app.secret_key:
        app.secret_key = config.AUTH_SECRET_KEY or service._secret()
    app.register_blueprint(auth_bp)
    app.before_request(_auth_gate)
    schema.init_db()
    return _self_check(app)
