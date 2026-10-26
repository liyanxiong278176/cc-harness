$ErrorActionPreference = 'Stop'
$taskRunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$taskBackup = 'D:\agent_data\cc-harness-observability\deployment\startup-before.json'
$taskBefore = Get-ItemProperty -LiteralPath $taskRunKey
$taskDockerProperty = $taskBefore.PSObject.Properties | Where-Object { $_.Name -match '^Docker Desktop$' }
if (-not (Test-Path -LiteralPath $taskBackup)) {
    @{ docker_desktop = $taskDockerProperty.Value; cc_harness = $taskBefore.PSObject.Properties['cc-harness-observability'].Value } | ConvertTo-Json | Set-Content -LiteralPath $taskBackup -Encoding UTF8
}
$taskScript = Join-Path $PSScriptRoot 'start_observability.ps1'
$taskCommand = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $taskScript + '"'
New-ItemProperty -LiteralPath $taskRunKey -Name 'cc-harness-observability' -Value $taskCommand -PropertyType String -Force | Out-Null
# Replace an existing ordinary Docker launcher with the D-drive launcher. Its
# original value is preserved above; unrelated startup entries are untouched.
if ($taskDockerProperty) { Remove-ItemProperty -LiteralPath $taskRunKey -Name $taskDockerProperty.Name }
Write-Output 'Installed current-user D-drive Docker, Langfuse and queue-worker startup.'
