# 註冊 Lore Vault 每日備份排程（需使用者本人執行；存成 UTF-8 with BOM）
$script = Join-Path $PSScriptRoot "backup.ps1"
if (Get-ScheduledTask -TaskName "LoreVaultBackup" -ErrorAction SilentlyContinue) {
    Write-Output "LoreVaultBackup 已存在，未變更"
    exit 0
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At "04:30"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "LoreVaultBackup" -Description "Lore Vault 每日 SQLite 備份（docker exec lore-vault）" -Action $action -Trigger $trigger -Settings $settings | Select-Object TaskName, State
