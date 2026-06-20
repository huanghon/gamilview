@echo off
chcp 65001 > nul
echo 正在关闭 Gmail Viewer ...
taskkill /f /im app.exe > nul 2>&1
echo 已关闭。
timeout /t 2 > nul
