#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""权限模块迁移脚本（spec §12）：备份 yxo.db → 初始化 auth.db（建表+种子）。

用法（仓库根）：
    python scripts/init_auth_db.py

初始口令仅从环境变量读（4 人各自独立）：
    YXO_AUTH_PASSWORD_MAOXIAOYANG / _FENGQIAN / _YANGYAWEN / _HANWENHAO / _YOUKE
未设置则 seed 用开发占位口令并 WARNING（生产必须设置）。
注：VISITOR_DEMO 为禁用演示账号，不进必需口令清单。

回滚：删 auth.db + git 切回（`del data\\auth.db`，再 git checkout  pre-auth 状态）。
yxo.db 本脚本只备份不修改；数据库本身不会自动回滚，恢复前先问毛骁洋。
"""
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def main():
    # 1. 备份 yxo.db（只读不动）
    if os.path.exists(config.DB_PATH):
        bak = config.DB_PATH + ".pre-auth-" + datetime.now().strftime("%Y%m%d%H%M%S") + ".bak"
        shutil.copy2(config.DB_PATH, bak)
        print("已备份 yxo.db →", bak)
    else:
        print("yxo.db 不存在（全新环境），跳过备份")
    # 2. 建表 + 种子
    from auth import schema
    schema.init_db(strict=True)
    print("auth.db 就绪 →", config.AUTH_DB_PATH)
    print("下一步：设置 4 人初始口令环境变量 → 重启 Flask → 用 POST /api/login 验证 → 跑 tests/auth_gate_test.py")


if __name__ == "__main__":
    main()
