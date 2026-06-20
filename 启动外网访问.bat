@echo off
setlocal EnableExtensions
title Cloudflare tunnel - http://127.0.0.1:8000

cd /d "%~dp0"

if not exist "cloudflared.exe" (
    echo [ERROR] cloudflared.exe was not found in this folder.
    echo Path: %CD%
    pause
    exit /b 1
)

echo ============================================================
echo   Starting Cloudflare Tunnel...
echo   Local URL: http://127.0.0.1:8000
echo ------------------------------------------------------------
echo   Wait a few seconds. A URL like this will appear:
echo     https://xxxx-xxxx-xxxx.trycloudflare.com
echo   That HTTPS URL is the public access address.
echo ------------------------------------------------------------
echo   Close this window to stop public access.
echo ============================================================
echo.

cloudflared.exe tunnel --url http://127.0.0.1:8000 --no-autoupdate

echo.
echo [INFO] Tunnel exited.
pause
