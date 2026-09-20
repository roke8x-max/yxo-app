"""C group tests: WeCom HTTP thin client (spec S5, 8 scenarios).

All HTTP is faked by stubbing ``urllib.request.urlopen`` -- never hits a
real WeCom service. Real names are never hardcoded here; the single
allowed source (``WeComNotifier._WECOM_NAME_BY_EMAIL``) is imported.
"""

import io
import json
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

import mailbots_next.config.secrets as secrets_mod
import mailbots_next.core.notify as notify_mod
import mailbots_next.core.wecom_notify as wecom_mod
from mailbots_next.core.notify import (
    DAILY_COUNTERS_PATH,
    WeComNotifier,
    get_counters,
)
from mailbots_next.core.wecom_notify import WeComConfigMissing, WeComHttpClient

TEST_URL = "http://127.0.0.1:5001/api/notify"
TEST_TOKEN = "test-token-abc123"


@pytest.fixture(autouse=True)
def clean_env():
    if DAILY_COUNTERS_PATH.exists():
        DAILY_COUNTERS_PATH.unlink()
    yield
    if DAILY_COUNTERS_PATH.exists():
        DAILY_COUNTERS_PATH.unlink()


@pytest.fixture()
def wecom_env(monkeypatch):
    """Configured client env; secrets cache emptied so env is authoritative."""
    monkeypatch.setenv("WECOM_NOTIFY_URL", TEST_URL)
    monkeypatch.setenv("WECOM_NOTIFY_TOKEN", TEST_TOKEN)
    monkeypatch.delenv("WECOM_KF_EMAILS", raising=False)
    monkeypatch.setattr(secrets_mod, "_SECRETS_CACHE", {})
    monkeypatch.setattr(notify_mod, "_client_unavailable_logged", False)
    return monkeypatch


class _FakeResp:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_stub(monkeypatch, behavior):
    """Stub urlopen; return (calls, timeouts) recorders.

    behavior: callable(req) -> _FakeResp, or an exception instance to raise.
    """
    calls, timeouts = [], []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        timeouts.append(timeout)
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior(req)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls, timeouts


def _req_info(req):
    headers = {k.lower(): v for k, v in req.header_items()}
    return {
        "url": req.full_url,
        "headers": headers,
        "body": json.loads(req.data.decode("utf-8")),
    }


def _real_name(email):
    return WeComNotifier._WECOM_NAME_BY_EMAIL[email]


# --- 1: success path -------------------------------------------------------

def test_1_success_returns_ok_and_channel(wecom_env, monkeypatch):
    calls, _ = _install_stub(
        monkeypatch,
        lambda req: _FakeResp(200, b'{"ok": true, "channel": "app"}'),
    )
    client = WeComHttpClient.from_secrets()
    name = _real_name("maoxiaoyang@cqtransit.com")
    ok, channel = client.notify_by_name(name, "hello-directions", False)
    assert (ok, channel) == (True, "app")
    assert len(calls) == 1
    info = _req_info(calls[0])
    assert info["url"] == TEST_URL
    assert info["headers"].get("x-notify-token") == TEST_TOKEN
    assert info["body"]["name"] == name  # 真名透传
    assert info["body"]["try_kf"] is False


# --- 2: failure statuses ----------------------------------------------------

@pytest.mark.parametrize(
    "behavior",
    [
        urllib.error.HTTPError(TEST_URL, 401, "unauthorized", {}, io.BytesIO(b"")),
        urllib.error.HTTPError(TEST_URL, 500, "boom", {}, io.BytesIO(b"")),
        lambda req: _FakeResp(200, b'{"ok": false, "channel": ""}'),
    ],
    ids=["401", "500", "200-ok-false"],
)
def test_2_failure_statuses_count_and_error(wecom_env, monkeypatch, behavior):
    calls, _ = _install_stub(monkeypatch, behavior)
    before = get_counters().get("notify_failed", 0)
    with patch.object(wecom_mod._log, "error") as mock_err:
        ok, channel = WeComHttpClient.from_secrets().notify_by_name(
            "some-name", "some-text")
    assert ok is False
    assert isinstance(channel, str)
    assert len(calls) == 1
    assert get_counters().get("notify_failed", 0) == before + 1
    assert mock_err.call_count >= 1


# --- 3: connection failure / timeout: no retry -------------------------------

@pytest.mark.parametrize(
    "exc",
    [urllib.error.URLError("conn refused"), TimeoutError("timed out")],
    ids=["conn-refused", "timeout"],
)
def test_3_transport_error_single_shot(wecom_env, monkeypatch, exc):
    calls, _ = _install_stub(monkeypatch, exc)
    before = get_counters().get("notify_failed", 0)
    with patch.object(wecom_mod._log, "error"):
        ok, channel = WeComHttpClient.from_secrets().notify_by_name(
            "some-name", "some-text")
    assert (ok, channel) == (False, "")
    assert len(calls) == 1  # 不重试
    assert get_counters().get("notify_failed", 0) == before + 1


# --- 4: missing config ---------------------------------------------------------

def test_4_missing_config_no_request(monkeypatch):
    import mailbots_next.core.notify as notify_mod

    monkeypatch.delenv("WECOM_NOTIFY_URL", raising=False)
    monkeypatch.delenv("WECOM_NOTIFY_TOKEN", raising=False)
    monkeypatch.setattr(secrets_mod, "_SECRETS_CACHE", {})
    monkeypatch.setattr(notify_mod, "_client_unavailable_logged", False)
    calls, _ = _install_stub(
        monkeypatch, lambda req: _FakeResp(200, b'{"ok": true}'))
    with pytest.raises(WeComConfigMissing):
        WeComHttpClient.from_secrets()
    n = WeComNotifier()
    assert n._notify_by_name is None
    assert n._client is None
    assert calls == []  # 没有发出任何请求


# --- 5: kf whitelist -------------------------------------------------------------

def test_5_kf_whitelist_case_and_spaces(wecom_env, monkeypatch):
    monkeypatch.setenv(
        "WECOM_KF_EMAILS",
        "MAOXiaoyang@cqtransit.com ,  yangyawen@cqtransit.com",
    )
    client = WeComHttpClient.from_secrets()
    assert client.kf_enabled(" maoxiaoyang@CQTRANSIT.com ") is True
    assert client.kf_enabled("YANGYAWEN@cqtransit.com") is True
    assert client.kf_enabled("hanwenhao@cqtransit.com") is False
    assert client.kf_enabled("") is False


def test_5_try_kf_follows_whitelist_end_to_end(wecom_env, monkeypatch):
    """notify() 循环按收件人邮箱判定 try_kf，真名照常透传。"""
    monkeypatch.setenv("WECOM_KF_EMAILS", "maoxiaoyang@cqtransit.com")
    bodies = []

    def behavior(req):
        bodies.append(json.loads(req.data.decode("utf-8")))
        return _FakeResp(200, b'{"ok": true, "channel": "app"}')

    _install_stub(monkeypatch, behavior)
    n = WeComNotifier()
    assert n._notify_by_name is not None
    ok = n.notify(
        "alarm",
        ["maoxiaoyang@cqtransit.com", "hanwenhao@cqtransit.com"],
        "kf-routing-check",
    )
    assert ok is True
    by_name = {b["name"]: b["try_kf"] for b in bodies}
    assert by_name[_real_name("maoxiaoyang@cqtransit.com")] is True
    assert by_name[_real_name("hanwenhao@cqtransit.com")] is False


# --- 6: unmapped recipient keeps downgrade path ----------------------------------

def test_6_unmapped_email_still_downgrades(wecom_env, monkeypatch):
    calls, _ = _install_stub(
        monkeypatch, lambda req: _FakeResp(200, b'{"ok": true}'))
    before = get_counters().get("notify_failed", 0)
    with patch.object(notify_mod._log, "error") as mock_err:
        ok = WeComNotifier().notify("alarm", ["ghost@example.com"], "x")
    assert ok is False
    assert calls == []  # 未映射不打 HTTP，直接走降级
    assert get_counters().get("notify_failed", 0) == before + 1
    assert mock_err.call_count >= 1  # 不静默


# --- 7: failure log redaction ------------------------------------------------------

def test_7_failure_log_has_no_token_or_text(wecom_env, monkeypatch):
    calls, _ = _install_stub(
        monkeypatch,
        urllib.error.HTTPError(TEST_URL, 500, "boom", {}, io.BytesIO(b"")),
    )
    secret_text = "secret-body-xyz-987"
    with patch.object(wecom_mod._log, "error") as mock_err:
        WeComHttpClient.from_secrets().notify_by_name("some-name", secret_text)
    texts = " ".join(str(c.args[0]) for c in mock_err.call_args_list)
    assert TEST_TOKEN not in texts
    assert secret_text not in texts


# --- 8: read timeout floor ------------------------------------------------------------

def test_8_read_timeout_floor(wecom_env, monkeypatch):
    assert WeComHttpClient.READ_TIMEOUT >= 40
    calls, timeouts = _install_stub(
        monkeypatch, lambda req: _FakeResp(200, b'{"ok": true}'))
    client = WeComHttpClient.from_secrets()
    assert client.read_timeout >= 40
    client.notify_by_name("some-name", "t")
    assert timeouts and timeouts[0] >= 40  # 实际生效的超时
