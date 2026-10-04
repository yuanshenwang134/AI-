# 一键跑完整条管线（Windows PowerShell）
# 用法：  .\scripts\run_demo.ps1            # 合成数据 demo
#         .\scripts\run_demo.ps1 -Seed 42 -Duration 1800

param(
    [int]$Seed = 7,
    [double]$Duration = 1200,
    [string]$Out = "out/demo"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:PYTHONPATH = "src"

Write-Host "==> 1/3 端到端自检" -ForegroundColor Cyan
python -B tests\test_plan_a.py
if ($LASTEXITCODE -ne 0) { throw "自检未通过，先修问题再跑管线" }

Write-Host "`n==> 2/3 生成合成比赛并跑管线" -ForegroundColor Cyan
python -B -m aihoop.cli demo --seed $Seed --duration $Duration --out $Out
if ($LASTEXITCODE -ne 0) { throw "管线执行失败" }

Write-Host "`n==> 3/3 同步演示数据到前端 web/demo（供无后端时演示）" -ForegroundColor Cyan
$demo = Join-Path $root "web/demo"
New-Item -ItemType Directory -Force -Path $demo | Out-Null
foreach ($f in @("game.json", "players.json", "shotchart.json", "report.md",
                 "report.json", "stats.csv", "shots.csv", "events.jsonl",
                 "tactics.json", "passes.csv", "spacing.csv")) {
    $src = Join-Path $Out $f
    if (Test-Path $src) { Copy-Item $src $demo -Force }
}
# 套餐 B：逐帧战术数据（JSONL -> JSON，前端演示模式直接读数组）
$frSrc = Join-Path $Out "tactics_frames.jsonl"
if (Test-Path $frSrc) {
    python scripts\jsonl_to_json.py $frSrc (Join-Path $demo "tactics_frames.json")
}
# 高光索引也给前端一份
$hl = Join-Path $Out "highlights/index.json"
if (Test-Path $hl) {
    New-Item -ItemType Directory -Force -Path (Join-Path $demo "highlights") | Out-Null
    Copy-Item $hl (Join-Path $demo "highlights") -Force
}

Write-Host "`n完成！" -ForegroundColor Green
Write-Host "  产物目录 : $Out"
Write-Host "  战报     : $Out/report.md"
Write-Host "  前端     : web/index.html  （浏览器直接打开）"
