# 開發指令

環境由 uv 管理，`.python-version` 釘在 3.14（與 hook 執行用的系統 Python 同版）。

| 用途 | 指令 |
|---|---|
| 建立／同步 `.venv` | `uv sync` |
| 全部測試（`tests/` + `agent_memory_spike/` 既有測試） | `uv run pytest` |
| lint | `uv run ruff check .` |
| 格式檢查 | `uv run ruff format --check .` |

- 測試路徑與 `--import-mode=importlib` 設在 `pyproject.toml`，兩處測試檔同名也不衝突。
- `agent_memory_spike/` 是併入的 spike 子樹，維持原有風格、只做最小改動，不納入 ruff。
- hook 路徑只用標準庫：`lore_vault.doctor.hook_imports.check_hook_imports` 靜態檢查，
  `tests/test_hook_imports.py` 另以 `python -S`（無 site-packages）實際 import 驗證。

## 新增 doctor 檢查項

框架在 `lore_vault.doctor`（純標準庫）；執行：`uv run python -m lore_vault.doctor [--json] [--category NAME]`。
有任何 fail 時 exit code 為 1，否則 0（warn、skipped 不影響）；參數錯誤（含未知分類）為 2。

1. 寫檢查函式，簽名固定為 `(ctx: DoctorContext) -> CheckResult`：
   ```python
   from lore_vault.doctor import CheckResult, DoctorContext


   def fts_rows_match_notes(ctx: DoctorContext) -> CheckResult:
       db = ctx.require("db")  # 缺資源 → 該項記為 skipped 並附原因
       notes, rows = ...
       counts = {"notes": notes, "fts_rows": rows}
       if notes != rows:
           return CheckResult.fail(
               "FTS 列數與 note 數不一致", counts=counts, details=[...]
           )
       return CheckResult.ok(counts=counts)
   ```
   - 設定值從 `ctx.settings`、執行期資源（db 連線等）從 `ctx.resources`／`ctx.require()` 取，**不要自己讀全域狀態**。
   - 結果四態：`CheckResult.ok / fail / warn / skipped(reason)`；fail／warn／skipped 必須附 summary。
     `details` 是字串序列，`counts` 是 `{str: int}`。
2. 到 `src/lore_vault/doctor/builtin.py` 的 `default_registry()` 加一行
   `registry.add(Check("<分類>.<項目>", "<分類>", func, "一行說明"))`。名稱重複會直接拋錯。
3. 測試：用 `Registry([Check(...)])` 或 `default_registry().run(DoctorContext(...), categories=[...])`
   建隔離的 context，並證明資料不一致時該項為 fail（對帳要能紅）。

框架保證：檢查拋例外或回傳非 `CheckResult` 時記成 fail（附 traceback），其他項照跑；
沒有模組級單例 registry，每次 `default_registry()` 都是新的。
