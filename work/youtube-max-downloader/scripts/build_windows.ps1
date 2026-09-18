$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Bootstrap = Join-Path $PSScriptRoot "bootstrap_windows.ps1"
& $Bootstrap -SkipLaunch
if ($LASTEXITCODE -ne 0) { throw "Bootstrap failed with code $LASTEXITCODE" }

$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
$UvExe = Join-Path $RuntimeRoot "uv\uv.exe"
$DenoRoot = Join-Path $RuntimeRoot "deno"
$DenoExe = Join-Path $DenoRoot "deno.exe"
$IconPng = Join-Path $ProjectRoot "assets\mmm_downloader.png"
$IconIco = Join-Path $ProjectRoot "assets\mmm_downloader.ico"
$ThemeQss = Join-Path $ProjectRoot "assets\mmm_downloader.qss"
$ChevronSvg = Join-Path $ProjectRoot "assets\chevron_down.svg"
$CheckSvg = Join-Path $ProjectRoot "assets\check.svg"
$env:UV_CACHE_DIR = Join-Path $RuntimeRoot "uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeRoot "python"
$env:UV_PYTHON_INSTALL_REGISTRY = "0"
$env:DENO_DIR = Join-Path $RuntimeRoot "deno-cache"
$env:PATH = "$DenoRoot;$env:PATH"
$env:QT_API = "pyside6"

Push-Location $ProjectRoot
try {
    & $UvExe sync --locked --extra build --extra dev
    if ($LASTEXITCODE -ne 0) { throw "Could not install build dependencies" }

    & $UvExe run --no-sync pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed with code $LASTEXITCODE" }

    $Arguments = @(
        "run", "--no-sync", "pyinstaller",
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "MMM Downloader",
        "--icon", $IconIco,
        "--paths", "src",
        "--collect-all", "yt_dlp",
        "--collect-all", "yt_dlp_ejs",
        "--collect-all", "imageio_ffmpeg",
        "--hidden-import", "keyring.backends.Windows",
        "--hidden-import", "win32ctypes.pywin32.win32cred",
        "--hidden-import", "win32ctypes.pywin32.pywintypes",
        "--hidden-import", "PySide6.support.deprecated",
        "--exclude-module", "tkinter",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        "--exclude-module", "PySide2",
        "--add-data", "${IconPng};assets",
        "--add-data", "${ThemeQss};assets",
        "--add-data", "${ChevronSvg};assets",
        "--add-data", "${CheckSvg};assets",
        "--add-binary", "$DenoExe;.",
        "launcher.py"
    )
    & $UvExe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with code $LASTEXITCODE" }

    $DistFolder = Join-Path $ProjectRoot "dist\MMM Downloader"
    Copy-Item -Force (Join-Path $ProjectRoot "README.md") $DistFolder
    Copy-Item -Force (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md") $DistFolder

    $ZipPath = Join-Path $ProjectRoot "dist\MMM-Downloader-Windows.zip"
    Remove-Item -Force -ErrorAction SilentlyContinue $ZipPath
    Compress-Archive -Path "$DistFolder\*" -DestinationPath $ZipPath -CompressionLevel Optimal
    Write-Host ""
    Write-Host "Ready: $ZipPath" -ForegroundColor Green
}
finally {
    Pop-Location
}
