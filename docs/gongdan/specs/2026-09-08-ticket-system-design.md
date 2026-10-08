# 工单系统（事业部 ↔ 渝新欧 待办协同）设计 Spec

> ⚠️ 设计稿 · 未落码（2026-09-24 归档）：本文是设计阶段产物，代码尚未实现。
> 归档目的是保留“设计意图”供将来参考；看到本文不代表相关模块已落码。
> ⚠️ 元信息更正（2026-09-24）：抬头原写「尚未落码、未 commit」；归档入库后「未 commit」不再成立，已删去（只留「尚未落码」）。另：文中出现的 `dev` 分支已于 2026-09-18 废弃 —— 现状只用 `main`（见 `WORKFLOW.md`）。

> 日期：2026-09-08（初稿）｜修订：2026-09-09（骁洋拍板 3 个坑点 + 新增 N1/N2/N3）
> 状态：**最终版，待你最后确认**（**尚未落码** —— 代码未实现）
> 代码基准：yxo-app @ dev 分支，并入仓库新目录 `ticket_system/` + 独立模块 `yxo_auth/`
> 关联：AGENTS.md（硬规则）、WORKFLOW.md（分支与部署流程）、scripts/deploy/DEPLOYMENT_MANUAL.md（nssm 部署）
> 本文是新子系统的唯一设计真相源；未被本文覆盖处，以 AGENTS.md 为准。

---

## 0. 拍板决策汇总（D1–D15 + 新增需求 N1–N4）

| # | 决策 | 内容 |
|---|---|---|
| D1 | 隔离方式 | **双端口 + 个人账号并存**。端口负责"分流与默认视图"，账号负责"安全与身份"。端口不是安全边界。 |
| D2 | 账号范围 | 账号体系做成**独立可复用模块 `yxo_auth/`**，工单系统是其第一个调用方；本次**不动**主系统（5011）鉴权。 |
| D3 | 外部账号粒度 | 渝新欧侧**一个共享账号**；4 位负责人各一个个人账号。 |
| D4 | 状态模型 | **"球在谁场地"**。工单只存 1 个状态字段 + 1 个"最后动作方"字段，渲染时按访问者身份翻译中文词。 |
| D5 | 关联粒度 | 一个工单可关联**多个**客编号/箱号，系统逐个查订舱库自动算出涉及的负责人集合。 |
| D6 | 完成判定 | **双方都能手动关闭**；且当全部负责人槽位提交完结论时**自动**关闭。 |
| D7 | 排序 | **待办区**：紧急优先，组内按最新回复倒序。**已完成区**：按完成时间倒序**固定**，新回复不置顶（D15）。 |
| D8 | 多人分工 | 工单内**按负责人分槽位**；详情页上层槽位进度区 + 下层对话流。 |
| D9 | 备注语义 | **不设独立便签**。每条发言即一条带时间戳的日志，与结论消息在同一时间轴混合显示。 |
| D10 | 部署方式 | 服务器 + nginx 反代公网（与现有订舱系统一致）。 |
| D11 | 架构方案 | **方案 A：双进程共享代码与库**。5021/5022 两个进程跑同一套代码，角色由启动参数区分，共享 `tickets.db`。 |
| D12 | 代码落点 | 并入 yxo-app 仓库新目录，复用现有 dev→PR→main 与 nssm 部署套路。 |
| D13 | 渝新欧端可见负责人 | 5022 列表卡片**显示**负责人标签（便于对方知道找谁催），但**顶部不给筛选器**——负责人是内部分工概念，不做成外部可用的筛选维度。 |
| D14 | 手动关闭的槽位处理 | 手动关闭时**强制把剩余 `pending` 槽位一并置为 `done`**，并写入一条 `system` 消息记录原因，避免"工单已完成、但有人槽位还挂着"的矛盾数据。 |
| D15 | 已完成工单的后续进展 | 已完成区按 `closed_at` 倒序**固定排列**，新回复不置顶。有新进展请**新建工单并手动引用原单号**，v1 **不做**工单间自动关联。 |

### 新增需求（2026-09-09 追加）

| # | 需求 | 内容 |
|---|---|---|
| N1 | 全局搜索 | 主界面顶部搜索框，**同时检索**工单标题 / 所有消息正文 / 关联客编号 / 关联箱号；结果以工单卡片列表展示，点击进详情页并**高亮关键词**。v1 用 `LIKE '%关键词%'`，数据量小，不引入 FTS5。 |
| N2 | 附件上传 | 详情页支持 **Ctrl+V 粘贴图片 / 拖拽 / 按钮选择**；单文件 ≤ 20MB，白名单 jpg/png/pdf/docx/xlsx；`messages.attachments` 存 JSON 数组；实体存 `ticket_files\{工单号}\`。 |
| N3 | 复制分享文案 | 详情页「复制分享文案」按钮，一键生成含 `@负责人` 的纯文本并写入剪贴板。**纯前端，不接微信开放平台**；`@xxx` 是普通文本，靠微信群昵称匹配生效。 |
| N4 | 企微通知 | **本次不做**，留待后续评估。 |

### 拍板后仍需你确认的 4 个点（不影响落码，可在实现期调整）

| # | 项 | 本文采用的默认值 | 说明 |
|---|---|---|---|
| C1 | 公网是否强制 HTTPS | **是，要求配 HTTPS** | 现有订舱系统是 HTTP 明文公网。本系统带登录，明文传密码风险更高（§8 R1）。**另：N3 的剪贴板 API 在非 HTTPS 下会失效**，见 R5。 |
| C2 | 端口与公网路径 | 5021→`/ticket/`，5022→`/partner/` | 避开与 5011/`/yxo/` 冲突；路径名好记但不易猜。 |
| C3 | 工单是否要标题字段 | **要**（必填，≤50 字） | 卡片需要一个可辨识的一句话；详情放正文。不想要可删。 |
| C4 | ~~渝新欧端负责人筛选器~~ | **已由 D13 拍板** | 卡片显示负责人标签，顶部不给筛选器。 |
| C5 | 建单后能否修改关联项 | **v1 不支持**（填错就关闭重建） | 若你希望支持"重新关联"，实现成本很低（§4.3 的重建槽位逻辑已覆盖），说一声就加。 |

> 说明：N3 生成分享链接时需要工单的**公网完整 URL**，依赖 `PUBLIC_BASE_URL` 配置（§7.6），与 C2 的端口/路径方案配套。

---

## 1. 背景与目标

微信群（事业部 4 人 + 渝新欧操作多方）里，待办事项与后续沟通混在一起，关键工作被聊天冲掉。

本系统把"待办"从群里抽出来，做成**双方共用的结构化工单列表**：事项有明确状态、有负责人归属、有可追溯的对话历史。

**非目标（本次不做）**：
- 不接邮件机器人、不接企微通知（N4）、不接订舱库写操作。
- 不做工单间自动关联（D15，改为手动引用原单号）、不做工单指派流转审批、不做统计报表。
- 不给主系统 5011 加鉴权（D2）。
- 附件不做服务端缩略图生成、不做在线预览（N2 仅提供原文件查看/下载）。

---

## 2. 总体架构

### 2.1 进程与端口（方案 A）

```
nginx（公网，HTTPS 终结）
 ├─ /ticket/   → 127.0.0.1:5021   进程 A  ROLE=internal  事业部端
 └─ /partner/  → 127.0.0.1:5022   进程 B  ROLE=partner   渝新欧端
```

- 两个进程跑**同一份代码** `ticket_system/app.py`，角色由环境变量 `TICKET_ROLE` 决定。
- 两进程**共享同一个** `tickets.db`（见 §2.3 并发说明）。
- 进程管理沿用 **nssm**，两个 Windows 服务：`YXO-Ticket-Internal`、`YXO-Ticket-Partner`。

### 2.2 目录与路径

| 环境 | 代码 | 数据 |
|---|---|---|
| 本机开发 | `C:\Users\Roke8x\Projects\yxo-app\ticket_system\` | `...\ticket_system\data\tickets.db` |
| 服务器开发 | `E:\yxo_app_dev\ticket_system\` | `E:\yxo_app_dev\ticket_system\data\tickets.db` |
| 服务器生产 | `D:\YXO_DATA\yxo_app\ticket_system\` | **`D:\YXO_DATA\data\tickets.db`** |

- 生产数据目录与 `yxo.db` 同级（`D:\YXO_DATA\data\`），复用现有备份与运维路径。
- `.gitignore` 增加 `ticket_system/data/`（数据库不入库）。

**附件目录（N2）**，与 `tickets.db` 同级，按工单号分目录：

| 环境 | 附件根目录 |
|---|---|
| 本机开发 | `...\ticket_system\data\ticket_files\{工单号}\` |
| 服务器开发 | `E:\yxo_app_dev\ticket_system\data\ticket_files\{工单号}\` |
| 服务器生产 | **`D:\YXO_DATA\data\ticket_files\{工单号}\`** |

**硬约束 H6：附件目录同样必须位于服务器本地磁盘**（同 H1），且**必须纳入备份**（H2 补充：备份需额外带走整个 `ticket_files\` 目录，否则附件丢失）。

### 2.3 双进程共享一个 SQLite 的并发说明

**结论：对业务端无感知。**

- SQLite 写锁为整库级，同一时刻只允许一个写者；但单条写入耗时约 1～5 毫秒。
- 必须设置：`PRAGMA journal_mode=WAL`、`PRAGMA busy_timeout=5000`。
  WAL 让读写互不阻塞；`busy_timeout` 让写冲突时**排队等**而非立刻抛 `database is locked`。
- 本系统并发量接近零（人工打字提交），实测不会发生可感知等待。

**硬约束 H1：`tickets.db` 必须位于服务器本地磁盘，禁止放在 UNC 网络共享路径。**
SQLite 的文件锁在 SMB/NFS 上不可靠，官方明确不推荐。两进程同机，直接读写 `D:\YXO_DATA\data\tickets.db`。

**硬约束 H2：备份必须同时带走 `tickets.db` + `tickets.db-wal` + `tickets.db-shm`**（WAL 模式下缺 `-wal` 会丢最近写入）。

---

## 3. 账号模块 `yxo_auth/`（独立可复用）

按 D2，这是一个**不依赖工单系统**的独立模块，未来主系统可直接接入。

### 3.1 设计约束

- 模块**只接受外部传入的 db 路径**初始化（`init_auth(db_path)`），不写死任何工单概念。
- 表前缀统一 `auth_`（`auth_users` / `auth_sessions`），与主业务表混存不冲突。
- 不依赖 Flask 全局 `g`，通过显式参数传递；提供薄适配层给 Flask 用。

### 3.2 表结构

```sql
CREATE TABLE auth_users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,      -- 登录名，英文/拼音
  display_name  TEXT NOT NULL,             -- 显示名，如 冯茜
  role          TEXT NOT NULL,             -- internal | partner
  owner_key     TEXT,                      -- 仅 internal：对应负责人 key（见 §5.2），partner 为 NULL
  password_hash TEXT NOT NULL,             -- pbkdf2_sha256，绝不明文
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL
);

CREATE TABLE auth_sessions (
  token       TEXT PRIMARY KEY,            -- 随机 32 字节 hex
  user_id     INTEGER NOT NULL,
  role        TEXT NOT NULL,               -- 冗余，便于按角色清理
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL
);
```

### 3.3 初始账号

| username | display_name | role | owner_key |
|---|---|---|---|
| maoxiaoyang | 毛骁洋 | internal | maoxiaoyang |
| fengqian | 冯茜 | internal | fengqian |
| yangyawen | 杨雅雯 | internal | yangyawen |
| hanwenhao | 韩文豪 | internal | hanwenhao |
| yxo_ops | 渝新欧操作 | partner | NULL |

- 初始密码由运维在服务器生成并线下分发，**不入库、不写文档**。
- 密码用 `werkzeug.security.generate_password_hash`（默认 pbkdf2:sha256）或等价实现。
- 会话 cookie：`HttpOnly` + `SameSite=Lax`；启用 HTTPS 时加 `Secure`（依赖 C1）。

### 3.4 登录流程

1. 未登录访问任意页面 → 302 到本端口的 `/login`。
2. 登录页**校验角色**：`internal` 账号不能登 5022，`partner` 账号不能登 5021（防止一侧地址泄露后被交叉登录）。
3. 校验通过后写 `auth_sessions`，下发 cookie，跳回原页面。
4. 连续 5 次失败 → 锁定该 username 10 分钟（内存计数即可，无需持久化）。

---

## 4. 数据模型（`tickets.db`）

### 4.1 tickets（工单主表）

```sql
CREATE TABLE tickets (
  id              INTEGER PRIMARY KEY,
  title           TEXT NOT NULL,             -- 事项一句话，≤50 字（C3）
  urgency         TEXT NOT NULL,             -- urgent | normal
  status          TEXT NOT NULL,             -- open | progress | closed
  last_actor_role TEXT NOT NULL,             -- internal | partner，「球在谁场地」
  created_by      TEXT NOT NULL,             -- display_name 快照
  created_by_role TEXT NOT NULL,
  created_at      TEXT NOT NULL,             -- YYYY-MM-DD HH:MM
  last_reply_at   TEXT NOT NULL,             -- 排序键
  closed_at       TEXT,
  closed_by       TEXT
);
CREATE INDEX idx_tickets_board ON tickets(status, urgency, last_reply_at DESC);
```

### 4.2 ticket_refs（关联的客编号 / 箱号）

```sql
CREATE TABLE ticket_refs (
  id          INTEGER PRIMARY KEY,
  ticket_id   INTEGER NOT NULL,
  ref_type    TEXT NOT NULL,                -- customer_code | container_no
  ref_value   TEXT NOT NULL,                -- 原始输入值，原样保存
  match_state TEXT NOT NULL,                -- matched | unmatched
  company     TEXT,                         -- 匹配到的开票子公司名称
  owner_key   TEXT                          -- 解析出的负责人 key，未匹配为 NULL
);
CREATE INDEX idx_refs_ticket ON ticket_refs(ticket_id);
```

一个工单可有多条 `ticket_refs`（D5）。

### 4.3 ticket_slots（负责人槽位）

```sql
CREATE TABLE ticket_slots (
  id           INTEGER PRIMARY KEY,
  ticket_id    INTEGER NOT NULL,
  owner_key    TEXT NOT NULL,               -- 负责人 key
  state        TEXT NOT NULL,               -- pending | done
  submitted_at TEXT,
  submitted_by TEXT,
  UNIQUE(ticket_id, owner_key)
);
```

- 建单/编辑关联项时，按解析出的负责人集合**重建**槽位（已 `done` 的槽位保留，不重置）。
- 负责人集合为空（全部未匹配）→ **该工单没有槽位**，只能手动关闭。

### 4.4 messages（对话流 / 时间轴）

```sql
CREATE TABLE messages (
  id          INTEGER PRIMARY KEY,
  ticket_id   INTEGER NOT NULL,
  author_role TEXT NOT NULL,                -- internal | partner
  author_name TEXT NOT NULL,                -- display_name 快照
  author_key  TEXT,                         -- internal 记负责人 key；partner 为 NULL
  body        TEXT NOT NULL DEFAULT '',     -- 纯附件消息允许为空（N2）
  msg_type    TEXT NOT NULL,                -- normal | conclusion | system
  parent_id   INTEGER,                      -- 引用回复：指向被引用消息 id（D9 功能A）
  attachments TEXT,                         -- N2：JSON 数组，见下；无附件为 NULL
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_messages_ticket ON messages(ticket_id, id);
```

`msg_type` 三态：

| 值 | 含义 | 时间轴渲染 |
|---|---|---|
| `normal` | 普通发言 | 普通气泡 |
| `conclusion` | 负责人提交结论（D9） | **高亮底色 + `[结论]` 标签** |
| `system` | 系统日志（如 D14 手动关闭记录） | 居中灰底小字，只读，**不参与**"按负责人筛选对话" |

- 建单时的正文作为第一条 `normal` 消息落库。
- `body` 为空且 `attachments` 非空 → 纯附件消息，正常渲染为附件卡片。

**`attachments` JSON 结构（N2）**

```json
[
  {"name": "报关单.pdf", "path": "1024/20260909_103000_a1b2c3_报关单.pdf", "size": 204800}
]
```

- `path` 为**相对 `ticket_files\` 根目录的相对路径**（含工单号子目录），便于整体搬迁；读取时拼接当前环境的根目录。
- `size` 单位字节，用于卡片显示（自动换算 KB/MB）。
- `name` 为**原始文件名**（用于展示），落盘文件名另做安全处理（§7.5），二者不要求一致。

---

## 5. 负责人匹配与筛选

### 5.1 匹配流程（对订舱库只读）

建单/编辑时，逐个 `ref_value` 查询 `yxo.db` 的 `records` 表：

| ref_type | 匹配方式 | 取值 |
|---|---|---|
| customer_code | `客户编码 = ?` | `开票子公司名称` |
| container_no | `箱号 = ?` | `开票子公司名称` |

只取 `is_deleted = 0` 的记录。

**硬约束 H3：对 `yxo.db` 严格只读。** 连接串用 `file:...?mode=ro` 并以只读方式打开；代码中禁止出现针对 `yxo.db` 的 `INSERT/UPDATE/DELETE`。这遵守 AGENTS.md 硬规则 1（绝不修改 `data/yxo.db`）。

**硬约束 H4：订舱库路径走配置**，生产指向服务器本地 `D:\YXO_DATA\data\yxo.db`，不通过 UNC 访问（同 H1 的锁可靠性理由）。

### 5.2 子公司 → 负责人映射（以骁洋 2026-09-08 确认为准）

| 开票子公司名称 | 负责人 | owner_key |
|---|---|---|
| 太平洋 | 毛骁洋 | maoxiaoyang |
| 港九港铁 | 毛骁洋 | maoxiaoyang |
| 保时达 | 冯茜 | fengqian |
| 同程配 | 杨雅雯 | yangyawen |
| 东盟 | 杨雅雯 | yangyawen |
| 沙坪坝 | 韩文豪 | hanwenhao |
| 中欧木业 | 韩文豪 | hanwenhao |

7 个子公司全部分配完毕，无遗漏。映射表以常量形式落在 `ticket_system/owners.py`，不在数据库里（便于 review 与版本控制）。

> ⚠️ **AGENTS.md 的"业务线（分工）"小节与此不符**（那里写的是 冯茜=太平洋/港九港铁、韩文豪=保时达）。本 spec 以骁洋确认为准。**建议后续单独提一次修改修正 AGENTS.md**，本次不动它。

### 5.3 数据可行性（已实测）

| 检查项 | 结果 |
|---|---|
| 客户编码 → 子公司 | **100% 唯一**（2102 个编码无一对应多个子公司；50 个无子公司值） |
| 箱号 → 子公司 | 1901 条中 1894 唯一，**仅 3 个箱号跨两个子公司** |
| 结论 | 匹配逻辑歧义极低，可直接按"取第一条匹配记录"实现 |

**极少数箱号跨两个子公司时**：按 `开票子公司名称` 去重后取**全部**命中的负责人（即该工单同时挂两个槽位）。这与"涉及多负责人"的语义一致，不丢信息。

### 5.4 筛选规则（事业部端 5021）

顶部 4 个负责人标签，**可复选**：

- 一个都不选 → 显示全部工单。
- 选中若干 → 显示：**（工单负责人集合 ∩ 选中集合 ≠ ∅）∪（工单负责人集合为空）**。
- **负责人集合为空的工单（全部关联项都未匹配），在任何筛选组合下都可见** —— 这是需求第 4 条明确要求的兜底，防止"查不到就谁也看不见"。

渝新欧端（5022）**卡片照常显示负责人标签，但顶部不给筛选器**（D13），仅提供紧急度 / 状态筛选。

---

## 6. 状态机

### 6.1 存储：两个字段

`status`（open / progress / closed）+ `last_actor_role`（internal / partner）。

### 6.2 渲染：按访问者身份翻译（D4）

| status | 判断条件（**相对当前访问者**） | 显示文案 |
|---|---|---|
| open | `last_actor_role` == 我方角色（球在对方场地，我方已踢出） | 已提交 |
| open | `last_actor_role` == 对方角色（球在我方场地，该我动） | 待处理 |
| progress | — | 有进展 |
| closed | — | 已完成 |

**两端共用同一条规则**，只是"我方"的指代不同：在 5021 上"我方"= 事业部，在 5022 上"我方"= 渝新欧。因此同一个 `open` 工单，事业部看到"已提交"的瞬间，渝新欧看到的就是"待处理"——不需要两份数据，也永远不会自相矛盾。

举例：事业部新建工单 → `status=open, last_actor_role=internal` → 事业部见"已提交"，渝新欧见"待处理"；渝新欧一回复 → `last_actor_role=partner` → 渝新欧改见"已提交"，事业部改见"待处理"。

### 6.3 流转规则

| 事件 | status 变化 | last_actor_role | 槽位 |
|---|---|---|---|
| 新建工单 | → open | 创建方角色 | 按 §4.3 建槽位 |
| 发普通消息 | open → **progress**；progress/closed 不变 | 发言方角色 | 不变 |
| 提交结论（槽位） | 该槽位 done；**若全部槽位 done → closed** | 提交方角色 | 该槽位 → done |
| **手动点「标记完成」** | → closed | 操作方角色 | **剩余 pending 全部强制 → done（D14）** |
| 已完成工单再次发言 | status 保持 closed（不回退） | 发言方角色 | 不变（已是 done） |

- **手动关闭双方都可**（D6），与"全部槽位完成自动关闭"并存，不冲突。

### 6.4 手动关闭的完整动作（D14）

在**一个事务**内完成：

1. `tickets.status='closed'`、`closed_at=now`、`closed_by=操作人显示名`。
2. 把该工单所有 `state='pending'` 的槽位**强制置为 `done`**，`submitted_by` 记为 `系统·自动完成`。
3. 写入一条 `msg_type='system'` 的消息，正文固定格式：

   ```
   由 [操作人] 手动关闭，剩余槽位自动完成
   ```

   操作人取 `display_name`。**即使当时没有剩余 pending 槽位，也照样写这条日志**（措辞不变），保证审计一致性。
4. 更新 `last_actor_role` 为操作方角色。

**为什么必须这样**：否则会出现"工单显示已完成，但某某的槽位还挂着待提交"的矛盾状态 —— 卡片和详情页对不上，时间一长就没人敢信这个系统了。

### 6.5 已完成工单的后续进展（D15）

- 已完成区**按 `closed_at` 倒序固定排列**，新回复**不置顶**。
- 实现上：`closed` 工单的排序键用 `closed_at`，待办区用 `last_reply_at`。已完成工单收到新消息时**仍更新 `last_reply_at`**（供详情页显示"最后活动"），但**不参与**已完成区排序。
- **有新进展的正确做法**：新建一个工单，正文里手动写"承接 #原工单号"��v1 不做工单间自动关联，不做"重新打开原工单"。
- 已完成工单**允许继续发言、上传附件**（补充资料），状态不回退。

---

## 7. 页面与交互

### 7.1 主界面（列表）

- **顶部工具条**（两端都有）：全局搜索框（N1，见 §7.4）+ 新建工单按钮。
- 两个分区：**待办**（open + progress）、**已完成**（closed）。
- 排序（D7 / D15）：
  - 待办区：**紧急优先**，紧急组、一般组各自按 `last_reply_at` 倒序。
  - 已完成区：按 `closed_at` 倒序**固定**，新回复不置顶。
- 卡片显示：
  - 紧急度标签（`紧急` 红底 / `一般` 灰底）
  - 标题（C3）
  - 工单号（`#1024`）—— 便于 N3 分享与 D15 手动引用原单号
  - 提交日期时间
  - 状态（按 §6.2 翻译，**随访问者身份变化**）
  - **涉及负责人标签：两端都显示**（D13 —— 渝新欧端也要看到，才知道找谁催）
  - 最后一条消息摘要；含附件时显示回形针标记（N2）
  - 搜索命中时，标题与摘要中的关键词**高亮**（N1）
- 事业部端额外：顶部负责人复选筛选器（§5.4）。
- 渝新欧端（5022）：**不给负责人筛选器**（D13），仅保留紧急度 / 状态筛选。

### 7.2 详情页（上下两层，D8）

**顶部操作栏**（工单号 + 状态 + 操作按钮）
- 「复制分享文案」按钮 → N3，见 §7.6
- 「标记完成」按钮 → 仅非 `closed` 工单显示；双方都可用（D6），动作见 §6.4

**上层 · 槽位进度区**
- 列出本工单涉及的负责人，每人一枚徽章：`待提交` / `已提交`。
- 若工单无匹配负责人 → 显示"未匹配到负责人，全员可见"，无槽位。
- internal 用户**只能操作自己那一枚**槽位的"提交结论"按钮，不能替别人提交。
- partner 用户**可见进度、不可提交**。

**下层 · 对话流**
- 微信/LINE 风格气泡：己方右侧、对方左侧；显示发言人 + 时间。
- `[结论]` 消息高亮底色 + 标签。
- **功能 A · 引用回复**（D9）：每条消息可"引用"，回复后气泡内嵌被引用内容摘要；点击摘要**滚动跳转并高亮**原文。`parent_id` 落库。
- **功能 B · 按负责人筛选对话**（D9）：顶部下拉筛选"全部 / 毛骁洋 / 冯茜 / 杨雅雯 / 韩文豪 / 渝新欧"。选中某人时显示：
  1. 该人发的消息；
  2. 他引用的那条原文（保证上下文可读）；
  3. 引用了他消息的消息。
- **底部输入区（N2）**：文本框 + 附件区，支持 **Ctrl+V 粘贴图片 / 鼠标拖拽 / 点按钮选择文件**；已选附件以小卡片列出、可单独移除。点「发送」时**文本与附件一起提交为一条消息**（正文可空，即纯附件消息）。
- 输入框只发**普通消息**（不会关闭槽位）。
- 「提交结论」的**唯一入口是上层槽位区的按钮**，不在输入框旁边设第二个 —— 避免"我以为我在回复，结果把工单结了"。

> 提交结论时弹出确认框，要求填写结论正文（可空，但建议填）。确认后：写入一条 `msg_type=conclusion` 的消息 + 该槽位转 `done` + 按 §6.3 判定是否整体完成。

### 7.3 新建工单表单

| 字段 | 必填 | 说明 |
|---|---|---|
| 标题 | 是 | ≤50 字（C3） |
| 紧急度 | 是 | 紧急 / 一般，默认一般 |
| 客编号 / 箱号 | 否 | 多行或空格分隔，支持多个（D5） |
| 正文 | 是 | 作为首条消息 |

**关联项实时解析**：输入后即时查询订舱库，把"匹配到 / 未匹配"与解析出的负责人显示给用户确认（避免填错编号导致工单落到错误负责人）。

### 7.4 全局搜索（N1）

**检索范围**（OR 关系，命中任一即算命中该工单）：

1. `tickets.title` —— 工单标题
2. `messages.body` —— 该工单下所有消息正文
3. `ticket_refs.ref_value` —— 关联的客编号 / 箱号

**实现**
- 用 `LIKE '%'||?||'%'`，**参数化查询**。
- **必须转义**用户输入里的 `%` 和 `_`（配合 `ESCAPE '\'`）：否则用户只输一个 `%` 就会匹配出全部工单。
- v1 不引入 FTS5 —— 数据量小，LIKE 足够。
- **附件内容不参与检索**（v1 不解析图片/文档内容）。

**结果呈现**
- 切换为"搜索结果"视图，**复用同一套工单卡片组件**，不按待办/已完���分区。
- 卡片额外显示**命中来源**（如"标题命中"、"消息正文命中 3 处"、"箱号命中"）。
- 排序：紧急优先 → `last_reply_at` 倒序（与待办区一致）。
- 空结果：显示"没有找到包含「关键词」的工单"。

**关键词高亮**
- 列表页：卡片标题、摘要中的命中片段用 `<mark>` 包裹。
- 详情页：从列表点进去时 URL 带 `?kw=`，详情页对**标题、所有消息正文、关联编号**做高亮。
- **安全**：必须先 HTML `escape` 再插入 `<mark>` 标签，禁止直接拼 HTML（防 XSS）。

### 7.5 附件上传（N2）

**交互**
- 三种入口：**Ctrl+V 粘贴**剪贴板图片、**鼠标拖拽**文件到输入区、点「选择文件」按钮。
- 支持一次选多个；已选附件以小卡片列出，发送前可逐个移除。
- 提交走 `multipart/form-data`，文本与附件**一次性提交为一条消息**。

**限制**（服务端强制校验，不依赖前端）

| 项 | 值 |
|---|---|
| 单文件大小 | ≤ 20 MB |
| 扩展名白名单 | jpg / jpeg / png / pdf / doc / docx / xls / xlsx |
| 单条消息附件数 | ≤ 10 |
| Flask `MAX_CONTENT_LENGTH` | 25 MB（留余量；超限时返回**明确中文提示**，不要 413 白页） |

**落盘安全**
- 文件名经 `werkzeug.utils.secure_filename` 处理，再加 `日期时间_随机6位_` 前缀，防覆盖、防路径穿越。
- 目录：`{附件根目录}\{工单号}\`。**工单号必须校验为纯数字**后才拼接路径，禁止直接拿用户输入构造路径。
- 中文文件名：原始名存 `attachments.name`（展示用），落盘用安全名（`attachments.path`），二者不要求一致。

**展示**
- 图片：`<img>` 直接显示，CSS 限制最大宽高（即缩略图效果），点击新窗口打开原图。v1 **不做服务端缩略图生成**（避免引入 Pillow 依赖）。
- 非图片：文件类型图标 + 文件名 + 大小（自动换算 KB/MB），点击下载。
- 访问路由 `GET /files/<int:ticket_id>/<filename>`，**必须登录后才能访问**（复用 §3.4 鉴权）。

**其他**
- 删除工单/消息时**不物理删除**文件（v1 简化，避免误删证据；要清理走人工运维）。
- 附件目录必须纳入备份（H6）。

### 7.6 复制分享文案（N3）

按钮位于详情页顶部操作栏，点击生成文本并写入剪贴板。

**模板**

```text
【工单提醒】#{工单号} {紧急度} {负责人@列表}
箱号：{关联箱号}
客编：{关联客编号}
内容摘要：{工单标题} {最新消息正文前50字}
点击查看：{工单完整URL}
```

| 占位符 | 取值规则 |
|---|---|
| `{工单号}` | `tickets.id`，如 `1024` |
| `{紧急度}` | `紧急` / `一般` |
| `{负责人@列表}` | 各负责人 display_name 前加 `@`，空格分隔（如 `@毛骁洋 @杨雅雯`）；**无负责人时整段省略** |
| `{关联箱号}` | 该工单所有 `container_no` 的 `ref_value`，`、` 分隔；无则填 `无` |
| `{关联客编号}` | 该工单所有 `customer_code` 的 `ref_value`，`、` 分隔；无则填 `无` |
| `{内容摘要}` | 标题 + 空格 + 最新一条**非 system** 消息正文前 50 字（超出加 `…`；无正文则只显示标题） |
| `{工单完整URL}` | `PUBLIC_BASE_URL` + 工单号，见下 |

**URL 生成** —— 新增配置项 `PUBLIC_BASE_URL`，每端各自配置（与 C2 配套）：

| 端 | 配置值示例 |
|---|---|
| 5021 事业部 | `https://<公网域名>/ticket/` |
| 5022 渝新欧 | `https://<公网域名>/partner/` |

- 完整 URL = `PUBLIC_BASE_URL` + 工单号。
- **未配置时回退** `request.host_url` 拼相对路径 —— 保证本机开发能跑，但生产环境必须先配，否则分享出去的是内网地址。

**实现要点**
- 纯前端 `navigator.clipboard.writeText(text)`，不接微信开放平台。
- **必须实现降级**（R5）：非 HTTPS 或旧浏览器下 Clipboard API 不可用时，回退隐藏 `<textarea>` + `document.execCommand('copy')`。
- 复制成功后按钮文案短暂变为"已复制"。
- `@毛骁洋` 是**普通文本**，微信群靠昵称匹配才触发真正的 @；昵称不匹配就只是普通文字，**不会报错**。
- 分享出去的是**当前端口**的链接：事业部端复制的链接，渝新欧 partner 账号打不开（§3.4 角色校验拦截）—— 这是**预期的安全行为**，不是 bug。

---

## 8. 风险与硬约束

| # | 项 | 说明与对策 |
|---|---|---|
| H1 | `tickets.db` 禁放网络盘 | 放服务器本地 `D:\YXO_DATA\data\`；见 §2.3 |
| H2 | 备份含 `-wal`/`-shm` | 否则丢最近写入 |
| H3 | `yxo.db` 只读 | `mode=ro`，代码禁止写操作；遵守 AGENTS.md 硬规则 1 |
| H4 | 订舱库走本地路径 | 不走 UNC |
| H5 | 敏感数据不入库文档/测试 | 遵守 AGENTS.md 硬规则 3（客户名 / 收发货人 / 运价 / 提单号 一律脱敏） |
| **R1** | **公网 HTTP 明文传密码** | 现有订舱系统是 `http://公网IP:5000/yxo/`。本系统带登录，明文传密码风险显著更高。**建议 nginx 配 HTTPS**（C1）；若短期无法上证书，至少限制访问来源 IP 或改走 VPN。这是本次最高优先级的安全项。 |
| R2 | 共享账号无法区分到人 | 渝新欧侧共用一个账号（D3）。当前接受；若后续需要追责，可升级为"共享账号 + 每次回复选署名人"。 |
| R3 | 两进程需分别守护 | 两个 nssm 服务；一侧挂掉不影响另一侧（这正是选方案 A 的理由）。 |
| R4 | 负责人离职/变动 | 映射表在 `owners.py` 常量里，改一行即可；账号在 `auth_users` 里改 `is_active`。 |
| **R5** | **剪贴板 API 依赖 HTTPS** | `navigator.clipboard` 只在 **HTTPS 或 localhost** 下可用。若 C1 未落地（仍走 HTTP 公网），N3 的复制会**静默失败**。对策：必须实现 `execCommand` 降级（§7.6）—— 但这反过来说明 HTTPS 更该配。 |
| **R6** | 附件上传是唯一落盘口 | 这是本系统**唯一**接收外部文件写入的地方，必须做到：扩展名白名单 + 大小限制 + `secure_filename` + 工单号纯数字校验 + 下载路由强制登录。缺任何一条都可能演变成任意文件写入/读取。 |
| R7 | LIKE 搜索的通配符与注入 | 用户输入 `%` / `_` 未转义会匹配出全部工单；必须参数化 + `ESCAPE`（§7.4）。 |
| R8 | 附件无病毒扫描 | v1 不做杀毒。服务器若开了实时防护会顺带覆盖落盘目录，算额外保障，但不应依赖它。 |
| H6 | 附件目录禁放网络盘 + 纳入备份 | 与 H1 同理；备份除 `tickets.db` 三件套外还要带走 `ticket_files\`（§2.2）。 |

---

## 9. 测试策略

沿用 pytest（`requirements-dev.txt` 已含）。**测试使用独立的临时 db，绝不碰生产 `yxo.db`**（H5）。

必测场景：

1. **账号**：internal 账号不能登 partner 端口；partner 反之；错误密码被拒；会话过期跳转登录。
2. **匹配**：客编号命中 → 正确负责人；箱号命中 → 正确负责人；跨两子公司的箱号 → 两个槽位；完全不存在的编号 → 无槽位且全员可见。
3. **筛选**：复选 1 人 / 多人 / 不选；未匹配工单在任何组合下都出现。
4. **状态机**：新建 → 双方各自看到的中文词正确；普通发言推到"有进展"不关槽位；提交结论关自己的槽位；全部槽位完成自动完成；任一方手动关闭生效；已完成后再发言不回退。
5. **排序**：待办区紧急恒在一般前、同组内最新回复在最前；**已完成区按 `closed_at` 固定，新回复后顺序不变**（D15）。
6. **引用**：`parent_id` 正确落库；按负责人筛选时能带出引用上下文。
7. **并发**：两个连接同时写 `tickets.db` 不抛 `database is locked`（验证 WAL + busy_timeout 生效）。
8. **手动关闭（D14）**：剩余 `pending` 槽位全部转 `done`；生成 `system` 日志且措辞正确；**无剩余槽位时也照样写日志**；整个过程在一个事务内（中途失败不留半截状态）。
9. **搜索（N1）**：标题 / 正文 / 客编号 / 箱号四类分别命中；**输入 `%` 不会返回全部工单**；参数化防注入；高亮不破坏 HTML（用关键词 `<script>` 验证已转义）。
10. **附件（N2）**：白名单外扩展名被拒；> 20MB 被拒；文件名含 `../` 与中文时落盘安全（无路径穿越）；图片/非图片渲染分支正确；**未登录访问 `/files/` 被拦截**。
11. **分享文案（N3）**：无负责人时 `@` 段整段省略；无关联编号时填"无"；摘要按 50 字截断；URL 取自 `PUBLIC_BASE_URL`。

---

## 10. 实施范围（本次交付）

| 模块 | 文件 |
|---|---|
| 账号模块（独立可复用） | `yxo_auth/__init__.py`、`store.py`、`service.py`、`flask_ext.py` |
| 工单应用 | `ticket_system/app.py`、`models.py`、`owners.py`、`matching.py`、`views_list.py`、`views_detail.py`、`templates/*.html`、`static/*.js|css` |
| 新增（N1/N2/N3） | `ticket_system/search.py`（搜索与转义/高亮）、`uploads.py`（落盘与校验）、`views_files.py`（附件下载路由）、`static/js/share.js`（复制与降级）、`static/js/upload.js`（粘贴/拖拽） |
| 配置与运维 | `ticket_system/config.py`（含 `PUBLIC_BASE_URL`、附件根目录、`MAX_CONTENT_LENGTH`）、启动入口、nssm 服务配置、`start_two.bat` |
| 测试 | `ticket_system/tests/test_*.py` |
| 文档 | 本 spec + 实施计划（writing-plans 产出） |

**不在本次范围**：主系统 5011 鉴权接入（D2）、企微/邮件通知（N4）、工单间自动关联（D15）、服务端缩略图生成、附件病毒扫描、统计报表、AGENTS.md 分工表修正。
