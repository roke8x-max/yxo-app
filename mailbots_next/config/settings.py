import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

YXO_DB_PATH = os.environ.get("YXO_DB_PATH", r"\\10.0.199.184\yxo_data\yxo_app\data\yxo.db")
BOT_CONFIG_DB_PATH = Path(os.environ.get("BOT_CONFIG_DB_PATH", str(DATA_DIR / "bot_config.db")))
DEDUP_DB_PATH = Path(os.environ.get("DEDUP_DB_PATH", str(DATA_DIR / "dedup.db")))
DRAFT_NUMS_DB_PATH = Path(os.environ.get("DRAFT_NUMS_DB_PATH", str(DATA_DIR / "draft_nums.db")))
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

# IDLE topology: per (folder x account) connection when true (default),
# legacy per-group connection when false (fast rollback if the server caps
# concurrent connections).
IDLE_PER_FOLDER = os.environ.get("IDLE_PER_FOLDER", "1") not in ("0", "false", "False")
# How long one IDLE wait lasts before re-checking (seconds).
MAX_IDLE_SEC = int(os.environ.get("MAX_IDLE_SEC", "1740"))

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