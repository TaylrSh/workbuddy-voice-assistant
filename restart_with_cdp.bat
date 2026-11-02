@echo off
setlocal
rem WARNING: this closes all WorkBuddy.exe processes before restarting.
rem Run it only after saving your work in WorkBuddy.

echo [1/3] Closing WorkBuddy...
taskkill /IM WorkBuddy.exe /F >nul 2>&1
timeout /t 5 /nobreak >nul

echo [2/3] Starting WorkBuddy with CDP on 127.0.0.1:9333...
set WORKBUDDY_REMOTE_DEBUGGING_PORT=9333
set CODEBUDDY_CONFIG_DIR=%USERPROFILE%\.workbuddy
set "WORKBUDDY_EXE=D:\Program Files (x86)\WorkBuddy\WorkBuddy.exe"
if not exist "%WORKBUDDY_EXE%" set "WORKBUDDY_EXE=C:\Program Files (x86)\WorkBuddy\WorkBuddy.exe"
if not exist "%WORKBUDDY_EXE%" set "WORKBUDDY_EXE=C:\Program Files\WorkBuddy\WorkBuddy.exe"
start "" "%WORKBUDDY_EXE%"

echo [3/3] Waiting for CDP...
set /a tries=0
:wait
timeout /t 2 /nobreak >nul
set /a tries+=1
curl -s -m 2 http://127.0.0.1:9333/json/version >nul 2>&1 && goto ready
if %tries% lss 30 goto wait
echo [ERROR] CDP did not become ready in time.
exit /b 1

:ready
echo CDP is ready on 127.0.0.1:9333.
exit /b 0
