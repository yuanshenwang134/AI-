# ==========================================================================
# 下载素材与依赖.ps1
# --------------------------------------------------------------------------
# 为什么要在**你自己的终端**里跑：沙箱里的命令没有外网（实测所有 HTTPS 都被挡），
# 但你的机器能上网。下载完文件落在本机，我就能接着处理。
#
# 用法（在项目根目录）：
#     powershell -ExecutionPolicy Bypass -File .\下载素材与依赖.ps1
#
# 只做三件事：① 克隆有用的仓库到 third_party/ ② 下载姿态模型 ③ 装可选依赖
# ==========================================================================

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot
New-Item -ItemType Directory -Force -Path third_party | Out-Null
New-Item -ItemType Directory -Force -Path data\calib_ds | Out-Null

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

Section "0/4 检查网络"
try {
    $r = Invoke-WebRequest -Uri 'https://github.com' -UseBasicParsing -TimeoutSec 10 -Method Head
    Write-Host "  ✓ 能访问 github.com（HTTP $($r.StatusCode)）" -ForegroundColor Green
} catch {
    Write-Host "  ✗ 访问不了 github.com：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "    请检查代理 / 网络后重试。" -ForegroundColor Yellow
    exit 1
}

Section "1/4 克隆有用的仓库到 third_party/"
$repos = @(
    @{ name = 'ByteTrack';                 url = 'https://github.com/FoundationVision/ByteTrack.git' },
    @{ name = 'KaliCalib';                 url = 'https://github.com/CEA-LIST/KaliCalib.git' },
    @{ name = 'camera-calibration-challenge'; url = 'https://github.com/DeepSportRadar/camera-calibration-challenge.git' },
    @{ name = 'nba-vision';                url = 'https://github.com/j7yn/nba-vision.git' },
    @{ name = 'AI_BasketBall_Analysis_v1'; url = 'https://github.com/HanaFEKI/AI_BasketBall_Analysis_v1.git' },
    @{ name = 'deepsport';                 url = 'https://github.com/gabriel-vanzandycke/deepsport.git' }
)
foreach ($r in $repos) {
    $dst = Join-Path 'third_party' $r.name
    if (Test-Path $dst) { Write-Host "  - $($r.name) 已存在，跳过"; continue }
    Write-Host "  ↓ $($r.name) …"
    git clone --depth 1 $r.url $dst 2>&1 | Select-Object -Last 1 | ForEach-Object { "      $_" }
}

Section "2/4 下载姿态模型（③ 动作识别要用）"
# 用 ultralytics 的官方姿态模型即可，不必装 mmaction2（Python 3.14 上大概率装不上）
$pose = 'yolo11n-pose.pt'
if (Test-Path $pose) {
    Write-Host "  - $pose 已存在，跳过"
} else {
    $u = "https://github.com/ultralytics/assets/releases/download/v8.3.0/$pose"
    try {
        Invoke-WebRequest -Uri $u -OutFile $pose -UseBasicParsing -TimeoutSec 300
        Write-Host "  ✓ 已下载 $pose（$([math]::Round((Get-Item $pose).Length/1MB,1)) MB）" -ForegroundColor Green
    } catch {
        Write-Host "  ✗ 下载失败：$($_.Exception.Message)" -ForegroundColor Red
        Write-Host "    可手动下载：https://github.com/ultralytics/assets/releases" -ForegroundColor Yellow
    }
}

Section "3/4 可选依赖"
Write-Host "  当前 Python 版本：" -NoNewline
& .venv\Scripts\python.exe -c "import sys; print(sys.version.split()[0])"
Write-Host "  注意：本项目 venv 是 Python 3.14，mmcv/mmaction2 大概率没有对应 wheel。" -ForegroundColor Yellow
Write-Host "  ③ 动作识别我改用 ultralytics 姿态模型 + 自训分类器，**不需要 mmaction2**。" -ForegroundColor Yellow
Write-Host "  如需尝试（可能失败，失败不影响其它功能）：" -ForegroundColor Yellow
Write-Host "     .venv\Scripts\pip.exe install mmaction2 mmcv" -ForegroundColor Gray

Section "4/4 标定数据集（② 自动球场标定要用）"
Write-Host "  已建好目录： data\calib_ds\"
Write-Host "  请把 camera-calibration-challenge 的**数据文件**放进去。该数据集的数据通常不在"
Write-Host "  GitHub 仓库里，需要按仓库 README 的说明申请/下载（可能是表单 + 网盘链接）："
Write-Host "     third_party\camera-calibration-challenge\README.md" -ForegroundColor Gray
Write-Host "  下载后解压到 data\calib_ds\ 即可（里面应有 images/ 与 labels/ 之类）。"

Write-Host "`n完成。把这一段输出发给我，我接着往下做。" -ForegroundColor Cyan
