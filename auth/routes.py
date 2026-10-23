#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""认证路由：/api/login /api/logout /api/csrf /api/auth_meta /api/change_password。

注意 /api/auth_meta 即 spec §7.4 的「/api/meta（登录后取身份+CSRF）」，
因与现存表格元数据接口 /api/meta 重名而改名（见 auth/rbac.py 头部说明）。
"""
import secrets
import string

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


def _shadow_denied():
    """影子期（AUTH_ENABLED=0）无鉴权：所有账号安全相关写操作一律拒绝。"""
    if not config.AUTH_ENABLED:
        return _err("HTTP_403", "该功能仅在登录模式可用（当前为影子期）", 403)
    return None


def _gen_password(length=14):
    """生成满足 password_strength 的随机密码：三类字符各先放 1 个，再补随机，最后打乱。"""
    upper, lower, digits = string.ascii_uppercase, string.ascii_lowercase, string.digits
    pool = upper + lower + digits
    for _ in range(50):
        chars = [secrets.choice(upper), secrets.choice(lower), secrets.choice(digits)]
        chars += [secrets.choice(pool) for _ in range(max(0, length - 3))]
        secrets.SystemRandom().shuffle(chars)
        pw = "".join(chars)
        ok, _msg = service.password_strength(pw)
        if ok:
            return pw
    raise RuntimeError("随机密码生成失败")


def _to_bool01(v):
    """disabled 归一化：1/True/'1'/'true' → 1；0/False/'0'/'false' → 0；其他 → None。"""
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int) and v in (0, 1):
        return v
    if isinstance(v, str) and v.strip().lower() in ("0", "1", "true", "false"):
        return 1 if v.strip().lower() in ("1", "true") else 0
    return None


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
    denied = _shadow_denied()
    if denied:
        return denied
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
    dao.delete_user_sessions(username)      # 改密后旧会话立即失效（含当前会话）
    audit.log_event("改密成功", username)
    return jsonify({"ok": True})


@auth_bp.route("/api/admin/users", methods=["GET"])
def api_admin_users():
    denied = _shadow_denied()
    if denied:
        return denied
    return jsonify({"ok": True, "users": dao.list_users()})


@auth_bp.route("/api/admin/users/reset_password", methods=["POST"])
def api_admin_user_reset_password():
    denied = _shadow_denied()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return _err("HTTP_400", "缺少 username", 400)
    if not dao.get_user(username):
        return _err("HTTP_404", "用户不存在", 404)

    # password 语义（写死）：字段不存在或 None → 生成随机密码；空串 → 400；非字符串 → 400；有值 → 过强度
    if "password" in data and data["password"] is not None:
        raw = data["password"]
        if not isinstance(raw, str):
            return _err("HTTP_400", "新密码格式错误", 400)
        if raw == "":
            return _err("HTTP_400", "新密码不能为空", 400)
        ok, msg = service.password_strength(raw)
        if not ok:
            return _err("HTTP_400", msg, 400)
        new_pw, generated = raw, False
    else:
        new_pw, generated = _gen_password(), True

    dao.set_password(username, service.hash_password(new_pw))
    dao.delete_user_sessions(username)               # 旧会话必须失效
    audit.log_event("重置密码", g.identity.username, target=f"user:{username}")
    resp = {"ok": True, "username": username,
            "self": username == getattr(g.identity, "username", "")}
    if generated:
        resp["password"] = new_pw                    # 只在随机生成时回传一次
    return jsonify(resp)


@auth_bp.route("/api/admin/users/set_disabled", methods=["POST"])
def api_admin_user_set_disabled():
    denied = _shadow_denied()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    flag = _to_bool01(data.get("disabled"))
    if not username:
        return _err("HTTP_400", "缺少 username", 400)
    if flag is None:
        return _err("HTTP_400", "disabled 只能是 0 或 1", 400)
    if not dao.get_user(username):
        return _err("HTTP_404", "用户不存在", 404)
    if username == getattr(g.identity, "username", ""):
        return _err("HTTP_403", "不能停用自己的账号", 403)
    dao.set_disabled(username, bool(flag))
    if flag:
        dao.delete_user_sessions(username)           # 停用即时踢下线
    audit.log_event("停用账号" if flag else "启用账号",
                    g.identity.username, target=f"user:{username}")
    return jsonify({"ok": True, "username": username, "disabled": flag})
