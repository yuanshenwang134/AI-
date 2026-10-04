# ==========================================================================
# 推到 GitHub.ps1 —— 一键把本仓库推到 GitHub（第一次用）
# --------------------------------------------------------------------------
# 用法：在项目根目录右键 →「在终端中打开」，然后运行
#         powershell -ExecutionPolicy Bypass -File .\推到GitHub.ps1
#       或者直接双击 打开演示.bat 旁边的本文件（PowerShell 窗口会打开）
#
# 前提：
#   1. 已经在 GitHub 上建好了空仓库（不要勾 README/.gitignore/license）
#   2. 仓库地址正确（下面 $Repo 变量）
#   3. 网络能访问 github.com
# ==========================================================================

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# ---- 要推到哪里（如果仓库名不一样，改这一行）----
$Repo = 'https://github.com/yuanshenwang134/AI-.git'

Write-Host "=== 1/4 检查仓库状态 ===" -ForegroundColor Cyan
if (-not (Test-Path .git)) {
    Write-Host "这不是 git 仓库，先执行：git init" -ForegroundColor Red
    exit 1
}
$files = git ls-files
Write-Host ("  待推送文件数：{0}" -f $files.Count)
# 兜底：确认没有把大文件带进去（GitHub 单文件上限 100MB）
$big = @()
foreach ($f in $files) {
    if (Test-Path -LiteralPath $f) {
        $len = (Get-Item -LiteralPath $f).Length
        if ($len -gt 50MB) { $big += ("{0:N1} MB  {1}" -f ($len / 1MB), $f) }
    }
}
if ($big.Count -gt 0) {
    Write-Host "  ⚠ 发现超过 50MB 的文件，GitHub 会拒绝，请先加入 .gitignore：" -ForegroundColor Yellow
    $big | ForEach-Object { Write-Host "    $_" }
    exit 1
}
Write-Host "  ✓ 没有超大文件" -ForegroundColor Green

Write-Host "`n=== 2/4 关联远程仓库 ===" -ForegroundColor Cyan
git remote remove origin 2>$null
git remote add origin $Repo
Write-Host "  origin → $Repo"

Write-Host "`n=== 3/4 确认分支与作者 ===" -ForegroundColor Cyan
git branch -M main
Write-Host ("  分支：{0}" -f (git branch --show-current))
Write-Host ("  作者：{0} <{1}>" -f (git config user.name), (git config user.email))

Write-Host "`n=== 4/4 推送 ===" -ForegroundColor Cyan
Write-Host "  如果弹出登录窗口，选「Sign in with your browser」最省事；" -ForegroundColor Yellow
Write-Host "  如果在终端里要密码，请粘贴 Personal Access Token（不是账号密码）。" -ForegroundColor Yellow
Write-Host ""
git push -u origin main
if ($LASTEXITCODE -eq 0) {
    Write-Host "`n✓ 推送成功！打开： $($Repo -replace '\.git$','')" -ForegroundColor Green
} else {
    Write-Host "`n✗ 推送失败。常见原因：" -ForegroundColor Red
    Write-Host "  1) 仓库名不对 → 改本文件顶部的 `$Repo，或执行："
    Write-Host "     git remote set-url origin <正确的仓库地址>"
    Write-Host "  2) 认证失败 → 用 Personal Access Token 当密码（github.com/settings/tokens）"
    Write-Host "  3) 网络问题 → 能打开 github.com 网页吗？"
}
