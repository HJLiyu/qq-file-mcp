$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$runtimeExe = [System.IO.Path]::GetFullPath((Join-Path $project '.local\napcat-node\node.exe'))
# Match the private executable path; a saved PID alone may have been reused.
$targets = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.ExecutablePath -eq $runtimeExe })
if ($targets.Count -eq 0) {
    Write-Output 'Project NapCat is already stopped.'
    return
}
$targetIds = @($targets.ProcessId)
# Stop the supervisor before its worker so it cannot restart the worker.
$parents = @($targets | Where-Object { $_.ParentProcessId -notin $targetIds })
$workers = @($targets | Where-Object { $_.ParentProcessId -in $targetIds })
foreach ($target in @($parents) + @($workers)) {
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($target.ProcessId)"
    if ($current -and $current.ExecutablePath -eq $runtimeExe -and
        $current.CreationDate -eq $target.CreationDate) {
        Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
    }
}
$remaining = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.ExecutablePath -eq $runtimeExe })
if ($remaining.Count -ne 0) { throw 'Some project NapCat processes are still running.' }
Write-Output 'Project NapCat stopped. You can now sign in to desktop QQ.'
