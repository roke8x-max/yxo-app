<#
    渝新欧订舱系统 —— 回滚脚本（只在服务器 D 盘生产目录运行）

    什么时候用：
        刚部署完，发现页面打不开 / 功能坏了 / 报错，
        先回滚恢复业务，再慢慢查原因。别在生产环境上调试。

    用法：
        powershell -ExecutionPolicy Bypass -File scripts\rollback.ps1
        powershell -ExecutionPolicy Bypass -File scripts\rollback.ps1 -DryRun   看看会退到哪，不执行
        powershell -ExecutionPolicy Bypass -File scripts\rollback.ps1 -Force    跳过确认

    它只回滚代码，不动数据库。
    数据库需要恢复时脚本会把命令打出来，由人手动执行 —— 因为回滚数据库
    会丢掉这段时间录进去的真实业务数据，这个决定必须人来做。
#>

param(
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
function Say($msg, $color = "White") { Write-Host $msg -ForegroundColor $color }

$repoRoot = git rev-parse --show-toplevel 2>$null
if (-not $repoRoot) { Say "[失败] 当前目录不是 git 仓库" Red; exit 1 }
$repoRoot = $repoRoot -replace '/', '\'
Set-Location $repoRoot

Say ""
Say "=======================================" Yellow
Say " 回滚到上一个部署版本" Yellow
Say "=======================================" Yellow

$lastDeployFile = Join-Path $repoRoot ".last_deploy"
if (-not (Test-Path $lastDeployFile)) {
    Say ""
    Say "[失败] 找不到 .last_deploy，说明这台机器还没用 deploy.ps1 部署过。" Red
    Say ""
    Say "手动回滚办法：" Yellow
    Say "  git log --oneline -10        先看最近的版本列表" Yellow
    Say "  git reset --hard <版本号>     退回你要的那个" Yellow
    exit 1
}

$info = @{}
Get-Content $lastDeployFile | ForEach-Object {
    $kv = $_ -split "=", 2
    if ($kv.Count -eq 2) { $info[$kv[0]] = $kv[1] }
}

$target = $info["commit"]
$backupDir = $info["backup"]
$deployTime = $info["time"]

if (-not $target) { Say "[失败] .last_deploy 内容异常，读不到版本号" Red; exit 1 }

$current = (git rev-parse HEAD).Trim()

Say ""
Say ("  现在版本   " + $current.Substring(0,7))
Say ("  退回到     " + $target.Substring(0,7) + "   （$deployTime 部署前的状态）") Yellow
Say ""

if ($current -eq $target) {
    Say "  当前已经是这个版本了，无需回滚。" Green
    exit 0
}

Say "  会被撤销的提交：" Yellow
git log --oneline "$target..HEAD" | ForEach-Object { Say "    $_" }
Say ""

$dirty = git status --porcelain
if ($dirty) {
    Say "  注意：工作树有未提交改动，回滚会一并丢弃：" Red
    $dirty | ForEach-Object { Say "    $_" Red }
    Say ""
}

if ($DryRun) {
    Say "[演习结束] 以上就是真实回滚会做的事，什么都没改。" Yellow
    exit 0
}

if (-not $Force) {
    $ans = Read-Host "  确认回滚？输入 yes 继续"
    if ($ans -ne "yes") { Say "  已取消。" DarkGray; exit 0 }
}

git reset --hard $target 2>&1 | ForEach-Object { Say "  $_" }
if ($LASTEXITCODE -ne 0) { Say "  [失败] 回滚失败，立刻联系骁洋" Red; exit 1 }

Say ""
Say "=======================================" Green
Say " 代码已回滚到 $($target.Substring(0,7))" Green
Say "=======================================" Green
Say ""
Say "接下来：" Cyan
Say "  1. 重启 Flask 服务（关掉窗口，重新跑 start.bat）"
Say "  2. 打开页面确认恢复正常"
Say ""

if ($backupDir -and (Test-Path $backupDir)) {
    Say "如果数据库也需要恢复（谨慎！会丢掉这段时间录入的业务数据）：" Yellow
    Say "  1. 先停掉 Flask 服务" Yellow
    Say "  2. 把现在的库另存一份：" Yellow
    Say "     Copy-Item data\yxo.db data\yxo.db.before_rollback" Yellow
    Say "  3. 再恢复备份：" Yellow
    Say ("     Copy-Item """ + (Join-Path $backupDir "yxo.db") + """ data\yxo.db -Force") Yellow
    Say ""
    Say "这一步必须先问过骁洋。" Red
    Say ""
}

Say "回滚完成后记得在 GitHub 上说明原因，把坏掉的改动从 main 上撤掉。" Cyan
Say ""
