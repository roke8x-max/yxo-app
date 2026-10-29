# -*- coding: utf-8 -*-
"""DSK 提取器「正文表格优先」测试（任务 07）。

根因：`extract()` 原先只从「附件名 + 主题」取箱号，`if not box_nos: return []`
写在正文表格兜底之前 ⇒ 箱号只写在正文表格里的 AGROSTOP 通知类邮件整封被丢弃。

对齐口径：老系统 `mailbots/Dsk_Robot.py:666` `box_list = [c[0] for c in container_rows]`
—— 正文表格是权威来源，附件名只在 `no_table` 告警文案里出现。

夹具全部为构造 HTML，不依赖真实邮件 / 外部库（`conftest.py` 已把 DB 指到临时目录）。
`extract(msg, parsed)` 只从 `parsed` 读 subject / sender / html_body / attachments 四个键，
`msg` 参数在本函数内未使用 ⇒ 传 None 即可。
"""
from mailbots_next.core.extract import ExtractedRow
from mailbots_next.core.extractors.dsk import DSKExtractor

# 场景 1 / 8 共用夹具：明细只在正文表格，附件名与主题都没有箱号
SUBJECT_AGROSTOP_435 = "DSK / AGROSTOP / YXO-435 - 04.06.2026 -"
SENDER_KASA = "Kasa | RTSB GmbH <kasa@rtsb.de>"
HTML_ONE_ROW = (
    "<table><tr><td>FWRU0072389</td><td>26PL30102D001QHOU0</td></tr></table>"
)


def _parsed(subject, sender, html_body, attachments=None):
    return {
        "subject": subject,
        "sender": sender,
        "html_body": html_body,
        "attachments": attachments or [],
    }


def test_1_table_only_boxes_are_extracted_for_kasa():
    """#1 发件人 kasa@rtsb.de；箱号只在正文表格；附件名与主题都无箱号。"""
    parsed = _parsed(SUBJECT_AGROSTOP_435, SENDER_KASA, HTML_ONE_ROW)

    rows = DSKExtractor().extract(None, parsed)

    assert len(rows) == 1
    assert isinstance(rows[0], ExtractedRow)
    assert rows[0].container_no == "FWRU0072389"


def test_2_table_only_boxes_are_extracted_for_non_kasa_sender():
    """#2 发件人 l.dragun@rtsb.de（非 kasa）；箱号只在正文表格。

    修复前该封被判 sender_type == "unknown"，即使有表格也不会走表格分支。
    """
    parsed = _parsed(
        "ODP: YXO-433 / DSK / AGROSTOP / 31.05.2026",
        "Dragun | RTSB GmbH <l.dragun@rtsb.de>",
        "<table><tr><td>FWRU0192023</td><td>26PL30102D002QHOU0</td></tr></table>",
    )

    rows = DSKExtractor().extract(None, parsed)

    assert len(rows) == 1
    assert rows[0].container_no == "FWRU0192023"


def test_3_table_wins_over_attachment_box_no():
    """#3 正文表格有箱号 且 附件名里有另一个箱号 → 以表格为准，不求并集。"""
    parsed = _parsed(
        SUBJECT_AGROSTOP_435,
        SENDER_KASA,
        HTML_ONE_ROW,
        [{"payload": b"", "filename": "CICU5556940.PDF",
          "content_type": "application/pdf"}],
    )

    rows = DSKExtractor().extract(None, parsed)

    assert [r.container_no for r in rows] == ["FWRU0072389"]


def test_4_attachment_box_no_used_when_no_table():
    """#4 正文无表格；箱号只在附件名 → 用附件名的箱号。"""
    parsed = _parsed(
        "DSK notification",
        SENDER_KASA,
        None,
        [{"payload": b"", "filename": "CICU5556940.PDF",
          "content_type": "application/pdf"}],
    )

    rows = DSKExtractor().extract(None, parsed)

    assert [r.container_no for r in rows] == ["CICU5556940"]


def test_5_subject_box_no_used_when_no_table():
    """#5 正文无表格；箱号只在主题 → 用主题的箱号。"""
    parsed = _parsed("DSK notification FWRU0042527", SENDER_KASA, None)

    rows = DSKExtractor().extract(None, parsed)

    assert [r.container_no for r in rows] == ["FWRU0042527"]


def test_6_no_box_no_row():
    """#6 正文无表格、附件名无箱号、主题无箱号 → 返回 []。"""
    parsed = _parsed("DSK notification", SENDER_KASA, None)

    rows = DSKExtractor().extract(None, parsed)

    assert rows == []


def test_7_duplicate_box_no_in_table_yields_one_row():
    """#7 表格里同一箱号出现两行 → 去重成 1 行。"""
    html = (
        "<table>"
        "<tr><td>CICU5556940</td><td>26PL30102D003QHOU0</td></tr>"
        "<tr><td>CICU5556940</td><td>26PL30102D004QHOU0</td></tr>"
        "</table>"
    )
    parsed = _parsed(
        "ODP: YXO-437 / DS / AGROSTOP - 17.06.2026", SENDER_KASA, html
    )

    rows = DSKExtractor().extract(None, parsed)

    assert len(rows) == 1
    assert rows[0].container_no == "CICU5556940"


def test_8_raw_data_keeps_sender_type_and_full_container_rows():
    """#8 raw_data 里保留 sender_type 与完整 container_rows（含关联单号）。"""
    parsed = _parsed(SUBJECT_AGROSTOP_435, SENDER_KASA, HTML_ONE_ROW)

    rows = DSKExtractor().extract(None, parsed)

    rd = rows[0].raw_data
    assert rd["sender_type"] == "kasa"
    assert len(rd["container_rows"]) == 1
    assert rd["container_rows"][0][0] == "FWRU0072389"
    assert rd["container_rows"][0][1] == "26PL30102D001QHOU0"
