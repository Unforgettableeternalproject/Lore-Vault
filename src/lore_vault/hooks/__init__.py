"""介面層：Claude Code／Codex hook（收料、注入、健康告警）的共用程式。

**約束：只用標準庫。** hook 由系統 Python 直接執行、不經 `.venv`，
且 PreToolUse 每次編輯都會跑。本子套件只能 import 標準庫、相對 import、
`lore_vault.hooks.*`，以及同樣只用標準庫的 `lore_vault.binding`／`lore_vault.schema`；
不得 import 其他 `lore_vault` 子套件（會連帶拖入第三方依賴）。
由 `lore_vault.doctor.hook_imports` 靜態檢查並有測試守護。

- `client_env`：hook 端設定（服務位址、token、concept 快照路徑）
- `service`：`urllib` 最小 HTTP 客戶端
- `spool`：episode 本地 spool 與推送（T-38／T-39）
- `concept_snapshot`：concept 快照檔的寫入、讀取與對帳（T-40）
"""
