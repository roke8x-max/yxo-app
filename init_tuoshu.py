# -*- coding: utf-8 -*-
"""
托书自动生成 · 建表与初始数据灌入（幂等，可重复执行）
2026-08-03 小叽
- tuoshu_templates : 模板登记（文件路径 + 字段映射 + 命名规则）
- tuoshu_dest_map  : 目的站 → 英文/国家 映射（含 alias 同义归一）
初始数据来源：tuoshu_engine.DEST_MAP / DEST_NORMALIZE（芙蕾雅原表，忠实迁移不改值）
"""
import json
import os
import sqlite3

import config

BASE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(config.DATA_DIR, "tuoshu_templates")
TPL_FILE = os.path.join(TPL_DIR, "托书模板.xlsx")

DDL = """
CREATE TABLE IF NOT EXISTS tuoshu_templates (
  id INTEGER PRIMARY KEY,
  name TEXT,
  file_path TEXT,
  field_map_json TEXT,
  name_pattern TEXT,
  created_by TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tuoshu_dest_map (
  station_cn TEXT PRIMARY KEY,
  en_name TEXT, country TEXT, alias TEXT
);
"""

# 忠实迁移自 tuoshu_engine.DEST_MAP（值拆成 en_name + country）
# alias 来自 DEST_NORMALIZE（逗号分隔的同义写法）
DEST_ROWS = [
    ("沃尔西诺",   "Vorsino沃尔西诺",       "RUS", ""),
    ("科里亚季奇", "Kolyadichi科里亚季奇",   "BEL", ""),
    ("叶卡捷琳堡", "Yekaterinburg叶卡捷琳堡", "RUS", ""),
    ("克列西哈",   "Kleshchikha克列西哈",   "RUS", ""),
    ("电煤",       "Dostyk电煤",            "KAZ", ""),
    ("杜伊斯堡",   "Duisburg杜伊斯堡",       "GER", "杜伊斯堡（时刻表）"),
    ("布达佩斯",   "Budapest布达佩斯",       "HUN", ""),
    ("马拉",       "Malaszewicze马拉",      "POL", "马拉（时刻表）"),
    ("别雷拉斯特", "Bely Rast别雷拉斯特",    "RUS", ""),
    ("谢利亚季诺", "Selyatino谢利亚季诺",    "RUS", "谢丽亚基诺"),
    ("莫斯科",     "Moscow莫斯科",          "RUS", ""),
    ("明斯克",     "Minsk明斯克",           "BLR", ""),
    ("满洲里",     "Manzhouli满洲里",        "CN",  ""),
    ("罗斯托夫",   "Rostov罗斯托夫",         "RUS", ""),
    # 系统里在用但原表缺失，补录（小叽 2026-08-03）：
    ("圣彼得堡",   "Saint-Petersburg圣彼得堡", "RUS", ""),
]

DEFAULT_FIELD_MAP = {
    "booking_date":   ["Sheet1", "E4"],
    "departure_date": ["Sheet1", "E13"],
    "destination":    ["Sheet1", "E15"],
    "container":      ["Sheet1", "B24"],
}
DEFAULT_NAME_PATTERN = "渝新欧订舱委托书 -{departure:%Y%m%d} -{station_cn} -{train_suffix}"


def main():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.executescript(DDL)

    # 目的站映射：只插不覆盖（用户后续自行维护的值不被脚本重置）
    added = 0
    for station, en, country, alias in DEST_ROWS:
        exists = cur.execute(
            "SELECT 1 FROM tuoshu_dest_map WHERE station_cn=?", (station,)
        ).fetchone()
        if not exists:
            cur.execute(
                "INSERT INTO tuoshu_dest_map(station_cn,en_name,country,alias) VALUES(?,?,?,?)",
                (station, en, country, alias),
            )
            added += 1

    # 模板登记：无则插入一条默认
    tpl_added = 0
    if not cur.execute("SELECT 1 FROM tuoshu_templates LIMIT 1").fetchone():
        from datetime import datetime
        cur.execute(
            "INSERT INTO tuoshu_templates(name,file_path,field_map_json,name_pattern,created_by,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            ("默认托书模板", TPL_FILE, json.dumps(DEFAULT_FIELD_MAP, ensure_ascii=False),
             DEFAULT_NAME_PATTERN, "小叽", datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        tpl_added = 1

    conn.commit()

    print(f"✓ 建表完成；目的站新增 {added} 条，模板新增 {tpl_added} 条")
    print("--- tuoshu_templates ---")
    for r in cur.execute("SELECT id,name,file_path,name_pattern FROM tuoshu_templates"):
        print(dict(r))
    print(f"--- tuoshu_dest_map 共 "
          f"{cur.execute('SELECT COUNT(*) FROM tuoshu_dest_map').fetchone()[0]} 条 ---")

    # 体检：系统在用的目的站里，哪些还没有映射
    used = [r[0] for r in cur.execute(
        'SELECT DISTINCT "目的站" FROM records '
        'WHERE COALESCE(is_deleted,0)=0 AND "目的站" IS NOT NULL AND "目的站"<>""'
    )]
    known = set()
    for r in cur.execute("SELECT station_cn,alias FROM tuoshu_dest_map"):
        known.add(r["station_cn"])
        for a in (r["alias"] or "").split(","):
            if a.strip():
                known.add(a.strip())
    missing = [u for u in used if u not in known]
    print(f"⚠ 系统在用但未配置映射的目的站 {len(missing)} 个：{missing}")

    print(f"模板文件存在：{os.path.exists(TPL_FILE)}  -> {TPL_FILE}")
    conn.close()


if __name__ == "__main__":
    main()
