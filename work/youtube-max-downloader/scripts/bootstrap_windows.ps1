param(
    [switch]$UpdateOnly,
    [switch]$SkipLaunch
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
$UvRoot = Join-Path $RuntimeRoot "uv"
$DenoRoot = Join-Path $RuntimeRoot "deno"
$UvExe = Join-Path $UvRoot "uv.exe"
$DenoExe = Join-Path $DenoRoot "deno.exe"
$UvVersion = "0.12.12"
$DenoVersion = "2.9.5"
$UvSha256 = "3d54912924c36e862c14f427d04f2ed70a99e8001d1c30caa101f6d5711626d5"
$DenoSha256 = "171efab55ac6b9881fd53ee4c20f8bf3bb1340ffc618483746909014db12216a"

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null

$Architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
switch ($Architecture) {
    "X64" {
        $UvAsset = "uv-x86_64-pc-windows-msvc.zip"
        $DenoAsset = "deno-x86_64-pc-windows-msvc.zip"
    }
    "Arm64" { throw "MMM Downloader 0.2.1 currently requires 64-bit Intel/AMD Windows (x64)." }
    default { throw "Unsupported Windows architecture: $Architecture" }
}

function Install-ZippedTool {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedExe,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    $Archive = Join-Path $env:TEMP ("mmm-downloader-" + [Guid]::NewGuid().ToString("N") + ".zip")
    $Staging = "$Destination.staging-" + [Guid]::NewGuid().ToString("N")
    try {
        Write-Host "Downloading $Url"
        Invoke-WebRequest -Uri $Url -OutFile $Archive -UseBasicParsing
        $ActualSha256 = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ActualSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
            throw "Checksum verification failed for $Url"
        }
        New-Item -ItemType Directory -Force -Path $Staging | Out-Null
        Expand-Archive -Path $Archive -DestinationPath $Staging -Force
        $StagedExe = Join-Path $Staging (Split-Path -Leaf $ExpectedExe)
        if (-not (Test-Path $StagedExe)) {
            throw "Downloaded archive does not contain $(Split-Path -Leaf $ExpectedExe)"
        }
        & $StagedExe --version | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Downloaded executable failed its version check: $StagedExe"
        }
        Set-Content `
            -LiteralPath (Join-Path $Staging ".archive-sha256") `
            -Value $ActualSha256 `
            -NoNewline `
            -Encoding ascii
        if (Test-Path $Destination) {
            Remove-Item -Recurse -Force $Destination
        }
        Move-Item -LiteralPath $Staging -Destination $Destination
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $Archive
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Staging
    }
}

function Test-ZippedTool {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedExe,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    if (-not (Test-Path $ExpectedExe)) { return $false }
    $Marker = Join-Path (Split-Path -Parent $ExpectedExe) ".archive-sha256"
    if (-not (Test-Path $Marker)) { return $false }
    try {
        $RecordedSha256 = (Get-Content -LiteralPath $Marker -Raw).Trim().ToLowerInvariant()
        if ($RecordedSha256 -ne $ExpectedSha256.ToLowerInvariant()) { return $false }
        & $ExpectedExe --version | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

if (-not (Test-ZippedTool -ExpectedExe $UvExe -ExpectedSha256 $UvSha256)) {
    Install-ZippedTool `
        -Url "https://github.com/astral-sh/uv/releases/download/$UvVersion/$UvAsset" `
        -Destination $UvRoot `
        -ExpectedExe $UvExe `
        -ExpectedSha256 $UvSha256
}

if (-not (Test-ZippedTool -ExpectedExe $DenoExe -ExpectedSha256 $DenoSha256)) {
    Install-ZippedTool `
        -Url "https://github.com/denoland/deno/releases/download/v$DenoVersion/$DenoAsset" `
        -Destination $DenoRoot `
        -ExpectedExe $DenoExe `
        -ExpectedSha256 $DenoSha256
}

$env:UV_CACHE_DIR = Join-Path $RuntimeRoot "uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeRoot "python"
$env:UV_PYTHON_INSTALL_REGISTRY = "0"
$env:DENO_DIR = Join-Path $RuntimeRoot "deno-cache"
$env:PATH = "$DenoRoot;$env:PATH"

Push-Location $ProjectRoot
try {
    if ($UpdateOnly) {
        Write-Host "Updating yt-dlp and its YouTube support components..."
        & $UvExe lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
        if ($LASTEXITCODE -ne 0) { throw "uv lock failed with code $LASTEXITCODE" }
    }

    Write-Host "Preparing the private app environment..."
    & $UvExe sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with code $LASTEXITCODE" }

    & $UvExe run --no-sync python (Join-Path $PSScriptRoot "verify_runtime.py")
    if ($LASTEXITCODE -ne 0) { throw "Runtime verification failed with code $LASTEXITCODE" }

    if (-not $SkipLaunch -and -not $UpdateOnly) {
        & $UvExe run --no-sync mmm-downloader-gui
        if ($LASTEXITCODE -ne 0) { throw "Application exited with code $LASTEXITCODE" }
    }
    elseif ($UpdateOnly) {
        Write-Host "Download engine updated successfully."
    }
}
finally {
    Pop-Location
}
