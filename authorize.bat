@echo off
setlocal EnableExtensions
title Gmail Authorization Tool
cd /d "%~dp0"

echo ================================================================
echo   Gmail Authorization Tool (no Python install required)
echo ================================================================
echo.

:input
set "alias="
set /p "alias=Enter the Gmail alias to authorize, example gmail4: "
if not defined alias (
  echo [ERROR] Gmail alias cannot be empty.
  echo.
  goto input
)

echo.
echo [*] Starting authorization for %alias%...
echo [*] A Google authorization page will open in your browser.
echo [*] Sign in to Gmail and allow the email read permission when prompted.
echo.

if not exist "%~dp0gmail_authorize.exe" (
  echo [ERROR] gmail_authorize.exe was not found in this folder.
  echo Please put this script and gmail_authorize.exe in the same folder.
  echo.
  pause
  exit /b
)

"%~dp0gmail_authorize.exe" authorize "%alias%" --dir "%~dp0gmail_credentials"
set "auth_exit_code=%ERRORLEVEL%"

echo.
echo ================================================================
if "%auth_exit_code%"=="0" (
  echo [OK] Authorization completed for %alias%.
  echo [OK] Token files were generated under gmail_credentials.
) else (
  echo [FAILED] Authorization failed. Please check the error above.
)
echo ================================================================
echo.
pause
