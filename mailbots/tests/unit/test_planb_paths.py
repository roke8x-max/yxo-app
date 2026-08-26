# -*- coding: utf-8 -*-
"""paths 网关与 Processor 协议。"""
import os
import pytest
from core import paths
from core.models import MailEvent, ProcessResult, Processor


@pytest.fixture(autouse=True)
def _reset_root_cache(monkeypatch):
    """detect_root 缓存与 YXO_ROOT env 不跨用例污染。
    monkeypatch 只还原自身改动，故 env 清除与缓存重置都要在此显式做：
    开发/CI 若导出 YXO_ROOT，会让 candidates 注入类用例失效（env 在 candidates 之前返回）。"""
    monkeypatch.delenv("YXO_ROOT", raising=False)
    paths._cached_root = None
    yield
    paths._cached_root = None


def test_detect_root_picks_candidate_with_wecombot(tmp_path):
    # 候选1 无 WeComBot\config.py；候选2 有 → 返回候选2
    cand2 = tmp_path / "root2"
    (cand2 / "WeComBot").mkdir(parents=True)
    (cand2 / "WeComBot" / "config.py").write_text("", encoding="utf-8")
    got = paths.detect_root(candidates=[str(tmp_path / "root1"), str(cand2)])
    assert got == str(cand2)


def test_detect_root_raises_when_none(tmp_path):
    with pytest.raises(paths.StartupError):
        paths.detect_root(candidates=[str(tmp_path)])


def test_load_accounts_never_raises(monkeypatch, tmp_path):
    # YXO_ROOT 注入避免探测真实候选（含 UNC 网络路径超时）
    monkeypatch.setenv("YXO_ROOT", str(tmp_path))
    assert isinstance(paths.load_accounts(), dict)


def test_env_override_and_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("YXO_ROOT", str(tmp_path))
    assert paths.detect_root() == str(tmp_path)
    assert paths.detect_root(candidates=[str(tmp_path / "x")]) == str(tmp_path)


def test_detect_root_negative_not_cached(monkeypatch, tmp_path):
    # 负结果不写缓存：两次无命中调用都完整重探（若负缓存存在，第二次将短路不再触发 isfile）
    monkeypatch.setattr(paths, "_CANDIDATE_ROOTS",
                        [str(tmp_path / "a"), str(tmp_path / "b")])
    probes = []
    real_isfile = os.path.isfile

    def counting_isfile(path):
        probes.append(path)
        return real_isfile(path)

    monkeypatch.setattr(paths.os.path, "isfile", counting_isfile)
    with pytest.raises(paths.StartupError):
        paths.detect_root()
    with pytest.raises(paths.StartupError):
        paths.detect_root()
    assert len([p for p in probes if p.startswith(str(tmp_path))]) == 4


def test_process_result_and_protocol():
    ev = MailEvent("a@cqtransit.com", "草单运单号", "<m@x>", "1", "s", "f@d", "", "")
    pr = ProcessResult(event=ev, action="record")
    assert pr.tier is None and pr.route == ()
    assert {"can_handle", "process"} <= set(Processor.__protocol_attrs__)
