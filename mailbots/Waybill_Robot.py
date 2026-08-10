#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运单号转发机器人 · v1.0
作者: 小叽 (2026-07-30)  设计依据: 芙蕾雅_开发沙盒/app_改动/运单号转发机器人设计方案.md
规则来源: 骁洋 2026-07-30 四点拍板（运单号不入库 / 匹配负责同事 / 单证驳回单独告警 / 加入待确认闭环）

核心规则:
  扫描: 4 人邮箱的「运单号」文件夹(IMAP modified-UTF7: &j9BTVVP3-)
  判定: 发件人白名单 docwbfb@yxologistics.com（系统自动推送, 零关键词, 不误伤下游）
  类型:
    A 运单号下发: 主题含班列/客户, 1 个 .xls 附件(多箱), 含 箱号/RWB NO(运单号)/客户编码
    B 单证审核驳回: 主题「单证审核驳回」, 无附件, 正文含客户编码
  匹配: 复用草单机器人 match_record(客户编码/箱号) → 负责同事(company_to_name)
  入库: 运单号【不写 yxo.db】, 仅体现在企微通知文本 + pending 记录里
  闭环: A/B 均进待确认队列(draft_pending, category=WAY_A/WAY_B) + 企微通知(确认/跳过)
  通知(规则 #118): 仅当本轮有实际动作(给负责同事发了企微通知)才给毛骁洋发摘要(邮件+微信/企微);
        空跑(无匹配/无动作)绝不发; TEST 期只投验证邮箱, 不骚扰毛骁洋
  安全: live 默认 false(TEST, 只投验证邮箱, 不真通知同事/不污染 pending 队列/不标已读);
        切 live=true 才正式运行; forward_since 护栏只处理之后的新邮件

配置(waybill_robot_config.json / 环境变量 WAYBILL_LIVE):
  live            true=正式 / false=TEST(只投验证邮箱, 不通知同事)
  forward_since   只处理该时间之后的新邮件; 留空且 LIVE=>首次启用自动设为此刻
  accounts        监控邮箱列表(默认 4 个 @cqtransit.com)
  folder          扫描文件夹(默认 运单号)
  sender_whitelist 发件人白名单(默认 docwbfb@yxologistics.com)

⚠ 上线前待办(属骁洋「排版」范畴, 本机器人先交付代码+TEST默认):
  1) engine.py 的「确认/跳过」命令需识别 WAY_A/WAY_B 类 pending(现仅认 A/B/C1/C2/W);
  2) 运单号不入库, 故 WAY_A「确认」语义≠草单的「转发写库」, 需定义(如: 仅标记已处理+清理.eml);
  3) start_dsk.bat 追加本机器人启动行后, 调度每 15 分钟拉起(当前未加入, 保持停)。
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
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime

from common_io import atomic_write_json, daemon_loop, to_local_naive

# ---------- 路径自适应（方案 B：YXO_ROOT 显式固定，写库永不回退 SMB） ----------
YXO_ROOT = os.environ.get("YXO_ROOT", r"D:\YXO_DATA").rstrip("\\/")
if os.path.exists(os.path.join(YXO_ROOT, "WeComBot", "config.py")):
    ROOT = YXO_ROOT
else:
    _SMB_ROOT = r"\\10.0.199.184\yxo_data"
    ROOT = _SMB_ROOT if os.path.exists(os.path.join(_SMB_ROOT, "WeComBot", "config.py")) else YXO_ROOT

WECOMBOT_DIR = os.path.join(ROOT, "WeComBot")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WECOMBOT_DIR)
sys.path.insert(0, HERE)
from config import (  # noqa: E402
    SMTP_SERVER, SMTP_PORT, ACCOUNTS,
    company_to_name, sender_for_company,
)

IMAP_SERVER = "imap.qiye.aliyun.com"
IMAP_PORT = 993
YXO_DB = os.path.join(YXO_ROOT, "yxo_app", "data", "yxo.db")

# ---------- 运单号文件夹 ----------
WAYBILL_FOLDER = "运单号"
WAYBILL_IMAP_FOLDER = "&j9BTVVP3-"   # 「运单号」的 IMAP modified-UTF7 编码

# ---------- 运行配置（可由系统管理-配置管理页编辑，本期先 JSON） ----------
CFG_PATH = os.path.join(HERE, "waybill_robot_config.json")
_DEFAULT_ACCOUNTS = [
    "maoxiaoyang@cqtransit.com",
    "yangyawen@cqtransit.com",
    "fengqian@cqtransit.com",
    "hanwenhao@cqtransit.com",
]
_DEFAULT_WHITELIST = ["docwbfb@yxologistics.com"]


def load_cfg():
    default = {"live": False, "forward_since": None,
               "accounts": list(_DEFAULT_ACCOUNTS),
               "folder": WAYBILL_FOLDER,
               "sender_whitelist": list(_DEFAULT_WHITELIST)}
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    default.update({k: v for k, v in cfg.items() if v is not None})
    return default


CFG = load_cfg()
LIVE = os.environ.get("WAYBILL_LIVE", "").strip() == "1" or bool(CFG.get("live"))
TEST_MODE = not LIVE
MAILBOXES = CFG.get("accounts") or list(_DEFAULT_ACCOUNTS)
WHITELIST = [s.lower() for s in (CFG.get("sender_whitelist") or _DEFAULT_WHITELIST)]

VERIFY_MAILBOX = "3841559246@qq.com"
ADMIN_MAILBOX = "maoxiaoyang@cqtransit.com"
ADMIN_NAME = "毛骁洋"
MAX_BODY = int(os.environ.get("MAX_BODY", "2000"))
MARK_SEEN = os.environ.get("MARK_SEEN", "1") == "1"
WECOM_NOTIFY = os.environ.get("WECOM_NOTIFY", "1") == "1"

# 企微通知（可选：无 requests / 无 cs_bot 时自动降级为不通知）
_notify_by_name = None
if WECOM_NOTIFY:
    try:
        from cs_bot.wecom_api import notify_by_name as _notify_by_name  # noqa: E402
    except Exception:
        _notify_by_name = None

import draft_pending  # 待确认队列(共享模块)  # noqa: E402

# 退舱跳过（2026-08-03）：状态=退舱的记录不再转发相关邮件
from db_write import is_box_cancelled  # noqa: E402

# BUG-A 热修：复用草单机器人的转发路由（客编/箱号→公司→收件人/抄送→同事邮箱转发），与草单完全一致
try:
    from Draft_Forward_Robot import (  # noqa: E402
        resolve_recipients, load_dsk_routing, build_forward, smtp_send as _draft_smtp_send,
    )
except Exception:
    resolve_recipients = load_dsk_routing = build_forward = _draft_smtp_send = None

LOG_DIR = os.path.join(HERE, "logs")
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

LEDGER_DB = os.path.join(DATA_DIR, "waybill_forward_ledger.db")

# ---------- 判据正则 ----------
CODE_RE = re.compile(r'CQWLJT[0-9A-Za-z\-]+')
CODE_NUM_RE = re.compile(r'CQWLJT(\d+)')
XLS_RE = re.compile(r'\.xls$', re.I)


def log(msg):
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(LOG_DIR, f"waybill_{today}.log")
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


# ---------- 邮件解析工具 ----------
def decode_any(raw):
    if not raw:
        return ""
    try:
        parts = decode_header(raw)   # 返回 [(片段, 编码), ...]；不可解包成两个变量
    except Exception:
        return str(raw)
    out = []
    for txt, enc in parts:
        if isinstance(txt, bytes):
            try:
                out.append(txt.decode(enc or "utf-8", "ignore"))
            except Exception:
                out.append(txt.decode("utf-8", "ignore"))
        else:
            out.append(txt)
    return "".join(out)


def attachment_names(msg):
    names = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            names.append(decode_any(fn))
    return names


def extract_body_text(msg, limit=MAX_BODY):
    text = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            try:
                text = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "ignore")
            except Exception:
                text = str(part.get_payload())
            break
    if not text:
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    text = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "ignore")
                except Exception:
                    text = str(part.get_payload())
                break
    return text[:limit]


def get_attachment_bytes(it, regex):
    raw = it.get("raw")
    if not raw:
        return None
    msg = email.message_from_bytes(raw)
    for part in msg.walk():
        fn = part.get_filename()
        if fn and regex.search(fn):
            try:
                return part.get_payload(decode=True)
            except Exception:
                return None
    return None


# ---------- yxo.db 读取 + 匹配（复用草单机器人逻辑） ----------
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
    """返回 (level, rec)  level: full/num/box/box_multi/none"""
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


# ---------- 扫描（PEEK, 永不标已读） ----------
def scan_mailbox(addr, password, folder):
    items = []
    try:
        m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        m.login(addr, password)
    except Exception as e:
        log(f"❌ {addr} IMAP 登录失败: {e}")
        return items
    res, _ = m.select(folder, readonly=True)
    if res != "OK":
        log(f"⚠ {addr} 无「{folder}」文件夹, 跳过")
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
                # 时区归一（体检报告 3.3）：统一走 common_io.to_local_naive，与草单机器人一致。
                dt = to_local_naive(parsed) if parsed is not None else None
                date_s = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""
            except Exception:
                dt, date_s = None, ""
            items.append({
                "mailbox": addr,
                "message_id": str(msg.get("Message-ID", "")).strip(),
                "subject": decode_any(msg.get("Subject", "")),
                "sender": parseaddr(msg.get("From", ""))[1].lower(),
                "date": date_s, "dt": dt,
                "atts": atts, "body": body, "raw": md[0][1],
            })
        except Exception as e:
            log(f"  ⚠ {addr} 邮件解析异常: {e}")
    m.logout()
    log(f"  {addr}: 扫到 {len(items)} 封（PEEK 只读）")
    return items


def fetch_raw_by_msgid(addr, password, message_id, folder):
    """按 Message-ID 重取原始邮件（落 .eml 用）。"""
    m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    m.login(addr, password)
    m.select(folder, readonly=True)
    res, data = m.search(None, "HEADER", "Message-ID", message_id)
    raw = None
    if res == "OK" and data[0]:
        _, md = m.fetch(data[0].split()[0], "(BODY.PEEK[])")
        raw = md[0][1]
    m.logout()
    return raw


def mark_seen_everywhere(message_id, boxes_seen):
    """已处理的邮件在所有出现过的邮箱标已读（\\Seen）。仅精确标记, 失败只记日志。"""
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
            m.select(WAYBILL_IMAP_FOLDER)
            res, data = m.search(None, "HEADER", "Message-ID", message_id)
            if res == "OK" and data[0]:
                for mid in data[0].split():
                    m.store(mid, "+FLAGS", "\\Seen")
                done.append(addr)
            m.logout()
        except Exception as e:
            log(f"  ⚠ 标已读失败 {addr}: {e}")
    return done


# ---------- 企微通知 ----------
def wecom_notify_person(real_name, text):
    if not (_notify_by_name and real_name):
        return False, ""
    try:
        return _notify_by_name(real_name, text)
    except Exception as e:
        log(f"  ⚠ 企微通知异常 {real_name}: {e}")
        return False, ""


def wecom_summary_to_admin(notified):
    """规则 #118: 仅当本轮有实际处理(notified 非空)才给管理员(毛骁洋)发微信/企微摘要;
    空跑(无处理)绝不发。返回 (ok, channel)。"""
    if not (_notify_by_name and notified):
        return False, ""
    n = len(notified)
    lines = [f"📋 运单号机器人 · 正式汇总 {datetime.now().strftime('%H:%M')}",
             f"本次实际处理 {n} 单"]
    for p in notified[:8]:
        lines.append(f"· [{p['category']}] {p.get('code') or '-'} → {p.get('owner') or '-'}")
    if n > 8:
        lines.append(f"…其余 {n - 8} 单")
    try:
        return _notify_by_name(ADMIN_NAME, "\n".join(lines))
    except Exception as e:
        log(f"  ⚠ 毛骁洋微信/企微汇总异常: {e}")
        return False, ""


# ---------- SMTP ----------
def smtp_send(msg, sender_email, sender_pwd, to_list, cc_list=None):
    if not to_list:
        return False
    try:
        s = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30)
        s.login(sender_email, sender_pwd)
        s.send_message(msg, from_addr=sender_email, to_addrs=to_list)
        s.quit()
        return True
    except Exception as e:
        log(f"  ⚠ SMTP 发送失败: {e}")
        return False


# ---------- .xls 解析（运单号提取） ----------
def _col_index(headers, candidates):
    for cand in candidates:
        for i, h in enumerate(headers):
            if h and cand.lower() in h.lower():
                return i
    return -1


def _cell_str(v):
    if v is None:
        return ""
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return str(v)
    return str(v).strip()


def parse_waybill_xls(raw_bytes):
    """解析运单号 .xls 附件, 返回 [{客户编码, 箱号, 运单号}] 列表。
    优先 xlrd(.xls) / openpyxl(.xlsx); 都不存在则退化为文本提取(仅客户编码)。"""
    rows = []
    # 1) xlrd 处理真正的 .xls (BIFF)
    try:
        import io, xlrd
        book = xlrd.open_workbook(file_contents=raw_bytes)
        sh = book.sheet_by_index(0)
        headers = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
        idx_code = _col_index(headers, ["客户编码", "客户代码", "code"])
        idx_box = _col_index(headers, ["箱号", "箱", "container", "box"])
        idx_rwb = _col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])
        for r in range(1, sh.nrows):
            code = _cell_str(sh.cell_value(r, idx_code)) if idx_code >= 0 else ""
            box = _cell_str(sh.cell_value(r, idx_box)) if idx_box >= 0 else ""
            rwb = _cell_str(sh.cell_value(r, idx_rwb)) if idx_rwb >= 0 else ""
            if not (code or box or rwb):
                continue
            rows.append({"客户编码": code, "箱号": box, "运单号": rwb})
        if rows:
            return rows
    except Exception as e:
        log(f"  ⚠ xlrd 解析失败: {e}")
    # 2) openpyxl 处理 .xlsx
    try:
        import io, openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        ws = wb.active
        data = list(ws.iter_rows(values_only=True))
        if data:
            headers = [str(h or "").strip() for h in data[0]]
            idx_code = _col_index(headers, ["客户编码", "客户代码", "code"])
            idx_box = _col_index(headers, ["箱号", "箱", "container", "box"])
            idx_rwb = _col_index(headers, ["rwb", "rwb no", "运单号", "运单", "waybill"])
            for r in data[1:]:
                code = str(r[idx_code]).strip() if idx_code >= 0 and idx_code < len(r) and r[idx_code] is not None else ""
                box = str(r[idx_box]).strip() if idx_box >= 0 and idx_box < len(r) and r[idx_box] is not None else ""
                rwb = str(r[idx_rwb]).strip() if idx_rwb >= 0 and idx_rwb < len(r) and r[idx_rwb] is not None else ""
                if not (code or box or rwb):
                    continue
                rows.append({"客户编码": code, "箱号": box, "运单号": rwb})
            return rows
    except Exception as e:
        log(f"  ⚠ openpyxl 解析失败: {e}")
    # 3) 文本退化：从字节里抽可打印 ASCII 行, 找 CQWLJT... 客户编码
    try:
        text = raw_bytes.decode("latin-1", "ignore")
        for line in text.splitlines():
            line = line.strip()
            mcode = CODE_RE.search(line)
            if mcode:
                rows.append({"客户编码": mcode.group(0).split("-")[0], "箱号": "", "运单号": ""})
        if rows:
            log("  ⚠ 无 xls 解析库, 使用文本退化提取(仅客户编码)")
    except Exception:
        pass
    return rows


def classify(subject, sender, atts, body):
    """返回 'A' / 'B' / None。仅认白名单发件人, 零关键词兜底。"""
    if sender not in WHITELIST:
        return None
    if "单证审核驳回" in (subject or ""):
        return "B"
    if any(XLS_RE.search(a) for a in (atts or [])):
        return "A"
    return None


def extract_code_from_body(body):
    m = CODE_RE.search(body or "")
    if m:
        return m.group(0).split("-")[0]
    return ""


# ---------- 通知文案 ----------
def build_waybill_notify(p, test_mode):
    if p["category"] == "WAY_B":
        head = "🚨 单证审核驳回告警" + ("【测试】" if test_mode else "")
        lines = [head,
                 f"客户编码: {p.get('code') or '-'}",
                 "该客户单证审核被拒, 请尽快重报。",
                 "回复「确认」已知悉, 回复「跳过」忽略。"]
    else:
        head = "⚠️ 运单号待确认（A类·运单号下发）" + ("【测试】" if test_mode else "")
        lines = [head,
                 f"班列/客户: {p.get('code') or '-'}  箱数: {p.get('box_count') or '?'}"]
        if p.get("rows"):
            lines.append("─" * 12)
            for r in p["rows"][:8]:
                lines.append(f"箱号 {r.get('箱号') or '-'} → 运单号 {r.get('运单号') or '-'}")
        lines.append("回复「确认」标记已处理, 回复「跳过」忽略。")
    if test_mode:
        lines.append("—— 本条为测试预览, 无需操作 ——")
    return "\n".join(lines)


# ---------- 台账 ----------
def ledger_conn():
    conn = sqlite3.connect(LEDGER_DB, timeout=15)
    conn.execute("""CREATE TABLE IF NOT EXISTS processed_mails(
        message_id TEXT PRIMARY KEY, subject TEXT, sender TEXT, date TEXT,
        category TEXT, code TEXT, owner TEXT, action TEXT, ts INTEGER)""")
    conn.commit()
    return conn


def ledger_seen_ids(conn):
    try:
        return {r[0] for r in conn.execute("SELECT message_id FROM processed_mails")}
    except Exception:
        return set()


# ---------- 主流程 ----------
def run():
    log("=" * 60)
    log(f"运单号机器人启动 · {'TEST(测试)' if TEST_MODE else 'LIVE(正式)'} · {datetime.now()}")
    records = load_records()
    log(f"yxo.db 记录 {len(records)} 条")

    # forward_since 护栏（仅 LIVE）
    forward_since = None
    fs = CFG.get("forward_since")
    if fs:
        try:
            forward_since = datetime.fromisoformat(fs)
        except Exception:
            forward_since = None
    if LIVE and not forward_since:
        forward_since = datetime.now()
        try:
            CFG["forward_since"] = forward_since.isoformat()
            atomic_write_json(CFG_PATH, CFG)
            log(f"  ℹ LIVE 首次启用, forward_since 自动设为 {forward_since}")
        except Exception:
            pass

    # 1) 扫描 4 邮箱 运单号 文件夹
    all_items = []
    for addr in MAILBOXES:
        pwd = ACCOUNTS.get(addr)
        if not pwd:
            log(f"  ⚠ 无 {addr} 密码, 跳过")
            continue
        all_items.extend(scan_mailbox(addr, pwd, WAYBILL_IMAP_FOLDER))

    # 2) 跨邮箱按 Message-ID 去重
    seen_ids = set()
    items = []
    for it in all_items:
        mid = it["message_id"]
        if not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)
        items.append(it)
    log(f"去重后 {len(items)} 封")

    # 3) ledger 去重 + forward_since 护栏 + 分类
    conn = ledger_conn()
    processed = ledger_seen_ids(conn)
    to_process = []
    skipped_old = 0
    for it in items:
        if it["message_id"] in processed:
            continue
        if forward_since and it["dt"] and it["dt"] < forward_since:
            skipped_old += 1
            continue
        cat = classify(it["subject"], it["sender"], it["atts"], it["body"])
        if not cat:
            continue
        to_process.append((it, cat))
    log(f"待处理 {len(to_process)} 封 (跳过历史/已处理/非运单号 {skipped_old})")

    # 4) 匹配负责同事
    plan = []
    for it, cat in to_process:
        subject = it["subject"]
        if cat == "A":
            xls_bytes = get_attachment_bytes(it, XLS_RE)
            rows = parse_waybill_xls(xls_bytes) if xls_bytes else []
            code = (rows[0]["客户编码"] if rows else "")
            box = (rows[0]["箱号"] if rows else "")
            num = CODE_NUM_RE.match(code).group(1) if (code and CODE_NUM_RE.match(code)) else ""
            level, rec = match_record(records, code, num, box)
            company = rec["company"] if rec else ""
            # ── 退舱跳过（2026-08-03）：状态=退舱的记录不再转发相关邮件 ──
            if (rec is not None and rec.get("status") == "退舱") or (box and is_box_cancelled(box)):
                log(f"  ⏭ 退舱跳过(WAY_A): {subject[:40]} (箱号 {box})")
                continue
            owner = company_to_name(company) if company else None
            if not owner:
                owner = ADMIN_NAME
                level = level or "none"
            plan.append({"category": "WAY_A", "code": code, "box": box,
                         "owner": owner, "company": company, "level": level,
                         "subject": subject, "message_id": it["message_id"],
                         "rows": rows, "box_count": len(rows),
                         "mailbox": it["mailbox"], "atts": it["atts"],
                         "date": it["date"], "boxes_seen": [it["mailbox"]],
                         "raw": it["raw"]})
        else:  # B 单证审核驳回
            code = extract_code_from_body(it["body"])
            num = CODE_NUM_RE.match(code).group(1) if (code and CODE_NUM_RE.match(code)) else ""
            level, rec = match_record(records, code, num, "")
            company = rec["company"] if rec else ""
            # ── 退舱跳过（2026-08-03）：状态=退舱的记录不再转发相关邮件 ──
            if (rec is not None and rec.get("status") == "退舱") or (box and is_box_cancelled(box)):
                log(f"  ⏭ 退舱跳过(WAY_B): {subject[:40]} (客编 {code})")
                continue
            owner = company_to_name(company) if company else None
            if not owner:
                owner = ADMIN_NAME
            plan.append({"category": "WAY_B", "code": code, "box": "",
                         "owner": owner, "company": company, "level": level or "none",
                         "subject": subject, "message_id": it["message_id"],
                         "rows": [], "box_count": 0,
                         "mailbox": it["mailbox"], "atts": it["atts"],
                         "date": it["date"], "boxes_seen": [it["mailbox"]],
                         "raw": it["raw"]})

    # 5) LIVE: 入队 pending + 企微通知负责同事; TEST: 只发验证邮箱汇总
    notified = []
    if LIVE:
        # BUG-A 热修：加载草单转发路由（公司/箱号→收件人/抄送），用于高置信自动转发
        _df_default_map, _df_box_map = (load_dsk_routing() if load_dsk_routing else ({}, {}))
        for p in plan:
            p["notify_ok"] = False
            p["notify_channel"] = ""
            p["pending_no"] = None
            notified.append(p)
            if p["category"] == "WAY_A":
                # ── BUG-A 热修：高置信命中(full/num 且路由可解析) → 自动转发, 不进队列 ──
                _auto = False
                if (p.get("level") in ("full", "num") and resolve_recipients and _draft_smtp_send):
                    _to, _cc, _src = resolve_recipients(
                        p.get("box"), p.get("company"), _df_default_map, _df_box_map)
                    if _to:
                        try:
                            _fwd, _subj = build_forward(p["raw"], "WAY_A")
                            _fwd["Subject"] = _subj
                            _sen = sender_for_company(p["company"]) if p["company"] else None
                            _ae, _ap = (_sen if (_sen and _sen[1]) else (ADMIN_MAILBOX, ACCOUNTS.get(ADMIN_MAILBOX)))
                            _draft_smtp_send(_fwd, _ae, _ap, _to, _cc)
                            conn.execute(
                                "INSERT OR REPLACE INTO processed_mails VALUES (?,?,?,?,?,?,?,?,?)",
                                (p["message_id"], p["subject"], "docwbfb@yxologistics.com",
                                 p["date"], p["category"], p["code"], p["owner"], "auto_forwarded",
                                 int(datetime.now().timestamp())))
                            try:
                                import forward_log
                                forward_log.record(
                                    "运单号", owner=p.get("owner") or "", company=p.get("company") or "",
                                    code=p.get("code") or "", box=p.get("box") or "",
                                    subject=p.get("subject") or "", to_list=_to, sender=_ae,
                                    note=f"{p['category']} 自动转发(级别={p['level']})", test=False)
                            except Exception:
                                pass
                            # 企微告知「已自动转发」(不含确认/跳过按钮)
                            wecom_notify_person(p["owner"],
                                f"✅ 运单号已自动转发（匹配级别={p['level']}，无需确认）\n"
                                f"主题: {p['subject'][:60]}\n"
                                f"客编: {p.get('code') or '-'} 箱号: {p.get('box') or '-'}\n"
                                f"已发往: {', '.join(_to)}")
                            mark_seen_everywhere(p["message_id"], p["boxes_seen"])
                            log(f"  🚀 [WAY_A] {p['code'] or '-'} → {p['owner']} 自动转发(级别={p['level']}) 收件人={_to}")
                            _auto = True
                        except Exception as _e:
                            log(f"  ⚠ [WAY_A] 自动转发失败({_e}), 回退到待确认队列")
                if _auto:
                    continue
                # 低/无置信 或 路由缺失 → 保持原行为: 进待确认队列, 等企微「确认/跳过」
                info = {
                    "message_id": p["message_id"], "subject": p["subject"],
                    "sender": "docwbfb@yxologistics.com", "date": p["date"],
                    "category": p["category"], "code": p["code"], "num": "",
                    "box": p["box"], "company": p["company"], "owner": p["owner"],
                    "reason": "运单号下发, 待确认转发",
                    "candidates": [], "boxes_seen": p["boxes_seen"], "to": [], "cc": [],
                }
                n = draft_pending.add_pending(info, p["raw"], test=False)
                p["pending_no"] = n
                # 低/无置信: 发「待确认」通知, 等企微「确认/跳过」
                text = build_waybill_notify(p, False)
                ok, ch = wecom_notify_person(p["owner"], text)
                p["notify_ok"] = ok
                p["notify_channel"] = ch
                conn.execute(
                    "INSERT OR REPLACE INTO processed_mails VALUES (?,?,?,?,?,?,?,?,?)",
                    (p["message_id"], p["subject"], "docwbfb@yxologistics.com",
                     p["date"], p["category"], p["code"], p["owner"], "pending",
                     int(datetime.now().timestamp())))
                # 统一转发日志（供每日 8 点汇总邮件用；失败不影响主流程）
                try:
                    import forward_log
                    forward_log.record(
                        "运单号", owner=p.get("owner") or "", company=p.get("company") or "",
                        code=p.get("code") or "", box=p.get("box") or "",
                        subject=p.get("subject") or "", to_list=[], sender="",
                        note=f"{p['category']} 待确认#{n}", test=False)
                except Exception:
                    pass
                log(f"  ✅ [WAY_A] {p['code'] or '-'} → {p['owner']} 待确认#{n} 通知{'✅' if ok else '❌'}({ch or '-'})")
            else:
                # B 类(单证审核驳回): 仅通知负责同事, 不进待确认队列(不污染待办)
                text = build_waybill_notify(p, False)
                ok, ch = wecom_notify_person(p["owner"], text)
                p["notify_ok"] = ok
                p["notify_channel"] = ch
                conn.execute(
                    "INSERT OR REPLACE INTO processed_mails VALUES (?,?,?,?,?,?,?,?,?)",
                    (p["message_id"], p["subject"], "docwbfb@yxologistics.com",
                     p["date"], p["category"], p["code"], p["owner"], "notified",
                     int(datetime.now().timestamp())))
                log(f"  🔔 [WAY_B] {p['code'] or '-'} → {p['owner']} 仅通知(不进待办) {'✅' if ok else '❌'}({ch or '-'})")
            mark_seen_everywhere(p["message_id"], p["boxes_seen"])
    else:
        for p in plan:
            log(f"  🔍 [TEST] [{p['category']}] {p['code'] or '-'} → 匹配负责:{p['owner']} (level={p['level']}) 箱数={p.get('box_count')}")

    # 关键修复：processed_mails 去重台账必须在关闭连接前提交，
    # 否则未提交的事务在 conn.close() 时被回滚 → 下次运行重复转发（每 15 分钟骚扰一次）。
    try:
        conn.commit()
    except Exception:
        pass
    conn.close()

    # 6) 汇总（规则 #118: 仅当有实际处理才通知毛骁洋）
    _send_summary_email(plan, notified)
    log(f"完成: 待处理 {len(plan)} 单 | 实际通知负责同事 {len(notified)} 单")
    log("=" * 60)
    return len(plan)


def _send_summary_email(plan, notified):
    try:
        lines = [f"运单号转发机器人 · {'测试' if TEST_MODE else '正式'}运行汇总",
                 f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                 f"扫描去重后待处理 {len(plan)} 单",
                 ""]
        for p in plan:
            lines.append(f"[{p['category']}] {p['subject'][:60]}")
            lines.append(f"    客编:{p.get('code') or '-'} 箱号:{p.get('box') or '-'} 负责:{p['owner']} 匹配:{p['level']}")
            if p["category"] == "WAY_A" and p.get("rows"):
                for r in p["rows"][:5]:
                    lines.append(f"      箱 {r.get('箱号') or '-'} → 运单 {r.get('运单号') or '-'}")
        summary = MIMEMultipart()
        summary.attach(MIMEText("\n".join(lines), "plain", "utf-8"))
        summary["Subject"] = f"[{'测试' if TEST_MODE else '正式'}] 运单号机器人运行汇总 {datetime.now().strftime('%H:%M')}"
        admin_pwd = ACCOUNTS.get(ADMIN_MAILBOX)
        _has_action = bool(notified)
        if TEST_MODE:
            # 测试期: 汇总发验证邮箱(原行为), 不骚扰毛骁洋
            smtp_send(summary, ADMIN_MAILBOX, admin_pwd, [VERIFY_MAILBOX])
            log("  📧 测试汇总已发验证邮箱(不通知毛骁洋)")
        elif _has_action:
            # 正式期且有实际处理: 邮件发毛骁洋 + 微信/企微摘要
            smtp_send(summary, ADMIN_MAILBOX, admin_pwd, [ADMIN_MAILBOX])
            log("  📧 正式汇总已发毛骁洋邮箱")
            ok, ch = wecom_summary_to_admin(notified)
            log(f"  📲 毛骁洋微信/企微汇总: {'✅' if ok else '❌'} 通道={ch or '-'}")
        else:
            log("  ⏭ LIVE 空跑(无实际处理), 跳过毛骁洋所有通知")
    except Exception as e:
        log(f"  ⚠ 汇总邮件发送失败: {e}")


if __name__ == "__main__":
    import traceback
    try:
        daemon_loop("waybill", run)
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        sys.exit(1)
