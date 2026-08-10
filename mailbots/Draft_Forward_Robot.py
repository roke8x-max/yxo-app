#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
草单转发机器人 · 规则 v3.1 实现
作者: 芙蕾雅 (2026-07-29)
规则依据: 芙蕾雅_小叽_留言/草单转发规则稿_v3.1_全量验证修正.md
接口约定: 小叽 2026-07-29 15:50 v3决策回信
  - TEST_MODE 期间不调 upsert_draft、不碰 yxo.db（只读 records 做匹配）
  - 台账 processed_mails 正式接入 yxo.db 前走留言箱错峰约定；TEST 期用独立 sqlite
  - 正式启用后 B 类刷新走小叽的 upsert_draft(allow_refresh=True)

核心规则（v3.1）:
  分类:
    草单件判据1: 附件名 ^[A-Z]{4}\\d{7}-\\d{6}-\\d{6}已加密\\.pdf$
    草单件判据2(兜底): 附件名 ^[A-Z]{4}\\d{7}\\.pdf$ 且 正文含更新草单类关键词
    A=原始草单 / B=更新草单: 由台账决定（该客编数字段此前转过草单=B, 否则=A）
    非草单件: 发件域 yxologistics.com => C1 问题反馈(转发)
              其他域(我方/客户)       => C2 确认回复(不转发只记录)
    主题含"运单号"且非草单件 => W 运单号通知(本期只记录, 留给运单号模块)
  匹配: 客编完全命中=自动转发; 数字段命中/仅箱号命中/未命中 => 待确认队列
  去重: 仅按 Message-ID 全局去重（跨 4 邮箱）; 扫描阶段 PEEK 不动已读状态
  已读: 【v3.2 骁洋拍板】已处理(转发/记录)的邮件在所有出现过的邮箱标为已读,
        避免同事误以为未处理; 未处理/待确认的保持原状
  通知: 【v3.2】每封转发给对应负责同事发企微通知(notify_by_name: 微信客服→应用回退)
  安全: TEST_MODE 默认开启, 全部外发只投递验证邮箱; DRAFT_FWD_LIVE=1 才放开
        TEST/停止模式(非 LIVE)下企微通知不发送（彻底静默, 不误打扰同事）; 仅 LIVE 才真实通知

  待确认（v3.3）: pending 邮件入 draft_pending 队列（领全局编号+落.eml）,
        企微通知**只发对应负责同事**（骁洋拍板: 不打扰他人; 无主单兜底发骁洋）,
        骁洋可随时回「待办」看全部; 确认/跳过命令在订舱助手(engine.py)侧
  提醒: LIVE 每轮顺带给挂起>1h 的待确认单补提醒(每小时一次, 0-8点静默)

环境变量 / 配置文件:
  DRAFT_FWD_LIVE=1   正式模式（默认0=TEST, 所有外发只到验证邮箱, 不写 yxo.db）
                      也可在 draft_robot_config.json 设 "live": true 启用（系统管理-配置管理页可改）
  draft_robot_config.json:
    live           true=正式转发 / false=TEST(只投验证邮箱)
    forward_since  只转发该时间(ISO, 如 2026-07-30T17:10:00)之后的新邮件; 留空且 LIVE=>首次启用自动设为此刻(历史全跳)
    accounts       监控邮箱列表(默认 4 个 @cqtransit.com)
  SAMPLE=A:2,B:2,C1:1  抽样发送（仅 TEST 用; 不设则处理全部）
  PENDING_SIM=毛骁洋,杨雅雯  模拟待确认（仅 TEST 用; 给每人挑一封入队+发通知）
  MARK_SEEN=0        关闭标已读（默认1=开）
  WECOM_NOTIFY=0     关闭企微通知（默认1=开; 依赖 requests, 无则自动跳过）
  MAX_BODY=2000      正文截取长度
"""
import imaplib
import smtplib
import email
import re
import os
import sys
import json
import sqlite3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime

from common_io import atomic_write_json, daemon_loop, to_local_naive

# ---------- 路径自适应（服务器 D:\YXO_DATA / 远程共享） ----------
# 用 config.py 是否存在判定（不能只看目录：空的残留目录会误判）
if os.path.exists(r"D:\YXO_DATA\WeComBot\config.py"):
    ROOT = r"D:\YXO_DATA"
else:
    ROOT = r"\\10.0.199.184\yxo_data"

WECOMBOT_DIR = os.path.join(ROOT, "WeComBot")
sys.path.insert(0, WECOMBOT_DIR)
from config import (  # noqa: E402
    SMTP_SERVER, SMTP_PORT, ACCOUNTS,
    DRAFT_WAYBILL_FOLDER, DRAFT_WAYBILL_IMAP_FOLDER,
    company_to_name, sender_for_company,
)

IMAP_SERVER = "imap.qiye.aliyun.com"
IMAP_PORT = 993

YXO_DB = os.path.join(ROOT, "yxo_app", "data", "yxo.db")

# ---------- 运行配置（可由系统管理-配置管理页编辑） ----------
# 字段: live=是否正式转发(否则 TEST 只投验证邮箱); forward_since=只转发此时间之后的新邮件(防历史);
#       accounts=监控邮箱列表
CFG_PATH = os.path.join(ROOT, "MailBots", "draft_robot_config.json")
_DEFAULT_ACCOUNTS = [
    "maoxiaoyang@cqtransit.com",
    "yangyawen@cqtransit.com",
    "fengqian@cqtransit.com",
    "hanwenhao@cqtransit.com",
]


def load_cfg():
    default = {"live": False, "forward_since": None, "accounts": list(_DEFAULT_ACCOUNTS)}
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    merged = dict(default)
    merged.update({k: cfg[k] for k in default if k in cfg and cfg[k] is not None})
    if not merged.get("accounts"):
        merged["accounts"] = list(_DEFAULT_ACCOUNTS)
    return merged


def save_cfg(cfg):
    atomic_write_json(CFG_PATH, cfg)


CFG = load_cfg()

DSK_CACHE = os.path.join(WECOMBOT_DIR, "cache", "dsk_config_cache.json")
DATA_DIR = os.path.join(ROOT, "MailBots", "data")
LOG_DIR = os.path.join(WECOMBOT_DIR, "logs")

# LIVE 开关：环境变量 DRAFT_FWD_LIVE=1 或 配置文件 live=true 任一即可启用正式转发
_is_live_env = os.environ.get("DRAFT_FWD_LIVE", "0") == "1"
_is_live_cfg = bool(CFG.get("live", False))
TEST_MODE = not (_is_live_env or _is_live_cfg)
VERIFY_MAILBOX = "3841559246@qq.com"
ADMIN_MAILBOX = "maoxiaoyang@cqtransit.com"
MAX_BODY = int(os.environ.get("MAX_BODY", "2000"))
MARK_SEEN = os.environ.get("MARK_SEEN", "1") == "1"
WECOM_NOTIFY = os.environ.get("WECOM_NOTIFY", "1") == "1"

# 企微通知（可选：无 requests / 无 cs_bot 时自动降级为不通知）
_notify_by_name = None
if WECOM_NOTIFY:
    try:
        from cs_bot.wecom_api import notify_by_name as _notify_by_name  # noqa: E402
    except Exception as _e:
        _notify_by_name = None

# 待确认队列（v3.3 共享模块）
import draft_pending  # noqa: E402
from config import company_to_name as _c2n  # noqa: E402

# 退舱跳过（2026-08-03）：状态=退舱的记录不再转发相关邮件
from db_write import is_box_cancelled  # noqa: E402
ADMIN_NAME = "毛骁洋"
PENDING_SIM = [x.strip() for x in os.environ.get("PENDING_SIM", "").split(",") if x.strip()]

# TEST 期台账独立存放，绝不碰 yxo.db；正式接入 yxo.db 前按约定留言错峰
LEDGER_DB = os.path.join(
    DATA_DIR, "draft_forward_ledger_TEST.db" if TEST_MODE else "draft_forward_ledger.db")

# 监控邮箱：默认 4 个；可由 draft_robot_config.json 的 accounts 覆盖（配置管理页可改）
MAILBOXES = CFG.get("accounts") or list(_DEFAULT_ACCOUNTS)

# ---------- 判据正则 ----------
ENC_PDF_RE = re.compile(r'^[A-Z]{4}\d{7}-\d{6}-\d{6}已加密\.pdf$', re.I)
BOX_PDF_RE = re.compile(r'^[A-Z]{4}\d{7}\.pdf$', re.I)
UPDATE_KEYWORDS = ("更新草单", "草单更新", "更新的草单", "请查收更新")
CONTAINER_RE = re.compile(r'[A-Z]{4}\d{7}(?![A-Za-z0-9])')
CODE_RE = re.compile(r'CQWLJT[0-9A-Za-z\-]+')
CODE_NUM_RE = re.compile(r'CQWLJT(\d+)')
YXO_DOMAIN = "yxologistics.com"

CATEGORY_LABEL = {
    "A": "【转草单】", "B": "【草单更新】", "C1": "【反馈问题】",
    "C2": "确认回复(不转发)", "W": "运单号通知(本期记录)",
}

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


def log(msg):
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(LOG_DIR, f"draft_forward_{today}.log")
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


# ---------- 台账 ----------
def ledger_conn():
    conn = sqlite3.connect(LEDGER_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS processed_mails(
        message_id TEXT PRIMARY KEY,
        mailboxes  TEXT,
        subject    TEXT,
        sender     TEXT,
        email_date TEXT,
        category   TEXT,
        code       TEXT,
        code_num   TEXT,
        box        TEXT,
        match_level TEXT,
        matched_rec_id INTEGER,
        action     TEXT,
        channel    TEXT,
        created_at INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS known_senders(
        sender TEXT PRIMARY KEY, first_seen TEXT)""")
    conn.commit()
    return conn


def ledger_seen_ids(conn):
    return {r[0] for r in conn.execute("SELECT message_id FROM processed_mails")}


def ledger_draft_nums(conn):
    """已转发过草单的客编数字段集合（用于 A/B 判定）。"""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT code_num FROM processed_mails "
        "WHERE category IN ('A','B') AND code_num IS NOT NULL")}


# ---------- 邮件解析 ----------
def decode_any(raw):
    if not raw:
        return ""
    out = ""
    for part, cs in decode_header(raw):
        if isinstance(part, bytes):
            out += part.decode(cs or "utf-8", errors="replace")
        else:
            out += str(part)
    return out


def extract_body_text(msg, limit=MAX_BODY):
    plain, html = None, None
    for part in msg.walk():
        if part.is_multipart():
            continue
        ct = part.get_content_type()
        disp = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in disp:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        cs = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(cs, errors="replace")
        except Exception:
            text = payload.decode("utf-8", errors="replace")
        if ct == "text/plain" and plain is None:
            plain = text
        elif ct == "text/html" and html is None:
            html = text
    if plain:
        return plain[:limit]
    if html:
        return re.sub(r"<[^>]+>", " ", html)[:limit]
    return ""


def attachment_names(msg):
    names = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn:
            names.append(decode_any(fn))
    return names


def parse_ids(subject, att_names):
    """从主题(优先)与附件名提取 客编/箱号/客编数字段。"""
    code, box = None, None
    codes = CODE_RE.findall(subject)
    if codes:
        code = codes[0]
    boxes = CONTAINER_RE.findall(subject + " ")
    if boxes:
        box = boxes[0]
    if not box:
        for fn in att_names:
            m = CONTAINER_RE.search(fn + " ")
            if m:
                box = m.group(0)
                break
    num = None
    if code:
        m = CODE_NUM_RE.match(code)
        if m:
            num = m.group(1)
    return code, num, box


def is_draft_attachment(att_names, body):
    """v3.1 草单件判据: 已加密命名 或 (箱号.pdf + 正文更新关键词)。"""
    for fn in att_names:
        if ENC_PDF_RE.match(fn.strip()):
            return True, fn
    for fn in att_names:
        if BOX_PDF_RE.match(fn.strip()) and any(k in body for k in UPDATE_KEYWORDS):
            return True, fn
    return False, None


def classify(subject, sender, att_names, body, draft_nums, code_num):
    """返回 category: A/B/C1/C2/W"""
    is_draft, _ = is_draft_attachment(att_names, body)
    if is_draft:
        key = code_num or (att_names[0] if att_names else subject)
        return "B" if (code_num and code_num in draft_nums) else "A"
    if "运单号" in subject:
        return "W"
    domain = sender.split("@")[-1].lower() if "@" in sender else ""
    return "C1" if domain == YXO_DOMAIN else "C2"


# ---------- 匹配 ----------
def load_records():
    rows = []
    try:
        conn = sqlite3.connect(f"file:{YXO_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        for r in conn.execute('SELECT id, "客户编码", "箱号", "开票子公司名称", "状态" FROM records'):
            rows.append({
                "id": r["id"],
                "code": str(r["客户编码"] or "").strip(),
                "box": str(r["箱号"] or "").strip(),
                "company": str(r["开票子公司名称"] or "").strip(),
                "status": str(r["状态"] or "").strip(),
            })
        conn.close()
    except Exception as e:
        log(f"⚠ 只读打开 yxo.db 失败: {e}")
    return rows


def match_record(records, code, num, box):
    """返回 (level, rec)  level: full/num/box/none"""
    if code:
        hits = [r for r in records if r["code"] == code]
        if hits:
            return "full", hits[0]
    if num:
        hits = [r for r in records if r["code"].startswith("CQWLJT") and
                CODE_NUM_RE.match(r["code"]) and CODE_NUM_RE.match(r["code"]).group(1) == num]
        if hits:
            return "num", hits[0]
    if box:
        hits = [r for r in records if r["box"] == box]
        if len(hits) == 1:
            return "box", hits[0]
        if len(hits) > 1:
            return "box_multi", hits[0]
    return "none", None


def load_dsk_routing():
    if not os.path.exists(DSK_CACHE):
        return {}, {}
    try:
        with open(DSK_CACHE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        return cache.get("default_map", {}), cache.get("box_record_map", {})
    except Exception as e:
        log(f"⚠ 读 DSK 路由缓存失败: {e}")
        return {}, {}


def resolve_recipients(box, company, default_map, box_map):
    info = box_map.get(box or "")
    if info and info.get("to"):
        return info["to"], info.get("cc", []), "箱号精确配置"
    if company and company in default_map:
        info = default_map[company]
        if info.get("to"):
            return info["to"], info.get("cc", []), "DEFAULT回退"
    return [], [], ""


# ---------- 扫描（PEEK, 永不标已读） ----------
def scan_mailbox(addr, password):
    items = []
    try:
        m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        m.login(addr, password)
    except Exception as e:
        log(f"❌ {addr} IMAP 登录失败: {e}")
        return items
    res, _ = m.select(DRAFT_WAYBILL_IMAP_FOLDER, readonly=True)
    if res != "OK":
        log(f"⚠ {addr} 无「{DRAFT_WAYBILL_FOLDER}」文件夹, 跳过")
        m.logout()
        return items
    res, data = m.search(None, "ALL")
    if res != "OK":
        m.logout()
        return items
    for mid in data[0].split():
        try:
            _, md = m.fetch(mid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(md[0][1])
            atts = attachment_names(msg)
            body = extract_body_text(msg)
            try:
                parsed = parsedate_to_datetime(msg.get("Date"))
                # 时区归一（体检报告 3.3）：邮件 Date 可能带时区，统一转成北京时间 naive 再存，
                # 避免把 UTC 直接当本地导致 8 小时偏差。
                dt = to_local_naive(parsed) if parsed is not None else None
                date_s = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""
            except Exception:
                dt, date_s = None, ""
            items.append({
                "mailbox": addr,
                "message_id": str(msg.get("Message-ID", "")).strip(),
                "subject": decode_any(msg.get("Subject", "")),
                "sender": parseaddr(msg.get("From", ""))[1].lower(),
                "date": date_s,
                "dt": dt,
                "atts": atts,
                "body": body,
            })
        except Exception as e:
            log(f"  ⚠ {addr} 邮件解析异常: {e}")
    m.logout()
    log(f"  {addr}: 扫到 {len(items)} 封（PEEK 只读）")
    return items


def fetch_raw_by_msgid(addr, password, message_id):
    """按 Message-ID 重取原始邮件（发送样本时用）。"""
    m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    m.login(addr, password)
    m.select(DRAFT_WAYBILL_IMAP_FOLDER, readonly=True)
    res, data = m.search(None, "HEADER", "Message-ID", message_id)
    raw = None
    if res == "OK" and data[0]:
        _, md = m.fetch(data[0].split()[0], "(BODY.PEEK[])")
        raw = md[0][1]
    m.logout()
    return raw


def mark_seen_everywhere(message_id, boxes_seen):
    """【v3.2】已处理邮件在所有出现过的邮箱标已读（\\Seen）。
    只对指定 Message-ID 精确标记, 绝不批量; 失败只记日志不中断主流程。"""
    if not (MARK_SEEN and message_id):
        return []
    done = []
    for addr in boxes_seen:
        pwd = ACCOUNTS.get(addr)
        if not pwd:
            continue
        try:
            m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            m.login(addr, pwd)
            m.select(DRAFT_WAYBILL_IMAP_FOLDER)  # 写模式
            res, data = m.search(None, "HEADER", "Message-ID", message_id)
            if res == "OK" and data[0]:
                for num in data[0].split():
                    m.store(num, "+FLAGS", "\\Seen")
                done.append(addr)
            m.logout()
        except Exception as e:
            log(f"  ⚠ 标已读失败 {addr}: {e}")
    return done


def wecom_notify_person(real_name, text):
    """给负责同事发企微通知; 通道由 notify_by_name 决定(微信客服→应用回退)。
    返回 (ok, channel)。"""
    if not (_notify_by_name and real_name):
        return False, ""
    try:
        return _notify_by_name(real_name, text)
    except Exception as e:
        log(f"  ⚠ 企微通知异常 {real_name}: {e}")
        return False, ""


def wecom_summary_to_admin(sent_report, stats):
    """规则 #118: 仅当本轮有实际转发(sent_report 非空)才给管理员(毛骁洋)发微信/企微摘要;
    空跑(无转发)绝不发。返回 (ok, channel)。"""
    if not (_notify_by_name and sent_report):
        return False, ""
    n = len(sent_report)
    lines = [f"📋 草单转发机器人 · 正式汇总 {datetime.now().strftime('%H:%M')}",
             f"本次实际转发 {n} 封"]
    for p in sent_report[:8]:
        dst = ",".join(p.get("to") or []) or (p.get("company") or "-")
        lines.append(f"· [{p['category']}] {p.get('code') or '-'} → {p.get('notify_to') or dst}")
    if n > 8:
        lines.append(f"…其余 {n - 8} 封")
    try:
        return _notify_by_name(ADMIN_NAME, "\n".join(lines))
    except Exception as e:
        log(f"  ⚠ 毛骁洋微信/企微汇总异常: {e}")
        return False, ""


def build_notify_text(p, test_mode):
    label = {"A": "转草单", "B": "草单更新", "C1": "反馈问题"}.get(p["category"], p["category"])
    head = "【测试·" + label + "】" if test_mode else "【" + label + "】"
    # 主题放宽到 200 字符（企微文本上限 2048 字节, 60 太狠会切掉尾部箱号）
    subj = p["subject"]
    if len(subj) > 200:
        subj = subj[:200] + "…"
    lines = [head + " 草单转发机器人通知",
             f"主题: {subj}",
             f"客编: {p['code'] or '-'}  箱号: {p['box'] or '-'}",
             f"公司: {p['company'] or '-'}"]
    if p["category"] == "B":
        lines.append("注意: 此为更新版草单, 旧版作废")
    if p["action"] == "forward":
        dst = "验证邮箱(测试)" if test_mode else (",".join(p["to"]) or "-")
        lines.append(f"已转发至: {dst}")
        if test_mode:
            lines.append(f"正式收件人将是: {','.join(p['to']) or '-'}")
    else:
        lines.append(f"状态: 待确认(匹配级别={p['level']}), 正式运行时请回「待办」查看")
    if test_mode:
        lines.append("—— 本条为测试预览, 无需操作 ——")
    return "\n".join(lines)


# ---------- 转发 ----------
def build_forward(raw_bytes, category, extra_note=None):
    orig = email.message_from_bytes(raw_bytes)
    subject = decode_any(orig.get("Subject", ""))
    out = MIMEMultipart("mixed")
    html_body, plain_body = None, None
    att_parts = []
    for part in orig.walk():
        if part.is_multipart():
            continue
        ct = part.get_content_type()
        disp = str(part.get("Content-Disposition", "")).lower()
        fn = part.get_filename()
        if "attachment" in disp or fn:
            att_parts.append((part, decode_any(fn) if fn else "attachment.bin"))
        elif ct == "text/html" and html_body is None:
            p = part.get_payload(decode=True)
            if p:
                html_body = p.decode(part.get_content_charset() or "utf-8", errors="replace")
        elif ct == "text/plain" and plain_body is None:
            p = part.get_payload(decode=True)
            if p:
                plain_body = p.decode(part.get_content_charset() or "utf-8", errors="replace")
    note = ""
    if category == "B":
        note = "【提示】此为更新版草单，此前版本作废，请以本邮件附件为准。"
    if extra_note:
        note = (note + "\n" + extra_note).strip()
    if html_body:
        if note:
            html_body = f'<p style="color:#c00;font-weight:bold">{note}</p>' + html_body
        out.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        text = ((note + "\n\n") if note else "") + (plain_body or "")
        out.attach(MIMEText(text, "plain", "utf-8"))
    for part, fn in att_parts:
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        np = MIMEBase(part.get_content_maintype(), part.get_content_subtype())
        np.set_payload(payload)
        encoders.encode_base64(np)
        np.add_header("Content-Disposition", "attachment", filename=("utf-8", "", fn))
        out.attach(np)
    return out, subject


def smtp_send(msg, sender_email, sender_pwd, to_list, cc_list=None):
    cc_list = cc_list or []
    msg["From"] = sender_email
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as s:
        s.login(sender_email, sender_pwd)
        s.sendmail(sender_email, list(set(to_list + cc_list)), msg.as_string())


# ---------- 主流程 ----------
def parse_sample_env():
    """SAMPLE=A:2,B:2,C1:1 -> dict; 空 = 不抽样(全量)。"""
    raw = os.environ.get("SAMPLE", "").strip()
    if not raw:
        return None
    out = {}
    for seg in raw.split(","):
        if ":" in seg:
            k, v = seg.split(":", 1)
            try:
                out[k.strip().upper()] = int(v)
            except ValueError:
                pass
    return out or None


def run():
    mode = "TEST(全部外发→验证邮箱)" if TEST_MODE else "LIVE 正式"
    sample = parse_sample_env()
    log("=" * 60)
    log(f"草单转发机器人 v3.1 启动 · 模式: {mode}" + (f" · 抽样: {sample}" if sample else ""))
    log(f"台账: {LEDGER_DB}")
    log("=" * 60)

    # 历史邮件护栏：LIVE 模式下只转发 forward_since 之后的新邮件
    # forward_since 为空且处于 LIVE => 首次启用，自动设为「此刻」，历史全部跳过（安全默认）
    forward_since = None
    fs_raw = CFG.get("forward_since")
    if fs_raw:
        try:
            forward_since = datetime.strptime(fs_raw, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            forward_since = None
    if not TEST_MODE and forward_since is None:
        forward_since = datetime.now()
        CFG["forward_since"] = forward_since.strftime("%Y-%m-%dT%H:%M:%S")
        save_cfg(CFG)
        log(f"🛡 首次启用 LIVE：已设 forward_since={CFG['forward_since']}，仅转发此后的新邮件（历史已跳过）")
    elif not TEST_MODE and forward_since is not None:
        log(f"🛡 LIVE 模式：只转发 {CFG['forward_since']} 之后的新邮件（历史跳过）")

    conn = ledger_conn()
    seen_ids = ledger_seen_ids(conn)
    draft_nums = ledger_draft_nums(conn)
    records = load_records()
    default_map, box_map = load_dsk_routing()
    log(f"records(只读): {len(records)} 行 · DSK路由: {len(default_map)} 公司")

    # 1) 扫描 4 邮箱（PEEK）
    all_items = []
    for addr in MAILBOXES:
        pwd = ACCOUNTS.get(addr)
        if not pwd:
            log(f"⚠ 无 {addr} 密码, 跳过")
            continue
        all_items.extend(scan_mailbox(addr, pwd))

    # 2) 跨邮箱 Message-ID 全局去重（保留最早入库邮箱, 记录出现过的邮箱）
    uniq = {}
    for it in all_items:
        mid = it["message_id"] or f"NOID::{it['mailbox']}::{it['subject']}::{it['date']}"
        if mid in uniq:
            uniq[mid]["boxes_seen"].append(it["mailbox"])
        else:
            it["boxes_seen"] = [it["mailbox"]]
            uniq[mid] = it
    items = sorted(uniq.values(), key=lambda x: x["dt"] or datetime.min)
    log(f"去重后待处理: {len(items)} 封 (原 {len(all_items)} 封)")

    # 3) 逐封分类 + 匹配（按时间序, A/B 由台账+本轮已见决定）
    plan = []
    new_senders = set()
    n_historical_skip = 0
    known = {r[0] for r in conn.execute("SELECT sender FROM known_senders")}
    for it in items:
        mid = it["message_id"]
        if mid and mid in seen_ids:
            continue  # 台账已处理过
        # 历史邮件护栏：LIVE 模式下日期早于 forward_since 的邮件直接跳过，不转发
        if not TEST_MODE and forward_since is not None:
            dt = it["dt"]
            # 时区归一（体检报告 3.3）：统一转成北京时间 naive 再与 forward_since 比较，
            # 修正原先 dt.replace(tzinfo=None) 把 UTC 当本地、最大 8 小时偏差的问题。
            dt = to_local_naive(dt) if dt is not None else None
            if dt is None or dt < forward_since:
                n_historical_skip += 1
                # 记录历史 Message-ID，确保后续运行不再重复扫描/转发（用户要求：历史只记录不转发）
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO processed_mails VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (mid, ",".join(it.get("boxes_seen", [])), it.get("subject", ""), it.get("sender", ""),
                         it.get("date", ""), "", "", "", "", "", "", "historical_skip", "",
                         int(datetime.now().timestamp())))
                    conn.commit()
                except Exception:
                    pass
                continue
        code, num, box = parse_ids(it["subject"], it["atts"])
        cat = classify(it["subject"], it["sender"], it["atts"], it["body"], draft_nums, num)
        if cat in ("A", "B") and num:
            draft_nums.add(num)
        level, rec = match_record(records, code, num, box)
        if it["sender"] and it["sender"] not in known:
            new_senders.add(it["sender"])
        company = rec["company"] if rec else None
        to_list, cc_list, route_src = resolve_recipients(box, company, default_map, box_map)
        # ── 退舱跳过（2026-08-03）：状态=退舱的记录不再转发相关邮件 ──
        if (rec is not None and rec.get("status") == "退舱") or (box and is_box_cancelled(box)):
            log(f"  ⏭ 退舱跳过: {it.get('subject', '')[:40]} (箱号 {box or (rec.get('box', '') if rec else '')})")
            action = "cancel"
        else:
            # 动作决策
            if cat in ("C2", "W"):
                action = "record"
            elif cat in ("A", "B", "C1"):
                action = "forward" if (level == "full" and to_list) else "pending"
        plan.append({
            **{k: it[k] for k in ("mailbox", "message_id", "subject", "sender", "date", "atts", "boxes_seen")},
            "code": code, "num": num, "box": box, "category": cat,
            "level": level, "rec_id": rec["id"] if rec else None,
            "company": company, "to": to_list, "cc": cc_list,
            "route_src": route_src, "action": action,
        })

    stats = {}
    for p in plan:
        stats[p["category"]] = stats.get(p["category"], 0) + 1
    log(f"分类统计: {stats}")
    if not TEST_MODE and n_historical_skip:
        log(f"🛡 已跳过历史邮件(早于 forward_since): {n_historical_skip} 封")

    # 4) 选择要实际发送的集合
    to_send = [p for p in plan if p["action"] == "forward"]
    if sample:
        picked = []
        # 强制包含兜底案例（VKMU0019432）
        force = [p for p in plan if p["category"] in ("A", "B")
                 and any("VKMU0019432" in a for a in p["atts"])]
        for cat, n in sample.items():
            pool = [p for p in plan if p["category"] == cat and p not in picked]
            # 优先取可转发的, 不足补 pending 的（测试要看各种情况）
            pool.sort(key=lambda x: 0 if x["action"] == "forward" else 1)
            # v3.2: 同类别内尽量分散到不同负责人, 让每位同事都能收到通知
            chosen, seen_names = [], set()
            for p in pool:
                nm = company_to_name(p["company"]) if p["company"] else None
                if nm and nm in seen_names:
                    continue
                chosen.append(p)
                if nm:
                    seen_names.add(nm)
                if len(chosen) >= n:
                    break
            for p in pool:  # 不够再回补
                if len(chosen) >= n:
                    break
                if p not in chosen:
                    chosen.append(p)
            picked.extend(chosen)
        for f in force:
            if f not in picked:
                picked.append(f)
        to_send = picked
        log(f"抽样后待发送: {len(to_send)} 封")

    # 5) 发送
    sent_report = []
    admin_pwd = ACCOUNTS.get(ADMIN_MAILBOX)
    for p in to_send:
        try:
            src_addr = p["mailbox"]
            raw = fetch_raw_by_msgid(src_addr, ACCOUNTS[src_addr], p["message_id"])
            if not raw:
                log(f"  ⚠ 重取失败: {p['subject'][:40]}")
                continue
            label = CATEGORY_LABEL.get(p["category"], "")
            orig_to = ",".join(p["to"]) if p["to"] else "(待确认/无路由)"
            note = None
            if p["action"] == "pending":
                note = f"【测试说明】此邮件匹配级别={p['level']}, 正式运行时将进入待确认队列而非直接转发。"
            fwd, subj = build_forward(raw, p["category"], note)
            if TEST_MODE:
                fwd["Subject"] = f"[测试·{label}→原收件人:{orig_to}] {subj}"
                real_to = [VERIFY_MAILBOX]
                real_cc = []
                s_email, s_pwd = ADMIN_MAILBOX, admin_pwd
            else:
                fwd["Subject"] = f"{label}{subj}" if p["category"] == "B" else subj
                real_to, real_cc = p["to"], p["cc"]
                sen = sender_for_company(p["company"]) if p["company"] else None
                s_email, s_pwd = (sen if (sen and sen[1]) else (ADMIN_MAILBOX, admin_pwd))
            smtp_send(fwd, s_email, s_pwd, real_to, real_cc)
            log(f"  ✅ 发送 [{p['category']}|{p['level']}] {subj[:50]} → {real_to}")

            # 统一转发日志（供每日 8 点汇总邮件用；失败不影响转发）
            try:
                import forward_log
                forward_log.record(
                    "草单", owner=(company_to_name(p["company"]) if p["company"] else "") or "",
                    company=p.get("company") or "", code=p.get("code") or "",
                    box=p.get("box") or "", subject=subj,
                    to_list=real_to, sender=s_email, test=TEST_MODE)
            except Exception:
                pass

            # 企微通知对应负责同事：仅 LIVE 正式模式真实发送；TEST/停止态不通知同事（避免误打扰）
            channel = ""
            responsible = company_to_name(p["company"]) if p["company"] else None
            if responsible and not TEST_MODE:
                ok, channel = wecom_notify_person(responsible, build_notify_text(p, TEST_MODE))
                log(f"  📲 企微通知 {responsible}: {'✅' if ok else '❌'} 通道={channel or '-'}")
            else:
                log(f"  ⚠ 无法定位负责人(公司={p['company']}), 跳过企微通知")
            p["notify_to"] = responsible
            p["notify_channel"] = channel

            # 标已读（v3.2）
            seen_at = mark_seen_everywhere(p["message_id"], p["boxes_seen"])
            if seen_at:
                log(f"  📖 已标已读: {','.join(seen_at)}")
            sent_report.append(p)
            # 写台账（TEST 写独立测试库）
            conn.execute(
                "INSERT OR REPLACE INTO processed_mails VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["message_id"], ",".join(p["boxes_seen"]), p["subject"], p["sender"],
                 p["date"], p["category"], p["code"], p["num"], p["box"], p["level"],
                 p["rec_id"], p["action"] + ("_test" if TEST_MODE else ""),
                 channel or "email", int(datetime.now().timestamp())))
            conn.commit()
        except Exception as e:
            log(f"  ❌ 发送失败 {p['subject'][:40]}: {e}")

    # 5.4) 待确认入队（v3.3）
    #   LIVE: 全部 pending 入队+通知负责同事
    #   TEST: 仅 PENDING_SIM 指定的人各挑一封模拟（供演示确认流程, 不占正式队列语义）
    def _pending_reason(p):
        if p["level"] == "num":
            return (f"客编数字段命中但完整客编不同（库里可能到站字母未更新）: "
                    f"邮件={p['code'] or '-'}")
        if p["level"] == "box":
            return "客编未命中, 仅箱号命中（请核对是否同一票）"
        if p["level"] == "box_multi":
            return "箱号命中多行记录（可能箱子复用, 需人工挑）"
        if p["level"] == "full":
            return "记录已完全命中, 但该公司没有配置转发路由"
        return "客编与箱号在系统里都没找到"

    def _enqueue_and_notify(p, simulated=False):
        raw = None
        try:
            raw = fetch_raw_by_msgid(p["mailbox"], ACCOUNTS[p["mailbox"]], p["message_id"])
        except Exception as e:
            log(f"  ⚠ pending 取原件失败: {e}")
        owner = (_c2n(p["company"]) if p["company"] else None) or ADMIN_NAME
        cands = []
        if p["rec_id"]:
            cands.append(f"记录#{p['rec_id']} {p['company'] or ''} 客编库内值待核")
        n = draft_pending.add_pending({
            **{k: p[k] for k in ("message_id", "subject", "sender", "date",
                                 "category", "code", "num", "box", "company",
                                 "boxes_seen", "to", "cc")},
            "owner": owner, "reason": _pending_reason(p), "candidates": cands,
        }, raw, test=TEST_MODE, simulated=simulated)
        # 不再逐条弹通知 —— 改为本轮结束后按 owner 汇总一条（见 _send_batch_pending_notify）
        log(f"  📥 待确认 #{n} owner={owner} 入队 [{p['category']}|{p['level']}] {p['subject'][:40]}")
        return n, p, owner

    def _send_batch_pending_notify(enqueued):
        """按负责同事合并, 每人弹一条汇总通知（避免一封一封打扰）。"""
        from collections import defaultdict
        by_owner = defaultdict(list)
        for n, p, owner in enqueued:
            by_owner[owner].append((n, p))
        for owner, items in by_owner.items():
            lines = [f"📥 本次有 {len(items)} 封草单进入待确认队列，请逐条处理："]
            for n, p in items:
                label = CATEGORY_LABEL.get(p["category"], p["category"])
                lines.append(f"· #{n}【{label}】{(p['subject'] or '')[:40]}")
                lines.append(f"    客编:{p['code'] or '-'} 箱号:{p['box'] or '-'}")
            lines.append("")
            lines.append("回「确认 编号」转发写库，回「跳过 编号」忽略。")
            lines.append("批量：确认全部 / 全部跳过 / 跳过我的 / 跳过别人的 / 跳过 姓名")
            ok, ch = wecom_notify_person(owner, "\n".join(lines))
            log(f"  📲 待确认汇总通知 {owner}: {'✅' if ok else '❌'}({ch or '-'}) 共{len(items)}条")

    queued_ids = draft_pending.known_message_ids()
    pend_all = [p for p in plan if p["action"] == "pending"
                and p["message_id"] not in queued_ids]
    if not TEST_MODE:
        enqueued = []
        for p in pend_all:
            try:
                enqueued.append(_enqueue_and_notify(p))
            except Exception as e:
                log(f"  ❌ pending 入队失败 {p['subject'][:40]}: {e}")
        if enqueued:
            try:
                _send_batch_pending_notify(enqueued)
            except Exception as e:
                log(f"  ⚠ 待确认汇总通知异常: {e}")
        # 顺带补提醒（挂起>1h, 每小时一次, 0-8点静默）
        try:
            nr = draft_pending.remind_due(wecom_notify_person)
            if nr:
                log(f"  ⏰ 补提醒 {nr} 条")
        except Exception as e:
            log(f"  ⚠ 补提醒异常: {e}")
    elif PENDING_SIM:
        # 模拟: 给每位指定同事挑一封 pending（没有真 pending 就拿一封 A/B/C1 顶上,
        # 指定 owner 演示; simulated=1 便于测试后清理）
        for name in PENDING_SIM:
            cand = None
            for p in pend_all:
                if ((_c2n(p["company"]) if p["company"] else None) or ADMIN_NAME) == name:
                    cand = p
                    break
            if cand is None:
                pool = [p for p in plan if p["category"] in ("A", "B", "C1")
                        and p["message_id"] not in queued_ids]
                pool.sort(key=lambda x: 0 if ((_c2n(x["company"]) if x["company"] else None) == name) else 1)
                cand = pool[0] if pool else None
            if cand is None:
                log(f"  ⚠ 找不到可给 {name} 模拟的邮件")
                continue
            try:
                cand = dict(cand)
                cand["company_owner_override"] = name
                # 强制 owner=name（模拟场景）
                raw = None
                try:
                    raw = fetch_raw_by_msgid(cand["mailbox"], ACCOUNTS[cand["mailbox"]], cand["message_id"])
                except Exception:
                    pass
                reason = _pending_reason(cand) + "（模拟演示单）"
                n = draft_pending.add_pending({
                    **{k: cand[k] for k in ("message_id", "subject", "sender", "date",
                                            "category", "code", "num", "box", "company",
                                            "boxes_seen", "to", "cc")},
                    "owner": name, "reason": reason, "candidates": [],
                }, raw, test=True, simulated=True)
                text = draft_pending.build_pending_notify(
                    n, {**cand, "reason": reason, "candidates": []}, test=True)
                ok, ch = wecom_notify_person(name, text)
                log(f"  📥 模拟待确认 #{n} → {name} 通知{'✅' if ok else '❌'}({ch or '-'})")
                queued_ids.add(cand["message_id"])
            except Exception as e:
                log(f"  ❌ 模拟入队失败 {name}: {e}")

    # 5.5) 只记录类(C2/W)入台账+标已读——仅 LIVE（TEST 不动真实邮箱、不占台账）
    if not TEST_MODE:
        for p in [x for x in plan if x["action"] == "record"]:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO processed_mails VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p["message_id"], ",".join(p["boxes_seen"]), p["subject"], p["sender"],
                     p["date"], p["category"], p["code"], p["num"], p["box"], p["level"],
                     p["rec_id"], "record", "", int(datetime.now().timestamp())))
                conn.commit()
                mark_seen_everywhere(p["message_id"], p["boxes_seen"])
            except Exception as e:
                log(f"  ⚠ record 入账失败 {p['subject'][:40]}: {e}")

    # LIVE 模式下若本轮无任何实际动作(空跑, 如午夜前的历史护栏期), 不发摘要避免刷屏
    _anything = bool(sent_report) or bool(new_senders) or bool(stats) or any(x["action"] == "pending" for x in plan)
    # 6) 汇总邮件（发验证邮箱 / 正式期发管理员）
    try:
        lines = [f"草单转发机器人 · {'测试' if TEST_MODE else '正式'}运行汇总",
                 f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                 f"扫描: 4邮箱共 {len(all_items)} 封, 去重后 {len(items)} 封, 待处理 {len(plan)} 封",
                 f"分类: " + " / ".join(f"{k}:{v}" for k, v in sorted(stats.items())),
                 ""]
        lines.append("— 本次实际发送 —")
        for p in sent_report:
            lines.append(f"[{p['category']}|{p['level']}] {p['subject'][:60]}")
            lines.append(f"    客编:{p['code'] or '-'} 箱号:{p['box'] or '-'} 公司:{p['company'] or '-'} 原收件人:{','.join(p['to']) or '(待确认)'}")
            lines.append(f"    企微通知:{p.get('notify_to') or '-'} 通道:{p.get('notify_channel') or '未发出'}")
        lines.append("")
        lines.append("— C2 确认回复(按规则不转发, 仅记录) 样例 —")
        for p in [x for x in plan if x["category"] == "C2"][:5]:
            lines.append(f"[C2] {p['subject'][:60]}  发件人:{p['sender']}")
        wcount = len([x for x in plan if x["category"] == "W"])
        if wcount:
            lines.append(f"\n— W 运单号通知 {wcount} 封(本期只记录, 留给运单号模块) —")
        if new_senders:
            lines.append("\n— 新发件人(首次出现, 单证部人员变动监测) —")
            for s in sorted(new_senders):
                lines.append(f"  {s}")
        pend = [x for x in plan if x["action"] == "pending"]
        lines.append(f"\n待确认队列(正式运行时将走企微确认流程): {len(pend)} 封")
        for p in pend[:10]:
            lines.append(f"  [{p['category']}|{p['level']}] {p['subject'][:55]}")
        summary = MIMEMultipart()
        summary.attach(MIMEText("\n".join(lines), "plain", "utf-8"))
        summary["Subject"] = f"[{'测试' if TEST_MODE else '正式'}] 草单转发机器人运行汇总 {datetime.now().strftime('%H:%M')}"
        # 规则 #118: 仅当本轮有实际转发(sent_report 非空)才给毛骁洋发通知; 空跑绝不发
        _has_forward = bool(sent_report)
        if TEST_MODE:
            # 测试期: 汇总发验证邮箱(原行为), 不骚扰毛骁洋
            smtp_send(summary, ADMIN_MAILBOX, admin_pwd, [VERIFY_MAILBOX])
            log("  📧 测试汇总已发验证邮箱(不通知毛骁洋)")
        elif _has_forward:
            # 正式期且有实际转发: 邮件发毛骁洋 + 微信/企微摘要
            smtp_send(summary, ADMIN_MAILBOX, admin_pwd, [ADMIN_MAILBOX])
            log("  📧 正式汇总已发毛骁洋邮箱")
            ok, ch = wecom_summary_to_admin(sent_report, stats)
            log(f"  📲 毛骁洋微信/企微汇总: {'✅' if ok else '❌'} 通道={ch or '-'}")
        else:
            log("  ⏭ LIVE 空跑(无实际转发 sent_report 为空), 跳过毛骁洋所有通知")
    except Exception as e:
        log(f"  ⚠ 汇总邮件发送失败: {e}")

    # 7) 记录新发件人
    for s in new_senders:
        conn.execute("INSERT OR IGNORE INTO known_senders VALUES (?,?)",
                     (s, datetime.now().strftime("%Y-%m-%d")))
    conn.commit()
    conn.close()
    log(f"完成: 发送 {len(sent_report)} 封 | 分类 {stats} | 新发件人 {len(new_senders)} 个")
    log("=" * 60)
    return len(sent_report)


if __name__ == "__main__":
    import traceback
    try:
        daemon_loop("draft", run)
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        sys.exit(1)
