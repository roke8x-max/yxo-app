# 重庆物流集团 yxo 系统 —— 协作工作流

> **本文只讲 git / 协作流程**（分支、PR、部署、故障自查）。
> 项目全貌（三大支柱、模块职责、硬规则、当前状态）见 **`AGENTS.md`** —— 新开对话请先读它。
> 最后更新：2026-09-18（**分支模型改版：只用 `main`，`dev` 废弃**；补唯一解释器、Squash 合并、故障自查新条目）

---

## 0. 一分钟看懂

```
        本机开发                    服务器 E 盘开发
   C:\...\Projects\yxo-app          E:\yxo_app_dev
        （骁洋 + 芙蕾雅）                （小叽）
              |                            |
              |  从 main 拉临时分支           |  从 main 拉临时分支
              |  push 到临时分支              |  push 到临时分支
              +-------------+--------------+
                            v
                   ┌──────────────────────┐
                   │  GitHub  临时分支      │  ← 一次改动的隔离区
                   └──────────┬───────────┘
                              │  Pull Request（骁洋审核后合并）
                              v
                   ┌──────────────────────┐
                   │  GitHub  main         │  ← 唯一真相，永远保持可运行
                   └──────────┬───────────┘
                              │  deploy.ps1 拉取（带备份）
                              v
                  服务器 D 盘生产环境
                  D:\YXO_DATA\yxo_app
                        （跑业务）
```

**三句话记住：**

1. 从 `main` 拉一条**扁平命名的临时分支**干活；**谁都不许直接推 `main`**
2. 干完开 Pull Request，骁洋点合并才进 `main`
3. 生产环境只认 `main`，而且只能用 `deploy.ps1` 部署（自动备份，出事能回滚）

> 📌 **2026-09-18 起：`dev` 已废弃**（洋批准）。所有临时分支**从 `main` 拉、PR 回 `main`**；`dev` 落后多少都无所谓，不要再往它推、也不要再从它拉。详见 §3。

---

## 1. 为什么这么设计

订舱系统现在已经在跑真实业务了。**它坏一天，业务就停一天。**

之前吃过一次亏：通过 filebrowser 直接把代码传到服务器，代码有问题，前端整个没了，只能重新开发。那次之后定的规矩就是——**任何进入生产的东西，都必须能一键退回上一个能跑的版本。**

这套流程就是把那条规矩落实到每一步：

| 环节 | 防的是什么 |
|---|---|
| 改动只走临时分支，不碰 main | 半成品代码进不了生产 |
| PR 才能合 main | 每次进生产都有一次人工过目 |
| 服务端分支保护拦截直推 main | main 由 GitHub 服务端强制，禁止直推，只能经 PR 合入 |
| 每条 PR 都有独立验收 | 合进 main 的东西是"已验证代码"，不需要再靠一个集成分支兜底 |
| deploy.ps1 先备份 | 部署前数据库和配置有快照 |
| rollback.ps1 | 出事 30 秒退回上一版 |
| 禁用 filebrowser 传代码 | 不留任何绕过 git 的口子 |

---

## 2. 三个环境

| 环境 | 路径 | 谁用 | 分支 | 能不能改代码 |
|---|---|---|---|---|
| 本机开发 | `C:\Users\Roke8x\Projects\yxo-app` | 骁洋 + 芙蕾雅 | 临时分支（从 main 拉） | 能 |
| 服务器开发 | `E:\yxo_app_dev` | 小叽 | 临时分支（从 main 拉） | 能 |
| 服务器生产 | `D:\YXO_DATA\yxo_app` | 无人 | main | **绝对不能** |

> ⚠️ 第二行那个**目录名叫 `yxo_app_dev`**，但它跟 git 分支 `dev` 没关系 —— 那只是服务器上的开发目录路径。

三个目录都是完整的 git 仓库，各自独立，**互不直接通信**——所有交流都经过 GitHub。

GitHub 仓库：`roke8x-max/yxo-app`（**公开仓库**，代码任何人可见；敏感信息一律走 `config_local.py` / 环境变量，代码内不硬编码）

---

## 3. 分支模型（2026-09-18 改版：**只用 `main`**）

**`main` —— 唯一的真相分支**
永远保持"拉下来就能跑"的状态。它是**所有临时分支的起点，也是唯一的终点**；任何人不许直接推，只能经 Pull Request 合入（服务端分支保护强制）。

**临时分支 —— 一次改动一条**
从 `main` 拉 → 干完 push → 开 PR 回 `main` → 合并后删掉。

```powershell
git checkout main && git pull --ff-only origin main      # ① 起点：先同步 main
# ② 建分支 —— 必须「扁平名」（连字符），且用 update-ref 而不是 checkout -b（原因见下方红线）
git update-ref refs/heads/fix-ingest-poll-uid-watermark <main 的完整 SHA>
git checkout fix-ingest-poll-uid-watermark               # ③ 切过去干活
```

> 📌 **`dev` 已废弃（2026-09-18，洋批准）—— 不再作为任何事情的起点或终点。**
> **为什么**：① 每条 PR 都经过**独立验收**，`main` 本身就是"已验证代码"；`dev` 当初作"集成沙箱"的价值被 PR 验收替代了。② 现在是单人 / 单流开发，没有"多条 feature 先集成"的需求。
> **怎么处置**：分支**暂时保留不删**（留作历史参照）；**落后 `main` 多少都无所谓** —— 不要往 `dev` 推，也不要再从 `dev` 拉。哪天觉得碍眼，让芙蕾雅删掉即可。

> ⚠️ **分支名绝不用斜杠**（老坑：`dev/fix-admin-api-load` 曾在 8/5 把 `.git` 搞坏；根因是 git 的引用按文件路径存，`dev` 与 `dev/xxx` 不能共存）。

> 🔴 **2026-09-17 实测：斜杠分支的「新形态」—— 不报错，但会把整仓显示成新增**
>
> 当天用 `git checkout -b fix/forward-layer-waybill-body` 建分支，**git 正常打印 `Switched to a new branch`**，但 `refs/heads/fix/` 那层目录连同文件**被静默抹掉**（`.git/refs/heads/` 里只剩 `dev`）⇒ **HEAD 指向一个不存在的分支**。
>
> **症状（记住这个画面）**：`git status` 把**整个仓库 ~250 个文件全显示成 `A`（新增）**，看起来像"所有文件都要提交"。此时若顺手 `git add` + `commit`，就会造出一个**把历史关系砸掉的 root commit**（父提交为空）。另外 `packed-refs` 里 `main` 也是过期值 —— 同一个老毛病。
>
> **立刻这样救**（全程不改任何文件内容）：
>
> ```powershell
> git symbolic-ref HEAD refs/heads/main    # ① 先把 HEAD 修回真实存在的分支
> git status --short                       # ② 确认只剩你真正改过的那几个文件
> git update-ref refs/heads/<扁平分支名> <main 的完整 SHA>   # ③ 用连字符，绝不用斜杠
> git checkout <扁平分支名>
> ls .git/refs/heads/                      # ④ 复核：这个 ref 文件真的在盘上
> ```
>
> `git update-ref refs/heads/fix/xxx` 同样「返回 0 但没落盘」；连手工 `mkdir` 建出的那层目录也会被抹掉。**扁平名实测稳定可用**（已用过：`fix-forward-layer-waybill-body`、`docs-interpreter-path`、`chore-workflow-main-only`）—— 这条已由洋确认写进 `AGENTS.md` §11。

---

## 4. 日常开发（本机 / E 盘都一样）

### 4.1 开工前先同步

```powershell
git checkout main
git pull --ff-only origin main     # 标准拉取；等价于 fetch + ff-merge
```

标准拉取就是 `git fetch origin` + `git merge --ff-only origin/main`。
**每次开工都要做。** 跳过这步 = 基于旧代码开发 = 待会儿一定冲突。

> ⚠️ **本机 git 引用损坏说明（历史，现已根治）**
> 之前本机 WorkBuddy 自带的 PortableGit 在改写 `.git/packed-refs` 时**静默失败**，`origin/main` 会变成 `[gone]`。
> 真因是**火绒(Huorong)实时防护**拦截了对 `packed-refs` 的删除/重命名（不是 Defender——Defender 当时已被火绒接管禁用，报 `0x800106ba`）。
> **已根治**：在火绒「信任区 / 排除项」加入仓库 `.git` 路径后，git 原生 `fetch`/`pull` 恢复，不再需要 `git sync` 兜底（`git sync` 别名与 `scripts/repair-refs.sh` 现已无实际操作，保留无害）。
> 若你机器未加排除项又出现 `[gone]`，把仓库 `.git` 加进火绒排除项即可，无需改用其他命令。

### 4.2 边改边存档

```powershell
git add <你改的那几个文件>          # ⚠️ 不要 git add . —— 见 4.5 安全提交流程
git commit -m "fix: 修正舱单导出时中文列名错位"
```

**commit 要勤，push 要慎。**

`commit` 就像游戏存档——只存在你自己电脑上，别人看不见，不占地方，改崩了随时能读档。憋到功能全做完才 commit 一次，中间三天的改动就没有任何存档点，想退回"昨天那个能跑的版本"是退不了的。

`push` 才是"发布给队友看"，这个确实要等确认之后再做。

commit 信息格式（照抄即可）：

```
feat: 新增了什么功能
fix:  修了什么 bug
docs: 改了文档
chore: 杂项（改配置、清理文件）
refactor: 重构，功能没变
```

### 4.3 自己验过再推

推之前**必须**在本地把改动跑一遍：启动服务、打开页面、点一下受影响的功能。

```powershell
git push origin <你的临时分支名>
```

推的是**你自己从 `main` 拉的那条临时分支**（例如 `fix-ingest-poll-uid-watermark`）。如果你手滑写成了 `main`，会看到这样的拦截提示：

```
  ======================================================
   已拦截：不允许直接 push 到 main
  ======================================================
```

看到了就把分支名改回你的临时分支，不要用 `--no-verify` 绕过。

### 4.4 冲突了怎么办

合并 `main` 时报 conflict，说明 `main` 上已经有了跟你改同一处的提交。

```powershell
git fetch origin
git merge origin/main            # 把 main 的新提交并进你的临时分支（也可以用 rebase）
git status                       # 看哪些文件冲突了
# 打开冲突文件，找 <<<<<<< ======= >>>>>>> 三行标记
# 手动决定保留哪部分，把三行标记全部删掉
git add <改好的文件>
git commit                       # 不用写 message，git 会自动生成
git push origin <你的临时分支名>
```

**拿不准就别猜**，把冲突文件发给骁洋或芙蕾雅，判断错了会把别人的代码删掉。

### 4.5 安全提交流程（强制 · 提交前逐条走一遍）

> 为什么有这一节：本项目真实踩过的坑 —— 有人手改生产代码留下"幽灵改动"被顺手带进提交；一次 `git add .` 把十几个不相干的文件混成一个"大杂烩"提交；密钥/生产库文件差点入库。**提交只需要三步：看清 → 定向 add → 验过再提交。** 别图快。

**第 0 步 · 先看清"要提交什么"（不看清单不 add）**

```powershell
git status --short          # 逐行看：哪些是改的（ M），哪些是新文件（??）
git diff --stat             # 看改动量；某个文件行数异常大就单独看一眼
git fetch origin            # 对齐远端
git branch --show-current   # 必须是你自己拉的那条临时分支，绝不能是 main
git symbolic-ref HEAD       # 看 HEAD 指向哪个分支
```

- ❌ 列表里出现 **`secrets.json` / `*.db` / `logs/` / `config_local.py` / 个人绝对路径** → **停下**：说明 `.gitignore` 漏了，先补忽略规则再提交。
- ❌ **`git status --short` 里"整个仓库的文件都标成 `A`" → 立刻停下，绝不 add/commit**：这是 **HEAD 悬空**（最常见成因：拿带斜杠的分支名建过分支，见 §3）。按 §3 的救援步骤 `git symbolic-ref HEAD refs/heads/main` 修回来再继续；**在悬空状态下提交会造出 root commit，把历史关系砸掉**。
- ❌ 出现**你根本没印象改过**的文件 → 逐个 `git diff <文件>` 看清楚；确认无关就 `git restore <文件>` 退回。**不要盲提交**（幽灵改动就是这么进库的）。
- ⚠️ `git fetch` 后 `git branch -vv` 才准。本机火绒会拦 `.git/packed-refs` 改写，导致 fetch 前显示虚假的 "ahead N"。

**第 1 步 · 定向 add —— 明令禁止 `git add .` 和 `git add -A`**

```powershell
git add <明确列出的文件或目录>       # 一个提交只装一个主题
git status --short                   # 复查：暂存区里刚好是这几个（左列出现 A/M）
```

**第 2 步 · 看"即将进去的内容"，而不是"改了哪些文件"**

```powershell
git diff --cached                    # 暂存区 vs HEAD —— 这才是这次真正要提交的东西
```

**第 3 步 · 测试必须先绿**

🔴 **用本机唯一解释器**（2026-09-18 定，洋批准）：

```powershell
$PY = "C:\Users\Roke8x\.workbuddy\binaries\python\versions\3.13.12\python.exe"
& $PY -m pip install -r requirements-dev.txt        # 首次配置（含测试专用 pytest / trustme）
& $PY -m pytest -q -p no:cacheprovider > out.txt 2>&1
# 读汇总行：必须 0 failed
```

> ⚠️ **不要用 `py -3.13`**（它指向 `D:\python.exe`，是**另一个**解释器），也不要用编辑器/其他助手自带的 Python。
> 🔴 **换解释器＝会出现"我这边绿、你那边红"** —— 9-17/18 真实踩过两次（`trustme` 没进 requirements ∧ 只装进了另一个解释器），真链路用例在别人那边**必挂**。**要换就整体换（含 OpenCode / 小叽 / CI）。**
> ⚠️ **必须用默认 basetemp**（OS 临时目录）。指定 `--basetemp=<项目内目录>` 会触发 safe-delete 拦截 → 测试在断言前假失败。若出现"跑到 100% 却没有汇总行、退出码 1"，多半是 pytest 清理旧 basetemp 触发了批量删除确认 —— **先清 `%TEMP%\pytest-of-Roke8x` 再跑**，那不是测试失败。

**第 4 步 · 提交，并把结果拿给人看**

```powershell
git commit -m "<type>: <一句话说清改了什么>"
git log --oneline -5
git show --stat HEAD                 # 确认这个提交恰好只包含预期文件
```

**第 5 步 · push 前必须获得明确批准**

- **commit 是"本机存档"，可以自主做；push 是"发布"，必须有骁洋明确点头**（"推 / 发 / 上传"）。没听到这句话就不推。
- 只推**你自己的临时分支**。`main` 有服务端保护，禁止直推。
- 不要 `git push --force`，不要 `--no-verify` 跳钩子。

**红线（碰了就是事故）**

| 禁止 | 原因 |
|---|---|
| `git add .` / `git add -A` / `git commit -a` | 会把无关改动、临时文件、密钥一次性卷进去 |
| `git push origin main` | 服务端保护会拦；别试图绕过 |
| `git push --force` | 会覆盖别人的提交 |
| 没备份就用 `git reset --hard` | 未提交的改动直接蒸发 |
| 提交 `secrets.json` / `*.db` / `logs/` / `config_local.py` | 密钥与生产数据 |
| 在生产目录 `D:\YXO_DATA\yxo_app` 改代码、commit | 那里只跑业务，只 `git pull` |

**多主题改动要拆开**

一次 `git status` 摆出几十个文件、横跨好几个功能时，**按主题拆成多个提交**（文件集尽量互不重叠），别攒成一个"大杂烩"：

- 好处：出问题能只回滚一个主题；review 时能一段一段看。
- 但若两个主题改到**同一个文件**、拆不干净，**宁可合成一个提交**，也不要用 `git add -p` 拆 hunk —— 那会造出"中间态跑不起来"的提交，比不拆更糟。
- 拆之前先想好每个提交的**文件清单**，再动手；边 add 边想要容易漏。

---

## 5. 开 PR 合入 main（Pull Request）

什么时候开 PR：**临时分支上的改动已经自测通过、可以上生产了。**

### 5.1 开 PR

命令行（推荐，装了 `gh` 的话）：

```powershell
gh pr create --base main --head <你的临时分支名> --title "本周订舱模块改进" --body "改了什么、测过什么"
```

> 💡 **正文里含 shell 代码块时别用 heredoc**
> 把带 `$PY = "…"` / `& $PY -m …` 这类 PowerShell 片段的正文直接 heredoc 喂给 `gh`，会被安全策略判成"从 Bash 调 PowerShell"而拒绝执行。
> **做法**：先把正文写进一个临时文件（如 `tmp/pr_body.md`），再用 `--body-file tmp/pr_body.md`。

没装 `gh` 就走网页：打开 https://github.com/roke8x-max/yxo-app ，
push 完 GitHub 会顶部弹出 **Compare & pull request** 按钮，点它，
确认 `base: main ← compare: <你的临时分支>`，填标题正文，提交。

### 5.2 骁洋审核

PR 页面的 **Files changed** 标签会逐行显示这次改了什么。

审的是**业务逻辑对不对**，不是代码写得好不好：
- 这个改动会不会影响正在跑的订舱流程？
- 有没有把客户名、运价、提单号这类真实数据写进代码？
- 数据库结构有没有变？变了的话老数据怎么办？

有问题就在 PR 里留言，开发的人继续往**这条临时分支**推，PR 会自动更新，不用重开。

### 5.3 合并

确认没问题后，在 PR 页面点 **Squash and merge** —— 本仓库 `main` 开了 `required_linear_history`（强制线性历史），**Merge 提交用不了**，下拉框里只有 Squash / Rebase 两项。

命令行等价操作：

```powershell
gh pr merge --squash
```

> ⚠️ **Squash 会在 `main` 上生成一个全新 SHA**（临时分支上那些原始提交不是 main 的祖先）。
> 在"只用 main"的流程下这**没有任何副作用** —— 不需要回合同步任何分支（`dev` 已废弃，见 §3）。
> 但有个**认知陷阱**：之后再从这条临时分支开 PR 时，PR 页面的 commit 列表会拖出一大串历史提交 —— **看 Files changed（diff）才算数**，那才是本次真正的改动。

合并后 GitHub 会问要不要删分支，**选删除**（临时分支用完即弃；要再改就重新从 `main` 拉一条）。

### 5.4 合并后要做什么

**只剩 `main` 一条线之后，就没有"同步分支"这回事了**（`dev` 已废弃，见 §3）。收尾三步：

```powershell
git checkout main
git pull --ff-only origin main      # 让本机 main 跟上
git branch -d <你的临时分支名>        # 删掉本地临时分支（远端那条 GitHub 已删）
```

> 📌 **历史遗留说明**（2026-09-18 之前适用）：那时 `dev` 作集成分支，而 `main` 强制线性历史、PR 只能 Squash ⇒ 每次合并都让 `dev` 与 `main` 分叉 ⇒ 本节规定"合并后必须把 main 合回 dev"。
> **现在不需要了**：`dev` 已废弃，落后多少都无所谓（只当历史参照留着）。

> ⚠️ **本机火绒会拦 `.git/packed-refs` 改写**：`git fetch` / `git push` 会"报告成功"，但本地 `origin/*` 引用**不更新** → `git rev-parse origin/main`、`git branch -vv` 都会拿到**过期值并导致误判**（2026-09-14 因此误判过"严重分叉"；2026-09-18 又出现 `git status -sb` 显示 `[gone]`，而远端分支其实好好的）。**判断远端状态一律用 `git ls-remote`（直接问服务器），或直接写完整 SHA。** 治本是给杀软信任区加仓库 `.git` 路径。

---

## 6. 部署到生产（只在 D 盘做）

> **本节只讲 Flask 订舱平台的部署。** 邮件机器人走的是另一条链路（nssm 服务 + 任务计划），
> 见 `scripts/deploy/DEPLOYMENT_MANUAL.md` 与 `AGENTS.md` §9.5。

**不要手动 `git pull`，用脚本。** 脚本会自动备份，手动拉不会。

### 6.1 正式部署

在服务器上打开 PowerShell：

```powershell
cd D:\YXO_DATA\yxo_app
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
```

脚本会依次做：

1. 检查是不是在 main 分支、工作树干不干净（**有人偷改过生产代码会当场报警**）
2. 备份 `data\yxo.db` 和 `config_local.py` 到 `D:\YXO_DATA\backups\时间戳\`
3. 把当前版本号记进 `.last_deploy`，供回滚使用
4. `git pull --ff-only origin main` 拉取新代码
5. 检查 `requirements.txt` 变没变，变了会提示装依赖
6. 提示重启服务

### 6.2 不确定就先演习

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -DryRun
```

会完整打印"这次会更新哪些文件、包含哪些提交"，但**什么都不改**。

### 6.3 部署后必做

1. 重启 Flask 服务（关掉原来的窗口，重新跑 `start.bat`）
2. 打开页面点几下：订舱能不能开、舱单能不能导出、后台能不能进

---

## 7. 出事了怎么回滚

**发现不对劲，先回滚恢复业务，再查原因。别在生产环境上调试。**

```powershell
cd D:\YXO_DATA\yxo_app
powershell -ExecutionPolicy Bypass -File scripts\rollback.ps1
```

脚本会显示"现在是哪个版本、要退回哪个版本、会撤销哪几个提交"，输入 `yes` 确认后执行，然后重启服务。

**数据库不会自动回滚。** 因为退回数据库会把这段时间录入的真实订舱数据一起抹掉，这个决定必须人来做。确实需要恢复时，脚本会把命令打出来，**执行前先问骁洋**。

回滚之后：
1. 在 GitHub 上说明原因
2. 把出问题的改动从 main 上撤掉（`git revert`），别让它躺在生产分支里
3. 拉一条新的临时分支修好，重新走 PR

---

## 8. 红线（碰了就是事故）

1. **绝不用 filebrowser / 远程桌面拖拽 / 手动复制粘贴的方式往服务器传代码。** 代码只能通过 git 进入服务器。这些方式绕过了 git，出事查不到是谁改的，也没法回滚——2026 年那次前端丢失就是这么来的。
2. **绝不在 D 盘生产目录直接改代码。** 哪怕只是改一个字。生产目录只做一件事：`git pull`。
3. **绝不直接 push 到 main。** hook 会拦你，别用 `--no-verify` 绕过（生产抢修除外，事后必须报备）。
4. **绝不把真实业务数据写进代码。** 客户名、收发货人、运价、提单号一律脱敏，测试用 `测试客户A` / `CQ0000000` 这类假数据。
5. **绝不提交 `config_local.py`、`data/yxo.db`、`*.xlsx`。** `.gitignore` 已经挡了，但 commit 前扫一眼 `git status` 是好习惯。

---

## 9. 新环境接入（一次性配置）

新机器要接进来，或者重新 clone 之后，按顺序做完这四步。

### 9.1 clone 仓库

```powershell
git clone https://github.com/roke8x-max/yxo-app.git
cd yxo-app
git checkout main        # 唯一的分支就是 main（dev 已废弃，见 §3）
```

### 9.2 装 hook（**重新 clone 后必须重做**）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-hooks.ps1
```

hook 存在 `.git\hooks\` 里，这个目录**不会跟着 git 走**，所以每台机器、每次重新 clone 都得重装一次。不装的话直推 main 就没人拦了。

### 9.3 配身份

```powershell
git config --global user.name  "你的名字"
git config --global user.email "GitHub 注册邮箱"
```

邮箱必须是 GitHub 注册用的那个，否则提交记录上的头像点不亮。

### 9.4 配代理（服务器专用，本机不需要）

服务器直连 GitHub 会超时，得走骁洋本机的代理：

```powershell
git config --global http.proxy  http://10.183.1.185:7897
git config --global https.proxy http://10.183.1.185:7897
```

⚠️ **`10.183.1.185` 是骁洋本机的内网 IP，会变。** 换过网络、重启过路由之后要重新确认，别直接照抄旧值。当前用的是 Clash Verge，端口 7897。

验证通不通：

```powershell
Test-NetConnection -ComputerName 10.183.1.185 -Port 7897
```

看 `TcpTestSucceeded` 是不是 `True`。

> **新方向（见 plans/01-remote-access-ladder.md）**：服务器后续改为**自身运行 mihomo 内核**直连出网（规则模式、内网 `10.0.0.0/8` 走 DIRECT），不再依赖本机 `10.183.1.185` 代理。落地前本条仍按上面本机代理配置执行。

### 9.5 配 GitHub 认证

用 **PAT（个人访问令牌）**，不要走 OAuth 网页登录——服务器经代理访问，OAuth 会跳转到第三方域名，基本必超时。

1. 骁洋在本机浏览器打开 https://github.com/settings/tokens/new
2. 选 Classic，勾 `repo`，有效期选 No expiration，生成
3. **令牌只显示一次**，关掉页面就再也看不到了，先复制下来
4. 服务器上执行：

```powershell
git config --global credential.helper store
```

5. 第一次 `git push` 时会问账号密码：Username 填 `roke8x-max`，Password **粘贴令牌**（不是 GitHub 密码）

存一次，以后不用再输。

---

## 10. 故障自查表

| 现象 | 原因 | 怎么办 |
|---|---|---|
| push 时看到"已拦截：不允许直接 push 到 main" | 推错分支了 | 把推送目标改回你自己的临时分支再推 |
| `! [rejected] ... non-fast-forward` | 别人先推了，你本地落后 | `git fetch origin` + `git merge origin/main` 解决冲突后再推 |
| `unable to auto-detect email address` | 没配 git 身份 | 见 9.3 |
| `detected dubious ownership` | 目录属主和当前登录用户不一致 | `git config --global --add safe.directory '*'` |
| push 卡住不动 / 超时 | 代理没配或 IP 变了 | 见 9.4，先 `Test-NetConnection` 验证 |
| deploy.ps1 报"生产目录有未提交的改动" | 有人直接改了生产代码 | **别急着丢弃**，先看是什么改动，有用的话在开发环境重做走 PR |
| deploy.ps1 报"拉取失败，历史分叉了" | 生产目录被 commit 过 | 联系骁洋，别自己 reset |
| `cannot lock ref 'refs/heads/xxx'` | 存在 `xxx/yyy` 这种带斜杠的分支 | 删掉那个分支（本地 `git branch -D`，远端 `git push origin --delete xxx/yyy`） |
| `git status` 里整仓文件都显示成 `A`（新增） | HEAD 悬空（多半是拿带斜杠的分支名建过分支） | **绝不要 add/commit**，按 §3 的救援步骤修 HEAD |
| PR 页面 commit 列表拖出一大串历史提交 | 那些是 squash 之前的原始提交，不是 main 的祖先 | 正常现象，**看 Files changed 的 diff 才算数** |
| 页面 500 / 功能坏了（刚部署完） | 新代码有问题 | 立刻 `rollback.ps1`，先恢复业务 |

---

## 11. 速查卡

**开发（本机 / E 盘）**

```powershell
# ① 开工：从 main 拉一条「扁平命名」的临时分支（绝不用斜杠）
git checkout main && git pull --ff-only origin main
git update-ref refs/heads/fix-xxx-yyy <main 的完整 SHA> && git checkout fix-xxx-yyy

# ② 边改边存档（勤做；⚠️ 定向 add，绝不用 git add .）
git add <你改的那几个文件> && git commit -m "fix: xxx"

# ③ 自测通过后 push 你自己的临时分支（push 前要有骁洋点头）
git push origin fix-xxx-yyy

# ④ 申请合入 main
gh pr create --base main --head fix-xxx-yyy --fill
```

**部署（D 盘）**

```powershell
cd D:\YXO_DATA\yxo_app
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -DryRun   # 先演习
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1           # 真部署
powershell -ExecutionPolicy Bypass -File scripts\rollback.ps1         # 出事回滚
```

**关键位置**

```
GitHub       roke8x-max/yxo-app（公开）
本机开发     C:\Users\Roke8x\Projects\yxo-app       临时分支（从 main 拉）
服务器开发   E:\yxo_app_dev                          临时分支（从 main 拉）
服务器生产   D:\YXO_DATA\yxo_app                     main
已废弃分支   dev（保留作历史参照 —— 勿推、勿拉，见 §3）
备份         D:\YXO_DATA\backups\时间戳\
服务器 IP    10.0.199.184
代理         http://10.183.1.185:7897（Clash Verge，IP 会变）
```

---

## 附：还没做的事

- [ ] **上云改造**：**等数科部完成申请阿里云的流程之后**再统一启动。现存 Linux 预研产物（`deploy/systemd`、`deploy/logrotate`、`scripts/deploy.sh`、`scripts/rollback.sh`）**暂时保留不动**，详见 `AGENTS.md` §9.6
- [ ] 服务器 Flask 服务用 nssm 注册成 Windows 服务（现在是手动跑 start.bat，重启机器要人工介入）—— 邮件机器人已走 nssm，Flask 还没
- [ ] 数据库结构变更（加字段、改表）目前没有迁移脚本，靠手工同步，有风险
- [ ] 远端遗留分支 `feature/plan-b-processors-idle` 待清理
- [ ] 本机 `C:\Users\Roke8x\Projects\yxo-app-broken-20260806` 是 8/6 修 git 时的备份目录，观察几天没问题就可以删
