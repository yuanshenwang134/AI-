@echo off
chcp 65001 >nul 2>&1
title 一键推送到 GitHub
cd /d "%~dp0"

echo.
echo ============================================================
echo    一键推送到 GitHub
echo ============================================================
echo.
echo  推到： https://github.com/yuanshenwang134/AI-
echo.

rem ---- 0) 先修 TLS：这台机器上 git 的 schannel 后端拿不到 Windows 凭据，
rem         不换 openssl 的话任何 push/clone 都会报 SEC_E_NO_CREDENTIALS ----
git config --global http.sslBackend openssl >nul 2>&1
if errorlevel 1 (
  echo  [提示] 全局配置写不进去，改用本仓库配置。
  git config http.sslBackend openssl >nul 2>&1
)
echo  [1/4] TLS 后端已设为 openssl

rem ---- 1) 检查状态 ----
for /f %%i in ('git rev-list --count HEAD 2^>nul') do set COMMITS=%%i
echo  [2/4] 本地提交数：%COMMITS%

echo.
echo  [3/4] 需要你的 GitHub 令牌（Personal Access Token）
echo.
echo        还没令牌？按这个来，1 分钟：
echo          1. 浏览器打开  https://github.com/settings/tokens/new
echo             ^(直达"新建令牌"页面，不用一层层找菜单^)
echo          2. Note 随便填，比如 push-via-agent
echo          3. Expiration 选 7 days
echo          4. 勾选第一个大类  repo  ^(全选^)
echo          5. 拉到底点绿色的  Generate token
echo          6. 复制那串 ghp_ 开头的，粘到下面
echo.
echo        注意：粘贴时画面上不会显示任何字符（正常现象），粘完按回车即可。
echo.
set /p TOKEN=  请粘贴令牌后回车:

if "%TOKEN%"=="" (
  echo.
  echo  [x] 没输入令牌，已取消。
  pause
  exit /b 1
)

echo.
echo  [4/4] 正在推送 ...
git push "https://%TOKEN%@github.com/yuanshenwang134/AI-.git" main
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
  echo  ============================================================
  echo    [OK] 推送成功！
  echo.
  echo    仓库地址： https://github.com/yuanshenwang134/AI-
  echo.
  echo    [重要] 这个令牌已经用完了，建议立刻作废它：
  echo           https://github.com/settings/tokens
  echo           找到刚才那个令牌，点 Delete / Revoke
  echo  ============================================================
) else (
  echo  ============================================================
  echo    [x] 推送失败（错误码 %RC%）
  echo.
  echo    常见原因：
  echo      1. 令牌粘错 / 少粘了一位  -^> 重新生成一个再来
  echo      2. 令牌没勾 repo 权限      -^> 重新生成，勾上 repo
  echo      3. 令牌过期了              -^> 重新生成
  echo      4. 网络不通                -^> 能打开 github.com 网页吗？
  echo.
  echo    上面 git 的原始报错里通常写着具体原因，可以对照看。
  echo  ============================================================
)
echo.
pause
