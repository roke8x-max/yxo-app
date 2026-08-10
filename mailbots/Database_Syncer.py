"""
数据库同步脚本
1. 运踪配置表同步：总表班列号+公司 → TABLE_CONFIG
2. DSK配置表同步：总表箱号+公司 → TABLE_DSK_CONFIG
3. 同步完成后写本地缓存，供机器人优先读取
"""

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import *
import sys
import re
import os
import imaplib
import email
import json
import time
import functools
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from common_io import atomic_write_json
import sys as _sys
_sys.path.insert(0, r'D:\YXO_DATA\WeComBot')
from config import (
    FEISHU_APP_ID as APP_ID,
    FEISHU_APP_SECRET as APP_SECRET,
    FEISHU_APP_TOKEN as APP_TOKEN,
)

# ================= 配置 =================
# 体检E（2026-08-04）：飞书凭据不再硬编码，统一从 WeComBot/secrets.json 保险库经 config 读取。

TABLE_MAIN = "tbl73fJJQmk4S8ly"        # 订舱总表
TABLE_CONFIG = "tbl4wFdo9scMmUM7"      # 运踪配置表
TABLE_DSK_CONFIG = "tblpp0CHtSYDDKru"   # DSK配置表

COL_TRAIN_NO = "班列号"
COL_COMPANY = "开票子公司名称"
COL_BOX = "箱号"
COL_CUSTOMER_CODE = "客户编码"
COL_STATUS = "数据状态"
COL_DEPART_TIME = "发班时间"

# DSK IMAP
DSK_IMAP_SERVER = "imap.qiye.aliyun.com"
DSK_IMAP_PORT = 993
DSK_FOLDER = "dsk"
# 凭据外置（2026-08-10 芙蕾雅）：原硬编码明文密码改为从 config_local 读取
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_local import DSK_ACCOUNTS

# 日期过滤（仅同步发班时间>=该日期的记录）
SYNC_FROM_DATE = datetime(2026, 6, 1)

# 容器编号正则
CONTAINER_RE = re.compile(r'^[A-Z]{4}\d{7}$')

# 公司名映射：总表中的公司名 → 内部短名（用于 DSK_CONFIG 匹配）
COMPANY_ALIAS = {
    "公运沙坪坝": "沙坪坝",
    "重轮太平洋": "太平洋",
}

# ===== 本地缓存 =====
CACHE_DIR = r"D:\YXO_DATA\WeComBot\cache"
CACHE_FILE = os.path.join(CACHE_DIR, "dsk_config_cache.json")
CACHE_TTL_MINUTES = 60


def retry_lark_api(api_func, *args, max_retries=3, base_delay=2, **kwargs):
    """指数退避重试飞书 API 调用"""
    last_result = None
    for attempt in range(max_retries):
        try:
            result = api_func(*args, **kwargs)
            if hasattr(result, 'success') and not result.success():
                last_result = result
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"    ⚠ 飞书API失败 [{result.code}]: {result.msg}，{delay}s后重试 ({attempt+1}/{max_retries})...")
                    time.sleep(delay)
                    continue
                return result
            return result
        except Exception as e:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"    ⚠ 飞书API异常: {e}，{delay}s后重试 ({attempt+1}/{max_retries})...")
                time.sleep(delay)
            else:
                raise
    return last_result


def retry_get_feishu_data(func):
    """装饰器：对 get_feishu_data 做整体重试（处理返回 None 的情况）"""
    @functools.wraps(func)
    def wrapper(client, table_id, max_retries=3, base_delay=2):
        for attempt in range(max_retries):
            result = func(client, table_id)
            if result is not None:
                return result
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"    ⚠ 读取表格 [{table_id}] 返回None，{delay}s后重试 ({attempt+1}/{max_retries})...")
                time.sleep(delay)
        return None
    return wrapper


# ==========================================

@retry_get_feishu_data
def get_feishu_data(client, table_id):
    all_items = []
    page_token = ""
    while True:
        builder = ListAppTableRecordRequest.builder() \
            .app_token(APP_TOKEN) \
            .table_id(table_id) \
            .page_size(500)
        if page_token:
            builder.page_token(page_token)
        request = builder.build()
        response = client.bitable.v1.app_table_record.list(request)
        if not response.success():
            print(f"❌ 读取表格 [{table_id}] 失败: {response.msg}")
            return None
        items = response.data.items
        if items:
            all_items.extend(items)
        if not response.data.has_more:
            break
        page_token = response.data.page_token
    return all_items


def scan_dsk_boxes():
    """遍历4个邮箱的dsk文件夹，从HTML表格提取所有出现过的箱号"""
    all_boxes = set()
    for email_addr, password in DSK_ACCOUNTS.items():
        try:
            conn = imaplib.IMAP4_SSL(DSK_IMAP_SERVER, DSK_IMAP_PORT, timeout=60)
            conn.login(email_addr, password)
            res, _ = conn.select(DSK_FOLDER)
            if res != 'OK':
                conn.logout()
                continue
            res, data = conn.search(None, 'ALL')
            if res != 'OK':
                conn.logout()
                continue
            mail_ids = data[0].split()
            print(f"  扫描 {email_addr} dsk 文件夹: {len(mail_ids)} 封邮件")

            for m_id in mail_ids:
                try:
                    _, msg_data = conn.fetch(m_id, '(RFC822)')
                    msg = email.message_from_bytes(msg_data[0][1])

                    html_body = None
                    plain_body = None
                    for part in msg.walk():
                        if part.is_multipart():
                            continue
                        content_type = part.get_content_type()
                        payload = part.get_payload(decode=True)
                        if not payload:
                            continue
                        charset = part.get_content_charset() or 'utf-8'
                        try:
                            text = payload.decode(charset, errors='replace')
                        except Exception:
                            continue
                        if content_type == 'text/html' and not html_body:
                            html_body = text
                        elif content_type == 'text/plain' and not plain_body:
                            plain_body = text

                    # 优先从 HTML 表格提取（与 Dsk_Robot.py 一致）
                    found_boxes = set()
                    if html_body:
                        soup = BeautifulSoup(html_body, 'html.parser')
                        for table in soup.find_all('table'):
                            for row in table.find_all('tr'):
                                cells = row.find_all(['td', 'th'])
                                if len(cells) >= 2:
                                    cell1_text = cells[0].get_text(strip=True)
                                    if CONTAINER_RE.match(cell1_text):
                                        found_boxes.add(cell1_text)

                    # Fallback：HTML 解析失败则纯文本正则搜索
                    if not found_boxes:
                        search_text = html_body or plain_body or ""
                        if search_text:
                            found_boxes.update(re.findall(r'\b[A-Z]{4}\d{7}\b', search_text))

                    all_boxes.update(found_boxes)
                except Exception:
                    pass

            conn.close()
            conn.logout()
        except Exception as e:
            print(f"  ⚠ {email_addr} 连接失败: {e}")

    return all_boxes


def sync_tracing(client, main_rows, config_rows):
    """运踪配置表同步（现有逻辑）"""
    print("\n--- 运踪配置表同步 ---")
    print(f"📈 总表: {len(main_rows)} 行")
    print(f"📈 配置表: {len(config_rows)} 行")

    config_map = {}
    for r in config_rows:
        t = str(r.fields.get('班列号', '')).strip()
        c = str(r.fields.get('公司名称', '')).strip()
        if t and c:
            config_map[f"{t}_{c}"] = True

    new_count = 0
    added_items_log = []

    for row in main_rows:
        fields = row.fields
        if fields.get(COL_STATUS) == '退舱':
            continue

        # 日期过滤
        if SYNC_FROM_DATE:
            depart_time = fields.get(COL_DEPART_TIME)
            if depart_time and isinstance(depart_time, (int, float)):
                dt = datetime.fromtimestamp(depart_time / 1000)
                if dt < SYNC_FROM_DATE:
                    continue

        train_no_raw = str(fields.get(COL_TRAIN_NO, '')).strip()
        company = str(fields.get(COL_COMPANY, '')).strip()

        if train_no_raw and company and train_no_raw != 'None' and company != 'None':
            train_id = re.sub(r'(?i)WB\s*', '', train_no_raw)
            key = f"{train_id}_{company}"
            if key not in config_map:
                print(f"   ✨ 新组合: {train_id} (原:{train_no_raw}) - {company}")
                new_record = {"班列号": train_id, "公司名称": company}
                req_builder = CreateAppTableRecordRequest.builder() \
                    .app_token(APP_TOKEN) \
                    .table_id(TABLE_CONFIG) \
                    .request_body(AppTableRecord.builder().fields(new_record).build())
                resp = retry_lark_api(
                    lambda: client.bitable.v1.app_table_record.create(req_builder.build())
                )
                if resp and resp.success():
                    config_map[key] = True
                    new_count += 1
                    added_items_log.append(
                        f"{datetime.now().strftime('%H:%M:%S')} | 班列: {train_id} | 公司: {company}")
                else:
                    print(f"   ⚠️ 写入失败: {resp.msg if resp else '重试耗尽'}")

    if added_items_log:
        log_dir = "SyncerLog"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        log_filename = f"{datetime.now().strftime('%Y-%m-%d')}.txt"
        log_path = os.path.join(log_dir, log_filename)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n--- 运踪新增 {new_count} 条 ---\n")
            f.write("\n".join(added_items_log) + "\n")
        print(f"\n📝 日志: {log_path}")

    print(f"✅ 运踪同步完成，新增 {new_count} 条")


def write_dsk_cache(main_rows, dsk_config_rows):
    """将 DSK 配置数据写入本地 JSON 缓存"""
    # 从总表构建 box_company 和 box_customer_code
    box_company = {}
    box_customer_code = {}
    for row in main_rows:
        fields = row.fields
        box = str(fields.get(COL_BOX, '')).strip()
        comp = str(fields.get(COL_COMPANY, '')).strip()
        comp = COMPANY_ALIAS.get(comp, comp)
        code = str(fields.get(COL_CUSTOMER_CODE, '')).strip()
        if box and comp and box.lower() != 'none' and comp.lower() != 'none':
            box_company[box] = comp
        if box and code and code.lower() != 'none':
            box_customer_code[box] = code

    # 从 DSK_CONFIG 构建 default_map, box_record_map, box_config_ids
    default_map = {}
    box_record_map = {}
    box_config_ids = {}

    def split_emails(email_str):
        if not email_str or str(email_str).strip().lower() == "none":
            return []
        raw_list = re.split(r'[;,，\s]+', str(email_str))
        return [e.strip() for e in raw_list if "@" in e]

    for r in dsk_config_rows:
        box_no = str(r.fields.get('箱号', '')).strip()
        comp = str(r.fields.get('公司名称', '')).strip()
        comp = COMPANY_ALIAS.get(comp, comp)
        to_list = split_emails(r.fields.get('收件人 (To)'))
        cc_list = split_emails(r.fields.get('抄送人 (CC)'))

        if not comp:
            continue

        if box_no.upper() == 'DEFAULT':
            default_map[comp] = {'to': to_list, 'cc': cc_list}
        else:
            box_record_map[box_no] = {'comp': comp, 'to': to_list, 'cc': cc_list}
            box_config_ids[box_no] = r.record_id

    cache = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "box_company": box_company,
        "box_customer_code": box_customer_code,
        "default_map": default_map,
        "box_record_map": box_record_map,
        "box_config_ids": box_config_ids,
        "ttl_minutes": CACHE_TTL_MINUTES
    }

    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

    atomic_write_json(CACHE_FILE, cache)

    print(f"\n📦 本地缓存已写入: {CACHE_FILE}")
    print(f"   箱号映射 {len(box_company)} 个, 配置 {len(box_record_map)} 条, DEFAULT {len(default_map)} 个")


def sync_dsk(client, main_rows, dsk_config_rows):
    """DSK配置表同步"""
    print("\n--- DSK配置表同步 ---")

    # 1. 扫描 dsk 文件夹已出现的箱号
    print("正在扫描邮件 dsk 文件夹...")
    already_sent_boxes = scan_dsk_boxes()
    print(f"  dsk 已出现箱号: {len(already_sent_boxes)} 个")

    # 2. 加载 DSK_CONFIG 现有数据
    dsk_map = {}       # {箱号: record_id}
    dsk_company_map = {}  # {箱号: 公司名称}
    dsk_default = {}   # {公司名称: {to, cc}}
    for r in dsk_config_rows:
        box = str(r.fields.get('箱号', '')).strip()
        comp = str(r.fields.get('公司名称', '')).strip()
        if box.upper() == 'DEFAULT':
            to_raw = r.fields.get('收件人 (To)', '')
            cc_raw = r.fields.get('抄送人 (CC)', '')
            dsk_default[comp] = {
                'to': to_raw if to_raw else '',
                'cc': cc_raw if cc_raw else ''
            }
        elif box:
            dsk_map[box] = r.record_id
            dsk_company_map[box] = comp
    print(f"  DSK_CONFIG: {len(dsk_map)} 条箱号记录, DEFAULT {len(dsk_default)} 个公司")

    # 2.5 清理：删除已在 dsk 出现的箱号记录（排除 DEFAULT 行）
    cleanup_count = 0
    for box in list(already_sent_boxes):
        if box in dsk_map:
            try:
                rid = dsk_map[box]
                req_builder = DeleteAppTableRecordRequest.builder() \
                    .app_token(APP_TOKEN) \
                    .table_id(TABLE_DSK_CONFIG) \
                    .record_id(rid)
                resp = retry_lark_api(
                    lambda: client.bitable.v1.app_table_record.delete(req_builder.build())
                )
                if resp and resp.success():
                    cleanup_count += 1
                    print(f"   🧹 清理: {box}（已在 dsk 出现）")
                    del dsk_map[box]
                    dsk_company_map.pop(box, None)
                else:
                    print(f"   ⚠ 清理失败 [{box}]: {resp.msg if resp else '重试耗尽'}")
            except Exception as e:
                print(f"   ⚠ 清理异常 [{box}]: {e}")
    if cleanup_count > 0:
        print(f"  🧹 清理完成: {cleanup_count} 条")

    # 3. 遍历总表
    new_count = 0
    update_count = 0
    added_items_log = []

    for row in main_rows:
        fields = row.fields

        # 跳过退舱
        if fields.get(COL_STATUS) == '退舱':
            continue

        box = str(fields.get(COL_BOX, '')).strip()
        comp = str(fields.get(COL_COMPANY, '')).strip()
        code = str(fields.get(COL_CUSTOMER_CODE, '')).strip()

        if not box or not comp or box.lower() == 'none' or comp.lower() == 'none':
            continue

        # 公司名别名映射
        comp = COMPANY_ALIAS.get(comp, comp)

        # 日期过滤
        if SYNC_FROM_DATE:
            depart_time = fields.get(COL_DEPART_TIME)
            if depart_time:
                if isinstance(depart_time, (int, float)):
                    dt = datetime.fromtimestamp(depart_time / 1000)
                    if dt < SYNC_FROM_DATE:
                        continue

        # 排除规则：dsk 已出现 → 跳过
        if box in already_sent_boxes:
            continue

        # 新增：总表有 + dsk未出现 + DSK_CONFIG无记录
        if box not in dsk_map:
            new_record = {
                "箱号": box,
                "公司名称": comp,
            }
            req_builder = CreateAppTableRecordRequest.builder() \
                .app_token(APP_TOKEN) \
                .table_id(TABLE_DSK_CONFIG) \
                .request_body(AppTableRecord.builder().fields(new_record).build())
            resp = retry_lark_api(
                lambda: client.bitable.v1.app_table_record.create(req_builder.build())
            )
            if resp and resp.success():
                dsk_map[box] = resp.data.record.record_id
                dsk_company_map[box] = comp
                new_count += 1
                added_items_log.append(
                    f"{datetime.now().strftime('%H:%M:%S')} | 新增: {box} | 公司: {comp}")
                print(f"   ✨ 新增: {box} → {comp}")
            else:
                print(f"   ⚠ 写入失败: {resp.msg if resp else '重试耗尽'}")

        # 变动更新：总表有 + DSK_CONFIG有 + 公司名变了
        elif dsk_company_map.get(box) != comp:
            old_comp = dsk_company_map[box]
            record_id = dsk_map[box]
            update_fields = {
                "公司名称": comp,
            }
            req_builder = UpdateAppTableRecordRequest.builder() \
                .app_token(APP_TOKEN) \
                .table_id(TABLE_DSK_CONFIG) \
                .record_id(record_id) \
                .request_body(AppTableRecord.builder().fields(update_fields).build())
            resp = retry_lark_api(
                lambda: client.bitable.v1.app_table_record.update(req_builder.build())
            )
            if resp and resp.success():
                dsk_company_map[box] = comp
                update_count += 1
                added_items_log.append(
                    f"{datetime.now().strftime('%H:%M:%S')} | 更新: {box} | "
                    f"{old_comp}→{comp}")
                print(f"   🔄 更新: {box} | {old_comp} → {comp}")
            else:
                print(f"   ⚠ 更新失败 [{box}]: {resp.msg if resp else '重试耗尽'}")

    if added_items_log:
        log_dir = "SyncerLog"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        log_filename = f"{datetime.now().strftime('%Y-%m-%d')}_DSK.txt"
        log_path = os.path.join(log_dir, log_filename)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n--- DSK 同步: 新增 {new_count} / 更新 {update_count} ---\n")
            f.write("\n".join(added_items_log) + "\n")
        print(f"\n📝 日志: {log_path}")

    print(f"✅ DSK同步完成，新增 {new_count} 条，更新 {update_count} 条")

    # 同步完成后写本地缓存
    write_dsk_cache(main_rows, dsk_config_rows)


def main():
    # ── 0. 开关闸门（2026-08-03 芙蕾雅：live=false 则本轮回退）──
    try:
        from bot_config import load_bot_config
        if not load_bot_config("syncer")["live"]:
            print("⏸ 数据库同步已停用(live=false)，本轮回退。")
            return
    except Exception as _e:
        print(f"  ⚠ 读取开关配置失败(按运行处理): {_e}")
    client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    print("🔄 正在连接飞书并提取数据...")

    main_rows = get_feishu_data(client, TABLE_MAIN)
    config_rows = get_feishu_data(client, TABLE_CONFIG)
    dsk_config_rows = get_feishu_data(client, TABLE_DSK_CONFIG)

    if main_rows is None or config_rows is None or dsk_config_rows is None:
        print("❌ 数据读取失败，终止。")
        return

    # 运踪同步
    sync_tracing(client, main_rows, config_rows)

    # DSK同步（内部会写缓存）
    sync_dsk(client, main_rows, dsk_config_rows)

    print("\n" + "=" * 40)
    print("全部同步任务完成。")


if __name__ == "__main__":
    import traceback as _tb
    ERROR_LOG_DIR = os.path.join(os.path.dirname(__file__), "error_logs")
    os.makedirs(ERROR_LOG_DIR, exist_ok=True)
    try:
        for _attempt in range(2):
            try:
                main()
                break
            except (TimeoutError, OSError, ConnectionError) as _e:
                if _attempt == 0:
                    print(f"⚠ 运行失败(超时/网络类, 第1次), 15秒后重试: {_e}")
                    time.sleep(15)
                    continue
                print(f"❌ 重试仍失败(超时/网络类): {_e}")
                raise
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
