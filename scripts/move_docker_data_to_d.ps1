param(
    [string]$Destination = 'D:\DockerDesktop',
    [string]$EvidenceRoot = 'D:\agent_data\cc-harness-observability\deployment'
)
$ErrorActionPreference = 'Stop'
$destinationPath = [IO.Path]::GetFullPath($Destination)
if ([IO.Path]::GetPathRoot($destinationPath) -ne 'D:\') {
    throw 'Docker persistent storage must be on D:.'
}
$settingsPath = Join-Path $env:APPDATA 'Docker\settings-store.json'
$desktopRoot = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop'
if (-not (Test-Path -LiteralPath $desktopRoot)) { $desktopRoot = 'C:\Program Files\Docker\Docker' }
$settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
$sourceRoot = if ($settings.PSObject.Properties['DataFolder']) { [string]$settings.DataFolder } else { Join-Path $env:LOCALAPPDATA 'Docker\wsl' }
$sourceRoot = [IO.Path]::GetFullPath($sourceRoot)
$sourceDisk = Join-Path $sourceRoot 'disk\docker_data.vhdx'
$targetDisk = Join-Path $destinationPath 'disk\docker_data.vhdx'
if ($sourceRoot -eq $destinationPath) {
    Write-Output "Docker storage is already configured at $destinationPath"
    exit 0
}
$containers = @(& docker ps -aq)
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Docker containers before relocation.' }
if ($containers.Count -gt 0) { throw 'Existing containers require a separate drain plan; no storage was changed.' }
if (-not (Test-Path -LiteralPath $sourceDisk -PathType Leaf)) { throw "Docker disk not found: $sourceDisk" }
if (Test-Path -LiteralPath $targetDisk) { throw "Destination disk already exists; refusing to overwrite: $targetDisk" }
New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $targetDisk) -Force | Out-Null
$backupPath = Join-Path $EvidenceRoot ('docker-settings-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
Copy-Item -LiteralPath $settingsPath -Destination $backupPath
& (Join-Path $desktopRoot 'DockerCli.exe') -Shutdown
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop did not accept shutdown.' }
$deadline = (Get-Date).AddSeconds(60)
do {
    try {
        $stream = [IO.File]::Open($sourceDisk, 'Open', 'Read', 'None')
        $stream.Dispose()
        $unlocked = $true
    } catch {
        $unlocked = $false
        if ((Get-Date) -gt $deadline) { throw 'Docker disk remains open; original settings and disk preserved.' }
        Start-Sleep -Milliseconds 500
    }
} until ($unlocked)
Copy-Item -LiteralPath $sourceDisk -Destination $targetDisk
$sourceHash = (Get-FileHash -LiteralPath $sourceDisk -Algorithm SHA256).Hash
$targetHash = (Get-FileHash -LiteralPath $targetDisk -Algorithm SHA256).Hash
if ($sourceHash -ne $targetHash) { throw 'Disk copy digest mismatch; original configuration preserved.' }
$settings | Add-Member -NotePropertyName DataFolder -NotePropertyValue $destinationPath -Force
$settings | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $settingsPath -Encoding utf8
@{
    source = $sourceDisk; destination = $targetDisk; sha256 = $targetHash
    settings_backup = $backupPath; original_preserved = $true
    copied_at = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'docker-storage-copy.json') -Encoding utf8
Start-Process -FilePath (Join-Path $desktopRoot 'Docker Desktop.exe') -WindowStyle Hidden
Write-Output "Docker disk copied and settings updated: $targetDisk"
Write-Output 'Original disk preserved. Verify the running engine uses the destination before creating volumes.'
