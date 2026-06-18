@echo off
chcp 65001 >nul
title Cloudflare 外网访问隧道 - http://127.0.0.1:8000

cd /d "%~dp0"

if not exist "cloudflared.exe" (
    echo [错误] 当前目录找不到 cloudflared.exe
    echo 路径: %CD%
    pause
    exit /b 1
)

echo ============================================================
echo   Cloudflare Tunnel 启动中...
echo   本地地址: http://127.0.0.1:8000
echo ------------------------------------------------------------
echo   稍等几秒，下面会出现一条形如：
echo     https://xxxx-xxxx-xxxx.trycloudflare.com
echo   这条 https 链接就是外网可访问的地址。
echo ------------------------------------------------------------
echo   关闭本窗口即停止外网访问。
echo ============================================================
echo.

cloudflared.exe tunnel --url http://127.0.0.1:8000 --no-autoupdate

echo.
echo [隧道已退出]
pause
