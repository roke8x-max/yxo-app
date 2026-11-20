"""下游回复草单识别（任务 24）。判据唯一定义处，别在 serve.py 里再写一份。

背景（02_TASK.md §1.2）：识别入口是 serve.MailProcessor._identify_type()，草单规则的
subject_pattern 走 re.search 子串匹配 ⇒ 前缀在前面**不影响命中**；真正卡住的是同一条
规则还带 sender_domain_whitelist=['yxologistics.com']，下游客户域不命中 ⇒ 整封判
UNCLASSIFIED ⇒ 进 error_queue ⇒ sweeper 反复重放 ⇒ 给运维发「程序错误」告警。

所以本模块只做「识别失败之后的兜底分流」：剥掉回复/转发前缀后，主题仍是完整草单骨架
（运单草单 + YXO-订单号 + 完整客编），且发件人域不是 yxo（yxo 是上游）⇒ 判为下游回复。
🔴 绝不放宽 TYPE_ROUTES 的域白名单 —— 那会把下游回复认成正常 DRAFT 自动转发（丢件/错发）。

纯函数：不读实例状态、不碰数据库，便于单测。
"""
import re
from typing import Optional, Tuple

from mailbots_next.config import CODE_RE, YXO_DOMAIN
from mailbots_next.core.sender_addr import normalize_sender_domain

# 循环剥前缀；大小写不敏感，中英文冒号都认。按 02_TASK.md §三.1 逐字保持。
_REPLY_PREFIX_RE = re.compile(
    r"^(?:re|fwd|fw|回复|答复|转发)\s*[:：]\s*", re.I
)
_ORDER_RE = re.compile(r"YXO-\d{4}-\d+")
# 箱号：02_TASK.md §4.1 写的是 \b[A-Z]{4}\d{7}\b，但 \b 在 "…-BLLST_TEFU4005959" 上
# **恒不命中**（"_" 也是 \w，"T" 前没有词边界）⇒ 三封真实主题的箱号全取不到。
# 改用 lookaround 表达同一语义（前后不能粘连字母/数字），"_" 当分隔符 —— 实测三封
# 真实主题均能取到，且 "XXTEFU4005959" 仍不误命中。取不到仍返回 ""，不阻塞进人工。
_BOX_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{4}\d{7}(?![A-Za-z0-9])")


def strip_reply_prefixes(subject: str) -> str:
    """循环剥掉回复/转发前缀，直到不再变化（不是只剥一次）。"""
    s = subject or ""
    while True:
        new = _REPLY_PREFIX_RE.sub("", s).strip()
        if new == s:
            return s
        s = new


def parse_draft_reply_ids(subject: str) -> Tuple[str, str]:
    """返回 (客编, 箱号)；取不到分别返回空串，不抛异常。

    客编用 config.settings.CODE_RE（全仓同一份），箱号取不到不影响进人工。
    """
    s = strip_reply_prefixes(subject)
    m = CODE_RE.search(s)
    code = m.group(0) if m else ""
    b = _BOX_RE.search(s)
    return code, (b.group(0) if b else "")


def is_downstream_draft_reply(sender: str, subject: str) -> bool:
    """是否下游回复的草单：剥前缀后含完整草单骨架，且发件人域非 yxo。

    第二道保险：本函数只在 _identify_type 返回 None 之后被调用，yxo 域邮件本就已被
    DRAFT 规则认出、到不了这里；域判断仍保留，防止将来调用点被挪动后误伤上游。

    域比较必须**精确相等**（normalize_sender_domain 已小写去尖括号）：用 `in` 子串
    会让 notyxologistics.com 误命中 ⇒ 上游误判成下游。
    """
    s = strip_reply_prefixes(subject)
    if "运单草单" not in s or not _ORDER_RE.search(s) or not CODE_RE.search(s):
        return False
    domain = normalize_sender_domain(sender)
    return bool(domain) and domain != YXO_DOMAIN


__all__ = [
    "strip_reply_prefixes",
    "parse_draft_reply_ids",
    "is_downstream_draft_reply",
]
