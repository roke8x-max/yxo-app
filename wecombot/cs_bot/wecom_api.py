# -*- coding: utf-8 -*-
"""
企业微信 API 封装：access_token、媒体下载、应用主动消息、微信客服(kf)收发。
"""
import os
import re
import time
import json
import requests

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, r"D:\YXO_DATA\MailBots")
from common_io import atomic_write_json
from config import CORP_ID, SECRET, KF_SECRET, AGENT_ID, BOT_LOG_DIR

QY = "https://qyapi.weixin.qq.com/cgi-bin"

# ==================== 企微 API 限流退避（2026-08-04 体检报告 2.4）====================
# 可重试错误：限流 / 系统繁忙 / 接口调用超限 / 网络层未知错误
RETRYABLE = {95001, 95018, -1, 45009}
# 致命错误：参数或凭据错误，重试无意义，立即放弃
FATAL = {40096, 40001, 42001}
_MAX_RETRY = 5
_BLACKLIST_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "wecom_blacklist.json")
)
# 连续 40096（无效 external_userid）达到该次数后，停止对该 ID 发送
_BLACKLIST_THRESHOLD = 3


def _load_blacklist():
    try:
        with open(_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_blacklist(data):
    os.makedirs(os.path.dirname(_BLACKLIST_FILE), exist_ok=True)
    atomic_write_json(_BLACKLIST_FILE, data)


def _is_blacklisted(external_userid):
    if not external_userid:
        return False
    return _load_blacklist().get(external_userid, {}).get("blocked", False)


def _mark_result(external_userid, ok):
    """ok=False 时累计 40096 失败；达到阈值则拉黑；成功则清零计数。"""
    if not external_userid:
        return
    bl = _load_blacklist()
    entry = bl.get(external_userid, {"fails": 0, "blocked": False})
    if ok:
        entry["fails"] = 0
    else:
        entry["fails"] = entry.get("fails", 0) + 1
        if entry["fails"] >= _BLACKLIST_THRESHOLD:
            entry["blocked"] = True
            _log(f"external_userid {external_userid} 连续 {entry['fails']} 次 40096，已拉黑停止发送")
    bl[external_userid] = entry
    _save_blacklist(bl)


def _wecom_post(api_path, payload, token_fn):
    """带指数退避 + 抖动的企微 API 调用封装。返回 (errcode, data)。

    - errcode==0           → 成功
    - errcode in FATAL     → 放弃，不再重试
    - errcode in RETRYABLE → 指数退避后重试（最多 _MAX_RETRY 次）
    - 其他 errcode         → 返回，不重试
    """
    import random
    for i in range(_MAX_RETRY):
        try:
            r = requests.post(f"{QY}{api_path}", params={"access_token": token_fn()},
                              json=payload, timeout=10)
            data = r.json()
        except Exception as e:
            wait = min(2 ** i, 32) + random.uniform(0, 1)
            _log(f"{api_path} 网络异常(第{i+1}次)，{wait:.1f}s 后重试: {e}")
            time.sleep(wait)
            continue
        code = data.get("errcode", -1)
        if code == 0:
            return 0, data
        if code in FATAL:
            _log(f"{api_path} 致命错误 {code}: {data.get('errmsg')}，放弃重试")
            return code, data
        if code in RETRYABLE:
            wait = min(2 ** i, 32) + random.uniform(0, 1)
            _log(f"{api_path} 限流/繁忙 {code}(第{i+1}次重试)，{wait:.1f}s 后重试")
            time.sleep(wait)
            continue
        _log(f"{api_path} 非重试错误 {code}: {data.get('errmsg')}")
        return code, data
    _log(f"{api_path} 重试 {_MAX_RETRY} 次仍失败")
    return -1, {}


_token_cache = {}  # secret -> {"token", "expires_at"}


def get_token():
    """自建应用 access_token（使用 SECRET）。"""
    return _get_token(SECRET)


def get_kf_token():
    """微信客服 access_token（优先 KF_SECRET，留空回落 SECRET）。"""
    return _get_token(KF_SECRET or SECRET)


def _get_token(secret):
    now = time.time()
    c = _token_cache.get(secret)
    if c and c["token"] and now < c["expires_at"] - 60:
        return c["token"]
    r = requests.get(f"{QY}/gettoken", params={"corpid": CORP_ID, "corpsecret": secret}, timeout=10)
    data = r.json()
    if data.get("errcode") != 0:
        _log(f"gettoken failed: {data}")
        raise RuntimeError(f"gettoken failed: {data}")
    _token_cache[secret] = {"token": data["access_token"], "expires_at": now + data.get("expires_in", 7200)}
    return data["access_token"]


def _log(text):
    from datetime import datetime
    path = os.path.join(BOT_LOG_DIR, f"csbot_{datetime.now().strftime('%Y-%m-%d')}.log")
    os.makedirs(BOT_LOG_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {text}\n")


def download_media(media_id, save_dir, fallback_name="file.bin"):
    """下载临时素材，返回本地文件路径。"""
    os.makedirs(save_dir, exist_ok=True)
    r = requests.get(f"{QY}/media/get", params={"access_token": get_token(), "media_id": media_id},
                     timeout=60, stream=True)
    # 若返回 json 则是错误
    ctype = r.headers.get("Content-Type", "")
    if "json" in ctype or "text/plain" in ctype:
        try:
            err = r.json()
            _log(f"media/get failed: {err}")
            return None
        except Exception:
            pass
    filename = fallback_name
    disp = r.headers.get("Content-Disposition", "")
    m = re.search(r'filename="?([^";]+)"?', disp)
    if m:
        filename = m.group(1)
    # 防重名
    path = os.path.join(save_dir, filename)
    base, ext = os.path.splitext(filename)
    i = 1
    while os.path.exists(path):
        path = os.path.join(save_dir, f"{base}({i}){ext}")
        i += 1
    with open(path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return path


def send_app_text(user_id, text):
    """自建应用主动发文本（分段防超长）。带限流退避重试。"""
    for seg in _split_text(text):
        payload = {
            "touser": user_id, "msgtype": "text", "agentid": AGENT_ID,
            "text": {"content": seg},
        }
        code, _ = _wecom_post("/message/send", payload, get_token)
        if code != 0:
            return False
    return True


def _split_text(text, limit=1800):
    """按字节上限分段（企微 text 上限 2048 字节）。"""
    segs = []
    cur = ""
    for line in text.split("\n"):
        candidate = (cur + "\n" + line) if cur else line
        if len(candidate.encode("utf-8")) > limit:
            if cur:
                segs.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        segs.append(cur)
    return segs or [""]


# ==================== 微信客服 (kf) ====================

_kf_cursor_file = None


def _cursor_path():
    global _kf_cursor_file
    if _kf_cursor_file is None:
        _kf_cursor_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "data", "kf_cursor.json")
        _kf_cursor_file = os.path.normpath(_kf_cursor_file)
    return _kf_cursor_file


def _load_cursor(open_kfid):
    try:
        with open(_cursor_path(), "r", encoding="utf-8") as f:
            return json.load(f).get(open_kfid, "")
    except Exception:
        return ""


def _save_cursor(open_kfid, cursor):
    path = _cursor_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data[open_kfid] = cursor
    atomic_write_json(path, data)


def kf_sync_msgs(token, open_kfid):
    """拉取微信客服新消息，返回消息列表。"""
    msgs = []
    cursor = _load_cursor(open_kfid)
    while True:
        payload = {"open_kfid": open_kfid, "token": token}
        if cursor:
            payload["cursor"] = cursor
        r = requests.post(f"{QY}/kf/sync_msg", params={"access_token": get_kf_token()},
                          json=payload, timeout=15)
        data = r.json()
        if data.get("errcode") != 0:
            _log(f"kf/sync_msg failed: {data}")
            break
        msgs.extend(data.get("msg_list", []))
        cursor = data.get("next_cursor", cursor)
        if not data.get("has_more"):
            break
    if cursor:
        _save_cursor(open_kfid, cursor)
    return msgs


def kf_send_text(open_kfid, external_userid, text):
    """微信客服主动发文本。带限流退避重试；external_userid 失效(40096)累计拉黑。"""
    if _is_blacklisted(external_userid):
        _log(f"kf_send_text 跳过已拉黑 external_userid: {external_userid}")
        return False
    for seg in _split_text(text):
        payload = {
            "touser": external_userid, "open_kfid": open_kfid,
            "msgtype": "text", "text": {"content": seg},
        }
        code, _ = _wecom_post("/kf/send_msg", payload, get_kf_token)
        if code == 40096:
            _mark_result(external_userid, ok=False)   # 失效 ID，累计拉黑
            return False
        if code != 0:
            return False
    _mark_result(external_userid, ok=True)            # 成功则清零失败计数
    return True


# ==================== 按真名发通知（微信可收） ====================

def _binding_external_id(real_name):
    """真名 -> 微信客服 external_userid（取最近一次绑定）。"""
    import sqlite3
    db = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "data", "cs_bot.db"))
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT external_id FROM bindings WHERE name=? ORDER BY ts DESC LIMIT 1",
            (real_name,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        _log(f"binding lookup failed: {e}")
        return None


def _default_open_kfid():
    """取客服账号 open_kfid（目前只有一个客服账号，直接取 cursor 文件的 key）。"""
    try:
        with open(_cursor_path(), "r", encoding="utf-8") as f:
            keys = list(json.load(f).keys())
        return keys[0] if keys else None
    except Exception:
        return None


def notify_by_name(real_name, text):
    """给同事发通知，微信直接可收。
    优先走微信客服通道（按绑定表 external_id），失败回退企微应用消息（仅通讯录内成员）。
    返回 (ok, channel)：channel 为 'kf' / 'app' / ''。
    注意：微信客服通道要求对方 48 小时内和订舱助手说过话，否则会 95018 失败并触发回退。"""
    ext_id = _binding_external_id(real_name)
    kfid = _default_open_kfid()
    if ext_id and kfid:
        if kf_send_text(kfid, ext_id, text):
            return True, "kf"
    from config import WECOM_USER_MAP
    uid = next((k for k, v in WECOM_USER_MAP.items() if v == real_name), None)
    if uid and send_app_text(uid, text):
        return True, "app"
    _log(f"notify_by_name failed: {real_name} (ext_id={bool(ext_id)}, kfid={bool(kfid)}, uid={uid})")
    return False, ""
