# -*- coding: utf-8 -*-
"""spec 刀1/D4：daemon_loop 异常必须落 traceback 到日志文件（根治隐身崩溃）。"""
import os
import common_io


def _err_dir(tmp_path):
    d = tmp_path / "error_logs"
    common_io.ERROR_LOG_DIR = str(d)          # 重定向到测试目录
    return str(d)


def test_run_once_unknown_exception_writes_traceback(tmp_path):
    d = _err_dir(tmp_path)

    def boom():
        raise ValueError("业务炸了")

    got = common_io.run_once("TestBot", boom)
    assert got == 0
    files = os.listdir(d)
    assert len(files) == 1 and files[0].startswith("TestBot_error_")
    content = open(os.path.join(d, files[0]), encoding="utf-8").read()
    assert "ValueError" in content and "业务炸了" in content and "Traceback" in content


def test_run_once_network_exception_logged(tmp_path):
    d = _err_dir(tmp_path)

    def netfail():
        raise ConnectionError("imap 断了")

    assert common_io.run_once("TestBot", netfail) == 0
    content = open(os.path.join(d, os.listdir(d)[0]), encoding="utf-8").read()
    assert "ConnectionError" in content


def test_run_once_success_returns_count(tmp_path):
    _err_dir(tmp_path)
    assert common_io.run_once("TestBot", lambda: 3) == 3
    assert common_io.run_once("TestBot", lambda: None) == 0
