# -*- coding: utf-8 -*-
"""forward_builder 用 real_imap_samples 草单原始字节验证重组/标签/测试主题。"""
import email
import glob
import os

from processors import forward_builder

DRAFT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "real_imap_samples")


def _draft_bytes(pattern):
    hits = glob.glob(os.path.join(DRAFT_DIR, pattern))
    assert hits, f"夹具缺失: {pattern}"
    with open(hits[0], "rb") as f:
        return f.read()


def _att_parts(msg):
    return [p for p in msg.walk()
            if not p.is_multipart()
            and ("attachment" in str(p.get("Content-Disposition", "")).lower()
                 or p.get_filename())]


def _html_payload(msg):
    for p in msg.walk():
        if p.get_content_type() == "text/html" and not p.get_filename():
            pl = p.get_payload(decode=True)
            if pl:
                return pl.decode(p.get_content_charset() or "utf-8", errors="replace")
    return None


def test_category_label_table():
    assert forward_builder.CATEGORY_LABEL == {
        "A": "【转草单】", "B": "【草单更新】", "C1": "【反馈问题】",
        "C2": "确认回复(不转发)", "WAY_A": "运单号确认", "WAY_B": "驳回告警",
    }


def test_build_forward_b_subject_prefixed_and_attachments_kept():
    raw = _draft_bytes("draft_319_*.eml")
    orig = email.message_from_bytes(raw)
    n_att = len(_att_parts(orig))
    orig_html = _html_payload(orig)
    fwd, subj = forward_builder.build_forward(raw, "B")
    # 附件数与原件一致
    assert len(_att_parts(fwd)) == n_att
    # HTML 正文存在时保留 HTML 部分（原文完整内嵌）
    out_html = _html_payload(fwd)
    assert out_html and orig_html and orig_html in out_html
    # B 类红色前置提示存在；主题前缀仅 B 类加 CATEGORY_LABEL
    assert out_html.startswith('<p style="color:#c00;font-weight:bold">')
    assert subj and not subj.startswith(forward_builder.CATEGORY_LABEL["B"])
    assert (fwd.get("Subject") or "").startswith(forward_builder.CATEGORY_LABEL["B"])
    assert (fwd.get("Subject") or "").endswith(subj)


def test_build_forward_extra_note_at_body_head():
    raw = _draft_bytes("draft_319_*.eml")
    note = "E2E-EXTRA-NOTE-MARKER"
    fwd, _subj = forward_builder.build_forward(raw, "B", extra_note=note)
    out_html = _html_payload(fwd)
    assert out_html is not None
    # extra_note 拼接在红色提示段内、位于原文之前
    assert out_html.index(note) < out_html.index('<br>' if '<br>' in out_html else len(out_html))
    assert note in out_html.split("</p>")[0]


def test_build_forward_a_no_subject_prefix():
    raw = _draft_bytes("draft_319_*.eml")
    fwd, subj = forward_builder.build_forward(raw, "A")
    assert subj and not subj.startswith(forward_builder.CATEGORY_LABEL["A"])
    # 非 B 类不加前缀：旧语义由调用方设置 Subject，此处保持未设
    assert fwd.get("Subject") is None or not (fwd.get("Subject") or "").startswith("【")


def test_test_subject_format():
    s = forward_builder.test_subject("原始主题", "a@x.com", "【转草单】")
    assert s == "[测试·【转草单】→原收件人:a@x.com] 原始主题"
