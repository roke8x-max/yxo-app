# yxo-app 分布式开发工作流

> 本文档是 yxo-app 项目的协作约定，OpenClaw（芙蕾雅 / 小叽）、毛骁洋都应遵守。
> 目标：多人/多 AI 在各自环境开发，通过 GitHub 协作，服务器只跑"被部署的制品"。

---

## 一、核心原则：单一真相源（Single Source of Truth）

**GitHub `roke8x-max/yxo-app` 是唯一的代码真相源。**

- `main` 分支 = 稳定、可部署的版本（只通过 review 合并，禁止直接 push）
- `dev` 分支 = 日常开发 / 协作分支（小叽、本机都 push 到这里）

任何环境的代码，最终都要回到 GitHub 才能算"正式存在"。

---

## 二、四个环境的角色（钉死）

| 环境 | 路径 | 角色 | 能不能直接改 |
|---|---|---|---|
| **本机开发** | `C:\Users\Roke8x\Projects\yxo-app` | 毛骁洋 + 芙蕾雅 开发 | ✅ dev 分支 |
| **服务器生产** | `D:\YXO_DATA\yxo_app` | 只跑的生产副本 | ❌ **绝不改**，只 `git pull main` 部署 |
| **服务器开发** | `E:\yxo_app_dev` | 小叽 开发用 | ✅ dev 分支 |
| **GitHub** | `roke8x-max/yxo-app` | 唯一真相源 | 经 PR / review |

> 服务器目录职责分离：**D 盘 = 跑业务的，E 盘 = 写代码的**。两者通过 GitHub 同步，互不直接干扰。

---

## 三、小叽（服务器）工作规则 —— 硬约束

1. **绝不改 `D:\YXO_DATA\yxo_app`**（生产目录）。
2. 所有开发在 **`E:\yxo_app_dev`** 进行。
3. 改完代码执行：
   ```powershell
   cd E:\yxo_app_dev
   git add .
   git commit -m "用业务语言描述你改了什么"
   git push origin dev
   ```
   > 服务器已配置 git 走本机 LAN 代理（见第五章），`git push` 自动借道本机梯子。若代理不可用，则**只 commit 不 push**，在需求文件里告诉毛骁洋"E 盘有未推送改动"，由本机代 push。
4. **改完不要 restart 生产服务**，也不要碰 D 盘生产目录——等毛骁洋 review 后由部署流程生效。
5. **不直接 merge 到 main**，main 由毛骁洋在本机裁决。
6. 若启动需要本机那样的"虚拟模块降级"，`E:\yxo_app_dev` 同样适用 `admin_api.py` 的优雅降级逻辑（环境变量 / `config_local.py` 配置外部模块路径）。

---

## 四、本机（毛骁洋 + 芙蕾雅）工作规则

1. 在 `C:\Users\Roke8x\Projects\yxo-app` 开发（dev 分支）。
2. 改完：`git add . && git commit && git push origin dev`。
3. 发起 review（PR 或人工核对），合并到 `main` 后再部署。
4. OpenClaw workspace 已指向本机 yxo-app，需求放 `.openclaw/requests/`，patch 放 `.openclaw/patches/`。

---

## 五、部署流程（中期方案：nssm 服务化，稳定、可远程）

### 服务器不装梯子（重要约束）
服务器电脑无法稳定常驻梯子（设备数限制 + 节点不稳），所以：
- **服务器不安装任何梯子**，所有对 GitHub 的访问通过「本机 LAN 代理」借道。
- 本机 FlClash/Clash 开启 **Allow LAN（局域网代理）**，记下本机内网 IP（如 `10.0.199.xxx`）和代理端口（如 `7890`）。
- 服务器一次性配置 git 走本机代理：
  ```powershell
  git config --global http.proxy http://10.0.199.xxx:7890
  git config --global https.proxy http://10.0.199.xxx:7890
  ```
- 部署/推送时**本机梯子必须开着**，服务器 git 自动借本机梯子访问 GitHub。本机关机或关梯子时服务器访问不了 GitHub——但部署是低频主动动作，你操作时本机必然在线，满足。

### nssm 服务化（稳定、可远程重启）
生产目录 `D:\YXO_DATA\yxo_app` 用 **nssm** 把 Flask 注册为 Windows 服务（服务名 `yxo-app`），部署只需：

```bat
@echo off
cd /d D:\YXO_DATA\yxo_app
git pull origin main
nssm restart yxo-app
echo 部署完成 -> http://127.0.0.1:5011
```

- nssm 安装（服务器一次性）：`nssm install yxo-app "C:\path\to\python.exe" "D:\YXO_DATA\yxo_app\app.py"`
- 之后部署 = `git pull` + `nssm restart`，无需远程桌面手动启停。

### 备选：服务器完全零外网（robocopy 推送）
若本机 LAN 代理也不可靠，可让服务器彻底不碰 GitHub：本机 `git pull main` → 本机 `robocopy C:\Users\Roke8x\Projects\yxo-app \\10.0.199.184\yxo_data\yxo_app /MIR /XD data .venv .git` → 服务器 `nssm restart`。小叽在 E 盘的改动也由本机代 push（见第六章）。

---

## 六、冲突处理（毛骁洋是业务裁决者，不是程序员）

- **不重叠的改动**：git 自动合并，秒过。
- **同一段代码两边都改了**：git 标冲突，停下来等人拍板。
- **git 不会"择优"**——它只做"不打架的自动留、打架的留给你选"。哪边好由人判断。
- 毛骁洋看不懂代码时：把冲突文件丢给芙蕾雅，用**业务语言**讲清"本机版改了 X、服务器版改了 Y、留 A/B/C"，用业务语义选，AI 改好后再合并。
- **冲突永不丢代码**——git 会把两边内容都标在文件里，随时可反悔。

### 本机做 Git 网关（服务器无梯子时的备选）
若本机 LAN 代理也不可靠，所有对 GitHub 的访问集中到本机（唯一有稳定梯子的地方）：
- 小叽在 E 盘 `git commit`（不 push），在需求文件标注"E 盘有未推送改动"。
- 本机从服务器拉取：`git fetch //10.0.199.184/yxo_data/yxo_app_dev`（或用 robocopy 同步 `.git`），`git merge` 后 `git push origin dev`。
- 部署时本机 `git pull main` → `robocopy C:\Users\Roke8x\Projects\yxo-app \\10.0.199.184\yxo_data\yxo_app /MIR /XD data .venv .git` → 服务器 `nssm restart yxo-app`。
- 这样服务器彻底零外网依赖，敏感代码不出内网。

---

## 七、第一次同步（历史债务清理，仅一次）

服务器 `D:\YXO_DATA\yxo_app` 工作区有截至 2026-08-05 的未提交小叽改动，需先收编：

```powershell
# 1. 确认状态（毛骁洋在服务器跑）
cd D:\YXO_DATA\yxo_app
git status

# 2. 收编未提交改动到 dev 分支
git add .
git commit -m "chore: 收编小叽截至 2026-08-05 的未提交改动"
git push origin dev

# 3. 本机对齐
cd C:\Users\Roke8x\Projects\yxo-app
git pull

# 4. 创建 E 盘开发目录（本地 clone，不依赖 GitHub 网络）
git clone D:\YXO_DATA\yxo_app E:\yxo_app_dev
```

收编后，D 盘工作区干净，严格走上文流程。
