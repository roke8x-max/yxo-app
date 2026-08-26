# -*- coding: utf-8 -*-
"""把 mailbots/ 加入 sys.path，使单元测试可以 import common_io / core.*"""
import os
import sys

MAILBOTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MAILBOTS_DIR not in sys.path:
    sys.path.insert(0, MAILBOTS_DIR)
