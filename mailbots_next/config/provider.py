import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .settings import (
    BOT_CONFIG_DB_PATH,
    DEFAULT_ACCOUNTS,
    DEDUP_DB_PATH,
    DRAFT_NUMS_DB_PATH,
    DAILY_COUNTERS_PATH,
)
from .types import EmailType, TYPE_ROUTES


_RUNTIME_CACHE: Dict[str, Any] = {}
_RUNTIME_CACHE_TTL = 60
_RUNTIME_CACHE_TIME: Dict[str, float] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ConfigSnapshot:
    mode: str
    type_routes: List[Dict]
    enabled_types: Dict[str, bool]
    runtime_overrides: Dict[str, Any]
    accounts: List[str]
    yxo_db_path: str
    dedup_db_path: str
    draft_nums_db_path: str
    daily_counters_path: str
    inbound_port: int
    inbound_shared_secret: str
    sweep_interval_sec: int
    sweep_max_retry: int
    digest_hour: int
    responsible_company_col: str
    company_alias: Dict[str, str]


def _load_bot_config_runtime() -> Dict[str, bool]:
    result = {}
    if not BOT_CONFIG_DB_PATH.exists():
        return result
    try:
        conn = sqlite3.connect(BOT_CONFIG_DB_PATH)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT bot, extra FROM bot_config WHERE scope='runtime' AND key='ENABLED'"
        ):
            bot = row["bot"]
            extra = json.loads(row["extra"] or "{}")
            result[bot] = extra.get("enabled", True)
        conn.close()
    except Exception:
        pass
    return result


def _get_cached(key: str, loader, ttl: int = _RUNTIME_CACHE_TTL) -> Any:
    now = time.time()
    with _CACHE_LOCK:
        if key in _RUNTIME_CACHE and now - _RUNTIME_CACHE_TIME.get(key, 0) < ttl:
            return _RUNTIME_CACHE[key]
        value = loader()
        _RUNTIME_CACHE[key] = value
        _RUNTIME_CACHE_TIME[key] = now
        return value


def get_enabled_types() -> Dict[str, bool]:
    def loader():
        runtime = _load_bot_config_runtime()
        defaults = {r["type"].value: r["enabled"] for r in TYPE_ROUTES}
        merged = {}
        for t in EmailType:
            key = t.value
            merged[key] = runtime.get(key, defaults.get(key, True))
        return merged

    return _get_cached("enabled_types", loader)


def is_type_enabled(email_type: str) -> bool:
    return get_enabled_types().get(email_type, True)


def snapshot() -> ConfigSnapshot:
    from .settings import (
        IDLE_GROUPS,
        SMTP_SERVER,
        SMTP_PORT,
        IMAP_SERVER,
        IMAP_PORT,
        INBOUND_PORT,
        INBOUND_SHARED_SECRET,
        SWEEP_INTERVAL_SEC,
        SWEEP_MAX_RETRY,
        DIGEST_HOUR,
        RESPONSIBLE_COMPANY_COL,
        COMPANY_ALIAS,
        YXO_DB_PATH,
    )

    return ConfigSnapshot(
        mode="live" if os.environ.get("MAILBOT_MODE", "test").lower() == "live" else "test",
        type_routes=TYPE_ROUTES,
        enabled_types=get_enabled_types(),
        runtime_overrides=_load_bot_config_runtime(),
        accounts=DEFAULT_ACCOUNTS,
        yxo_db_path=YXO_DB_PATH,
        dedup_db_path=str(DEDUP_DB_PATH),
        draft_nums_db_path=str(DRAFT_NUMS_DB_PATH),
        daily_counters_path=str(DAILY_COUNTERS_PATH),
        inbound_port=INBOUND_PORT,
        inbound_shared_secret=INBOUND_SHARED_SECRET,
        sweep_interval_sec=SWEEP_INTERVAL_SEC,
        sweep_max_retry=SWEEP_MAX_RETRY,
        digest_hour=DIGEST_HOUR,
        responsible_company_col=RESPONSIBLE_COMPANY_COL,
        company_alias=COMPANY_ALIAS,
    )


def get_type_route(email_type: str) -> Optional[Dict]:
    for route in TYPE_ROUTES:
        if route["type"].value == email_type:
            return route
    return None


def get_idle_groups() -> Dict:
    from .settings import IDLE_GROUPS
    return IDLE_GROUPS


def invalidate_cache():
    with _CACHE_LOCK:
        _RUNTIME_CACHE.clear()
        _RUNTIME_CACHE_TIME.clear()