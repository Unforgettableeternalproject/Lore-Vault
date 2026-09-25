# Lore Vault 每日備份（主機 Windows 排程呼叫，A13 同一套慣例）
# 注意：本檔必須存成 UTF-8 with BOM（PS 5.1 會吃掉無 BOM 的中文）；
# Python 輸出走 cmd /c 重導向，不用 *>>。
param(
    [string]$Container = "lore-vault",
    [string]$LogDir = "E:\ProgramFiles\Lore Vault\logs"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd")
$log = Join-Path $LogDir "backup-$stamp.log"

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"), $msg
    Add-Content -Path $log -Value $line -Encoding UTF8
}

# 容器沒在跑就記錄並以非 0 結束；doctor 的 backup.recent 會在逾時後變紅
$state = cmd /c "docker inspect -f {{.State.Running}} $Container 2>nul"
if ($LASTEXITCODE -ne 0 -or $state -ne "true") {
    Write-Log "[skip] 容器 $Container 未執行，本次不備份"
    exit 2
}

Write-Log "[start] 備份開始"
cmd /c "docker exec $Container python -m lore_vault.storage.backup >> `"$log`" 2>&1"
$code = $LASTEXITCODE
Write-Log "[end] exit=$code"
exit $code
