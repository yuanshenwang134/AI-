# 把分析产物同步到 web/demo，供前端在「无后端」时演示
# 用法：  .\scripts\sync_demo.ps1                 # 默认从 out/demo 同步
#         .\scripts\sync_demo.ps1 -From out/mygame
#
# 为什么需要：前端要能在 file:// 下直接打开演示（答辩现场后端可能起不来），
# 所以把一份真实产物固化到 web/demo 里当兜底数据。
#
# ⚠️ 重要：本文件必须保存为「UTF-8 带 BOM」。
#    Windows PowerShell 5.1 会把无 BOM 的 UTF-8 当 ANSI(GBK) 读，中文注释乱码后
#    可能把下一行代码"吃掉"（注释吞行），脚本行为变得莫名其妙。
#    如果你用编辑器改过本文件，请确认 BOM 还在；修复命令见 README 或：
#      python -c "p='scripts/sync_demo.ps1';r=open(p,'rb').read();open(p,'wb').write(b'\xef\xbb\xbf'+r) if not r.startswith(b'\xef\xbb\xbf') else None"

param(
    [string]$From = "out/demo",
    [string]$To = "web/demo"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path $From)) {
    throw "找不到产物目录 $From ，请先运行 .\scripts\run_demo.ps1"
}

New-Item -ItemType Directory -Force -Path $To | Out-Null

$files = @("game.json", "players.json", "shotchart.json", "report.md",
           "report.json", "stats.csv", "shots.csv", "events.jsonl",
           "tactics.json", "passes.csv", "spacing.csv")

$n = 0
foreach ($f in $files) {
    $src = Join-Path $From $f
    if (Test-Path $src) {
        Copy-Item $src $To -Force
        $n++
    } else {
        Write-Warning "缺少 $f（跳过）"
    }
}

# 高光索引：两个位置都放，兼容前端不同的读取约定
#   web/demo/highlights.json        —— 前端 api.js 的 loadDemo() 默认读这个
#   web/demo/highlights/index.json  —— 与后端产物目录结构保持一致
$hlSrc = Join-Path $From "highlights/index.json"
if (Test-Path $hlSrc) {
    Copy-Item $hlSrc (Join-Path $To "highlights.json") -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $To "highlights") | Out-Null
    Copy-Item $hlSrc (Join-Path $To "highlights") -Force
    Write-Host "已同步高光索引 highlights.json"
}

# 套餐 B：逐帧战术数据在后端是 JSONL（一行一帧），前端演示模式要 JSON 数组，
# 这里转换一次。没有 tactics_frames.jsonl 就跳过（老产物 / 关闭了战术层）。
$frSrc = Join-Path $From "tactics_frames.jsonl"
$frDst = Join-Path $To "tactics_frames.json"
if (Test-Path $frSrc) {
    python scripts\jsonl_to_json.py $frSrc $frDst
    if ($LASTEXITCODE -eq 0) {
        Write-Host "已同步俯视战术图逐帧数据 tactics_frames.json"
        $n++
    } else {
        Write-Warning "tactics_frames.jsonl 转换失败（战术页仍可用，但没有逐帧动画）"
    }
} else {
    Write-Warning "缺少 tactics_frames.jsonl（战术页将没有俯视战术图动画）"
}

Write-Host "已同步 $n 个文件到 $To" -ForegroundColor Green
Write-Host "现在直接用浏览器打开 web/index.html 即可看到完整演示" -ForegroundColor Cyan
