# 02 · n8n 驱动的 CI/CD 与多 Agent 协作

> 目标：PR 自动 review、合并自动部署通知、push 自动跑测试；多个 agent（你/本机 opencode/服务器 opencode/小叽）在统一分支模型下并行不撞。
> 本机探测：无 Docker（docker-cli 缺失）、n8n 未装、node 可用 → n8n 走 `npx` 或 nssm 服务化。

## 0. 分支与任务映射（5.1–5.5）
- 既有铁律：`dev` → PR → `main`，main 服务端保护禁直推。
- **分支命名铁律（2026-08-09 晚拍板）**：**分支名绝不带斜杠**。`feature/5.1-...` 这种带斜杠的写法曾是导致 8/5 `.git` 损坏的真正根因（斜杠分支在 `refs/heads/` 里与目录路径冲突，`dev/fix-admin-api-load` 曾让 `dev` 分支根本建不出来）。一律改用连字符：
  - `feature-5.1-backend-layer`、`feature-5.2-error-handling`、`feature-5.3-config`、`feature-5.4-tests`、`feature-5.5-mailbots`
  - 两个开发环境提交分开：本机与小叽各自用独立 feature 分支（小叽侧可加 `-xiaoji` 后缀），**彼此不互相 merge**，只各自周期性拉取 `main`。
- 归属建议：5.1–5.4 偏后端重构 → 本机 opencode + 你；5.5 MailBots 线上报错最紧迫 → 小叽优先/并行。分目录，冲突面小。
- 防冲突沿用「分目录 + 只追加不删除 + 细粒度提交」（见之前 5.1/5.5 冲突分析）。

### 0.1 `tasks/` 任务下发与同步（回答"tasks 在 main 吗 / 两个开发环境怎么拉 main"）
- `tasks/` 是仓库内的**新目录**（目前尚未创建），存放任务说明 / 审批标记。走 `dev` → PR → `main`，所以**合到 main 后、生产 `D:\YXO_DATA\yxo_app` 在 `deploy.ps1` 跑 `git pull --ff-only origin main` 时才会拿到**；开发期小叽（和本机）从 `dev` 经 `git pull origin dev` 读取 tasks（标准分布式做法：两个开发环境都站 `dev`、拉 `origin dev`、从 dev 开 `feature-xxx`、PR 回 `dev`，见下）。
- **两个开发环境把最新 dev 拉下来的指令（两边一样，标准分布式做法）**：
  ```bash
  git checkout dev
  git pull origin dev          # 拉取团队公共进展 + tasks/ 任务清单（快进合并）
  git fetch origin             # 同步远端所有分支引用（含 main/dev/feature-*）
  # 偶尔对齐生产真相：
  git merge origin/main        # 把 main 的新提交并入当前 dev（避免偏离生产太远）
  ```
  - 本机（芙蕾雅/骁洋）和服务器 `E:\yxo_app_dev`（小叽）**都站 `dev`**、都跑上面这组即可；这是**公共集成分支**，谁都能拉——"两个环境提交分开"指的是各自 `feature-xxx` 私人分支分开，**不**是指 `dev` 不拉。
  - 从 `dev` 开活：`git checkout -b feature-5.2-xiaoji` → 写完 `git push -u origin feature-5.2-xiaoji` → 开 PR 目标选 `dev`。
  - 生产 `D:\YXO_DATA\yxo_app`（在 `main`）部署时：`git pull --ff-only origin main`（`deploy.ps1` 已内置）——**只有生产机拉 main，开发环境不拉**。

## 1. n8n 跑在哪 —— 已定：两边都装，但只有一边 Active

| 位置 | 角色 | 状态 | 装法 |
| --- | --- | --- | --- |
| **服务器** | 生产编排中枢 | **Active，7×24 常驻** | nssm 服务化（无 Docker） |
| **本机** | 流程设计/调试台 | **全部 Inactive，只手动执行** | `npx n8n`，用完即关 |

心智模型直接套用现有分支策略：**本机 n8n = dev 沙箱，服务器 n8n = main 生产**。设计在本机、执行在服务器，设计好导出 JSON 导入服务器。

### 1.1 为什么生产必须在服务器
n8n **只有常驻才有意义**。本机会关机、会带走、会睡觉；服务器 7×24 在线（2026-08-09 01:06 实测，凌晨仍在跑，watchdog 稳跑 31h）。定时轮询、夜间自动 review 这些事，本机一关机就全废。

### 1.2 ⚠️ 两边不能同时 Active（重复触发陷阱）
同一个工作流在两边都 Active 会导致：
- 同一个 PR 被 opencode **审两次**（浪费 API 额度）
- 企微/钉钉收到**两条重复通知**
- 最坏情况：**触发两次部署**

**铁律：本机所有工作流一律保持 Inactive，只用「手动执行」按钮测试。**

### 1.3 小叽给 opencode 派活，不需要 n8n
两者在**同一台机器**上，小叽直接 `subprocess.run(["opencode", "run", ...])` 即可。中间塞 n8n 只是凭空多一个故障点。
**n8n 的职责边界 = 跨系统事件编排（GitHub 事件 → 动作 → 通知），不是本机进程调度。**

### 1.4 webhook 进不来 → 先用轮询
本机和服务器都在 ZeroTier 内网、**无公网 IP**，GitHub 的 webhook 推不到任何一边。

| 方案 | 实时性 | 依赖 | 建议 |
| --- | --- | --- | --- |
| **Schedule Trigger 轮询 GitHub API** | 延迟 ≤2 分钟 | 零 | **先用这个**，API 配额 5000/h 绰绰有余 |
| Cloudflare Tunnel | 实时 | 需域名 + 配置 | 觉得 2 分钟延迟难忍时再上 |
| ngrok | 实时 | 免费版地址会变 | 不推荐用于常驻 |

→ WF1/WF2 的 GitHub Trigger 节点改成 **Schedule + GitHub API 查询**。

## 2. WF1 · PR Opened → 自动 Review
触发：GitHub `pull_request` 事件 `opened`/`synchronize`/`reopened` → n8n GitHub Trigger 节点。
节点流：
1. **GitHub Trigger**（PR opened）→ 取 PR 号、diff、base/main。
2. **Code/HTTP**：调 opencode 做评审。两种方式：
   - 本地/服务器 opencode 暴露 HTTP：`curl -X POST <opencode>/review -d '{"pr":<n>}'`
   - 或 n8n 直接 Execute Command：`opencode run "review PR #<n> and comment"`
3. **GitHub** 节点：在 PR 下贴评审评论（issue_comment / review）。
4. **HTTP Request**：POST 企微/钉钉 webhook → 「新 PR 待审：#号 标题 链接」。
- **关键门**：WF1 只贴评审 + 通知，**绝不自动 merge**（合并必须你点）。
- 失败处理：opencode 失败重试 2 次，仍失败 → 通知人「review 失败，请人工」。

## 3. WF2 · PR Merged → 部署通知
触发：GitHub `pull_request` `closed` 且 `merged == true`，且 base == main。
节点流：
1. 判断 `merged` 与 base 分支。
2. **HTTP Request** → 企微/钉钉：「PR #n 已合 main，请部署」。
3. （可选自动）经 ZeroTier/SSH 调**生产机** `D:\YXO_DATA\yxo_app`：拉 `git pull --ff-only origin main` + 跑 `deploy.ps1`（小叽的 dev 环境不负责部署，只提交 PR）。
4. 部署结果回写群（成功/失败）。
- **关键门**：先「通知 + 小叽确认」手动部署；稳定后改「自动部署」。列入待拍板。

## 4. GitHub Action · 自动测试（零依赖，先上）
`.github/workflows/ci.yml`：
```yaml
name: CI
on:
  push:
    branches: [dev]
  pull_request:
    branches: [dev, main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.13' }
      - run: pip install -r requirements.txt
      - run: pytest -q
```
失败 → PR 标红 + 群通知。这份不依赖 n8n，今天就能受益。

## 5. 通知渠道（企微/钉钉）
- 选一个群机器人 webhook，设 `YXO_NOTIFY_WEBHOOK`（已有 `scripts/notify.ps1` 可复用）。
- n8n 用 HTTP Request POST 该 webhook。

## 6. 职责边界（防过度设计）

| 干这个 | 用什么 | 不要用 |
| --- | --- | --- |
| 跑 pytest | **GitHub Actions** | n8n（无谓引入穿透与维护成本） |
| PR 自动 review | n8n（服务器）→ opencode | — |
| 发通知 | n8n → 企微/钉钉 webhook | — |
| 小叽调本机 opencode | **直接 subprocess** | n8n |
| 部署 | `deploy.ps1` | 让 n8n 自己写文件 |

## 7. 执行顺序
① GitHub Actions 自动测试（**纯本机可做，零依赖，立刻受益**）→ ② 本机 `npx n8n` 设计 WF1/WF2（Inactive）→ ③ 服务器装 n8n + nssm 常驻 → ④ 导入 JSON 并 Active → ⑤ 接企微/钉钉通知。

## 8. 已定 & 待确认

**已定**
- n8n：服务器 Active 生产 + 本机 Inactive 设计台
- 事件获取：先用定时轮询，不做内网穿透
- CI 测试：GitHub Actions，不进 n8n
- 小叽 → opencode：直接调用，不经 n8n

**待确认**
- [ ] 合并后：自动部署，还是只通知小叽手动部署？
- [ ] 通知用企微还是钉钉？
- [ ] 5.1–5.5 谁认领哪条分支（本机 opencode / 服务器 opencode / 小叽）？
