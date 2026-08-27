import lark_oapi as lark
from lark_oapi.api.bitable.v1 import *
import re
import os
import imaplib
import smtplib
import email
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
from collections import Counter
import sys
import time
sys.path.insert(0, os.path.dirname(__file__))
from dedup_store import is_processed, mark, mark_many, is_seeded, set_seeded
from db_write import get_conn
from common_io import norm_train_no, daemon_loop

sys.path.insert(0, r'D:\YXO_DATA\WeComBot')
from config import (
    sender_for_company,
    FEISHU_APP_ID as APP_ID,
    FEISHU_APP_SECRET as APP_SECRET,
    FEISHU_APP_TOKEN as APP_TOKEN,
)

# ================= 1. 配置部分 =================
# 体检E（2026-08-04）：飞书凭据不再硬编码，统一从 WeComBot/secrets.json 保险库经 config 读取。
# 删明文后请务必去飞书开放平台重置 APP_SECRET（旧值已明文暴露过）。

TABLE_CONFIG = "tbl4wFdo9scMmUM7"  # 运踪配置表
TABLE_LOG = "tblm1skrYicbmNqk"     # 运踪日志表

IMAP_SERVER = "imap.qiye.aliyun.com"
SMTP_SERVER = "smtp.qiye.aliyun.com"
IMAP_PORT = 993
SMTP_PORT = 465

# 凭据外置（2026-08-10 芙蕾雅）：原硬编码明文密码改为从 config_local 读取
from config_local import ACCOUNTS  # 同目录已在 sys.path（见本文件 line15）

SENDER_FILTER = "tracing-system@yxologistics.com"


def split_emails(email_str):
    """同时兼容分号、逗号、空格分割的邮箱"""
    if not email_str or str(email_str).strip().lower() == "none":
        return []
    raw_list = re.split(r'[;,，\s]+', str(email_str))
    return [e.strip() for e in raw_list if "@" in e]

class FeishuBitable:
    def __init__(self):
        self.client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

    def get_all_records(self, table_id):
        all_items = []
        page_token = ""
        while True:
            builder = ListAppTableRecordRequest.builder().app_token(APP_TOKEN).table_id(table_id).page_size(500).automatic_fields(True)
            if page_token: builder.page_token(page_token)
            request = builder.build()
            response = self.client.bitable.v1.app_table_record.list(request)
            if not response.success(): break
            items = response.data.items
            if items: all_items.extend(items)
            if not response.data.has_more: break
            page_token = response.data.page_token
        return all_items

    def add_record(self, table_id, fields):
        request = CreateAppTableRecordRequest.builder() \
            .app_token(APP_TOKEN) \
            .table_id(table_id) \
            .request_body(AppTableRecord.builder().fields(fields).build()) \
            .build()
        response = self.client.bitable.v1.app_table_record.create(request)
        if not response.success():
            # 这一行是关键：如果存不上日志，黑窗口会立刻告诉你为什么！
            print(f"      ❌ 飞书日志保存失败! 原因: {response.msg} (错误码: {response.code})")
        else:
            print(f"      ✅ 飞书日志已同步。")
        return response.success()

    def delete_record(self, table_id, record_id):
        request = DeleteAppTableRecordRequest.builder().app_token(APP_TOKEN).table_id(table_id).record_id(record_id).build()
        self.client.bitable.v1.app_table_record.delete(request)

def forward_email_via_smtp(original_msg, to_list, cc_list, sender_email, sender_password):
    msg = MIMEMultipart()
    msg['Subject'] = original_msg['Subject']
    msg['From'] = sender_email
    msg['To'] = ", ".join(to_list)
    if cc_list: msg['Cc'] = ", ".join(cc_list)
    
    for part in original_msg.walk():
        if part.get_content_maintype() == 'multipart': continue
        if part.get_content_maintype() == 'text':
            msg.attach(part)
        else:
            payload = part.get_payload(decode=True)
            if payload:
                new_part = MIMEBase(part.get_content_maintype(), part.get_content_subtype())
                new_part.set_payload(payload)
                encoders.encode_base64(new_part)
                filename = part.get_filename()
                if filename:
                    new_part.add_header('Content-Disposition', 'attachment', filename=filename)
                msg.attach(new_part)

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(sender_email, sender_password)
        # 发送给所有人（To + Cc）
        all_recipients = list(set(to_list + cc_list)) # 去重防错
        server.sendmail(sender_email, all_recipients, msg.as_string())

# ================= 3. 主逻辑 =================

def run_robot():
    # ── 0. 开关闸门（2026-08-03 芙蕾雅：live=false 则本轮回退）──
    try:
        from bot_config import load_bot_config
        _cfg = load_bot_config("tracing")
        if not _cfg["live"]:
            print("⏸ 运踪机器人已停用(live=false)，本轮回退（不扫描/不转发）。")
            return 0
    except Exception as _e:
        print(f"  ⚠ 读取开关配置失败(按运行处理): {_e}")
    fs = FeishuBitable()
    print(f"🚀 机器人启动: {datetime.now().strftime('%H:%M:%S')}")
    forwarded = 0

    # A. 预加载路由（P4②：改读本地 yxo.db bot_config，不再读飞书 TABLE_CONFIG）
    default_map, train_companies = load_tracing_routing()
    print(f"  📋 本地路由: {len(default_map)} 个公司 DEFAULT, {len(train_companies)} 个班列映射")

    # B. 去重指纹改读本地 dedup_store（P3 起唯一数据源）；首次把飞书 90 天历史迁移进 yxo.db tracing_log
    try:
        if seed_tracking_history(fs, TABLE_LOG):
            print("  ✅ 运踪历史已迁移至 yxo.db tracing_log")
    except Exception as e:
        print(f"  ⚠ 运踪历史迁移失败(下次启动重试): {e}")

    # B2. 体检D：首次把现有 tracing_log 历史播种进 tracing_snapshot（仅时间线，节点/状态为NULL；
    #     真正的节点/状态从今往后由实时邮件正文解析补上）。幂等，仅执行一次。
    try:
        if seed_tracking_snapshot():
            print("  ✅ 运踪历史已播种至 tracing_snapshot")
    except Exception as e:
        print(f"  ⚠ 运踪历史播种失败(下次启动重试): {e}")
    
    # C. 扫描多账户
    for email_addr, password in ACCOUNTS.items():
        print(f"🔍 扫描: {email_addr}")
        try:
            mail_conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=60)
            mail_conn.login(email_addr, password)
            res, _ = mail_conn.select("Tracing")
            if res != 'OK': continue

            res, data = mail_conn.search(None, 'UNSEEN')
            mail_ids = data[0].split()
            
            for m_id in mail_ids:
                _, msg_data = mail_conn.fetch(m_id, '(RFC822)')
                msg = email.message_from_bytes(msg_data[0][1])
                sender = email.utils.parseaddr(msg['From'])[1]
                if sender.lower() != SENDER_FILTER.lower(): continue
                
                msg_id = str(msg['Message-ID']).strip()
                if is_processed('tracking', msg_id):
                    mail_conn.store(m_id, '+FLAGS', '\\Seen')
                    continue
                
                # 标题解析
                subject_raw = msg['Subject']
                decoded, encoding = email.header.decode_header(subject_raw)[0]
                if isinstance(decoded, bytes): decoded = decoded.decode(encoding or 'utf-8')
                
                match = re.search(r'train\s+(\d+)', decoded, re.IGNORECASE)
                if not match: continue
                train_id = match.group(1)

                companies = resolve_train_companies(train_id, train_companies)
                if not companies:
                    print(f"   ⚠️ 班列 [{train_id}] 无路由（bot_config 与 records 均无公司），跳过。")
                    continue
                print(f"   🎯 匹配到班列 [{train_id}]，准备分发...")
                for comp in companies:
                    # ── 运踪退舱跳过（2026-08-03）：该公司本班列箱号全部退舱则不发 ──
                    if tracing_company_all_cancelled(train_id, comp):
                        print(f"      ⏭ 班列 [{train_id}] 公司 [{comp}] 本班列箱号全部为退舱，跳过运踪转发")
                        continue
                    info = default_map.get(comp)
                    if not info or not info['to']:
                        print(f"      ⚠ 公司 {comp} 无 DEFAULT 路由，跳过")
                        continue
                    cfg = {'to': info['to'], 'cc': info['cc'], 'tag': comp}
                    # 发件人：按接收公司负责人发信（与 ATB/DSK 一致）
                    sen = sender_for_company(cfg['tag'])
                    s_email, s_pwd = (sen if (sen and sen[1]) else (email_addr, password))
                    try:
                        # 1. 执行发送
                        forward_email_via_smtp(msg, cfg['to'], cfg['cc'], s_email, s_pwd)
                        print(f"      ✅ 转发成功 -> {cfg['tag']} | 由:{s_email}")

                        # 2. 写入本地 yxo.db tracing_log（P4①：已摘除飞书 TABLE_LOG 写入，本地即为唯一落点）
                        now = datetime.now()
                        log_id_val = f"{train_id}_{cfg['tag']}_{now.strftime('%H%M%S')}"
                        fwd_detail = f"发至:{','.join(cfg['to'])} | 由:{s_email}"
                        write_tracing_log(log_id_val, train_id, cfg['tag'], msg_id, fwd_detail, now.strftime("%Y-%m-%d %H:%M"), _msg_text(msg))
                        forwarded += 1
                    except Exception as e_send:
                        print(f"      ❌ 发送失败 -> {cfg['tag']}: {e_send}")

                # 全部发完后标为已读
                mail_conn.store(m_id, '+FLAGS', '\\Seen')
                mark('tracking', msg_id)
            mail_conn.close()
            mail_conn.logout()
        except Exception as e: print(f"   ❌ 异常: {e}")
    return forwarded

# ================= 2.5 运踪快照解析（体检D · 2026-08-04） =================
# best-effort 从邮件正文提取 箱号/节点/状态，纯辅助、绝不抛异常影响主流程。
_NODE_KW = [
    ("阿拉山口", "阿拉山口"), ("多斯特克", "多斯特克"), ("霍尔果斯", "霍尔果斯"),
    ("马拉舍维奇", "马拉舍维奇"), ("马拉", "马拉"), ("杜伊斯堡", "杜伊斯堡"), ("汉堡", "汉堡"),
    ("布列斯特", "布列斯特"), ("二连", "二连浩特"), ("满洲里", "满洲里"), ("西安", "西安"),
    ("重庆", "重庆"), ("成都", "成都"), ("郑州", "郑州"), ("武汉", "武汉"), ("长沙", "长沙"),
    ("苏州", "苏州"), ("合肥", "合肥"), ("济南", "济南"), ("天津", "天津"), ("广州", "广州"),
    ("深圳", "深圳"), ("赣州", "赣州"), ("金华南", "金华南"), ("莫斯科", "莫斯科"),
    ("叶卡捷琳堡", "叶卡捷琳堡"), ("明斯克", "明斯克"), ("列日", "列日"), ("蒂尔堡", "蒂尔堡"),
    ("Alashankou", "阿拉山口"), ("Dostyk", "多斯特克"), ("Khorgos", "霍尔果斯"),
    ("Malaszewicze", "马拉舍维奇"), ("Mala", "马拉舍维奇"), ("Duisburg", "杜伊斯堡"),
    ("Hamburg", "汉堡"), ("Brest", "布列斯特"), ("Moscow", "莫斯科"),
]
_STATUS_KW = [
    ("到达", "到达"), ("抵达", "到达"), ("到港", "到达"), ("发车", "发车"), ("发出", "发出"),
    ("离港", "离港"), ("启运", "发车"), ("换装", "换装"), ("换轨", "换装"), ("通关", "通关"),
    ("查验", "查验"), ("提箱", "提箱"), ("还箱", "还箱"), ("进站", "进站"), ("出站", "出站"),
    ("arrived", "到达"), ("arrival", "到达"), ("departed", "发车"), ("departure", "发车"),
    ("transshipment", "换装"), ("clearance", "通关"),
]
_BOX_RE = re.compile(r"[A-Za-z]{4}\d{7}")

def _msg_text(msg):
    """提取邮件 标题+纯文本正文，供快照解析。失败返回空串。"""
    try:
        parts = []
        subj = msg.get("Subject")
        if subj:
            parts.append(str(subj))
        for p in msg.walk():
            if p.get_content_type() == "text/plain":
                try:
                    parts.append(p.get_payload(decode=True).decode("utf-8", "replace"))
                except Exception:
                    pass
        return "\n".join(parts)
    except Exception:
        return ""

def _extract_tracing(text):
    """从邮件文本 best-effort 提取 (box_no, node, status)；解析失败返回 (None,None,None)。"""
    if not text:
        return (None, None, None)
    try:
        low = text.lower()
        box = _BOX_RE.search(text)
        box_no = box.group(0).upper() if box else None
        node = next((zh for kw, zh in _NODE_KW if kw.lower() in low), None)
        status = next((zh for kw, zh in _STATUS_KW if kw.lower() in low), None)
        return (box_no, node, status)
    except Exception:
        return (None, None, None)

def append_snapshot(conn, train_no, msg_id, date, raw_text):
    """运踪快照（建议1）：每次运踪事件追加一行，幂等 (train_key, event_time, source)。
    纯增量，不影响 tracing_log 写入。解析拿不到的字段存 NULL。"""
    try:
        box_no, node, status = _extract_tracing(raw_text)
        tk = norm_train_no(train_no)
        src = "email:" + str(msg_id)
        conn.execute(
            """INSERT INTO tracing_snapshot(train_key, box_no, node, status, event_time, source)
               SELECT ?,?,?,?,?,?
               WHERE NOT EXISTS (
                   SELECT 1 FROM tracing_snapshot
                   WHERE train_key=? AND event_time=? AND source=?)""",
            (tk, box_no, node, status, date, src, tk, date, src))
    except Exception as e:
        print(f"      ⚠ 运踪快照写入失败(不影响主流程): {e}")


def write_tracing_log(log_id, train_no, company, msg_id, detail, date, raw_text=None):
    """双写运踪转发日志到本地 yxo.db（与飞书并行，P4 摘飞书写入后即为唯一落点）。
    train_key 用 norm_train_no 归一（'491'->'WB491'），使运踪能按班列号 JOIN records。"""
    try:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO tracing_log(log_id, train_no, company, mail_msg_id, forward_detail, log_date, train_key) "
            "VALUES(?,?,?,?,?,?,?)",
            (log_id, train_no, company, msg_id, detail, date, norm_train_no(train_no))
        )
        # 体检D：追加运踪快照（纯增量，幂等；解析失败/表缺失都不影响主流程）
        try:
            append_snapshot(conn, train_no, msg_id, date, raw_text)
        except Exception as e_snap:
            print(f"      ⚠ 运踪快照写入失败(不影响主流程): {e_snap}")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"      ⚠ 本地运踪日志写入失败: {e}")


def tracing_company_all_cancelled(train_id, company):
    """运踪退舱跳过（2026-08-03，按『公司×班列』判定，贴合扇形转发模型）：
    若某公司在本班列的所有未删箱号均为『退舱』，则返回 True（不对该公司发运踪）。
    - 邮件标题 train_id 为纯数字(如 802)，库内班列号为 WB+3位(如 WB802)，直接拼 'WB'+train_id 对齐。
    - 库内无该公司本班列箱号（配置扇形收件人但无对应订舱）→ 返回 False（保守，保留已配置扇形）。
    - 查询失败 → 返回 False（宁可发，不因数库异常漏发）。"""
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT 状态 FROM records WHERE COALESCE(is_deleted,0)=0 "
            "AND 班列号=? AND 开票子公司名称=?",
            ("WB" + str(train_id), company),
        ).fetchall()
        conn.close()
        if not rows:
            return False
        return all((r[0] or "") == "退舱" for r in rows)
    except Exception as e:
        print(f"  ⚠ 运踪退舱判定失败(班列 {train_id} 公司 {company}): {e}")
        return False


def load_tracing_routing():
    """P4②：从本地 yxo.db bot_config 读取运踪路由，替代飞书 TABLE_CONFIG。
    返回 (default_map, train_companies):
      default_map:     {公司: {'to':[...], 'cc':[...]}}  来自 scope='company'
      train_companies: {班列号: [公司,...]}              来自 scope='train'（扇形转发，多公司）
    """
    import json as _json
    conn = get_conn()
    cur = conn.cursor()
    default_map = {}
    for scope, to_a, cc_a in cur.execute(
        "SELECT key, to_addrs, cc_addrs FROM bot_config WHERE bot='tracking' AND scope='company'"):
        default_map[scope] = {'to': _json.loads(to_a or '[]'), 'cc': _json.loads(cc_a or '[]')}
    train_companies = {}
    for k, extra in cur.execute(
        "SELECT key, extra FROM bot_config WHERE bot='tracking' AND scope='train'"):
        comps = (_json.loads(extra or '{}').get('companies') or [])
        if comps:
            train_companies[k] = comps
    conn.close()
    return default_map, train_companies


def resolve_train_companies(train_id, train_companies=None):
    """运踪 Layer1：这封邮件归哪些公司。
    优先 bot_config 覆盖层；否则从主数据 records(班列号->开票子公司名称) 推导。
    train_id 形如 '793'（来自主题中 "train" 后接数字的模式，如 YXO-2026-793）。
    """
    if train_companies is None:
        _, train_companies = load_tracing_routing()
    override = train_companies.get(train_id)
    if override:
        return list(override)
    comps, seen = [], set()
    try:
        conn = get_conn()
        try:
            for pat in (f"WB{train_id}", train_id):
                rows = conn.execute(
                    "SELECT DISTINCT 开票子公司名称 FROM records "
                    "WHERE (班列号=? OR 班列号 LIKE ?) AND (is_deleted IS NULL OR is_deleted=0)",
                    (pat, f"%{pat}%"),
                ).fetchall()
                for (name,) in rows:
                    if name and name not in seen:
                        seen.add(name)
                        comps.append(name)
        finally:
            conn.close()
    except Exception as e:
        print(f"  ⚠ [resolve] records 查询异常(班列 {train_id}): {e}")
    return comps


def seed_tracking_history(fs, table_id):
    """首次运行：把飞书 TABLE_LOG 现有记录(90天内)一次性灌入 yxo.db tracing_log，
    并 seed 去重指纹到本地 dedup_store(source='tracking')。幂等，仅执行一次。"""
    if is_seeded("tracking_log"):
        return False
    try:
        rows = fs.get_all_records(table_id)
    except Exception as e:
        print(f"  ⚠ 读取飞书运踪日志失败(下次重试): {e}")
        return False
    if not rows:
        # 飞书返回 0 条视为读取失败，不置标记，下次重试（防历史指纹永久丢失）
        return False
    mids = []
    conn = get_conn()
    try:
        for r in rows:
            f = r.fields
            mid = str(f.get('邮件唯一标识') or '').strip()
            if not mid:
                continue
            mids.append(mid)
            conn.execute(
                "INSERT OR IGNORE INTO tracing_log(log_id, train_no, company, mail_msg_id, forward_detail, log_date, train_key) "
                "VALUES(?,?,?,?,?,?,?)",
                (str(f.get('记录ID') or ''), str(f.get('班列号') or ''),
                 str(f.get('接收公司') or ''), mid,
                 str(f.get('转发详情') or ''), str(f.get('日期') or ''),
                 norm_train_no(str(f.get('班列号') or '')))
            )
        conn.commit()
    finally:
        conn.close()
    if mids:
        mark_many('tracking', mids)
    set_seeded("tracking_log")
    print(f"  🌱 运踪历史迁移完成：{len(mids)} 条 → yxo.db tracing_log（飞书 90 天内）")
    return True


def seed_tracking_snapshot():
    """体检D：首次把现有 tracing_log 历史播种进 tracing_snapshot（仅时间线，节点/状态为 NULL）。
    真正的节点/状态从今往后由实时邮件正文解析补上。幂等，仅执行一次。"""
    if is_seeded("tracing_snapshot"):
        return False
    conn = get_conn()
    try:
        # 同一封邮件按扇形转发多家 → tracing_log 多条；按 (mail_msg_id) 去重成一条快照
        rows = conn.execute(
            "SELECT DISTINCT train_key, log_date, mail_msg_id FROM tracing_log "
            "WHERE IFNULL(train_key,'')<>'' AND IFNULL(log_date,'')<>''"
        ).fetchall()
        n = 0
        for train_key, log_date, mid in rows:
            src = "email:" + str(mid)
            conn.execute(
                """INSERT INTO tracing_snapshot(train_key, box_no, node, status, event_time, source)
                   SELECT ?,NULL,NULL,NULL,?,?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM tracing_snapshot
                       WHERE train_key=? AND event_time=? AND source=?)""",
                (train_key, log_date, src, train_key, log_date, src))
            n += 1
        conn.commit()
    finally:
        conn.close()
    set_seeded("tracing_snapshot")
    print(f"  🌱 运踪快照播种完成：{n} 条 → yxo.db tracing_snapshot")
    return True


if __name__ == "__main__":
    import traceback as _tb
    ERROR_LOG_DIR = os.path.join(os.path.dirname(__file__), "error_logs")
    os.makedirs(ERROR_LOG_DIR, exist_ok=True)
    try:
        daemon_loop("tracing", run_robot)

        # 0点汇总 (0-1点之间运行会触发) — P4：改读本地 tracing_log，不再读飞书 TABLE_LOG
        if datetime.now().hour == 0:
            print("⏰ 正在整理昨日转发汇报...")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            conn = get_conn()
            rows = conn.execute(
                "SELECT train_no FROM tracing_log WHERE log_date LIKE ?", (yesterday + '%',)
            ).fetchall()
            conn.close()
            todays = [r[0] for r in rows]
            if todays:
                log_dir = "TracingLog"
                if not os.path.exists(log_dir): os.makedirs(log_dir)
                counts = Counter(todays)
                with open(os.path.join(log_dir, f"{yesterday}.txt"), "w", encoding="utf-8") as f:
                    f.write(f"📅 运踪日报 [{yesterday}]\n总计转发: {len(todays)} 封\n" + "-"*30 + "\n")
                    for tid, n in counts.items(): f.write(f"班列 {tid}: {n} 封\n")
                print("✅ 汇报已生成。")

        # 9点清理已移除（P4②：飞书 TABLE_LOG / TABLE_CONFIG 已退役；本地 tracing_log 永久保留，无需清理）
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
