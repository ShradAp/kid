$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root 'dist'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (!(Test-Path -LiteralPath $compiler)) { throw 'Windows .NET Framework 4.x C# compiler is required.' }
$loader = Join-Path $root 'installer/loader.ps1'
$source = Join-Path $root 'installer/Launcher.cs'
$exe = Join-Path $output 'kid-setup.exe'
& $compiler /nologo /target:winexe /platform:x64 /reference:System.Windows.Forms.dll "/resource:$loader,loader.ps1" "/out:$exe" $source
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
Write-Host "Built $exe"
