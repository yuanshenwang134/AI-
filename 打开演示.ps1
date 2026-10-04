# ============================================================================
#  AI 篮球分析软件 · 一键打开演示
#  ---------------------------------------------------------------------------
#  为什么需要这个脚本：
#    直接双击 web\index.html 时，浏览器（尤其 Chrome）在 file:// 协议下会
#    拦截读取本地 JSON，结果就是白屏或"演示数据加载失败"。
#    本脚本在本机起一个最小 HTTP 服务，再用浏览器打开，彻底绕开这个问题。
#
#  用法：双击同目录下的「打开演示.bat」即可，无需手动运行本文件。
#        自测/无头环境：powershell -File 打开演示.ps1 -NoBrowser -ExitAfterCheck
# ============================================================================

param(
    # 只起服务、不开浏览器（自测用）
    [switch]$NoBrowser,
    # 检测到服务可用后立即退出（CI / 自测用）
    [switch]$ExitAfterCheck
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = $PSScriptRoot
$WebDir = Join-Path $Root "web"
$Index = Join-Path $WebDir "index.html"

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  AI 篮球分析软件 · 打开前端" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# ---- 0) 没有数据就先生成一份（仓库里不带产物）-------------------------------
$DemoJson = Join-Path $WebDir "demo\game.json"
$FixJson = Join-Path $Root "out\fixture\game.json"
if ((-not (Test-Path $DemoJson)) -and (-not (Test-Path $FixJson))) {
    Write-Host "[提示] 还没有任何分析产物。先生成一场合成比赛（约 10 秒）…" -ForegroundColor Yellow
}

if (-not (Test-Path $Index)) {
    Write-Host "[错误] 找不到 $Index" -ForegroundColor Red
    Write-Host "       请确认本脚本放在 aihoopanalyst 目录下。" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}

# ---- 1) 找一个可用的 Python -------------------------------------------------
$candidates = @(
    (Join-Path $Root ".venv\Scripts\python.exe"),
    (Join-Path $Root "venv\Scripts\python.exe")
)
$Python = $null
foreach ($c in $candidates) {
    if (Test-Path $c) { $Python = $c; break }
}
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}
# 最后再试 py 启动器
$PyLauncher = $null
if (-not $Python) {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { $PyLauncher = $cmd.Source }
}

if (-not $Python -and -not $PyLauncher) {
    Write-Host "[提示] 未检测到 Python —— 改用备用方式打开（file:// 直开）。" -ForegroundColor Yellow
    Write-Host "       如果浏览器显示白屏，请安装 Python 3.10+ 后重试：" -ForegroundColor Yellow
    Write-Host "       https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host ""
    Start-Process $Index
    Write-Host "已尝试用默认浏览器打开。若白屏，请看 README.md 的『离线演示』一节。" -ForegroundColor Yellow
    Read-Host "按回车键退出"
    exit 0
}
if ($Python) { Write-Host "[1/3] Python: $Python" -ForegroundColor Green }
else         { Write-Host "[1/3] Python: 通过 py 启动器" -ForegroundColor Green }

# ---- 2) 从 8080 起找一个没被占用的端口 --------------------------------------
$port = 0
for ($p = 8080; $p -le 8100; $p++) {
    $busy = $false
    try {
        $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $p)
        $l.Start(); $l.Stop()
    } catch { $busy = $true }
    if (-not $busy) { $port = $p; break }
}
if ($port -eq 0) {
    Write-Host "[错误] 8080~8100 端口都被占用了，请先关掉占用端口的程序。" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}
$Url = "http://127.0.0.1:$port/web/index.html"
Write-Host "[2/3] 本地服务端口: $port" -ForegroundColor Green

# ---- 3) 启动服务（新窗口，方便你看日志/随时关掉）---------------------------
Write-Host "[3/3] 正在启动本地服务并打开浏览器…" -ForegroundColor Green
Write-Host ""
Write-Host "  访问地址：$Url" -ForegroundColor White
Write-Host ""
Write-Host "  ★ 会弹出一个新窗口跑服务，演示期间请不要关它；" -ForegroundColor Yellow
Write-Host "    演示结束后在那个窗口按 Ctrl+C，或直接关掉该窗口。" -ForegroundColor Yellow
Write-Host ""

$inner = @"
`$Host.UI.RawUI.WindowTitle = 'AI 篮球分析 · 本地服务（演示期间请勿关闭）'
Set-Location -LiteralPath '$Root'
Write-Host '正在提供本地服务，演示期间请勿关闭本窗口。' -ForegroundColor Green
Write-Host '结束后按 Ctrl+C 停止。' -ForegroundColor DarkGray
Write-Host ''
& '$Python' -m http.server $port --bind 127.0.0.1
"@
if (-not $Python) {
    $inner = @"
`$Host.UI.RawUI.WindowTitle = 'AI 篮球分析 · 本地服务（演示期间请勿关闭）'
Set-Location -LiteralPath '$Root'
Write-Host '正在提供本地服务，演示期间请勿关闭本窗口。' -ForegroundColor Green
Write-Host ''
& py -m http.server $port --bind 127.0.0.1
"@
}

Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $inner) `
    -WindowStyle Minimized

# 等服务起来（最多等 5 秒）
$ready = $false
for ($i = 0; $i -lt 25; $i++) {
    Start-Sleep -Milliseconds 200
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}

if ($ready) {
    Write-Host "✔ 本地服务已就绪。" -ForegroundColor Green
    if (-not $NoBrowser) {
        Start-Process $Url
        Write-Host "  已在浏览器打开。若页面提示「未找到数据」，先在根目录跑：" -ForegroundColor Green
        Write-Host "    `$env:PYTHONPATH='src'; python -m aihoop.cli demo --seed 7 --duration 600 --out out/demo" -ForegroundColor Green
    } else {
        Write-Host "  （-NoBrowser：未自动打开浏览器）" -ForegroundColor DarkGray
    }
} else {
    Write-Host "[提示] 服务启动较慢或失败。" -ForegroundColor Yellow
    Write-Host "       请手动访问：$Url" -ForegroundColor Yellow
    if (-not $NoBrowser) { Start-Process $Url }
}

# 自测模式：确认服务可用就退出，不留下需要手动关闭的窗口
if ($ExitAfterCheck) {
    if ($ready) { exit 0 } else { exit 1 }
}

Write-Host ""
Read-Host "按回车键关闭本窗口（不影响已打开的演示）"
