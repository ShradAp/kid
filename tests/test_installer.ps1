$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
# Parse all scripts with Windows PowerShell 5.1, the actual bootstrap runtime.
foreach ($relative in @('install.ps1', 'installer/common.ps1', 'installer/loader.ps1', 'scripts/build_installer.ps1')) {
    $tokens = $null
    $errors = $null
    [Management.Automation.Language.Parser]::ParseFile((Join-Path $root $relative), [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count) { throw ($errors | Out-String) }
}
. (Join-Path $root 'installer/common.ps1')
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('kid-installer-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $file = Join-Path $temporary 'current.json'
    Write-AtomicJson $file @{schema=1; directory='first'}
    Write-AtomicJson $file @{schema=1; directory='second'}
    if ((Get-Content -Raw -LiteralPath $file | ConvertFrom-Json).directory -ne 'second') { throw 'Pointer was not committed.' }
    if ((Get-Content -Raw -LiteralPath "$file.previous" | ConvertFrom-Json).directory -ne 'first') { throw 'Rollback pointer lost.' }
    $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    Assert-Hash $file $hash
    $rejected = $false
    try { Assert-Hash $file ('0' * 64) } catch { $rejected = $true }
    if (!$rejected) { throw 'Invalid hash accepted.' }
    # Exclusive lock is released without a stale PID file recovery procedure.
    $handle = [IO.File]::Open((Join-Path $temporary 'lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $blocked = $false
    try { $other = [IO.File]::Open((Join-Path $temporary 'lock'), 'OpenOrCreate', 'ReadWrite', 'None'); $other.Dispose() } catch { $blocked = $true }
    $handle.Dispose()
    if (!$blocked) { throw 'Concurrent installer was allowed.' }
    $handle = [IO.File]::Open((Join-Path $temporary 'lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $handle.Dispose()
    Write-Host 'Installer regression checks passed.'
} finally {
    # Only delete the exact test directory constructed under the temporary root.
    $resolved = [IO.Path]::GetFullPath($temporary)
    if (!(Split-Path -Leaf $resolved).StartsWith('kid-installer-test-') -or !(Split-Path -Parent $resolved).Equals([IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe cleanup path.' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
