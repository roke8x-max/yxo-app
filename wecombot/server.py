"""
企业微信 Bot 服务入口
启动: python server.py
"""

import sys
import os
import time
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, request, make_response

# 确保能导入 shared 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pythonw / 后台运行时 stdout 管道可能失效，print 会抛异常导致 500。
# 统一把 stdout/stderr 重定向到日志文件，彻底杜绝。
_here = os.path.dirname(os.path.abspath(__file__))
_stdlog = os.path.join(_here, "logs", "server_stdout.log")
os.makedirs(os.path.dirname(_stdlog), exist_ok=True)
try:
    _f = open(_stdlog, "a", encoding="utf-8", buffering=1)
    sys.stdout = _f
    sys.stderr = _f
except Exception:
    pass

from config import (
    TOKEN, ENCODING_AES_KEY, CORP_ID, AGENT_ID, SECRET,
    SERVER_HOST, SERVER_PORT, BOT_LOG_DIR,
    WECOM_USER_MAP, CS_STAGING_DIR
)
from shared.wx_crypto import WXBizMsgCrypt
from cs_bot import engine, store, wecom_api

os.makedirs(BOT_LOG_DIR, exist_ok=True)

app = Flask(__name__)
wx_crypt = WXBizMsgCrypt(TOKEN, ENCODING_AES_KEY, CORP_ID)


# ==================== 消息解析 ====================

def parse_message(xml_str):
    """将企业微信回调的 XML 解析为 dict"""
    root = ET.fromstring(xml_str)
    msg = {}
    for child in root:
        msg[child.tag] = child.text
    return msg


def build_text_reply(from_user, to_user, content):
    """构造文本回复的 XML"""
    timestamp = str(int(time.time()))
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{from_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{to_user}]]></FromUserName>"
        f"<CreateTime>{timestamp}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"</xml>"
    )


def log_message(msg):
    """记录消息到本地日志"""
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(BOT_LOG_DIR, f"{today}.log")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {json.dumps(msg, ensure_ascii=False)}\n")


# ==================== 消息处理（cs_bot 规则引擎） ====================

def resolve_user(raw_id):
    """企微 UserID / 外部用户 ID → 真名；未绑定返回原始 ID。"""
    name = WECOM_USER_MAP.get(raw_id)
    if name:
        return name
    name = store.get_binding(raw_id)
    return name or raw_id


def run_engine_text(raw_id, content):
    """跑文本引擎，处理绑定协议。"""
    user = resolve_user(raw_id)
    try:
        reply = engine.handle_text(user, content)
    except Exception as e:
        log_message({"engine_error": str(e), "content": content})
        return f"❌ 处理出错：{e}\n请稍后重试或联系毛骁洋。"
    if isinstance(reply, str) and reply.startswith("__BIND__"):
        name = reply[len("__BIND__"):]
        store.set_binding(raw_id, name)
        return f"✅ 绑定成功！你好，{name}。回复「帮助」查看可用指令。"
    return reply


def run_engine_media(raw_id, media_id, fallback_name):
    """下载媒体文件并交给文件引擎。"""
    user = resolve_user(raw_id)
    try:
        path = wecom_api.download_media(media_id, CS_STAGING_DIR, fallback_name)
        if not path:
            return "❌ 文件下载失败，请重发一次。"
        return engine.handle_file(user, path)
    except Exception as e:
        log_message({"engine_error": str(e), "media_id": media_id})
        return f"❌ 文件处理出错：{e}"


def handle_kf_event(msg):
    """微信客服事件：拉取新消息并逐条处理、主动回复。"""
    token = msg.get("Token", "")
    open_kfid = msg.get("OpenKfId", "")
    if not token:
        return
    for m in wecom_api.kf_sync_msgs(token, open_kfid):
        origin = m.get("origin")
        mtype = m.get("msgtype", "")
        kfid = m.get("open_kfid", open_kfid)
        reply = None
        ext_id = m.get("external_userid") or ""

        if origin == 3:      # 3=微信客户发送的消息
            if mtype == "text":
                content = (m.get("text") or {}).get("content", "")
                reply = run_engine_text(ext_id, content)
            elif mtype in ("file", "image", "video"):
                media = m.get(mtype) or {}
                media_id = media.get("media_id", "")
                fname = media.get("filename") or f"{mtype}_{m.get('msgid','x')}.bin"
                if mtype == "image" and not media.get("filename"):
                    fname = f"image_{m.get('msgid','x')}.jpg"
                reply = run_engine_media(ext_id, media_id, fname)
        elif origin == 4 and mtype == "event":  # 4=系统推送的事件
            ev_data = m.get("event") or {}
            ev = ev_data.get("event_type", "")
            if ev == "enter_session":
                ext_id = ev_data.get("external_userid", "") or ext_id
                user = resolve_user(ext_id)
                if user in ("毛骁洋", "冯茜", "杨雅雯", "韩文豪"):
                    reply = f"👋 {user}，我在。回复「帮助」查看指令。"
                else:
                    reply = "👋 欢迎！请先回复：绑定 你的姓名（如：绑定 冯茜）"

        if reply and ext_id:
            wecom_api.kf_send_text(kfid, ext_id, reply)


def _safe_kf_event(msg):
    """后台线程跑 kf 事件，异常只记日志不外抛。"""
    try:
        handle_kf_event(msg)
    except Exception as e:
        try:
            log_message({"kf_event_error": str(e)})
        except Exception:
            pass


def handle_message(msg):
    """
    消息处理入口。返回回复 XML；返回 None 表示无需被动回复。
    """
    msg_type = msg.get("MsgType", "")
    from_user = msg.get("FromUserName", "")
    to_user = msg.get("ToUserName", "")

    if msg_type == "text":
        reply = run_engine_text(from_user, msg.get("Content", ""))
        return build_text_reply(from_user, to_user, reply)

    elif msg_type in ("image", "file", "video"):
        media_id = msg.get("MediaId", "")
        fallback = msg.get("Title") or (
            f"image_{msg.get('MsgId','x')}.jpg" if msg_type == "image"
            else f"{msg_type}_{msg.get('MsgId','x')}.bin")
        reply = run_engine_media(from_user, media_id, fallback)
        return build_text_reply(from_user, to_user, reply)

    elif msg_type == "event":
        event = msg.get("Event", "")
        if event == "kf_msg_or_event":
            # 放到后台线程处理，立即响应企微（避免超过5秒被判超时重推）
            import threading
            threading.Thread(target=_safe_kf_event, args=(msg,), daemon=True).start()
            return None
        if event == "subscribe":
            return build_text_reply(from_user, to_user,
                                    "欢迎使用 YXO 客服助手！回复「帮助」查看指令。")
        return None

    else:
        return build_text_reply(from_user, to_user, f"收到 {msg_type} 类型消息，暂未支持。")


# ==================== Flask 路由 ====================

@app.route("/", methods=["GET"])
def index():
    """根路径：返回友好状态页（避免导航卡片点到 404）"""
    return """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>企业微信回调服务</title></head>
<body style="font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;padding:40px;color:#222">
<h1>企业微信回调服务</h1>
<p>运行状态：正常</p>
<p>消息回调地址：<code>/wecom/callback</code></p>
<p style="color:#888">该地址由企业微信服务器调用，并非人工浏览页面。</p>
</body>
</html>"""


@app.route("/wecom/callback", methods=["GET"])
def verify_url():
    """企业微信回调 URL 验证（GET 请求）"""
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    echostr = request.args.get("echostr", "")

    if not all([msg_signature, timestamp, nonce, echostr]):
        return "missing parameters", 400

    result = wx_crypt.verify_url(msg_signature, timestamp, nonce, echostr)
    if result is None:
        return "verify failed", 403
    return result


@app.route("/wecom/callback", methods=["POST"])
def receive_message():
    """接收企业微信推送的消息（POST 请求）"""
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    raw_body = request.data.decode("utf-8")

    # 解密消息
    err, decrypted = wx_crypt.decrypt_msg(msg_signature, timestamp, nonce, raw_body)
    if err != 0:
        print(f"解密失败: {decrypted}")
        return "decrypt failed", 403

    # 从这里开始任何异常都不能回 500（否则企业微信认为投递失败，消息会丢）
    try:
        # 解析为 dict
        msg = parse_message(decrypted)
        try:
            print(f"📩 收到消息: {json.dumps(msg, ensure_ascii=False)}")
        except Exception:
            pass

        # 记录日志
        try:
            log_message(msg)
        except Exception:
            pass

        # 处理消息
        reply_xml = handle_message(msg)

        # 无需被动回复（如 kf 事件已主动回复）
        if reply_xml is None:
            return ""

        # 加密回复
        err, encrypted = wx_crypt.encrypt_msg(reply_xml)
        if err != 0:
            return ""  # 放弃被动回复，但告知企微已收到

        response = make_response(encrypted)
        response.headers["Content-Type"] = "text/xml; charset=utf-8"
        return response
    except Exception as e:
        try:
            log_message({"callback_error": str(e)})
        except Exception:
            pass
        return ""  # 永远向企微返回成功，避免消息被判定投递失败


@app.route("/health", methods=["GET"])
def health():
    """健康检查"""
    return {"status": "ok", "time": datetime.now().isoformat()}


# ==================== 启动 ====================

if __name__ == "__main__":
    print("=" * 50)
    print("🚀 企业微信 Bot 服务启动中...")
    print(f"   监听地址: {SERVER_HOST}:{SERVER_PORT}")
    print(f"   回调路径: /wecom/callback")
    print(f"   健康检查: http://localhost:{SERVER_PORT}/health")
    print(f"   CorpID: {CORP_ID}")
    print(f"   AgentID: {AGENT_ID}")
    print("=" * 50)
    # 生产服务器优先用 waitress（2026-07-29 芙蕾雅改造）：
    # 多线程、稳定、无开发服务器告警。未安装则自动回退 Flask 自带服务器。
    # 安装：pip install waitress
    try:
        from waitress import serve
        print("✅ 使用 waitress 生产服务器")
        serve(app, host=SERVER_HOST, port=SERVER_PORT, threads=8)
    except ImportError:
        print("⚠ 未安装 waitress，回退到 Flask 开发服务器。"
              "请在服务器执行: pip install waitress 后重启本服务。")
        app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
