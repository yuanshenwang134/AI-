@echo off
chcp 65001 >nul 2>&1
title 推送到 GitHub（浏览器登录，无需令牌）
cd /d "%~dp0"

echo.
echo ============================================================
echo    推送到 GitHub  ^(用浏览器登录，不需要令牌^)
echo ============================================================
echo.
echo  推到： https://github.com/yuanshenwang134/AI-
echo.
echo  接下来会发生什么：
echo    1) 自动配好 TLS 后端  ^(这台机器上 git 默认的 schannel 是坏的^)
echo    2) 弹出登录窗口  -^>  点 "Sign in with your browser"
echo    3) 浏览器里点一下 Authorize 授权
echo    4) 自动推送完成
echo.
echo  如果第 2 步没弹窗，看本窗口最后的提示，有兜底办法。
echo.
pause
echo.

rem ---- 1) TLS 后端：这台机器上 schannel 拿不到 Windows 凭据，
rem         不换 openssl 的话任何 push/clone 都会报 SEC_E_NO_CREDENTIALS ----
git config --global http.sslBackend openssl >nul 2>&1
if errorlevel 1 (
  echo  [提示] 全局配置写不进去，改用本仓库配置。
  git config http.sslBackend openssl >nul 2>&1
)
echo  [1/3] TLS 后端 = openssl

rem ---- 2) 用 Git Credential Manager（会弹浏览器登录窗）----
git config --global credential.helper manager >nul 2>&1
if errorlevel 1 ( git config credential.helper manager >nul 2>&1 )
echo  [2/3] 凭据助手 = manager

echo.
echo  [3/3] 开始推送 ...
echo.
git push -u origin main
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
  echo  ============================================================
  echo    [OK] 推送成功！
  echo.
  echo    打开看看： https://github.com/yuanshenwang134/AI-
  echo  ============================================================
  echo.
  pause
  exit /b 0
)

echo  ============================================================
echo    [!] 用 manager 没成功（错误码 %RC%）
echo.
echo    改用另一个凭据助手 wincred 再试一次 —— 它更可靠：
echo      ^(如果它要你输用户名/密码，用户名填 yuanshenwang134，
echo        密码去 github.com/settings/tokens 生成一个令牌粘进来^)
echo  ============================================================
echo.
git config credential.helper wincred
git push -u origin main
set RC2=%ERRORLEVEL%
echo.
if "%RC2%"=="0" (
  echo    [OK] 推送成功！ https://github.com/yuanshenwang134/AI-
) else (
  echo    [x] 还是失败（错误码 %RC2%）。
  echo.
  echo    兜底办法：手动加远程地址，把令牌写在地址里推一次
  echo      git push https://^<你的令牌^>@github.com/yuanshenwang134/AI-.git main
  echo.
  echo    或者在浏览器里直接传文件：
  echo      https://github.com/yuanshenwang134/AI-/upload/main
  echo      ^(把项目里除 data/uploads、out、_tmp、tools\ffmpeg 之外的文件夹拖进去^)
)
echo.
pause
