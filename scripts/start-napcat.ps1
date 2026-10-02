param([switch]$OpenWebUI)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $project '.local\napcat-node'
$entry = Join-Path $runtime 'index.js'
if (-not (Test-Path -LiteralPath $entry)) { throw 'Run scripts/setup.ps1 -WithNapCat first.' }
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 6099 -ErrorAction SilentlyContinue)
if ($listeners.Count -eq 0) {
    $process = Start-Process -FilePath (Join-Path $runtime 'node.exe') -ArgumentList 'index.js' `
        -WorkingDirectory $runtime -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtime 'stdout.log') `
        -RedirectStandardError (Join-Path $runtime 'stderr.log') -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $project '.local\napcat-process.pid')
    Write-Output ('Started NapCat process ' + $process.Id)
} else {
    Write-Output 'Port 6099 already has a listener; no duplicate process was started.'
}
if ($OpenWebUI) {
    $configuration = Get-Content -LiteralPath (Join-Path $runtime 'napcat\config\webui.json') | ConvertFrom-Json
    $url = 'http://127.0.0.1:6099/webui?token=' + [uri]::EscapeDataString($configuration.token)
    Start-Process $url
}
