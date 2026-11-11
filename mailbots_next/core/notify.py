import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..config import (
    DAILY_COUNTERS_PATH,
    DAILY_DIGEST_PATH,
    DAILY_ISSUES_PATH,
    DIGEST_HOUR,
    NOTIFY_COLLAPSE_WINDOW_SEC,
)
from .log import get_logger, mask_recipients

_log = get_logger(__name__)
_counter_lock = threading.Lock()

# 客户端不可用时的启动期 ERROR 防刷屏：每个进程只记一次（B 组 T3a）。
_client_unavailable_logged = False

# 程序错误通知折叠（2026-09-21）：短时间内的同类 program_error 只发首条，
# 窗口结束时补发一条"共 N 次"汇总。日志仍逐条、计数不受影响、不同种类绝不合并。
_COLLAPSIBLE_TYPES = {"program_error", "send_check"}
_COLLAPSE_MAX_ENTRIES = 500
_collapse_state: Dict[str, Dict[str, Any]] = {}
_collapse_lock = threading.Lock()
# 导入时刻的默认值：用于分辨"测试 monkeypatch 了 notify 模块attr"还是"动了 settings"。
_DEFAULT_COLLAPSE_WINDOW = NOTIFY_COLLAPSE_WINDOW_SEC


def _now() -> float:
    """窗口时钟（单调时钟，可被测试 monkeypatch 推进；测试不许用 sleep）。"""
    return time.monotonic()


def _normalize_content(content: str) -> str:
    """归一化每次必然变化的 token：① 去掉 [error_id=…] 片段 ② 连续数字→#。

    只做这两件事：不同来源的错误文本归一化后天然不同 ⇒ 不同问题不会被合并。
    """
    text = re.sub(r"\[error_id=[^\]]*\]", "", content or "")
    text = re.sub(r"\d+", "#", text)
    return text


def _fingerprint(notify_type: str, content: str) -> str:
    return f"{notify_type}|{_normalize_content(content)}"


def _collapse_summary(content: str, count: int) -> str:
    """汇总文案（单实现，两处共用）：不写"最近 N 秒"——汇总可能迟到（如下一条
    同类错误几天后才来），时间断言会失真。"""
    return content + f"\n（同一批同类错误共发生 {count} 次，已折叠 {count - 1} 条）"


def _get_collapse_window() -> int:
    """折叠窗口秒数：env 优先（支持测试 setenv），否则 notify 模块attr（支持
    monkeypatch.setattr(notify_mod, ...)，否则 settings 当前值（支持直接
    patch settings）。非法值 ⇒ 60（与 settings 解析口径一致），不抛异常。"""
    if "NOTIFY_COLLAPSE_WINDOW_SEC" in os.environ:
        try:
            v = int(os.environ["NOTIFY_COLLAPSE_WINDOW_SEC"])
        except (TypeError, ValueError):
            return 60
        return 60 if v < 0 else v
    cur = globals().get("NOTIFY_COLLAPSE_WINDOW_SEC", _DEFAULT_COLLAPSE_WINDOW)
    if cur != _DEFAULT_COLLAPSE_WINDOW:
        try:
            v = int(cur)
        except (TypeError, ValueError):
            return 60
        return 60 if v < 0 else v
    try:
        from ..config import settings as _settings_mod
        return int(getattr(_settings_mod, "NOTIFY_COLLAPSE_WINDOW_SEC", _DEFAULT_COLLAPSE_WINDOW))
    except (TypeError, ValueError):
        return 60


def _load_counters() -> Dict[str, Any]:
    if DAILY_COUNTERS_PATH.exists():
        try:
            with open(DAILY_COUNTERS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            from mailbots_next.core.log import get_logger
            _log = get_logger(__name__)
            _log.error(f"Failed to load daily counters: {type(e).__name__}: {e}")
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


def get_counters_snapshot(date: Optional[str] = None) -> Dict[str, int]:
    """取**指定日期**（默认今天）的计数；文件里是别的日期就返回空。

    与 `get_counters()` 的区别：后者不看日期，跨天没写过就会把**昨天**的计数
    当今天返回 —— 每日邮件不能用它（会把昨天的记账失败算到今天）。
    """
    data = _load_counters()
    day = date or datetime.now().strftime("%Y-%m-%d")
    if data.get("date") != day:
        return {}
    return {k: int(v or 0) for k, v in (data.get("counts") or {}).items()}


# ---------------------------------------------------------------- 当日状态（任务 16）
#
# 任务 16 起，原来「逐封弹企微」的东西（未分类 / 配置缺失 / 待办 / 程序错误）
# 改为**记账**，由 12:30 那封每日邮件统一列出。
# 与 daily_counters.json 同目录、同「按自然日分桶」语义：跨天第一次写入自动清空
# 旧日期，不需要任何定时清理任务。

_ISSUES_MAX = 500        # 单日上限，防病态输入把文件写爆
_issues_lock = threading.Lock()


def _load_issues() -> Dict[str, Any]:
    if DAILY_ISSUES_PATH.exists():
        try:
            with open(DAILY_ISSUES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            _log.error(f"Failed to load daily issues: {type(e).__name__}: {e}")
    return {}


def _save_issues(data: Dict[str, Any]) -> None:
    DAILY_ISSUES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _issues_lock:
        with open(DAILY_ISSUES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)


def record_issue(kind: str, detail: str, code: str = "", at: Optional[datetime] = None) -> None:
    """记一条要进每日邮件汇总的项（**只记账，不发任何通知**）。

    kind: unclassified / config_missing / pending / program_error
    code: 细分码（如 no_recipients / no_smtp_credentials），供邮件第三节计数
    绝不抛异常：记账失败不能影响转发主链路。
    """
    try:
        now = at or datetime.now()
        today = now.strftime("%Y-%m-%d")
        data = _load_issues()
        if data.get("date") != today:
            data = {"date": today, "items": []}
        items = data.setdefault("items", [])
        items.append({
            "kind": kind,
            "code": code or "",
            "at": now.strftime("%H:%M"),
            "detail": str(detail or "")[:300],
        })
        if len(items) > _ISSUES_MAX:
            del items[:len(items) - _ISSUES_MAX]
        _save_issues(data)
    except Exception as e:
        _log.error(f"record_issue failed | kind={kind}: {type(e).__name__}: {e}")


def get_issues(date: Optional[str] = None) -> List[Dict[str, Any]]:
    """取指定日期（默认今天）记下的项；文件里是别的日期就返回空列表。"""
    data = _load_issues()
    day = date or datetime.now().strftime("%Y-%m-%d")
    if data.get("date") != day:
        return []
    return list(data.get("items") or [])


def clear_issues(date: Optional[str] = None) -> None:
    """清空指定日期的项（测试隔离用；生产不需要定时清理）。"""
    data = _load_issues()
    day = date or datetime.now().strftime("%Y-%m-%d")
    if data.get("date") == day:
        _save_issues({"date": day, "items": []})


def write_digest_snapshot(counts: Dict[str, int], date: Optional[str] = None) -> None:
    """09:00 flush 之后把「这一段」的计数落地成当日快照。

    flush_counters() 会把计数器清零；每日邮件 12:30 才发，所以必须先落地，
    否则邮件读到的永远是空。**保留 flush 本身**（任务书 1.2）。
    """
    day = date or datetime.now().strftime("%Y-%m-%d")
    DAILY_DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(DAILY_DIGEST_PATH, "w", encoding="utf-8") as f:
            json.dump({"date": day, "counts": dict(counts or {})}, f,
                      ensure_ascii=False, indent=1)
    except Exception as e:
        _log.error(f"write_digest_snapshot failed: {type(e).__name__}: {e}")


def get_digest_snapshot(date: Optional[str] = None) -> Dict[str, int]:
    """读当日 digest 快照；日期不是当天就返回空（绝不拿昨天的充今天）。"""
    day = date or datetime.now().strftime("%Y-%m-%d")
    try:
        if DAILY_DIGEST_PATH.exists():
            with open(DAILY_DIGEST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == day:
                return {k: int(v or 0) for k, v in (data.get("counts") or {}).items()}
    except Exception as e:
        _log.error(f"Failed to load digest snapshot: {type(e).__name__}: {e}")
    return {}


def _mask_addrs(addrs) -> list:
    """收件人掩码 —— 委托 log 的模块级唯一实现，保证全仓同一口径。

    不再调用 EmailLogger 的私有方法：loud fail 就在这条路径上，
    跨模块依赖私有 API 一旦改名，会从"静默失败"变成"崩在告警自身"。
    """
    return mask_recipients(list(addrs))


class WeComNotifier:
    def __init__(self):
        self._client = None
        self._init_client()

    def _init_client(self):
        try:
            from .wecom_notify import WeComHttpClient
            self._client = WeComHttpClient.from_secrets()
            self._notify_by_name = self._client.notify_by_name
        except Exception as e:
            self._client = None
            self._notify_by_name = None
            global _client_unavailable_logged
            if not _client_unavailable_logged:
                _client_unavailable_logged = True
                _log.error(f"WeCom client not available ({type(e).__name__}), "
                           f"notifications will loud-fail with notify_failed counter")

    # 企微发送适配：内部以邮箱标识同事，但现网 wecom_api.notify_by_name 按企微姓名发送。
    # 本表仅作"邮箱→企微姓名"转换用，严禁把姓名当业务键流通到 notify.py 之外。
    # 仓库内唯一允许出现同事真名的位置（纯净铁律例外）。
    _WECOM_NAME_BY_EMAIL = {
        "maoxiaoyang@cqtransit.com": "毛骁洋",
        "yangyawen@cqtransit.com": "杨雅雯",
        "fengqian@cqtransit.com": "冯茜",
        "hanwenhao@cqtransit.com": "韩文豪",
        "ops@example.com": "运维负责人",
    }

    def notify(self, notify_type: str, recipients: List[str], content: str, error_id: Optional[str] = None) -> bool:
        """折叠闸门（薄层）：可折叠类型 + 窗口>0 才走折叠，其余直发（行为与旧完全一致）。"""
        # 先把过期窗口的汇总补发掉（对非折叠类型同样执行：它只清理"已过期"的窗口，
        # 不会为当前这条建立任何 state，也不会递归）。
        self._flush_expired_windows()
        if notify_type not in _COLLAPSIBLE_TYPES or _get_collapse_window() <= 0:
            return self._send_now(notify_type, recipients, content, error_id)

        fp = _fingerprint(notify_type, content)
        now = _now()
        evicted: List[Dict[str, Any]] = []
        with _collapse_lock:
            st = _collapse_state.get(fp)
            if st is None:
                _collapse_state[fp] = {"window_start": now, "count": 1, "recipients": list(recipients),
                                       "notify_type": notify_type, "content": content, "error_id": error_id}
                send_now = True
            else:
                st["count"] += 1
                send_now = False
            while len(_collapse_state) > _COLLAPSE_MAX_ENTRIES:
                oldest = min(_collapse_state, key=lambda k: _collapse_state[k]["window_start"])
                evicted.append(_collapse_state.pop(oldest))
        for est in evicted:
            if est["count"] > 1:
                try:
                    self._send_now(est["notify_type"], est["recipients"],
                                   _collapse_summary(est["content"], est["count"]),
                                   est["error_id"])
                except Exception as e:
                    _log.error(f"Notify collapse evict flush failed: {type(e).__name__}: {e}")
        if send_now:
            _log.info(f"Notify window start | type={notify_type} | fp={fp}")
            return self._send_now(notify_type, recipients, content, error_id)
        with _collapse_lock:
            cur_count = _collapse_state.get(fp, {}).get("count", 0)
        _log.info(f"Notify collapsed | type={notify_type} | fp={fp} | count={cur_count}")
        return True

    def _flush_expired_windows(self) -> None:
        """补发已过期窗口的汇总（count>1 才发），走 _send_now 直发（不进折叠判定 ⇒ 防递归）。

        网络发送一律在锁外；state 读写在锁内。
        """
        window = _get_collapse_window()
        if window <= 0:
            return
        now = _now()
        expired: List[Dict[str, Any]] = []
        with _collapse_lock:
            for fp, st in list(_collapse_state.items()):
                if now - st["window_start"] >= window:
                    expired.append(st)
                    del _collapse_state[fp]
        for st in expired:
            if st["count"] > 1:
                try:
                    self._send_now(st["notify_type"], st["recipients"],
                                   _collapse_summary(st["content"], st["count"]),
                                   st["error_id"])
                except Exception as e:
                    _log.error(f"Notify collapse flush failed: {type(e).__name__}: {e}")

    def _send_now(self, notify_type: str, recipients: List[str], content: str, error_id: Optional[str] = None) -> bool:
        """Send to a list of owner *emails*; each is translated to the WeCom
        display name via _WECOM_NAME_BY_EMAIL before calling notify_by_name.
        Unmapped emails are logged, counted as failed, and escalated to the
        ops owner by SMTP (never silently dropped, never sent raw)."""
        # loud fail（B 组 T3a）：发不出去必留痕 —— ERROR（含类型与脱敏收件人）
        # + notify_failed 计数，再返回 False。不重试、不抛异常（铁律：企微挂了
        # 不能阻断转发主链路）。返回值语义不变（仍是 bool）。
        if not self._notify_by_name:
            _log.error(f"WeCom notify failed: client not available | type={notify_type} "
                       f"to={_mask_addrs(recipients)}", error_id=error_id)
            increment_counter("notify_failed")
            return False

        success_count = 0
        unmapped = []
        for recipient in dict.fromkeys(recipients):      # 去重：同一人只发一次
            name = self._WECOM_NAME_BY_EMAIL.get(recipient)
            if not name:
                _log.error(
                    f"WeCom recipient has no name mapping, escalating: {recipient}",
                    error_id=error_id,
                )
                _log.log_notify("", notify_type, [recipient], False)
                increment_counter("notify_failed")
                unmapped.append(recipient)
                continue
            try:
                client = self._client
                try_kf = client.kf_enabled(recipient) if client is not None else False
                ok, channel = self._notify_by_name(name, content, try_kf)
                if ok:
                    success_count += 1
                else:
                    increment_counter("notify_failed")
                _log.log_notify("", notify_type, [recipient], ok)
            except Exception as e:
                _log.error(f"WeCom notify failed for {recipient}: {e}", error_id=error_id)
                increment_counter("notify_failed")
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

    def send_forwarded(self, responsible_person: str, admin_person: str, detail: str,
                       error_id: Optional[str] = None, cc_admin: bool = True) -> bool:
        """转发成功通知。

        `cc_admin=False` 时**只发负责人、不抄送管理员**（任务 16：运踪成功通知
        不再发给洋）。🔴 必须是"少一个收件人"，不能拿空串/None/伪造邮箱去顶
        管理员那一位 —— `_send_now` 对无企微映射的收件人会走 SMTP 降级邮件发给
        运维负责人（`_fallback_owner_email`），那等于没停。
        """
        content = f"✅ 已转发\n{detail}"
        recipients = [responsible_person]
        if cc_admin:
            recipients.append(admin_person)
        return self.notify("forwarded", recipients, content, error_id)

    def send_program_error(self, admin_person: str, error_id: str, detail: str) -> bool:
        content = f"❌ 程序错误 [error_id={error_id}]\n{detail}"
        # 任务 16：程序错误仍实时发企微（行为不变），同时记账进每日邮件第三节
        # —— 只在"发之前"记一笔，写文件失败也绝不影响企微发送。
        record_issue("program_error", detail, code=str(error_id or ""))
        return self.notify("program_error", [admin_person], content, error_id)

    def send_config_missing(self, admin_person: str, error_id: str, detail: str) -> bool:
        content = f"⚙️ 配置缺失 [error_id={error_id}]\n{detail}"
        return self.notify("config_missing", [admin_person], content, error_id)

    def send_send_check(self, recipients: List[str], detail: str, error_id: Optional[str] = None) -> bool:
        content = f"📮 发送自检\n{detail}"
        return self.notify("send_check", recipients, content, error_id)

    def send_digest(self, admin_person: str, counts: Dict[str, int]) -> bool:
        if not counts:
            return True
        lines = [f"📊 每日汇总 ({datetime.now().strftime('%Y-%m-%d')})"]
        # H2补丁 R1/R2：有记账失败时首行后显著告警（中文自然语言，三要素）
        n = counts.get("post_send_bookkeeping_failed", 0) or 0
        if n:
            lines.append(
                f"⚠️ 注意：今天有 {n} 次「邮件已发出但记账失败」——"
                f"邮件已正常发给客户，不会重发；"
                f"但对应的 DSK/ATB 时间戳或运踪记录可能没写上，请按需人工核对。"
            )
        _LABELS = {
            "post_send_bookkeeping_failed": "发信后记账失败",
            "send_check_skip_no_id": "自检跳过(邮件无匹配键)",
            "send_check_skip_no_sender": "自检跳过(行缺发信账号)",
            "send_check_failed": "自检抓取失败",
            "send_check_alerted": "自检告警条数",
        }
        for k, v in sorted(counts.items()):
            # R3：可读中文标签（键写错就静默显示英文键，所以这里必须用 dict）
            label = _LABELS.get(k, k)
            lines.append(f"  {label}: {v}")
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


def reset_notifier() -> None:
    """Reset the notifier singleton for testing purposes."""
    global _notifier_instance
    with _notifier_lock:
        _notifier_instance = None
    # 折叠 state 一并清空（测试隔离；生产路径不受影响）。
    with _collapse_lock:
        _collapse_state.clear()