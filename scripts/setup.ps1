param([switch]$WithNapCat, [string]$QQInstallDirectory = 'C:\Program Files\Tencent\QQNT')
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $project
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if ($WithNapCat) {
    & (Join-Path $PSScriptRoot 'setup-napcat.ps1') -QQInstallDirectory $QQInstallDirectory
}
Write-Output 'Setup complete. Configure .env, start NapCat, then run doctor.'
