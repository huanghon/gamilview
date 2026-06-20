@echo off
setlocal EnableExtensions
title Start local service + Cloudflare tunnel

cd /d "%~dp0"

if not exist "cloudflared.exe" (
    echo [ERROR] cloudflared.exe was not found in this folder.
    pause
    exit /b 1
)

if not exist "app.py" (
    echo [ERROR] app.py was not found in this folder.
    pause
    exit /b 1
)

echo ============================================================
echo   Step 1/2: Start local service app.py on port 8000
echo ============================================================
start "Local service app.py port 8000" cmd /k "python app.py"

echo Waiting for the local service to start...
timeout /t 4 /nobreak >nul

echo.
echo ============================================================
echo   Step 2/2: Start Cloudflare tunnel
echo ============================================================
start "Cloudflare tunnel" cmd /k "cd /d ""%~dp0"" && cloudflared.exe tunnel --url http://127.0.0.1:8000 --no-autoupdate"

echo.
echo ============================================================
echo   Two windows have been opened:
echo     1) Local service app.py
echo     2) Cloudflare tunnel, look for the trycloudflare.com URL there.
echo.
echo   Close the corresponding window to stop the service or tunnel.
echo ============================================================
echo.
pause
