import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

YXO_DB_PATH = os.environ.get("YXO_DB_PATH", r"\\10.0.199.184\yxo_data\yxo_app\data\yxo.db")
BOT_CONFIG_DB_PATH = Path(os.environ.get("BOT_CONFIG_DB_PATH", str(DATA_DIR / "bot_config.db")))
DEDUP_DB_PATH = Path(os.environ.get("DEDUP_DB_PATH", str(DATA_DIR / "dedup.db")))
DAILY_COUNTERS_PATH = Path(os.environ.get("DAILY_COUNTERS_PATH", str(DATA_DIR / "daily_counters.json")))
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
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))

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

DEDUP_RETENTION_DAYS = 90

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