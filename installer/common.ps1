Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $Program (exit $LASTEXITCODE)" }
}

function Get-Download {
    param([string]$Url, [string]$Destination)
    if (![Uri]::IsWellFormedUriString($Url, [UriKind]::Absolute) -or !$Url.StartsWith('https://')) {
        throw 'Only HTTPS downloads are supported.'
    }
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile "$Destination.part" -TimeoutSec 180 -Headers @{'User-Agent'='kid-installer'}
            Move-Item -LiteralPath "$Destination.part" -Destination $Destination -Force
            return
        } catch {
            if ($attempt -eq 3) { throw }
            Start-Sleep -Seconds $attempt
        }
    }
}

function Assert-Hash {
    param([string]$Path, [string]$Expected)
    if ($Expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) {
        throw "SHA256 verification failed: $([IO.Path]::GetFileName($Path)). Retry the installer."
    }
}

function Write-AtomicJson {
    param([string]$Path, $Value)
    $Value | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath "$Path.tmp" -Encoding UTF8
    if (Test-Path -LiteralPath $Path) {
        [IO.File]::Replace("$Path.tmp", $Path, "$Path.previous")
    } else { [IO.File]::Move("$Path.tmp", $Path) }
}

function Initialize-Runtime {
    param([string]$Root, [string]$Manifest)
    $spec = Get-Content -Raw -LiteralPath $Manifest | ConvertFrom-Json
    if ($spec.schema -ne 1) { throw 'Unsupported runtime manifest.' }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $uvDir = Join-Path $Root "uv-$($spec.uv)"
    $uv = Join-Path $uvDir 'uv.exe'
    if (!(Test-Path -LiteralPath (Join-Path $uvDir '.ready'))) {
        $zip = Join-Path $Root 'uv.zip'
        $url = "https://github.com/astral-sh/uv/releases/download/$($spec.uv)/uv-x86_64-pc-windows-msvc.zip"
        Get-Download $url $zip
        Get-Download "$url.sha256" "$zip.sha256"
        Assert-Hash $zip ((Get-Content -Raw -LiteralPath "$zip.sha256").Trim() -split '\s+')[0]
        Expand-Archive -LiteralPath $zip -DestinationPath $uvDir -Force
        Invoke-Checked $uv @('--version')
        Set-Content -LiteralPath (Join-Path $uvDir '.ready') -Value $spec.uv
    }
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Root 'python'
    $env:UV_PYTHON_DOWNLOADS = 'automatic'
    Invoke-Checked $uv @('python', 'install', $spec.python)
    $python = & $uv python find --managed-python $spec.python
    if ($LASTEXITCODE -ne 0) { throw 'Managed Python could not be located.' }
    $nodeDir = Join-Path $Root "node-v$($spec.node)-win-x64"
    if (!(Test-Path -LiteralPath (Join-Path $nodeDir '.ready'))) {
        $filename = "node-v$($spec.node)-win-x64.zip"
        $zip = Join-Path $Root $filename
        $url = "https://nodejs.org/dist/v$($spec.node)"
        Get-Download "$url/$filename" $zip
        Get-Download "$url/SHASUMS256.txt" (Join-Path $Root 'node-sums.txt')
        $line = @(Get-Content -LiteralPath (Join-Path $Root 'node-sums.txt') | Where-Object { $_ -match ('\s+' + [regex]::Escape($filename) + '$') })
        if ($line.Count -ne 1) { throw 'Node checksum is missing.' }
        Assert-Hash $zip ($line[0] -split '\s+')[0]
        Expand-Archive -LiteralPath $zip -DestinationPath $Root -Force
        Invoke-Checked (Join-Path $nodeDir 'node.exe') @('--version')
        Invoke-Checked (Join-Path $nodeDir 'npm.cmd') @('--version')
        Set-Content -LiteralPath (Join-Path $nodeDir '.ready') -Value $spec.node
    }
    Invoke-Checked (Join-Path $nodeDir 'node.exe') @('--version')
    $env:PATH = "$nodeDir;$env:PATH"
    return @{ Python = ([string]$python).Trim(); Uv = $uv; Node = $nodeDir }
}

function Test-WebViewRuntime {
    foreach ($key in @('HKCU:\Software\Microsoft\EdgeUpdate\Clients', 'HKLM:\Software\Microsoft\EdgeUpdate\Clients', 'HKLM:\Software\WOW6432Node\Microsoft\EdgeUpdate\Clients')) {
        foreach ($client in @(Get-ChildItem -LiteralPath $key -ErrorAction SilentlyContinue)) {
            $value = Get-ItemProperty -LiteralPath $client.PSPath
            if ($value.PSObject.Properties['name'] -and $value.PSObject.Properties['pv'] -and $value.name -match 'WebView2' -and $value.pv -ne '0.0.0.0') { return $true }
        }
    }
    return $false
}

function Initialize-WebView {
    param([string]$Root)
    if (Test-WebViewRuntime) { return }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $bootstrapper = Join-Path $Root 'MicrosoftEdgeWebview2Setup.exe'
    Get-Download 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' $bootstrapper
    $signature = Get-AuthenticodeSignature -LiteralPath $bootstrapper
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw 'WebView2 installer signature is invalid.' }
    $process = Start-Process -FilePath $bootstrapper -ArgumentList '/silent /install' -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0 -or !(Test-WebViewRuntime)) { throw 'WebView2 installation did not complete. Retry setup, or install Microsoft Edge WebView2 Runtime from Microsoft.' }
}

function Initialize-VisualCpp {
    param([string]$Root)
    $key = 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
    $runtime = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
    if ($runtime -and $runtime.PSObject.Properties['Installed'] -and $runtime.Installed -eq 1) { return }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $installer = Join-Path $Root 'vc_redist.x64.exe'
    Get-Download 'https://aka.ms/vs/17/release/vc_redist.x64.exe' $installer
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw 'Visual C++ installer signature is invalid.' }
    Write-Host 'Microsoft Visual C++ Runtime is missing. Windows will ask for administrator confirmation.'
    $process = Start-Process -FilePath $installer -ArgumentList '/install /quiet /norestart' -Verb RunAs -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -eq 3010) { throw 'Visual C++ Runtime installed. Restart Windows, then run kid setup again.' }
    if ($process.ExitCode -ne 0) { throw "Visual C++ Runtime installation failed (exit $($process.ExitCode)). Retry setup." }
}
