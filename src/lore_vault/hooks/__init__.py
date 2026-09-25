"""介面層：Claude Code／Codex hook（收料、注入、健康告警）。

**約束：只用標準庫。** hook 由系統 Python 直接執行、不經 `.venv`，
且 PreToolUse 每次編輯都會跑。本子套件只能 import 標準庫、相對 import
與 `lore_vault.hooks.*`，不得 import 其他 `lore_vault` 子套件（會連帶拖入第三方依賴）。
由 `lore_vault.doctor.hook_imports` 靜態檢查並有測試守護。
"""
