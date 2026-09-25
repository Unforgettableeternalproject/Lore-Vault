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

## docker 與備份

### 映像與服務（T-26）

- `Dockerfile`：`python:3.14-slim-trixie`（與 `.python-version` 同版），uv 0.9.0 依 `uv.lock`
  分兩層安裝（先相依、後專案，`--frozen --no-dev`）；非 root（uid 10001）；tini 當 PID 1 轉送 SIGTERM。
  建置與啟動前都跑 `python -m lore_vault.storage.sqlite_check`（SQLite ≥ 3.37、FTS5、STRICT），不符即非 0 結束。
- 映像內設定 `docker/config.toml`（`LORE_VAULT_CONFIG` 指向它）：DB `/data/lore.db`、Ollama
  `http://host.docker.internal:11434`、備份目錄 `/backups`；個別項目仍可用 `LORE_VAULT_<區段>_<項目>` 覆寫。
- `docker-compose.yml`：服務 `lore-vault`，`127.0.0.1:5056 → 8000`；資料是 named volume `lore-vault-data`
  （**禁止 bind mount 資料庫**）；`/backups` 是主機 bind mount，只放備份檔。
  `.env` 需要 `LORE_VAULT_API_TOKEN`、`OPENAI_API_KEY`、`LORE_VAULT_HOST_BACKUP_DIR`（經 `env_file`，不進映像）。
- `.dockerignore` 採白名單：只送 `pyproject.toml`、`uv.lock`、`src/`、`docker/`，`.env` 與語料不會進建置上下文。
- 建置：`docker build -t lore-vault:local .`。**不要用 `docker compose config` 檢查設定**：它會把 `env_file`
  內容（含密鑰）展開印出；要驗語法用 `docker compose config --quiet`。
- `docker compose down -v` 會刪掉 named volume（= 刪庫），不要用。

### 備份（T-27）

`python -m lore_vault.storage.backup [--db PATH] [--dest DIR] [--keep N]`（缺省走設定 `database.path`、
`backup.dir`、`backup.keep`；容器內即 `/data/lore.db` → `/backups`）。

- 以唯讀連線 `VACUUM INTO` 到同目錄暫存名 → 唯讀開啟驗證 `integrity_check` 與 `user_version` →
  `os.replace` 成 `lore-<UTC 毫秒時間>Z.db` → 寫 sidecar `last_backup.json` → 只保留最新 N 份。
  任何一步失敗都刪暫存檔，不留半檔、不更新 sidecar。不寫 live DB、不佔遷移版本號。
- doctor `backup.recent`（分類 `backup`）：`python -m lore_vault.doctor --category backup --backup-dir /backups
  [--backup-max-age-hours 26]`。從未備份、sidecar 損毀、引用的備份檔不在、超過門檻皆為 fail；
  未給備份目錄為 skipped。門檻設定項 `backup.max_age_hours`（預設 26）。
- 還原：停服務後把備份檔複製回 volume 的 `/data/lore.db`（並刪除舊的 `-wal`／`-shm`）；尚未做成指令。

### 排程方式（未建立；需裁決）

| | 容器內定時（worker 旁的簡單排程） | 主機 Windows 排程呼叫 `docker exec lore-vault python -m lore_vault.storage.backup` |
|---|---|---|
| 優點 | 跟服務同生命週期、隨 restart policy 復原；不依賴主機排程與 docker CLI；設定都在 image/compose 內 | 與服務程序獨立（服務卡死或 worker 崩潰不影響排程觸發）；與 A13 主機排程慣例一致；排程失敗在工作排程器有紀錄 |
| 缺點 | 要改 API 程序（`api/`，跨本卡範圍）；與服務同程序，程序掛了備份也停；多一個背景迴圈要測 | Docker Desktop 登入後才啟動，未啟動時 `docker exec` 失敗；要沿用 T-43 的 PowerShell 坑（UTF-8 BOM、`cmd /c` 重導向）；需要艾斯維爾授權建立排程 |

任一方案沒跑時，doctor `backup.recent` 在 26 小時後變紅，所以不會靜默失效。

## 啟動 API（本機開發）

HTTP 服務以 factory 啟動（Dockerfile 也用同一個進入點）：

```bash
# token 至少 16 字元、不可含空白；未設定時 create_app 直接拋 ConfigError，服務不會啟動
export LORE_VAULT_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export LORE_VAULT_DATABASE_PATH=/path/to/lore.db   # 資料目錄在 repo 外
export LORE_VAULT_API_ENRICH_WORKER=false          # 選用：不在服務內跑背景補算
uv run uvicorn --factory lore_vault.api.app:create_app --host 127.0.0.1 --port 8000
```

- 所有 `/v1/*`（含 `/v1/openapi.json`）都要 `Authorization: Bearer <token>`，本機也一樣；
  只有 `GET /healthz` 免認證（只回 `{"ok": true}`，不碰資料庫）。
- 端點為 RPC 式、一律 `POST` + JSON body，一對一對應 MCP 工具：
  `/v1/vault_resolve`、`/v1/recall`、`/v1/get`、`/v1/list`、`/v1/write`、`/v1/update`、
  `/v1/status`，另有 `/v1/vaults`（明確建 vault；`write` 不會自動建）。
  錯誤格式統一為 `{"error": {"code", "message", ...}}`。
- 啟動時（lifespan）遷移資料庫，之後每個請求各開一條連線（WAL + busy_timeout）。
- 背景補算 worker 預設在同一程序內以執行緒執行（`[api] enrich_worker`）；
  關閉路徑：SIGTERM 經 tini 轉給 uvicorn → lifespan 結束 → worker `stop()`，
  等執行緒結束最多 5 秒（進行中的模型呼叫無法中斷；compose `stop_grace_period` 需大於此值）。
  `/v1/status` 的 `ok` 同時反映 doctor 與 worker 是否起得來（`fatal_error`）。
- recall 與 write 查重的 embedding 用短逾時 `embedding.query_timeout`（預設 3 秒），
  逾時即降級為只走 lexical 並標 `degraded`；背景補算仍用 `embedding.timeout`。
- 測試一律用 FastAPI `TestClient`（`tests/api/`），不啟動長駐服務。
