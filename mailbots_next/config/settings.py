import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = Path(os.environ.get("LOGS_DIR", str(BASE_DIR / "logs")))

YXO_DB_PATH = os.environ.get("YXO_DB_PATH", r"\\10.0.199.184\yxo_data\yxo_app\data\yxo.db")
BOT_CONFIG_DB_PATH = Path(os.environ.get("BOT_CONFIG_DB_PATH", str(DATA_DIR / "bot_config.db")))
DEDUP_DB_PATH = Path(os.environ.get("DEDUP_DB_PATH", str(DATA_DIR / "dedup.db")))
DAILY_COUNTERS_PATH = Path(os.environ.get("DAILY_COUNTERS_PATH", str(DATA_DIR / "daily_counters.json")))

# ---- 每日一封汇总邮件（任务 16）：四份「当天」状态文件 ----
# 全是运行期产物（data/ 已 gitignore），不进包、不进库：数据库表/列结构一律不动。
# 09:00 flush 后的计数快照（原企微 digest 的全部计数改由邮件第四节承载）
DAILY_DIGEST_PATH = Path(os.environ.get("DAILY_DIGEST_PATH", str(DATA_DIR / "daily_digest.json")))
# 当日待汇总项：未分类 / 配置缺失 / 待办 / 程序错误（原逐封弹企微的改记这里）
DAILY_ISSUES_PATH = Path(os.environ.get("DAILY_ISSUES_PATH", str(DATA_DIR / "daily_issues.json")))
# 12:00 归档核对 stats 快照（原企微 archive_summary 的内容）
ARCHIVE_STATS_PATH = Path(os.environ.get("ARCHIVE_STATS_PATH", str(DATA_DIR / "archive_stats.json")))
# 「今天这封已发」标记 —— 「每天恰好一封」的执行依据
DAILY_REPORT_SENT_PATH = Path(os.environ.get("DAILY_REPORT_SENT_PATH", str(DATA_DIR / "daily_report_sent.json")))
# 旧系统转发明细库：草单/运单号/ATB/DSK 只写这张表（运踪写 yxo.db 的 tracing_log）
LEGACY_FORWARD_LOG_PATH = Path(os.environ.get(
    "LEGACY_FORWARD_LOG_PATH", str(BASE_DIR.parent / "mailbots" / "data" / "forward_log.db")))

SECRETS_PATH = Path(os.environ.get("MAILBOT_SECRETS_PATH", str(BASE_DIR / "secrets.json")))

SMTP_SERVER = "smtp.qiye.aliyun.com"
SMTP_PORT = 465
IMAP_SERVER = "imap.qiye.aliyun.com"
IMAP_PORT = 993

MODE = os.environ.get("MAILBOT_MODE", "test").lower()
assert MODE in ("test", "live"), "MAILBOT_MODE must be 'test' or 'live'"


def is_live() -> bool:
    """Runtime live check. Reads the environment on every call so it is never
    frozen by import time (unlike the MODE constant above, which main() may
    set after imports). All send/write/mark-seen gates must use this."""
    return os.environ.get("MAILBOT_MODE", "test").lower() == "live"

# 运维负责人邮箱：程序错误(E1)/配置缺失(E2)/每日汇总/业务通知副本 的默认接收人。
# 代码内一律引用本常量，禁止硬编码任何个人姓名或邮箱字面量；换人只改 env/此处。
OPS_OWNER_EMAIL = os.environ.get("OPS_OWNER_EMAIL", "maoxiaoyang@cqtransit.com")

INBOUND_PORT = int(os.environ.get("INBOUND_PORT", "8765"))
INBOUND_SHARED_SECRET = os.environ.get("INBOUND_SHARED_SECRET", "")

SWEEP_INTERVAL_SEC = int(os.environ.get("SWEEP_INTERVAL_SEC", "300"))
SWEEP_MAX_RETRY = int(os.environ.get("SWEEP_MAX_RETRY", "3"))

# ---- 任务 33：失败告警延后到重判确认后 ----
# 宽限期从**首次失败**起算（重判失败不续期）。默认 180 秒 = 2 × INGEST_POLL_SEC(90)，
# 覆盖轮询重认领一个完整周期；洋 2026-11-27 10:40 拍板，不得改默认值。
# 可通过环境变量配置（离线验收可缩短），但默认值写死在这里。
ALARM_GRACE_SEC = max(1, int(os.environ.get("ALARM_GRACE_SEC", "180")))
# 共享 RecordsSnapshotProvider 的刷新间隔：默认 60 秒，provider 内部硬上限
# SWEEP_INTERVAL_SEC（300 秒）。只由主循环/显式事件触发，poller 线程不 reload。
SNAPSHOT_REFRESH_SEC = max(1, int(os.environ.get("SNAPSHOT_REFRESH_SEC", "60")))
# dedup 认领租约（秒）：认领后这段时间内同一个 key 不许第二个人认领。
# 只挡「并发重复发」，不影响失败重判 —— 发失败那次会在 set_status 里清租约。
# 取值理由：实测一封从认领到 settle 是 2~4 秒；SMTP 单连接 timeout=30s、
# 一封最多 8 家扇出 ⇒ 正常上限约 60s。取 900 是给足余量：宁可崩了晚 15 分钟
# 被重判，也不许并发重发一次。
DEDUP_CLAIM_LEASE_SEC = int(os.environ.get("DEDUP_CLAIM_LEASE_SEC", "900"))
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))
# 每日一封汇总邮件的固定发送时点（12:30 —— 等 12:00 归档核对跑完，数据才齐）。
# 见任务 16 / 06_EMAIL-DESIGN.md「发送规则（写死）」。
DAILY_REPORT_HOUR = int(os.environ.get("DAILY_REPORT_HOUR", "12"))
DAILY_REPORT_MINUTE = int(os.environ.get("DAILY_REPORT_MINUTE", "30"))

# Raw mail kept per error-queue entry is hex-encoded (~2x). Entries whose raw
# bytes exceed this cap store no payload (WARN + counter instead of silent
# truncation, which would make replays silently wrong).
RAW_MAX_BYTES = int(os.environ.get("RAW_MAX_BYTES", str(2 * 1024 * 1024)))

# Batch mark-seen (scheme B): aggregate uids this long before one flush.
MARK_SEEN_FLUSH_SEC = int(os.environ.get("MARK_SEEN_FLUSH_SEC", "300"))
# Flush a bucket early once it holds this many uids.
MARK_SEEN_BATCH_CAP = int(os.environ.get("MARK_SEEN_BATCH_CAP", "100"))

# 唯一收信机制：短周期轮询 + UID 水位线发现（2026-09-16，IDLE 已彻底删除）。
# 平均延迟 N/2、最坏 N，上界可预测。<=0 时 WARN + 回落 30（0 = 彻底不收信，不允许）。
# ⚠️ 上限不在这里校验：--poll-secs 会绕过本模块，两条入口的夹取统一在 serve.py 合流之后
#    （POLL_SECS_MAX / _clamp_poll_secs）。
_ingest_poll_raw = int(os.environ.get("INGEST_POLL_SEC", "30"))
if _ingest_poll_raw <= 0:
    import warnings as _warnings
    _warnings.warn(f"INGEST_POLL_SEC={_ingest_poll_raw} invalid, fallback to 30")
INGEST_POLL_SEC = _ingest_poll_raw if _ingest_poll_raw > 0 else 30

# 同类程序错误的通知折叠窗口（秒）：窗口内同类只发首条，窗口结束时补发一条"共 N 次"。
# 0 = 关闭折叠（逐条发送）。<0 或非法值 ⇒ WARN + 回落 60。
def _parse_collapse_window(raw: str) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        import warnings as _warnings2
        _warnings2.warn(f"NOTIFY_COLLAPSE_WINDOW_SEC={raw!r} invalid, fallback to 60")
        return 60
    if v < 0:
        import warnings as _warnings3
        _warnings3.warn(f"NOTIFY_COLLAPSE_WINDOW_SEC={v} invalid, fallback to 60")
        return 60
    return v


NOTIFY_COLLAPSE_WINDOW_SEC = _parse_collapse_window(os.environ.get("NOTIFY_COLLAPSE_WINDOW_SEC", "60"))
# UID 水位线状态文件：(account, folder) -> (uidvalidity, last_uid)，JSON 原子写。
# 必须落在 DATA_DIR 下（.gitignore 已忽略 mailbots_next/data/*.json）。
IMAP_STATE_PATH = Path(os.environ.get("IMAP_STATE_PATH", str(DATA_DIR / "imap_state.json")))

# 上线护栏：只处理该时刻之后到达的邮件，防止首次 live 启动把历史未读邮件
# 批量转发给外部客户。格式 YYYY-MM-DD，留空表示不过滤。
FORWARD_SINCE = os.environ.get("FORWARD_SINCE", "")

# NDR / 退信监控（2026-09-10 增强，堵「空 refused 静默丢」盲区）
# 默认关闭；上线前由运维确认 bounce 邮箱 + VERP 授权后开启。
BOUNCE_MONITOR_ENABLED = os.environ.get("BOUNCE_MONITOR_ENABLED", "0") in ("1", "true", "True")
# 退信监控专用邮箱（接收 NDR）。建议与同事发件账号同域、单独邮箱。
# 同时作为 VERP 基址：VERP 本地名 = "bounce+" + forward_id，域名取自本地址 @ 之后。
BOUNCE_ADDRESS = os.environ.get("BOUNCE_ADDRESS", "mailbots-bounce@cqtransit.com")
# 是否启用 VERP（+ 标签）。阿里企业邮实测不支持 + 标签时设 "0"，
# 此时 envelope MAIL FROM 直接用 BOUNCE_ADDRESS，关联仅靠 X-YXO-Forward-Id 头。
BOUNCE_USE_VERP = os.environ.get("BOUNCE_USE_VERP", "1") in ("1", "true", "True")
BOUNCE_IMAP_SERVER = os.environ.get("BOUNCE_IMAP_SERVER", IMAP_SERVER)
BOUNCE_IMAP_PORT = int(os.environ.get("BOUNCE_IMAP_PORT", str(IMAP_PORT)))
BOUNCE_IMAP_USER = os.environ.get("BOUNCE_IMAP_USER", "")
BOUNCE_IMAP_PASSWORD = os.environ.get("BOUNCE_IMAP_PASSWORD", "")
# 读 NDR 的邮箱文件夹（多数 NDR 落在 INBOX）
BOUNCE_FOLDER = os.environ.get("BOUNCE_FOLDER", "INBOX")
# 轮询周期，复用 sweep 节奏；置于 sweep 之前，让 NDR 尽快入队
BOUNCE_POLL_SEC = int(os.environ.get("BOUNCE_POLL_SEC", str(SWEEP_INTERVAL_SEC)))
# envelope MAIL FROM 模式（rev4：不依赖专用退信邮箱）：
# sender（默认）= 本封发信账号，NDR 落回同事自己收件箱；
# fixed = BOUNCE_ADDRESS；verp = bounce+<fid>@<BOUNCE_ADDRESS 域名>。
BOUNCE_ENVELOPE_MODE = os.environ.get("BOUNCE_ENVELOPE_MODE", "sender")
# 轮询账号（逗号分隔）。留空时：sender 模式 → DEFAULT_ACCOUNTS，
# 否则 → [BOUNCE_IMAP_USER]。
BOUNCE_POLL_ACCOUNTS = os.environ.get("BOUNCE_POLL_ACCOUNTS", "")

# ---- 专列聚合（放箱令 / 还箱令，任务 11 / spec 03）----
# 三闸，先到先发：
#   静默闸（主）  now - last_item_at  >= RELEASE_QUIET_SEC
#   条数闸        item_count         >= RELEASE_MAX_ITEMS
#   硬上限        now - first_item_at >= RELEASE_MAX_WAIT_SEC
# 用「最后一封 + 120s」而不是「首封 + 120s」：实测专列 3 分钟内连发 55 封，
# 按首封计时会在第 2 分钟就把前一批发掉，漏掉后面 30 封。
RELEASE_QUIET_SEC = int(os.environ.get("RELEASE_QUIET_SEC", "120"))
RELEASE_MAX_ITEMS = int(os.environ.get("RELEASE_MAX_ITEMS", "200"))
RELEASE_MAX_WAIT_SEC = int(os.environ.get("RELEASE_MAX_WAIT_SEC", "1800"))
# 单个 zip 超过这个体积才分卷（仍在同一封邮件里，多个 _partN.zip 附件）。
# 实测 55 票一票一文件 = 8.11MB（zip 只省 5%，PDF 已压缩）⇒ 50MB 约可容 340 票。
RELEASE_ZIP_MAX_BYTES = int(os.environ.get("RELEASE_ZIP_MAX_BYTES", str(50 * 1024 * 1024)))

DEDUP_RETENTION_DAYS = 90

# ---- 任务 20：未发出池 / 告警折叠（全部**写死为常量**，不走 env）----
# 理由：这几个值直接决定「会不会重复发」与「多久提醒一次」，让运维在环境变量里
# 改成 0 或负数会静默改掉业务口径（例如折叠窗口设 0 ⇒ 每轮都告警；设成 -1 ⇒
# 永不告警）。要改就改代码、走评审。
#
# D3：同一告警 key 在 24h 内只发 1 条，重复的只累加计数（窗口到期由 sweeper 补发
# 一条「共 N 次」）。折叠是**频率策略，不是规则排除**。
ALARM_FOLD_SEC = 24 * 3600
# D3：同一件 unresolved 超过 N 天**必报**（文案带已在池天数）。
POOL_STALE_DAYS = 2
# D2：原文连续取不到几次才认定「邮件已删」并转 closed(mail_gone)。
# 留 3 次余量：一次 IMAP 抖动不该被判成"邮件已删"。
POOL_EMPTY_RETRY_MAX = 3
# D2：单轮重判最多处理多少件（防一次涌入把 sweeper 拖长）。
REPOOL_BATCH_MAX = 200
# 3.4.1：路由失败时按发件人历史归属反查负责人，回看最近 N 条 forward_log。
FAILURE_OWNER_HISTORY_N = 20
# D2 触发器①：舱单导入提交成功后写的脏标记（serve 消费后删除）。
REPOOL_REQUEST_PATH = Path(os.environ.get(
    "REPOOL_REQUEST_PATH", str(DATA_DIR / "repool_request.json")))
# D2 触发器②：每日一次重判的完成日期（{"last_daily_date": "YYYY-MM-DD"}，原子写）。
REPOOL_STATE_PATH = Path(os.environ.get(
    "REPOOL_STATE_PATH", str(DATA_DIR / "repool_state.json")))

DEFAULT_ACCOUNTS = [
    "maoxiaoyang@cqtransit.com",
    "yangyawen@cqtransit.com",
    "fengqian@cqtransit.com",
    "hanwenhao@cqtransit.com",
]

IDLE_GROUPS = {
    "group1_draft_waybill": {
        "folders": ["运单草单", "运单号"],
        "folder_utf7": ["&j9BTVYNJU1U-", "&j9BTVVP3-"],
        "types": ["draft", "waybill"],
    },
    "group2_dsk_atb": {
        "folders": ["DSK", "ATB"],
        "folder_utf7": ["DSK", "ATB"],
        "types": ["dsk", "atb"],
    },
    "group3_tracing": {
        "folders": ["Tracing"],
        "folder_utf7": ["Tracing"],
        "types": ["tracing"],
    },
}

RESPONSIBLE_COMPANY_COL = "开票子公司名称"

COMPANY_ALIAS = {
    "公运沙坪坝": "沙坪坝",
    "重轮太平洋": "太平洋",
}

CONTAINER_RE = re.compile(r'^[A-Z]{4}\d{7}$')
CODE_RE = re.compile(r'CQWLJT[0-9A-Za-z\-]+')
CODE_NUM_RE = re.compile(r'CQWLJT(\d+)')

# ---- 发送后自检（2026-10-29）----
# 自检/抄送旁路地址：只有它被 SMTP 拒收时可以不算业务失败（客户侧已收到）。
# 置空 = 关闭这个例外（任何收件人被拒都判失败）。
SELFCHECK_CC_ADDR = os.environ.get("SELFCHECK_CC_ADDR", "3841559246@qq.com")
# 默认关闭：代码合入、单测通过、真实邮箱不碰；切换验证完再由运维打开。
SEND_CHECK_ENABLED = os.environ.get("SEND_CHECK_ENABLED", "0") in ("1", "true", "True")
# 发出去多久之后才核对（秒）。实测到达 QQ 7.9~22s、已发送 <21s，取 120 留 5~15 倍余量。
SEND_CHECK_DELAY_SEC = int(os.environ.get("SEND_CHECK_DELAY_SEC", "120"))
# 每轮最多核对多少封（防一次涌入过多把 sweep 拖长）。
SEND_CHECK_BATCH_MAX = int(os.environ.get("SEND_CHECK_BATCH_MAX", "200"))
# 自检判 NONE（两路都没有副本）后自动补发一次（洋 2026-11-18 拍板）。
# 🔴 默认开，但它会**主动再发一封给客户**，所以三道闸一个都不能少：
#   ① 至少等 RESEND_MIN_AGE 秒（实测 QQ 到达 7.9~22s、阿里已发送 <21s，30 分钟足够）
#   ② 每封最多补发一次（resent_at 原子占位）
#   ③ 每轮最多 RESEND_MAX_PER_ROUND 封（防一次性爆发）
SEND_CHECK_RESEND_ENABLED = os.environ.get("SEND_CHECK_RESEND_ENABLED", "1") in ("1", "true", "True")
SEND_CHECK_RESEND_MIN_AGE_SEC = int(os.environ.get("SEND_CHECK_RESEND_MIN_AGE_SEC", "1800"))
SEND_CHECK_RESEND_MAX_PER_ROUND = int(os.environ.get("SEND_CHECK_RESEND_MAX_PER_ROUND", "5"))
# QQ 侧每个文件夹取「序号尾部 K 封」来比对（腾讯 SEARCH SINCE 无效，只能这样）。
SEND_CHECK_TAIL_N = int(os.environ.get("SEND_CHECK_TAIL_N", "300"))
# 阿里「已发送」的 IMAP 文件夹名（UTF-7 编码的「已发送」）
SENT_FOLDER_UTF7 = os.environ.get("SENT_FOLDER_UTF7", "&XfJT0ZAB-")
# QQ 旁路邮箱（第二路证据）。凭据从 secrets.json 读，env 优先。
QQ_VERIFY_IMAP_SERVER = os.environ.get("QQ_VERIFY_IMAP_SERVER", "imap.qq.com")
QQ_VERIFY_IMAP_PORT = int(os.environ.get("QQ_VERIFY_IMAP_PORT", "993"))
QQ_VERIFY_USER = os.environ.get("QQ_VERIFY_USER", "")
QQ_VERIFY_PASSWORD = os.environ.get("QQ_VERIFY_PASSWORD", "")
# QQ「其他文件夹」的 IMAP 前缀（UTF-7）；其下所有子夹都要查，不写死清单
QQ_VERIFY_OTHER_ROOT = os.environ.get("QQ_VERIFY_OTHER_ROOT", "&UXZO1mWHTvZZOQ-")
# QQ 侧必须排除的文件夹（前两个实测 EXAMINE 会报 [CLIENTBUG]）
QQ_VERIFY_EXCLUDE = ("Sent Messages", "Deleted Messages", "Drafts", "Junk")
# 「其他文件夹」下要跳过的子夹（按名字最后一段匹配，不含前缀）。
# 依据：tracing 不纳入核对（洋 2026-10-29 拍板，
#      见 docs/mailbots/specs/2026-10-29-mailbots_next-发送后自检.md）。
# ⚠️ 跳过它本身只是省时间，**前提是运踪不进候选** —— 该前提由
#    store.get_pending_checks 按 ROBOT_LABELS[EmailType.TRACING] 排除维持。
#    2026-11-16 任务 19 之后运踪开始写 forward_log，这个前提一度失效，
#    造成 2026-11-18 运踪自检全部判不出 AB（A_ONLY 6 / NONE 14）。两处必须同时成立。
QQ_VERIFY_SKIP_SUBFOLDERS = ("tracing",)