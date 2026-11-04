@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -ManagedRuntime
if errorlevel 1 (
  echo Installation failed. You can run this file again.
  pause
  exit /b 1
)
call "%~dp0start.bat"
