@echo off
chcp 65001 >nul 2>&1
rem ==========================================================================
rem  一键重启后端 —— 专治"关掉窗口也重启不了"。
rem
rem  为什么会重启不了（实测根因）：
rem    启动后端.bat 用 scripts\serve_reload.py 跑 uvicorn，uvicorn 是它的
rem    **子进程**。关掉控制台窗口时，子进程常常没跟着死，变成孤儿进程继续
rem    占着 8000 端口。你再启动一次，新进程发现端口被占就立刻退出 ——
rem    窗口一闪而过，看起来就是"重启不了"。
rem
rem  这个脚本先**找出占着端口的是哪个进程**，杀掉，确认端口空了，再启动。
rem ==========================================================================
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

echo.
echo ============================================================
echo    AI 篮球分析软件 · 一键重启后端（端口 %PORT%）
echo ============================================================
echo.

rem ---- 1) 找出占着端口的进程并杀掉 ----
echo [1/4] 检查端口 %PORT% 是不是被旧进程占着 ...
set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
  if not "%%p"=="0" set "FOUND=%%p"
)

if not defined FOUND (
  echo       端口是空的，不用杀。
  goto :start
)

echo       端口被 PID %FOUND% 占着，正在结束它 ...
taskkill /PID %FOUND% /F >nul 2>&1
if errorlevel 1 (
  echo       [!] 结束失败 —— 可能权限不够，或者那是个系统进程。
  echo           请用**管理员身份**再运行一次这个文件。
  echo.
  pause
  exit /b 1
)
echo       已结束 PID %FOUND%。

rem 给它一点时间释放端口，然后确认
ping -n 3 127.0.0.1 >nul 2>&1
set "STILL="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
  if not "%%p"=="0" set "STILL=%%p"
)
if defined STILL (
  echo       [!] 端口还被 PID %STILL% 占着，再等 2 秒 ...
  ping -n 3 127.0.0.1 >nul 2>&1
  taskkill /PID %STILL% /F >nul 2>&1
)
echo       端口已释放。

:start
echo.
echo [2/4] 启动后端（这个窗口要一直开着；关掉它后端就停了）...
echo       提示：按 Ctrl+C 可以正常停止后端。
echo.

start "AI Hoop 后端 :%PORT%" cmd /k ""%~dp0启动后端.bat" %PORT%"

rem ---- 3) 等它起来 ----
echo [3/4] 等待后端就绪 ...
set "OK="
for /l %%i in (1,1,40) do (
  if not defined OK (
    ping -n 2 127.0.0.1 >nul 2>&1
    for /f %%h in ('powershell -NoProfile -Command "try{(Invoke-RestMethod http://127.0.0.1:%PORT%/api/health -TimeoutSec 3).ok}catch{''}"') do (
      if /i "%%h"=="True" set "OK=1"
    )
  )
)

echo.
if defined OK (
  echo [4/4] [OK] 后端已就绪： http://127.0.0.1:%PORT%/api/health
  echo.
  echo       顺便看一眼源码是不是最新的（stale 那一项）：
  powershell -NoProfile -Command "try{$h=Invoke-RestMethod http://127.0.0.1:%PORT%/api/health -TimeoutSec 3; Write-Host ('       code_rev        = ' + $h.code_rev); Write-Host ('       code_loaded_at  = ' + $h.code_loaded_at); if($h.stale){Write-Host '       stale           = True  << 后端还是旧代码，等 2 秒再看' -ForegroundColor Yellow}else{Write-Host '       stale           = False  << 已是最新代码' -ForegroundColor Green}}catch{}"
  echo.
  echo       现在回浏览器按 Ctrl+F5 刷新页面。
) else (
  echo [4/4] [x] 等了 80 秒还没就绪 —— 请看那个后端窗口里的报错。
  echo       常见原因：依赖没装好（窗口里会写清楚缺什么）。
)
echo.
pause
