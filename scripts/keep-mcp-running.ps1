# Hidden sign-in worker: one HTTP server/model for all local MCP clients.
[CmdletBinding()]
param([string]$Distro = 'Ubuntu', [ValidateRange(1, 65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$server = Join-Path $repo 'mcp_server.py'
if (-not (Test-Path -LiteralPath $python)) { throw "Install the virtual environment first: $python" }
$logDirectory = Join-Path $env:LOCALAPPDATA 'project-memory'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory 'mcp-autostart.log'
$stdoutPath = Join-Path $logDirectory 'mcp-server.stdout.log'
$stderrPath = Join-Path $logDirectory 'mcp-server.stderr.log'
$env:REDIS_WSL_DISTRO = $Distro
# This process serves all projects; a user-level override would silently pin all of them.
if ($env:MEMORY_PROJECT) { throw 'Unset MEMORY_PROJECT before starting the shared MCP server.' }
while ($true) {
    foreach ($path in @($logPath, $stdoutPath, $stderrPath)) {
        if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -gt 1MB) {
            Move-Item -LiteralPath $path -Destination "$path.previous" -Force
        }
    }
    $process = $null
    try {
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) Starting shared MCP on 127.0.0.1:$Port"
        # A fresh hidden console has no pending stdin read (important for scipy on Windows).
        $process = Start-Process -FilePath $python -ArgumentList "-u `"$server`" --transport streamable-http --port $Port --parent-pid $PID" `
            -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        # Retain the handle so Windows PowerShell can read ExitCode after the launcher exits.
        $null = $process.Handle
        $process.WaitForExit()
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) MCP exited ($($process.ExitCode)); retrying in 10 seconds"
    } catch {
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $_; retrying in 10 seconds"
    } finally {
        if ($process) {
            if (-not $process.HasExited) { $process.Kill() }
            $process.Dispose()
        }
    }
    Start-Sleep -Seconds 10
}
