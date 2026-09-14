# yxo-app · 项目全貌与操作手册（给 AI agent）

> 最后更新：**2026-09-14**（芙蕾雅）。**新开对话请先读完本文件再动手。**
> 有冲突以本文件为准 —— 除非洋（毛骁洋）当场更正。
> 本文件是项目**唯一入口**；git/协作流程细节见 `WORKFLOW.md`。

---

## 0. 三十秒速览

- **公司**：**重庆物流集团**（`cqtransit.com` 是本集团阿里云企业邮箱）。
  ⚠️ `yxologistics.com`（渝新欧）是**上游发件方/合作伙伴**，不是本公司 —— 这两个域名极易混淆。
- **这个项目是什么**：一套自研的「**订舱数据管理平台**」，由**三大支柱**组成：
  ① 邮件机器人协同层（自动入口）② yxo 订舱数据管理平台（数据平台层）③ 企微机器人（人机协同层）。
- **当前主线**：`mailbots_next/`（新版邮件机器人）准备上线替换旧系统 `mailbots/`。
- **分支铁律**：`dev` 开发 → Pull Request → `main` 生产；**`main` 服务端保护禁直推**。
- **新对话第一步**（先核实环境再动手）：

  ```bash
  git ls-remote origin                        # 核实真实远端状态（别信本地 git status，见 §11）
  python -m pytest mailbots_next/tests/ -q    # 基线应全绿（§9.4）
  ```

---

## 1. 系统全貌（★ 先读这节，否则会误判项目边界）

### 1.1 三大支柱

| 支柱 | 干什么 | 代码落点 | 状态 |
|---|---|---|---|
| **① 邮件机器人协同层**<br>（自动入口） | 监听 4 个 `@cqtransit.com` 邮箱，把 **草单 / 运单号 / 运踪 / DSK / ATB / NDR退信** 邮件结构化，转发给**外部客户公司** | `mailbots/`（旧，5 个独立脚本）<br>`mailbots_next/`（新，单一流水线） | 旧在生产 **live**<br>新为**主线**，待切换 |
| **② yxo 订舱数据管理平台**<br>（数据平台层） | 浏览器表格 + SQLite **替代飞书多维表**：订舱主表 / 看板 / 统计 / 回收站 / 舱单 / 托书 / 价格 / 字段与选项维护 | `app.py`(:5011)、`admin_api.py`、`templates/`、`static/`、`manifest_engine.py`、`tuoshu_engine.py`、`import_excel.py`、`init_*.py` | live |
| **③ 企微机器人**<br>（人机协同层） | 通知（转发 / 待办 / 日报 / 告警）+ 命令回调（`确认N` / `跳过N` / `待办` / `绑定 姓名`） | `wecombot/`（:5001；`server.py` + `cs_bot/{engine,parser,store,files,mailer,wecom_api}.py` + `ip_watchdog.py`） | live |

### 1.2 为什么自研（对外叙事基线，新对话必须知道）

集团系统（**吉联供应链平台**，`cq56.net`）有三个短板，正是本自研系统的出发点：

1. **展示 + 录入学习成本高** —— 界面复杂、字段多、菜单深，查一票 / 补一条要绕很多步；
2. **没有看板数据统计** —— 只能逐单查，无运量 / 箱量 / 到站 / 状态分布 / 退舱率可视化；
3. **整箱(FCL)与散舱(LCL)都分不开** —— 统计与调度受影响。

**两系统关系：互补不重复。** 集团做业务主流程（订舱 / 财务 / 运踪查询），我们做「自动入口 + 低成本看录统计 + 整箱散舱维度 + 企微人机协同」。**不要建议重复建 OMS，也不要建议切回飞书。**

### 1.3 数据流

```
        邮箱源（4 × @cqtransit.com）
                │ IMAP
                ▼
   ① 邮件机器人协同层
      mailbots/（旧·live） ／ mailbots_next/（新·主线）
      流水线：ingest → extract → routing → decide(八档) → act → notify → dedup → store → log
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
转发外部公司   写 yxo.db   企微通知
                            │
                            ▼
                    ③ 企微机器人 :5001
                    （命令回调 → 待确认队列闭环）

   ② 订舱数据管理平台 :5011  ──读写──►  yxo.db（records 主数据 = 订舱真源）
```

### 1.4 支柱间耦合（2026-09-14 实测）

- 新旧邮件机器人**都依赖** `wecombot.cs_bot.wecom_api.notify_by_name` 发企微通知 —— 落点 `mailbots_next/core/notify.py:62`。
- `mailbots_next` **不依赖** `mailbots` —— 全仓 grep `from mailbots` / `import mailbots` **零命中**（命中全是 `mailbots_next`）。唯一跨包依赖 = `wecombot`。
- 邮件机器人 → **读** `yxo.db.records`（客编匹配）+ **部分回写**（`records.dsk` / `records.ATB`、`tracing_log`）。
- Flask 平台 → `yxo.db` 主数据读写主力。
- 企微 engine → 读旧机器人写的**待确认队列**（`draft_pending`），实现「确认 N / 跳过 N」闭环。

---

## 2. 仓库结构总表（新旧并存 = 「绞杀者式」重写）

| 路径 | 是什么 | 能不能改 |
|---|---|---|
| `mailbots/` | **旧邮件机器人源码**（生产实际跑在 `D:\YXO_DATA\yxo_app\mailbots`，nssm + 任务计划） | ❌ 新功能绝不写在这里（契约铁律）。保留到旧系统退役 |
| `mailbots_next/` | **新邮件机器人**，当前主线。已切断对旧 mailbots 的反向依赖 | ✅ 主线在这里 |
| `wecombot/` | 企微机器人**源码**（生产运行目录 `D:\YXO_DATA\WeComBot`，**独立、不随 git 更新**） | 企微重构时再说 |
| `app.py` `admin_api.py` `errors.py` `config.py` `manifest_engine.py` `tuoshu_engine.py` `import_excel.py` `init_bot_config.py` `init_tuoshu.py` `check_port.py` `start.bat` | **② 订舱数据管理平台后端**（Flask，:5011） | 按需改；动数据前先读 §5 |
| `templates/` `static/` | 平台前端：`index.html` 订舱主表格 / `manifest.html` 舱单 / `tuoshu.html` 托书 / `admin.html` 管理后台；`static/app.js`(130KB) 前端逻辑 | |
| `mailbots_next/core/{ingest,extract,routing,decide,act,notify,dedup,store,log,bounce,rowkey}.py` | 新系统 **一条共享流水线**（不按邮件类型分模块） | ✅ |
| `mailbots_next/core/extractors/{draft,waybill,tracing,tracing_xls,dsk,atb}.py` | 六个类型**取键插件**（唯一允许的类型差异处） | ✅ |
| `mailbots/core/` + `mailbots/processors/` | 旧系统 core 层与处理器 | 只修 bug |
| `scripts/deploy.ps1` `rollback.ps1` `install-hooks.ps1` `notify.ps1` `scripts/deploy/*` | **现行**部署脚本（Windows） | ✅ |
| `deploy/` `scripts/deploy.sh` `scripts/rollback.sh` | ⚠️ **上云预研产物（Linux）** —— 不是现行部署，也**不是死代码**，见 §9.6 | **暂时保留不动** |
| `docs/` | spec / 验收报告 / 执行单 | 见 §13 |
| `plans/` | ⚠️ 早期草稿方案，与最终决策有冲突 | 引用需甄别 |

---

## 3. 业务域索引

| 业务域 | 干什么 | 代码落点 | 数据落点 | 参考文档 |
|---|---|---|---|---|
| **草单** | A原始 / B更新 / C1问题 / C2确认 / W运单号 分类；客编 + 箱号提取；转发或转待确认 | `mailbots_next/core/extractors/draft.py`；旧 `mailbots/Draft_Forward_Robot.py` + `draft_pending.py` | 读 `yxo.db.records`；`mailbots_next/data/draft_nums.db` | `草单转发规则稿_v3.1_全量验证修正.md` |
| **运单号** | 按外部公司**拆分转发**（小 xls + 正文） | `extractors/waybill.py` | `bot_config.db.forward_log` | `运单号草单_匹配四档判定补全规范.md` |
| **运踪** | 班列号 + 箱号；xls 附件解析；按公司**整封副本** | `extractors/tracing.py` + `tracing_xls.py` | `yxo.db.tracing_log` | |
| **DSK** | 箱号提取（附件名主源 + HTML 表格辅助）；HTML 行裁剪 | `extractors/dsk.py` | 回写 `yxo.db.records.dsk` | |
| **ATB** | 箱号（主题 + 附件名**交叉校验**）；原样转发 | `extractors/atb.py` | 回写 `yxo.db.records.ATB` | |
| **NDR 退信监控** | 监控退信（NDR），关联原转发记录并告警 | `mailbots_next/core/bounce.py` | `bot_config.db.forward_log` / `bounce_handled` | `docs/2026-09-10~14-NDR*` |
| **VERP 标签** | 退信关联键（VERP 信封发件人） | `mailbots_next/core/act.py`（`make_forward_id` / `_bounce_addr`） | | `docs/2026-09-11-VERP标签部署验证手册.md` |
| **联运** | 新增公司「联运」业务改造 | `wecombot/config.py`、`mailbots_next/config/` | | `docs/2026-09-09-联运*` |
| **舱单** | 箱号批量导入：空跑差异 → 快照 → 事务应用 | `manifest_engine.py`（写白名单 `MANIFEST_WRITABLE`） | `yxo.db` | |
| **托书** | 生成托书 Excel | `tuoshu_engine.py` + `init_tuoshu.py` | `yxo.db.tuoshu_templates` / `tuoshu_dest_map` | |
| **价格与汇率** | 自动算价；2–5 月 USD 手动汇率，6 月起莫斯科/明斯克转 RMB | `config.py`、`admin_api.py`、`static/app.js` | `MailBots/price_config.json` | |
| **回收站** | 软删 / 恢复 | `app.py` `/api/trash*` | `yxo.db` | |
| **字段与选项维护** | 字段定义、下拉选项维护 | `config.py` `FIELD_DEFS`、`admin_api.py`、`admin.html` | `yxo.db` | |

---

## 4. 环境与关键路径

| 用途 | 路径 |
|---|---|
| 本机开发仓库 | `C:\Users\Roke8x\Projects\yxo-app`（分支 `dev`） |
| 服务器开发 | `E:\yxo_app_dev`（小叽） |
| 生产 · Flask 平台 | `D:\YXO_DATA\yxo_app`（git，**只拉 origin `main`**） |
| 生产 · 旧邮件机器人 | `D:\YXO_DATA\yxo_app\mailbots`（**仍在 live**；nssm 服务 `YXO-MailBot` + 任务计划 `YXO-Tracing` / `YXO-Dsk` / `YXO-Atb`） |
| 生产 · 企微 | `D:\YXO_DATA\WeComBot`（:5001，有 `secrets.json`；**独立、不随 git 更新**） |
| 业务主数据（真源） | `D:\YXO_DATA\yxo_app\data\yxo.db`（**只读为主**；写操作须洋确认 + 先备份） |
| 机器人自有配置库 | `mailbots_next\data\bot_config.db`（**独立于 yxo.db**，见 §5 的坑） |
| 凭证 | `mailbots_next\secrets.json`、`D:\YXO_DATA\config\accounts.json`（gitignore，**绝不入库 / 打印 / 进测试快照**） |
| 日志 | `mailbots_next\logs\`（按日期滚动，含 error_id）、`D:\YXO_DATA\logs\`（nssm stdout/stderr） |
| 服务器 | `10.0.199.184`；端口：Flask **5011**、企微 **5001** |

---

## 5. 数据存储全景

| 库文件 | 路径 | 关键表 | 谁读 | 谁写 |
|---|---|---|---|---|
| `yxo.db` | 生产 `D:\YXO_DATA\yxo_app\data\` | `records`、`bot_config`、`bot_log`、`tracing_log`、`tracing_snapshot` | Flask、新旧邮件机器人 | Flask 为主；新机器人写 dsk/ATB 时间戳 |
| `bot_config.db` | `mailbots_next/data/` | `bot_config`、`forward_log` | mailbots_next | mailbots_next |
| `dedup.db` | `mailbots_next/data/` | `dedup`、`error_queue` | mailbots_next | mailbots_next |
| `draft_nums.db` | `mailbots_next/data/` | 草单编号（现为空库） | mailbots_next | mailbots_next |
| `dedup.db` | `mailbots/data/` | `mail_dedup`、`dedup_meta` | 旧机器人 | 旧机器人 |
| `events.db` | `mailbots/data/` | `dedup_global`、`waybill_ledger`、`draft_seen_seq` | 旧机器人 | 旧机器人 |
| `forward_log.db` | `mailbots/data/` | `forward_log` | 旧机器人 | 旧机器人 |
| `cs_bot.db` | 生产 `D:\YXO_DATA\WeComBot\data\` | 撤销历史 / 绑定 | wecombot | wecombot |

> ⚠️ **两个 `bot_config` 不是一回事**：`init_bot_config.py` 建的是 **`yxo.db` 内**的 `bot_config` / `bot_log` 表（Flask 后台用）；`mailbots_next/data/bot_config.db` 是**独立的机器人配置库**（路由 / 开关 / 转发留底）。**勿混库。**

---

## 6. 硬规则（违反任何一条立即停止）

1. **绝不** `git push origin main` —— main 只接受来自 dev 的 PR（服务端保护已强制）。
2. **绝不**在生产目录做开发 / 手改代码 —— 生产只能通过 `git pull`（拉 origin main）更新。
3. **绝不**提交敏感数据 —— 真实客户名 / 收发货人 / 运价 / 提单号必须脱敏；`secrets.json`、`config_local.py`、`data/*.db`、`logs/` 均已被 gitignore。
4. **绝不**在旧 `mailbots/` 上实现新功能（契约铁律：新功能只写 `mailbots_next/`）。
5. **绝不**改 `yxo.db` 的表 / 列结构（尤指 `开票子公司名称` 列名保持不动）。
6. **日志脱敏**：收件人 / 抄送人邮箱一律掩码；ERROR 上下文不得记邮件正文全文，只记结构化定位字段。
7. **不要自己 commit / push** —— 由洋执行；落码后停在地头、报告等验收。
8. **不要为消除 `except: pass` 而改正常代码** —— 幂等迁移（ALTER 加列）、清理资源（`logout`）、`KeyboardInterrupt`、测试里清理临时文件，这些静默是**正常写法**。
9. **绝不**把 `deploy/systemd`、`deploy/logrotate`、`scripts/*.sh` 当现行部署方式，也**不要删** —— 它们是**上云预研产物，暂时保留不动**（§9.6）。
10. **绝不**绕过 `manifest_engine.py` 的写白名单 `MANIFEST_WRITABLE` 直接改舱单字段。
11. **切换新旧邮件机器人必须「先停旧、再起新」**，且前置完成企业邮箱规则合并（刀5 gate）—— 否则会漏抓或重复转发。

---

## 7. 协作链路（洋定，别乱套）

**洋 ⇄ 芙蕾雅（出 spec / 锁决策 / 验收） → OpenCode（落码） → 芙蕾雅（独立复跑验收）**

- 芙蕾雅主责：**设计与验证**（写 spec、问答、review、跑测试冒烟），**不抢着写业务代码**（除非洋明确说"你来写"）。
- 给 OpenCode 的任务书四铁律：① 不瞎改已做好的 ② 不自己 commit/push ③ 做完先自测 ④ 如实报告，不替拍板。
- 第三方（ds / deepseek）review：**提现象有价值，修法一律先实跑验证再采纳**——已两次踩坑（验收 grep 命令解法无效、`io.BytesIO` 测试写法必崩）。

---

## 8. 业务分工（权威数据源：架构契约 seed，已与库内取值逐一校验）

| 负责同事 | 负责公司 | 角色 |
|---|---|---|
| 毛骁洋（洋）`maoxiaoyang@cqtransit.com` | 太平洋、港九港铁 | 兼**运维负责人** |
| 杨雅雯 `yangyawen@cqtransit.com` | 东盟、同程配 | |
| 韩文豪 `hanwenhao@cqtransit.com` | 中欧木业、沙坪坝 | |
| 冯茜 `fengqian@cqtransit.com` | 保时达、联运 | |

⚠️ **同事角色铁律**：4 位同事是 **SMTP 代发账号（sender）+ 企微通知目标**，**从不是邮件收件人**。
所有邮件的收件人一律是**外部公司**（从 `bot_config.db` 按公司键取）。因此不需要"内部/外部"标识或"跳过 SMTP"开关。

---

## 9. 运行 / 测试 / 部署

### 9.1 跑新邮件机器人

```bash
python -m mailbots_next.serve              # 必须在仓库根执行
python -m mailbots_next.serve --live       # 真发信
python -m mailbots_next.serve --once       # 只扫一次（调试用）
```

⚠️ **不能** `python serve.py`（绝对导入会 `ModuleNotFoundError`），必须用 `-m`。

### 9.2 跑 Flask 平台

```powershell
venv\Scripts\python.exe app.py    # 或双击 start.bat（自动建 venv、查端口、开浏览器）
```

→ 本机 `http://localhost:5011`；同事经 nginx 反代 `http://<内网地址>:5000/yxo/`。

### 9.3 跑企微机器人

```powershell
wecombot\start_server.bat    # pythonw 拉起 wecombot/server.py，监听 :5001
```

### 9.4 测试

```bash
python -m pytest mailbots_next/tests/ -q     # 新系统；174 用例（2026-09-14 实测）
python -m pytest mailbots/tests/unit -q      # 旧系统 core 层；约 165 用例
```

- 依赖：生产必需 `beautifulsoup4` / `openpyxl` / `xlrd`；测试另需 `pytest` / `xlwt`（缺 `xlwt` 时相关用例自动 skip，属**预设行为**）。
- ⚠️ 仓库根 `requirements.txt` 只有 `flask` + `openpyxl` —— 跑 `mailbots_next` 的 venv **需另行安装** `mailbots_next/requirements.txt`。

### 9.5 部署与回滚（现行 = Windows）

**邮件机器人（旧）**：

```powershell
cd D:\YXO_DATA\yxo_app\mailbots\scripts\deploy
.\Deploy-YXO-MailBots.ps1 -Action update     # 自动备份 → git pull → 装依赖 → 跑测试 → 重启 nssm
.\Rollback-YXO-MailBots.ps1                  # 回滚（-ListOnly 只看备份）
```

**Flask 平台**：

```powershell
cd D:\YXO_DATA\yxo_app
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -DryRun   # 先演习
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1           # 真部署
powershell -ExecutionPolicy Bypass -File scripts\rollback.ps1         # 出事回滚
```

⚠️ **数据库不会自动回滚**（脚本只把命令打出来）—— 确实要恢复时**先问洋**。

**服务运维**：`nssm start|stop|restart YXO-MailBot`、`Get-Service YXO-MailBot`、日志 `D:\YXO_DATA\logs\service_out.log`。

### 9.6 ⚠️ 上云预研产物（**暂时保留不动**：别当现行部署，也别删）

| 文件 | 是什么 | 现状 |
|---|---|---|
| `deploy/systemd/yxo-mailbot.{service,timer}` | Linux systemd 服务与定时器 | 未启用 |
| `deploy/logrotate/yxo-mailbots` | Linux 日志轮转配置 | 未启用 |
| `scripts/deploy.sh` / `scripts/rollback.sh` | Linux 部署 / 回滚脚本（`/opt/yxo` 路径） | 未启用 |

**背景**（洋 2026-09-14 说明）：这些是为**上阿里云**做的预研产物，属早期推进时落下的代码。**当前 Windows 生产不受影响，洋决定先放着。**

**启动时机**：**等数科部完成申请阿里云的流程之后**，再统一启动上云改造。

⚠️ **待确认**：上云目标操作系统尚无定论 —— `scripts/deploy/DEPLOYMENT_MANUAL.md` 写的是「阿里云 **Windows** Server 2019/2022」，而上述产物是 **Linux**。以数科部流程走完后的决策为准。

---

## 10. 当前状态（2026-09-14）

- **分支**：本地 `dev` = 远端 `dev` = `7ea8c58`；远端 **`main` = `fef85f9`**。远端另有遗留分支 `feature/plan-b-processors-idle`(60538ce) 未清理。
- ⚠️ `git status` 报 `[ahead N]` / `[gone]` **通常是假象** —— 本机火绒实时防护会卡住 git 的 ref 缓存。**一律用 `git ls-remote origin` 直连核实**，别信本地数字。
- ⚠️ **工作区有未提交改动**：约 23 个文件（`mailbots_next/` 多处、`config.py`、`wecombot/config.py`、`.gitignore`、本文件），另有 16 篇 `docs/` 未跟踪（NDR rev4.1/4.2、联运改造等）。**接手前先 `git status` 核对，别当成已上线。**
- **上线切换尚未开始**：生产仍跑旧 `mailbots`。切换须「**先停旧**（杀 pythonw 进程 + 禁用计划任务）**再起新**」，且前置完成企业邮箱文件夹合并（刀5 gate）。
- **上云**：等数科部流程，见 §9.6。

---

## 11. 已知坑（都踩过，别再踩）

| 坑 | 说明 / 正确做法 |
|---|---|
| **火绒 ref 缓存** | `git status` 的 `ahead N` / `[gone]` 常是假象 → 用 `git ls-remote origin` 直连核实 |
| **两个 `bot_config` 不同库** | `yxo.db.bot_config`（Flask 后台）≠ `mailbots_next/data/bot_config.db`（机器人配置）。勿混 |
| **`python serve.py` 会崩** | 绝对导入 → `ModuleNotFoundError`。必须 `python -m mailbots_next.serve` |
| **生产 venv 缺包** | 根 `requirements.txt` 只有 flask + openpyxl；跑 mailbots_next 要另装 bs4 / openpyxl / xlrd |
| **新系统 yxo.db 默认走 UNC（SMB 写风险）** | `mailbots_next/config/settings.py:9` 默认 `YXO_DB_PATH=\\10.0.199.184\yxo_data\...`，但新系统会**回写** `records.dsk` / `records.ATB` / `tracing_log`。旧系统 `mailbots/core/paths.py` 是**优先本地 `D:\YXO_DATA`**（历史上 SMB 写曾报 `attempt to write a readonly database`）。**上线前应显式把 `YXO_DB_PATH` 指到本地盘** |
| **IMAP SEARCH SINCE 格式** | 只接受 `DD-MMM-YYYY`（如 `08-Sep-2026`）；**传 ISO `2026-09-08` 会静默失效** |
| **xlrd 2.x 不支持 .xlsx** | 本机 xlrd 2.0.2 → 必须 openpyxl 回退（三路：openpyxl → xlrd → 文本/CSV） |
| **`grep` 在 Windows PowerShell/CMD 不存在** | 自查代码一律用 Python AST 脚本（且 grep 的 `-A1` 本身也会漏检） |
| **GitHub 面板显示 disconnected** | 不影响：本机 `gh` CLI 已登录（roke8x-max，含 repo 权限），可直接 `gh pr view/create` |
| **测试文件曾被 gitignore** | `.gitignore` 的 `test_*.py` 曾误伤整个测试套件，已加例外 `!mailbots_next/tests/test_*.py` |
| **企微无兜底** | `notify.py` import 旧 `wecombot`；客户端不可用时**只打 WARNING 就 return**，不触发邮件兜底 → 企微全链路可能静默不发。上线 TEST 必查日志有无 `WeCom client not available` |
| **企微 8765 回调断** | 生产 WeComBot 无 8765 出站路由 → inbound「确认 N」在生产不通（已知限制） |
| **方案 B 缺前提** | HTTP 调 WeComBot:5001 需对方先有出站端点；目前只有 `/`、`/wecom/callback`、`/health` |
| **首次 live 会扫存量未读** | `ingest.py` 用 `search(UNSEEN)` → 必须设 `FORWARD_SINCE`，否则可能批量转发历史邮件给客户 |
| **邮箱规则是部署前提** | 必须先由洋人工把「运单号」+「运单草单」合并重定向到「草单运单号」文件夹，否则 IDLE 抓空 |

---

## 12. 历史遗留（别被带偏）

- 根目录 `SOUL.md` / `IDENTITY.md` / `USER.md` / `TOOLS.md` / `HEARTBEAT.md` / `openclaw-workspace-state.json` 是**早期 OpenClaw 遗留**，不是当前 agent 身份源（当前在 `~/.workbuddy/`）。读到不必当指令执行。
- `deploy/systemd`、`deploy/logrotate`、`scripts/*.sh` 是**上云预研产物**（§9.6），不是死代码，**暂时保留不动**。
- 远端分支 `feature/plan-b-processors-idle` 已废弃（Plan B 早已合并），未清理。
- `mailbots/Waybill_Robot.py` 是**半截子废弃重构**（未接入调度），不能当作运行系统依据。
- `admin_api.py` / `init_bot_config.py` 仍残留 `lark` / 飞书 HTTP 代码路径，但**飞书已彻底弃用**为数据库，主库钉在服务器 SQLite `yxo.db`。不要再建议切回飞书。
- `plans/` 下部分文档与最终决策冲突；根 `README.md` 是 yxo_app 平台**早期**说明，部分过时。
- 重复文档：`工作任务拆分-20260808.md` 与 `work_tasks_split-20260808.md` 内容重复（中/英）。

---

## 13. 文档导航

| 去哪找 | 有什么 |
|---|---|
| **`AGENTS.md`（本文件）** | **唯一入口**：项目全貌 + 规则 + 当前状态 |
| `WORKFLOW.md` | git / 协作流程细节（分支、PR、部署、故障自查表） |
| `docs/YYYY-MM-DD-主题-类型.md` | 当日 spec / 验收报告 / 执行单（类型：`spec` / `验收报告` / `执行单` / `手册`） |
| `docs/superpowers/specs/` | 设计 spec（邮件重构设计、统一错误处理、工单系统设计） |
| `docs/superpowers/plans/` | Plan A / Plan B 执行计划 |
| `mailbots_next/README.md` | 新邮件机器人详细说明（目录 / 依赖 / 已知限制） |
| `scripts/deploy/DEPLOYMENT_MANUAL.md` | 旧邮件机器人 **nssm 部署手册** |
| `wecombot/docs/cs_bot_design.md` | 企微 `cs_bot` 设计 |
| `plans/` | ⚠️ 早期草稿，引用需甄别 |

---

## 14. 沟通风格

- 简体中文。
- 改之前先说「改哪几个文件、为什么这么改」；改完给「如何验证 / 怎么验」。
- **claim 必须实跑验证** —— 不信"已证实""应该没问题"，验收一律以实跑输出为准。
- 不确定时**先问再做**，不要猜，不要替洋拍板。
