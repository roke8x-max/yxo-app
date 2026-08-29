# YXO MailBots 生产环境部署手册

> 版本：v1.0 | 更新：2026-08-29 | 适用：阿里云 Windows Server 2019/2022
> 设计文档：`docs/superpowers/specs/2026-08-26-mailbots-refactor-design.md` (N4, 第10节)

---

## 1. 架构约定

| 项目 | 说明 |
|------|------|
| **运行模型** | Y：生产直跑 git 检出目录 `D:\YXO_DATA\yxo_app\mailbots` |
| **进程管理** | nssm (Non-Sucking Service Manager) + PID 文件锁防重复运行 |
| **触发方式** | mailbot_serve.py 单长驻进程，IDLE watch「草单运单号」文件夹 (4账号×1连接) |
| **短进程** | Tracing/Dsk/Atb 保持 15 分钟 Windows 任务计划触发，调用 core 公共层 |
| **配置来源** | 环境变量优先 → `$CONFIG_DIR\config.py` → `$CONFIG_DIR\accounts.json` |

---

## 2. 目录结构

```
D:\YXO_DATA\
├── yxo_app\mailbots\      # git 检出目录 (生产直跑)
├── venv\                  # Python 虚拟环境
├── config\                # 配置文件目录
│   ├── config.py          # 主配置 (自动生成)
│   ├── accounts.json      # 邮箱密码 (手工维护，600 权限)
│   └── accounts.json.example
├── logs\                  # 服务日志 (nssm stdout/stderr + mailbot_serve 内部日志)
├── data\                  # 数据库/状态文件
│   ├── yxo.db             # 主数据 (records, bot_config, dedup_global, ...)
│   ├── events.db          # 事件仓库 (waybill_ledger, forward_log, ...)
│   ├── eml_repo\          # .eml 落盘仓库
│   ├── .last_deploy       # 最近部署记录
│   └── YXO-MailBot.pid    # PID 锁文件
├── backups\               # 部署自动备份 (按时间戳)
└── tools\nssm.exe         # nssm 可执行文件
```

---

## 3. 前置要求

### 3.1 系统环境
- Windows Server 2019/2022 (阿里云 ECS)
- Python 3.11+ (已加入 PATH)
- Git for Windows (已加入 PATH)
- nssm 2.24+ (放置于 `D:\YXO_DATA\tools\nssm.exe`)

### 3.2 下载 nssm
```powershell
# 手工下载或使用脚本
Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile "nssm.zip"
Expand-Archive nssm.zip -DestinationPath D:\YXO_DATA\tools
Copy-Item D:\YXO_DATA\tools\nssm-2.24\win64\nssm.exe D:\YXO_DATA\tools\nssm.exe
```

### 3.3 邮箱规则 (前置 Gate，刀5)
> **必须由骁洋人工完成**：将企业邮箱规则中「运单号」+「运单草单」合并重定向到「草单运单号」文件夹。
> 部署前未完成会导致 IDLE watch 抓空邮件。

---

## 4. 部署流程

### 4.1 首次安装
```powershell
# 1. 以管理员身份打开 PowerShell
# 2. 执行部署脚本
cd D:\YXO_DATA\yxo_app\mailbots\scripts\deploy
.\Deploy-YXO-MailBots.ps1 -Action install

# 3. 脚本会提示后续手工步骤：
#    - 编辑 D:\YXO_DATA\config\accounts.json 填入真实邮箱密码
#    - 设置环境变量 WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET
#    - 启动服务: nssm start YXO-MailBot
```

### 4.2 配置环境变量 (永久生效)
```powershell
# 系统环境变量 (需管理员)
[Environment]::SetEnvironmentVariable('WECOM_CORP_ID', 'xxxxxxxx', 'Machine')
[Environment]::SetEnvironmentVariable('WECOM_AGENT_ID', '1000001', 'Machine')
[Environment]::SetEnvironmentVariable('WECOM_SECRET', 'xxxxxxxx', 'Machine')
[Environment]::SetEnvironmentVariable('IMAP_SERVER', 'imap.qiye.aliyun.com', 'Machine')
[Environment]::SetEnvironmentVariable('SMTP_SERVER', 'smtp.qiye.aliyun.com', 'Machine')
# 如需覆盖默认账号，也可设置 YXO_MAIL_ACCOUNT_1 / YXO_MAIL_PASSWORD_1 等

# 生效需重启 PowerShell 或重启服务
nssm restart YXO-MailBot
```

### 4.3 编辑 accounts.json
```powershell
# 复制模板
cp D:\YXO_DATA\config\accounts.json.example D:\YXO_DATA\config\accounts.json

# 编辑 (记事本或 VS Code)
notepad D:\YXO_DATA\config\accounts.json
```
内容示例：
```json
{
  "maoxiaoyang@cqtransit.com": "actual_password_1",
  "yangyawen@cqtransit.com": "actual_password_2",
  "fengqian@cqtransit.com": "actual_password_3",
  "hanwenhao@cqtransit.com": "actual_password_4"
}
```
> **安全**：文件权限应限制为仅管理员可读。

### 4.4 启动服务
```powershell
nssm start YXO-MailBot
# 或
Start-Service YXO-MailBot
```

### 4.5 验证部署
```powershell
# 查看服务状态
.\Deploy-YXO-MailBots.ps1 -Action status

# 实时查看日志
Get-Content D:\YXO_DATA\logs\service_out.log -Wait
Get-Content D:\YXO_DATA\logs\service_err.log -Wait

# 检查 PID 锁
type D:\YXO_DATA\data\YXO-MailBot.pid
```

---

## 5. 更新部署

```powershell
cd D:\YXO_DATA\yxo_app\mailbots\scripts\deploy
.\Deploy-YXO-MailBots.ps1 -Action update
```
脚本会自动：
1. 备份当前版本到 `D:\YXO_DATA\backups\mailbots_YYYYMMDD_HHMMSS`
2. `git pull` 最新代码
3. 更新 Python 依赖
4. 运行单元测试
5. 重启 nssm 服务

---

## 6. 回滚操作

```powershell
# 交互式选择
.\Rollback-YXO-MailBots.ps1

# 或指定版本
.\Rollback-YXO-MailBots.ps1 -Version mailbots_20260829_143000

# 仅查看可用备份
.\Rollback-YXO-MailBots.ps1 -ListOnly
```

回滚流程：
1. 停止服务
2. 创建紧急备份 (`emergency_YYYYMMDD_HHMMSS`)
3. 替换代码目录为指定备份
4. 启动服务并验证

---

## 7. 常用运维命令

| 操作 | 命令 |
|------|------|
| 查看服务状态 | `Get-Service YXO-MailBot` |
| 启动服务 | `nssm start YXO-MailBot` |
| 停止服务 | `nssm stop YXO-MailBot` |
| 重启服务 | `nssm restart YXO-MailBot` |
| 查看服务日志 | `Get-Content D:\YXO_DATA\logs\service_out.log -Wait` |
| 查看错误日志 | `Get-Content D:\YXO_DATA\logs\service_err.log -Wait` |
| 查看业务日志 | `Get-Content D:\YXO_DATA\logs\mailbot_$(Get-Date -Format 'yyyy-MM-dd').log -Wait` |
| 手工触发一次扫描 | `cd D:\YXO_DATA\yxo_app\mailbots && D:\YXO_DATA\venv\Scripts\python.exe -m mailbots.mailbot_serve --once` |
| 检查数据库连接 | `D:\YXO_DATA\venv\Scripts\python.exe -c "from core import paths; import sqlite3; c=sqlite3.connect(paths.yxo_db_path()); print(c.execute('select count(*) from records').fetchone())"` |

---

## 8. 故障排查

### 8.1 服务无法启动
1. 检查 nssm 错误日志：`D:\YXO_DATA\logs\service_err.log`
2. 检查 PID 文件是否残留：`D:\YXO_DATA\data\YXO-MailBot.pid` (如有残留进程已死，删除即可)
3. 检查配置文件语法：`D:\YXO_DATA\venv\Scripts\python.exe -m py_compile D:\YXO_DATA\config\config.py`
4. 检查数据库是否存在：`D:\YXO_DATA\data\yxo.db`

### 8.2 IDLE 连接异常
- 日志会显示指数退避重连 (10s, 20s, 40s... max 300s)
- 检查阿里云企业邮箱 IMAP 是否开启、防火墙/安全组 993 端口
- 可暂时切换轮询模式：在 config.py 设置 `USE_IDLE = False`

### 8.3 邮件不触发转发
1. 确认邮箱规则已生效，邮件落入「草单运单号」文件夹
2. 检查去重表：`sqlite3 D:\YXO_DATA\data\yxo.db "select * from dedup_global where source='draft_log' order by claimed_at desc limit 5"`
3. 检查路由配置：`sqlite3 D:\YXO_DATA\data\yxo.db "select * from bot_config where bot in ('draft','waybill')"`
4. 检查企微通知：日志中搜索 `notify_realtime` / `notify_alarm`

### 8.4 运踪/DSK/ATB 短进程异常
- 由 Windows 任务计划每 15 分钟触发
- 检查任务计划库：`taskschd.msc` → YXO-Tracing / YXO-Dsk / YXO-Atb
- 手工触发：`D:\YXO_DATA\venv\Scripts\python.exe D:\YXO_DATA\yxo_app\mailbots\Tracing_Robot_IMAP.py`

---

## 9. 安全加固清单

- [ ] `D:\YXO_DATA\config\accounts.json` 仅管理员可读 (icacls /inheritance:r /grant:r "Administrators:(R)")
- [ ] 企微 Secret 仅通过环境变量注入，不写入代码/配置文件
- [ ] 邮箱密码不提交 git (`.gitignore` 已包含 `accounts.json` 和 `config_local.py`)
- [ ] nssm 服务以普通用户运行 (非 SYSTEM)，已配置 `NoNewPrivileges=yes` 等效限制
- [ ] 防火墙仅开放必要出站：IMAP 993, SMTP 465, 企微 API 443, 邮戳 API 5011

---

## 10. 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-08-29 | 初版：nssm 部署 + PID 锁 + 回滚脚本 + 运维手册 |

---

## 附录：核心文件路径速查

| 文件 | 路径 |
|------|------|
| 入口脚本 | `D:\YXO_DATA\yxo_app\mailbots\mailbots\mailbot_serve.py` |
| 草单处理器 | `D:\YXO_DATA\yxo_app\mailbots\mailbots\processors\draft.py` |
| 运单处理器 | `D:\YXO_DATA\yxo_app\mailbots\mailbots\processors\waybill.py` |
| 运踪机器人 | `D:\YXO_DATA\yxo_app\mailbots\Tracing_Robot_IMAP.py` |
| DSK 机器人 | `D:\YXO_DATA\yxo_app\mailbots\Dsk_Robot.py` |
| ATB 机器人 | `D:\YXO_DATA\yxo_app\mailbots\Atb_Robot.py` |
| 核心层 | `D:\YXO_DATA\yxo_app\mailbots\mailbots\core\` |
| 测试集 | `D:\YXO_DATA\yxo_app\mailbots\mailbots\tests\` |
| 部署脚本 | `D:\YXO_DATA\yxo_app\mailbots\scripts\deploy\` |