import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..config import DAILY_COUNTERS_PATH, DIGEST_HOUR
from .log import get_logger

_log = get_logger(__name__)
_counter_lock = threading.Lock()


def _load_counters() -> Dict[str, Any]:
    if DAILY_COUNTERS_PATH.exists():
        try:
            with open(DAILY_COUNTERS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": datetime.now().strftime("%Y-%m-%d"), "counts": {}}


def _save_counters(data: Dict[str, Any]):
    DAILY_COUNTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _counter_lock:
        with open(DAILY_COUNTERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def increment_counter(key: str, count: int = 1):
    data = _load_counters()
    today = datetime.now().strftime("%Y-%m-%d")
    if data.get("date") != today:
        data = {"date": today, "counts": {}}
    data["counts"][key] = data["counts"].get(key, 0) + count
    _save_counters(data)


def get_counters() -> Dict[str, int]:
    data = _load_counters()
    return data.get("counts", {})


def flush_counters() -> Dict[str, int]:
    data = _load_counters()
    counts = data.get("counts", {})
    _save_counters({"date": datetime.now().strftime("%Y-%m-%d"), "counts": {}})
    return counts


class WeComNotifier:
    def __init__(self):
        self._client = None
        self._init_client()

    def _init_client(self):
        try:
            from wecombot.cs_bot.wecom_api import notify_by_name
            self._notify_by_name = notify_by_name
        except Exception:
            self._notify_by_name = None
            _log.warning("WeCom client not available")

    # 企微发送适配：内部以邮箱标识同事，但现网 wecom_api.notify_by_name 按企微姓名发送。
    # 本表仅作"邮箱→企微姓名"转换用，严禁把姓名当业务键流通到 notify.py 之外。
    # 仓库内唯一允许出现同事真名的位置（纯净铁律例外）。
    _WECOM_NAME_BY_EMAIL = {
        "maoxiaoyang@cqtransit.com": "毛骁洋",
        "yangyawen@cqtransit.com": "杨雅雯",
        "fengqian@cqtransit.com": "冯茜",
        "hanwenhao@cqtransit.com": "韩文豪",
    }

    def notify(self, notify_type: str, recipients: List[str], content: str, error_id: Optional[str] = None) -> bool:
        """Send to a list of owner *emails*; each is translated to the WeCom
        display name via _WECOM_NAME_BY_EMAIL before calling notify_by_name.
        Unmapped emails are logged, counted as failed, and escalated to the
        ops owner by SMTP (never silently dropped, never sent raw)."""
        from ..config import OPS_OWNER_EMAIL

        if not self._notify_by_name:
            _log.warning("WeCom notify skipped: client not available")
            return False

        success_count = 0
        unmapped = []
        for recipient in recipients:
            name = self._WECOM_NAME_BY_EMAIL.get(recipient)
            if not name:
                _log.error(
                    f"WeCom recipient has no name mapping, escalating: {recipient}",
                    error_id=error_id,
                )
                _log.log_notify("", notify_type, [recipient], False)
                unmapped.append(recipient)
                continue
            try:
                ok, channel = self._notify_by_name(name, content)
                if ok:
                    success_count += 1
                _log.log_notify("", notify_type, [recipient], ok)
            except Exception as e:
                _log.error(f"WeCom notify failed for {recipient}: {e}", error_id=error_id)
        if unmapped:
            if self._fallback_owner_email(notify_type, unmapped, content, error_id):
                success_count += 1
        return success_count > 0

    def _fallback_owner_email(self, notify_type: str, unmapped: List[str],
                              content: str, error_id: Optional[str] = None) -> bool:
        """Downgrade path for recipients without a WeCom name mapping: forward
        the alert by SMTP to the ops owner so it is never silently lost."""
        from ..config import OPS_OWNER_EMAIL, SMTP_SERVER, SMTP_PORT
        from ..config.secrets import get_accounts
        import smtplib
        from email.mime.text import MIMEText

        password = get_accounts().get(OPS_OWNER_EMAIL, "")
        if not password:
            _log.error(
                f"WeCom fallback unsendable, no SMTP password for {OPS_OWNER_EMAIL}; "
                f"original alert kept in logs: {unmapped}",
                error_id=error_id,
            )
            return False
        subject = f"企微未映射告警收件人({notify_type})，已降级邮件通知：{','.join(unmapped)}"
        body = f"{subject}\n\n以下告警收件人未在企微映射，原告警原文如下：\n\n{content}"
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = OPS_OWNER_EMAIL
            msg["To"] = OPS_OWNER_EMAIL
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.login(OPS_OWNER_EMAIL, password)
                refused = server.sendmail(OPS_OWNER_EMAIL, [OPS_OWNER_EMAIL], msg.as_string())
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
            _log.info(f"WeCom fallback email sent for unmapped recipients: {unmapped}")
            return True
        except Exception as e:
            _log.error(f"WeCom fallback email failed: {e}", error_id=error_id)
            return False

    def send_alarm(self, responsible_person: str, admin_person: str, detail: str, error_id: Optional[str] = None) -> bool:
        content = f"🚨 报警通知\n{detail}"
        return self.notify("alarm", [responsible_person, admin_person], content, error_id)

    def send_pending(self, responsible_person: str, admin_person: str, detail: str, error_id: Optional[str] = None) -> bool:
        content = f"📥 待办通知\n{detail}"
        return self.notify("pending", [responsible_person, admin_person], content, error_id)

    def send_forwarded(self, responsible_person: str, admin_person: str, detail: str, error_id: Optional[str] = None) -> bool:
        content = f"✅ 已转发\n{detail}"
        return self.notify("forwarded", [responsible_person, admin_person], content, error_id)

    def send_draft_update(self, responsible_person: str, admin_person: str, detail: str, error_id: Optional[str] = None) -> bool:
        content = f"🔄 草单更新\n{detail}"
        return self.notify("draft_update", [responsible_person, admin_person], content, error_id)

    def send_program_error(self, admin_person: str, error_id: str, detail: str) -> bool:
        content = f"❌ 程序错误 [error_id={error_id}]\n{detail}"
        return self.notify("program_error", [admin_person], content, error_id)

    def send_config_missing(self, admin_person: str, error_id: str, detail: str) -> bool:
        content = f"⚙️ 配置缺失 [error_id={error_id}]\n{detail}"
        return self.notify("config_missing", [admin_person], content, error_id)

    def send_digest(self, admin_person: str, counts: Dict[str, int]) -> bool:
        if not counts:
            return True
        lines = [f"📊 每日汇总 ({datetime.now().strftime('%Y-%m-%d')})"]
        for k, v in sorted(counts.items()):
            lines.append(f"  {k}: {v}")
        content = "\n".join(lines)
        return self.notify("digest", [admin_person], content)


_notifier_instance = None
_notifier_lock = threading.Lock()


def get_notifier() -> WeComNotifier:
    global _notifier_instance
    with _notifier_lock:
        if _notifier_instance is None:
            _notifier_instance = WeComNotifier()
        return _notifier_instance