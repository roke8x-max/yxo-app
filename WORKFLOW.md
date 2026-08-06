# 渝新欧订舱系统 —— 协作工作流

> 本文是**唯一权威**的协作规则。AGENTS.md 只讲代码规范，流程一律以本文为准。
> 最后更新：2026-08-06

---

## 0. 一分钟看懂

```
        本机开发                    服务器 E 盘开发
   C:\...\Projects\yxo-app          E:\yxo_app_dev
        （骁洋 + 芙蕾雅）                （小叽）
              |                            |
              |  push 到 dev               |  push 到 dev
              +-------------+--------------+
                            v
                   ┌─────────────────┐
                   │  GitHub  dev    │  ← 集成沙箱，允许出错
                   └────────┬────────┘
                            │  Pull Request（骁洋审核后合并）
                            v
                   ┌─────────────────┐
                   │  GitHub  main   │  ← 生产真相，永远保持可运行
                   └────────┬────────┘
                            │  deploy.ps1 拉取（带备份）
                            v
                  服务器 D 盘生产环境
                  D:\YXO_DATA\yxo_app
                        （跑业务）
```

**三句话记住：**

1. 所有人写代码都推 `dev`，谁也不许直接推 `main`
2. `dev` 跑通了，开一个 Pull Request，骁洋点合并才进 `main`
3. 生产环境只认 `main`，而且只能用 `deploy.ps1` 部署（自动备份，出事能回滚）

---

## 1. 为什么这么设计

订舱系统现在已经在跑真实业务了。**它坏一天，业务就停一天。**

之前吃过一次亏：通过 filebrowser 直接把代码传到服务器，代码有问题，前端整个没了，只能重新开发。那次之后定的规矩就是——**任何进入生产的东西，都必须能一键退回上一个能跑的版本。**

这套流程就是把那条规矩落实到每一步：

| 环节 | 防的是什么 |
|---|---|
| 只推 dev，不碰 main | 半成品代码进不了生产 |
| PR 才能合 main | 每次进生产都有一次人工过目 |
| 本地 hook 拦截直推 | 手滑推错分支会被当场拦下 |
| deploy.ps1 先备份 | 部署前数据库和配置有快照 |
| rollback.ps1 | 出事 30 秒退回上一版 |
| 禁用 filebrowser 传代码 | 不留任何绕过 git 的口子 |

---

## 2. 三个环境

| 环境 | 路径 | 谁用 | 分支 | 能不能改代码 |
|---|---|---|---|---|
| 本机开发 | `C:\Users\Roke8x\Projects\yxo-app` | 骁洋 + 芙蕾雅 | dev | 能 |
| 服务器开发 | `E:\yxo_app_dev` | 小叽 | dev | 能 |
| 服务器生产 | `D:\YXO_DATA\yxo_app` | 无人 | main | **绝对不能** |

三个目录都是完整的 git 仓库，各自独立，**互不直接通信**——所有交流都经过 GitHub。

GitHub 仓库：`roke8x-max/yxo-app`（私有）

---

## 3. 分支模型

只有两条分支，别再建第三条。

**`dev` —— 集成沙箱**
日常干活的地方。允许有 bug、允许推到一半、允许两个人的改动在这里撞车。它的作用就是让代码在进生产之前先碰个面。

**`main` —— 生产真相**
永远保持"拉下来就能跑"的状态。**只能通过 Pull Request 从 dev 合入**，任何人不许直接推。

> **为什么不用 `feature/xxx` 这类分支？**
> 现在是三个角色串行干活，加更多分支只会增加记忆负担。真遇到"改到一半不能上线、但又得先发另一个东西"的情况，临时从 dev 开一条，五分钟的事。
>
> **另外提醒一句**：分支名不要带斜杠。之前有个 `dev/fix-admin-api-load` 分支，直接导致 `dev` 分支创建失败（git 的引用是按文件路径存的，`dev` 和 `dev/xxx` 不能共存），8/5 那次 `.git` 损坏也是它引起的。

---

## 4. 日常开发（本机 / E 盘都一样）

### 4.1 开工前先同步

```powershell
git checkout dev
git pull origin dev
```

**每次开工都要做。** 跳过这步 = 基于旧代码开发 = 待会儿一定冲突。

### 4.2 边改边存档

```powershell
git add .
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
git push origin dev
```

推的是 `dev`。如果你手滑写成了 `main`，会看到这样的拦截提示：

```
  ======================================================
   已拦截：不允许直接 push 到 main
  ======================================================
```

看到了就按提示改回 dev，不要用 `--no-verify` 绕过。

### 4.4 冲突了怎么办

`git pull` 时报 conflict，说明你和别人改了同一个地方。

```powershell
git pull origin dev              # 报冲突
git status                       # 看哪些文件冲突了
# 打开冲突文件，找 <<<<<<< ======= >>>>>>> 三行标记
# 手动决定保留哪部分，把三行标记全部删掉
git add <改好的文件>
git commit                       # 不用写 message，git 会自动生成
git push origin dev
```

**拿不准就别猜**，把冲突文件发给骁洋或芙蕾雅，判断错了会把别人的代码删掉。

---

## 5. 从 dev 合入 main（Pull Request）

什么时候开 PR：**dev 上积累的改动已经自测通过，可以上生产了。**

### 5.1 开 PR

命令行（推荐，装了 `gh` 的话）：

```powershell
gh pr create --base main --head dev --title "本周订舱模块改进" --body "改了什么、测过什么"
```

没装 `gh` 就走网页：打开 https://github.com/roke8x-max/yxo-app ，
push 完 GitHub 会顶部弹出 **Compare & pull request** 按钮，点它，
确认 `base: main ← compare: dev`，填标题正文，提交。

### 5.2 骁洋审核

PR 页面的 **Files changed** 标签会逐行显示这次改了什么。

审的是**业务逻辑对不对**，不是代码写得好不好：
- 这个改动会不会影响正在跑的订舱流程？
- 有没有把客户名、运价、提单号这类真实数据写进代码？
- 数据库结构有没有变？变了的话老数据怎么办？

有问题就在 PR 里留言，开发的人继续往 dev 推，PR 会自动更新，不用重开。

### 5.3 合并

确认没问题后，在 PR 页面点 **Merge pull request**。

命令行等价操作：

```powershell
gh pr merge --merge
```

> 用 **Merge**（默认那个），不要选 Squash 或 Rebase——保留完整历史，出事时好查是哪一次改动引起的。

合并后 GitHub 会问要不要删 dev 分支，**选不删**。dev 是长期分支，一直用。

### 5.4 合并后各环境同步

```powershell
git checkout dev
git pull origin dev      # dev 此时和 main 一致了
```

---

## 6. 部署到生产（只在 D 盘做）

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
3. 在 dev 上修好，重新走 PR

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
git checkout dev
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
| push 时看到"已拦截：不允许直接 push 到 main" | 推错分支了 | `git checkout dev` 再推 |
| `! [rejected] ... non-fast-forward` | 别人先推了，你本地落后 | `git pull origin dev` 解决冲突后再推 |
| `unable to auto-detect email address` | 没配 git 身份 | 见 9.3 |
| `detected dubious ownership` | 目录属主和当前登录用户不一致 | `git config --global --add safe.directory '*'` |
| push 卡住不动 / 超时 | 代理没配或 IP 变了 | 见 9.4，先 `Test-NetConnection` 验证 |
| deploy.ps1 报"生产目录有未提交的改动" | 有人直接改了生产代码 | **别急着丢弃**，先看是什么改动，有用的话在开发环境重做走 PR |
| deploy.ps1 报"拉取失败，历史分叉了" | 生产目录被 commit 过 | 联系骁洋，别自己 reset |
| `cannot lock ref 'refs/heads/dev'` | 存在 `dev/xxx` 这种带斜杠的分支 | 删掉那个分支：`git push origin --delete dev/xxx` |
| 页面 500 / 功能坏了（刚部署完） | 新代码有问题 | 立刻 `rollback.ps1`，先恢复业务 |

---

## 11. 速查卡

**开发（本机 / E 盘）**

```powershell
git checkout dev && git pull origin dev     # 开工
git add . && git commit -m "fix: xxx"       # 存档（勤做）
git push origin dev                          # 发布给队友（自测通过后）
gh pr create --base main --head dev --fill   # 申请上生产
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
GitHub       roke8x-max/yxo-app（私有）
本机开发     C:\Users\Roke8x\Projects\yxo-app       dev
服务器开发   E:\yxo_app_dev                          dev
服务器生产   D:\YXO_DATA\yxo_app                     main
备份         D:\YXO_DATA\backups\时间戳\
服务器 IP    10.0.199.184
代理         http://10.183.1.185:7897（Clash Verge，IP 会变）
```

---

## 附：还没做的事

- [ ] 服务器 Flask 服务用 nssm 注册成 Windows 服务（现在是手动跑 start.bat，重启机器要人工介入）
- [ ] 数据库结构变更（加字段、改表）目前没有迁移脚本，靠手工同步，有风险
- [ ] 本机 `C:\Users\Roke8x\Projects\yxo-app-broken-20260806` 是 8/6 修 git 时的备份目录，观察几天没问题就可以删
