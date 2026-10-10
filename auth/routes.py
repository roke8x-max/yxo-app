#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""认证路由：/api/login /api/logout /api/csrf /api/auth_meta /api/change_password。

注意 /api/auth_meta 即 spec §7.4 的「/api/meta（登录后取身份+CSRF）」，
因与现存表格元数据接口 /api/meta 重名而改名（见 auth/rbac.py 头部说明）。
"""
import secrets

from flask import Blueprint, g, jsonify, request

import config
from auth import audit, dao, service
from auth.rbac import role_permissions

auth_bp = Blueprint("auth", __name__)


def _err(code, msg, status):
    try:
        req_id = g.get("request_id", "-")
    except Exception:
        req_id = "-"
    return jsonify({"ok": False, "error": code, "msg": msg, "req_id": req_id}), status


@auth_bp.route("/api/csrf", methods=["GET"])
def api_csrf():
    seed = request.cookies.get(service.CSRF_COOKIE, "")
    if not seed:
        seed = secrets.token_hex(16)
    resp = jsonify({"ok": True, "csrf_token": service.csrf_token_for(seed)})
    resp.set_cookie(service.CSRF_COOKIE, seed,
                    httponly=True, samesite="Lax", path="/", max_age=30 * 86400)
    return resp


@auth_bp.route("/api/login", methods=["POST"])
def api_login():
    import config
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    remember = bool(data.get("remember_me"))
    ip = request.remote_addr or ""
    if not username or not password:
        return _err("HTTP_400", "用户名或密码缺失", 400)
    if service.is_locked(username, ip):
        audit.log_event("登录失败-锁定", username, detail=f"fails>={config.AUTH_MAX_FAILS}")
        return _err("HTTP_429", "失败次数过多，已锁定 15 分钟", 429)
    user = dao.get_user(username)
    if not user or user["disabled"] or not service.verify_password(user["pw_hash"], password):
        fails, locked = service.record_fail(username, ip)
        audit.log_event("登录失败" + ("-锁定" if locked else ""),
                        username, detail=f"fails={fails}")
        if locked:
            return _err("HTTP_429", "失败次数过多，已锁定 15 分钟", 429)
        return _err("HTTP_401", "用户名或密码错误", 401)
    service.clear_fails(username, ip)
    # 7–30 天：remember → AUTH_REMEMBER_DAYS（默认 30），否则会话级短 session
    sid = service.create_session(username, user["id"], remember)
    hours_days = config.AUTH_REMEMBER_DAYS * 86400 if remember else config.AUTH_SESSION_HOURS * 3600
    audit.log_event("登录成功", username)
    resp = jsonify({"ok": True, "username": username, "role": user["role"]})
    resp.set_cookie(service.SESSION_COOKIE, sid, httponly=True, samesite="Lax",
                    path="/", max_age=hours_days)
    return resp


@auth_bp.route("/api/logout", methods=["POST"])
def api_logout():
    username = getattr(getattr(g, "identity", None), "username", "")
    service.destroy_session(request.cookies.get(service.SESSION_COOKIE, ""))
    audit.log_event("登出", username)
    resp = jsonify({"ok": True})
    resp.set_cookie(service.SESSION_COOKIE, "", httponly=True, samesite="Lax",
                    path="/", max_age=0)
    return resp


@auth_bp.route("/api/auth_meta", methods=["GET"])
def api_auth_meta():
    ident = g.identity
    seed = request.cookies.get(service.CSRF_COOKIE, "")
    if not seed:
        seed = secrets.token_hex(16)
    resp = jsonify({
        "ok": True,
        "mode": "auth" if config.AUTH_ENABLED else "shadow",
        "users": dao.list_shadow_users(),
        "username": ident.username,
        "role": ident.role,
        "permissions": ident.permissions,
        "scope": {"type": ident.scope_type, "companies": ident.companies},
        "csrf_token": service.csrf_token_for(seed),
    })
    resp.set_cookie(service.CSRF_COOKIE, seed, httponly=True, samesite="Lax",
                    path="/", max_age=30 * 86400)
    return resp


@auth_bp.route("/api/change_password", methods=["POST"])
def api_change_password():
    data = request.get_json(silent=True) or {}
    old = data.get("old_password") or ""
    new = data.get("new_password") or ""
    username = g.identity.username
    user = dao.get_user(username)
    if not user or not service.verify_password(user["pw_hash"], old):
        audit.log_event("改密失败", username, detail="旧密码错误")
        return _err("HTTP_400", "旧密码错误", 400)
    ok, msg = service.password_strength(new)
    if not ok:
        return _err("HTTP_400", msg, 400)
    dao.set_password(username, service.hash_password(new))
    audit.log_event("改密成功", username)
    return jsonify({"ok": True})
