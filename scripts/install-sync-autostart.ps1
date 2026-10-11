# Install the lightweight client watcher; it starts heavy synchronization only while clients are open.
[CmdletBinding()]
param(
    [string]$UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\pythonw.exe'
$script = Join-Path $repo 'sync_memory.py'
$config = Join-Path $repo 'sync_projects.json'
foreach ($path in @($python, $script, $config)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing sync prerequisite: $path" }
}
$taskName = 'ProjectMemory-Sync'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -notlike 'Project memory sync:*') {
    throw "The task $taskName is not owned by this installer."
}
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" --watch --config `"$config`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$definition = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Project memory sync: watch local Claude/Codex clients; initial, 15-minute and final incremental imports.'
Register-ScheduledTask -TaskName $taskName -InputObject $definition -Force | Out-Null
if ($existing -and $existing.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
        if ((Get-Date) -ge $deadline) { throw 'Previous sync watcher did not stop within 30 seconds.' }
        Start-Sleep -Milliseconds 200
    }
}
Start-ScheduledTask -TaskName $taskName
Write-Output "Installed $taskName for $UserId. Logs: LOCALAPPDATA\project-memory\sync.log and sync-watch.log."
