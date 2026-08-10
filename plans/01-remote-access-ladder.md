# 01 · 小叽服务器：远程可达 + 自主出网（梯子）+ 服务化 + 唤醒恢复

> 目标：让小叽 7×24 可用。你不在办公室/下班后，仍能远程救火；小叽也能在自己跑完时自动 push，不等你第二天手动叫。
> 状态：本机探测已完成，以下均为**待执行方案**，未改动任何服务器/本机。

## 0. 现状（2026-08-09 01:06 复测，权威版）

> ⚠️ **更正 8/8 初版的误判**：初版写「本机→服务器全不可达」，是因为用 ICMP ping 判断存活。
> Windows 防火墙默认丢弃 ICMP echo，机器在线也 ping 不通。**该结论作废。**

**服务器实际状态：7×24 在线，经 ZeroTier 完全可达。**

| 通道 | 状态 | 说明 |
| --- | --- | --- |
| ZeroTier 隧道 | ✓ | 本机 `10.0.199.74` ↔ 服务器 `10.0.199.184`，同 `10.0.199.0/24` |
| 445 SMB / 135 RPC | ✓ | 共享盘 `yxo_data` 可读写，二层网络完全打通 |
| 5011 yxo_app / 5001 WeComBot | ✓ | 业务服务在跑（凌晨 01:06 实测） |
| watchdog | ✓ pid 15572 | 8/7 17:35 起跑 31h 无异常，挂了自动拉起 5011/5001 |
| 3389 RDP / 22 SSH / 5985 WinRM | ✗ | **唯一缺口：没有远程执行命令的通道** |
| `C$` / `ADMIN$` | ✗ | 无管理员凭据，PsExec 与远程注册表暂不可用 |

**结论：问题不是"服务器只有白天能用"，服务器一直在线。问题是你只能读写文件、不能执行命令。**

- 本机有本地代理 `127.0.0.1:7897`，GitHub 直连通。服务器出网方式仍待确认（夜间是否稳定）。
- 本机已具备：git 2.54、node v22、OpenSSH 客户端、ZeroTier。缺失：Docker、n8n、nssm、psexec。
- **iNode 客户端已确认安装**（SSL VPN，已连，网关 `113.204.234.214`，账号 `xiaomao`，无域）。**不能当小叽出网梯子**，详见 §4。

## 1. 远程可达 —— 已解决，无需新建

**ZeroTier 已经在跑，且已验证可达。这一层不用做任何事。**

> 2026-08-09 晚用户决定：ZeroTier 稳定性**先观察、暂不加固**。因关键链路（任务/审批/部署）已解耦到公网 GitHub + 企微 `:5001`，ZeroTier 即便波动也不影响流水线；自建 Moon / WireGuard 迁移降级为可选，等观察后再定。

抽拉式分层（逐层降级，任一层可用即可救火）：

| 层 | 手段 | 用途 | 依赖 | 状态 |
| --- | --- | --- | --- | --- |
| ① | ZeroTier + **OpenSSH** | 远程执行命令，**主力** | ZeroTier | 待装 sshd |
| ② | ZeroTier + **RDP** | 图形界面兜底（nssm 配置、事件查看器） | ZeroTier | 待启用 |
| ③ | **RustDesk / 向日葵** | ZeroTier 服务本身挂掉时 | 服务器能出网 | 待评估 |
| ④ | **智能插座** | 系统完全无响应，硬重启 | 电力 | BIOS 已就绪 |
| 旁路 | **iNode SSL VPN** | 白天办公的独立备用路径 | 需数科部授权 | 可选，优先级最低 |

### 1.1 关键澄清：3389 不需要找数科部开

**要开的 3389 在服务器自己的 Windows 防火墙上，不在公司防火墙上。**

原理：ZeroTier 在两端各建一块**虚拟网卡**。RDP 流量先被 ZeroTier 加密封装，再以普通 UDP 发出——公司防火墙只看到"一条加密 UDP 流量"，无从识别其中是 RDP，**它没得可拦，也就没得可开**。

证据：445 / 135 这类通常被严管的端口都已通，说明 ZeroTier 二层网络完全打通；3389 不通纯粹是服务器没启用远程桌面。

**→ 你自己就能搞定，不必走数科部审批。**

### 1.2 ⚠️ RDP 会话抢占（必须先想清楚）

Win10/11 客户端版**只允许一个活动会话**：
- 你 RDP 连入 → 服务器物理桌面被锁定
- 你断开 → 会话保留，但桌面维持锁屏

**如果小叽的自动化依赖图形界面**（Outlook COM 收发邮件、Excel COM 处理报表），锁屏可能直接让它跑不动。

规避：
- 断开时用 `tscon 1 /dest:console` 把会话推回物理控制台，保持不锁屏
- 或把依赖 GUI 的任务改造成无头方式（IMAP/SMTP 代替 Outlook COM、openpyxl 代替 Excel COM）
- **根本对策：优先用 SSH。** SSH 是无头的，不抢占桌面会话，小叽照跑不误

**→ 待确认：小叽 MailBots 是 Outlook COM 还是纯 IMAP/SMTP？**

### 1.3 ⚠️ 空密码账号禁止远程登录

Windows 默认策略 `LimitBlankPasswordUse=1`：**空密码账号只允许本地登录，禁止一切网络/远程登录**。若服务器账号真是空密码，3389 开了也进不去，SSH 密码登录同样被拒。

**正解**：给账号设密码 → 配置自动登录，两者兼得。
```powershell
# 方式一：netplwiz → 取消勾选「要使用本计算机，用户必须输入用户名和密码」
# 方式二（推荐，微软官方工具，密码 LSA 加密存储，比注册表明文安全）：
#   下载 Sysinternals Autologon.exe → 填账号/密码 → Enable
```
效果：开机仍直达桌面，但 RDP / SSH 可用了，物理安全也从"零门槛"提升到"至少有密码"。

**→ 待确认：当前是真空密码，还是已有密码+自动登录？**

## 2. 服务器侧远程操作准备（连上服务器后做）
### 2.1 OpenSSH Server（推荐，比 RDP 轻、可脚本化）
```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH' -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22
# 公钥登录：本机 ssh-keygen 公钥写入服务器 C:\ProgramData\ssh\administrators_authorized_keys
```
### 2.2 WinRM（PowerShell 远程）
```powershell
Enable-PSRemoting -Force
# 跨网段/非域时把本机加入信任主机（仅内网/信任网络）
Set-Item WSMan:\localhost\Client\TrustedHosts -Value '10.0.199.74' -Concatenate -Force
```
用法：`Invoke-Command -ComputerName <IP> -ScriptBlock { Get-Service XiaojiRunner }`
### 2.3 远程桌面 RDP（GUI 兜底）
前置：账号必须**有密码**（见 §1.3），否则 Windows 直接拒绝远程登录。
```powershell
# 启用 RDP
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
# 只对 ZeroTier 网卡放行 3389（比全局放行安全得多）
New-NetFirewallRule -DisplayName 'RDP over ZeroTier' -Direction Inbound -Protocol TCP `
  -LocalPort 3389 -RemoteAddress 10.0.199.0/24 -Action Allow
```
用于 nssm 图形配置、事件查看器、Clash 界面等。**注意 §1.2 的会话抢占问题**，断开前执行：
```powershell
tscon 1 /dest:console   # 把会话推回物理控制台，避免锁屏打断 GUI 自动化
```
### 2.4 PsExec（本机准备，进程级强控）
本机下载 PsTools → `C:\Tools\PsTools\` 加入 PATH。当 SSH/WinRM 都异常时：`psexec \\<IP> -u <admin> cmd`
### 2.5 nssm（服务器侧，崩溃自动拉起）
服务器下载 nssm → `C:\Tools\nssm\`。把常驻进程注册成服务：
```text
nssm install XiaojiRunner "C:\Python\python.exe" "C:\yxo_app_dev\run_xiaoji.py"
nssm set XiaojiRunner AppExit Default Restart
nssm set XiaojiRunner Start SERVICE_AUTO_START
```
同理注册 `n8n`（若不用 Docker）、`opencode` 守护。

## 3. 服务器「梯子」—— 自主出网（小叽能自己 push）

**已定：Clash Verge。但生产环境要拆开用。**

- 问题：服务器现在可能靠办公室出口/某机器代理出网；那台机器夜间一关，服务器也断 GitHub → 小叽没法 commit/push，活全堆到第二天。

### 3.1 分工：本机 GUI 调规则，服务器跑内核

| 位置 | 装什么 | 为什么 |
| --- | --- | --- |
| 本机 | **Clash Verge Rev**（GUI） | 设置丰富，方便可视化调分流规则 |
| 服务器 | **mihomo.exe 内核**（无 GUI）+ nssm | Verge 是 Electron GUI 程序，nssm 服务化别扭；且 GUI 跑在用户会话里，会受 RDP 会话抢占影响。内核纯配置驱动，最稳 |

流程：本机 GUI 调好规则 → 导出 `config.yaml` → 拷到服务器喂给 mihomo。两边规则一致，服务器端无 GUI 依赖。

> 注意用 **Clash Verge Rev**（社区维护版），原版 clash-verge 仓库已归档停更。

### 3.2 ⚠️ 服务器上绝对不要开 TUN 模式 / 系统代理

TUN 模式和系统代理会**接管整机全部流量**，把内网业务（`10.0.199.0/24`、SMB 445、业务端口 5011/5001）也一起卷进代理 —— **生产会炸**。

正确姿势：**规则模式 + 只给 git/opencode 显式设环境变量**。分流规则示例：
```yaml
rules:
  - DOMAIN-SUFFIX,github.com,PROXY
  - DOMAIN-SUFFIX,githubusercontent.com,PROXY
  - DOMAIN-SUFFIX,googleapis.com,PROXY      # LLM API
  - IP-CIDR,10.0.0.0/8,DIRECT               # 内网一律直连
  - MATCH,DIRECT                            # 兜底直连，不是兜底代理
```

### 3.3 让 git/工具走代理
```powershell
git config --global http.proxy http://127.0.0.1:<代理端口>
git config --global https.proxy http://127.0.0.1:<代理端口>
# 或设系统环境变量让 opencode/n8n 也走
[Environment]::SetEnvironmentVariable('HTTP_PROXY','http://127.0.0.1:<端口>','Machine')
[Environment]::SetEnvironmentVariable('HTTPS_PROXY','http://127.0.0.1:<端口>','Machine')
```
- **无人时段提交**：服务器 git 用 PAT（不是 OAuth，避免跨域超时，历史踩过坑）+ 配 `user.name/user.email` + `credential.helper store`，push 才不用你每次手动叫。
- 验证（服务器上）：`git ls-remote https://github.com/roke8x-max/yxo-app` 通；夜间小叽能自推。
- 合规提醒：梯子是网络出口，选公司允许的代理/专线，别用来绕公司安全策略。

## 4. iNode SSL VPN：被低估的「白天备用通道」

**事实（截图读出 + 用户原话）**
- 连接类型：**SSL VPN**（按需代理回公司内网，**不是**全流量接管）
- 网关：`113.204.234.214`（公网入口 → 公司 VPN 集中器）
- 当前用户：`xiaomao`（毛骁洋简称）
- 域：无（不走 AD 认证，账号在公司本地库）
- 安全状态：未检查（iNode EAD 准入未跑或被跳过）
- 用户原话：「反代理一直开着，直连就行」
- 数科部给它的定位：让员工**安全访问订舱数据管理平台**等公司内网业务系统

**对方案的真实价值**

| 维度 | iNode SSL VPN | 我们的真实需求（夜间小叽出网） |
|------|--------------|--------------------------------|
| 流量方向 | 本机 → 公网 → 公司内网 | 服务器 → 公网（GitHub/LLM API） |
| 目的 | 让**人**访问公司资源 | 让**服务器**出公网 |
| 合规边界 | 给个人账号 "xiaomao" 访问业务系统 | 让公司机器 24h 出公网 |
| 协议设计 | 反向代理（按需） | 正向代理（出网） |
| 接管面 | 透明代理，不接管所有流量 | 需配置规则覆盖特定域名 |

**核心结论**：**iNode 方向反了，不能当小叽的梯子**。即便技术上能"借"这条通道，让小叽用你的个人账号 24h 在线出公网也违规（数科部会审计流量，"xiaomao 凌晨 3 点访问 GitHub" 很容易被叫停）。

**iNode 在方案里真正的位置：白天办公时的「第二条远程通道」**

白天你用 ZeroTier（或 iNode 单独走公司内网）回到服务器所在网段，理论上能 RDP 进服务器——但**这要求数科部在防火墙给你开放 `113.204.234.214 → 10.0.199.184:3389`**，这通常需要走正式的"远程办公维护"申请。

**它对方案真正的贡献**：补一个「即便 ZeroTier 挂了、白天办公时段仍能进服务器」的兜底。

**落地（待你拍板）**
- [ ] **白天备用通道（推荐上）**：走数科部流程，开「SSL VPN → 服务器 RDP/SSH」的权限。ZeroTier + iNode 双通道，白天任意一条能用就行。
- [ ] **夜间小叽出网（不要走 iNode）**：必须用 nssm 把「服务器本机的代理客户端」服务化、规则模式只放 GitHub。这条通道属于"公司机器出公网"，不归 iNode 管，也不该走 iNode 账号。

**为什么之前容易误以为"iNode 能给小叽用"**
- 名字里带"VPN"，听起来像"任何公司网络出口都能用"
- 实际它是"反代理"：把**特定流量反向代理回公司**，方向是"用户→公司"
- 服务器要出公网，得**在服务器侧自己跑代理客户端**，这才是 nssm 服务化的对象（见 §3）

---

## 5. 唤醒与恢复（小叽卡了/崩了，你能远程救）

**好消息：BIOS 已设「通电自动开机」，WoL 完全不需要搞了。** 智能插座 = 远程硬重启按钮。

**已有的自愈能力（别重复造轮子）**：服务器上 `watchdog.py` 已在跑（计划任务 `YXO_Watchdog`，pid 15572），每 30 秒探测 `5011` / `5001`，挂了自动 `subprocess.Popen` 拉起，日志滚动写 `watchdog.log`。8/7 17:35 起 31 小时无异常。
→ **扩展它比新建 nssm 服务更省事**：把 opencode / mihomo / n8n 加进 `SERVICES` 列表即可。

- **常态自愈**：现有 watchdog + nssm `AppExit Default Restart`；关键服务设 `SERVICE_AUTO_START`。
- **断电重启**：**智能插座**（米家/TP-Link Kasa）+ BIOS 通电自启 → 远程 APP 断电/上电即重启。
  - ⚠️ 选插座注意功率匹配（看服务器电源额定功率 vs 插座 10A/2200W 上限）。

### 5.1 ⚠️ 硬断电会损坏 yxo.db

SQLite 正在写入时被拔电，**生产库可能损坏**——`yxo.db` 里是客户名、收发货人、运价、提单号。

- **必做**：开 WAL 模式，断电恢复能力强很多
  ```sql
  PRAGMA journal_mode=WAL;
  ```
- **铁律**：智能插座是**最后手段**，优先走 SSH 软重启（这也是把 SSH 排在第 1 层的原因）
- 现有 `deploy.ps1` 只在部署时备份 `yxo.db`，日常应另加定时备份到 `D:\YXO_DATA\backups\`

### 5.2 救火清单（发现小叽没反应时，按序执行）
1. `ssh xiaoji@10.0.199.184` → `Restart-Service <服务名>` 或重启 watchdog
2. SSH 不通 → RDP `mstsc /v:10.0.199.184`（记得断开前 `tscon 1 /dest:console`）
3. 都不通 → 查本机 ZeroTier 状态；再试 RustDesk
4. 系统完全无响应 → 智能插座断电 → 等 10 秒 → 上电（BIOS 自启）
5. 部署/数据异常 → `rollback.ps1`（只回代码，数据库需人工确认）

### 5.3 物理安全提醒
免密进桌面意味着任何人走到机器前就能直接拿走全部客户数据。至少做到「设密码 + 自动登录」（§1.3），有条件再考虑 BitLocker。

## 6. 执行顺序（已按 8/9 实测事实重排）
① 账号设密码 + 配自动登录 → ② 服务器 OpenSSH（主力）→ ③ RDP + 仅 ZeroTier 网段放行 3389 → ④ yxo.db 开 WAL → ⑤ 服务器梯子（mihomo + nssm）→ ⑥ 扩展 watchdog 纳管新进程 → ⑦ 智能插座 → ⑧ iNode 数科部备案（可选，最低优先级）

## 7. 已定 & 待确认

**已定**
- 远程可达：ZeroTier 为主（已可用），不加 Tailscale
- 3389：**不需要数科部**，服务器本机防火墙即可
- 梯子：Clash Verge（本机 GUI 调规则 → 服务器 mihomo 内核跑）
- 唤醒：智能插座，WoL 不做
- 分层：抽拉式 SSH → RDP → RustDesk → 插座

**待确认**
- [ ] 小叽 MailBots 是 Outlook COM 还是纯 IMAP/SMTP？（决定 RDP 是否会打断自动化）
- [ ] 服务器账号是真空密码，还是已有密码+自动登录？
- [ ] 服务器现在到底怎么出网？（需 SSH 上去后 `curl -I https://github.com` 实测）
- [ ] iNode 备用通道要不要走数科部申请？（ZeroTier 已够用，可暂缓）
