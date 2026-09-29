#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RBAC 共享辅助（B 工单内联，A 工单复用同一文件，不得重复定义）。

is_production(app)：唯一判定口径。
"""
import os


def is_production(app):
    # 唯一判定口径；bool() 包裹避免 PYTEST_CURRENT_TEST 返回字符串污染 or 表达式
    return not (
        bool(getattr(app, "debug", False)) or
        bool(getattr(app, "testing", False)) or
        bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )
