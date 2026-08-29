<# 
.SYNOPSIS
    YXO MailBots 生产环境部署脚本 (Windows/阿里云)
    
.DESCRIPTION
    部署 YXO MailBots 到 Windows 生产环境。
    运行模型：生产直跑 git 检出目录 D:\YXO_DATA\yxo_app\mailbots
    使用 nssm 管理服务 + PID 锁防重复运行
    
.PARAMETER Action
    install  - 全新安装 (默认)
    update   - 更新代码并重启服务
    rollback - 回滚到指定版本
    status   - 查看服务状态
    nssm-install - 安装 nssm 服务
    
.PARAMETER Version
    回滚时指定的版本标签或提交哈希
    
.EXAMPLE
    .\Deploy-YXO-MailBots.ps1 -Action install
    .\Deploy-YXO-MailBots.ps1 -Action update
    .\Deploy-YXO-MailBots.ps1 -Action rollback -Version v1.2.3
    .\Deploy-YXO-MailBots.ps1 -Action nssm-install
#>

param(
    [ValidateSet('install','update','rollback','status','nssm-install')]
    [string]$Action = 'install',
    
    [string]$Version = '',
    
    [string]$RepoUrl = 'https://github.com/roke8x-max/yxo-app.git',
    
    [string]$Branch = 'main',
    
    [string]$InstallRoot = 'D:\YXO_DATA',
    
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ============================================================
# 配置常量
# ============================================================
$APP_NAME       = 'yxo-mailbots'
$APP_DIR        = Join-Path $InstallRoot 'yxo_app\mailbots'
$VENV_DIR       = Join-Path $InstallRoot 'venv'
$CONFIG_DIR     = Join-Path $InstallRoot 'config'
$LOG_DIR        = Join-Path $InstallRoot 'logs'
$DATA_DIR       = Join-Path $InstallRoot 'data'
$NSSM_PATH      = Join-Path $InstallRoot 'tools\nssm.exe'
$BACKUP_DIR     = Join-Path $InstallRoot 'backups'
$SERVICE_NAME   = 'YXO-MailBot'
$PID_FILE       = Join-Path $DATA_DIR "$SERVICE_NAME.pid"
$GIT_EXE        = 'git.exe'
$PYTHON_EXE     = 'python.exe'

# 颜色输出
function Write-Info { param([string]$msg) Write-Host "[INFO] $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Error { param([string]$msg) Write-Host "[ERROR] $msg" -ForegroundColor Red }

# ============================================================
# 工具函数
# ============================================================
function Check-Admin {
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "请以管理员身份运行 PowerShell"
        exit 1
    }
}

function Check-Dependencies {
    Write-Info "检查依赖..."
    $missing = @()
    if (-not (Get-Command $GIT_EXE -ErrorAction SilentlyContinue)) { $missing += 'git' }
    if (-not (Get-Command $PYTHON_EXE -ErrorAction SilentlyContinue)) { $missing += 'python' }
    if (-not (Test-Path $NSSM_PATH)) { $missing += "nssm (需放置于 $NSSM_PATH)" }
    
    if ($missing.Count -gt 0) {
        Write-Error "缺少依赖: $($missing -join ', ')"
        exit 1
    }
    Write-Info "依赖检查通过"
}

function Ensure-Directories {
    Write-Info "创建目录结构..."
    $dirs = $APP_DIR, $CONFIG_DIR, $LOG_DIR, $DATA_DIR, $BACKUP_DIR, (Split-Path $NSSM_PATH)
    foreach ($d in $dirs) {
        if (-not (Test-Path $d)) {
            New-Item -ItemType Directory -Path $d -Force | Out-Null
            Write-Info "  创建: $d"
        }
    }
}

function Setup-Venv {
    Write-Info "检查/创建虚拟环境..."
    if (-not (Test-Path (Join-Path $VENV_DIR 'Scripts\python.exe'))) {
        Write-Info "创建虚拟环境: $VENV_DIR"
        & $PYTHON_EXE -m venv $VENV_DIR
    }
    # 升级 pip
    & (Join-Path $VENV_DIR 'Scripts\pip.exe') install --upgrade pip -q
}

function Deploy-Code {
    Write-Info "部署代码..."
    if (Test-Path (Join-Path $APP_DIR '.git')) {
        Write-Info "  更新现有仓库..."
        Set-Location $APP_DIR
        & $GIT_EXE fetch --all --prune
        & $GIT_EXE checkout $Branch
        & $GIT_EXE pull origin $Branch
    } else {
        Write-Info "  克隆仓库..."
        & $GIT_EXE clone --branch $Branch --depth 1 $RepoUrl $APP_DIR
    }
    # 确保子目录权限
    icacls $APP_DIR /grant "Users:(OI)(CI)RX" /T /C /Q >$null 2>&1
}

function Install-Dependencies {
    Write-Info "安装 Python 依赖..."
    $pip = Join-Path $VENV_DIR 'Scripts\pip.exe'
    & $pip install -r (Join-Path $APP_DIR 'requirements.txt') -q
}

function Create-Config {
    Write-Info "创建/更新配置文件..."
    
    # 确保配置目录存在
    if (-not (Test-Path $CONFIG_DIR)) { New-Item -ItemType Directory -Path $CONFIG_DIR -Force | Out-Null }
    
    $configPath = Join-Path $CONFIG_DIR 'config.py'
    if (-not (Test-Path $configPath) -or $Force) {
        @"
# -*- coding: utf-8 -*-
\"\"\"YXO MailBots 生产环境配置 (Windows)\"\"\"
import os

# ============================================================
# 基础路径 (运行模型 Y: 生产直跑 git 检出目录)
# ============================================================
YXO_ROOT     = os.environ.get('YXO_ROOT', r'$InstallRoot\\yxo_app')
DATA_DIR     = os.environ.get('YXO_DATA_DIR', r'$DATA_DIR')
CONFIG_DIR   = os.environ.get('YXO_CONFIG_DIR', r'$CONFIG_DIR')
LOG_DIR      = os.environ.get('YXO_LOG_DIR', r'$LOG_DIR')

# 数据库路径
YXO_DB_PATH     = os.path.join(DATA_DIR, 'yxo.db')
EVENTS_DB_PATH  = os.path.join(DATA_DIR, 'events.db')
EML_REPO_DIR    = os.path.join(DATA_DIR, 'eml_repo')

# IMAP/SMTP 配置
IMAP_SERVER = os.environ.get('IMAP_SERVER', 'imap.qiye.aliyun.com')
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.qiye.aliyun.com')
IMAP_PORT   = int(os.environ.get('IMAP_PORT', '993'))
SMTP_PORT   = int(os.environ.get('SMTP_PORT', '465'))

# 账号配置 (从环境变量或 accounts.json 加载)
def load_accounts():
    accounts = {}
    # 环境变量优先
    for i in range(1, 10):
        email = os.environ.get(f'YXO_MAIL_ACCOUNT_{i}')
        pwd   = os.environ.get(f'YXO_MAIL_PASSWORD_{i}')
        if email and pwd:
            accounts[email] = pwd
    # 回退到配置文件
    config_file = os.path.join(CONFIG_DIR, 'accounts.json')
    if os.path.exists(config_file):
        import json
        with open(config_file, encoding='utf-8') as f:
            accounts.update(json.load(f))
    return accounts

ACCOUNTS = load_accounts()

# SMTP 端点
def smtp_endpoint():
    return SMTP_SERVER, SMTP_PORT

# 企微配置
WECOM_CORP_ID  = os.environ.get('WECOM_CORP_ID')
WECOM_AGENT_ID = os.environ.get('WECOM_AGENT_ID')
WECOM_SECRET   = os.environ.get('WECOM_SECRET')

# 邮戳 API
STAMP_API   = 'http://127.0.0.1:5011/api/stamp'
STAMP_TOKEN = os.environ.get('STAMP_TOKEN')

# nssm 服务配置
NSSM_SERVICE_NAME = '$SERVICE_NAME'
PID_FILE          = r'$PID_FILE'
"@ | Set-Content -Path $configPath -Encoding UTF8
        Write-Info "  已生成: $configPath"
    } else {
        Write-Warn "  配置文件已存在，跳过 (使用 -Force 强制覆盖): $configPath"
    }
    
    # accounts.json 模板
    $accountsTemplate = Join-Path $CONFIG_DIR 'accounts.json.example'
    if (-not (Test-Path $accountsTemplate)) {
        @"
{
  "maoxiaoyang@cqtransit.com": "your_password_here",
  "yangyawen@cqtransit.com": "your_password_here",
  "fengqian@cqtransit.com": "your_password_here",
  "hanwenhao@cqtransit.com": "your_password_here"
}
"@ | Set-Content -Path $accountsTemplate -Encoding UTF8
        Write-Info "  已生成: $accountsTemplate (请复制为 accounts.json 并填入真实密码)"
    }
}

function Install-NssmService {
    Write-Info "安装 nssm 服务: $SERVICE_NAME"
    
    if (-not (Test-Path $NSSM_PATH)) {
        Write-Error "未找到 nssm.exe: $NSSM_PATH"
        Write-Warn "请从 https://nssm.cc/download 下载 nssm 并放置到 $NSSM_PATH"
        exit 1
    }
    
    $pythonExe = Join-Path $VENV_DIR 'Scripts\python.exe'
    $scriptPath = Join-Path $APP_DIR 'mailbots\mailbot_serve.py'
    
    # 停止并删除旧服务
    if (Get-Service $SERVICE_NAME -ErrorAction SilentlyContinue) {
        Write-Info "  停止现有服务..."
        & $NSSM_PATH stop $SERVICE_NAME | Out-Null
        Start-Sleep 2
        & $NSSM_PATH remove $SERVICE_NAME confirm | Out-Null
    }
    
    # 安装新服务
    & $NSSM_PATH install $SERVICE_NAME $pythonExe "-m mailbots.mailbot_serve --live"
    & $NSSM_PATH set $SERVICE_NAME AppDirectory $APP_DIR
    & $NSSM_PATH set $SERVICE_NAME AppEnvironmentExtra "PYTHONPATH=$APP_DIR;YXO_ROOT=$InstallRoot\yxo_app;YXO_DATA_DIR=$DATA_DIR;YXO_CONFIG_DIR=$CONFIG_DIR;YXO_LOG_DIR=$LOG_DIR"
    & $NSSM_PATH set $SERVICE_NAME DisplayName "YXO MailBot Service"
    & $NSSM_PATH set $SERVICE_NAME Description "YXO MailBots 自动转发服务 (草单/运单号/运踪/DSK/ATB)"
    & $NSSM_PATH set $SERVICE_NAME Start SERVICE_AUTO_START
    & $NSSM_PATH set $SERVICE_NAME AppStdout (Join-Path $LOG_DIR 'service_out.log')
    & $NSSM_PATH set $SERVICE_NAME AppStderr (Join-Path $LOG_DIR 'service_err.log')
    & $NSSM_PATH set $SERVICE_NAME AppRotateFiles 1
    & $NSSM_PATH set $SERVICE_NAME AppRotateBytes 10485760  # 10MB
    
    Write-Info "  nssm 服务安装完成"
}

function Setup-PidLock {
    Write-Info "配置 PID 锁机制..."
    # mailbot_serve.py 内部已实现 PID 文件锁，此处仅确保目录存在
    if (-not (Test-Path (Split-Path $PID_FILE))) {
        New-Item -ItemType Directory -Path (Split-Path $PID_FILE) -Force | Out-Null
    }
}

function Initialize-Database {
    Write-Info "初始化数据库..."
    $pythonExe = Join-Path $VENV_DIR 'Scripts\python.exe'
    $initScript = @"
import sys
sys.path.insert(0, r'$APP_DIR')
from core import events_store
conn = events_store.connect(events_store.events_db_path())
events_store.ensure_schema(conn)
print('数据库初始化完成')
"@
    & $pythonExe -c $initScript
}

function Run-Tests {
    Write-Info "运行测试..."
    Set-Location $APP_DIR
    $pythonExe = Join-Path $VENV_DIR 'Scripts\python.exe'
    & $pythonExe -m pytest mailbots/tests/unit -q --tb=short
}

function Backup-CurrentVersion {
    Write-Info "备份当前版本..."
    $timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $backupPath = Join-Path $BACKUP_DIR "mailbots_$timestamp"
    if (Test-Path $APP_DIR) {
        Copy-Item -Path $APP_DIR -Destination $backupPath -Recurse -Force
        Write-Info "  备份至: $backupPath"
    }
}

function Rollback-Version {
    param([string]$TargetVersion)
    
    if (-not $TargetVersion) {
        Write-Info "可用备份:"
        Get-ChildItem $BACKUP_DIR -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 10 | 
            Format-Table Name, LastWriteTime -AutoSize
        $TargetVersion = Read-Host "请输入要回滚的备份目录名"
    }
    
    $backupPath = Join-Path $BACKUP_DIR $TargetVersion
    if (-not (Test-Path $backupPath)) {
        Write-Error "备份不存在: $backupPath"
        exit 1
    }
    
    Write-Info "回滚到 $TargetVersion ..."
    
    # 停止服务
    if (Get-Service $SERVICE_NAME -ErrorAction SilentlyContinue) {
        Write-Info "  停止服务..."
        & $NSSM_PATH stop $SERVICE_NAME | Out-Null
        Start-Sleep 3
    }
    
    # 清空并恢复
    Remove-Item -Path (Join-Path $APP_DIR '*') -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item -Path (Join-Path $backupPath '*') -Destination $APP_DIR -Recurse -Force
    
    # 重启服务
    & $NSSM_PATH start $SERVICE_NAME | Out-Null
    Write-Info "回滚完成"
}

function Show-Status {
    Write-Info "=== 服务状态 ==="
    $svc = Get-Service $SERVICE_NAME -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Info "服务名称: $($svc.Name)"
        Write-Info "显示名称: $($svc.DisplayName)"
        Write-Info "状态: $($svc.Status)"
        Write-Info "启动类型: $($svc.StartType)"
    } else {
        Write-Warn "服务未安装: $SERVICE_NAME"
    }
    
    Write-Info "`n=== 进程信息 ==="
    $procs = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*$APP_DIR*" }
    if ($procs) {
        $procs | Format-Table Id, ProcessName, CPU, WS, StartTime -AutoSize
    } else {
        Write-Warn "未发现运行中的 Python 进程"
    }
    
    Write-Info "`n=== PID 文件 ==="
    if (Test-Path $PID_FILE) {
        $pid = Get-Content $PID_FILE
        Write-Info "PID 文件: $PID_FILE"
        Write-Info "PID 内容: $pid"
        if (Get-Process -Id $pid -ErrorAction SilentlyContinue) {
            Write-Info "进程存活: 是"
        } else {
            Write-Warn "进程存活: 否 (PID 文件可能残留)"
        }
    } else {
        Write-Warn "PID 文件不存在: $PID_FILE"
    }
    
    Write-Info "`n=== 最近部署 ==="
    $lastDeploy = Join-Path $DATA_DIR '.last_deploy'
    if (Test-Path $lastDeploy) {
        Get-Content $lastDeploy
    } else {
        Write-Warn "无部署记录"
    }
}

# ============================================================
# 主流程
# ============================================================
Write-Host "==========================================="
Write-Host "  YXO MailBots 部署脚本 (Windows 生产环境)"
Write-Host "  动作: $Action"
Write-Host "==========================================="

Check-Admin
Check-Dependencies
Ensure-Directories

switch ($Action) {
    'install' {
        Setup-Venv
        Deploy-Code
        Install-Dependencies
        Create-Config
        Install-NssmService
        Setup-PidLock
        Initialize-Database
        Run-Tests
        
        # 记录部署版本
        Set-Location $APP_DIR
        $commit = & $GIT_EXE rev-parse --short HEAD
        $date = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        "$date  $commit  $Branch" | Set-Content -Path (Join-Path $DATA_DIR '.last_deploy') -Encoding UTF8
        
        Write-Info "`n安装完成！"
        Write-Info "请执行以下操作:"
        Write-Info "  1. 编辑 $CONFIG_DIR\accounts.json 填入真实邮箱密码"
        Write-Info "  2. 设置环境变量 WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET"
        Write-Info "  3. 启动服务: nssm start $SERVICE_NAME"
        Write-Info "  4. 查看日志: Get-Content $LOG_DIR\service_out.log -Wait"
    }
    
    'update' {
        Backup-CurrentVersion
        Deploy-Code
        Install-Dependencies
        Initialize-Database
        Run-Tests
        
        # 记录部署版本
        Set-Location $APP_DIR
        $commit = & $GIT_EXE rev-parse --short HEAD
        $date = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        "$date  $commit  $Branch" | Set-Content -Path (Join-Path $DATA_DIR '.last_deploy') -Encoding UTF8
        
        Write-Info "重启服务..."
        & $NSSM_PATH restart $SERVICE_NAME | Out-Null
        Write-Info "更新完成"
    }
    
    'rollback' {
        Rollback-Version -TargetVersion $Version
    }
    
    'status' {
        Show-Status
    }
    
    'nssm-install' {
        Install-NssmService
        Setup-PidLock
        Write-Info "nssm 服务安装完成，请执行: nssm start $SERVICE_NAME"
    }
}

Write-Host "==========================================="
Write-Host "  部署脚本执行完毕"
Write-Host "==========================================="