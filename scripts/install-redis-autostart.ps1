# Sign-in startup needs no administrator. Optional boot startup requires an elevated PowerShell.
[CmdletBinding()]
param(
    [ValidateSet('Boot', 'Logon')][string]$Mode = 'Logon',
    [string]$Distro = 'Ubuntu',
    [string]$UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
)
$ErrorActionPreference = 'Stop'
$taskName = 'ProjectMemory-Redis'
$worker = Join-Path $PSScriptRoot 'keep-redis-running.ps1'
if ($Distro -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Use a WSL distribution name containing only letters, digits, dots, underscores or hyphens.' }

# Refuse to take over an unrelated task that happens to have the same name.
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -notlike 'Project memory Redis:*') {
    throw "The task $taskName already exists and is not owned by this installer."
}

$taskAction = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$worker`" -Distro `"$Distro`""
if ($Mode -eq 'Boot') {
    # WSL distributions belong to their Windows user, so SYSTEM cannot use this Ubuntu install.
    # S4U runs as its owner before login without storing a password.
    $taskTrigger = New-ScheduledTaskTrigger -AtStartup
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited
} else {
    $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
}
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$definition = New-ScheduledTask -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal `
    -Settings $taskSettings -Description "Project memory Redis: keep $Distro and the existing shared Redis container ready ($Mode startup)."
# Registration failure leaves the previous task untouched.
Register-ScheduledTask -TaskName $taskName -InputObject $definition -Force | Out-Null
if ($existing -and $existing.State -eq 'Running') {
    $oldWorkers = @(Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($worker) } |
        ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    Stop-ScheduledTask -TaskName $taskName
    # Stop is asynchronous. Wait for the previous worker's console to close before starting a
    # replacement, otherwise Windows can send the replacement the old console's close signal.
    foreach ($oldWorker in $oldWorkers) {
        if (-not $oldWorker.WaitForExit(30000)) { throw 'The previous Redis keeper did not stop within 30 seconds.' }
    }
}
Start-ScheduledTask -TaskName $taskName
Write-Output "Installed $taskName for $UserId ($Mode). Check Get-ScheduledTaskInfo and the log in LOCALAPPDATA\project-memory."
