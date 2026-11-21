# Build the Vanta Windows app (Phase 4).
# Run from the repo root on Windows PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# Output: dist\Vanta\Vanta.exe (+ Vanta-<version>-Windows.zip next to it).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

$Version = (Get-Content VERSION -Raw).Trim()
Write-Host "Building Vanta v$Version for Windows..."

if (-not (Test-Path .venv)) {
    python -m venv .venv
}
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
pip install -r requirements-dev.txt
pip install pyinstaller

$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed — build aborted." }

pyinstaller --noconfirm vanta.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$ZipName = "Vanta-$Version-Windows.zip"
if (Test-Path "dist\$ZipName") { Remove-Item "dist\$ZipName" }
Compress-Archive -Path dist\Vanta -DestinationPath "dist\$ZipName"
$Hash = (Get-FileHash "dist\$ZipName" -Algorithm SHA256).Hash.ToLower()
"$Hash  $ZipName" | Out-File -Encoding ascii "dist\$ZipName.sha256"
Write-Host "Built dist\Vanta\Vanta.exe"
Write-Host "Zip: dist\$ZipName"
Write-Host "SHA-256: $Hash"
