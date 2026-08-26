# -*- coding: utf-8 -*-
"""paths 网关与 Processor 协议。"""
import os
import pytest
from core import paths
from core.models import MailEvent, ProcessResult, Processor


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


def test_load_accounts_never_raises():
    assert isinstance(paths.load_accounts(), dict)


def test_process_result_and_protocol():
    ev = MailEvent("a@cqtransit.com", "草单运单号", "<m@x>", "1", "s", "f@d", "", "")
    pr = ProcessResult(event=ev, action="record")
    assert pr.tier is None and pr.route == ()
    assert {"can_handle", "process"} <= set(Processor.__protocol_attrs__)
