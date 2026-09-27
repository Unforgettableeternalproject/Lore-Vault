# 排程入口：每日凌晨跑記憶管線。
#
# Phase 3 定案：走 Windows 工作排程器，不掛 SessionEnd hook——
# 蒸餾要幾分鐘，hook 得 detach、失敗靜默難追，而且剛結束工作時機器最忙。
#
# 註冊（手動執行一次即可）：
#   schtasks /create /tn "AgentMemoryPipeline" /sc daily /st 03:30 ^
#     /tr "powershell -NoProfile -ExecutionPolicy Bypass -File <本檔絕對路徑>"
#
# 兩個實測逼出來的細節：
# 1. 本檔必須存成 UTF-8 **含 BOM**——Windows PowerShell 5.1 讀無 BOM 的
#    UTF-8 會當成 ANSI，中文註解的位元組會吃掉後面的程式碼行
#    （實測 $log 整行被吞成 Null，錯誤只在排程的黑箱裡發生）
# 2. Python 的輸出用 cmd /c 原生重導，不用 PowerShell 的 *>>——
#    後者把 stderr 每一行包成 NativeCommandError 紀錄，log 全是包裝噪音

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"

$repo = Split-Path -Parent $PSScriptRoot
# 直譯器用本 repo 的 .venv（不依賴 U.E.P env）；由本檔位置推導，不寫死機器路徑
$python = Join-Path $repo ".venv\Scripts\python.exe"
$script = Join-Path $PSScriptRoot "pipeline.py"
# log 目錄以 paths.LOG_DIR 為準（唯一來源，含 D5 過渡 fallback 與 LORE_VAULT_SPIKE_HOME 覆寫）；
# 取不到才退回預設位置，免得 log 無處可寫
$logDir = & $python -S -c "import sys; sys.path.insert(0, sys.argv[1]); import paths; print(paths.LOG_DIR)" $PSScriptRoot 2>$null |
    Select-Object -Last 1
if (-not $logDir) { $logDir = Join-Path $env:USERPROFILE ".lore-vault\logs" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 一天一檔。管線本身有 lockfile，重複觸發不會疊跑，log 用 append 也不會互相蓋掉
$log = Join-Path $logDir ("pipeline-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

# --max-groups 24 對齊手動時代的單批大小：實測 20 組單次裁決要 ~13 分鐘，
# 40 組會頂到逾時；24 是歷史上驗證過的評審批次。
# --calibrate-max 72 只放大校準：積壓近 700 條未校準、每晚 24 條要清一個月；
# 近期 24 條的校準階段只花 2–9 分鐘，72 條仍在單次裁決 30 分鐘的逾時內
"=== pipeline start $(Get-Date -Format o) ===" | Out-File -FilePath $log -Append -Encoding utf8
& cmd /c "`"$python`" `"$script`" --run --max-groups 24 --calibrate-max 72 >> `"$log`" 2>&1"
$code = $LASTEXITCODE
"=== pipeline exit $code $(Get-Date -Format o) ===" | Out-File -FilePath $log -Append -Encoding utf8

# --run 成功才推 concept 到服務（PreToolUse 讀的服務端快照靠這一步更新）。
# --run 失敗（含階段失敗、鎖被占用）不推：池子可能只收斂了一半。
# 推送結果（成敗）由 pipeline.py 寫進 pipeline_state.json 的 concept_push，健康告警讀它；
# 這裡的 log 與 exit code 讓排程器本身也看得到失敗
if ($code -eq 0) {
    "=== push-concepts start $(Get-Date -Format o) ===" | Out-File -FilePath $log -Append -Encoding utf8
    & cmd /c "`"$python`" `"$script`" --push-concepts >> `"$log`" 2>&1"
    $pushCode = $LASTEXITCODE
    "=== push-concepts exit $pushCode $(Get-Date -Format o) ===" | Out-File -FilePath $log -Append -Encoding utf8
    if ($pushCode -ne 0) { $code = $pushCode }
} else {
    "=== push-concepts skipped (pipeline exit $code) ===" | Out-File -FilePath $log -Append -Encoding utf8
}
exit $code
