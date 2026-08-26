# -*- coding: utf-8 -*-
"""脚手架自检：能 import 公共层且 norm_train_no 行为不变。"""
from common_io import norm_train_no


def test_import_common_io():
    assert norm_train_no("491") == "WB491"
    assert norm_train_no("wb 492") == "WB492"
