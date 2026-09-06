#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一业务异常（Phase 0 · 2026-09-07）。

设计见 docs/superpowers/specs/2026-09-06-unified-error-handling.md。

约定：
- error_code：SCREAMING_SNAKE_CASE 语义化枚举，与 HTTP 状态码**完全解耦**。
- http_status：默认 200。业务规则被违反 ≠ 服务器故障，故业务错默认 200；
  只有「非 AppError 的未知 Exception」被全局兜底捕获时才 → 500。
- 各模块专属错误码在「业务规则所在层」（services/ 或 routes/）定义，
  本基类仅预设唯一兜底值 INTERNAL_SERVER_ERROR。
"""

__all__ = ["AppError"]


class AppError(Exception):
    def __init__(self, error_code: str = "INTERNAL_SERVER_ERROR",
                 msg: str = "", http_status: int = 200):
        super().__init__(msg)
        self.error_code = error_code
        self.msg = msg
        self.http_status = http_status
