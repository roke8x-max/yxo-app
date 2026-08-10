#!/usr/bin/env python
"""
DSK 邮件自动转发机器人 (v3)
基于 v2 架构，新增：本地缓存优先加载 + 飞书 API 重试 + 转发后本地缓存清理
（机器人不再删除 DSK_CONFIG 飞书记录，改由 Database_Syncer 统一清理）

功能流程：
  1. 优先读本地缓存 → 过期则回退飞书直读 → 飞书直读后写缓存
  2. 遍历 config.ACCOUNTS 全部 4 账户，每个账户选中 "dsk" 文件夹的未读邮件
  3. 发件人过滤：kasa@rtsb.de（从 config.DSK_SENDER_FILTER 导入）
  4. 去重：本地 SQLite 指纹库点查（dedup_store，首次从 TABLE_DSK_LOG 播种一次，
     之后不再拉飞书；90 天自动清理）
  5. 从正文 HTML 表格提取箱号行 → 查 box_company（来自缓存或总表）
  6. 按箱号拆分转发（HTML 行裁剪 + 附件按文件名匹配）
  7. 路由：box_record_map（箱号精确）→ default_map（公司DEFAULT）→ 跳过
  8. 每封转发后写入 TABLE_DSK_LOG（审计流水，非去重源）+ 回写总表时间戳
     + 落本地去重指纹 + 更新本地缓存
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

# ==================== 路径与依赖 ====================
sys.path.insert(0, r'D:\YXO_DATA\WeComBot')
from common_io import atomic_write_json, daemon_loop

from bs4 import BeautifulSoup
import lark_oapi as lark
from lark_oapi.api.bitable.v1 import *

from config import (
    FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN,
    TABLE_MAIN, TABLE_DSK_CONFIG, TABLE_DSK_LOG,
    SMTP_SERVER, SMTP_PORT,
    ACCOUNTS,
    DSK_SENDER_FILTER,
    DSK_DEFAULT_TO, DSK_DEFAULT_CC,
    BOT_LOG_DIR,
    company_to_name,
    sender_for_company,
)
from cs_bot import wecom_api

# 本地去重指纹库（2026-07-31 芙蕾雅 P1：替代每次全量拉取 TABLE_DSK_LOG）
import dedup_store

# 退舱跳过（2026-08-03）：状态=退舱的记录不再转发相关邮件
from db_write import is_box_cancelled, get_conn

# ==================== DSK 常量 ====================
DSK_IMAP_SERVER = "imap.qiye.aliyun.com"
DSK_IMAP_PORT = 993
DSK_FOLDER = "dsk"

# 去重命名空间：DSK 与 ATB 共用（改造前两者都读整张 TABLE_DSK_LOG，保持等价）
DEDUP_SOURCE = "dsk_log"
DEDUP_RETENTION_DAYS = 90

# 容器编号正则：4 个大写字母 + 7 位数字（如 MCDU1780087）
CONTAINER_RE = re.compile(r'^[A-Z]{4}\d{7}$')

# 公司名映射：总表中的公司名 → 内部短名（与 Database_Syncer.py 保持一致）
COMPANY_ALIAS = {
    "公运沙坪坝": "沙坪坝",
    "重轮太平洋": "太平洋",
}

# ===== 本地缓存 =====
CACHE_DIR = r"D:\YXO_DATA\WeComBot\cache"
CACHE_FILE = os.path.join(CACHE_DIR, "dsk_config_cache.json")
CACHE_TTL_MINUTES = 60

os.makedirs(BOT_LOG_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# ==================== 测试模式 ====================
# 设为 True 时：搜 7 月以来全部邮件、统一发送到测试邮箱、
# 标题加入公司名称和客户编码、跳过去重、不标已读
TEST_MODE = False
TEST_TO = "3841559246@qq.com"


# ==================== 工具函数 ====================

def split_emails(email_str):
    """将收件人字符串拆分为邮箱列表，兼容分号、逗号、空格分隔"""
    if not email_str or str(email_str).strip().lower() == "none":
        return []
    raw_list = re.split(r'[;,，\s]+', str(email_str))
    return [e.strip() for e in raw_list if "@" in e]


def log(msg):
    """写入本地日志 + 控制台输出"""
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(BOT_LOG_DIR, f"dsk_{today}.log")
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def decode_subject(msg):
    """解码邮件标题"""
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


def retry_lark_api(api_call, max_retries=3, base_delay=2):
    """指数退避重试飞书 API 调用"""
    last_result = None
    for attempt in range(max_retries):
        try:
            result = api_call()
            if hasattr(result, 'success') and not result.success():
                last_result = result
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    log(f"      ⚠ 飞书API失败 [{result.code}]: {result.msg}，{delay}s后重试 ({attempt+1}/{max_retries})...")
                    time.sleep(delay)
                    continue
                return result
            return result
        except Exception as e:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                log(f"      ⚠ 飞书API异常: {e}，{delay}s后重试 ({attempt+1}/{max_retries})...")
                time.sleep(delay)
            else:
                raise
    return last_result


def load_dsk_config_from_cache():
    """从本地缓存加载 DSK 配置数据。返回 dict 或 None"""
    if not os.path.exists(CACHE_FILE):
        return None

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)

        updated_at = cache.get("updated_at", "")
        ttl = cache.get("ttl_minutes", CACHE_TTL_MINUTES)

        # 检查 TTL
        try:
            cache_time = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
            age = (datetime.now() - cache_time).total_seconds() / 60
            if age > ttl:
                log(f"  ⏰ 本地缓存已过期（{age:.1f}分钟 > {ttl}分钟TTL），回退飞书直读")
                return None
        except (ValueError, Exception):
            log(f"  ⚠ 缓存时间格式异常，回退飞书直读")
            return None

        log(f"  ✅ 命中本地缓存（{updated_at}，已存{age:.1f}分钟）")
        return cache

    except Exception as e:
        log(f"  ⚠ 读取本地缓存失败: {e}，回退飞书直读")
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


# ==================== 飞书多维表格 ====================

class FeishuBitable:
    """飞书多维表格读写封装（带重试）"""

    def __init__(self):
        self.client = lark.Client.builder() \
            .app_id(FEISHU_APP_ID) \
            .app_secret(FEISHU_APP_SECRET) \
            .build()

    def get_all_records(self, table_id, max_retries=3):
        """分页拉取表格全部记录（带整体重试）"""
        for attempt in range(max_retries):
            all_items = []
            page_token = ""
            failed = False
            while True:
                builder = ListAppTableRecordRequest.builder() \
                    .app_token(FEISHU_APP_TOKEN) \
                    .table_id(table_id) \
                    .page_size(500)
                if page_token:
                    builder.page_token(page_token)
                request = builder.build()
                response = self.client.bitable.v1.app_table_record.list(request)
                if not response.success():
                    log(f"  ❌ 读取表格 [{table_id}] 失败: {response.msg}")
                    failed = True
                    break
                items = response.data.items
                if items:
                    all_items.extend(items)
                if not response.data.has_more:
                    break
                page_token = response.data.page_token

            if not failed:
                return all_items

            if attempt < max_retries - 1:
                delay = 2 * (2 ** attempt)
                log(f"  ⚠ 读取 [{table_id}] 重试 ({attempt+1}/{max_retries})，{delay}s后...")
                time.sleep(delay)

        log(f"  ❌ 读取 [{table_id}] 重试耗尽，返回空列表")
        return []

    def add_record(self, table_id, fields):
        """向表格追加一条记录（带重试）"""
        req_builder = CreateAppTableRecordRequest.builder() \
            .app_token(FEISHU_APP_TOKEN) \
            .table_id(table_id) \
            .request_body(AppTableRecord.builder().fields(fields).build())
        resp = retry_lark_api(lambda: self.client.bitable.v1.app_table_record.create(req_builder.build()))
        if resp is None or not resp.success():
            msg = resp.msg if resp else '重试耗尽'
            print(f"      ❌ 飞书日志保存失败! 原因: {msg}")
            return False
        return True

    def update_record(self, table_id, record_id, fields):
        """更新表格中一条记录的指定字段（带重试）"""
        req_builder = UpdateAppTableRecordRequest.builder() \
            .app_token(FEISHU_APP_TOKEN) \
            .table_id(table_id) \
            .record_id(record_id) \
            .request_body(AppTableRecord.builder().fields(fields).build())
        resp = retry_lark_api(lambda: self.client.bitable.v1.app_table_record.update(req_builder.build()))
        if resp is None or not resp.success():
            msg = resp.msg if resp else '重试耗尽'
            log(f"      ⚠ 更新记录 [{record_id}] 失败: {msg}")
            return False
        return True


# ==================== 邮件解析与 HTML 重组 ====================

def parse_email_structure(msg):
    """
    解析邮件 MIME 结构，返回：
      html_body  : HTML 正文 (str or None)
      plain_body : 纯文本正文 (str or None)
      attachments: [(part, filename), ...]
      inline_parts: [part, ...]
    """
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


def extract_container_table(html_body):
    """
    从 HTML 正文中定位「容器编号表格」。
    识别标准：表格中有单元格文本匹配 CONTAINER_RE（4 大写字母 + 7 数字）。

    返回: (soup, container_rows, table_element)
      container_rows: [(box_no, associated_code, tr_soup_element), ...]
    """
    if not html_body:
        return None, [], None

    soup = BeautifulSoup(html_body, 'html.parser')

    for table in soup.find_all('table'):
        rows_data = []
        for row in table.find_all('tr'):
            cells = row.find_all(['td', 'th'])
            if len(cells) >= 2:
                cell1_text = cells[0].get_text(strip=True)
                if CONTAINER_RE.match(cell1_text):
                    cell2_text = cells[1].get_text(strip=True)
                    rows_data.append((cell1_text, cell2_text, row))
        if rows_data:
            return soup, rows_data, table

    return None, [], None


def build_single_box_html(soup, table, all_rows, target_box):
    """修改 HTML：表格中删除非 target_box 的行。返回修改后的 HTML 字符串。"""
    for box_no, _, row in all_rows:
        if box_no != target_box:
            row.decompose()
    return str(soup)


def filter_attachments(attachments, box_no):
    """筛选附件：仅保留文件名中包含 box_no 的（不区分大小写）。"""
    matched = []
    for part, filename in attachments:
        if box_no.upper() in filename.upper():
            matched.append((part, filename))
    return matched


# ==================== SMTP 转发 ====================

def forward_one_box(original_msg, box_no, assoc_code,
                    html_body, plain_body,
                    attachments, inline_parts,
                    to_list, cc_list, sender_email, sender_password,
                    custom_subject=None):
    """为单个箱号构建并发送转发邮件。"""
    msg = MIMEMultipart('mixed')

    if custom_subject:
        msg['Subject'] = custom_subject
    else:
        original_subject = decode_subject(original_msg)
        msg['Subject'] = f"{original_subject} - {box_no}"
    msg['From'] = sender_email
    msg['To'] = ", ".join(to_list)
    if cc_list:
        msg['Cc'] = ", ".join(cc_list)

    # 正文：重新解析 HTML，只保留当前箱号行
    if html_body:
        soup, container_rows, table = extract_container_table(html_body)
        if soup and container_rows:
            modified_html = build_single_box_html(soup, table, container_rows, box_no)
            msg.attach(MIMEText(modified_html, 'html', 'utf-8'))
        else:
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    elif plain_body:
        msg.attach(MIMEText(plain_body, 'plain', 'utf-8'))

    # 附件：按箱号筛选
    matched = filter_attachments(attachments, box_no)
    for part, filename in matched:
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        new_part = MIMEBase(
            part.get_content_maintype(),
            part.get_content_subtype()
        )
        new_part.set_payload(payload)
        encoders.encode_base64(new_part)
        new_part.add_header(
            'Content-Disposition', 'attachment',
            filename=('utf-8', '', filename)
        )
        msg.attach(new_part)

    # 内联资源全部保留
    for part in inline_parts:
        msg.attach(part)

    # 发送
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(sender_email, sender_password)
        all_recipients = list(set(to_list + cc_list))
        server.sendmail(sender_email, all_recipients, msg.as_string())


# ==================== 路由解析 ====================

def resolve_recipients(box_no, company, company_map, default_map, box_record_map):
    """
    三级回退解析收件人：

    1. box_record_map[box_no] → 先查该箱号记录的 To
       - 如果 To 不为空 → 直接使用
       - 如果 To 为空 → 进入第 2 级
    2. default_map[company] → 查该公司的 DEFAULT 行的 To
       - 如果 To 不为空 → 使用
       - 如果 To 为空 → 进入第 3 级
    3. config.py DSK_DEFAULT_TO / DSK_DEFAULT_CC → 硬编码兜底

    返回: (to_list, cc_list, source_str)
    """
    to_list = []
    cc_list = []
    source = ""

    # 1. 箱号记录（含空 To 的情况）
    box_info = box_record_map.get(box_no)
    if box_info and box_info['to']:
        to_list = box_info['to']
        cc_list = box_info['cc']
        source = "箱号精确配置"
        return to_list, cc_list, source

    # 2. DEFAULT 回退（如果第1级箱号记录To为空或不存在）
    if company and company in default_map:
        def_info = default_map[company]
        if def_info['to']:
            to_list = def_info['to']
            cc_list = def_info['cc']
            source = "DEFAULT回退"
            return to_list, cc_list, source

    # 3. 无路由配置
    return [], [], ""


def load_dsk_routing():
    """P4③（2026-08-06 小叽）：从本地 yxo.db bot_config 读取 DSK 路由，替代飞书 TABLE_DSK_CONFIG。
    返回 (default_map, box_record_map)：
      default_map:    {公司: {'to':[...], 'cc':[...]}}  来自 scope='company'
      box_record_map: {}（逐箱路由已废弃，P4③ 只迁 DEFAULT）
    """
    import json as _json
    try:
        conn = get_conn()
    except Exception as e:
        log(f"  ⚠ 读取本地 DSK 路由失败: {e}")
        return {}, {}
    cur = conn.cursor()
    default_map = {}
    for key, to_a, cc_a in cur.execute(
        "SELECT key, to_addrs, cc_addrs FROM bot_config WHERE bot='dsk' AND scope='company'"):
        default_map[key] = {
            'to': _json.loads(to_a or '[]'),
            'cc': _json.loads(cc_a or '[]'),
        }
    conn.close()
    return default_map, {}


# ==================== 主流程 ====================

def run_dsk_robot():
    # ── 0. 开关闸门（2026-08-03 芙蕾雅：live=false 则本轮回退，不扫描/不转发/不写库）──
    try:
        from bot_config import load_bot_config
        _cfg = load_bot_config("dsk")
        if not _cfg["live"]:
            log("⏸ DSK 机器人已停用(live=false)，本轮回退（不扫描/不转发/不写库）。")
            return 0
    except Exception as _e:
        log(f"  ⚠ 读取开关配置失败(按运行处理): {_e}")
    fs = FeishuBitable()
    _MAIN_LOOKUP.clear()
    log("=" * 60)
    log(f"DSK 邮件自动转发机器人 启动 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    log("=" * 60)

    # ── A. 加载配置数据（三级：本地缓存 → 飞书直读 → 过期缓存兜底）──

    cache = load_dsk_config_from_cache()
    loaded_from_cache = cache is not None

    if loaded_from_cache:
        box_company = cache["box_company"]
        box_customer_code = cache["box_customer_code"]
        default_map = cache["default_map"]
        box_record_map = cache["box_record_map"]
        box_config_ids = cache.get("box_config_ids", {})
        company_map = {}
        for box_no, info in box_record_map.items():
            if info.get('to'):
                comp = info.get('comp', '')
                if comp not in company_map:
                    company_map[comp] = []
                company_map[comp].append({'to': info['to'], 'cc': info['cc']})
        box_record_id = cache.get("box_record_id", {})  # 缓存命中：优先用缓存的 record_id，避免全表扫描
        log(f"  箱号映射 {len(box_company)} 个, "
            f"客户编码 {len(box_customer_code)} 个, "
            f"DEFAULT {len(default_map)} 个公司, "
            f"箱号记录 {len(box_record_map)} 条")
    else:
        # 回退飞书直读
        log("正在加载订舱总表 (TABLE_MAIN) ...")
        main_records = fs.get_all_records(TABLE_MAIN)
        box_company = {}
        box_customer_code = {}
        box_record_id = {}
        for r in main_records:
            box = str(r.fields.get('箱号', '')).strip()
            comp = str(r.fields.get('开票子公司名称', '')).strip()
            comp = COMPANY_ALIAS.get(comp, comp)
            code = str(r.fields.get('客户编码', '')).strip()
            if box and comp and box.lower() != 'none' and comp.lower() != 'none':
                box_company[box] = comp
            if box and code and code.lower() != 'none':
                box_customer_code[box] = code
            if box:
                box_record_id[box] = r.record_id
        log(f"  总表: {len(main_records)} 行 → 有效箱号映射 {len(box_company)} 个, "
            f"客户编码 {len(box_customer_code)} 个")

        # P4③（2026-08-06 小叽）：DSK/ATB 路由配置改读本地 yxo.db bot_config，
        # 不再读飞书 TABLE_DSK_CONFIG（只迁逐公司 DEFAULT，逐箱路由已废弃）。
        default_map, box_record_map = load_dsk_routing()
        company_map = {}
        box_config_ids = {}
        log(f"  📋 本地路由: DEFAULT {len(default_map)} 个公司（逐箱路由已停用）")

        # 飞书直读后写本地缓存
        # 启动回放待回填时间戳队列（2026-07-31 芙蕾雅：箱号稍后才进总表的，转发时漏写的可在此补回）
    _replay_pending_stamps(fs, TABLE_MAIN, box_record_id)
    save_dsk_config_cache(box_company, box_customer_code, default_map, box_record_map, box_config_ids, box_record_id)

    # ── C. 去重指纹：本地 SQLite 点查（2026-07-31 芙蕾雅 P1 改造）──
    # 改造前：每次运行都 fs.get_all_records(TABLE_DSK_LOG) 拉整张飞书日志表，
    #         该表从不清理 → 两年后越拉越慢（这是「每次转发都全表搜索」的真实来源）。
    # 改造后：首次运行播种一次，之后判重走本地主键点查 O(1)，不再拉飞书；90 天自动清理。
    if not TEST_MODE:
        dedup_store.seed_from_feishu(
            DEDUP_SOURCE, fs, TABLE_DSK_LOG, field_name="邮件唯一标识", log=log
        )
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
            mail_conn = imaplib.IMAP4_SSL(DSK_IMAP_SERVER, DSK_IMAP_PORT, timeout=60)
            mail_conn.login(email_addr, password)
        except Exception as e:
            log(f"  ❌ IMAP 登录失败: {e}")
            continue

        res, _ = mail_conn.select(DSK_FOLDER)
        if res != 'OK':
            log(f"  ⚠ 无法选中文件夹 [{DSK_FOLDER}]，跳过此账户")
            mail_conn.logout()
            continue

        # 搜索邮件（测试模式：7月以来全部；正常模式：未读）
        if TEST_MODE:
            res, data = mail_conn.search(None, '(SINCE "01-Jul-2026")')
        else:
            res, data = mail_conn.search(None, 'UNSEEN')
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
                if sender.lower() != DSK_SENDER_FILTER.lower():
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

                # --- 提取容器表格 ---
                soup, container_rows, _ = extract_container_table(html_body)
                if not container_rows:
                    log(f"  ⏭ [{subject}] — 正文中未找到箱号表格")
                    if not TEST_MODE:
                        mail_conn.store(m_id, '+FLAGS', '\\Seen')
                    total_skipped += 1
                    continue

                box_list = [c[0] for c in container_rows]
                log(f"  📧 [{subject}] 箱号: {box_list}")

                # --- 路由预检：所有箱号必须可路由，否则整封跳过 ---
                unresolved_box = None
                unresolved_company = None
                for box_no, assoc_code, _ in container_rows:
                    tmp_company = box_company.get(box_no)
                    tmp_to, _, _ = resolve_recipients(
                        box_no, tmp_company, company_map, default_map, box_record_map
                    )
                    if not tmp_to:
                        unresolved_box = box_no
                        unresolved_company = tmp_company
                        break
                if unresolved_box:
                    log(f"  ⏭ [{subject}] 箱号 {unresolved_box} "
                        f"(公司: {unresolved_company or '未知'}) 无路由配置(DEFAULT行), "
                        f"整封邮件跳过")
                    total_skipped += 1
                    continue

                # --- 按箱号拆分转发 ---
                for box_no, assoc_code, _ in container_rows:
                    company = box_company.get(box_no)
                    customer_code = box_customer_code.get(box_no, "")

                    # ── 退舱跳过（2026-08-03）：状态=退舱的箱号不再转发相关邮件 ──
                    if is_box_cancelled(box_no):
                        log(f"     ⏭ [{box_no}] 状态=退舱，跳过转发（不发送 DSK 邮件）")
                        total_skipped += 1
                        continue

                    # ── 测试模式：固定测试收件人 ──
                    if TEST_MODE:
                        to_list = split_emails(TEST_TO)
                        cc_list = []
                        src = "测试模式"
                    else:
                        to_list, cc_list, src = resolve_recipients(
                            box_no, company, company_map, default_map, box_record_map
                        )

                    # 记录路由来源
                    if not company:
                        src = "总表无此箱号"

                    # 发件人：按箱号所属公司的负责同事发信（收件人看到的是同事邮箱）
                    sen = sender_for_company(company)
                    s_email, s_pwd = (sen if (sen and sen[1]) else (email_addr, password))

                    # ── 测试模式：标题加入公司名和客户编码 ──
                    if TEST_MODE:
                        subject_override = (f"【{box_no}】{subject} - "
                                            f"{company or '未知公司'} - "
                                            f"{customer_code or '未知编码'}")
                    else:
                        subject_override = (f"【{box_no}】{subject} - "
                                            f"{customer_code or '未知编码'}")

                    try:
                        forward_one_box(
                            msg, box_no, assoc_code,
                            html_body, plain_body,
                            attachments, inline_parts,
                            to_list, cc_list, s_email, s_pwd,
                            custom_subject=subject_override
                        )
                        log(f"     ✅ [{box_no}] → 公司:{company or '未知'} "
                            f"({src}) To:{to_list} | 由:{s_email}")
                        total_forwarded += 1

                        # 统一转发日志（供每日 8 点汇总邮件用；失败不影响转发）
                        try:
                            import forward_log
                            forward_log.record(
                                "DSK", owner=company_to_name(company) or "",
                                company=company or "", code=customer_code or "",
                                box=box_no, subject=subject,
                                to_list=to_list, sender=s_email, test=TEST_MODE)
                        except Exception:
                            pass

                        # 通知负责人（微信客服通道，微信可收；失败回退企微应用消息）
                        try:
                            owner = company_to_name(company)
                            if owner and not TEST_MODE:
                                notice = (
                                    f"📧 DSK 邮件已转发\n"
                                    f"箱号：{box_no}\n"
                                    f"公司：{company or '未知'}\n"
                                    f"收件人：{','.join(to_list)}\n"
                                    f"发件：{s_email}"
                                )
                                ok, ch = wecom_api.notify_by_name(owner, notice)
                                log(f"     📲 通知 {owner}: {'成功(' + ch + ')' if ok else '失败'}")
                        except Exception as e:
                            log(f"     ⚠ 通知负责人失败: {e}")

                        # 回写 dsk 时间戳到总表（2026-07-31 芙蕾雅修复：缓存命中时 box_record_id 为空，改现查总表兜底）
                        if not TEST_MODE:
                            _write_stamp(fs, TABLE_MAIN, box_record_id, box_no, "dsk")

                        # 本地审计：不再写飞书日志表（P4③ 2026-08-06 小叽）
                        if not TEST_MODE:
                            remove_box_from_cache(box_no)
                    except Exception as e:
                        log(f"     ❌ [{box_no}] 发送失败: {e}")

                # 全部箱号处理完毕 → 标为已读 + 落本地去重指纹（测试模式跳过）
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
    log(f"DSK 转发完成: 成功 {total_forwarded} 封 | 跳过 {total_skipped} 封")
    log("=" * 60)
    return total_forwarded


# ==================== 时间戳回写兜底（2026-07-31 芙蕾雅修复）====================
# 根因：缓存命中运行时 box_record_id 被置空({})，导致原 752 行的
#   `if box_no in box_record_id` 恒为 False，DSK 时间戳几乎从不回写总表。
# 修复：写时间戳时若缓存无 record_id，现查一次总表兜底；仍找不到则记入待回填队列，
#       下一轮启动回放补写（处理箱号稍后才进总表的时序差）。
_MAIN_LOOKUP = {}
_PENDING_STAMP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_stamps.json")


def _resolve_record_id(fs, table, box_record_id, box_no):
    """返回总表中该箱号的 record_id；缓存缺失时现查总表兜底；找不到返回 None。"""
    if box_no in box_record_id:
        return box_record_id[box_no]
    if box_no not in _MAIN_LOOKUP:
        try:
            rows = fs.get_all_records(table)
            for r in rows:
                b = str(r.fields.get('箱号', '') or '').strip()
                if b:
                    _MAIN_LOOKUP[b] = r.record_id
        except Exception as e:
            log(f"     ⚠ 现查总表失败(回写时间戳兜底): {e}")
    rid = _MAIN_LOOKUP.get(box_no)
    if rid:
        box_record_id[box_no] = rid
    return rid


# ===== B方案P1：时间戳回写目标由飞书总表切到 yxo.db（经 Flask /api/stamp）=====
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

def _write_stamp(fs, table, box_record_id, box_no, field):
    """B方案P1：时间戳回写 yxo.db（不再写飞书总表）；匹配键=箱号；失败/缺失记入待回填队列。"""
    ts = datetime.now().strftime("%m/%d %H:%M")
    if _stamp_via_api(box_no, field, ts):
        return True
    log(f"     ⚠ 箱号 {box_no} 时间戳未写入 yxo.db（箱号尚未入系统或接口异常），已记入待回填队列")
    _enqueue_pending_stamp(box_no, field, ts)
    return False


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


def _replay_pending_stamps(fs, table, box_record_id):
    if TEST_MODE:
        return
    lst = _load_pending_stamps()
    if not lst:
        return
    remain = []
    done = 0
    for it in lst:
        # B方案P1：经 /api/stamp 按箱号回写 yxo.db
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


if __name__ == "__main__":
    import traceback as _tb
    ERROR_LOG_DIR = os.path.join(os.path.dirname(__file__), "error_logs")
    os.makedirs(ERROR_LOG_DIR, exist_ok=True)
    try:
        daemon_loop("dsk", run_dsk_robot)

        # ── 每日 0 点日报 ──
        now = datetime.now()
        if now.hour == 0 and now.minute < 30:
            log("⏰ 正在整理昨日 DSK 转发日报...")
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            log_path = os.path.join(BOT_LOG_DIR, f"dsk_{yesterday}.log")
            report_path = os.path.join(BOT_LOG_DIR, f"dsk_report_{yesterday}.txt")

            if os.path.exists(log_path):
                forwarded_lines = []
                companies = {}
                boxes = {}
                with open(log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if "✅" in line and "→" in line:
                            forwarded_lines.append(line.strip())
                            m = re.search(r'公司:(\S+)', line)
                            if m:
                                comp = m.group(1)
                                companies[comp] = companies.get(comp, 0) + 1
                            m2 = re.search(r'\[([A-Z]{4}\d{7})\]', line)
                            if m2:
                                box = m2.group(1)
                                boxes[box] = boxes.get(box, 0) + 1

                total = len(forwarded_lines)
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(f"DSK 转发日报 [{yesterday}]\n")
                    f.write(f"总计转发: {total} 封\n")
                    f.write("=" * 40 + "\n\n")
                    if companies:
                        f.write("按公司统计:\n")
                        for c, n in sorted(companies.items(), key=lambda x: -x[1]):
                            f.write(f"  {c}: {n} 封\n")
                    if boxes:
                        f.write("\n箱号明细:\n")
                        for b, n in sorted(boxes.items()):
                            f.write(f"  {b}: {n} 封\n")
                log(f"✅ DSK 日报已生成: {report_path}")
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
