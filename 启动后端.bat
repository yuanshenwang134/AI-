@echo off
chcp 65001 >nul 2>&1
rem ==========================================================================
rem  Start the FastAPI backend (AI Hoop Analyst).
rem  --------------------------------------------------------------------------
rem  Why this file is bigger than it looks:
rem    Dependencies are NOT always installed globally. On this machine they live
rem    in ..\aihoop_assets\libs, while the older version of this script only set
rem    PYTHONPATH=%CD%\src -- so `import uvicorn` failed with
rem    "ModuleNotFoundError: No module named 'uvicorn'", and the window just sat
rem    at "press any key" looking like a successful start.
rem    This version finds the dependency folder automatically and, if it still
rem    cannot start, prints exactly what is missing and how to fix it.
rem
rem  Usage:  double-click this file        (optional first argument = port)
rem ==========================================================================
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

rem ---- 1) locate the dependency folder (deliberately kept out of the repo) ----
set "LIBS="
if defined AIHOOP_LIBS if exist "%AIHOOP_LIBS%" set "LIBS=%AIHOOP_LIBS%"
if not defined LIBS if exist "%~dp0..\aihoop_assets\libs" set "LIBS=%~dp0..\aihoop_assets\libs"
if not defined LIBS if exist "%~dp0aihoop_assets\libs" set "LIBS=%~dp0aihoop_assets\libs"
if not defined LIBS if exist "%~dp0assets\libs" set "LIBS=%~dp0assets\libs"

set "PYTHONPATH=%~dp0src"
if defined LIBS set "PYTHONPATH=%PYTHONPATH%;%LIBS%"

rem ---- 2) env vars the pipeline needs.
rem         Without YOLO_CONFIG_DIR, ultralytics tries to write %APPDATA% and
rem         dies with "PermissionError: [WinError 5]". ----
set "YOLO_CONFIG_DIR=%~dp0_tmp\yolo_cfg"
set "TMP=%~dp0_tmp\tmp"
set "TEMP=%TMP%"
set "MPLCONFIGDIR=%~dp0_tmp\mpl"
set "TORCH_HOME=%~dp0_tmp\torch"
if not exist "%YOLO_CONFIG_DIR%" mkdir "%YOLO_CONFIG_DIR%" >nul 2>&1
if not exist "%TMP%" mkdir "%TMP%" >nul 2>&1

rem ---- 3) pick an interpreter: .venv first, then whatever `python` resolves to ----
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem ---- 4) verify this interpreter can actually import what we need ----
"%PY%" -c "import uvicorn, fastapi" >nul 2>&1
if errorlevel 1 (
  echo.
  echo [x] This Python cannot import uvicorn / fastapi:
  echo       interpreter : %PY%
  echo       deps folder : %LIBS%
  echo.
  echo     Two ways to fix it:
  echo       1^) install the deps into that interpreter:
  echo            "%PY%" -m pip install -r requirements.txt
  echo       2^) or point AIHOOP_LIBS at the folder holding them:
  echo            set AIHOOP_LIBS=D:\path\to\libs
  echo.
  echo     Then run this file again.
  pause
  exit /b 1
)

rem ---- 5) start (default port 8000, or the first argument) ----
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

echo ============================================================
echo   AI Hoop Analyst - backend (FastAPI)
echo ------------------------------------------------------------
echo   API docs : http://127.0.0.1:%PORT%/docs
echo   Health   : http://127.0.0.1:%PORT%/api/health
echo   Frontend : run the demo launcher, then re-probe if needed
echo ------------------------------------------------------------
echo   AUTO-RESTART IS ON: editing src\aihoop\*.py reloads in ~2s.
echo   Keep this window open - closing it stops the backend.
echo   The upload page shows code_rev / code_loaded_at / stale from
echo   /api/health, so a stale process is visible at a glance.
echo ============================================================
echo.

rem --kill-port���Ȱ�ռ������˿ڵ�**�ɽ���**�����������
rem Ϊʲô��Ҫ��������������ӽ����� uvicorn���ص�����ʱ�ӽ��̳���û��������
rem ��ɹ¶�ռ�Ŷ˿ڣ��������ͻ�ʧ�ܡ�����һ������ ����
rem �û������ľ���"�ص�����Ҳ��������"��ʵ��ȵ���PID һֱ���ţ���
"%PY%" scripts\serve_reload.py %PORT% --kill-port
pause
