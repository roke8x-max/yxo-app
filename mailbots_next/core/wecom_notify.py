"""Thin HTTP client for WeCom notifications (C group, 2026-09-20).

Replaces the old direct import of the on-disk companion package
with a single ``POST {WECOM_NOTIFY_URL}`` to the companion notification service
running on the same machine. All WeCom logic (access_token cache, dual
channel, kf, 1800-byte segmentation) stays server-side; this module only
transports ``(name, text, try_kf)`` and reports ``(ok, channel)``.

Transport uses the standard library (``urllib.request``) so no new
dependency is added. ``urlopen`` takes a single timeout, therefore the
effective socket timeout for both connect and read is ``READ_TIMEOUT``
(45s); ``CONNECT_TIMEOUT`` documents the intended connect budget.
Deviation from spec S3.1-3 is intentional and documented there.
"""

import json
import os
import urllib.error
import urllib.request
from typing import Set, Tuple

from .log import get_logger

_log = get_logger(__name__)


class WeComConfigMissing(Exception):
    """URL or token not configured."""


class WeComHttpClient:
    """Single-shot POST client for ``POST {url}`` (``/api/notify``)."""

    # Intended connect budget (spec S3.1-3). stdlib urlopen only accepts
    # one timeout, so the effective timeout applied below is READ_TIMEOUT;
    # a 3s connect against 127.0.0.1 either succeeds or fails fast anyway.
    CONNECT_TIMEOUT = 3.0
    # Must stay >= 40s: try_kf=true takes the server ~35.8s (kf attempt that
    # fails over to app message) before it answers 200 (spec S3.1-3).
    READ_TIMEOUT = 45.0

    def __init__(self, url: str, token: str, kf_emails: Set[str]):
        self.url = url
        self._token = token
        self._kf_emails = {e.strip().lower() for e in kf_emails if e.strip()}

    @classmethod
    def from_secrets(cls) -> "WeComHttpClient":
        """Build from env first, then ``mailbots_next/secrets.json``.

        Raises WeComConfigMissing when URL or TOKEN is absent from both.
        """
        from ..config.secrets import load_secrets

        try:
            secrets = load_secrets() or {}
        except Exception:
            secrets = {}
        if not isinstance(secrets, dict):
            secrets = {}

        url = os.environ.get("WECOM_NOTIFY_URL") or secrets.get("WECOM_NOTIFY_URL") or ""
        token = os.environ.get("WECOM_NOTIFY_TOKEN") or secrets.get("WECOM_NOTIFY_TOKEN") or ""
        raw_kf = os.environ.get("WECOM_KF_EMAILS", None)
        if raw_kf is None:
            raw_kf = secrets.get("WECOM_KF_EMAILS", "")
        if isinstance(raw_kf, (list, tuple, set)):
            kf_emails = {str(e) for e in raw_kf}
        else:
            kf_emails = set(str(raw_kf or "").split(","))

        url = str(url).strip()
        token = str(token).strip()
        if not url or not token:
            raise WeComConfigMissing(
                "WECOM_NOTIFY_URL / WECOM_NOTIFY_TOKEN not configured "
                "(env first, then mailbots_next/secrets.json)"
            )
        return cls(url, token, kf_emails)

    def kf_enabled(self, email: str) -> bool:
        """True when *email* is in the kf whitelist (case-insensitive)."""
        return str(email or "").strip().lower() in self._kf_emails

    @property
    def read_timeout(self) -> float:
        return self.READ_TIMEOUT

    def notify_by_name(self, name: str, text: str, try_kf: bool = False) -> Tuple[bool, str]:
        """POST one notification; never raises, never retries.

        Returns ``(ok, channel)`` with the same shape as the old
        package-level ``notify_by_name``. Any failure (timeout, connection
        error, 401, non-200, ``ok=false``) is logged (status/exception
        type/channel only -- never token, never full text) and counted
        via ``notify_failed``.
        """
        from .notify import increment_counter

        payload = json.dumps(
            {"name": name, "text": text, "try_kf": bool(try_kf)},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Notify-Token": self._token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.READ_TIMEOUT) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
        except urllib.error.HTTPError as e:
            channel = _extract_channel(_safe_read(e))
            _log.error(
                f"WeCom notify http failed: status={e.code} channel={channel!r}"
            )
            increment_counter("notify_failed")
            return False, channel
        except Exception as e:
            _log.error(f"WeCom notify http failed: {type(e).__name__} channel=''")
            increment_counter("notify_failed")
            return False, ""

        ok, channel = _parse_body(body)
        if status == 200 and ok:
            return True, channel
        _log.error(f"WeCom notify http failed: status={status} channel={channel!r}")
        increment_counter("notify_failed")
        return False, channel


def _safe_read(e: urllib.error.HTTPError) -> bytes:
    try:
        return e.read()
    except Exception:
        return b""


def _extract_channel(body: bytes) -> str:
    _, channel = _parse_body(body)
    return channel


def _parse_body(body: bytes) -> Tuple[bool, str]:
    """Return ``(ok, channel)`` from an ``/api/notify`` response body."""
    try:
        data = json.loads((body or b"").decode("utf-8"))
    except Exception:
        return False, ""
    if not isinstance(data, dict):
        return False, ""
    channel = data.get("channel", "")
    return bool(data.get("ok")), channel if isinstance(channel, str) else ""
