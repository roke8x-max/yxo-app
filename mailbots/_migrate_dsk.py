# -*- coding: utf-8 -*-
"""一次性迁移：飞书 DSK 配置表(TABLE_DSK_CONFIG) -> yxo.db bot_config(bot='atb'/'dsk')。
- DEFAULT 行(公司->To/CC) -> scope='company', key=公司（atb 与 dsk 各一份，二者共用同一配置）
- 逐箱号行(逐条 to/cc) 不迁移（P4③ 用户确认"只迁 DEFAULT、逐箱匹配不需要了"）
idempotent: ON CONFLICT(bot,scope,key) DO UPDATE
"""
import json, sqlite3, re
import sys
import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (ListAppTableRecordRequest,)

sys.path.insert(0, r'D:\YXO_DATA\WeComBot')
from config import (
    FEISHU_APP_ID as APP_ID,
    FEISHU_APP_SECRET as APP_SECRET,
    FEISHU_APP_TOKEN as APP_TOKEN,
)
TABLE_DSK_CONFIG = "tblpp0CHtSYDDKru"
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
    cfg = fs.get_all_records(TABLE_DSK_CONFIG)
    default_rows = {}
    for r in cfg:
        f = r.fields or {}
        box_no = str(f.get('箱号') or '').strip()
        comp = str(f.get('公司名称') or '').strip()
        if box_no.upper() == 'DEFAULT' and comp:
            default_rows[comp] = {
                'to': split_emails(f.get('收件人 (To)')),
                'cc': split_emails(f.get('抄送人 (CC)')),
            }

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    n = 0
    for bot in ('atb', 'dsk'):
        for comp, v in default_rows.items():
            cur.execute(
                """INSERT INTO bot_config(bot,scope,key,to_addrs,cc_addrs,source,updated_at)
                   VALUES(?,'company',?,?,?,'feishu',datetime('now','localtime'))
                   ON CONFLICT(bot,scope,key) DO UPDATE SET to_addrs=excluded.to_addrs,
                     cc_addrs=excluded.cc_addrs, source='feishu', updated_at=excluded.updated_at""",
                (bot, comp, json.dumps(v['to'], ensure_ascii=False), json.dumps(v['cc'], ensure_ascii=False)))
            n += 1
    conn.commit()

    cur.execute("SELECT bot,COUNT(*) FROM bot_config WHERE bot IN ('atb','dsk') AND scope='company' GROUP BY bot")
    print("bot_config(atb/dsk) 汇总:", dict(cur.fetchall()))
    print("\nDEFAULT(company) 样本(atb):")
    cur.execute("SELECT key,to_addrs,cc_addrs FROM bot_config WHERE bot='atb' AND scope='company' ORDER BY key")
    for k, t, cc in cur.fetchall():
        print(f"  {k}: to={json.loads(t)} cc={json.loads(cc)}")
    print(f"\n迁移完成: 写入 {n} 行 (atb/dsk 各 {len(default_rows)} 公司)")
    conn.close()


if __name__ == "__main__":
    main()
