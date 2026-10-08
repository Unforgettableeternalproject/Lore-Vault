"""任務層（D15 附加層）：借 OpenSpec 的目錄格式與 propose → apply → archive 生命週期。

`python -m lore_vault.tasks {init,propose,list,validate,archive,doctor}`。

- 附加層：核心（`lore_vault` 其他子套件）不得 import 本套件，由
  `tests/test_tasks_isolation.py` 與 doctor `tasks.isolation` 以 AST 掃描守住；
  本套件可依賴核心（只用 hook 端的標準庫 HTTP 客戶端）。
- 寫入 Lore Vault 只在 archive，經既有 HTTP API（`/v1/write` 等），不碰 DB。
- 狀態不手填，一律由 `.openspec.yaml` 欄位、DECISIONS.md 與目錄位置推導。

設計見 `docs/hidden/design/TASK_LAYER.md`。
"""
