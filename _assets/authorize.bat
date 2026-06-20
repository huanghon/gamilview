@echo off
chcp 65001 > nul
title Gmail 邮箱授权工具
cd /d "%~dp0"

echo ================================================================
echo   Gmail 邮箱授权工具 (免安装 Python)
echo ================================================================
echo.

:input
set /p alias="请输入需要新增授权的邮箱别名 (例如 gmail4): "
if "%alias%"=="" (
  echo [错误] 邮箱别名不能为空！
  echo.
  goto input
)

echo.
echo [*] 正在为 %alias% 启动授权流程...
echo [*] 这将在您的浏览器中打开谷歌授权页面。
echo [*] 请登录您的 Gmail 并按提示勾选“查看您的电子邮件”权限。
echo.

if not exist "%~dp0gmail_authorize.exe" (
  echo [错误] 未在此目录下找到 gmail_authorize.exe！
  echo 请确保本脚本与主程序放置在同一文件夹下。
  echo.
  pause
  exit /b
)

"%~dp0gmail_authorize.exe" authorize %alias% --dir .\gmail_credentials

echo.
echo ================================================================
if %ERRORLEVEL% equ 0 (
  echo [成功] 授权完成！已为账号 %alias% 在 gmail_credentials 目录中生成 token.json。
) else (
  echo [失败] 授权失败，请查看上方具体的错误提示。
)
echo ================================================================
echo.
pause
