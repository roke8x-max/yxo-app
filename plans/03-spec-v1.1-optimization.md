# 基于《双 Agent 协作规范 v1.1》的方案复核与优化

> 配套：README.md / 01-remote-access-ladder.md / 02-n8n-cicd.md
> 目标：结合本地代码（yxo-app）+ 服务器 WeComBot + GitHub 设置，复核 spec v1.1 并给出优化；重点解决"ZeroTier 不稳定"的单点故障。
> 状态：未提交草稿，待骁洋审阅拍板。

---

## 🔴 重大更正（2026-08-09 看过 WeComBot 后）

我上一版（本节原稿）说"spec 的企微回调已就绪不成立"——**那是错的**，只因为我当时只看了 yxo-app 仓库里的 `scripts/notify.ps1`（单向出站），没看服务器 `D:\YXO_DATA\WeComBot`。

WeComBot 里早就有一套**完整入站企微回调服务**：
- `server.py`：Flask + waitress，监听 `0.0.0.0:5001`，`/wecom/callback` 同时支持 GET（URL 验证 `verify_url`）+ POST（收消息 `receive_message`），`/health` 健康检查；已用 waitress 生产化、在 watchdog 守护下 7×24 在线。
- `shared/wx_crypto.py`：标准 `WXBizMsgCrypt`——AES-256-CBC 解密 + SHA1 签名校验 + corp_id 校验。入站是**鉴权 + 加密**的，不是裸端口。
- `cs_bot/wecom_api.py`：出站封装（access_token 缓存、限流退避、按真名 `notify_by_name`、客服 `kf_send_text`、`download_media`）。

结论：**spec v1.1 的"企微自建应用 + 回调 URL 已就绪"基本成立**——回调服务已上线且对公网（至少对企微云）可达。缺的不是"建穿透"，而是**把这条现成的入站通道接入双 Agent 编排的"胶水层"**（WF2「确认部署」、WF3「企微回调→任务」）。下面据此重写。

---

## 0. 一句话结论

spec 方向对，但有两处要修：
1. **任务队列别放 SMB**（与 WORKFLOW.md §2 铁律冲突 + 单点故障），改走 **GitHub `tasks/`**；
2. **"确认部署 / 企微回调"不用新建 VPS 穿透**——直接复用已上线、公网可达、加密鉴权的 **WeComBot 回调（:5001）**。

这两条合起来，关键链路（任务 → dev → PR → 审查 → 审批 → 部署）可以做到 **100% 不依赖 ZeroTier**。

你担心的 ZeroTier 波动，最省事的不是"换更好的 VPN"，而是**把协作面从 VPN 解耦**：机器对机器走 GitHub（公网），人对服务器的审批/轻量指令走企微回调（公网、已跑通）。ZeroTier 只剩"你人工翻文件/远程桌面"这一条非关键路径，而那条本就有 iNode / RustDesk 备份。

---

## 1. spec 关键事实复核（已结合 WeComBot + 仓库）

| # | spec v1.1 假设 | 核对结果 | 影响 |
|---|---|---|---|
| 1 | 企微自建应用 + 回调 URL 已就绪，公网穿透已完成 | **基本成立（我上一版判错）**。WeComBot `server.py`+`wx_crypto.py` 已是完整入站回调，监听 `0.0.0.0:5001`，已上线、在 watchdog 守护下运行。缺的是接入双 Agent 编排的胶水 | WF2「确认部署」/ WF3「企微回调」**可实现，无需新建 VPS 穿透** |
| 2 | 任务队列放 SMB `\...\agent_queue\` | 与 WORKFLOW.md §2 铁律冲突；SMB 跑在 ZeroTier 上 → 单点故障 | 任务下发/领取全依赖 ZT，正是波动点 |
| 3 | 分支 `feature/...-xiaoji`（带斜杠） | 违反 WORKFLOW.md §3 铁律（8/5 `dev/...` 致 `.git` 损坏） | 改成连字符 `feature-xxx-xiaoji` |
| 4 | 路径 `C:\yxo_app_dev/prod` | 现实 `E:\yxo_app_dev` / `D:\YXO_DATA\yxo_app` | 照抄会指错目录 |
| 5 | git 身份 `yxo-xiaoji`/`yxo-local` 分离 | 现实单一身份，需补（非阻塞） | — |

核对动作：读 `AGENTS.md` / `WORKFLOW.md` / `scripts/notify.ps1`；grep 仓库确认 yxo-app 内企微仅出站；**额外查看共享盘 `\\10.0.199.184\yxo_data\WeComBot` 的 `server.py` / `wx_crypto.py` / `wecom_api.py` / `ip_watchdog.py` / `config.py` / `docs/cs_bot_design.md`**，确认入站回调已完整实现并运行。

---

## 2. WeComBot 回调 = 现成的「ZeroTier 无关」服务器命令通道（关键洞察）

这是看 WeComBot 后最大的收获，也是 ZeroTier 波动问题的优雅解：

`server.py` 已经是"消息进 → `engine.handle_text` → 回复出"的通用回路，目前只接了客服机器人指令。一旦给 `engine` 增加**管理员（毛骁洋）专用**的两个能力，就等于拿到一条不依赖 ZeroTier 的服务器控制面：

- **审批（WF2「确认部署」按钮）**：你手机发"确认部署 PR#12" → 企微云推到服务器 `:5001` 回调（公网、已跑通）→ admin 处理器把"批准"标记写入 GitHub `tasks/`（公网）→ **本地 Agent（芙蕾雅）轮询 GitHub 读到批准 → 执行 `deploy.ps1`**。全程不碰 ZeroTier。
- **轻量远程执行备份**：admin 专用"运行: <cmd>"模式 → 服务器执行并把结果回企微。这样**即使 ZeroTier 和 iNode 同时挂了**，只要 `:5001` 公网入站还在（它独立于 ZT），你仍能从手机戳服务器（git pull / 重启服务 / 看状态）。

**为什么这条通道比 ZeroTier 稳**：企微"企业可信 IP"白名单**只限制服务器"出站调企微 API"（主动发消息）**，不限制"企微→服务器"的入站回调推送。所以即便双线公网 IP 跳动（`ip_watchdog.py:133` 明文"双线路会在已知 IP 间来回跳"，IP 变了会邮件提醒更新白名单），**入站回调 :5001 不受影响**——它才是你手上最稳的公网端点，比 ZeroTier 海外中继可靠得多。

> **已坐实（不是推断）**：`ip_watchdog.py` 监控的就是服务器自身的公网出口 IP（每 5 分钟查 `ipify`/`3322`/`ipip`），且日志显示双线在已知 IP 间来回跳——这证明 (1) 服务器有**真实公网 IP**、(2) 企微云能稳定推送到 `:5001`（bot 一直在收消息，`logs/atb_*` 一路活跃到 8/9）。底层防火墙/端口转发由你掌握，但入站回调已实际跑通，无需新建穿透。

> ⚠️ 安全提醒：给 engine 加"运行命令"是高危操作，必须 (a) 仅限 `ADMIN_USERS`（config 已含"毛骁洋"）；(b) 命令白名单或二次确认；(c) 全程留痕到 `logs/`。不能裸开 shell。

> ⚠️ 仓库隔离（更新方向）：按最新决策，**WeComBot / MailBots 的代码要纳入统一 GitHub 仓库**（子目录 `wecombot/` `mailbots/`）统一改造管理；但 `secrets.json`、生产 `yxo.db`、以及全部服务器本地绝对路径**绝不入库**——改用 `secrets.example.json` 模板 + `.gitignore` 屏蔽 + `config/` 读环境变量。即"纳入代码、外置凭证"，与 `00-项目说明.md` §4 一致。（旧版"绝不提交"已过时）

---

## 3. 优化方案：协作面从 VPN 解耦（最高杠杆）

| 协作环节 | 现状（依赖 ZT） | 优化后（公网） |
|---|---|---|
| 任务内容下发 | 写 SMB JSON（spec） | 写 GitHub `tasks/`，本地 commit→push，服务器 `git pull` 读 |
| 审批（确认部署） | （原以为需新建 VPS 穿透） | **复用 WeComBot 企微回调 :5001**（已公网可达）→ 写 GitHub approve 标记 |
| 任务/部署通知 | 企微出站（notify.ps1 / notify_by_name） | 不变，出站本就走公网 |
| 服务器出网（推 PR/拉代码） | 走本机 Clash（ZT IP） | 服务器自己跑 mihomo（01 方案） |
| 兜底 | SMB 5 分钟轮询（同坏通道） | GitHub 轮询（公网）真兜底 |

效果：关键链路 100% ZeroTier 无关。ZT 抖只影响人工看文件/远程桌面，那条有 iNode / RustDesk 备份。
（与 WORKFLOW.md §2 既定语义完全一致："所有交流都经过 GitHub"。）

---

## 4. ZeroTier 不稳定 —— 分层备份 / 加固（修订）

- **第 0 层（架构，先做）**：第 3 节解耦 + 用 WeComBot 企微回调做审批/轻量远程指令。流水线本身不依赖 VPN。
- **第 1 层（残留 ZT 用途：远程桌面 / 人工翻文件）**：iNode（白天备用）、RustDesk（零依赖 ZT）、RDP-over-ZT。
- **第 1.5 层（企微通道兼做备份）**：如上，即使 ZT + iNode 双挂，手机经企微 :5001 仍能戳服务器。
- **第 2 层（让 ZT 本身更稳 / 替换）**：自建 ZeroTier Moon（国内 VPS，~¥60–100/年，改动最小）；或迁 WireGuard 网状（更稳但配置量大）。
- **第 3 层（最后手段）**：智能插座断电；WoL 不做。

---

## 5. 部署确认（WF2 / WF3）—— 现在不用 VPS 也能做

原以为 WF2「确认部署」按钮依赖新建入站穿透。现在看清：**WeComBot 回调 :5001 已是公网入站**，直接复用即可：
- 你企微发"确认部署 PR#12" → 服务器回调收到 → 写 GitHub `tasks/pr-12/approve` → 本地 Agent 轮询 GitHub 触发 `deploy.ps1`。
- 部署结果用 `notify_by_name(毛骁洋, ...)` 出站回企微（已支持）。

无需 Cloudflare / frp / VPS。仍建议**核心流水线先只通知、批准后部署**，但"批准"这一步已可走企微，体验接近 spec 原想。

---

## 6. 决策状态（2026-08-09 晚拍板）

| # | 议题 | 结论 |
| --- | --- | --- |
| 1 | 任务队列载体 | **GitHub `tasks/`**（放弃 SMB，解耦 ZeroTier） |
| 2 | 分支命名 | **连字符 `feature-xxx-xiaoji`**，绝不用斜杠（斜杠是 8/5 `.git` 损坏根因）；两个环境提交分开 |
| 3 | WeComBot 改库 | **要改**——加 admin「确认部署 / 运行命令」模式，须白名单 + 全程留痕 |
| 4 | ZeroTier 加固 | **暂缓，先观察**——回家后暂无强工作需求；关键链路已 ZeroTier 无关，加固降级为可选 |

> 待确认落地细节（方向已定，逐项再细聊）：工作流设置、各模块改造与功能需求、GitHub 仓库重组（见 §7 / 架构图二）。

---

## 7. 顺带要改 spec 的地方

- 路径 `C:\yxo_app_dev/prod` → `E:\yxo_app_dev` / `D:\YXO_DATA\yxo_app`
- 分支 `feature/...` → `feature-...`（连字符）
- "生效前提：公网穿透已完成" → 改为"**WeComBot 回调 :5001 已公网可达，直接复用，无需新建穿透**"
- agent_queue → `tasks/`（若选 GitHub 载体）

---

## 8. 共享盘在用模块盘点（2026-08-09 补，你要求查漏）

列举 `\\10.0.199.184\yxo_data` 顶层 26 项，逐一看是否该纳入统一改造。结论：**除了你点名的 yxo_app / WeComBot / MailBots，至少还有 3 个在用核心模块此前没算进**——`yxo_web`、`gate`、`Tools`。

### 8.1 已用上、建议纳入统一 GitHub 仓库的模块

| 模块(目录) | 角色 | 证据 | 纳入建议 |
|---|---|---|---|
| `yxo_app` | Flask 业务引擎（订舱委托书/舱单） | `D:\YXO_DATA\yxo_app` | ✅ 已定 |
| `yxo_web`（前端） | **独立前端工程（沿用 web 命名）**。起点 = yxo_app 现有 `templates/*.html` + `static/{app.js,style.css}`（洋设计的最新前端：index/tuoshu/manifest/admin 四页 + 130KB app.js）。旧的 `/yxo/` Flask+SQLite 表格原型为早期实验，**归档不并入** | yxo_app 现有前端文件 | ✅ 建议纳入——作为独立前端，seed 来自 yxo_app 前端，不是旧原型 |
| `WeComBot` | 企微回调 :5001（入站加密+出站） | `server.py`/`wx_crypto.py` | ✅ 已定（你明确说纳入） |
| `MailBots` | 邮件机器人 DSK/ATB/运踪（部分仍读飞书） | `Database_Syncer.py` 等 | ✅ 已定 |
| `gate` | **LLM 出网合规网关（已收窄定位）**：PII 脱敏/阻断 + 模型白名单 + 审计。**只守"手写 LLM 调用"**——主要是 WeComBot 未来接免费模型做 NLP（识别自然语言 / 理解新命令 / 收集真实需求）+ 跨模型超时切换；agent 自身推理、opencode 内置免费模型**不走它** | `data_gate.py`/`gate_config.json`（dry_run） | ✅ 建议纳入——作为 WeComBot 免费模型 NLP 的唯一合规出口 |
| `Tools` | 历史脚本集（多为飞书期）。**选择性复用**：`daily_health.py`→数据质量监控（输出改发**邮件**给毛骁洋，不走企微纯文本）；`health_check.bat`→服务监控（⚠️ 守的 WeComBot 端口 5000≠实际 5001，需改）。**不迁**：`match_price`（yxo_app 已有 `compute_price`，冗余）、`数据清洗`（yxo_app 已有舱单上传）、`sync_to_subtables`/`cache_manager`（纯飞书已无意义） | 17 个 .py/.bat | ⚠️ 部分复用，非整体纳入——仅迁有复用价值的逻辑 |
| `config/`(新建) | 统一配置：gate_config.json、price_config.json、secrets 外置模板 | — | ✅ 新建 |
| `scripts/`(新建) | 统一运维：deploy.ps1、start_all_services.bat、health_check.bat、notify.ps1 | 散落各目录 | ✅ 新建 |
| `tasks/`(新建) | GitHub 任务队列（解耦 ZeroTier） | — | ✅ 新建 |

### 8.2 运维/基础设施类（架构图体现，但不进业务仓库代码）

- `watchdog`（YXO_Watchdog 计划任务，守护 yxo_app+WeComBot）—— 运维层，保持服务器本地
- `FileBrowser`（NAS 文件管理器，**即 8/5 事故源头**）—— 独立软件，不纳入
- `Log` / `output` / `备份` —— 日志/输出/备份，数据类不入库

### 8.3 不纳入（数据/临时/安装包/助手自身）

- `todo`（混合物：CursorUserSetup/Git/python/zerotier 安装包 + Excel 台账 + `托书自动化`/`价格表`/`历史数据` 子目录）—— **不整体纳入**；其 `托书自动化`(autotuoshu.py)、`价格表`、`历史数据` 子目录含在用脚本/数据，需单独判断（可能并入 Tools / 数据目录）
- `.workbuddy`（服务器上 WorkBuddy 助手自身 memory）—— 不纳入
- `芙蕾雅_开发沙盒`（开发暂存区 app_改动/工具脚本/说明）—— 不纳入生产
- 根目录散落 `_*.py`（历史分析/清理脚本）、`脚本清单.xlsx`、`芙蕾雅_小哩_留言`、`优化方案_飞书依赖替换.md` —— 临时/历史/文档，按需归档

### 8.4 ⚠️ 纳入统一仓库时的 secrets 隔离（重中之重）

盘点发现多处明文凭证，统一入库前必须外置：

- `WeComBot/secrets.json`：CORP_ID/SECRET/TOKEN/AES_KEY + 邮箱密码 —— **绝不可入库**，保持本地
- `MailBots`：`Database_Syncer.py` 等把飞书 `APP_SECRET` 明文硬编码（见 `优化方案_飞书依赖替换.md` §6）—— 迁库后注销飞书凭证，移入环境变量
- `gate/gate_config.json`：`"sk-替换为你的deepseek_key"` 占位 + `__secret:NVIDIA_API_KEY` 引用式 —— 用 secrets 外置 + `.gitignore` 屏蔽
- 生产 `yxo.db`：任何模块都直读 `D:\YXO_DATA\yxo_app\data\yxo.db`，**不入库**
- 做法：仓库内只留 `*.example.json` 模板 + `.gitignore` 屏蔽 `secrets.json`/`*.db`/`config_local.py`/`*.xlsx`；统一经 `config/` 读环境变量

### 8.5 修订后的图2 模块清单（见架构图二，已对齐 00 项目说明）

已确认纳入（实框 ✔）：`yxo_app`(后端) `wecombot` `mailbots`（三者**纳入代码但 secrets/生产 yxo.db 不入库**）
建议纳入（虚框 ?）：`yxo_web`(前端·seed=yxo_app 现有前端，非旧 /yxo/ 原型) `gate`(收窄:仅 WeComBot 免费模型 NLP) `tools`(选择性复用) `config` `scripts` `tasks`
不纳入（运维/数据/临时）：`watchdog` `FileBrowser` `Log` `output` `备份` `todo` `.workbuddy` `芙蕾雅_开发沙盒` 根散落文件 / 旧 `yxo_web` 实验原型
