"""还箱令提取器。一封邮件只产 1 行（row_idx=0）。

三个必须照抄、不许"优化"的点（都踩过坑）：
1. 不能用 `\\b` 做边界。老模板是「您的箱号**为**CICU5557232**的**集装箱」，
   中文紧邻字母时 `\\b` 不成立（中文也算 \\w），`\\b([A-Z]{4}\\d{7})\\b` 完全匹配不到。
   必须 lookaround。
2. `RE_DOC` 不能把 `\\s` 放进排除集。真实文件名含空格
   （`957班Релиз приема_ЕО Электроугли25_МТТ(8)`），加 `\\s` 会截成 `957班Релиз`。
   用非贪婪 + 终止断言。
3. `train_no` 取 `group(2)`。`YXO-(\\d{4})-(\\d+)` 的 group(1) 是年份 2026，
   用它会得 `WB2026`；正确是 `f"WB{m.group(2)}"` → `WB957`。
"""
import re
from email.message import Message
from typing import Any, Dict, List

from mailbots_next.config import CODE_NUM_RE, EmailType
from mailbots_next.core.extract import BaseExtractor, ExtractedRow
from mailbots_next.core.extractors.release import build_attachments, normalize_body_text
from mailbots_next.core.log import get_logger

_log = get_logger(__name__)

RE_CODE = re.compile(r"(?<![0-9A-Za-z])(CQWLJT[0-9A-Za-z]+-[0-9A-Za-z]+)(?![0-9A-Za-z])")
RE_BOX = re.compile(r"(?<![A-Za-z0-9])([A-Z]{4}\d{7})(?![0-9])")
RE_DOC = re.compile(r"还箱令为\s*[：:]\s*(.{1,80}?)(?=[，,。；;]|\s+点击|$)")
RE_LINK = re.compile(r"(https?://[^\s，,。；;）)]+)")
RE_TRAIN = re.compile(r"YXO-(\d{4})-(\d+)")

BODY_TEXT_MAX = 2000


class RetboxExtractor(BaseExtractor):
    def extract(self, msg: Message, parsed: Dict[str, Any]) -> List[ExtractedRow]:
        subject = parsed["subject"] or ""
        txt = normalize_body_text(parsed)
        message_id = (msg.get("Message-ID") or "").strip()

        # 客编：老模板（2026-04 之前）没有客编，为 None 属正常，走箱号兜底
        codes = RE_CODE.findall(txt)
        customer_code = codes[0].strip() if codes else None
        if len(codes) > 1:
            _log.warning(
                f"Retbox: {len(codes)} customer_code found, use first | "
                f"msg_id={message_id[:50]} | code={customer_code}"
            )

        # 箱号靠 lookaround 抓，不能改回 \b（中文紧邻字母时匹配不到）
        boxes = RE_BOX.findall(txt)
        container_no = boxes[0] if boxes else None
        if not boxes:
            _log.warning(f"Retbox: container_no not found | msg_id={message_id[:50]}")

        if not customer_code and not container_no:
            # 客编与箱号都没有：无从落档，返回空列表交上层入队
            _log.warning(f"Retbox: no customer_code and no container_no | msg_id={message_id[:50]}")
            return []

        customer_code_num = None
        if customer_code:
            m_code = CODE_NUM_RE.match(customer_code)
            if m_code:
                customer_code_num = m_code.group(1)

        retbox_doc = None
        m_doc = RE_DOC.search(txt)
        if m_doc:
            retbox_doc = m_doc.group(1).strip()

        link = None
        m_link = RE_LINK.search(txt)
        if m_link:
            link = m_link.group(1)

        # 主路径取主题；主题没有时从正文兜底。两条路都必须 group(2)
        train_no = None
        m_train = RE_TRAIN.search(subject)
        if m_train:
            train_no = f"WB{m_train.group(2)}"
        if not train_no:
            m_train_body = RE_TRAIN.search(txt)
            if m_train_body:
                train_no = f"WB{m_train_body.group(2)}"

        raw_data = {
            "message_id": message_id,
            "release_no": None,
            "code_no": None,
            "validity": "",
            "tail_note": "",
            "is_cancel": False,
            "attachments": build_attachments(parsed),
            "link": link,
            "retbox_doc": retbox_doc,
            "body_text": txt[:BODY_TEXT_MAX],
        }
        if len(codes) > 1:
            raw_data["multi_code"] = True

        row = ExtractedRow(
            row_idx=0,
            email_type=EmailType.RETBOX,
            customer_code=customer_code,
            customer_code_num=customer_code_num,
            container_no=container_no,
            train_no=train_no,
            raw_data=raw_data,
        )
        return [row]
