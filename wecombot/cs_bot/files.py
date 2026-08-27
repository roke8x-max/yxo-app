# -*- coding: utf-8 -*-
"""
文件归档：D:\\YXO_DATA\\YXO_DATA\\{年}\\{MM}\\{M.D 班列号}\\{客编}_{箱号}\\{随车|报放}
"""
import os
import re
import sys
import shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import ARCHIVE_ROOT, CS_TRASH_DIR


def _sanitize(s):
    return re.sub(r'[\\/:*?"<>|]', "_", str(s or "").strip())


def archive_dir(rec, sub="随车"):
    """根据记录算出归档目录（不创建）。sub: 随车 | 报放"""
    date_str = str(rec.get("发班时间") or "").strip()      # 如 2026/08/08
    m = re.match(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", date_str)
    if m:
        year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    else:
        now = datetime.now()
        year, month, day = str(now.year), now.month, now.day
    train = _sanitize(rec.get("班列号") or "未知班列")
    code = _sanitize(rec.get("客户编码") or "未知客编")
    box = _sanitize(rec.get("箱号") or "无箱号")
    return os.path.join(ARCHIVE_ROOT, year, f"{month:02d}",
                        f"{month}.{day} {train}", f"{code}_{box}", sub)


def save_file(src_path, rec, sub="随车"):
    """把文件移动到归档目录，返回目标路径。"""
    dest_dir = archive_dir(rec, sub)
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(src_path)
    dest = os.path.join(dest_dir, name)
    base, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{base}({i}){ext}")
        i += 1
    shutil.move(src_path, dest)
    return dest


def list_folder(rec, sub="随车"):
    """列出归档目录中的文件名。"""
    d = archive_dir(rec, sub)
    if not os.path.isdir(d):
        return d, []
    names = sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
    return d, names


def trash_file(path):
    """撤销存档：移入回收目录（不物理删除），返回新路径。"""
    os.makedirs(CS_TRASH_DIR, exist_ok=True)
    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.path.basename(path)}"
    dest = os.path.join(CS_TRASH_DIR, name)
    shutil.move(path, dest)
    return dest
