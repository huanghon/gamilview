@echo off
chcp 65001 > nul
title Gmail Viewer
cd /d "%~dp0"

if not exist .env (
  copy .env.example .env > nul
  echo.
  echo [*] 首次启动，已生成 .env，请把 APP_ACCESS_TOKEN 改成你自己的密码后保存关闭。
  echo.
  notepad .env
)

echo.
echo ================================================================
echo   Gmail Mail Viewer
echo   Local URL : http://127.0.0.1:8000
echo   Stop      : double-click stop.bat
echo ================================================================
echo.

start "GmailViewer" "%~dp0app.exe"

timeout /t 3 /nobreak > nul
start "" "http://127.0.0.1:8000/"

echo [OK] 已启动，可关闭此窗口（后台继续运行）。
timeout /t 5 > nul
