# 開發指令

環境由 uv 管理，`.python-version` 釘在 3.14（與 hook 執行用的系統 Python 同版）。

| 用途 | 指令 |
|---|---|
| 建立／同步 `.venv` | `uv sync` |
| 全部測試（`tests/` + `agent_memory_spike/` 既有測試） | `uv run pytest` |
| lint | `uv run ruff check .` |
| 格式檢查 | `uv run ruff format --check .` |

- 測試路徑與 `--import-mode=importlib` 設在 `pyproject.toml`，兩處測試檔同名也不衝突。
- `agent_memory_spike/` 是凍結的併入子樹，不納入 ruff。
- hook 路徑只用標準庫：`lore_vault.doctor.hook_imports.check_hook_imports` 靜態檢查，
  `tests/test_hook_imports.py` 另以 `python -S`（無 site-packages）實際 import 驗證。
