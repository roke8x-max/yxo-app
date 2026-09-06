# mailbots_next

统一邮件处理流水线 · 绿地重写替代原 5 个独立机器人脚本

## 简介

`mailbots_next` 是邮件机器人的新一代架构，采用 **单一流水线 + 配置驱动** 设计，替代原有 5 个互相独立、逻辑重复的脚本（草单、运单号、运踪、DSK、ATB）。

核心特性：
- **统一入口** (`serve.py`)：3 组 IDLE 进程监听 5 个 IMAP 文件夹
- **配置驱动**：类型识别、提取键、路由、八档判定、开关全部为配置/数据，不写死代码分支
- **按行处理**：一封邮件解析出多行，每行独立跑八档，互不影响
- **统一去重**：单一共享 SQLite 去重库，claim-before-act 防并发重复转发
- **统一企微通知**：单一 `notify.py` 模块，outbound + inbound 端点
- **错误队列 + 恢复扫描**：`error_queue` 持久化失败记录，sweeper 定时重试，人工介入接口

## 目录结构

```
mailbots_next/
├── serve.py                 # 单一入口：读配置、启 3 个 IDLE 组、注册 shutdown、HTTP inbound 端点
├── config/
│   ├── __init__.py
│   ├── settings.py          # 全局常量、路径、MODE、IDLE 分组、正则
│   ├── types.py             # EmailType 枚举、提取键、TYPE_ROUTES 双轨路由规则表
│   ├── routing.py           # (保留兼容) 旧路由配置
│   ├── provider.py          # ★ 配置单一门禁：结构层常量 + runtime 覆盖 + snapshot()
│   └── secrets.py           # 凭证外置加载（secrets.json / 环境变量）
├── core/
│   ├── __init__.py
│   ├── log.py               # 专用日志模块（logs/ 按天滚动，error_id 追溯，脱敏）
│   ├── dedup.py             # 统一去重 + error_queue（claim-before-act，WAL+busy_timeout）
│   ├── store.py             # yxo.db 只读 + bot_config.db 读写（负责人/收件人/运行开关）
│   ├── ingest.py            # IMAP IDLE 长连接（5 文件夹 × 4 账号 = 20 连接）+ 批量标已读
│   ├── extract.py           # 通用解析 → 分发到 extractors/* 插件
│   ├── extractors/
│   │   ├── __init__.py
│   │   ├── draft.py         # 草单：A/B/C1/C2/OTHER 分类、客编+箱号提取
│   │   ├── waybill.py       # 运单号：xls 解析、按外部公司拆分转发
│   │   ├── tracing.py       # 运踪：班列号+箱号、xls 解析、按公司整封副本
│   │   ├── dsk.py           # DSK：箱号提取（附件名主源、HTML 表格辅助）、HTML 行裁剪
│   │   └── atb.py           # ATB：箱号（主题+附件名交叉校验）、原样转发
│   ├── routing.py           # 主数据匹配：records → 负责公司 → 负责人/收件人/抄送人
│   ├── decide.py            # 八档判定引擎（规则表驱动，仅草单/运单号/DSK/ATB）
│   ├── act.py               # 动作执行：转发(含拆分)/告警/待办、SMTP 发送结果校验
│   └── notify.py            # 企微集中模块 + 每日汇总计数器
├── data/                    # 运行期产物（gitignored）：dedup.db / bot_config.db / draft_nums.db / daily_counters.json
├── logs/                    # 专用日志文件夹（按日期滚动）
└── tests/                   # 单测（含 test_inbound.py 强制 4 场景、test_notify_email.py、test_core.py）
```

## 环境依赖

- Python 3.10+
- 依赖包：见项目根目录 `requirements.txt`
- 系统依赖：`xlrd`、`openpyxl`、`xlwt`、`beautifulsoup4`、`requests`

## 配置

所有配置通过 `config/settings.py` 统一管理，支持环境变量覆盖：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MAILBOT_MODE` | `test` | `test` 只打印不发送/不标已读/不写业务库；`live` 真实运行 |
| `YXO_DB_PATH` | `\\10.0.199.184\yxo_data\yxo_app\data\yxo.db` | 业务主数据库路径 |
| `INBOUND_PORT` | `8765` | inbound HTTP 端点端口（回环地址） |
| `INBOUND_SHARED_SECRET` | `""` | inbound 鉴权密钥，**生产必须配置非空** |
| `SWEEP_INTERVAL_SEC` | `300` | sweeper 扫描间隔(秒) |
| `SWEEP_MAX_RETRY` | `3` | 重试上限，超限转 manual + E1 告警 |
| `DIGEST_HOUR` | `9` | 每日汇总发送时刻(09:00) |
| `MAIL_PWD_<email>` | - | SMTP 密码（如 `MAIL_PWD_maoxiaoyang_cqtransit_com`） |

**凭证文件**：`mailbots_next/secrets.json`（gitignored，参考 `secrets.example.json`）

```json
{
  "ACCOUNTS": {
    "maoxiaoyang@cqtransit.com": "password1",
    "yangyawen@cqtransit.com": "password2",
    "fengqian@cqtransit.com": "password3",
    "hanwenhao@cqtransit.com": "password4"
  }
}
```

**运行开关（热更新）**：写入 `bot_config.db` 表 `bot_config`，`scope='runtime'`，`bot=<type>`，`key='ENABLED'`，`extra='{"enabled": true}'`。缺行时用代码默认 `enabled=True` + 记一次 INFO。

**负责人映射（seed 写死）**：`bot_config` 表 `scope='owner'`，`bot='all'`，`key=<公司名>`，`extra='{"email": "<同事邮箱>"}'`：
- 太平洋/港九港铁 → `maoxiaoyang@cqtransit.com`
- 东盟/同程配 → `yangyawen@cqtransit.com`
- 中欧木业/沙坪坝 → `hanwenhao@cqtransit.com`
- 保时达 → `fengqian@cqtransit.com`

**收件人/抄送人**：`bot_config` 表 `scope='company'`，`bot='all'`，`key=<公司名>`，`to_addrs`/`cc_addrs` 为 JSON 数组。统一外部收件人源，5 类邮件共用。

## 运行

```bash
# TEST 模式（默认，只打印不发送/不标已读）
python serve.py

# LIVE 模式（真实发送/标已读/写库）
python serve.py --live

# 单轮扫描退出（冒烟测试）
python serve.py --once

# 轮询降级（禁用 IDLE，每 N 秒轮询）
python serve.py --poll-secs 60

# 错误队列管理
python serve.py --errors list
python serve.py --errors retry --error-id 123
python serve.py --errors resolve --error-id 123
```

### 子命令：错误队列管理

| 命令 | 说明 |
|------|------|
| `--errors list` | 列出所有 pending + manual 记录 |
| `--errors retry --error-id N` | 将指定记录标记为 resolved（重试） |
| `--errors resolve --error-id N` | 将指定记录标记为 resolved（人工确认已处理） |

### inbound 端点（手动转发闭环）

- 地址：`POST http://127.0.0.1:<INBOUND_PORT>/internal/forward`
- 鉴权：Header `X-Forward-Secret` == `INBOUND_SHARED_SECRET`（非空时强制校验）
- 请求体：`{"error_id": <int>}`
- 响应：`200` 成功 / `400` 请求体非法 / `401` 鉴权失败 / `404` 记录不存在或已处理 / `500` 转发异常
- 去重：走 `error_queue` 状态机原子认领（`claim_manual_forward`），防并发双发

## 核心架构

### 双轨类型识别（TYPE_ROUTES）

| 类型 | 识别方式 | 优先级 |
|------|----------|--------|
| waybill | `sender_whitelist: ["docwbfb@yxologistics.com"]` | 10 |
| atb | `sender_whitelist: ["atb@yxologistics.com"]` | 20 |
| dsk | `sender_whitelist: ["kasa@rtsb.de", "reex.mala@deutschebahn.com"]` | 30 |
| tracing | `sender_whitelist: ["tracing-system@yxologistics.com"]` | 40 |
| draft | `attachment_pattern: "已加密|箱号"` + `sender_exclude` 排除系统发件人 | 50 |

- **folder 仅作 IDLE 分组提示，不参与类型判定**，合并文件夹不破坏识别
- 优先级低者先命中即停

### 统一流水线

```
IDLE 监听（3 组）
   └─► 收件(ingest) ─► 解析提取(extract, 按类型插件) ─► 主数据匹配(routing)
        └─► 八档判定(decide) ─► 动作(act: 转发/告警/待办, 含拆分)
              └─► 企微通知(notify) ─► 写库(store)
                             │
                             └─► 日志与错误上报(log，贯穿全程，无静默)
```

### 八档判定（仅草单/运单号/DSK/ATB）

| 档 | 条件 | 处置 |
|----|------|------|
| T1 | 客编精确唯一 / 箱号唯一 | 自动转发 + 企微通知 |
| T0 | 无客编且(无箱号/箱号0命中) | 报警(运维+负责同事) |
| T4 | 序号不在库 | 报警 |
| T5 | 无客编+箱号唯一 | 报警 |
| T2 | 序号同+箱号同+到站后缀异 | 待办 |
| T3 | 序号同+箱号异 | 待办 |
| T6 | 箱号被≥2票复用 | 待办(DSK/ATB默认报警，待D1调整) |
| T7 | 序号跨≥2未退舱后缀 | 待办(防御性) |

- **序号/到站后缀** 从 `records.客户编码` 按 `-` 拆分（序号=整段数字段，非 `seq` 列；后缀=末尾短码，与 `目的站` 仅参考映射）
- 解析范围：`WHERE 状态<>'退舱' AND is_deleted=0`

### 转发拆分规则

| 类型 | 拆分行为 |
|------|----------|
| 运踪 | 整封副本 × 命中公司数，不裁内容，箱号仅作门控 |
| 草单/运单号/DSK | 多箱号表格 → 按行拆分，附件按箱号匹配随行，其余正文/标题/无关附件原样带入每封 |
| ATB | 单箱号，原样转发（不改标题） |
| DSK特例 | HTML 行裁剪仅保留当前箱号行；拆分后标题追加客编（从主数据反查 `客户编码`） |

### 错误队列 + 恢复扫描

- 表：`error_queue`（同 dedup.db，字段：id/message_id/row_key/stage/error_type/error_detail/error_id/attempt_count/last_attempt_at/status/created_at/raw_hex/account/folder/uid/subject/sender/date/extra）
- 入队：任一环节异常且无法当场处理 → 记日志 + 入队（status=pending，**附带重放原文与账号/文件夹/uid**），邮件不标已读
- 扫描：独立 sweeper，周期 `SWEEP_INTERVAL_SEC`（默认 300s），每条 pending **真实重放整封邮件**（重放前释放原认领，重放内重新认领，去重仍是唯一真相源）；成功则关闭条目（uid 非零且同封无其他未决行时入队标已读），失败则计数
- 重试上限 `SWEEP_MAX_RETRY`（默认 3）→ 转 manual + 首报 E1 一次（`extra.notified_manual_at` 防重发）+ 每日 09:00 汇总未解决项持续告警
- 人工接口：`python serve.py --errors list|retry|resolve`

### IDLE 拓扑与批量标已读

- **每 (文件夹 × 账号) 一条 IDLE 连接**：5 文件夹 × 4 账号 = **20 条**（旧组拓扑 12 条）。`IDLE_PER_FOLDER=0` 可回退旧拓扑；`MAX_IDLE_SEC`（默认 1740s）可配；每次 `idle_check` 后必调 `idle_done()`；IDLE 连接只读（`readonly=True`）。
- ⚠ **服务器并发上限需实测**：20 条长连接是否触顶取决于邮箱服务商限制，上线前必须在测试环境验证连接稳定性，触顶时用 `IDLE_PER_FOLDER=0` 回退。
- **批量标已读（方案 B）**：转发成功后只入队 `(account, folder, uid)`；后台线程按该二元组聚合，每 `MARK_SEEN_FLUSH_SEC`（默认 300s）或每批 `MARK_SEEN_BATCH_CAP`（默认 100）用一次短连接单条 `STORE "uid1,uid2,..."` 刷盘。失败整批重试 1 次，仍失败记 WARN + 计数，不进 error_queue；进程退出时 flush 残留批次。

### 日志与错误上报

- 专用日志：`logs/mailbot_YYYY-MM-DD.log`，含 `error_id` 追溯
- 分流：
  - 程序错误（异常/DB/去重/IMAP/配置）→ 日志 + 企微 **仅运维负责人**
  - 业务告警/待办（T0/T4/T5/T2/T3/T6/T7）→ **运维负责人 + 负责同事**
- 脱敏：凭证不记、收件人邮箱掩码、正文不全量落盘

## 测试

```bash
# 运行所有测试
pytest mailbots_next/tests/ -v

# 运行特定测试
pytest mailbots_next/tests/test_inbound.py -v
pytest mailbots_next/tests/test_notify_email.py -v
pytest mailbots_next/tests/test_core.py -v
```

**强制测试场景**（`test_inbound.py` 必须全绿）：
1. 确认 N 成功 → `error_queue.status: manual→resolved` + `forward_email` called once
2. 并发防双发 → 两线程同确认同 `error_id`，仅一次转发成功
3. 无效 ID → 404，不调用 SMTP
4. 鉴权拦截 → 无 Secret/错 Secret → 401，不调用 SMTP

## 部署与切换

1. 新代码部署到测试环境，运行 `python serve.py`（TEST 模式）
2. 运维负责人用真实邮箱实测，验证 5 类邮件路由输出符合萃取清单
3. 灰度切换：先停旧 `mailbots/` 调度，启 `mailbots_next` LIVE
4. 全量通过后归档旧脚本

**注意**：
- 旧 `mailbots/` 保留运行直到测试环境实跑通过
- 不在原 `mailbots/` 文件上改，新功能只在 `mailbots_next/`
- `config/provider.py` 为配置唯一门禁，未来改 YAML/JSON 只动此处

## 合规与数据驻留

- 业务主数据仅读 `yxo.db`（生产机 `\\10.0.199.184\yxo_data\yxo_app\data\yxo.db`）
- 机器人自身配置/去重/状态库落**服务器本地盘**（`D:\YXO_DATA\...` 或 `mailbots_next/data/`），**严禁 SMB/UNC**（SQLite WAL 并发锁不可靠）
- 凭证外置 `secrets.json` / 环境变量，**绝不入库、绝不打印、绝不写测试快照**
- 日志脱敏基线：凭证/密码不记、收件人邮箱掩码、正文不全量落盘、DEBUG 级正文需显式开启