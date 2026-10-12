# A hidden scheduled-task worker. Keep existing WSL containers/volumes; create neither.
[CmdletBinding()]
param([string]$Distro = 'Ubuntu')
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$containers = @('project-memory-redis')
$containerConfig = Join-Path $repo 'redis-containers.local.json'
if (Test-Path -LiteralPath $containerConfig) {
    $containers = Get-Content -LiteralPath $containerConfig -Raw | ConvertFrom-Json
}
if (-not $containers.Count) { throw 'At least one Redis container is required.' }
foreach ($container in $containers) {
    if ($container -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]+$') { throw 'Invalid Redis container name.' }
}
if ($Distro -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Invalid WSL distribution name.' }
$logDirectory = Join-Path $env:LOCALAPPDATA 'project-memory'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory 'redis-autostart.log'
if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1MB) {
    Move-Item -LiteralPath $logPath -Destination "$logPath.previous" -Force
}
Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) Starting Redis keeper for $Distro"
# stdin avoids shell quoting differences in Windows PowerShell's native argument handling.
$linuxCommand = @'
set -eu
attempt=0
until docker info >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then echo 'Docker did not become ready within 60 seconds' >&2; exit 1; fi
    sleep 1
done
for container in __CONTAINERS__; do
docker start "$container"
attempt=0
until docker exec "$container" redis-cli ping | grep -q '^PONG'; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then echo 'Redis did not become ready within 60 seconds' >&2; exit 1; fi
    sleep 1
done
echo 'Redis ready; keeping WSL alive'
done
exec cat >/dev/null
'@
$linuxCommand = $linuxCommand.Replace('__CONTAINERS__', ($containers -join ' '))
while ($true) {
    $keeper = New-Object System.Diagnostics.Process
    $keeperStarted = $false
    $keeper.StartInfo.FileName = "$env:WINDIR\System32\wsl.exe"
    $keeper.StartInfo.Arguments = "--distribution $Distro --exec sh -s"
    $keeper.StartInfo.UseShellExecute = $false
    $keeper.StartInfo.CreateNoWindow = $true
    $keeper.StartInfo.RedirectStandardInput = $true
    $keeper.StartInfo.RedirectStandardOutput = $true
    $keeper.StartInfo.RedirectStandardError = $true
    try {
        $keeperStarted = $keeper.Start()
        $errors = $keeper.StandardError.ReadToEndAsync()
        $keeper.StandardInput.Write($linuxCommand.Replace("`r", '') + "`n")
        # Keep stdin open: cat exits on EOF when this Windows process ends, so stopping or
        # replacing the task does not leave a detached Linux sleep process behind.
        $keeper.StandardInput.Flush()
        while ($null -ne ($line = $keeper.StandardOutput.ReadLine())) {
            Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $line"
        }
        $keeper.WaitForExit()
        if ($errors.Result) { Add-Content -LiteralPath $logPath -Value $errors.Result }
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) WSL keeper exited ($($keeper.ExitCode)); retrying in 10 seconds"
    } catch {
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $_; retrying in 10 seconds"
    } finally {
        if ($keeperStarted) {
            $keeper.StandardInput.Close()
            if (-not $keeper.HasExited) { $keeper.Kill() }
        }
        $keeper.Dispose()
    }
    if ((Get-Item -LiteralPath $logPath).Length -gt 1MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.previous" -Force
    }
    # Retry here as well: Task Scheduler did not restart a manually started failing task on the
    # tested machine. Its restart policy remains a fallback for failure of PowerShell itself.
    Start-Sleep -Seconds 10
}
