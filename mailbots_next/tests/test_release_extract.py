"""放箱令 / 取消放箱令 / 还箱令 —— 识别与提取（任务 09 第一阶段）。

数据用 `tests/data/release_eml/` 下 6 封真实脱敏样本，不自己编字符串。
脱敏说明：PDF 附件被换成 917 字节占位文件，**文件名与 content_type 保留**，
所以附件断言只断数量 / 文件名 / content_type，不断内容。

⚠️ 三条新规则是 `enabled: False`（第一阶段停用）。识别层用例必须在
「临时启用」夹具下跑，否则 `_identify_type` 对任何输入都返回 None ——
反例用例会"永远绿"但根本没测到主题逻辑，正例则永远红（假测试）。
"""
from unittest.mock import Mock

import pytest

from mailbots_next.config import EmailType
from mailbots_next.core.extract import extract_email

EML_DIR = "data/release_eml"

R_A = "01_放箱令_模板A_带null.eml"
R_B = "02_放箱令_模板B_带有效期.eml"
R_CANCEL = "03_取消放箱令.eml"
RB_A = "04_还箱令_模板A_俄文文件名.eml"
RB_C = "05_还箱令_模板C_QQ发件人.eml"
RB_OLD = "06_还箱令_老模板_无客编.eml"


def _raw(name):
    from pathlib import Path
    return (Path(__file__).parent / EML_DIR / name).read_bytes()


def _extract(name, email_type):
    rows = extract_email(email_type, _raw(name))
    assert len(rows) == 1, f"{name}: 一封只产 1 行，实际 {len(rows)}"
    return rows[0]


@pytest.fixture
def routes_enabled():
    """临时把三条新规则的 enabled 改成 True，测完恢复原值。

    ⚠️ 必须同时清 `provider` 的运行时缓存。`get_enabled_types()` 有 60 秒 TTL，
    若在规则「打开」期间被填充过一次，恢复 `TYPE_ROUTES` 后缓存仍返回 True，
    后续测试（如 `test_core.py::test_enabled_types_defaults`）就会按执行顺序飘红。
    项目既有测试（test_h2_h3 / test_p1_coverage / test_bounce_e2e）同样先清缓存，
    此处保持一致。
    """
    from mailbots_next.config.provider import invalidate_cache
    from mailbots_next.config.types import TYPE_ROUTES
    new_types = (EmailType.RELEASE, EmailType.RELEASE_CANCEL, EmailType.RETBOX)
    saved = [(r, r.get("enabled")) for r in TYPE_ROUTES if r["type"] in new_types]
    for r, _ in saved:
        r["enabled"] = True
    invalidate_cache()
    try:
        yield
    finally:
        for r, old in saved:
            r["enabled"] = False if old is None else old
        invalidate_cache()


class TestReleaseExtract:
    def test_template_a_with_null_tail(self):
        row = _extract(R_A, EmailType.RELEASE)
        assert row.email_type == EmailType.RELEASE
        assert row.customer_code == "CQWLJT261108051-VXN"
        assert row.customer_code_num == "261108051"
        assert row.raw_data["release_no"] == "0156-33596910-1"
        assert row.raw_data["code_no"] == "49"
        assert row.raw_data["tail_note"] == ""
        assert row.raw_data["validity"] == ""
        assert row.raw_data["is_cancel"] is False
        assert row.raw_data["message_id"] == "<787337603.1423.1793343110430@localhost>"
        atts = row.raw_data["attachments"]
        assert len(atts) == 1
        assert "0156-33596910-1" in atts[0]["filename"]
        assert atts[0]["content_type"] == "application/pdf"
        assert atts[0]["size"] == len(atts[0]["payload"])
        assert atts[0]["size"] > 0

    def test_template_b_with_validity(self):
        row = _extract(R_B, EmailType.RELEASE)
        assert row.customer_code == "CQWLJT261111005-MA"
        assert row.raw_data["release_no"] == "YXOTR20261030101"
        assert row.raw_data["code_no"] == "10"
        # 提取层不加空格；加空格是渲染层（spec 03）的事
        assert row.raw_data["validity"] == "10月30日到11月1日"
        assert " " not in row.raw_data["validity"]
        assert row.raw_data["tail_note"] != ""
        assert row.raw_data["tail_note"].endswith("10月30日到11月1日")
        assert len(row.raw_data["attachments"]) == 1

    def test_cancel_plain_body(self):
        # 这封是纯 text/plain，走 plain 分支，同样 unescape
        row = _extract(R_CANCEL, EmailType.RELEASE_CANCEL)
        assert row.email_type == EmailType.RELEASE_CANCEL
        assert row.raw_data["is_cancel"] is True
        assert row.customer_code == "CQWLJT261005003-Kol"
        assert row.raw_data["release_no"] == "YXOCZ20260918101"
        assert row.raw_data["code_no"] == "8"
        assert row.raw_data["attachments"] == []
        assert row.raw_data["tail_note"] == ""
        assert row.raw_data["body_text"].startswith("您好!")


class TestRetboxExtract:
    def test_template_a_ru_filename(self):
        row = _extract(RB_A, EmailType.RETBOX)
        assert row.email_type == EmailType.RETBOX
        assert row.customer_code == "CQWLJT261015003-DMZ"
        assert row.customer_code_num == "261015003"
        assert row.container_no == "XHCU5271865"
        assert row.train_no == "WB957"
        # 文件名含空格，不能被截断成 "957班Релиз"
        assert row.raw_data["retbox_doc"] == "957班Релиз приема_ЕО Электроугли25_МТТ(8)"
        assert "Электроугли" in row.raw_data["retbox_doc"]
        assert row.raw_data["link"].startswith("https://365.kdocs.cn/")

    def test_template_c_qq_sender(self):
        row = _extract(RB_C, EmailType.RETBOX)
        assert row.customer_code == "CQWLJT260402010-DMZ"
        assert row.container_no == "TBJU7482194"
        assert row.train_no == "WB327"
        assert "kdocs.cn" in row.raw_data["link"]
        assert row.raw_data["retbox_doc"] == "327班中铁柜还箱令Релиз приема_Купавна_6"

    def test_old_template_without_customer_code(self):
        row = _extract(RB_OLD, EmailType.RETBOX)
        assert row.customer_code is None
        # 空值守卫：CODE_NUM_RE.match(None) 会抛 TypeError，必须不炸
        assert row.customer_code_num is None
        assert row.container_no == "CICU5557232"
        assert row.train_no is None
        assert row.raw_data["retbox_doc"] == "HPYXOIN260402B"


class TestIdentifyType:
    def _proc(self):
        from mailbots_next.config import snapshot
        from mailbots_next.serve import MailProcessor
        proc = MailProcessor(snapshot())
        proc.notifier = Mock()
        return proc

    def test_forwarded_release_not_matched(self, routes_enabled):
        # 罗桦转发件：只看主题会重复转发，必须被发件人白名单挡掉
        proc = self._proc()
        assert proc._identify_type(
            "JUDYLUO <luohua@yxologistics.com>", "Fw: 渝新欧放箱", b"") is None

    def test_cancel_without_ling_not_matched(self, routes_enabled):
        # 「取消放箱」缺「令」：cancel 不匹配，release 又被 subject_exclude 挡掉
        proc = self._proc()
        assert proc._identify_type(
            "shidong@yxologistics.com", "取消放箱", b"") is None

    def test_release_matched(self, routes_enabled):
        proc = self._proc()
        assert proc._identify_type(
            "shidong@yxologistics.com", "渝新欧放箱", b"") == "release"

    def test_release_cancel_matched(self, routes_enabled):
        proc = self._proc()
        assert proc._identify_type(
            "shidong@yxologistics.com", "渝新欧取消放箱令", b"") == "release_cancel"

    def test_retbox_matched(self, routes_enabled):
        proc = self._proc()
        assert proc._identify_type(
            "JUDYLUO <luohua@yxologistics.com>", "YXO-2026-957 还箱令", b"") == "retbox"


class TestRulesDisabledByDefault:
    """三条新规则默认停用：不启用时任何输入都不该被认成这三类。"""

    def test_new_types_disabled(self):
        from mailbots_next.config.types import TYPE_ROUTES
        new_types = (EmailType.RELEASE, EmailType.RELEASE_CANCEL, EmailType.RETBOX)
        for r in TYPE_ROUTES:
            if r["type"] in new_types:
                assert r["enabled"] is False, r["type"]
