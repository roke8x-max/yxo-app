"""Test isolation: all DB paths point at a temp dir (set before any
mailbots_next import). Note: `xlwt` is test-only (builds the tracing XLS
fixture); without it test_tracing_extractor skips via skipif."""
import sys
import os
import tempfile
from pathlib import Path

# Create a temp directory for test databases
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="mailbots_next_test_"))

# Temp yxo.db seeded with the minimal records the suite needs. No test may
# depend on any external database file (CI-clean-machine rule).
_YXO_TMP = _TEST_DATA_DIR / "yxo_test.db"


def _seed_yxo(path):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY, 客户编码 TEXT, 箱号 TEXT,"
            " 班列号 TEXT, 目的站 TEXT, 开票子公司名称 TEXT, 本地货源公司 TEXT,"
            " 状态 TEXT, is_deleted INTEGER, dsk TEXT, ATB TEXT)"
        )
        conn.executemany(
            "INSERT INTO records (客户编码, 箱号, 班列号, 目的站, 开票子公司名称,"
            " 本地货源公司, 状态, is_deleted, dsk, ATB)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("CQWLJT260810001-SPB", "CICU1000001", "WB1", "测试站",
                 "太平洋", "", "正常", 0, "", ""),
                ("CQWLJT260810002-VXN", "CICU1000002", "WB1", "测试站",
                 "东盟", "", "正常", 0, "", ""),
            ],
        )
        conn.execute(
            "CREATE TABLE tracing_log (log_id TEXT, train_no TEXT, company TEXT,"
            " mail_msg_id TEXT, forward_detail TEXT, log_date TEXT, train_key TEXT)"
        )
        conn.execute(
            "CREATE TABLE tracing_snapshot (train_key TEXT, box_no TEXT, node TEXT,"
            " status TEXT, event_time TEXT, source TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


if not _YXO_TMP.exists():
    _seed_yxo(_YXO_TMP)

# Set environment variables BEFORE any mailbots_next imports
os.environ.setdefault("MAILBOT_MODE", "test")
os.environ.setdefault("YXO_DB_PATH", str(_YXO_TMP))
os.environ.setdefault("INBOUND_SHARED_SECRET", "test_secret_123")
os.environ.setdefault("BOT_CONFIG_DB_PATH", str(_TEST_DATA_DIR / "bot_config.db"))
os.environ.setdefault("DEDUP_DB_PATH", str(_TEST_DATA_DIR / "dedup.db"))
os.environ.setdefault("DRAFT_NUMS_DB_PATH", str(_TEST_DATA_DIR / "draft_nums.db"))
os.environ.setdefault("DAILY_COUNTERS_PATH", str(_TEST_DATA_DIR / "daily_counters.json"))

# Fake credentials file: tests must never touch the real secrets.json.
# MAILBOT_SECRETS_PATH is read by settings at import time, so this must stay
# at module level, before any mailbots_next import.
_FAKE_SECRETS = {
    "ACCOUNTS": {
        "maoxiaoyang@cqtransit.com": "fake-pwd-mao",
        "yangyawen@cqtransit.com": "fake-pwd-yang",
        "fengqian@cqtransit.com": "fake-pwd-feng",
        "hanwenhao@cqtransit.com": "fake-pwd-han",
    }
}
_FAKE_SECRETS_PATH = _TEST_DATA_DIR / "secrets.json"
if not _FAKE_SECRETS_PATH.exists():
    import json as _json

    _FAKE_SECRETS_PATH.write_text(_json.dumps(_FAKE_SECRETS), encoding="utf-8")
os.environ.setdefault("MAILBOT_SECRETS_PATH", str(_FAKE_SECRETS_PATH))

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_env():
    # Environment already set at module level
    yield