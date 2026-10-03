$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
$envFile = Join-Path $project '.env'
if (-not (Test-Path -LiteralPath $envFile)) { throw 'Create .env before registering the tool.' }
# The token stays inside the local .env, never in a command line or Codex's global config.
codex mcp add qq-files --env PYTHONUTF8=1 -- $python -m qq_file_mcp --env-file $envFile serve
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP registration failed.' }
Write-Output 'Registered qq-files. Reload the Codex MCP connection or open a new chat.'
