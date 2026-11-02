@echo off
setlocal
cd /d "%~dp0"

set "PYW=pythonw.exe"
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" set "PYW=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"

if not exist "voice_assistant.py" (
  echo [ERROR] voice_assistant.py was not found in this folder.
  pause
  exit /b 1
)

rem Launch directly without CDP checks or a watchdog.
start "WorkBuddy Voice Assistant" /D "%~dp0" "%PYW%" "voice_assistant.py" %*
if errorlevel 1 (
  echo [ERROR] Failed to start the voice assistant.
  pause
  exit /b 1
)
exit /b 0
