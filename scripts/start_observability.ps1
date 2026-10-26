param([string]$RepoRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$taskDataRoot = 'D:\agent_data\cc-harness-observability'
$taskDockerReady = $false
try { & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null; $taskDockerReady = ($LASTEXITCODE -eq 0) } catch {}
if (-not $taskDockerReady) {
    & (Join-Path $PSScriptRoot 'start_docker_on_d.ps1')
    for ($taskAttempt = 0; $taskAttempt -lt 60; $taskAttempt++) {
        Start-Sleep -Seconds 2
        try { & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $taskDockerReady = $true; break } } catch {}
    }
}
if (-not $taskDockerReady) { throw 'D-drive Docker engine did not become ready' }
$taskPython = (Get-Command python -ErrorAction Stop).Source
& $taskPython (Join-Path $PSScriptRoot 'manage_langfuse.py') up
if ($LASTEXITCODE -ne 0) { throw 'Local Langfuse did not become ready' }
$taskWorker = Start-Process -FilePath $taskPython -ArgumentList @('-m', 'cc_harness.observability.worker', '--source', ('"' + $RepoRoot + '"')) -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskDataRoot 'deployment\worker.log') -RedirectStandardError (Join-Path $taskDataRoot 'deployment\worker-errors.log') -PassThru
$taskWorker.Id | Set-Content -LiteralPath (Join-Path $taskDataRoot 'deployment\worker.pid')
