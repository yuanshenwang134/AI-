# 本机启动队友的后端（依赖装在 aihoop_assets\libs，不是 pip 全局）
#
# 为什么不能用他自带的 `启动后端.bat`：
#   那个脚本写的是 set PYTHONPATH=%CD%\src，而我们（以及任何把依赖装在
#   自定义目录的人）需要把依赖目录一起带上，否则：
#     ModuleNotFoundError: No module named 'fastapi'
#   同一个毛病也在 scripts\serve_reload.py:97 与 scripts\check_all.py:29
#   （它们都做 env["PYTHONPATH"] = str(SRC)，是**覆盖**而不是追加）。
#
# 本脚本的做法：**不改他的源码**，每次启动时从 serve_reload.py 生成一份
# 打了补丁的副本到 _tmp\，只把那一行改成"追加依赖目录"。他更新源码后，
# 下一次启动会自动用新源码重新生成。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\run_backend_local.ps1          # 默认 8000
#   powershell -ExecutionPolicy Bypass -File .\run_backend_local.ps1 8010     # 换端口
param([int]$Port = 8000)

$ErrorActionPreference = 'Stop'
$repo   = $PSScriptRoot
$assets = Join-Path (Split-Path $repo -Parent) 'aihoop_assets'
$libs   = Join-Path $assets 'libs'

if (-not (Test-Path (Join-Path $libs 'fastapi'))) {
  Write-Host "[x] 找不到依赖目录：$libs" -ForegroundColor Red
  Write-Host "    先确认 aihoop_assets 还在（旧项目的依赖安装目录就在里面）。" -ForegroundColor Red
  exit 1
}

# --- 五个必需的环境变量（缺一个就会出现"找不到 cv2"或 Ultralytics 写 AppData 被拒）---
$env:PYTHONPATH = "$repo\src;$assets\_patch;$libs"
$env:TMP = Join-Path $repo '_tmp'; $env:TEMP = $env:TMP
New-Item -ItemType Directory -Force -Path $env:TMP | Out-Null
$env:YOLO_CONFIG_DIR = Join-Path $assets '_ulcfg'
$env:MPLCONFIGDIR   = Join-Path $assets '_mpl'
$env:TORCH_HOME     = Join-Path $assets '_torch'
$env:HF_HOME        = Join-Path $assets '_hf'
$env:PYTHONIOENCODING = 'utf-8'
$env:AIHOOP_MODELS  = Join-Path $assets 'models'
$env:AIHOOP_SAMPLES = Join-Path $assets 'samples'

# --- 生成打了补丁的服务启动副本 ---
$orig    = Join-Path $repo 'scripts\serve_reload.py'
$patched = Join-Path $env:TMP 'serve_reload_local.py'
$text    = Get-Content $orig -Raw -Encoding UTF8
$needle  = 'env["PYTHONPATH"] = str(SRC)'
# 必须用 .Contains()：通配符里 [ ] 是字符集，写成 -like "*$needle*" 会永远匹配不上（踩过）。
if (-not $text.Contains($needle)) {
  Write-Host "[!] serve_reload.py 里没找到预期的那一行，可能他改过实现了。" -ForegroundColor Yellow
  Write-Host "    先看第 90~100 行确认 PYTHONPATH 怎么设的，再决定是否直接跑原文件。" -ForegroundColor Yellow
  $patched = $orig
} else {
  $replacement = 'env["PYTHONPATH"] = str(SRC) + os.pathsep + r"' + $libs + '" + os.pathsep + r"' + (Join-Path $assets '_patch') + '"'
  $text = $text.Replace($needle, $replacement)
  # 确保 os 已导入（他的文件本来就有 import os，这里兜一层）
  if ($text -notmatch '(?m)^import os\b') { $text = "import os`n" + $text }
  Set-Content -Path $patched -Value $text -Encoding UTF8
  Write-Host "已生成补丁副本：$patched"
}

Write-Host "仓库    : $repo"
Write-Host "依赖    : $libs"
Write-Host "权重    : $env:AIHOOP_MODELS  （rim_ball_v6.pt = 自训练的篮球+篮筐双类）"
Write-Host "素材    : $env:AIHOOP_SAMPLES"
Write-Host "后端    : http://127.0.0.1:$Port/api/health"
Write-Host "接口文档: http://127.0.0.1:$Port/docs"
Write-Host ""

& python $patched $Port
