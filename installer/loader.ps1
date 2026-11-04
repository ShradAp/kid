param([string]$ReleaseTag = '', [switch]$LaunchOnly, [switch]$NoLaunch, [switch]$Rollback)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$repository = 'ShradAp/kid'
$root = Join-Path $env:LOCALAPPDATA 'kid-launcher'
$versions = Join-Path $root 'versions'
$currentFile = Join-Path $root 'current.json'
$lock = $null
$transcript = $false

function Download([string]$Url, [string]$Path) {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile "$Path.part" -TimeoutSec 180 -Headers @{'User-Agent'='kid-loader'}
            Move-Item -LiteralPath "$Path.part" -Destination $Path -Force
            return
        } catch { if ($attempt -eq 3) { throw }; Start-Sleep -Seconds $attempt }
    }
}

function Read-Current {
    if (!(Test-Path -LiteralPath $currentFile)) { throw 'No completed installation. Run the installer first.' }
    $state = Get-Content -Raw -LiteralPath $currentFile | ConvertFrom-Json
    if ($state.schema -ne 1 -or $state.directory -notmatch '^[a-f0-9]{32}$') { throw 'Invalid installation pointer.' }
    return $state
}

function Start-Kid($State) {
    $directory = Join-Path $versions $State.directory
    $python = Join-Path $directory '.venv/Scripts/pythonw.exe'
    $statePath = Join-Path $directory '.install-state.json'
    if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $statePath)) { throw 'Incomplete installation. Run the installer again.' }
    $installed = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    if (!$installed.complete) { throw 'Installation has not completed.' }
    $env:PATH = "$($installed.node);$env:PATH"
    Start-Process -FilePath $python -ArgumentList ('"' + (Join-Path $directory 'WEB_UI.pyw') + '"') -WorkingDirectory $directory -WindowStyle Hidden
}

try {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Windows x64 with 64-bit PowerShell is required.' }
    New-Item -ItemType Directory -Force -Path $versions | Out-Null
    $lock = [IO.File]::Open((Join-Path $root 'loader.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    Start-Transcript -LiteralPath (Join-Path $root 'loader.log') -Append | Out-Null
    $transcript = $true
    if ($Rollback) {
        $previous = "$currentFile.previous"
        if (!(Test-Path -LiteralPath $previous)) { throw 'No previous version available.' }
        $state = Get-Content -Raw -LiteralPath $previous | ConvertFrom-Json
        if ($state.directory -notmatch '^[a-f0-9]{32}$' -or !(Test-Path -LiteralPath (Join-Path $versions "$($state.directory)/.install-state.json"))) { throw 'Previous installation is incomplete.' }
        Copy-Item -LiteralPath $previous -Destination "$currentFile.tmp" -Force
        [IO.File]::Replace("$currentFile.tmp", $currentFile, "$currentFile.rollback")
        Start-Kid (Read-Current)
        return
    }
    if ($LaunchOnly) { Start-Kid (Read-Current); return }
    $endpoint = "https://api.github.com/repos/$repository/releases/latest"
    if ($ReleaseTag) {
        if ($ReleaseTag -notmatch '^v\d+\.\d+\.\d+$') { throw 'Expected a stable version tag such as v1.0.0.' }
        $endpoint = "https://api.github.com/repos/$repository/releases/tags/$ReleaseTag"
    }
    $release = Invoke-RestMethod -Uri $endpoint -TimeoutSec 30 -Headers @{'User-Agent'='kid-loader'}
    if ($release.draft -or $release.prerelease -or $release.tag_name -notmatch '^v\d+\.\d+\.\d+$') { throw 'No supported stable release available.' }
    $tag = $release.tag_name
    $source = @($release.assets | Where-Object name -eq 'kid-source.zip')
    $sums = @($release.assets | Where-Object name -eq 'SHA256SUMS')
    if ($source.Count -ne 1 -or $sums.Count -ne 1) { throw 'Release is not ready: kid-source.zip / SHA256SUMS missing. Try again after the release build finishes.' }
    if (Test-Path -LiteralPath $currentFile) {
        $current = Read-Current
        if ($current.tag -eq $tag -and $NoLaunch) {
            Write-Host 'This version is already installed. Close kid through the tray and reopen the desktop shortcut.'
            Read-Host 'Press Enter to close' | Out-Null
            return
        }
    }
    # A new directory for EVERY attempt. Never activate an incomplete version,
    # and never move an existing virtualenv (its scripts contain absolute paths).
    $id = [guid]::NewGuid().ToString('N')
    $directory = Join-Path $versions $id
    New-Item -ItemType Directory -Path $directory | Out-Null
    $zip = Join-Path $root "$id.zip"
    $sumPath = Join-Path $root "$id.sums"
    Write-Host "Downloading $tag ..."
    Download $source[0].browser_download_url $zip
    Download $sums[0].browser_download_url $sumPath
    $lines = @(Get-Content -LiteralPath $sumPath | Where-Object { $_ -match '^[a-fA-F0-9]{64}\s+\*?kid-source\.zip$' })
    if ($lines.Count -ne 1 -or (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne ($lines[0] -split '\s+')[0]) { throw 'Source archive checksum failed. Run the installer again.' }
    # Validate every member before extraction (no traversal, absolute paths or symlinks).
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName -match '(^[/\\]|:|(^|[/\\])\.\.([/\\]|$))' -or (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw 'Unsafe source archive entry.' }
        }
    } finally { $archive.Dispose() }
    Expand-Archive -LiteralPath $zip -DestinationPath $directory
    foreach ($file in @('install.ps1', 'installer/runtime.json', 'desktop_app.py', 'requirements.txt', 'UI/package-lock.json')) {
        if (!(Test-Path -LiteralPath (Join-Path $directory $file))) { throw "Release is missing $file" }
    }
    $env:KID_RUNTIME_HOME = Join-Path $root 'runtime'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $directory 'install.ps1') -ManagedRuntime
    if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Previous version is still available. Run this installer again to retry.' }
    $statePath = Join-Path $directory '.install-state.json'
    if (!(Test-Path -LiteralPath $statePath)) { throw 'Release installer did not produce a completion marker.' }
    $installation = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    if ($installation.schema -ne 1 -or $installation.complete -ne $true) { throw 'Release installation is incomplete.' }
    @{schema=1; tag=$tag; repository=$repository} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $directory '.release.json') -Encoding UTF8
    # Keep a permanent loader, independent of temp EXE extraction and release folders.
    $loaderPath = Join-Path $root 'loader.ps1'
    # Upgrade the loader too. Release assets are verified before executing any
    # release code; its loader keeps the same command-line/schema contract.
    $newLoader = Join-Path $directory 'installer/loader.ps1'
    Copy-Item -LiteralPath $newLoader -Destination "$loaderPath.new" -Force
    if (Test-Path -LiteralPath $loaderPath) { [IO.File]::Replace("$loaderPath.new", $loaderPath, "$loaderPath.previous") }
    else { [IO.File]::Move("$loaderPath.new", $loaderPath) }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'kid.lnk'))
    $shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
    $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $loaderPath + '" -LaunchOnly'
    $shortcut.WorkingDirectory = $root
    $shortcut.Save()
    @{schema=1; tag=$tag; directory=$id} | ConvertTo-Json | Set-Content -LiteralPath "$currentFile.tmp" -Encoding UTF8
    if (Test-Path -LiteralPath $currentFile) { [IO.File]::Replace("$currentFile.tmp", $currentFile, "$currentFile.previous") }
    else { [IO.File]::Move("$currentFile.tmp", $currentFile) }
    Write-Host "Installed $tag successfully. Use the kid desktop shortcut." -ForegroundColor Green
    if (!$NoLaunch) { Start-Kid (Read-Current) }
    else { Write-Host 'Close kid completely, including the tray icon, then reopen it from the desktop shortcut.'; Read-Host 'Press Enter to close' | Out-Null }
} catch {
    Write-Host "kid installer: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Log: $root\loader.log. Retry is safe; completed versions and personal settings are preserved."
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
} finally {
    if ($transcript) { Stop-Transcript | Out-Null }
    if ($lock) { $lock.Dispose() }
}
