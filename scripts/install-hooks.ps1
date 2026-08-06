<#
    安装 git hooks —— 每个环境（本机 / E盘开发 / D盘生产）跑一次就行

    用法：在仓库根目录执行
        powershell -ExecutionPolicy Bypass -File scripts\install-hooks.ps1

    装完之后，这台机器上就再也不能直接 push 到 main 了，
    误操作会被当场拦下并告诉你正确做法。

    注意：hook 文件存在 .git\hooks\ 里，这个目录不入库，
    所以每台机器、每次重新 clone 之后都要重跑一次本脚本。
#>

$ErrorActionPreference = "Stop"

# 定位仓库根目录
$repoRoot = git rev-parse --show-toplevel 2>$null
if (-not $repoRoot) {
    Write-Host "[失败] 当前目录不是 git 仓库，请先 cd 到 yxo-app 目录再运行" -ForegroundColor Red
    exit 1
}
$repoRoot = $repoRoot -replace '/', '\'

$srcDir = Join-Path $repoRoot "scripts\hooks"
$dstDir = Join-Path $repoRoot ".git\hooks"

if (-not (Test-Path $srcDir)) {
    Write-Host "[失败] 找不到 $srcDir，仓库可能不完整，先 git pull" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "仓库位置: $repoRoot" -ForegroundColor Cyan
Write-Host ""

$count = 0
Get-ChildItem -File $srcDir | ForEach-Object {
    $dst = Join-Path $dstDir $_.Name
    Copy-Item $_.FullName $dst -Force
    Write-Host ("  已安装  {0}" -f $_.Name) -ForegroundColor Green
    $count++
}

Write-Host ""
Write-Host "完成，共安装 $count 个 hook。" -ForegroundColor Green
Write-Host ""
Write-Host "自测一下（应该被拦下）:" -ForegroundColor Yellow
Write-Host "  git checkout main"
Write-Host "  git push origin main"
Write-Host ""
