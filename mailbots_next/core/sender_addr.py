"""发件人地址规范化 —— 入口规则与 DraftExtractor 共用的唯一实现。

为什么必须只有一份：识别入口（serve._identify_type）与分类
（core/extractors/draft.py）都靠「发件人域」判断，两份实现早晚漂移；
只改一处会出现「识别进来了、分类还是 C2」的上线故障（任务 13）。

为什么不能用 sender.split("@")[-1]：parse_email() 的 sender 是
msg.get("From")，str() 出来是**完整 From 头**（例如
`"chenlong@yxologistics.com" <chenlong@yxologistics.com>`），直接 split
拿到的是 `yxologistics.com>`（带尖括号）⇒ 与 YXO_DOMAIN 不相等 ⇒
内部回复被误判成 C2 留人工。这里统一用 email.utils.parseaddr() 取纯地址。

纯函数：不依赖网络 / 数据库 / 全局配置；取不到域返回 ""，不抛异常。
"""
from email.utils import parseaddr

__all__ = ["normalize_sender_domain"]


def normalize_sender_domain(sender) -> str:
    """取发件人域名（小写、已去尖括号与空白）；取不到返回 ""。

    sender 可以是 str、_UniqueAddressHeader（msg.get("From") 的返回）或 None。
    """
    text = str(sender or "").strip()
    if not text:
        return ""

    _name, addr = parseaddr(text)
    addr = (addr or "").strip()
    if "@" not in addr:
        # parseaddr 对残缺头（尾随 ">"、"<" 不配对等）会返回空串：
        # 原文里还有 @ 就用原文再取一次，仍然取不到才判失败。
        if "@" not in text:
            return ""
        addr = text

    at = addr.rfind("@")
    # 去掉残留的尖括号 / 空白，再统一小写
    return addr[at + 1:].strip().strip("<>").strip().lower()
