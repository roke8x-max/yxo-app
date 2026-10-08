# 新增公司「联运」改造 Spec（OpenCode 修正版 v3）

> 日期：2026-09-09 ｜ 作者：芙蕾雅（spec）｜ 执行：OpenCode ｜ 状态：待洋拍板后开工
> **v3 修订（2026-09-09 晚）**：针对最新一轮 review（实跑 wecom_api 反查 3/4→None + 深挖根 config `CORP_ID/SECRET` 空串"静默失效"）做了**实跑导入链验证**，结论：
> ① 新系统运行时 `config` 解析到 **`wecombot/config.py`**（`wecom_api.py:12` 的 `sys.path.insert(0, wecombot_dir)` 所致，已用 `find_spec` 实证 `[B]/[C]` 两次均为 `wecombot/config.py`），根 `config.py` 的 `WECOM_USER_MAP` **实际不会被新系统读取** → v2 把它列为"必须改的功能修复"是**误判**，本版降级为「防御性一致性」；
> ② "CORP_ID/SECRET 空串导致冯茜通知石沉大海"**不成立**——wecom_api 导入的是**无前缀** `CORP_ID`（来自 `wecombot/secrets.json` 真凭据），而根 config 的空串是**带前缀** `WECOM_CORP_ID`，变量名根本不同；
> ③ 冯茜企微通知在新系统里**本来就通**（`wecombot/config.py:87` 已是 `BanXian→冯茜` + `notify.py:74` 已含冯茜）。
> 其余 v2 修订（§4 建表 P0、README 漏联运、grep 改 Python、重启服务）维持。field_options 联运洋已加 → §9 前置已满足。

---

## 0. OpenCode 四铁律（洋定，务必遵守）
1. **不瞎改已做好内容** —— 只动本 spec 列出的落点，禁止"顺手重构"。
2. **不自己 commit / push** —— 改完本地自测，等洋 review + 合并。
3. **做完先自测** —— `pytest mailbots_next/tests/ -q` 全绿（基线见 §6）。
4. **如实报告，不替拍板** —— 遇到 spec 没覆盖的再问，不自行扩大范围。

---

## 1. 改动总览

| 层 | 落点 | 性质 | 谁执行 |
|---|---|---|---|
| 代码 | `store.py` seed_owner_mapping 加联运 | 提交 dev（新系统功能修复） | OpenCode |
| 代码 | `config.py` 冯茜+联运 + `WECOM_USER_MAP` 对齐（**防御性一致性，非功能修复**） | 提交 dev | OpenCode |
| 代码 | `wecombot/config.py`（仓库+生产）冯茜+联运 | 提交 dev + 生产改 | OpenCode / 小叽 |
| 代码 | `config.py:71-72` FIELD_DEFS options 加联运 | 提交 dev（低危） | OpenCode |
| 代码 | 测试同步（test_core / test_p1_coverage） | 提交 dev | OpenCode |
| 文档 | `AGENTS.md` + `mailbots_next/README.md` 加联运 | 提交 dev | OpenCode |
| 数据 | `yxo.db` 插 shared 联运行（旧系统即时生效） | 生产写操作 | 小叽（洋点头+备份后） |
| 数据 | `bot_config.db` 灌 8 家 company 行 | 部署期操作 | 小叽（新系统上线前） |
| 运维 | 改完 config 须重启对应服务（:5011 / :5001） | 生产 | 小叽 |

---

## 2. 代码改动（逐条，含已验证行号）

### 2.1 `mailbots_next/core/store.py` — seed_owner_mapping（✅ ds 方案正确，新系统功能修复）
- 位置：`seed_owner_mapping()` 字典，约 `:229-237`，`"保时达": "fengqian@cqtransit.com"` 在 `:236`。
- 改法：新增一行 `"联运": "fengqian@cqtransit.com"`（冯茜已拥有保时达，联运是新增 key，不冲突）。
- 原因：新系统负责人映射在代码里硬编码 seed（`store.py:254` 模块级调用），不在此加则 `get_responsible_person("联运")` 返回 None → 联运邮件无负责同事。**这是新系统真正需要的功能修复之一。**

### 2.2 `config.py` — 三处（⚠️ v3 已降级为防御性一致性，非新系统功能修复）
> **重要前提（v3 实跑验证）**：新系统运行时，`wecombot/cs_bot/wecom_api.py:12` 会在模块加载时执行 `sys.path.insert(0, wecombot_dir)`；之后 `wecom_api.py:15/317` 的 `from config import ...` 一律解析到 **`wecombot/config.py`**（已用 `importlib.util.find_spec` 实跑确认：`[B]` 加载 wecom_api 后、`[C]` 调用时均为 `wecombot/config.py`）。因此**根 `config.py` 的 `WECOM_USER_MAP` 实际不会被新系统的企微链路读取**——它是一份"潜在地雷"而非"现役代码"。v2 曾把它列为"必须改的功能修复"，那是误判，本版纠正。

- **2.2a `WECOM_USER_MAP` 对齐（防御性，照洋要求与 `wecombot/config.py:84-89` 对齐）**
  - `config.py:141`：`"MaoXiaoYang": "MaoXiaoYang"` → `"MaoXiaoYang": "毛骁洋"`
  - `config.py:143`：`"wulala": "吴拉拉"` → `"wulala": "杨雅雯"`
  - `config.py:144`：`"BanXian": "半仙"` → `"BanXian": "冯茜"`
  - 性质：**防御性一致性**。新系统不会读这里；仅当某条测试/异常入口绕过 `wecom_api.py:12` 的 sys.path 改写时，根 config 才会被误用。对齐后消除该地雷。**冯茜企微通知在新系统里本来就通**（`wecombot/config.py:87` 已是 `BanXian→冯茜` + `notify.py:74` 的 `_WECOM_NAME_BY_EMAIL` 已含冯茜），本改**不改变**新系统行为。
- **2.2b `USER_COMPANIES`（旧系统公司→负责人映射，功能性）**
  - `config.py:151`：`"吴拉拉": ["同程配", "东盟"]` → `"杨雅雯": ["同程配", "东盟"]`（换人未同步的旧 bug，键名修正，使 `COMPANY_TO_EMAIL` 不再缺同程配/东盟）
  - `config.py:152`：`"冯茜": ["保时达"]` → `"冯茜": ["保时达", "联运"]`
  - 原因：根 config 的 `USER_COMPANIES` 被 `mailbots/`（旧系统，若从 yxo_app 仓库根运行）读取，决定"以谁身份发信"。新系统**不用它**（用 `store.py` seed），但旧系统需要。`:151` 的修正连同 2.2a 一并处理。

### 2.3 `wecombot/config.py` — 冯茜加联运（两处都要）
- 仓库 `wecombot/config.py:95`：`"冯茜": ["保时达"]` → `["保时达", "联运"]`
- 生产 `D:\YXO_DATA\WeComBot\config.py`（行号以生产文件为准）：同样改。
- 原因：`company_to_name` 据此把联运归到冯茜（旧系统 / wecom_api 的负责人解析）。
- ⚠️ **`wecombot/config.py:84-89` 的 `WECOM_USER_MAP` 已是正确的**（`BanXian→冯茜` 等），**无需动**；真正的企微 uid 反查用的就是这份，所以**新系统冯茜通知本来就通**。本 § 只加 `USER_COMPANIES` 的联运归属。
- 吴拉拉那两处（`wecombot/config.py:86/:94`）保持不动（已正确）。

### 2.4 测试同步（✅ ds 方案正确，注意有两处）
- `mailbots_next/tests/test_core.py`：
  - `seed_company_recipients()` 字典（`:51-58`）新增 `"联运": {"to": ["lx_to@test.com"], "cc": ["3841559246@qq.com"]}`
  - `test_seed_owner_mapping` 的 `assert len(rows) == 7`（`:249`）→ `== 8`
- `mailbots_next/tests/test_p1_coverage.py`：
  - `_SEVEN_COMPANIES` 字典（`:39-47` 附近）新增 `"联运": (["lx_to@test.com"], ["3841559246@qq.com"])`（`_seed_recipients()` 会随字典自动迭代，无需改循环）
- 原因：不加则这些测试必红，且能锁住"联运收件人/负责人"行为。

### 2.5 文档（AGENTS.md + README）
- `AGENTS.md`：冯茜一行（`保时达`）→ `保时达、联运`。
- `mailbots_next/README.md:114`：负责人映射列表的 `- 保时达 → fengqian@cqtransit.com` 改为 `- 保时达/联运 → fengqian@cqtransit.com`（review 发现漏了联运）。
- 原因：文档与代码保持一致，避免新对话误以为联运没配。

---

## 3. 数据改动一：yxo.db 插联运行（旧系统即时生效）⚠️ 生产写操作

> 旧系统（生产现役 5 机器人）读 `yxo.db.bot_config` 的 `bot='shared', scope='company'`。插了这一行，联运立刻能被旧系统转发。

**执行前**：备份 `yxo.db`（小叽/洋操作）；**执行需洋明确点头**。

```sql
INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra, source)
VALUES (
  'shared', 'company', '联运',
  '["gongqilin@cqjzxly.cn","yuyanling@cqjzxly.cn","jjb@cqjzxly.cn","wanglu@cqjzxly.cn"]',
  '["3841559246@qq.com"]',
  '{}', 'manual'
);
```
- 收件人 4 位以洋最终确认为准（此处取自梳理文档）；cc 按 Q2 加公共抄送。
- 前置：联运的订舱记录须先进入 `records` 表（Q3：9 月中旬），否则匹配不到客户编码/箱号，配了收件人也发不出。

---

## 4. 数据改动二：mailbots_next/data/bot_config.db 灌 8 家 company 行 ⚠️ 部署期必做（v2 修正 P0）

> **关键修正点（ds 原方案只插了联运 1 行）**：新系统 `get_recipients()`（`store.py:108-124`，被 `routing.py`/`serve.py` 现役调用）读 `bot_config.db` 的 `scope='company'`，**代码里没有任何 company 的 seed**（只有 owner 有 seed）。生产上该文件从未存在过 → 上线若只插联运，现有 7 家会**全量静默 no_route**。必须一次性灌满 **8 家**（7 现有 + 联运）。

> ⚠️ **P0（来自 review，已修正）**：**脚本不要自己 `CREATE TABLE`**。新系统 `init_bot_config_db()`（`store.py:44-54`）建的表带 `id INTEGER PRIMARY KEY AUTOINCREMENT` 和 `UNIQUE(bot, scope, key)`。自己建简版表会被 `CREATE TABLE IF NOT EXISTS` 永久固化，且 `UNIQUE` 缺失会让 `INSERT OR REPLACE` 退化成只插不替（company 行重复）；更糟的是 `seed_owner_mapping()` 每次启动都靠 `UNIQUE` 去重 → owner 行无限膨胀。正确做法：**import `mailbots_next.core.store` 触发它自己的建表**，再复用它的连接。

**做法：从生产真库 `yxo.db` 镜像 7 家 + 补联运 = 8 行。** 本地 yxo.db 是空壳，须对真库跑。

部署脚本（一次性、幂等，跑于新系统上线前）：

```python
# seed_next_bot_config.py  —— 在装有真 yxo.db 的机器上运行（部署期，一次性、幂等）
import os, sys, json, datetime, sqlite3

next_db = sys.argv[2]   # mailbots_next/data/bot_config.db 目标路径
# 必须在 import store 之前设定，否则建到默认路径
os.environ["BOT_CONFIG_DB_PATH"] = next_db

# 把仓库根加入 path，import 时即执行 init_bot_config_db()（正确表结构）+ seed_owner_mapping()
repo_root = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(os.path.dirname(next_db))
sys.path.insert(0, repo_root)
import mailbots_next.core.store as store   # 关键：用新系统自己的建表，不要自己 CREATE TABLE

yxo_db = sys.argv[1]   # 真 yxo.db 路径
conn_y = sqlite3.connect(yxo_db)
rows = conn_y.execute(
    "SELECT key, to_addrs, cc_addrs FROM bot_config "
    "WHERE bot='shared' AND scope='company'"
).fetchall()
conn_y.close()

existing = {r[0] for r in rows}
if '联运' not in existing:          # 若 yxo.db 已先插联运，则免手动补，避免重复
    rows.append(("联运",
        json.dumps(["gongqilin@cqjzxly.cn","yuyanling@cqjzxly.cn",
                    "jjb@cqjzxly.cn","wanglu@cqjzxly.cn"], ensure_ascii=False),
        json.dumps(["3841559246@qq.com"], ensure_ascii=False)))

# 复用新系统自己的连接（表结构已正确），INSERT OR REPLACE 靠 UNIQUE(bot,scope,key) 去重
conn_n = store.get_bot_config_connection()
for key, to, cc in rows:
    conn_n.execute(
        "INSERT OR REPLACE INTO bot_config "
        "(bot, scope, key, to_addrs, cc_addrs, extra, updated_at) "
        "VALUES ('all','company',?,?,?,?,?)",
        (key, to, cc, '{}', datetime.datetime.now().isoformat()))
conn_n.commit(); conn_n.close()
print("seeded", len(rows), "company rows into", next_db)
```

> 备选（不 import 整包时）：照抄 `store.py:44-54` 的完整 DDL（含 `id` 主键与 `UNIQUE`）再建表；但 import 方式更不易随表结构演进失同步，推荐前者。
> 跑完应看到 `seeded 8 company rows`。`seed_owner_mapping`（代码 seed）上线时自动补 owner 行，无需在此处理。此文件 `data/` 在 `.gitignore` 内，**不提交**，纯部署动作。

---

## 5. 不在本次范围 / 禁止改动（铁律①）
- ❌ 不改 `mailbots/` 旧系统代码（只通过 yxo.db 数据行影响旧系统）。
- ❌ 不动 `docs/superpowers/specs/2026-09-08-ticket-system-design.md`（未跟踪设计草稿，地位待确认）。
- ❌ 不改 `settings.py` 的 `COMPANY_ALIAS`（Q1=联运无别名，无需映射）。
- ❌ 不消除任何合法 `except: pass`（P0-3 已审，剩余 8 处全部无害）。
- ❌ 不碰 `wecombot/config.py` 的吴拉拉两处（已正确）、不碰其 `WECOM_USER_MAP`（已正确）。

---

## 6. 验收标准
1. **单测**：改动前先 `pytest mailbots_next/tests/ -q` 记基线 passed 数；改动后重跑 **必须全绿且 passed ≥ 基线**（新增公司相关断言使总数略增）。
2. **行为断言**（建议加进 test_core.py）：
   - `get_responsible_person("联运") == "fengqian@cqtransit.com"`
   - `get_recipients("联运")` 返回 `to` 含 4 个邮箱、`cc == ["3841559246@qq.com"]`
3. **代码自检（Windows 友好）**：**不要用 `grep`**——Windows PowerShell/CMD 无 grep（`AGENTS.md:115` 已记此坑）。改用 Python 一段：
   ```python
   import pathlib
   files = list(pathlib.Path("mailbots_next").rglob("*.py")) + \
           [pathlib.Path("config.py"), pathlib.Path("wecombot/config.py")]
   for f in files:
       if f.exists() and "联运" in f.read_text(encoding="utf-8", errors="ignore"):
           print("HIT", f)
   ```
   期望命中：store.py(seed)、config.py（:152 冯茜 / :72 FIELD_DEFS 若加 / :141/:143/:144 WECOM_USER_MAP / :151 USER_COMPANIES）、wecombot/config.py（:95）、tests（2 处）。命中位置即本 spec 落点。
4. **端到端（上线后）**：造一条联运测试记录 + 发测试邮件 → 确认 4 个收件人收到；旧系统走 yxo.db shared，新系统走 bot_config.db。
5. **企微送达验证（上线 TEST 必查，trust-but-verify）**：发一封触发联运/冯茜通知的测试邮件，确认**冯茜真在企微收到**（而非只看日志无报错）。理由：尽管代码路径正确（wecombot/config.py 真凭据 + 干净映射），但生产环境的 secrets.json / 网络 / 绑定表可能与本机不同，必须实测确认送达。⚠️ 注：这不是修"静默失效 bug"——经导入链实跑，新系统 `config` 解析到 `wecombot/config.py`（真凭据），**不存在"空串导致冯茜通知石沉大海"的情况**；此步纯粹是生产环境 verify。

---

## 7. 建议执行顺序
1. OpenCode 改代码（§2.1–2.5）→ 跑 pytest 全绿 → 不自 commit。
2. 洋 review 代码 → 合并 dev。
3. 小叽：备份 yxo.db → 执行 §3 SQL（洋点头后）。**注意 config 是 import 时加载，写完不重启不生效**：
   - 改了根 `config.py`（:141/:143/:144/:151/:152/:72）→ **重启 :5011（yxo_app Flask / 订舱数据管理）**；
   - 改了 `wecombot/config.py`（:95）→ **重启 :5001（订舱助手 / 企微机器人 WeComBot）**。
4. 新系统上线前：跑 §4 镜像脚本灌 8 家 → 验证 `seeded 8`。
5. 上线后按 §6.4–6.5 端到端 + 企微送达验收。

---

## 8. 风险与回退
- **最大风险**：§4 漏灌 7 家 → 新系统上线后现有 7 家静默不转发（最贵 bug）。 mitigation：上线 checklist 强制 §4 输出 `seeded 8`，且脚本必须用新系统自己的 `init_bot_config_db()` 建表（P0）。
- **回退**：代码改动可 revert commit；yxo.db 用备份恢复；bot_config.db 删文件即回到空壳（新系统 owner 仍由代码 seed，仅 company 行丢失，重跑 §4 即可）。

---

## 9. 上线前确认项（已大部分满足）
- ✅ **field_options 联运**：洋已在 `yxo.db` 的 `field_options` 下拉新增"联运" → §2.2c 的"生产写操作"不再需要；前端手工维护 records 时也能选到联运。
- **联运记录进 `records` 的方式**：无论前端手工还是 Excel 导入 base 字段，因 field_options 已含联运，两种方式都通。Q3 不变：联运订舱记录须先进入 `records`（9 月中旬）才能匹配转发。
- **后续建议（方案B 重构时一并做，非本次范围）**：`wecom_api.py:12-15/317` 依赖 `sys.path.insert` 魔法把 `from config import` 指向 `wecombot/config.py`，脆弱且易踩（本次 review 的"根 config 误用"即源于此）。建议改为绝对导入 `from wecombot.config import ...`，使配置来源确定性，彻底消除"解析到哪个 config"的地雷。另：洋已拍板企微走 HTTP 调运行中的 WeComBot:5001（方案B），届时 `notify.py` 的 `from wecombot.cs_bot.wecom_api import` 可整体换成 HTTP 客户端。
