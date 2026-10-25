$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $Program (exit $LASTEXITCODE)" }
}

if (!(Get-Command python -ErrorAction SilentlyContinue)) { throw 'Install Python 3.14 x64 and enable Add Python to PATH.' }
if (!(Get-Command node -ErrorAction SilentlyContinue) -or !(Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw 'Install Node.js 24 LTS, then open a new PowerShell window.'
}
Invoke-Checked 'python' @('-c', 'import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize(''P'')==8, ''Python 3.12+ x64 required''')
Invoke-Checked 'node' @('-e', 'if (parseInt(process.versions.node) < 22) process.exit(1)')
if (!(Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    Invoke-Checked 'python' @('-m', 'venv', '.venv')
}
$venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
Invoke-Checked $venvPython @('-c', 'import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize(''P'')==8, ''Recreate .venv using Python 3.12+ x64''')
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
Invoke-Checked $venvPython @('-m', 'pip', 'install', '-r', 'requirements.txt')
Invoke-Checked $venvPython @('-m', 'playwright', 'install', 'chromium')
Push-Location -LiteralPath (Join-Path $PSScriptRoot 'UI')
try {
    Invoke-Checked 'npm.cmd' @('ci', '--no-audit', '--no-fund')
    Invoke-Checked 'npm.cmd' @('run', 'build')
} finally { Pop-Location }
Invoke-Checked $venvPython @('-c', 'import config_manager; config_manager.load_config(); print(''User data: '' + str(config_manager.CONFIG_DIR))')
Write-Host 'Installation complete. Launch start.bat.'
