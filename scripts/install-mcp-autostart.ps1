# Run once after installing Python dependencies and Redis sign-in startup.
[CmdletBinding()]
param(
    [string]$Distro = 'Ubuntu',
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
)
$ErrorActionPreference = 'Stop'
$taskName = 'ProjectMemory-MCP'
$worker = Join-Path $PSScriptRoot 'keep-mcp-running.ps1'
$repo = Split-Path -Parent $PSScriptRoot
if ($Distro -and $Distro -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Invalid WSL distribution name.' }
if (-not (Test-Path -LiteralPath (Join-Path $repo '.venv\Scripts\python.exe'))) {
    throw 'Install this repository virtual environment and dependencies first.'
}
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -notlike 'Project memory MCP:*') {
    throw "The task $taskName already exists and is not owned by this installer."
}
$taskAction = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$worker`" -Distro `"$Distro`" -Port $Port" `
    -WorkingDirectory $repo
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$definition = New-ScheduledTask -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal `
    -Settings $taskSettings -Description "Project memory MCP: shared warm HTTP server on 127.0.0.1:$Port (sign-in startup)."
Register-ScheduledTask -TaskName $taskName -InputObject $definition -Force | Out-Null
if ($existing -and $existing.State -eq 'Running') {
    $oldWorkers = @(Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($worker) } |
        ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    Stop-ScheduledTask -TaskName $taskName
    foreach ($oldWorker in $oldWorkers) {
        if (-not $oldWorker.WaitForExit(30000)) { throw 'The previous MCP worker did not stop within 30 seconds.' }
    }
}
Start-ScheduledTask -TaskName $taskName
Write-Output "Installed $taskName for $UserId. After model warmup, check http://127.0.0.1:$Port/health."
