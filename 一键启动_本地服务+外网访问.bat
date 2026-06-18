@echo off
chcp 65001 >nul
title 一键启动：本地服务 + Cloudflare 外网隧道

cd /d "%~dp0"

if not exist "cloudflared.exe" (
    echo [错误] 当前目录找不到 cloudflared.exe
    pause
    exit /b 1
)

if not exist "app.py" (
    echo [错误] 当前目录找不到 app.py
    pause
    exit /b 1
)

echo ============================================================
echo   步骤 1/2 启动本地服务 app.py (端口 8000)
echo ============================================================
start "本地服务 app.py (端口 8000)" cmd /k "chcp 65001 >nul && python app.py"

echo 等待本地服务起来...
timeout /t 4 /nobreak >nul

echo.
echo ============================================================
echo   步骤 2/2 启动 Cloudflare 隧道
echo ============================================================
start "Cloudflare 外网隧道" cmd /k "chcp 65001 >nul && cd /d ""%~dp0"" && cloudflared.exe tunnel --url http://127.0.0.1:8000 --no-autoupdate"

echo.
echo ============================================================
echo   已分别打开两个窗口：
echo     1) 本地服务 app.py
echo     2) Cloudflare 隧道（请在该窗口里找 trycloudflare.com 的外网链接）
echo.
echo   关闭对应窗口即可停止本地服务或外网访问。
echo ============================================================
echo.
pause
