"""放箱令 / 取消放箱令提取器。

两类邮件正文结构相同，只差「已分配 / 已取消分配」，因此共用一个提取器。
一封邮件只产 1 行（row_idx=0）。

正则与取法均按 spec 01 定稿，**不要"优化"**：
- RE_PAIR 的锚点是「放箱令：」，不能写「已分配放箱令」——取消件是「已取消分配放箱令」
- RE_PAIR 的两个 group 都要 strip——原文放箱令号带尾随空格（`【0156-33596910-1 】`）
- validity 提取结果**不加空格**（实测 `"10月30日到11月1日"`），加空格是渲染层的事
"""
import html
import re
from email.message import Message
from typing import Any, Dict, List

from mailbots_next.config import CODE_NUM_RE, EmailType
from mailbots_next.core.extract import BaseExtractor, ExtractedRow
from mailbots_next.core.log import get_logger

_log = get_logger(__name__)

RE_CODE = re.compile(r"客户编码\s*【\s*(CQWLJT[^】\s]{4,})\s*】")
RE_PAIR = re.compile(r"放箱令\s*[：:]\s*【\s*([^】]{2,40}?)\s*】\s*【\s*([^】]{0,10}?)\s*】")
RE_VALID = re.compile(r"有效期\s*(\d{1,2}月\d{1,2}日\s*到\s*\d{1,2}月\d{1,2}日)")

# 三种签名都要留：正文已用 \s+ 归一化，全角空格 \u3000 会变成半角空格，
# 只写 "石　东" 永远匹配不到。
SIGNATURES = ("石东", "石 东", "石　东")

BODY_TEXT_MAX = 2000


def normalize_body_text(parsed: Dict[str, Any]) -> str:
    """取正文（优先 html，回退 plain），去标签 + unescape + 空白归一化。

    plain 分支同样要 unescape：纯文本里也可能有字面 `&nbsp;`。
    已知副作用：纯文本里若有字面 `&amp;` 会被还原成 `&`，本期接受。
    """
    html_body = parsed.get("html_body")
    if html_body:
        text = html.unescape(re.sub(r"<[^>]+>", " ", html_body))
    else:
        text = html.unescape(parsed.get("plain_body") or "")
    return re.sub(r"\s+", " ", text).strip()


def build_attachments(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """parsed["attachments"] 只有 {payload, filename, content_type}，这里补 size。

    payload 是 bytes，**只在内存里传给下游打包**，不许落库、不许 JSON 序列化。
    """
    out = []
    for att in parsed.get("attachments") or []:
        payload = att.get("payload") or b""
        out.append({
            "filename": att.get("filename") or "",
            "content_type": att.get("content_type") or "",
            "size": len(payload),
            "payload": payload,
        })
    return out


class ReleaseExtractor(BaseExtractor):
    def extract(self, msg: Message, parsed: Dict[str, Any]) -> List[ExtractedRow]:
        subject = parsed["subject"] or ""
        txt = normalize_body_text(parsed)
        message_id = (msg.get("Message-ID") or "").strip()

        codes = RE_CODE.findall(txt)
        if not codes:
            # 抓不到客编：返回空列表，由上层 EMPTY_ROWS 机制入队
            _log.warning(f"Release: customer_code not found | msg_id={message_id[:50]}")
            return []
        customer_code = codes[0].strip()
        if len(codes) > 1:
            _log.warning(
                f"Release: {len(codes)} customer_code found, use first | "
                f"msg_id={message_id[:50]} | code={customer_code}"
            )

        customer_code_num = None
        if customer_code:
            m_code = CODE_NUM_RE.match(customer_code)
            if m_code:
                customer_code_num = m_code.group(1)

        # 多票防御：实测 550 封均单票，这里只用第一个 + WARN。
        # 用 finditer 而非 findall，是为了同时拿到 pair.end() 供 tail_note 截取。
        pairs = list(RE_PAIR.finditer(txt))
        release_no = None
        code_no = None
        pair = pairs[0] if pairs else None
        if pair:
            release_no = pair.group(1).strip()
            code_no = pair.group(2).strip()
            if len(pairs) > 1:
                _log.warning(
                    f"Release: {len(pairs)} release pairs found, use first | "
                    f"msg_id={message_id[:50]} | release_no={release_no}"
                )

        validity = ""
        m_valid = RE_VALID.search(txt)
        if m_valid:
            validity = m_valid.group(1)

        tail_note = ""
        if pair:
            after = txt[pair.end():].strip()
            for sig in SIGNATURES:
                if after.endswith(sig):
                    after = after[: -len(sig)].strip()
                    break
            # 模板①尾巴是 "null 石东"，null 是发件人模板的空字段，丢弃
            tail_note = "" if after.lower() in ("", "null") else after

        # 类型与 is_cancel 都只认主题；正文与主题矛盾时只 WARN，不改判
        is_cancel = "取消" in subject
        body_cancel = "已取消分配" in txt
        if is_cancel != body_cancel:
            _log.warning(
                f"Release: subject/body cancel mismatch | subject={subject[:50]} | "
                f"is_cancel={is_cancel} | body_cancel={body_cancel} | msg_id={message_id[:50]}"
            )

        raw_data = {
            "message_id": message_id,
            "release_no": release_no,
            "code_no": code_no,
            "validity": validity,
            "tail_note": tail_note,
            "is_cancel": is_cancel,
            "attachments": build_attachments(parsed),
            "link": None,
            "retbox_doc": None,
            "body_text": txt[:BODY_TEXT_MAX],
        }
        if len(codes) > 1:
            raw_data["multi_code"] = True

        row = ExtractedRow(
            row_idx=0,
            email_type=EmailType.RELEASE_CANCEL if is_cancel else EmailType.RELEASE,
            customer_code=customer_code,
            customer_code_num=customer_code_num,
            container_no=None,
            train_no=None,
            raw_data=raw_data,
        )
        return [row]
