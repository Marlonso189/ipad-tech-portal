# Build the Windows desktop folder and, when Inno Setup 6 is installed, the installer.
# Run from any directory:  powershell -ExecutionPolicy Bypass -File desktop\build.ps1
param(
    [switch]$RequireInstaller
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (Get-Command py -ErrorAction SilentlyContinue) {
    $Py = "py"
    $PyPrefix = @("-3.12")
} else {
    $Py = "python"
    $PyPrefix = @()
}

& $Py @PyPrefix -m pip install --upgrade pip
& $Py @PyPrefix -m pip install -r requirements.txt -r requirements-desktop.txt
& $Py @PyPrefix -m PyInstaller --noconfirm --clean desktop\ipad-tech-portal.spec

if (-not (Test-Path "dist\ipad-tech-portal\ipad-tech-portal.exe")) {
    throw "PyInstaller did not produce dist\ipad-tech-portal\ipad-tech-portal.exe"
}

$candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if ($iscc) {
    & $iscc "desktop\installer.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE"
    }
    Write-Host "Installer: desktop\output\ipad-tech-portal-setup.exe"
} elseif ($RequireInstaller) {
    throw "Inno Setup 6 is not installed. The application folder is in dist\ipad-tech-portal."
} else {
    Write-Warning "Inno Setup 6 is not installed. The application folder is in dist\ipad-tech-portal. Install Inno Setup and run this script again to build the installer."
}
