# -*- coding: utf-8 -*-
"""一次性迁移：飞书运踪配置表(TABLE_CONFIG) -> yxo.db bot_config(bot='tracking')。
- DEFAULT 行(公司->To/CC) -> scope='company', key=公司
- 逐条班列行(仅取 班列->公司 关联) -> scope='train', key=班列号, extra={'company':公司}
冗余的逐条 to/cc（与所属公司 DEFAULT 完全相同）不迁移，路由一律走 DEFAULT。
"""
import json, sqlite3, re
import sys
import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    ListAppTableRecordRequest, CreateAppTableRecordRequest,
    AppTableRecord, DeleteAppTableRecordRequest,
)

sys.path.insert(0, r'D:\YXO_DATA\WeComBot')
from config import (
    FEISHU_APP_ID as APP_ID,
    FEISHU_APP_SECRET as APP_SECRET,
    FEISHU_APP_TOKEN as APP_TOKEN,
)
TABLE_CONFIG = "tbl4wFdo9scMmUM7"
DB = "D:/YXO_DATA/yxo_app/data/yxo.db"


def split_emails(s):
    if not s or str(s).strip().lower() == "none":
        return []
    return [e.strip() for e in re.split(r'[;,，\s]+', str(s)) if "@" in e]


class FeishuBitable:
    def __init__(self):
        self.client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

    def get_all_records(self, table_id):
        all_items, page_token = [], ""
        while True:
            b = ListAppTableRecordRequest.builder().app_token(APP_TOKEN).table_id(table_id).page_size(500).automatic_fields(True)
            if page_token:
                b = b.page_token(page_token)
            resp = self.client.bitable.v1.app_table_record.list(b.build())
            if not resp.success():
                break
            items = resp.data.items
            if items:
                all_items.extend(items)
            if not resp.data.has_more:
                break
            page_token = resp.data.page_token
        return all_items


def main():
    fs = FeishuBitable()
    cfg = fs.get_all_records(TABLE_CONFIG)
    default_rows = {}
    train_companies = {}  # train_id -> set(公司)
    for r in cfg:
        f = r.fields or {}
        tid = str(f.get('班列号') or '').strip()
        comp = str(f.get('公司名称') or '').strip()
        if tid.upper() == 'DEFAULT':
            default_rows[comp] = {'to': split_emails(f.get('收件人 (To)')), 'cc': split_emails(f.get('抄送人 (CC)'))}
        else:
            train_companies.setdefault(tid, set()).add(comp)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    n_def = n_train = 0
    for comp, v in default_rows.items():
        cur.execute(
            """INSERT INTO bot_config(bot,scope,key,to_addrs,cc_addrs,source,updated_at)
               VALUES('tracking','company',?,?,?,'feishu',datetime('now','localtime'))
               ON CONFLICT(bot,scope,key) DO UPDATE SET to_addrs=excluded.to_addrs,
                 cc_addrs=excluded.cc_addrs, source='feishu', updated_at=excluded.updated_at""",
            (comp, json.dumps(v['to'], ensure_ascii=False), json.dumps(v['cc'], ensure_ascii=False)))
        n_def += 1
    for tid, comps in train_companies.items():
        cur.execute(
            """INSERT INTO bot_config(bot,scope,key,to_addrs,cc_addrs,extra,source,updated_at)
               VALUES('tracking','train',?,?,?,?,'feishu',datetime('now','localtime'))
               ON CONFLICT(bot,scope,key) DO UPDATE SET extra=excluded.extra,
                 source='feishu', updated_at=excluded.updated_at""",
            (tid, '[]', '[]', json.dumps({'companies': sorted(comps)}, ensure_ascii=False)))
        n_train += 1
    conn.commit()

    cur.execute("SELECT scope,COUNT(*) FROM bot_config WHERE bot='tracking' GROUP BY scope")
    print("bot_config(tracking) 汇总:", dict(cur.fetchall()))
    cur.execute("SELECT key,to_addrs,cc_addrs FROM bot_config WHERE bot='tracking' AND scope='company' ORDER BY key")
    print("\nDEFAULT(company):")
    for k, t, cc in cur.fetchall():
        print(f"  {k}: to={json.loads(t)} cc={json.loads(cc)}")
    cur.execute("SELECT key,extra FROM bot_config WHERE bot='tracking' AND scope='train' ORDER BY CAST(key,'INT') LIMIT 3")
    print("\ntrain 样本(公司列表):", cur.fetchall())
    # 验证多公司扇形：611 应包含多公司
    cur.execute("SELECT extra FROM bot_config WHERE bot='tracking' AND scope='train' AND key='611'")
    print("611 公司集合:", cur.fetchone())
    print(f"\n迁移完成: DEFAULT={n_def}, 去重班列={n_train}")
    conn.close()


if __name__ == "__main__":
    main()
