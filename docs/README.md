# docs/ —— 文档索引地图

> **这是文档树的总入口。** 找文档先看这里，不要 `ls` 整个目录（82 份文档里大部分是历史归档）。
> 结构自 2026-10-08 起按「**模块 + 时效**」两层组织，**此为固定规范**。

---

## 一、怎么用这套结构

每个模块目录下三个子目录，**含义固定**：

| 目录 | 含义 | 什么时候看 |
|---|---|---|
| **`specs/`** | **当前生效**的设计 / 约定 / 接口口径 | 动手改这个模块**之前**必看 |
| **`drafts/`** | 正在讨论、还没拍板的草案 / 建议稿 / 待执行任务单 | 想知道"接下来要做什么" |
| **`archive/`** | 已完成的历史执行记录（任务书、验收报告、回执） | **只在追溯"当时为什么这么做"时看** |

> ⚠️ **`archive/` 里的东西一律不是现行依据。** 里面的 spec 描述的是"当时打算怎么做"，其中不少已被后续决定推翻——引用前先确认它有没有被取代。
> 📌 另外：archive 里的文档若引用了**其它文档的旧路径**（2026-10-08 重构前的平铺路径 `docs/xxx.md`），那是历史写法，**不是现存链接**，别直接点。

---

## 二、模块划分

| 模块 | 覆盖范围 | 代码位置 |
|---|---|---|
| [`mailbots/`](mailbots/README.md) | 邮件机器人（收信 / 转发 / 退信 / 草单分类 / 命名） | `mailbots_next/`（新）、`mailbots/`（旧·生产在跑） |
| [`auth/`](auth/README.md) | 权限模块（RBAC、登录、CSRF、门禁） | `auth/` |
| [`manifest/`](manifest/README.md) | 舱单导入（差异比对、放行、终态出口） | `manifest_engine.py`、`templates/manifest.html` |
| [`mainsite/`](mainsite/README.md) | 主站订舱平台（后端 + 前端，含退舱约定） | `app.py`、`admin_api.py`、`static/`、`templates/` |
| [`gongdan/`](gongdan/README.md) | 工单系统（**尚未开工**） | 规划中 |
| [`wecombot/`](wecombot/README.md) | 企微机器人 | `wecombot/`、生产 `D:\YXO_DATA\WeComBot` |
| [`ops/`](ops/README.md) | 部署 / 环境 / 发布 / 仓库治理 | `scripts/`、`.gitignore` |
| [`meta/`](meta/README.md) | 协作规范 / 方法论 / 改造路线图 / 批次决策 | — |

---

## 三、我想知道 X → 看哪份

| 我想知道 | 看这份 |
|---|---|
| **项目整体是什么、怎么跑、仓库地图** | `README.md`（仓库根） |
| **AI 在本仓库干活的硬规则** | `AGENTS.md`（仓库根，最高优先级） |
| **git 分支 / PR / 部署流程** | `WORKFLOW.md`（仓库根） |
| **新机器怎么接进来（clone / hook / 代理 / 认证）** | [ops/specs/新环境接入.md](ops/specs/新环境接入.md) |
| **仓库转私有的影响与操作** | [ops/specs/仓库转私有.md](ops/specs/仓库转私有.md) |
| **退舱记录能碰不能碰（业务铁律）** | [mainsite/specs/退舱记录处理约定.md](mainsite/specs/退舱记录处理约定.md) |
| **主站有哪些页面 / 业务域对应哪段代码** | [mainsite/specs/模块-主站订舱数据管理平台.md](mainsite/specs/模块-主站订舱数据管理平台.md) |
| **验收怎么做（影子期 / 对答案 / 开闸判据）** | [meta/specs/2026-10-06-验收圣经-可长期复用.md](meta/specs/2026-10-06-验收圣经-可长期复用.md) |
| **主站下一步要改什么** | [mainsite/drafts/2026-10-06-主站全面盘点-优化重做与新增(建议稿).md](mainsite/drafts/2026-10-06-主站全面盘点-优化重做与新增(建议稿).md) |
| **整体改造的大方向与阶段划分** | [meta/drafts/2026-10-04-整体改造路线图(给执行方).md](meta/drafts/2026-10-04-整体改造路线图(给执行方).md) |
| **权限模块怎么上线、当前缺什么** | [auth/drafts/2026-10-06-权限模块定位与上线spec.md](auth/drafts/2026-10-06-权限模块定位与上线spec.md) |
| **邮件机器人新旧怎么切换** | `mailbots/README.md` + `mailbots_next/README.md`（仓库内，代码模块说明） |

---

## 四、`docs/superpowers/` 已拆掉（2026-10-08）

这里原本放着 16 份「superpowers 工作流」产出的设计稿（`plans/` + `specs/`）。**现已全部按模块归位，该目录已删除**：

| 原在 `docs/superpowers/` 的文档 | 现在的位置 |
|---|---|
| mailbots 重构设计、Plan A、Plan B | `mailbots/archive/` |
| 权限 RBAC 模块总体设计 | `auth/specs/` |
| RBAC 加固 / 验收 / 任务书等 7 份（9-29 ~ 9-30） | `auth/archive/` |
| 统一错误处理设计 | `mainsite/archive/` |
| 工单系统设计 | `gongdan/specs/` |
| 部署交接 / 部署清单 / D4 日志 3 份 | `ops/archive/` |

> 📌 **以后新产出直接放进对应模块的 `specs/` / `drafts/`**，不要再另建工作流目录——否则各模块 README 又得维护一堆外部引用，每次产出都要回来改一遍。
> 🛠️ **技能本体**仍在 `~/.workbuddy/skills/`（16 个，当前**全部是 `disable: true`**，所以 "/" 里叫不出来；要用需去掉该字段并重启）。

---

## 五、给 AI 的阅读顺序建议

1. 仓库根 `README.md`（项目全貌）→ `AGENTS.md`（硬规则）
2. 定位要改的模块 → 读该模块的 `README.md`
3. 该模块 `specs/` 全部过一遍（**这是现行口径**）
4. 需要背景时才翻 `archive/`，并注意"是否已被取代"

---

*维护要求：新增文档放进对应模块的 `specs/`（现行）或 `drafts/`（未定）；一份工作完成后把过程文档移入 `archive/`，并在模块 README 里更新 Current 列表。*
