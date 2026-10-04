# 环境自检：一条命令看清这台机器能跑到哪一步
# 用法：  .\scripts\check_env.ps1

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Check($name, $ok, $hint) {
    if ($ok) {
        Write-Host ("  [OK]   {0}" -f $name) -ForegroundColor Green
    } else {
        Write-Host ("  [MISS] {0}  -> {1}" -f $name, $hint) -ForegroundColor Yellow
    }
}

Write-Host "== 基础环境 ==" -ForegroundColor Cyan
$py = Get-Command python -ErrorAction SilentlyContinue
Check "Python" ($null -ne $py) "安装 Python 3.10+ 并加入 PATH"
if ($py) {
    $ver = (python -c "import sys;print('.'.join(map(str,sys.version_info[:3])))") 
    Write-Host "        版本：$ver"
}

Write-Host "`n== 套餐 A 最小依赖 ==" -ForegroundColor Cyan
python -c "import fastapi" 2>$null; Check "fastapi" ($LASTEXITCODE -eq 0) "pip install -r requirements.txt"
python -c "import uvicorn" 2>$null; Check "uvicorn" ($LASTEXITCODE -eq 0) "pip install -r requirements.txt"
python -c "import pytest"  2>$null; Check "pytest"  ($LASTEXITCODE -eq 0) "pip install -r requirements.txt"

Write-Host "`n== 完整推理依赖（可选）==" -ForegroundColor Cyan
python -c "import torch;print(torch.__version__, 'cuda:', torch.cuda.is_available())" 2>$null
Check "torch" ($LASTEXITCODE -eq 0) "pip install -r requirements-full.txt"
python -c "import ultralytics" 2>$null; Check "ultralytics (YOLOv8)" ($LASTEXITCODE -eq 0) "pip install ultralytics"
python -c "import cv2" 2>$null;         Check "opencv-python" ($LASTEXITCODE -eq 0) "pip install opencv-python"

Write-Host "`n== 外部工具 ==" -ForegroundColor Cyan
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
Check "ffmpeg（高光剪辑）" ($null -ne $ff) "winget install Gyan.FFmpeg"
$fp = Get-Command ffprobe -ErrorAction SilentlyContinue
Check "ffprobe（读视频信息）" ($null -ne $fp) "随 ffmpeg 一起安装"
$git = Get-Command git -ErrorAction SilentlyContinue
Check "git（克隆开源底座）" ($null -ne $git) "winget install Git.Git"

Write-Host "`n== 工程自检 ==" -ForegroundColor Cyan
$env:PYTHONPATH = "src"
python -B -c "import aihoop; print('  aihoop 包可导入，版本', aihoop.__version__)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  aihoop 导入失败，请确认在 aihoopanalyst 目录下运行" -ForegroundColor Red
}

Write-Host "`n建议：" -ForegroundColor Cyan
Write-Host "  * 只有 CPU 也能跑：合成 demo 秒出，真视频推理会慢 5~20 倍，用 --stride 2 抽帧"
Write-Host "  * 没装 ffmpeg 时高光只导出时间码（clips.json），不影响其他功能"
Write-Host "  * 先跑 .\scripts\run_demo.ps1 确认链路，再上真视频"
