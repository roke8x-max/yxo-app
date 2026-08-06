<#
    渝新欧订舱系统 —— 生产部署脚本（只在服务器 D 盘生产目录运行）

    用法：
        powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
        powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -DryRun

    它做什么（按顺序）：
        1. 确认在 main 分支、工作树干净（有人偷改过生产代码会当场报警）
        2. 备份数据库 + 本地配置到仓库外的 backups 目录
        3. 记下当前版本号，写进 .last_deploy，供回滚使用
        4. git pull --ff-only 拉取新代码（不会产生合并，拉不动就是有问题）
        5. 提示要不要装依赖、要不要重启服务

    出事了怎么办：
        powershell -ExecutionPolicy Bypass -File scripts\rollback.ps1
#>

param(
    [switch]$DryRun,
    [switch]$SkipBackup
)

$ErrorActionPreference = "Stop"

function Say($msg, $color = "White") { Write-Host $msg -ForegroundColor $color }
function Step($n, $msg) { Write-Host ""; Write-Host "[$n] $msg" -ForegroundColor Cyan }

$repoRoot = git rev-parse --show-toplevel 2>$null
if (-not $repoRoot) { Say "[失败] 当前目录不是 git 仓库" Red; exit 1 }
$repoRoot = $repoRoot -replace '/', '\'
Set-Location $repoRoot

Say ""
Say "=======================================" Cyan
Say " 渝新欧订舱系统  生产部署" Cyan
Say " 目录: $repoRoot" Cyan
if ($DryRun) { Say " 模式: 演习（不会真的改任何东西）" Yellow }
Say "=======================================" Cyan

# ---------- 1. 前置检查 ----------
Step 1 "前置检查"

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "main") {
    Say "  [失败] 当前在 $branch 分支，生产目录必须停在 main" Red
    Say "         执行: git checkout main" Yellow
    exit 1
}
Say "  分支 main  OK" Green

$dirty = git status --porcelain
if ($dirty) {
    Say "  [失败] 生产目录有未提交的改动，说明有人直接改了生产代码：" Red
    $dirty | ForEach-Object { Say "         $_" Red }
    Say ""
    Say "  这些改动不在 GitHub 上，直接部署会把它们覆盖掉。" Yellow
    Say "  处理办法（三选一）：" Yellow
    Say "    a. 改动有用   -> 复制出来，在开发环境重做一遍走 PR 流程" Yellow
    Say "    b. 改动没用   -> git checkout -- . 丢弃后重跑本脚本" Yellow
    Say "    c. 拿不准     -> 先别动，问骁洋" Yellow
    exit 1
}
Say "  工作树干净  OK" Green

$currentCommit = (git rev-parse HEAD).Trim()
Say ("  当前版本 " + $currentCommit.Substring(0,7)) Green

# ---------- 2. 备份 ----------
Step 2 "备份（出事就靠它）"

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupRoot = Join-Path (Split-Path $repoRoot -Parent) "backups"
$backupDir = Join-Path $backupRoot $stamp

if ($SkipBackup) {
    Say "  已跳过（-SkipBackup）" Yellow
} elseif ($DryRun) {
    Say "  [演习] 会备份到 $backupDir" Yellow
} else {
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

    $targets = @("data\yxo.db", "config_local.py")
    foreach ($t in $targets) {
        $src = Join-Path $repoRoot $t
        if (Test-Path $src) {
            $dst = Join-Path $backupDir (Split-Path $t -Leaf)
            Copy-Item $src $dst -Force
            $mb = "{0:N1}" -f ((Get-Item $dst).Length / 1MB)
            Say "  已备份 $t  ($mb MB)" Green
        } else {
            Say "  跳过 $t（不存在）" DarkGray
        }
    }

    # 只留最近 10 份，避免磁盘被吃满
    $old = Get-ChildItem -Directory $backupRoot -ErrorAction SilentlyContinue |
           Sort-Object Name -Descending | Select-Object -Skip 10
    if ($old) {
        $old | Remove-Item -Recurse -Force
        Say ("  已清理 " + $old.Count + " 份过期备份（保留最近 10 份）") DarkGray
    }
    Say "  备份位置 $backupDir" Green
}

# ---------- 3. 记录版本号 ----------
Step 3 "记录当前版本（供回滚）"

$lastDeployFile = Join-Path $repoRoot ".last_deploy"
if ($DryRun) {
    Say "  [演习] 会把 $($currentCommit.Substring(0,7)) 写进 .last_deploy" Yellow
} else {
    @(
        "commit=$currentCommit"
        "time=$stamp"
        "backup=$backupDir"
    ) | Set-Content -Path $lastDeployFile -Encoding UTF8
    Say "  已写入 .last_deploy" Green
}

# ---------- 4. 拉取新代码 ----------
Step 4 "拉取 main 最新代码"

git fetch origin 2>&1 | Out-Null
$remoteCommit = (git rev-parse origin/main).Trim()

if ($currentCommit -eq $remoteCommit) {
    Say "  已经是最新版本，无需更新" Green
    Say ""
    Say "部署结束（没有变化）。" Green
    exit 0
}

Say "  将从 $($currentCommit.Substring(0,7)) 更新到 $($remoteCommit.Substring(0,7))" Yellow
Say ""
Say "  本次会变动的文件：" Yellow
git diff --stat HEAD origin/main | ForEach-Object { Say "    $_" }
Say ""
Say "  包含的提交：" Yellow
git log --oneline HEAD..origin/main | ForEach-Object { Say "    $_" }

if ($DryRun) {
    Say ""
    Say "[演习结束] 以上就是真实部署会做的事，什么都没改。" Yellow
    exit 0
}

$reqBefore = ""
if (Test-Path "requirements.txt") { $reqBefore = (Get-FileHash "requirements.txt").Hash }

git pull --ff-only origin main 2>&1 | ForEach-Object { Say "  $_" }
if ($LASTEXITCODE -ne 0) {
    Say ""
    Say "  [失败] 拉取失败。最常见原因：生产目录的历史和 GitHub 分叉了" Red
    Say "         （有人在生产目录直接 commit 过）" Red
    Say "  代码没有被改动，现在还是 $($currentCommit.Substring(0,7))，可以安全排查。" Yellow
    exit 1
}
Say "  拉取成功" Green

# ---------- 5. 收尾提示 ----------
Step 5 "收尾"

if (Test-Path "requirements.txt") {
    $reqAfter = (Get-FileHash "requirements.txt").Hash
    if ($reqBefore -ne $reqAfter) {
        Say "  requirements.txt 变了，需要装依赖：" Yellow
        Say "    .\.venv\Scripts\pip.exe install -r requirements.txt" Yellow
    } else {
        Say "  依赖无变化，不用重装" Green
    }
}

Say ""
Say "=======================================" Green
Say " 部署完成  $($currentCommit.Substring(0,7)) -> $($remoteCommit.Substring(0,7))" Green
Say "=======================================" Green
Say ""
Say "接下来手动做两件事：" Cyan
Say "  1. 重启 Flask 服务（关掉原来的窗口，重新跑 start.bat）"
Say "  2. 打开页面点几下，确认订舱 / 舱单功能正常"
Say ""
Say "如果发现不对劲，立刻回滚：" Yellow
Say "  powershell -ExecutionPolicy Bypass -File scripts\rollback.ps1" Yellow
Say ""
