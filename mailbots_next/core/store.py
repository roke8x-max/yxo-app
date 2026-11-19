import importlib.util
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from ..config import YXO_DB_PATH, BOT_CONFIG_DB_PATH, RESPONSIBLE_COMPANY_COL, COMPANY_ALIAS
from .. import config
from .log import get_logger

_log = get_logger(__name__)
_yxo_lock = threading.Lock()
_bot_config_lock = threading.Lock()


# ==================== 负责人映射的单一真相源（任务 21）====================
# 口径真相源 = `D:\YXO_DATA\WeComBot\config.py` 的 USER_COMPANIES / USER_EMAILS。
# 该文件是历史形成的配置中心（不在任何 git 仓库），旧系统 5 个机器人与
# WeComBot(5001) 都是 sys.path 指过去 `from config import`。
#
# 本模块**不再自带**一份会覆盖数据库的写死口径：旧版 seed 用无条件
# INSERT OR REPLACE 把内置字典刷进 bot_config，导致运维在库里手改负责人必被
# 覆盖、且每次 import（影子任务每 30 分钟一次）都重置回旧人。现在改为每次启动
# 跟随真相源，内置字典降级为 fallback。
#
# 路径探测：本文件在 <代码根>/mailbots_next/core/ ⇒ parents[2] 即代码根
# （生产 D:\YXO_DATA\yxo_app），其父目录（D:\YXO_DATA）下的 WeComBot\config.py。
# 硬契约（洋 2026-11-17 拍板）：
#   ① 暴露模块常量 WECOMBOT_CONFIG_PATH（pathlib.Path）；
#   ② 同名环境变量 WECOMBOT_CONFIG_PATH 在 import 期覆盖默认值；
#   ③ seed_owner_mapping() 只读该常量、绝不重新探测 —— 测试因此可以
#      「改常量后显式重调 seed_owner_mapping()」或「先设 env 再 importlib.reload(store)」。
def _detect_wecombot_config_path() -> Path:
    return Path(__file__).resolve().parents[2].parent / "WeComBot" / "config.py"


_ENV_WECOMBOT_CONFIG_PATH = os.environ.get("WECOMBOT_CONFIG_PATH")
WECOMBOT_CONFIG_PATH = (
    Path(_ENV_WECOMBOT_CONFIG_PATH)
    if _ENV_WECOMBOT_CONFIG_PATH
    else _detect_wecombot_config_path()
)

# 内置 fallback（2026-11-17 业务口径）：**仅**在真相源不可用时兜底。
# ⚠️ 这不是真相源；改负责人请改 D:\YXO_DATA\WeComBot\config.py 的 USER_COMPANIES。
# 若这里不同步，真相源不可用时系统会退回旧人 —— 故每次人事变更要连同此处一起看。
_FALLBACK_OWNER_EMAILS = {
    "太平洋": "maoxiaoyang@cqtransit.com",
    "港九港铁": "maoxiaoyang@cqtransit.com",
    "东盟": "yangyawen@cqtransit.com",
    "同程配": "yangyawen@cqtransit.com",
    "中欧木业": "yangyawen@cqtransit.com",   # 2026-11-17 韩文豪 → 杨雅雯
    "沙坪坝": "fengqian@cqtransit.com",      # 2026-11-17 韩文豪 → 冯茜
    "保时达": "fengqian@cqtransit.com",
    "联运": "fengqian@cqtransit.com",
}


def _is_unc_path(path) -> bool:
    s = str(path)
    return s.startswith("\\\\") or s.startswith("//")


def get_yxo_connection(readonly: bool = True) -> sqlite3.Connection:
    uri = f"file:{YXO_DB_PATH}?mode={'ro' if readonly else 'rw'}"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not readonly:
        if _is_unc_path(YXO_DB_PATH):
            _log.warning(
                f"YXO_DB_PATH is UNC ({YXO_DB_PATH}) — WAL 不兼容网络盘，"
                f"建议改用本地路径 D:\\YXO_DATA\\yxo_app\\data\\yxo.db；"
                f"本次回退为 DELETE 模式，写库可能失败。"
            )
        else:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception as e:
                _log.warning(f"WAL pragma failed, fallback to DELETE: {e}")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_bot_config_connection() -> sqlite3.Connection:
    Path(BOT_CONFIG_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(BOT_CONFIG_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_bot_config_db():
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    to_addrs TEXT DEFAULT '[]',
                    cc_addrs TEXT DEFAULT '[]',
                    extra TEXT DEFAULT '{}',
                    updated_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(bot, scope, key)
                );
                CREATE INDEX IF NOT EXISTS idx_bot_config_lookup ON bot_config(bot, scope, key);
                """
            )
            conn.commit()
        finally:
            conn.close()


def load_records(active_only: bool = True) -> List[Dict[str, Any]]:
    with _yxo_lock:
        conn = get_yxo_connection(readonly=True)
        try:
            where = ""
            if active_only:
                where = "WHERE 状态 <> '退舱' AND COALESCE(is_deleted, 0) = 0"
            rows = conn.execute(
                f"""
                SELECT id, 客户编码, 箱号, 班列号, 目的站, 开票子公司名称, 本地货源公司, 状态, is_deleted, dsk, ATB, 班列类型
                FROM records
                {where}
                """
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                company = d.get(RESPONSIBLE_COMPANY_COL, "")
                d["company"] = COMPANY_ALIAS.get(company, company)
                result.append(d)
            return result
        finally:
            conn.close()


def get_responsible_person(company: str) -> Optional[str]:
    if not company:
        return None
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            cursor = conn.execute(
                "SELECT extra FROM bot_config WHERE scope='owner' AND bot='all' AND key=?",
                (company,),
            )
            row = cursor.fetchone()
            if row and row["extra"]:
                import json
                extra = json.loads(row["extra"])
                return extra.get("email")
            return None
        finally:
            conn.close()


def get_recipients(company: str, scope: str = "company") -> Tuple[List[str], List[str]]:
    if not company:
        return [], []
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            import json
            cursor = conn.execute(
                "SELECT to_addrs, cc_addrs FROM bot_config WHERE bot='all' AND scope=? AND key=?",
                (scope, company),
            )
            row = cursor.fetchone()
            if row:
                return json.loads(row["to_addrs"] or "[]"), json.loads(row["cc_addrs"] or "[]")
            return [], []
        finally:
            conn.close()


def get_train_companies(train_no: str) -> List[str]:
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            import json
            cursor = conn.execute(
                "SELECT extra FROM bot_config WHERE bot='tracking' AND scope='train' AND key=?",
                (train_no,),
            )
            row = cursor.fetchone()
            if row and row["extra"]:
                extra = json.loads(row["extra"])
                return extra.get("companies", [])
            return []
        finally:
            conn.close()


def get_company_default_recipients(company: str) -> Tuple[List[str], List[str]]:
    return get_recipients(company, "company")


def get_owner_email(company: str) -> Optional[str]:
    return get_responsible_person(company)


def write_dsk_timestamp(box_no: str, field: str, timestamp: str) -> bool:
    # Defense in depth: business-DB writes only happen live, even if a
    # caller forgets the act-layer gate.
    if not config.is_live():
        return False
    with _yxo_lock:
        conn = get_yxo_connection(readonly=False)
        try:
            cursor = conn.execute(
                f"UPDATE records SET {field}=? WHERE 箱号=? AND 状态 <> '退舱' AND COALESCE(is_deleted, 0) = 0",
                (timestamp, box_no),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def write_tracing_log(
    log_id: str,
    train_no: str,
    company: str,
    mail_msg_id: str,
    forward_detail: str,
    log_date: str,
    train_key: str,
) -> bool:
    if not config.is_live():
        return False
    with _yxo_lock:
        conn = get_yxo_connection(readonly=False)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO tracing_log
                   (log_id, train_no, company, mail_msg_id, forward_detail, log_date, train_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (log_id, train_no, company, mail_msg_id, forward_detail, log_date, train_key),
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()


def write_tracing_snapshot(
    train_key: str,
    box_no: Optional[str],
    node: Optional[str],
    status: Optional[str],
    event_time: str,
    source: str,
) -> bool:
    if not config.is_live():
        return False
    with _yxo_lock:
        conn = get_yxo_connection(readonly=False)
        try:
            conn.execute(
                """INSERT INTO tracing_snapshot(train_key, box_no, node, status, event_time, source)
                   SELECT ?,?,?,?,?,?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM tracing_snapshot
                       WHERE train_key=? AND event_time=? AND source=?)""",
                (train_key, box_no, node, status, event_time, source, train_key, event_time, source),
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()


def _load_owner_mapping_from_wecombot(path: Path) -> Dict[str, str]:
    """从 WeComBot config.py 反向生成 `{公司关键词: 邮箱}`。失败一律抛给调用方。

    该文件是 .py 不是 JSON，故用 importlib 按路径加载。注意它在 import 期就会读同目录
    `secrets.json`，缺失/损坏直接 `RuntimeError` —— 这就是调用方必须 try/except 的原因
    （不加的话整个服务 import 不起来）。键缺失、映射为空同理抛出。
    """
    spec = importlib.util.spec_from_file_location("_wecombot_config_owner_source", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 构造 module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # ← secrets.json 缺失在这里 RuntimeError
    user_companies = getattr(module, "USER_COMPANIES")
    user_emails = getattr(module, "USER_EMAILS")
    mapping: Dict[str, str] = {}
    for real_name, keywords in dict(user_companies).items():
        for kw in keywords or []:
            email = dict(user_emails).get(real_name)
            if not email:
                raise KeyError(f"USER_EMAILS 缺「{real_name}」的邮箱，无法归属关键词「{kw}」")
            mapping[str(kw)] = str(email)
    if not mapping:
        raise ValueError("USER_COMPANIES 未生成任何 公司→邮箱 映射")
    return mapping


def _owner_mapping() -> Dict[str, str]:
    """真相源优先、内置 fallback 兜底。**永不抛异常**（服务必须起得来）。"""
    path = WECOMBOT_CONFIG_PATH          # 只读模块常量，不重新探测（硬契约）
    try:
        return _load_owner_mapping_from_wecombot(Path(path))
    except Exception as e:
        _log.warning(
            f"WeComBot config 不可用（{path}）→ 负责人映射回退内置 fallback："
            f"{type(e).__name__}: {e}"
        )
        return dict(_FALLBACK_OWNER_EMAILS)


def seed_owner_mapping():
    """把负责人映射写进 `bot_config`（bot='all', scope='owner'）。

    来源 = WeComBot/config.py（单一真相源，任务 21）；内置字典只在真相源不可用时兜底。
    保留 INSERT OR REPLACE 的语义 = 「每次启动跟随真相源」，改配置中心一处即全系统生效。
    （上游若改库手改，下一次启动仍会被真相源覆盖 —— 这正是设计意图，别在库里改。）

    口径差异（既有行为，本次不改，仅标注）：USER_COMPANIES 的关键词在上游是**子串**
    语义（`kw in company`，见 WeComBot company_to_name），而 get_responsible_person()
    是 SQL `key=?` **精确**匹配；本函数写入的 key 就是关键词原文，故只有公司名与关键词
    完全相同时才命中。
    """
    mapping = _owner_mapping()
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            import json
            for company, email in mapping.items():
                conn.execute(
                    """INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra, updated_at)
                       VALUES ('all', 'owner', ?, '[]', '[]', ?, datetime('now'))""",
                    (company, json.dumps({"email": email})),
                )
            conn.commit()
        finally:
            conn.close()


def check_company_recipients_configured() -> bool:
    """启动自检（只读）：company 收件人行数为 0 → ERROR（启动期绝不写 company 行）。

    为空意味着 get_recipients() 永远返回空 → 所有邮件判 no_route。
    """
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM bot_config WHERE bot='all' AND scope='company'"
            ).fetchone()
            n = row["n"] if row else 0
        finally:
            conn.close()
    if n == 0:
        _log.error("No company recipients configured — 所有邮件将判 no_route。"
                   "请跑 scripts/seed_company_recipients.py --apply")
        return False
    return True


init_bot_config_db()
seed_owner_mapping()


def init_forward_log():
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS forward_log (
                    forward_id TEXT PRIMARY KEY,
                    message_id TEXT, row_key TEXT,
                    account TEXT, folder TEXT, uid INTEGER,
                    subject TEXT, sender TEXT, date_hdr TEXT,
                    raw_hex TEXT, to_list TEXT, cc_list TEXT,
                    sent_by TEXT DEFAULT '',
                    sent_at TEXT, bounced INTEGER DEFAULT 0)"""
            )
            conn.commit()
            try:
                conn.execute("ALTER TABLE forward_log ADD COLUMN sent_by TEXT DEFAULT ''")
                conn.commit()
            except Exception:
                pass  # 列已存在（新库建表时自带）
            # 发送后自检（任务 08）新增三列。用 PRAGMA 逐列判断而不是「三条 ALTER
            # 包一个 try 靠事务回滚」：Python sqlite3 默认不为 DDL 开事务，
            # 中途失败时 rollback() 是空操作（实测），失败后第 1 条 ALTER 会在下次
            # 启动时直接抛、整段被跳过，第 2/3 列永远补不上。
            # 这是启动期代码（本函数在模块底部被调用），不许抛异常。
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(forward_log)")}
                # 任务 18：日报改读新表后要的 5 列。sent_local 是**本地**时间
                # （日报按它分天/取时分）；sent_at 保持 UTC 语义不动（send_check 与
                # 下面的 SKIP_LEGACY 迁移都按 UTC 比）。迁移**不回填**历史行：
                # sent_local 的定义是「写入时刻的本地时间」，+8h 推算是伪造观测值。
                for _col, _ddl in (
                    ("company", "ALTER TABLE forward_log ADD COLUMN company TEXT DEFAULT ''"),
                    ("checked_at", "ALTER TABLE forward_log ADD COLUMN checked_at TEXT DEFAULT ''"),
                    ("check_result", "ALTER TABLE forward_log ADD COLUMN check_result TEXT DEFAULT ''"),
                    ("robot", "ALTER TABLE forward_log ADD COLUMN robot TEXT DEFAULT ''"),
                    ("sent_local", "ALTER TABLE forward_log ADD COLUMN sent_local TEXT DEFAULT ''"),
                    ("code", "ALTER TABLE forward_log ADD COLUMN code TEXT DEFAULT ''"),
                    ("box", "ALTER TABLE forward_log ADD COLUMN box TEXT DEFAULT ''"),
                    ("train_no", "ALTER TABLE forward_log ADD COLUMN train_no TEXT DEFAULT ''"),
                    # 任务 22-E：补发占位标记。老行 '' = 没补发过（不回填历史值）。
                    ("resent_at", "ALTER TABLE forward_log ADD COLUMN resent_at TEXT DEFAULT ''"),
                ):
                    if _col not in cols:
                        conn.execute(_ddl)
                conn.commit()
                # 日报按 sent_local 前缀分天，给它一条索引（幂等，独立于上面是否加过列）。
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fl_sent_local ON forward_log(sent_local)"
                )
                conn.commit()
                # 独立、幂等、可自愈：只标「一天前就存在、且从未核对过」的行。
                # 不依赖「本次是否加过列」，所以上一次启动半途失败也能在这一次补标。
                conn.execute(
                    "UPDATE forward_log SET checked_at=datetime('now'), "
                    "check_result='SKIP_LEGACY' "
                    "WHERE checked_at='' AND check_result='' "
                    "  AND sent_at < datetime('now','-1 day')"
                )
                conn.commit()
            except Exception as _e:
                conn.rollback()
                _log.warning(
                    f"forward_log 迁移未完成（不影响启动，下次启动会重试）: "
                    f"{type(_e).__name__}: {_e}"
                )
        finally:
            conn.close()


def write_forward_log(forward_id, message_id, row_key, account, folder, uid,
                      subject, sender, date_hdr, raw_bytes, to_list, cc_list,
                      sent_by="", company="", no_fid=False,
                      robot="", code="", box="", train_no=""):
    import json
    from datetime import datetime, timezone
    if not config.is_live():
        return
    # 🔴 两个时间变量并存，语义不同，不许互相替换：
    #   now_str  —— UTC，与 SQLite datetime('now') 同基准（write 时的 checked_at / 自检窗口用）。
    #   local_str —— 本地时间（东八区），只进 sent_local，供日报分天/取时分（任务 18）。
    # 不要用已弃用的 datetime.utcnow()；也不要把 sent_at 的写入语义改成本地时间。
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    local_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _s(v):
        return "" if v is None else str(v)

    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO forward_log "
                "(forward_id, message_id, row_key, account, folder, uid, "
                "subject, sender, date_hdr, raw_hex, to_list, cc_list, sent_by, "
                "company, checked_at, check_result, sent_at, bounced, "
                "robot, sent_local, code, box, train_no) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),0,?,?,?,?,?)",
                (forward_id, message_id, row_key, account, folder, uid, subject, sender,
                 date_hdr, raw_bytes.hex() if raw_bytes else "",
                 json.dumps(to_list, ensure_ascii=False),
                 json.dumps(cc_list, ensure_ascii=False), sent_by, company,
                 now_str if no_fid else "",
                 "SKIP_NO_ID" if no_fid else "",
                 _s(robot), local_str, _s(code), _s(box), _s(train_no)))
            conn.commit()
        finally:
            conn.close()


def get_forward_log(forward_id):
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            row = conn.execute("SELECT * FROM forward_log WHERE forward_id=?", (forward_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def find_forward_log_by_message_id(message_id: str):
    """按 message_id 找一条 forward_log（rowid 倒序 = 最新在前），没有则 None。

    专列聚合（任务 11）的崩溃恢复判据用它：合并邮件的 message_id 固定为
    `BATCH::<batch_id>`，命中即证明这批**已经发出去了**（flush_batch 里
    「写 forward_log」严格早于「标 sent」，见 aggregate.flush_batch 第 6.5 步）。
    只读，不改任何状态。"""
    if not message_id:
        return None
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            row = conn.execute(
                "SELECT * FROM forward_log WHERE message_id=? ORDER BY rowid DESC LIMIT 1",
                (message_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_pending_checks(delay_sec=120, limit=200):
    """返回到期待核对的行（dict 列表，按 sent_at 升序）。

    条件：未核对 / 未退信 / 已过延迟窗口 / 不是运踪。
    sent_at 与 datetime('now') 都是 UTC，同一基准，不要引入本地时间。

    🔴 运踪不进候选：2026-10-29 拍板「tracing 不纳入核对」
    （docs/mailbots/specs/2026-10-29-mailbots_next-发送后自检.md §1 第 5 条 / §9）。
    任务 19（2026-11-16）之后运踪改走单发路径并写 forward_log，若不在这里排除，
    运踪会进候选、而 QQ 旁路的 tracing 归档夹又被 QQ_VERIFY_SKIP_SUBFOLDERS 跳过
    ⇒ 永远判不出 AB，只能落 A_ONLY / NONE，NONE 还会按 SPEC §12-① 直推公司负责人
    （2026-11-18 误报事故）。标签取 ROBOT_LABELS，不写字面串 —— 改字时这里跟着走。
    """
    tracing_label = config.ROBOT_LABELS[config.EmailType.TRACING]
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            rows = conn.execute(
                "SELECT forward_id, sent_by, company, subject, to_list, sent_at "
                "FROM forward_log "
                "WHERE checked_at = '' AND bounced = 0 "
                "  AND sent_at <= datetime('now', ?) "
                "  AND COALESCE(robot, '') <> ? "
                "ORDER BY sent_at LIMIT ?",
                (f"-{int(delay_sec)} seconds", tracing_label, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def mark_check_result(forward_id, result):
    """写核对结论（幂等；再次调用会覆盖）。result 取值见 send_check.RESULT_*。"""
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute(
                "UPDATE forward_log SET checked_at=datetime('now'), check_result=? "
                "WHERE forward_id=?",
                (result, forward_id),
            )
            conn.commit()
        finally:
            conn.close()


def list_resend_candidates(min_age_sec=None, limit=None):
    """E：可补发的行 = 判 NONE + 未补发过 + 未退信 + **非运踪** + 已过等待窗口（只读）。

    默认值走 `None` → 内部读 `config.SEND_CHECK_RESEND_*`：**只有一处真相源**，
    不要在这里写 1800 / 5 的字面量（否则改 env 配置时这里不跟着变）。

    🔴 必须排除运踪：运踪不纳入自检（B 项），但库里**已经存在** `check_result='NONE'`
    的运踪历史行（2026-11-18 就有 14 条）。不排除的话，本功能一上线就会把这 14 条
    全部补发一遍给客户 —— 客户当天已经收过 4 份，再来第 5 份，正是本任务要消灭的事故。
    """
    if min_age_sec is None:
        min_age_sec = config.SEND_CHECK_RESEND_MIN_AGE_SEC
    if limit is None:
        limit = config.SEND_CHECK_RESEND_MAX_PER_ROUND
    tracing_label = config.ROBOT_LABELS[config.EmailType.TRACING]
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            rows = conn.execute(
                "SELECT forward_id, message_id, row_key, account, folder, uid, "
                "       subject, to_list, cc_list, sent_by, company, raw_hex, sent_at "
                "  FROM forward_log "
                " WHERE check_result = 'NONE' AND COALESCE(resent_at, '') = '' "
                "   AND COALESCE(bounced, 0) = 0 "
                "   AND COALESCE(robot, '') <> ? "
                "   AND sent_at <= datetime('now', ?) "
                " ORDER BY sent_at LIMIT ?",
                (tracing_label, f"-{int(min_age_sec)} seconds", int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def claim_resend(forward_id) -> bool:
    """E：原子占位。只有第一次调用返回 True —— 抢不到就说明别人已经在补发了。

    🔴 必须在**发送之前**调用：先占位再发，进程崩在中间也不会重复补发第二遍。
    """
    if not forward_id:
        return False
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            cur = conn.execute(
                "UPDATE forward_log SET resent_at = datetime('now') "
                " WHERE forward_id = ? AND COALESCE(resent_at, '') = ''",
                (forward_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def mark_forward_bounced(forward_id):
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute("UPDATE forward_log SET bounced=1 WHERE forward_id=?", (forward_id,))
            conn.commit()
        finally:
            conn.close()


def init_bounce_handled():
    """本地 handled 索引（D1）：(account, uid) 主键，替代 Seen 做幂等。"""
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS bounce_handled (
                    account TEXT NOT NULL, uid INTEGER NOT NULL,
                    message_id TEXT, forward_id TEXT, outcome TEXT,
                    handled_at TEXT NOT NULL,
                    PRIMARY KEY (account, uid))"""
            )
            conn.commit()
        finally:
            conn.close()


def _with_handled_table(fn, *args):
    """bounce_handled 缺表时自动建表后重试一次（旧库/裸库容错）。"""
    import sqlite3
    try:
        return fn(*args)
    except sqlite3.OperationalError:
        init_bounce_handled()
        return fn(*args)


def _is_bounce_handled(account, uid):
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM bounce_handled WHERE account=? AND uid=?",
                (account, uid)).fetchone()
            return row is not None
        finally:
            conn.close()


def is_bounce_handled(account, uid):
    return _with_handled_table(_is_bounce_handled, account, uid)


def _mark_bounce_handled(account, uid, message_id="", forward_id="", outcome=""):
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bounce_handled "
                "(account, uid, message_id, forward_id, outcome, handled_at) "
                "VALUES (?,?,?,?,?,datetime('now'))",
                (account, uid, message_id, forward_id, outcome))
            conn.commit()
        finally:
            conn.close()


def mark_bounce_handled(account, uid, message_id="", forward_id="", outcome=""):
    _with_handled_table(_mark_bounce_handled, account, uid,
                        message_id, forward_id, outcome)


def purge_old_bounce_handled(days=90):
    """清理 NDR 到达窗口之外的 handled 记录（默认 90 天，随 poll 每轮执行）。"""
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            cur = conn.execute(
                "DELETE FROM bounce_handled WHERE handled_at < datetime('now', ?)",
                ("-%d days" % int(days),))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()


def _forward_log_candidates(recipient, sent_by=None):
    """返回所有 to/cc 含 recipient、未 bounced 的 forward_log 行（rowid 倒序=最新在前）。
    供 NDR 次键关联：调用方据条数判定唯一性。"""
    import json
    out = []
    with _bot_config_lock:
        conn = get_bot_config_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM forward_log WHERE bounced=0 ORDER BY rowid DESC"
            ).fetchall()
        finally:
            conn.close()
    for r in rows:
        d = dict(r)
        if sent_by and (d.get("sent_by") or "") != sent_by:
            continue
        hit = False
        for col in ("to_list", "cc_list"):
            try:
                addrs = json.loads(d.get(col) or "[]")
            except Exception:
                addrs = []
            if recipient in addrs:
                hit = True
                break
        if hit:
            out.append(d)
    return out


def find_forward_log_by_recipient(recipient, sent_by=None):
    """次键关联：在 to/cc 里找含 recipient 且未 bounced 的最新一行。"""
    cands = _forward_log_candidates(recipient, sent_by)
    return cands[0] if cands else None


init_forward_log()
init_bounce_handled()