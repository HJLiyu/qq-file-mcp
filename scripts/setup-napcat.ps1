param([string]$QQInstallDirectory = 'C:\Program Files\Tencent\QQNT')
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $project '.local\napcat-node'
$archive = Join-Path $project '.local\artifacts\NapCat.Shell.Windows.Node-v4.18.28.zip'
$expectedHash = 'fb64fa3b036ad2df1a5d7c204c482694c20e4b763978c8a4968fd3474c05b4a8'
$sourceUrl = 'https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.28/NapCat.Shell.Windows.Node.zip'

if (-not (Test-Path -LiteralPath (Join-Path $runtime 'index.js'))) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $archive) -Force | Out-Null
    if (-not (Test-Path -LiteralPath $archive)) {
        Invoke-WebRequest -Uri $sourceUrl -OutFile $archive
    }
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) { throw 'NapCat release checksum mismatch. Archive was not executed.' }
    Expand-Archive -LiteralPath $archive -DestinationPath $runtime -Force
}

# v4.18.28 Windows.Node omits these dependencies. Reuse the user's installed QQ files;
# never patch the installed client, stop its processes, or redistribute these binaries.
$versionRoot = Join-Path $QQInstallDirectory 'versions'
$versionDirs = @()
if (Test-Path -LiteralPath $versionRoot) {
    $versionDirs = @(Get-ChildItem -LiteralPath $versionRoot -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+-\d+$' } |
        Sort-Object { [version]($_.Name.Replace('-', '.')) } -Descending)
}
foreach ($dll in @('crypto.dll', 'ssl.dll')) {
    $destination = Join-Path $runtime $dll
    if (Test-Path -LiteralPath $destination) { continue }
    $candidates = @((Join-Path $QQInstallDirectory ('resources\app\' + $dll)))
    $candidates += @($versionDirs | ForEach-Object { Join-Path $_.FullName ('resources\app\' + $dll) })
    $source = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $source) { throw "Missing $dll. Install official QQNT or pass -QQInstallDirectory." }
    Copy-Item -LiteralPath $source -Destination $destination
}
& (Join-Path $project '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'configure-runtime.py')
if ($LASTEXITCODE -ne 0) { throw 'Runtime configuration failed.' }
Write-Output 'Isolated NapCat prepared. Run scripts/start-napcat.ps1.'
