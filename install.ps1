param([switch]$ManagedRuntime)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'installer/common.ps1')

$lock = $null
try {
    $lock = [IO.File]::Open((Join-Path $PSScriptRoot '.install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    if (![Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Windows x64 and 64-bit PowerShell are required.' }
    $runtime = $null
    if ($ManagedRuntime -or !(Get-Command python -ErrorAction SilentlyContinue) -or !(Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        $runtimeRoot = if ($env:KID_RUNTIME_HOME) { $env:KID_RUNTIME_HOME } else { Join-Path $PSScriptRoot '.runtime' }
        $runtime = Initialize-Runtime $runtimeRoot (Join-Path $PSScriptRoot 'installer/runtime.json')
        $python = $runtime.Python
    } else { $python = (Get-Command python).Source }
    Invoke-Checked $python @('-c', 'import sys,struct; assert (3,12) <= sys.version_info < (3,15) and struct.calcsize(''P'')==8, ''Python 3.12-3.14 x64 required; retry with -ManagedRuntime''')
    Invoke-Checked 'node' @('-e', 'if (parseInt(process.versions.node) < 22) process.exit(1)')
    Initialize-WebView (Join-Path $PSScriptRoot '.runtime')
    Initialize-VisualCpp (Join-Path $PSScriptRoot '.runtime')
    $venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    $valid = $false
    if (Test-Path -LiteralPath $venvPython) {
        & $venvPython -c 'import sys,struct; assert (3,12) <= sys.version_info < (3,15) and struct.calcsize(''P'')==8' 2>$null
        $valid = $LASTEXITCODE -eq 0
    }
    if (!$valid) {
        if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.venv')) {
            Move-Item -LiteralPath (Join-Path $PSScriptRoot '.venv') -Destination (Join-Path $PSScriptRoot ('.venv.failed-' + [guid]::NewGuid().ToString('N')))
        }
        if ($runtime) { Invoke-Checked $runtime.Uv @('venv', '--seed', '--python', $python, '.venv') }
        else { Invoke-Checked $python @('-m', 'venv', '.venv') }
    }
    Invoke-Checked $venvPython @('-c', 'import sys,struct; assert (3,12) <= sys.version_info < (3,15) and struct.calcsize(''P'')==8, ''Recreate .venv using Python 3.12-3.14 x64; Python 3.15+ is incompatible with pinned dependencies''')
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '-r', 'requirements.txt')
    Invoke-Checked $venvPython @('-m', 'pip', 'check')
    Invoke-Checked $venvPython @('-m', 'playwright', 'install', 'chromium')
    Invoke-Checked $venvPython @('-c', 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); browser=p.chromium.launch(headless=True); browser.close(); p.stop(); import webview, requests, socks, curl_cffi, lancedb, fastembed, sounddevice')
    Push-Location -LiteralPath (Join-Path $PSScriptRoot 'UI')
    try {
        Invoke-Checked 'npm.cmd' @('ci', '--no-audit', '--no-fund')
        if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'UI/.next')) {
            Move-Item -LiteralPath (Join-Path $PSScriptRoot 'UI/.next') -Destination (Join-Path $PSScriptRoot ('UI/.next.failed-' + [guid]::NewGuid().ToString('N')))
        }
        Invoke-Checked 'npm.cmd' @('run', 'build')
    } finally { Pop-Location }
    Invoke-Checked $venvPython @('-c', 'import config_manager; config_manager.load_config(); print(''User data: '' + str(config_manager.CONFIG_DIR))')
    Write-AtomicJson (Join-Path $PSScriptRoot '.install-state.json') @{ schema=1; node=(Split-Path -Parent (Get-Command node).Source); complete=$true }
    Write-Host 'Installation complete. Launch start.bat.'
} finally { if ($lock) { $lock.Dispose() } }
