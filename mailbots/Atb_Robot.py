#!/usr/bin/env python
"""
ATB 邮件自动转发机器人 (v3 - 飞书弃用版)
基于 Dsk_Robot v3 架构，适配 ATB 邮件规则。新增：本地缓存优先加载 + 本地 SQLite 去重 + 本地路由配置
（彻底移除飞书依赖：lark_oapi、FeishuBitable、TABLE_MAIN/TABLE_DSK_CONFIG/TABLE_DSK_LOG 均不再使用）

与 DSK 的关键差异：
1. 文件夹: ATB (非 dsk)
2. 发件人过滤: atb@yxologistics.com (非 kasa@rtsb.de)
3. 按原内容转发：标题已含客编箱号，不做修改
4. 箱号提取：HTML表格 → 主题正则回退（ATB Type A 无表格）
5. 无多箱号拆分：每封 ATB 邮件均为单箱号
"""

import imaplib
import smtplib
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
import re
import os
import sys
import json
import time
import functools
from datetime import datetime, timedelta

sys.path.insert(0, r'D:\YXO_DATA\WeComBot')
from common_io import atomic_write_json, daemon_loop

from bs4 import BeautifulSoup

from config import (
    SMTP_SERVER, SMTP_PORT,
    ACCOUNTS,
    company_to_name,
    sender_for_company,
)
from cs_bot import wecom_api

# 本地去重指纹库（2026-07-31 芙蕾雅 P1：替代每次全量拉取 TABLE_DSK_LOG）
import dedup_store

# 退舱跳过（2026-08-03）：状态=退舱的记录不再转发相关邮件
from db_write import is_box_cancelled, get_conn

# ==================== ATB 常量 ====================
ATB_IMAP_SERVER = "imap.qiye.aliyun.com"
ATB_IMAP_PORT = 993
ATB_FOLDER = "ATB"
ATB_SENDER_FILTER = "atb@yxologistics.com"

# 去重命名空间：与 DSK 共用（改造前两者都读整张 TABLE_DSK_LOG，保持等价）
DEDUP_SOURCE = "dsk_log"
DEDUP_RETENTION_DAYS = 90

# 容器编号正则
CONTAINER_RE = re.compile(r'^[A-Z]{4}\d{7}$')

# 公司名映射
COMPANY_ALIAS = {
    "公运沙坪坝": "沙坪坝",
    "重轮太平洋": "太平洋",
}

# ===== 本地缓存 =====
CACHE_DIR = r"D:\YXO_DATA\WeComBot\cache"
CACHE_FILE = os.path.join(CACHE_DIR, "dsk_config_cache.json")
CACHE_TTL_MINUTES = 60

# 本地去重文件：记录已成功转发的邮件 Message-ID。
# 即使飞书日志表因限流读取失败，也能防止重复转发。
DEDUP_FILE = os.path.join(CACHE_DIR, "atb_forwarded.json")

BOT_LOG_DIR = r"D:\YXO_DATA\WeComBot\logs"
os.makedirs(BOT_LOG_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

TEST_MODE = False
FORCE_ALL = False
TEST_TO = "3841559246@qq.com"


# ==================== 工具函数 ====================

def split_emails(email_str):
    if not email_str or str(email_str).strip().lower() == "none":
        return []
    raw_list = re.split(r'[;,，\s]+', str(email_str))
    return [e.strip() for e in raw_list if "@" in e]


def log(msg):
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(BOT_LOG_DIR, f"atb_{today}.log")
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def decode_subject(msg):
    raw = msg['Subject']
    if not raw:
        return "(无主题)"
    parts = decode_header(raw)
    result = ""
    for part, charset in parts:
        if isinstance(part, bytes):
            result += part.decode(charset or 'utf-8', errors='replace')
        else:
            result += str(part)
    return result


# ==================== 本地缓存 ====================

def load_dsk_config_from_cache():
    """从本地缓存加载 DSK 配置数据。返回 dict 或 None"""
    if not os.path.exists(CACHE_FILE):
        return None

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)

        updated_at = cache.get("updated_at", "")
        ttl = cache.get("ttl_minutes", CACHE_TTL_MINUTES)

        try:
            cache_time = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
            age = (datetime.now() - cache_time).total_seconds() / 60
            if age > ttl:
                log(f"  ⏰ 本地缓存已过期（{age:.1f}分钟 > {ttl}分钟TTL），回退 yxo.db 直读")
                return None
        except (ValueError, Exception):
            log(f"  ⚠ 缓存时间格式异常，回退 yxo.db 直读")
            return None

        log(f"  ✅ 命中本地缓存（{updated_at}，已存{age:.1f}分钟）")
        return cache

    except Exception as e:
        log(f"  ⚠ 读取本地缓存失败: {e}，回退 yxo.db 直读")
        return None


def save_dsk_config_cache(box_company, box_customer_code, default_map, box_record_map, box_config_ids, box_record_id=None):
    """将 DSK 配置数据写入本地缓存（含 box_record_id，避免回写时间戳时反复全表扫描）"""
    cache = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "box_company": box_company,
        "box_customer_code": box_customer_code,
        "default_map": default_map,
        "box_record_map": box_record_map,
        "box_config_ids": box_config_ids,
        "box_record_id": box_record_id or {},
        "ttl_minutes": CACHE_TTL_MINUTES
    }
    try:
        atomic_write_json(CACHE_FILE, cache)
        log(f"  📦 本地缓存已更新")
    except Exception as e:
        log(f"  ⚠ 写本地缓存失败: {e}")


def remove_box_from_cache(box_no):
    """从本地缓存中删除指定箱号记录"""
    try:
        if not os.path.exists(CACHE_FILE):
            return
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)

        removed = False
        if box_no in cache.get("box_record_map", {}):
            del cache["box_record_map"][box_no]
            removed = True
        if box_no in cache.get("box_config_ids", {}):
            del cache["box_config_ids"][box_no]
            removed = True

        if removed:
            cache["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            atomic_write_json(CACHE_FILE, cache)
            log(f"        🗑 已从本地缓存删除箱号 [{box_no}]")
    except Exception as e:
        log(f"        ⚠ 更新本地缓存失败: {e}")


def load_local_dedup():
    """读取旧版 JSON 去重集合（atb_forwarded.json）。

    2026-07-31 起去重已迁移到 dedup_store（SQLite），此函数仅在**首次播种**时
    调用一次，把历史指纹并入新库，之后不再使用。旧文件保留不删，作为回滚依据。
    """
    if not os.path.exists(DEDUP_FILE):
        return set()
    try:
        with open(DEDUP_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


# ==================== 邮件解析 ====================

def parse_email_structure(msg):
    html_body = None
    plain_body = None
    attachments = []
    inline_parts = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = str(part.get('Content-Disposition', '')).lower()
        filename = part.get_filename() or ''

        if 'attachment' in disposition:
            attachments.append((part, filename))
        elif 'inline' in disposition:
            inline_parts.append(part)
        elif content_type == 'text/html':
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or 'utf-8'
                try:
                    html_body = payload.decode(charset, errors='replace')
                except Exception:
                    html_body = payload.decode('utf-8', errors='replace')
        elif content_type == 'text/plain':
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or 'utf-8'
                try:
                    plain_body = payload.decode(charset, errors='replace')
                except Exception:
                    plain_body = payload.decode('utf-8', errors='replace')

    return html_body, plain_body, attachments, inline_parts


def extract_boxes(html_body, subject):
    """
    提取箱号：HTML表格 → 主题正则回退。
    返回: list of (box_no, extra_info)
    """
    boxes = []

    # 1. HTML 表格提取
    if html_body:
        soup = BeautifulSoup(html_body, 'html.parser')
        for table in soup.find_all('table'):
            for row in table.find_all('tr'):
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 2:
                    c1 = cells[0].get_text(strip=True)
                    if CONTAINER_RE.match(c1):
                        c2 = cells[1].get_text(strip=True)
                        boxes.append((c1, c2))

    # 2. 主题回退
    if not boxes and subject:
        found = re.findall(r'\b[A-Z]{4}\d{7}\b', subject)
        for f in found:
            if not any(b[0] == f for b in boxes):
                boxes.append((f, ""))

    # 去重保持顺序
    seen = set()
    result = []
    for b in boxes:
        if b[0] not in seen:
            seen.add(b[0])
            result.append(b)
    return result


def build_forward_email(original_msg, html_body, plain_body,
                        attachments, inline_parts):
    """构建原样转发邮件（不修改 HTML 表格，不过滤附件）。"""
    msg = MIMEMultipart('mixed')

    # 标题原样保留
    original_subject = decode_subject(original_msg)
    msg['Subject'] = original_subject

    # 正文原样
    if html_body:
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    elif plain_body:
        msg.attach(MIMEText(plain_body, 'plain', 'utf-8'))

    # 附件全部保留
    for part, filename in attachments:
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        new_part = MIMEBase(part.get_content_maintype(), part.get_content_subtype())
        new_part.set_payload(payload)
        encoders.encode_base64(new_part)
        new_part.add_header('Content-Disposition', 'attachment',
                            filename=('utf-8', '', filename))
        msg.attach(new_part)

    for part in inline_parts:
        msg.attach(part)

    return msg


# ==================== SMTP 转发 ====================

def forward_atb_email(original_msg, box_no, company,
                      html_body, plain_body,
                      attachments, inline_parts,
                      to_list, cc_list, sender_email, sender_password):
    """转发单个箱号的 ATB 邮件。"""
    msg = build_forward_email(
        original_msg, html_body, plain_body,
        attachments, inline_parts
    )

    msg['From'] = sender_email
    msg['To'] = ", ".join(to_list)
    if cc_list:
        msg['Cc'] = ", ".join(cc_list)

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(sender_email, sender_password)
        all_recipients = list(set(to_list + cc_list))
        server.sendmail(sender_email, all_recipients, msg.as_string())


# ==================== 路由解析 ====================

def resolve_recipients(box_no, company, default_map, box_record_map):
    """
    三级回退：
    1. 箱号精确配置
    2. 公司 DEFAULT 回退
    3. 无路由配置
    """
    to_list = []
    cc_list = []
    source = ""

    # 1. 箱号记录
    box_info = box_record_map.get(box_no)
    if box_info and box_info['to']:
        to_list = box_info['to']
        cc_list = box_info['cc']
        source = "箱号精确配置"
        return to_list, cc_list, source

    # 2. DEFAULT 回退
    if company and company in default_map:
        def_info = default_map[company]
        if def_info['to']:
            to_list = def_info['to']
            cc_list = def_info['cc']
            source = "DEFAULT回退"
            return to_list, cc_list, source

    # 3. 无路由配置
    return [], [], ""


def load_atb_routing():
    """P4③（2026-08-06 小叽）：从本地 yxo.db bot_config 读取 ATB 路由，替代飞书 TABLE_DSK_CONFIG。
    返回 (default_map, box_record_map)：
      default_map:    {公司: {'to':[...], 'cc':[...]}}  来自 scope='company'
      box_record_map: {}（逐箱路由已废弃，P4③ 只迁 DEFAULT）
    """
    import json as _json
    try:
        conn = get_conn()
    except Exception as e:
        log(f"  ⚠ 读取本地 ATB 路由失败: {e}")
        return {}, {}
    cur = conn.cursor()
    default_map = {}
    for key, to_a, cc_a in cur.execute(
        "SELECT key, to_addrs, cc_addrs FROM bot_config WHERE bot='atb' AND scope='company'"):
        default_map[key] = {
            'to': _json.loads(to_a or '[]'),
            'cc': _json.loads(cc_a or '[]'),
        }
    conn.close()
    return default_map, {}


# ==================== 主流程 ====================

def run_atb_robot():
    # ── 0. 开关闸门（2026-08-03 芙蕾雅：live=false 则本轮回退，不扫描/不转发/不写库）──
    try:
        from bot_config import load_bot_config
        _cfg = load_bot_config("atb")
        if not _cfg["live"]:
            log("⏸ ATB 机器人已停用(live=false)，本轮回退（不扫描/不转发/不写库）。")
            return 0
    except Exception as _e:
        log(f"  ⚠ 读取开关配置失败(按运行处理): {_e}")
    _MAIN_LOOKUP.clear()
    log("=" * 60)
    log(f"ATB 邮件自动转发机器人 启动 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    log("=" * 60)

    # ── A. 加载配置数据（两级：本地缓存 → yxo.db 直读）──

    cache = load_dsk_config_from_cache()
    loaded_from_cache = cache is not None

    if loaded_from_cache:
        box_company = cache["box_company"]
        box_customer_code = cache["box_customer_code"]
        default_map = cache["default_map"]
        box_record_map = cache["box_record_map"]
        box_config_ids = cache.get("box_config_ids", {})
        box_record_id = cache.get("box_record_id", {})
        log(f"  箱号映射 {len(box_company)} 个, "
            f"客户编码 {len(box_customer_code)} 个, "
            f"DEFAULT {len(default_map)} 个公司, "
            f"箱号记录 {len(box_record_map)} 条")
    else:
        # 回退 yxo.db 直读
        log("正在从 yxo.db 读取订舱记录 (records 表) ...")
        try:
            conn = get_conn()
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT 箱号, 开票子公司名称, 客户编码 FROM records "
                "WHERE 箱号 IS NOT NULL AND 箱号<>'' AND 箱号 NOT LIKE 'none%'"
            ).fetchall()
            conn.close()
        except Exception as e:
            log(f"  ❌ 读取 yxo.db records 失败: {e}")
            box_company = {}
            box_customer_code = {}
            box_record_id = {}
        else:
            box_company = {}
            box_customer_code = {}
            box_record_id = {}
            for r in rows:
                box = str(r[0] or '').strip()
                comp = str(r[1] or '').strip()
                comp = COMPANY_ALIAS.get(comp, comp)
                code = str(r[2] or '').strip()
                if box and comp and box.lower() != 'none' and comp.lower() != 'none':
                    box_company[box] = comp
                if box and code and code.lower() != 'none':
                    box_customer_code[box] = code
                # 无飞书 record_id，留空
            log(f"  yxo.db records: 有效箱号映射 {len(box_company)} 个, "
                f"客户编码 {len(box_customer_code)} 个")

        # P4③（2026-08-06 小叽）：DSK/ATB 路由配置改读本地 yxo.db bot_config，
        # 不再读飞书 TABLE_DSK_CONFIG（只迁逐公司 DEFAULT，逐箱路由已废弃）。
        default_map, box_record_map = load_atb_routing()
        box_config_ids = {}
        log(f"  📋 本地路由: DEFAULT {len(default_map)} 个公司（逐箱路由已停用）")

        # yxo.db 直读后写本地缓存
        save_dsk_config_cache(box_company, box_customer_code, default_map, box_record_map, box_config_ids, {})

    # ── C. 去重指纹：本地 SQLite 点查（2026-07-31 芙蕾雅 P1 改造）──
    # 改造前：每次运行都 fs.get_all_records(TABLE_DSK_LOG) 拉整张飞书日志表，
    #         且每转发一封就把整个集合重写一遍 atb_forwarded.json，双重低效。
    # 改造后：首次播种（本地历史）后走本地主键点查 O(1)；90 天自动清理。
    if not TEST_MODE:
        # 本地播种（无飞书依赖）
        if dedup_store.is_seeded(DEDUP_SOURCE):
            log(f"  本地去重指纹库: {dedup_store.count(DEDUP_SOURCE)} 条（点查, 不扫飞书）")
        else:
            log("  🌱 首次运行：从本地数据播种去重指纹（无飞书依赖）...")
            # 这里可以从 yxo.db 或其他本地来源播种，暂时跳过
            dedup_store.set_seeded(DEDUP_SOURCE)
            log(f"  ✅ 播种标记已置位")
        try:
            purged = dedup_store.purge(DEDUP_SOURCE, DEDUP_RETENTION_DAYS)
            if purged:
                log(f"  🧹 清理 {DEDUP_RETENTION_DAYS} 天前去重指纹 {purged} 条")
        except Exception as e:
            log(f"  ⚠ 去重指纹清理失败: {e}")
        log(f"  本地去重指纹库: {dedup_store.count(DEDUP_SOURCE)} 条（点查, 不扫飞书）")
    else:
        log("测试模式：跳过 Message-ID 去重")

    total_forwarded = 0
    total_skipped = 0

    # ── D. 遍历所有账户 ──
    for email_addr, password in ACCOUNTS.items():
        log("-" * 40)
        log(f"扫描账户: {email_addr}")

        try:
            mail_conn = imaplib.IMAP4_SSL(ATB_IMAP_SERVER, ATB_IMAP_PORT, timeout=60)
            mail_conn.login(email_addr, password)
        except Exception as e:
            log(f"  ❌ IMAP 登录失败: {e}")
            continue

        res, _ = mail_conn.select(ATB_FOLDER)
        if res != 'OK':
            log(f"  ⚠ 无法选中文件夹 [{ATB_FOLDER}]，跳过此账户")
            mail_conn.logout()
            continue

        if TEST_MODE:
            res, data = mail_conn.search(None, '(SINCE "01-Jul-2026")')
        else:
            # 与 DSK 一致：仅处理未读邮件（本地去重已防止重复转发）。
            search_crit = 'ALL' if (FORCE_ALL or TEST_MODE) else 'UNSEEN'
            res, data = mail_conn.search(None, search_crit)
        if res != 'OK':
            log("  ⚠ 搜索邮件失败")
            mail_conn.logout()
            continue

        mail_ids = data[0].split()
        label = "7月以来" if TEST_MODE else "未读"
        log(f"  {label}邮件: {len(mail_ids)} 封")

        for m_id in mail_ids:
            try:
                _, msg_data = mail_conn.fetch(m_id, '(RFC822)')
                msg = email.message_from_bytes(msg_data[0][1])

                # --- 发件人过滤 ---
                sender = email.utils.parseaddr(msg['From'])[1]
                if sender.lower() != ATB_SENDER_FILTER.lower():
                    if not TEST_MODE:
                        mail_conn.store(m_id, '+FLAGS', '\\Seen')
                    total_skipped += 1
                    continue

                # --- 去重检查（本地指纹库点查，O(1)）---
                msg_id = str(msg['Message-ID']).strip()
                if not TEST_MODE and dedup_store.is_processed(DEDUP_SOURCE, msg_id):
                    mail_conn.store(m_id, '+FLAGS', '\\Seen')
                    total_skipped += 1
                    continue

                # --- 解析邮件 ---
                subject = decode_subject(msg)
                html_body, plain_body, attachments, inline_parts = parse_email_structure(msg)

                # --- 提取箱号 ---
                boxes = extract_boxes(html_body, subject)
                if not boxes:
                    log(f"  ⏭ [{subject}] — 正文和主题中均未找到箱号")
                    if not TEST_MODE:
                        mail_conn.store(m_id, '+FLAGS', '\\Seen')
                    total_skipped += 1
                    continue

                box_list = [b[0] for b in boxes]
                log(f"  📧 [{subject}] 箱号: {box_list}")

                # --- 按箱号转发 ---
                skip_email = False
                for box_no, extra_info in boxes:
                    company = box_company.get(box_no)
                    customer_code = box_customer_code.get(box_no, "")

                    # ── 退舱跳过（2026-08-03）：状态=退舱的箱号不再转发相关邮件 ──
                    if is_box_cancelled(box_no):
                        log(f"     ⏭ [{box_no}] 状态=退舱，跳过转发（不发送 ATB 邮件）")
                        total_skipped += 1
                        continue

                    if TEST_MODE:
                        to_list = split_emails(TEST_TO)
                        cc_list = []
                        src = "测试模式"
                    else:
                        to_list, cc_list, src = resolve_recipients(
                            box_no, company, default_map, box_record_map
                        )

                    if not to_list:
                        log(f"  ⏭ [{box_no}] — 公司 '{company or '未知'}' 无路由配置(DEFAULT行), 跳过该邮件")
                        total_skipped += 1
                        skip_email = True
                        break

                    if not company:
                        src = "yxo.db无此箱号"

                    # 发件人：按箱号所属公司的负责同事发信（收件人看到的是同事邮箱）
                    sen = sender_for_company(company)
                    s_email, s_pwd = (sen if (sen and sen[1]) else (email_addr, password))

                    try:
                        forward_atb_email(
                            msg, box_no, company,
                            html_body, plain_body,
                            attachments, inline_parts,
                            to_list, cc_list, s_email, s_pwd
                        )
                        log(f"     ✅ [{box_no}] → 公司:{company or '未知'} "
                            f"({src}) To:{to_list} | 由:{s_email}")
                        total_forwarded += 1

                        # 统一转发日志（供每日 8 点汇总邮件用；失败不影响转发）
                        try:
                            import forward_log
                            forward_log.record(
                                "ATB", owner=company_to_name(company) or "",
                                company=company or "", code="",
                                box=box_no, subject=subject,
                                to_list=to_list, sender=s_email, test=TEST_MODE)
                        except Exception:
                            pass

                        # 通知负责人（微信客服通道，微信可收；失败回退企微应用消息）
                        try:
                            owner = company_to_name(company)
                            if owner and not TEST_MODE:
                                notice = (
                                    f"📧 ATB 邮件已转发\n"
                                    f"箱号：{box_no}\n"
                                    f"公司：{company or '未知'}\n"
                                    f"收件人：{','.join(to_list)}\n"
                                    f"发件：{s_email}"
                                )
                                ok, ch = wecom_api.notify_by_name(owner, notice)
                                log(f"     📲 通知 {owner}: {'成功(' + ch + ')' if ok else '失败'}")
                        except Exception as e:
                            log(f"     ⚠ 通知负责人失败: {e}")

                        # 回写时间戳到 yxo.db（不再写飞书总表）
                        if not TEST_MODE:
                            _write_stamp(box_no, "ATB")

                        # 本地审计：更新缓存
                        if not TEST_MODE:
                            remove_box_from_cache(box_no)
                    except Exception as e:
                        log(f"     ❌ [{box_no}] 发送失败: {e}")

                # 标为已读 + 落本地去重指纹（单行 INSERT，不再重写整个 JSON）
                if not skip_email:
                    if not TEST_MODE:
                        mail_conn.store(m_id, '+FLAGS', '\\Seen')
                        dedup_store.mark(DEDUP_SOURCE, msg_id)

            except Exception as e:
                log(f"  ❌ 邮件处理异常: {e}")
                if not TEST_MODE:
                    try:
                        mail_conn.store(m_id, '+FLAGS', '\\Seen')
                    except Exception:
                        pass

        mail_conn.close()
        mail_conn.logout()

    log("-" * 40)
    log(f"ATB 转发完成: 成功 {total_forwarded} 封 | 跳过 {total_skipped} 封")
    log("=" * 60)
    return total_forwarded


# ==================== 时间戳回写 yxo.db（不再写飞书总表）====================
# B方案P1：时间戳回写目标由飞书总表切到 yxo.db（经 Flask /api/stamp）=====
STAMP_API = "http://127.0.0.1:5011/api/stamp"
# 凭据外置（2026-08-10 芙蕾雅）：原硬编码 token 改为从 config_local 读取
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_local import STAMP_TOKEN


def _stamp_via_api(box_no, field, ts):
    """把时间戳写到 yxo.db（Flask /api/stamp，按箱号匹配最新未删除记录）。返回 True=已写入。"""
    import urllib.request, json as _json
    payload = _json.dumps({"box_no": box_no, "field": field, "value": ts}).encode("utf-8")
    req = urllib.request.Request(STAMP_API, data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "X-Stamp-Token": STAMP_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
        return bool(body.get("updated"))
    except Exception as e:
        log(f"     ⚠ /api/stamp 调用失败(箱号 {box_no}, {field}): {e}")
        return False


def _write_stamp(box_no, field):
    """时间戳回写 yxo.db（不再写飞书总表）；匹配键=箱号；失败/缺失记入待回填队列。"""
    ts = datetime.now().strftime("%m/%d %H:%M")
    if _stamp_via_api(box_no, field, ts):
        return True
    log(f"     ⚠ 箱号 {box_no} 时间戳未写入 yxo.db（箱号尚未入系统或接口异常），已记入待回填队列")
    _enqueue_pending_stamp(box_no, field, ts)
    return False


_PENDING_STAMP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_stamps.json")


def _load_pending_stamps():
    try:
        if os.path.exists(_PENDING_STAMP_FILE):
            with open(_PENDING_STAMP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_pending_stamps(lst):
    try:
        atomic_write_json(_PENDING_STAMP_FILE, lst)
    except Exception:
        pass


def _enqueue_pending_stamp(box_no, field, ts):
    lst = _load_pending_stamps()
    lst.append({
        "box_no": box_no,
        "field": field,
        "ts": ts,
        "enqueued": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_pending_stamps(lst)


def _replay_pending_stamps():
    if TEST_MODE:
        return
    lst = _load_pending_stamps()
    if not lst:
        return
    remain = []
    done = 0
    for it in lst:
        # 经 /api/stamp 按箱号回写 yxo.db
        if _stamp_via_api(it["box_no"], it["field"], it["ts"]):
            done += 1
            log(f"     ✅ 回填 {it['field']} 时间戳 → 箱号 {it['box_no']}")
        else:
            remain.append(it)
    if done or len(remain) != len(lst):
        _save_pending_stamps(remain)
    if done:
        log(f"     🔄 待回填队列处理 {done} 条，剩余 {len(remain)} 条")
    if remain:
        log(f"     ⚠【自检】待回填时间戳队列仍有 {len(remain)} 条未消：这些箱号可能尚未入 yxo.db，"
            f"或需重启机器人后下一轮再试。明细见 pending_stamps.json。")


# ==================== 入口 ====================

if __name__ == "__main__":
    import traceback as _tb
    ERROR_LOG_DIR = os.path.join(os.path.dirname(__file__), "error_logs")
    os.makedirs(ERROR_LOG_DIR, exist_ok=True)
    try:
        daemon_loop("atb", run_atb_robot)

        # ── 每日 0 点日报 ──
        now = datetime.now()
        if now.hour == 0 and now.minute < 30:
            log("⏰ 正在整理昨日 ATB 转发日报...")
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            log_path = os.path.join(BOT_LOG_DIR, f"atb_{yesterday}.log")
            report_path = os.path.join(BOT_LOG_DIR, f"atb_report_{yesterday}.txt")

            if os.path.exists(log_path):
                forwarded_lines = []
                companies = {}
                with open(log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if "✅" in line and "→" in line:
                            forwarded_lines.append(line.strip())
                            m = re.search(r'公司:(\S+)', line)
                            if m:
                                comp = m.group(1)
                                companies[comp] = companies.get(comp, 0) + 1

                total = len(forwarded_lines)
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(f"ATB 转发日报 [{yesterday}]\n")
                    f.write(f"总计转发: {total} 封\n")
                    f.write("=" * 40 + "\n\n")
                    if companies:
                        f.write("按公司统计:\n")
                        for c, n in sorted(companies.items(), key=lambda x: -x[1]):
                            f.write(f"  {c}: {n} 封\n")
                log(f"✅ ATB 日报已生成: {report_path}")
            else:
                log(f"⚠ 昨日日志不存在: {log_path}")
    except Exception as e:
        script_name = os.path.basename(__file__)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        err_file = os.path.join(ERROR_LOG_DIR, f"{script_name}_{ts}.log")
        with open(err_file, "w", encoding="utf-8") as f:
            f.write(f"Time: {datetime.now().isoformat()}\n")
            f.write(f"Script: {script_name}\n")
            f.write(f"Error: {e}\n\n")
            f.write(_tb.format_exc())
        print(f"FATAL ERROR logged: {err_file}")