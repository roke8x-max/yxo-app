#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""认证/会话/限流/密码校验（spec §5）。

口令哈希：优先 Argon2（argon2-cffi），无则 bcrypt，无则标准库
pbkdf2_hmac（sha256, 20 万轮）。存储自带前缀，verify 按前缀分发。
明文密码零容忍：库里只存哈希。

会话：自研 sqlite 服务端 session（auth_sessions 表），不定级依赖
Flask-Session——单文件单进程最稳（spec §5.1 意图；内网 4 人）。
Cookie：HttpOnly + SameSite=Lax + Path=/（内网 HTTP 无 Secure）。
"""
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta

SESSION_COOKIE = "yxo_session"
CSRF_COOKIE = "csrf_seed"
CSRF_HEADER = "X-CSRF-Token"

_PBKDF2_ITER = 200_000


# ---------------- 口令哈希 ----------------
# 有意选择：优先 argon2/bcrypt（若已安装则用），否则退到标准库
# pbkdf2_hmac（sha256, 20 万轮）—— 不在 requirements.txt 声明额外依赖（G5）。

def hash_password(password):
    try:
        from argon2 import PasswordHasher
        return "$argon2$" + PasswordHasher().hash(password)
    except ImportError:
        pass
    try:
        import bcrypt
        return "$bcrypt$" + bcrypt.hashpw(password.encode("utf-8"),
                                          bcrypt.gensalt()).decode("ascii")
    except ImportError:
        pass
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return f"$pbkdf2${_PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(stored, password):
    try:
        if stored.startswith("$argon2$"):
            from argon2 import PasswordHasher
            try:
                return PasswordHasher().verify(stored[len("$argon2$"):], password)
            except Exception:
                return False
        if stored.startswith("$bcrypt$"):
            import bcrypt
            return bcrypt.checkpw(password.encode("utf-8"), stored[len("$bcrypt$"):].encode("ascii"))
        if stored.startswith("$pbkdf2$"):
            _, _, it, salt_hex, dk_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(it))
            return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False
    return False


def password_strength(password):
    """新密码强度：长度≥12、含大小写字母、含数字（spec §7.4）。"""
    p = password or ""
    if len(p) < 12:
        return False, "新密码长度至少 12 位"
    if not any(c.islower() for c in p) or not any(c.isupper() for c in p):
        return False, "新密码须同时含大小写字母"
    if not any(c.isdigit() for c in p):
        return False, "新密码须含数字"
    return True, ""


# ---------------- 用户管理（种子/测试/改密用） ----------------

def create_user(username, password, role="visitor", scope_type="none", companies=None):
    import json
    from auth import dao
    conn = dao.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO auth_users(username, pw_hash, role, scope_type,"
            " scope_companies, disabled, created_at) VALUES(?,?,?,?,?,0,?)",
            (username, hash_password(password), role, scope_type,
             json.dumps(companies or [], ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()


# ---------------- 会话 ----------------

def _lifetimes():
    import config
    return config.AUTH_SESSION_HOURS, config.AUTH_REMEMBER_DAYS


def create_session(username, user_id, remember=False):
    from auth import dao
    sid = secrets.token_hex(32)
    hours, days = _lifetimes()
    delta = timedelta(days=days) if remember else timedelta(hours=hours)
    dao.save_session(sid, user_id, username,
                     (datetime.now() + delta).isoformat(timespec="seconds"), remember)
    return sid


def get_session(sid):
    """验 session：存在 + 未过期 + 用户未被禁用 → 滑动续期。否则 None。"""
    from auth import dao
    if not sid:
        return None
    row = dao.load_session(sid)
    if not row:
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
    except Exception:
        dao.delete_session(sid)
        return None
    if exp <= datetime.now():
        dao.delete_session(sid)
        return None
    user = dao.get_user(row["username"])
    if not user or user["disabled"]:
        dao.delete_session(sid)
        return None
    hours, days = _lifetimes()
    delta = timedelta(days=days) if row["remember"] else timedelta(hours=hours)
    dao.touch_session(sid, (datetime.now() + delta).isoformat(timespec="seconds"))
    return user


def destroy_session(sid):
    from auth import dao
    if sid:
        dao.delete_session(sid)


def destroy_user_sessions(username):
    from auth import dao
    dao.delete_user_sessions(username)


# ---------------- CSRF（double-submit：seed cookie + HMAC token，无服务端存储） ----------------

def _secret():
    import config
    s = config.AUTH_SECRET_KEY
    if not s:
        global _RUNTIME_SECRET
        try:
            return _RUNTIME_SECRET
        except NameError:
            _RUNTIME_SECRET = secrets.token_hex(32)
            print("[auth] WARNING: YXO_AUTH_SECRET 未设置，用进程内随机值（重启后 CSRF token 失效）")
            return _RUNTIME_SECRET
    return s


def csrf_token_for(seed):
    return hmac.new(_secret().encode("utf-8"), ("csrf:" + seed).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_csrf(req):
    seed = req.cookies.get(CSRF_COOKIE, "")
    token = req.headers.get(CSRF_HEADER, "")
    if not seed or not token:
        return False
    return hmac.compare_digest(csrf_token_for(seed), token)


# CSRF 豁免：GET 与 static 已在门禁第一层豁免；此处仅「非 GET 但自带认证」的例外。
# /api/stamp 靠 X-Stamp-Token 认证（机器人无 session、无 CSRF），token 即认证。
CSRF_EXEMPT_PATHS = frozenset({"/api/stamp"})


# ---------------- 登录限流（spec §5.4） ----------------

def is_locked(username, ip):
    from auth import dao
    import config
    row = dao.get_attempt(username, ip)
    if not row or not row["locked_until"]:
        return False
    try:
        until = datetime.fromisoformat(row["locked_until"])
    except Exception:
        return False
    if until > datetime.now():
        return True
    dao.clear_attempt(username, ip)
    return False


def record_fail(username, ip):
    """失败计数 + 延迟；达阈值锁定。返回 (fails, locked)。"""
    from auth import dao
    import config
    time.sleep(0.3)  # 响应延迟，拖慢撞库
    row = dao.get_attempt(username, ip)
    fails = (row["fails"] if row else 0) + 1
    locked_until = ""
    locked = False
    if fails >= config.AUTH_MAX_FAILS:
        locked_until = (datetime.now() + timedelta(minutes=config.AUTH_LOCK_MINUTES)).isoformat(
            timespec="seconds")
        locked = True
    dao.save_attempt(username, ip, fails, locked_until)
    return fails, locked


def clear_fails(username, ip):
    from auth import dao
    dao.clear_attempt(username, ip)
