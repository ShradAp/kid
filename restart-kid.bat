@echo off
setlocal
set "KID_RESTART_ROOT=%~dp0"
set "KID_RESTART_DELAY=%~1"
if not defined KID_RESTART_DELAY set "KID_RESTART_DELAY=15"
powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command "$r=$env:KID_RESTART_ROOT; $d=[int]$env:KID_RESTART_DELAY; if($d -lt 0 -or $d -gt 600){exit 2}; $q=[char]34; $c=$q+(Join-Path $r '.venv\Scripts\pythonw.exe')+$q+' '+$q+(Join-Path $r 'scripts\restart_kid.py')+$q+' --delay '+$d; $p=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$c;CurrentDirectory=$r}; if($p.ReturnValue -ne 0){exit 1}"
exit /b %errorlevel%
