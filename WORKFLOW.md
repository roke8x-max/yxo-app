# yxo-app 协作工作流

> 适用于：毛骁洋（业务裁决）、芙蕾雅（本机 AI）、小叽（服务器 AI）
> 最后更新：2026-08-06

---

## 一、一句话原则

**GitHub 的 `main` 分支是唯一真相源。谁改的代码，只有 push 上去了才算数。**

代码进了 `main` ≠ 已经上线。生产环境要手动 `git pull` 才生效——这一步手动操作，就是上线前的最后一道闸门。

---

## 二、四个环境

| 环境 | 路径 | 谁在用 | 能改代码吗 |
|---|---|---|---|
| 本机开发 | `C:\Users\Roke8x\Projects\yxo-app` | 骁洋 + 芙蕾雅 | 可以 |
| 服务器开发 | `E:\yxo_app_dev` | 小叽 | 可以 |
| 服务器生产 | `D:\YXO_DATA\yxo_app` | 跑业务的 | **不可以，只 pull** |
| GitHub | `roke8x-max/yxo-app` | 真相源 | 通过 push |

**服务器上 D 盘和 E 盘的分工：D 盘只管跑，E 盘只管写。** 两个目录各自独立，通过 GitHub 同步，互不干扰。

三个本地目录的 `origin` 都必须指向 GitHub，不能互相指。

---

## 三、只用一条 main 分支

不设 `dev` 分支。原因：

- 隔离已经由「不同机器」实现了——本机和 E 盘各改各的，没 push 之前谁也看不见谁
- 三个角色基本串行工作，不存在"多人同时改同一个功能"
- 分支多了就有人记错该推哪条，历史上已经留下过两个没人清理的死分支

**什么时候才需要开分支**：某个改动做到一半不能上线，但同时又必须先部署另一个紧急修复。到那天临时开一条，五分钟的事。

---

## 四、日常开发怎么做

### commit 勤一点，push 慎一点

- `git commit` = 存档。只在自己机器上，不影响任何人，改崩了能读档回去。**改一小块能跑通就存一次。**
- `git push` = 发布给队友。**功能整体确认没问题了再推。**

### 本机（骁洋 + 芙蕾雅）

```powershell
cd C:\Users\Roke8x\Projects\yxo-app
git pull                          # 开工前先拉，避免和小叽撞车
# ... 改代码 ...
git add .
git commit -m "用业务语言说清改了什么"
git push origin main              # 确认无误后再推
```

### 服务器 E 盘（小叽）

```powershell
cd E:\yxo_app_dev
git pull
# ... 改代码 ...
git add .
git commit -m "用业务语言说清改了什么"
git push origin main
```

小叽的硬约束：

1. **绝不碰 `D:\YXO_DATA\yxo_app`**，所有开发都在 E 盘
2. 改完不要重启生产服务，等骁洋走部署流程
3. push 前先 `git pull`，把别人的改动合进来
4. 如果代理不通导致 push 失败：**只 commit 不 push**，在 `.openclaw/requests/` 留言说明"E 盘有未推送改动"，由本机代推

### commit 信息怎么写

用业务语言，不要写"修改若干文件"。好例子：

- `fix: 舱单导出漏掉中转口岸字段`
- `feat: 订舱委托书支持批量生成`
- `chore: 报价单解析兼容合并单元格`

---

## 五、部署到生产

```powershell
cd D:\YXO_DATA\yxo_app
git pull --ff-only origin main
# 重启服务（nssm 装好后改为 nssm restart yxo-app）
```

### 为什么用 `--ff-only`

`--ff-only` 的意思是"只允许快进"。如果 D 盘生产目录被人偷偷改过代码，这条命令会**直接失败并报错**，而不是稀里糊涂合并。等于自动发现违规，但不会删掉任何东西。

**如果 pull 报错了怎么办**：说明 D 盘有人动过。先跑 `git status` 看改了什么，把有价值的改动挪到 E 盘重做一遍，再回来部署。不要用 `git reset --hard` 硬来，那会直接抹掉。

### nssm 服务化（待办）

生产目录的 Flask 目前还是手动启停。装好 nssm 后：

```powershell
nssm install yxo-app "<python路径>" "D:\YXO_DATA\yxo_app\app.py"
```

之后部署就是 `git pull --ff-only` + `nssm restart yxo-app`，不用远程桌面手动开关。

---

## 六、两边改了同一个地方怎么办

- **改的不是同一处**：git 自动合并，无感通过
- **改的是同一处**：git 会停下来标记冲突，等人拍板

**git 不会自己"择优"**，它只做两件事：不打架的自动留下，打架的原样标出来让人选。**冲突永远不会丢代码**，两边的内容都会写在文件里，随时可以反悔。

骁洋看不懂代码时的处理方式：把冲突文件丢给芙蕾雅，要求用**业务语言**讲清楚"本机版改成了什么效果、服务器版改成了什么效果、可选 A/B/C"，按业务语义拍板，AI 改完再合并。

---

## 七、环境配置速查

### 服务器上网（服务器不装梯子）

服务器通过本机的 Clash Verge 借道访问 GitHub。

- 本机内网 IP：`10.183.1.185`（以太网），代理端口 `7897`
- 本机 Clash Verge 必须开着「局域网连接」开关
- 服务器一次性配置：

```powershell
git config --global http.proxy http://10.183.1.185:7897
git config --global https.proxy http://10.183.1.185:7897
```

⚠️ 上面的 IP 是真实值，直接复制即可。如果本机 IP 变了，需要同步更新这两条。

**前提**：push/部署时本机必须开机且 Clash Verge 在线。这是低频主动操作，骁洋操作时本机必然在线，可以接受。

### GitHub 认证用 PAT，不用 OAuth

网页版 GitHub 登录时走 Google 账号授权会超时（代理规则不覆盖第三方认证域名）。统一用 Personal Access Token：

- 生成：GitHub → Settings → Developer settings → Tokens (classic) → 勾 `repo` → No expiration
- 服务器一次性配置：`git config --global credential.helper store`
- 第一次 push 时，Username 填 GitHub 用户名，Password 粘贴 token（不是账号密码）
- 之后自动记住，不再询问

### git 身份（不配会拒绝 commit）

```powershell
git config --global user.name "你的名字"
git config --global user.email "GitHub 注册邮箱"
```

### 跨用户操作报「可疑所有权」

服务器上不同 Windows 账号操作同一个目录时会被 git 拦截：

```powershell
git config --global --add safe.directory '*'
```

---

## 八、绝对不能进 GitHub 的东西

`.gitignore` 已经屏蔽了整个 `data/` 目录。push 前如果发现下面这些出现在 `git status` 里，**停下来先处理**：

- `*.db` / `*.db.bak_*` —— 生产数据库和备份
- `config_local.py` / `.env` —— 密码、webhook 地址
- `data/tuoshu_out/` —— 客户订舱委托书
- `*.xlsx` / `*.zip` —— 可能含客户名、运价、提单号

代码文件里也不要硬编码密钥，敏感配置一律放 `config_local.py`（该文件不入库）。

---

## 九、卡住了怎么自查

| 现象 | 多半是什么 | 怎么办 |
|---|---|---|
| push 报 rejected | 别人先推了，你落后了 | 先 `git pull` 再 push |
| pull 报 conflict | 两边改了同一处 | 看第六章，找芙蕾雅用业务语言拍板 |
| push 卡住不动 | 本机 Clash Verge 没开 | 开梯子，或只 commit 等本机代推 |
| 生产 pull 报错 | D 盘被人改过代码 | 看第五章，别用 reset --hard |
| commit 被拒绝 | 没配 user.name / user.email | 看第七章 |
| 提示可疑所有权 | 跨 Windows 账号操作 | `safe.directory '*'` |

---

## 十、OpenClaw 协作约定

- 需求单放 `.openclaw/requests/`
- patch 摘要放 `.openclaw/patches/`
- 小叽有话对骁洋说，写在需求单里，不要只在对话里讲
