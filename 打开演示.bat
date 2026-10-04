@echo off
chcp 65001 >nul 2>&1
title AI 篮球分析软件 · 演示启动器

cd /d "%~dp0"

echo.
echo ==============================================
echo   AI 篮球分析软件 · 套餐 A 演示
echo ==============================================
echo.
echo 正在启动，请稍候...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; & '%~dp0打开演示.ps1'"

if errorlevel 1 (
  echo.
  echo [提示] 启动脚本执行失败。
  echo        你也可以直接双击 web\index.html
  echo        如果浏览器白屏，请看 README.md 的「离线演示」一节。
  echo.
  pause
)
