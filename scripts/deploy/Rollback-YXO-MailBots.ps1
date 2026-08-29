<# 
.SYNOPSIS
    YXO MailBots 回滚脚本 (Windows 生产环境)
    
.DESCRIPTION
    回滚到指定的备份版本或上一个部署版本
    
.PARAMETER Version
    指定要回滚的备份目录名 (如: mailbots_20260829_143000)
    不指定则列出可用备份供选择
    
.PARAMETER ListOnly
    仅列出可用备份，不执行回滚
    
.EXAMPLE
    .\Rollback-YXO-MailBots.ps1 -Version mailbots_20260829_143000
    .\Rollback-YXO-MailBots.ps1 -ListOnly
    .\Rollback-YXO-MailBots.ps1
#>

param(
    [string]$Version = '',
    [switch]$ListOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$InstallRoot = 'D:\YXO_DATA'
$BACKUP_DIR  = Join-Path $InstallRoot 'backups'
$APP_DIR     = Join-Path $InstallRoot 'yxo_app\mailbots'
$SERVICE_NAME = 'YXO-MailBot'
$NSSM_PATH    = Join-Path $InstallRoot 'tools\nssm.exe'

function Write-Info { param([string]$msg) Write-Host "[INFO] $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Error { param([string]$msg) Write-Host "[ERROR] $msg" -ForegroundColor Red }

# 检查管理员权限
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "请以管理员身份运行 PowerShell"
    exit 1
}

# 列出可用备份
function List-Backups {
    Write-Info "可用备份列表:"
    $backups = Get-ChildItem $BACKUP_DIR -Directory -ErrorAction SilentlyContinue | 
               Sort-Object LastWriteTime -Descending
    if (-not $backups) {
        Write-Warn "无可用备份"
        return
    }
    $backups | Select-Object -First 20 | Format-Table Name, LastWriteTime, @{Name='Size(MB)';Expression={"{0:N1}" -f ($_.GetDirectorySize()/1MB)}} -AutoSize
}

# 扩展 DirectoryInfo 添加 GetDirectorySize 方法
Add-Type -TypeDefinition @"
using System.IO;
public static class DirExt {
    public static long GetDirectorySize(this DirectoryInfo dir) {
        long size = 0;
        try {
            foreach (FileInfo f in dir.GetFiles("*", SearchOption.AllDirectories)) {
                size += f.Length;
            }
        } catch {}
        return size;
    }
}
"@

if ($ListOnly -or -not $Version) {
    List-Backups
    if (-not $ListOnly) {
        $Version = Read-Host "请输入要回滚的备份目录名 (留空取消)"
        if (-not $Version) { Write-Warn "已取消"; exit 0 }
    } else { exit 0 }
}

$backupPath = Join-Path $BACKUP_DIR $Version
if (-not (Test-Path $backupPath)) {
    Write-Error "备份不存在: $backupPath"
    List-Backups
    exit 1
}

Write-Info "准备回滚到: $Version"
Write-Warn "这将停止服务并替换当前代码目录，确认继续? (y/N)"
$confirm = Read-Host
if ($confirm -notmatch '^[yY]$') { Write-Warn "已取消"; exit 0 }

# 停止服务
if (Get-Service $SERVICE_NAME -ErrorAction SilentlyContinue) {
    Write-Info "停止服务 $SERVICE_NAME..."
    & $NSSM_PATH stop $SERVICE_NAME | Out-Null
    Start-Sleep 3
}

# 备份当前版本 (以防万一)
$emergencyBackup = Join-Path $BACKUP_DIR "emergency_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
Write-Info "创建紧急备份: $emergencyBackup"
Copy-Item -Path $APP_DIR -Destination $emergencyBackup -Recurse -Force

# 恢复目标版本
Write-Info "恢复代码..."
Remove-Item -Path (Join-Path $APP_DIR '*') -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item -Path (Join-Path $backupPath '*') -Destination $APP_DIR -Recurse -Force

# 重启服务
Write-Info "启动服务..."
& $NSSM_PATH start $SERVICE_NAME | Out-Null
Write-Info "回滚完成"