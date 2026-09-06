import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

from .settings import SECRETS_PATH


_SECRETS_CACHE: Optional[Dict] = None


def _secrets_path() -> Path:
    """Resolve the credentials file on every call so MAILBOT_SECRETS_PATH
    injection (tests, alternate deployments) takes effect immediately."""
    return Path(os.environ.get("MAILBOT_SECRETS_PATH", str(SECRETS_PATH)))


def load_secrets() -> Dict:
    global _SECRETS_CACHE
    if _SECRETS_CACHE is not None:
        return _SECRETS_CACHE

    secrets_path = _secrets_path()
    if secrets_path.exists():
        with open(secrets_path, "r", encoding="utf-8") as f:
            _SECRETS_CACHE = json.load(f)
    else:
        _SECRETS_CACHE = {}

    for email in [
        "maoxiaoyang@cqtransit.com",
        "yangyawen@cqtransit.com",
        "fengqian@cqtransit.com",
        "hanwenhao@cqtransit.com",
    ]:
        env_key = f"MAIL_PWD_{email.replace('@', '_').replace('.', '_')}"
        if env_key in os.environ and email not in _SECRETS_CACHE.get("ACCOUNTS", {}):
            _SECRETS_CACHE.setdefault("ACCOUNTS", {})[email] = os.environ[env_key]

    return _SECRETS_CACHE


def get_accounts() -> Dict[str, str]:
    return load_secrets().get("ACCOUNTS", {})


def get_sender(email: str) -> Optional[Tuple[str, str]]:
    """Look up SMTP credentials by owner email.

    Callers (routing.responsible_person) already carry emails from
    bot_config scope='owner' rows; this is a thin alias kept so existing
    import sites keep working.
    """
    return get_sender_by_email(email)


def get_sender_by_email(email: str) -> Optional[Tuple[str, str]]:
    accounts = get_accounts()
    if email in accounts:
        return email, accounts[email]
    return None