#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
草单待确认队列（共享模块） · 芙蕾雅 2026-07-29
被两边共用:
  - MailBots/Draft_Forward_Robot.py  : 扫描发现"没把握"的邮件 → add_pending 领号入队
  - WeComBot/cs_bot/engine.py        : 订舱助手命令 待办 / 确认N / 跳过N

设计要点（规则 v3.1 + 小叽 16:45 评审意见）:
  - pending 即落 .eml（data/pending_eml/N.eml）, 闭环(确认/跳过)即清理 —— 补转发 100% 可靠
  - 全局自增编号 N（sqlite AUTOINCREMENT）, 台账永久留存操作人/时间
  - 权限: 同事只能操作 owner=自己 的单; ADMIN_USERS 全权
  - test=1 的条目: 确认时只转发到验证邮箱、不写 yxo.db、不标已读（TEST 全链路彩排）
  - 提醒: remind_due() 给挂起>1小时的单每小时补提醒; 00:00-08:00 静默, 08:00 后补发
  - 确认只转发、不改库里客编（骁洋拍板: 客编维护走网页人工, 机器人不越权）
"""
import os
import re
import sys
import json
import email
import sqlite3
import imaplib
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from datetime import datetime

# ---------- 路径自适应（方案 B：YXO_ROOT 显式固定，写库永不回退 SMB） ----------
# 写库路径一律使用 YXO_ROOT（默认本地 D:\YXO_DATA），绝不因 config.py 是否存在而「猜」路径。
# 仅 config.py 导入时本地缺失才回退到 SMB 只读共享（只用于读配置，不影响任何写库路径）。
YXO_ROOT = os.environ.get("YXO_ROOT", r"D:\YXO_DATA").rstrip("\\/")

if os.path.exists(os.path.join(YXO_ROOT, "WeComBot", "config.py")):
    ROOT = YXO_ROOT
else:
    _SMB_ROOT = r"\\10.0.199.184\yxo_data"
    ROOT = _SMB_ROOT if os.path.exists(os.path.join(_SMB_ROOT, "WeComBot", "config.py")) else YXO_ROOT

WECOMBOT_DIR = os.path.join(ROOT, "WeComBot")
if WECOMBOT_DIR not in sys.path:
    sys.path.insert(0, WECOMBOT_DIR)
from config import (  # noqa: E402
    SMTP_SERVER, SMTP_PORT, ACCOUNTS,
    DRAFT_WAYBILL_IMAP_FOLDER,
    USER_COMPANIES, ADMIN_USERS,
    sender_for_company,
)

# 写库路径一律走本地 YXO_ROOT（绝不用 SMB 共享写，规避 attempt to write a readonly database）
MAILBOTS_DIR = os.path.join(YXO_ROOT, "MailBots")
DATA_DIR = os.path.join(MAILBOTS_DIR, "data")
EML_DIR = os.path.join(DATA_DIR, "pending_eml")
QUEUE_DB = os.path.join(DATA_DIR, "pending_queue.db")

IMAP_SERVER = "imap.qiye.aliyun.com"
IMAP_PORT = 993
VERIFY_MAILBOX = "3841559246@qq.com"
ADMIN_MAILBOX = "maoxiaoyang@cqtransit.com"
ADMIN_NAME = "毛骁洋"

os.makedirs(EML_DIR, exist_ok=True)

CATEGORY_LABEL = {"A": "转草单", "B": "草单更新", "C1": "反馈问题",
                 "WAY_A": "运单号下发", "WAY_B": "单证驳回"}


def _conn():
    # BUG-B 热修：强制 read-write-create 模式，规避某些环境下连接被继承成只读而写不进 journal
    conn = sqlite3.connect(f"file:{QUEUE_DB}?mode=rwc", uri=True, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")  # 降低并发锁异常
    except Exception:
        pass
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # 单写者下 WAL 更稳，避免 rollback-journal 锁冲突
    except Exception:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS pending_queue(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id  TEXT,
        subject     TEXT,
        sender      TEXT,
        email_date  TEXT,
        category    TEXT,
        code        TEXT,
        code_num    TEXT,
        box         TEXT,
        company     TEXT,
        owner       TEXT,
        reason      TEXT,
        candidates  TEXT,
        boxes_seen  TEXT,
        to_list     TEXT,
        cc_list     TEXT,
        eml_path    TEXT,
        test        INTEGER DEFAULT 1,
        simulated   INTEGER DEFAULT 0,
        status      TEXT DEFAULT 'open',
        created_at  INTEGER,
        operator    TEXT,
        operated_at INTEGER,
        last_remind_at INTEGER
    )""")
    # 2026-08-08 热修：提醒次数上限所需字段（老库自动补列，失败忽略）
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_queue)")}
        if "remind_count" not in cols:
            conn.execute("ALTER TABLE pending_queue ADD COLUMN remind_count INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    return conn


def known_message_ids():
    """队列里所有 message_id（任何状态）→ 机器人扫描时据此去重, 不重复入队。"""
    conn = _conn()
    ids = {r[0] for r in conn.execute(
        "SELECT message_id FROM pending_queue WHERE message_id IS NOT NULL")}
    conn.close()
    return ids


def add_pending(info, raw_bytes, test=True, simulated=False):
    """入队领号并落 .eml。info 需含:
    message_id/subject/sender/date/category/code/num/box/company/owner/
    reason/candidates/boxes_seen/to/cc
    返回编号 N。"""
    now = int(datetime.now().timestamp())
    conn = _conn()
    cur = conn.execute(
        """INSERT INTO pending_queue(
            message_id,subject,sender,email_date,category,code,code_num,box,
            company,owner,reason,candidates,boxes_seen,to_list,cc_list,
            eml_path,test,simulated,status,created_at,last_remind_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
        (info.get("message_id"), info.get("subject"), info.get("sender"),
         info.get("date"), info.get("category"), info.get("code"),
         info.get("num"), info.get("box"), info.get("company"),
         info.get("owner") or ADMIN_NAME, info.get("reason"),
         json.dumps(info.get("candidates") or [], ensure_ascii=False),
         ",".join(info.get("boxes_seen") or []),
         ",".join(info.get("to") or []), ",".join(info.get("cc") or []),
         None, 1 if test else 0, 1 if simulated else 0, now, now))
    n = cur.lastrowid
    eml_path = os.path.join(EML_DIR, f"{n}.eml")
    try:
        if raw_bytes:
            with open(eml_path, "wb") as f:
                f.write(raw_bytes)
            conn.execute("UPDATE pending_queue SET eml_path=? WHERE id=?", (eml_path, n))
    except Exception:
        pass  # 落盘失败不阻塞入队（确认时还能按 Message-ID 回邮箱重取）
    conn.commit()
    conn.close()
    return n


def _row_to_dict(r, cols):
    return {c: r[i] for i, c in enumerate(cols)}


_COLS = ("id,message_id,subject,sender,email_date,category,code,code_num,box,"
         "company,owner,reason,candidates,boxes_seen,to_list,cc_list,eml_path,"
         "test,simulated,status,created_at,operator,operated_at,last_remind_at")


def get(n):
    conn = _conn()
    r = conn.execute(f"SELECT {_COLS} FROM pending_queue WHERE id=?", (n,)).fetchone()
    conn.close()
    return _row_to_dict(r, _COLS.split(",")) if r else None


def list_open(user=None):
    """user=None 或管理员 → 全部; 否则只看 owner=自己。"""
    conn = _conn()
    if user and user not in ADMIN_USERS:
        rows = conn.execute(
            f"SELECT {_COLS} FROM pending_queue WHERE status='open' AND owner=? ORDER BY id",
            (user,)).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COLS} FROM pending_queue WHERE status='open' ORDER BY id").fetchall()
    conn.close()
    return [_row_to_dict(r, _COLS.split(",")) for r in rows]


def _can_operate(user, row):
    return user in ADMIN_USERS or row["owner"] == user


def _decode_any(raw):
    if not raw:
        return ""
    out = ""
    for part, cs in decode_header(raw):
        out += part.decode(cs or "utf-8", errors="replace") if isinstance(part, bytes) else str(part)
    return out


def _build_forward(raw_bytes, category, extra_note=None):
    """从原始邮件构造转发件（正文+全部附件）。与机器人同逻辑。"""
    orig = email.message_from_bytes(raw_bytes)
    subject = _decode_any(orig.get("Subject", ""))
    out = MIMEMultipart("mixed")
    html_body, plain_body, att_parts = None, None, []
    for part in orig.walk():
        if part.is_multipart():
            continue
        ct = part.get_content_type()
        disp = str(part.get("Content-Disposition", "")).lower()
        fn = part.get_filename()
        if "attachment" in disp or fn:
            att_parts.append((part, _decode_any(fn) if fn else "attachment.bin"))
        elif ct == "text/html" and html_body is None:
            p = part.get_payload(decode=True)
            if p:
                html_body = p.decode(part.get_content_charset() or "utf-8", errors="replace")
        elif ct == "text/plain" and plain_body is None:
            p = part.get_payload(decode=True)
            if p:
                plain_body = p.decode(part.get_content_charset() or "utf-8", errors="replace")
    note = "【提示】此为更新版草单，此前版本作废，请以本邮件附件为准。" if category == "B" else ""
    if extra_note:
        note = (note + "\n" + extra_note).strip()
    if html_body:
        if note:
            html_body = f'<p style="color:#c00;font-weight:bold">{note}</p>' + html_body
        out.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        out.attach(MIMEText(((note + "\n\n") if note else "") + (plain_body or ""), "plain", "utf-8"))
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


def _smtp_send(msg, sender_email, sender_pwd, to_list, cc_list=None):
    cc_list = cc_list or []
    msg["From"] = sender_email
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as s:
        s.login(sender_email, sender_pwd)
        s.sendmail(sender_email, list(set(to_list + cc_list)), msg.as_string())


def _load_raw(row):
    """优先读 .eml 存档; 没有则按 Message-ID 回各邮箱重取。"""
    if row["eml_path"] and os.path.isfile(row["eml_path"]):
        with open(row["eml_path"], "rb") as f:
            return f.read()
    for addr in (row["boxes_seen"] or "").split(","):
        pwd = ACCOUNTS.get(addr.strip())
        if not pwd:
            continue
        try:
            m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            m.login(addr.strip(), pwd)
            m.select(DRAFT_WAYBILL_IMAP_FOLDER, readonly=True)
            res, data = m.search(None, "HEADER", "Message-ID", row["message_id"])
            raw = None
            if res == "OK" and data[0]:
                _, md = m.fetch(data[0].split()[0], "(BODY.PEEK[])")
                raw = md[0][1]
            m.logout()
            if raw:
                return raw
        except Exception:
            continue
    return None


def _mark_seen(message_id, boxes_seen):
    done = []
    for addr in (boxes_seen or "").split(","):
        addr = addr.strip()
        pwd = ACCOUNTS.get(addr)
        if not pwd:
            continue
        try:
            m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            m.login(addr, pwd)
            m.select(DRAFT_WAYBILL_IMAP_FOLDER)
            res, data = m.search(None, "HEADER", "Message-ID", message_id)
            if res == "OK" and data[0]:
                for num in data[0].split():
                    m.store(num, "+FLAGS", "\\Seen")
                done.append(addr)
            m.logout()
        except Exception:
            continue
    return done


def _write_db(row):
    """LIVE 确认后写库: 调小叽的 upsert_draft（草单=已收, B 类 allow_refresh）。
    只转发、不改库里客编（骁洋拍板）。"""
    try:
        if MAILBOTS_DIR not in sys.path:
            sys.path.insert(0, MAILBOTS_DIR)
        import db_write
        yxo_db = os.path.join(YXO_ROOT, "yxo_app", "data", "yxo.db")
        conn = sqlite3.connect(yxo_db, timeout=15)
        ok = db_write.upsert_draft(
            conn, row["code"], row["box"], row["company"], row["email_date"],
            dry_run=False, allow_insert=False,
            allow_refresh=(row["category"] == "B"),
            depart_year=(row.get("email_date") or "")[:4] or None)
        conn.commit()
        conn.close()
        return bool(ok), ""
    except Exception as e:
        return False, str(e)


def _diag_log(msg):
    """写库异常诊断：把实际路径/YXO_ROOT/errno 落盘，下次再出 readonly 一眼定位。"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(os.path.join(DATA_DIR, "pending_diag.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


# ==================== BUG-B 根治：单写者 + 文件工单 ====================
# 真因（2026-08-08 实测坐实）：同一个 pending_queue.db，机器人进程写得进（remind_due 能更新
# last_remind_at），企微助手进程写不进（_close 报 readonly）。同文件两种结果 → 不是文件只读、
# 不是杀软锁，而是【两个进程的运行账号不同，企微助手那个账号对 data 目录没有写权限】。
# SQLite 写库必须能在同目录建 -wal/-journal 文件，目录不可写就报 "readonly database"。
#
# 绕过方式（业内叫「单写者原则」）：写不进库的进程不再硬写，改为投递一张"工单"文件；
# 由写得进库的机器人进程定期收单代办。用户点「跳过」立刻有回执，最迟下一轮机器人跑时生效。
_OPS_SUBDIR = "pending_ops"


def _ops_dir_candidates():
    """工单目录候选，按优先级。跨账号共享写首选 ProgramData（Users 组默认可写）。"""
    cands = [os.path.join(DATA_DIR, _OPS_SUBDIR)]
    pd = os.environ.get("ProgramData") or r"C:\ProgramData"
    cands.append(os.path.join(pd, "YXO", _OPS_SUBDIR))
    try:
        import tempfile
        cands.append(os.path.join(tempfile.gettempdir(), "yxo_" + _OPS_SUBDIR))
    except Exception:
        pass
    return cands


def _write_op(payload):
    """投递一张工单。返回落盘路径；全部候选目录都写不了则返回 None。"""
    name = f"{int(datetime.now().timestamp()*1000)}_{os.getpid()}_{payload.get('op','x')}.json"
    for d in _ops_dir_candidates():
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, name)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, p)  # 原子落盘，避免收单方读到半截文件
            return p
        except Exception:
            continue
    return None


def drain_ops():
    """收单：由【写得进库】的机器人进程调用，代为执行工单后删除。返回处理条数。
    自身写库失败则原样保留工单，下轮再试（不丢单）。"""
    done = 0
    for d in _ops_dir_candidates():
        if not os.path.isdir(d):
            continue
        try:
            files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
        except Exception:
            continue
        for fn in files:
            fp = os.path.join(d, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    job = json.load(f)
            except Exception:
                try:
                    os.remove(fp)  # 坏文件直接丢弃，避免卡死队列
                except Exception:
                    pass
                continue
            try:
                if job.get("op") == "close":
                    _close_direct(job["id"], job["status"], job.get("operator") or "")
                elif job.get("op") == "clear_open":
                    _clear_open_direct(job.get("operator") or "维护")
                os.remove(fp)
                done += 1
                _diag_log(f"✅ 收单执行: {job}")
            except Exception as e:
                _diag_log(f"⚠ 收单执行失败(保留待重试) {fn}: {type(e).__name__}: {e}")
    return done


def _clear_open_direct(operator="维护"):
    """把当前所有 open 单一次性关掉（status=cleared），并清理对应 .eml。"""
    conn = _conn()
    rows = conn.execute("SELECT id, eml_path FROM pending_queue WHERE status='open'").fetchall()
    conn.execute(
        "UPDATE pending_queue SET status='cleared', operator=?, operated_at=? WHERE status='open'",
        (operator, int(datetime.now().timestamp())))
    conn.commit()
    conn.close()
    for _id, p in rows:
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except Exception:
                pass
    _diag_log(f"🧹 清空待办队列: 关闭 {len(rows)} 单 (operator={operator})")
    return len(rows)


# 维护信号文件：在任一候选目录（或 data 目录）放一个空文件即可远程触发，执行后自动删除。
# 用途：没有远程 shell 时，靠"扔个文件"当遥控器。
_FLAGS = {"CLEAR_PENDING.flag": "clear_open"}


def apply_maintenance():
    """检查维护信号文件并执行（自毁式：执行后删除信号文件）。返回执行的动作列表。
    防呆：文件名大小写不敏感、允许被 Windows 自动追加 .txt（CLEAR_PENDING.flag.txt 同样识别）。"""
    acted = []
    dirs = [DATA_DIR] + _ops_dir_candidates()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except Exception:
            continue
        for fn in entries:
            low = fn.lower()
            op = next((o for f, o in _FLAGS.items() if low.startswith(f.lower())), None)
            if not op:
                continue
            fp = os.path.join(d, fn)
            if not os.path.isfile(fp):
                continue
            try:
                if op == "clear_open":
                    n = _clear_open_direct("维护(信号文件)")
                    acted.append(f"{op}:{n}")
                os.remove(fp)
            except Exception as e:
                _diag_log(f"⚠ 维护信号 {fn} 执行失败: {type(e).__name__}: {e}")
    return acted


def self_test():
    """BUG-B 热修：进程启动即做写库自检，把"只读"的真因(路径/权限/共享盘)打在日志，
    而不是等用户点「确认」时才在 _close 里冒出一个看不懂的 OperationalError。
    自检失败只记录、不抛异常（让机器人仍能启动；真正写失败会在 _close 原样上浮）。"""
    try:
        abspath = os.path.abspath(QUEUE_DB)
        parent = os.path.dirname(abspath)
        is_net = abspath.startswith("\\\\") or "://" in abspath
        diag = [f"[pending_queue 自检] 路径={abspath}",
                f"  父目录存在={os.path.isdir(parent)} 可写={os.access(parent, os.W_OK)}",
                f"  网络/共享路径={is_net}"]
        if is_net:
            diag.append("  ⚠ 落在网络/共享路径，极可能只读 → 这就是 BUG-B 的高危根因！")
        c = sqlite3.connect(f"file:{QUEUE_DB}?mode=rwc", uri=True, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS _selftest(id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO _selftest(id) VALUES (1)")
        c.execute("DELETE FROM _selftest WHERE id=1")
        c.commit()
        c.close()
        diag.append("  ✅ 写库自检通过")
        _diag_log("\n".join(diag))
    except Exception as e:
        _diag_log("\n".join(diag) + f"\n  ❌ 写库自检失败: errno={getattr(e, 'errno', None)} "
                  f"{type(e).__name__}: {e}")


# BUG-B 热修：模块导入即自检一次（WeComBot/运单号机器人 import 本模块时也会触发）
self_test()


def _close_direct(n, status, operator):
    """真正写库关单 + 清理 .eml。写失败直接抛异常（供收单方判断是否保留工单）。"""
    conn = _conn()
    conn.execute(
        "UPDATE pending_queue SET status=?, operator=?, operated_at=? WHERE id=?",
        (status, operator, int(datetime.now().timestamp()), n))
    conn.commit()
    row = conn.execute("SELECT eml_path FROM pending_queue WHERE id=?", (n,)).fetchone()
    conn.close()
    if row and row[0] and os.path.isfile(row[0]):
        try:
            os.remove(row[0])
        except Exception:
            pass


def _close(n, status, operator):
    """关单（对外入口）。
    BUG-B 根治：本进程写得进就直接写；写不进（企微助手账号无 data 目录写权限）则
    降级投递工单文件，由机器人进程下轮收单代办 —— 用户侧不再看到 readonly 报错。"""
    last_err = None
    for _attempt in range(2):
        try:
            _close_direct(n, status, operator)
            return True  # 立即生效
        except Exception as e:
            last_err = e
            if _attempt == 0:
                import time as _t
                _t.sleep(1)
    # 直写失败 → 投工单降级
    _diag_log(f"⚠ _close 直写失败, 转工单: id={n} status={status} "
              f"QUEUE_DB={QUEUE_DB} | YXO_ROOT={YXO_ROOT} | "
              f"errno={getattr(last_err, 'errno', None)} | {type(last_err).__name__}: {last_err}")
    p = _write_op({"op": "close", "id": n, "status": status, "operator": operator,
                   "at": int(datetime.now().timestamp())})
    if p:
        return False  # 已受理, 待收单生效
    err = (f"❌ _close 写库失败且工单投递也失败: QUEUE_DB={QUEUE_DB} | YXO_ROOT={YXO_ROOT} | "
           f"errno={getattr(last_err, 'errno', None)} | {type(last_err).__name__}: {last_err}")
    _diag_log(err)
    raise RuntimeError(err) from last_err


def _close_note(applied):
    """_close 的回执尾注：True=已落库；False=已投工单，待机器人下轮收单生效。"""
    return "" if applied else "\n📥 队列状态已受理，最迟 15 分钟内自动生效（本进程无写库权限，已转后台代办）。"


def confirm(n, user):
    """确认 N: 按原定路由转发 + (LIVE)写库 + 标已读 + 关单。返回回复文本。"""
    row = get(n)
    if not row:
        return f"❌ 没有找到待确认单 #{n}。回复「待办」查看当前队列。"
    if row["status"] != "open":
        return (f"ℹ #{n} 已由 {row['operator'] or '?'} 处理过了"
                f"（{row['status']}），无需重复操作。")
    if not _can_operate(user, row):
        return f"⛔ #{n} 属于 {row['owner']} 名下（{row['company'] or '未匹配公司'}），你无权操作。"

    # —— 运单号类(WAY_A/WAY_B)：只发企微待确认，不转发邮件、不写 records ——
    if row["category"] in ("WAY_A", "WAY_B"):
        seen = _mark_seen(row["message_id"], row["boxes_seen"])
        applied = _close(n, "confirmed", user)
        note = f"✅ #{n} 运单号已确认处理（不转发邮件，仅标记已读 + 关单）"
        if seen:
            note += f"，已在 {len(seen)} 个邮箱标为已读"
        return note + _close_note(applied)

    raw = _load_raw(row)
    if not raw:
        return (f"❌ #{n} 原件取不到了（存档丢失且邮箱重取失败），"
                f"请人工处理该邮件：{row['subject'][:50]}")

    to_list = [x for x in (row["to_list"] or "").split(",") if x]
    cc_list = [x for x in (row["cc_list"] or "").split(",") if x]
    is_test = bool(row["test"])
    admin_pwd = ACCOUNTS.get(ADMIN_MAILBOX)
    try:
        fwd, subj = _build_forward(raw, row["category"])
        if is_test:
            fwd["Subject"] = f"[测试·确认#{n}→原收件人:{','.join(to_list) or '-'}] {subj}"
            _smtp_send(fwd, ADMIN_MAILBOX, admin_pwd, [VERIFY_MAILBOX])
            sent_to = VERIFY_MAILBOX + "（测试）"
        else:
            if not to_list:
                return (f"⚠ #{n} 没有可用的转发路由（公司={row['company'] or '未匹配'}），"
                        f"请先在网页端把该票公司/路由维护好，再回「确认 {n}」。单子保留。")
            label = "【草单更新】" if row["category"] == "B" else ""
            fwd["Subject"] = f"{label}{subj}" if label else subj
            sen = sender_for_company(row["company"]) if row["company"] else None
            s_email, s_pwd = (sen if (sen and sen[1]) else (ADMIN_MAILBOX, admin_pwd))
            _smtp_send(fwd, s_email, s_pwd, to_list, cc_list)
            sent_to = ",".join(to_list)
    except Exception as e:
        return f"❌ #{n} 转发失败：{e}\n单子保留，可稍后重试「确认 {n}」。"

    notes = [f"✅ #{n} 已确认并转发 → {sent_to}"]
    if not is_test:
        # 只有草单类(A/B)确认时才写库；运单号类(WAY_A/WAY_B)只转发标已读，不碰 records 状态字段
        if row["code"] and row["category"] in ("A", "B"):
            ok, err = _write_db(row)
            notes.append("已写库（草单=已收" + ("，更新版已刷新" if row["category"] == "B" else "") + "）"
                         if ok else f"⚠ 写库未成功：{err or '请在网页端手动更新'}")
        seen = _mark_seen(row["message_id"], row["boxes_seen"])
        if seen:
            notes.append(f"已在 {len(seen)} 个邮箱标为已读")
    applied = _close(n, "confirmed", user)
    return "\n".join(notes) + _close_note(applied)


def skip(n, user):
    """跳过 N: 不转发不写库, 关单留痕。"""
    row = get(n)
    if not row:
        return f"❌ 没有找到待确认单 #{n}。回复「待办」查看当前队列。"
    if row["status"] != "open":
        return f"ℹ #{n} 已由 {row['operator'] or '?'} 处理过了（{row['status']}）。"
    if not _can_operate(user, row):
        return f"⛔ #{n} 属于 {row['owner']} 名下（{row['company'] or '未匹配公司'}），你无权操作。"
    applied = _close(n, "skipped", user)
    return (f"🗑 #{n} 已跳过（不转发、不写库），台账已记录操作人：{user}。"
            + _close_note(applied))


# ==================== 批量 / 筛选操作（2026-07-31 芙蕾雅） ====================

def _open_rows_for(user):
    """当前用户可见的 open 单（管理员=全部, 同事=仅自己）。"""
    conn = _conn()
    if user and user not in ADMIN_USERS:
        rows = conn.execute(
            f"SELECT {_COLS} FROM pending_queue WHERE status='open' AND owner=? ORDER BY id",
            (user,)).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COLS} FROM pending_queue WHERE status='open' ORDER BY id").fetchall()
    conn.close()
    return [_row_to_dict(r, _COLS.split(",")) for r in rows]


def _skip_where(user, pred, label):
    """关闭当前用户可操作且符合 pred 的全部 open 单。"""
    rows = _open_rows_for(user)
    done, blocked, queued = 0, 0, 0
    for row in rows:
        if not pred(row):
            continue
        if not _can_operate(user, row):
            blocked += 1
            continue
        if not _close(row["id"], "skipped", user):
            queued += 1
        done += 1
    if done == 0 and blocked == 0:
        return f"✅ 没有符合「{label}」的挂起单，队列很干净。"
    parts = [f"🗑 已跳过「{label}」{done} 单（不转发、不写库）"]
    if blocked:
        parts.append(f"⛔ {blocked} 单无权限，已忽略")
    parts.append(f"台账已记录操作人：{user}")
    msg = "\n".join(parts)
    if queued:
        msg += _close_note(False)
    return msg


def skip_all(user):
    """全部跳过：关闭自己可见的全部 open 单。"""
    return _skip_where(user, lambda r: True, "全部")


def skip_mine(user):
    """只跳过自己名下的（owner == 自己）。"""
    return _skip_where(user, lambda r: r["owner"] == user, "自己名下")


def skip_others(user):
    """只跳过非自己名下的（owner != 自己）。"""
    return _skip_where(user, lambda r: r["owner"] != user, "他人名下")


def skip_owner(user, owner_name):
    """只跳过指定负责同事名下的全部。"""
    name = (owner_name or "").strip()
    if not name:
        return "❓ 请指定同事姓名，例如：跳过 杨雅雯"
    return _skip_where(user, lambda r: r["owner"] == name, f"{name}名下")


def confirm_all(user, force=False):
    """确认全部：逐条走 confirm 逻辑转发+写库。
    force=False 仅预览数量并请二次确认；force=True 才真正执行。"""
    rows = _open_rows_for(user)
    targets = [r for r in rows if _can_operate(user, r)]
    if not targets:
        return f"✅ 没有你可以确认的挂起单。"
    if not force:
        head = f"⚠ 将确认全部 {len(targets)} 单并逐条转发写库："
        items = "\n".join(
            f"  · #{t['id']}【{CATEGORY_LABEL.get(t['category'], t['category'])}】{(t['subject'] or '')[:36]}"
            for t in targets[:25])
        tail = "\n  …" if len(targets) > 25 else ""
        return (f"{head}\n{items}{tail}\n"
                f"确认无误请回复「确认全部 确定」执行（不可撤销）。")
    ok_n, fail = 0, []
    for t in targets:
        res = confirm(t["id"], user)
        if res.startswith("✅"):
            ok_n += 1
        else:
            fail.append(f"#{t['id']}: {res.splitlines()[0] if res else '未知'}")
    msg = f"✅ 已确认 {ok_n}/{len(targets)} 单"
    if fail:
        msg += "\n以下未成功，可稍后单条重试：\n" + "\n".join(fail[:10])
    return msg


def format_todo(user):
    """「待办」命令输出。"""
    rows = list_open(user)
    if not rows:
        scope = "全部" if user in ADMIN_USERS else "你名下"
        return f"✅ {scope}没有挂起的待确认单，很干净。"
    is_admin = user in ADMIN_USERS
    lines = [f"📋 待确认队列（{'全部 ' if is_admin else ''}{len(rows)} 单）："]
    for r in rows:
        label = CATEGORY_LABEL.get(r["category"], r["category"])
        age_h = (int(datetime.now().timestamp()) - (r["created_at"] or 0)) // 3600
        head = f"#{r['id']}【{label}】{r['subject'][:48]}"
        lines.append(head)
        info = f"    客编:{r['code'] or '-'} 箱号:{r['box'] or '-'}"
        if is_admin:
            info += f" 负责:{r['owner']}"
        if age_h >= 1:
            info += f" ⏱挂起{age_h}h"
        lines.append(info)
        if r["reason"]:
            lines.append(f"    疑点: {r['reason']}")
    lines.append("")
    lines.append("回「确认 编号」转发写库，回「跳过 编号」忽略。")
    lines.append("批量：确认全部(二次确认) / 全部跳过 / 跳过我的 / 跳过别人的 / 跳过 姓名")
    return "\n".join(lines)


def build_pending_notify(n, row_or_info, test=False):
    """入队/提醒时的企微通知文案。"""
    d = row_or_info
    label = CATEGORY_LABEL.get(d.get("category"), d.get("category", ""))
    head = f"【{'测试·' if test else ''}待确认 #{n}】{label}，需人工判断"
    subj = d.get("subject") or ""
    if len(subj) > 200:
        subj = subj[:200] + "…"
    lines = [head, f"主题: {subj}",
             f"客编: {d.get('code') or '-'}  箱号: {d.get('box') or '-'}"]
    if d.get("reason"):
        lines.append(f"疑点: {d['reason']}")
    cands = d.get("candidates") or []
    if isinstance(cands, str):
        try:
            cands = json.loads(cands)
        except Exception:
            cands = []
    for c in cands[:3]:
        lines.append(f"候选: {c}")
    lines.append(f"回「确认 {n}」转发写库，回「跳过 {n}」忽略，回「待办」看全部。")
    if test:
        lines.append("—— 本条为测试预览 ——")
    return "\n".join(lines)


# 单条待确认单最多补提醒几次（超过后停催，避免"跳过没生效→无限刷屏"死循环）。
MAX_REMIND = int(os.environ.get("YXO_MAX_REMIND", "3"))


def remind_due(notify_fn, max_age_hours=1):
    """给挂起超过 max_age_hours 且距上次提醒>=1h 的单补提醒。
    00:00-08:00 静默（不发也不更新 last_remind_at, 08:00 后自然补发）。
    notify_fn(name, text) -> (ok, channel)

    2026-08-08 热修：
      1) 开头先【收单 + 维护信号】——本函数由写得进库的机器人进程调用，是代办工单的最佳时机；
      2) 提醒次数超过 MAX_REMIND 的单不再催，杜绝无限刷屏。"""
    # 收单代办（企微助手写不进库时投递的工单）+ 维护信号（如清空队列）
    try:
        n_ops = drain_ops()
        if n_ops:
            _diag_log(f"📥 本轮收单 {n_ops} 条")
    except Exception as e:
        _diag_log(f"⚠ drain_ops 异常: {type(e).__name__}: {e}")
    try:
        acted = apply_maintenance()
        if acted:
            _diag_log(f"🔧 维护信号已执行: {acted}")
    except Exception as e:
        _diag_log(f"⚠ apply_maintenance 异常: {type(e).__name__}: {e}")

    now = datetime.now()
    if now.hour < 8:
        return 0
    ts = int(now.timestamp())
    sent = 0
    conn = _conn()
    rows = conn.execute(
        f"SELECT {_COLS} FROM pending_queue WHERE status='open' "
        f"AND created_at <= ? AND last_remind_at <= ? "
        f"AND COALESCE(remind_count,0) < ?",
        (ts - max_age_hours * 3600, ts - 3600, MAX_REMIND)).fetchall()
    conn.close()
    for r in rows:
        row = _row_to_dict(r, _COLS.split(","))
        text = "⏰ 提醒：仍有未处理的待确认单\n" + build_pending_notify(
            row["id"], row, test=bool(row["test"]))
        try:
            ok, _ = notify_fn(row["owner"], text)
        except Exception:
            ok = False
        if ok:
            try:
                conn = _conn()
                conn.execute("UPDATE pending_queue SET last_remind_at=?, "
                             "remind_count=COALESCE(remind_count,0)+1 WHERE id=?",
                             (ts, row["id"]))
                conn.commit()
                conn.close()
            except Exception as e:
                _diag_log(f"⚠ 更新提醒计数失败 id={row['id']}: {type(e).__name__}: {e}")
            sent += 1
    return sent


# ==================== 模块引导（放在文件末尾：此时所有函数已定义） ====================
# 收单 + 维护信号不能只挂在草单机器人的 remind_due 上（草单机器人挂了就永远收不到单）。
# 这里让【任何 import 本模块的进程】启动时都尝试一次；写不进库的进程会静默失败并保留工单。
def _boot_maintenance():
    try:
        n = drain_ops()
        if n:
            _diag_log(f"📥 启动收单 {n} 条")
    except Exception:
        pass
    try:
        acted = apply_maintenance()
        if acted:
            _diag_log(f"🔧 启动维护信号已执行: {acted}")
    except Exception:
        pass


if os.environ.get("YXO_SKIP_BOOT_MAINT") != "1":
    _boot_maintenance()
