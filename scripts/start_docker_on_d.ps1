param([string]$RuntimeRoot = 'D:\DockerDesktop\local-appdata')
$ErrorActionPreference = 'Stop'
if ([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($RuntimeRoot)) -ne 'D:\') { throw 'Docker runtime data must be on D:' }
$taskExecutable = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'
if (-not (Test-Path -LiteralPath $taskExecutable)) { $taskExecutable = 'C:\Program Files\Docker\Docker\Docker Desktop.exe' }
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
$taskStart = [Diagnostics.ProcessStartInfo]::new()
$taskStart.FileName = $taskExecutable
$taskStart.UseShellExecute = $false
$taskStart.CreateNoWindow = $true
$taskStart.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
$taskStart.Environment['LOCALAPPDATA'] = $RuntimeRoot
[Diagnostics.Process]::Start($taskStart) | Out-Null
