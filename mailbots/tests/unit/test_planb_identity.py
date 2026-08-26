# -*- coding: utf-8 -*-
"""identity：公司关键词 → 发件身份映射 + 覆盖层；WeComBot 缺席时静默降级。"""
import json
import pytest
from core import identity
from core import paths


@pytest.fixture(autouse=True)
def _clean_override(monkeypatch):
    """_override 为模块级全局，逐用例复位避免跨用例污染。"""
    monkeypatch.setattr(identity, "_override", None)


def test_override_json_takes_priority(tmp_path):
    # 覆盖层形状 {"公司关键词": "邮箱"}：命中关键词即返回注入邮箱
    p = tmp_path / "override.json"
    p.write_text(
        json.dumps({"港九港铁": "maoxiaoyang@cqtransit.com"}, ensure_ascii=False),
        encoding="utf-8")
    identity.override_json(str(p))
    email, name = identity.sender_for("港九港铁子公司")
    assert email == "maoxiaoyang@cqtransit.com"
    assert name is None


def test_unmatched_company_returns_none(monkeypatch):
    monkeypatch.setattr(identity, "_wecombot_maps", lambda: ({}, {}))
    assert identity.sender_for("查无此公司") == (None, None)


def test_wecombot_keyword_substring_match(monkeypatch):
    # 关键词子串匹配与现役行为一致：公司名包含关键词即命中
    monkeypatch.setattr(
        identity, "_wecombot_maps",
        lambda: ({"港铁": "a@cqtransit.com"}, {"港铁": "张三"}))
    email, name = identity.sender_for("XX港铁YY子公司")
    assert email == "a@cqtransit.com" and name == "张三"


def test_real_name_of(monkeypatch):
    monkeypatch.setattr(
        identity, "_wecombot_maps",
        lambda: ({"德迅": "b@cqtransit.com"}, {"德迅": "李四"}))
    assert identity.real_name_of("德迅物流") == "李四"
    assert identity.real_name_of("无名氏") is None


def test_wecombot_maps_degrade_without_config(monkeypatch, tmp_path):
    # 本机无 WeComBot config：YXO_ROOT 指向空目录 → detect_root 成功但
    # import config 失败 → 吞掉异常返回空表（绝不向调用方抛错）
    monkeypatch.setenv("YXO_ROOT", str(tmp_path))
    kw_email, kw_name = identity._wecombot_maps()
    assert kw_email == {} and kw_name == {}


def test_wecombot_maps_degrade_on_startup_error(monkeypatch):
    # detect_root 抛 StartupError（候选全空）同样静默降级为空表
    monkeypatch.delenv("YXO_ROOT", raising=False)
    monkeypatch.setattr(paths, "_CANDIDATE_ROOTS", [])
    monkeypatch.setattr(paths, "_cached_root", None)
    kw_email, kw_name = identity._wecombot_maps()
    assert kw_email == {} and kw_name == {}
