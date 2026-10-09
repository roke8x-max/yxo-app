#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G1 strict 子进程探针（_ 前缀避开 pytest 收集）。

用法：测试用 subprocess.run([sys.executable, 本文件], env={清空口令 env + YXO_AUTH_DB=临时库})
内容仅两行逻辑：from auth.schema import init_db + init_db(strict=True)。
子进程无 PYTEST_CURRENT_TEST、无 app 上下文 → enforce=True。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.schema import init_db  # noqa: E402

init_db(strict=True)
