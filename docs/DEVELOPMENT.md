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

## MCP 殼（T-29～T-31）

每台機器跑一個本地 **stdio** MCP 殼，轉發到服務 HTTP（A15）。用本 repo `.venv` 的 Python 啟動
（它不是 hook，可以用第三方套件）：

```bash
uv run python -m lore_vault.mcp [--config PATH] [--env-file PATH]
```

stdout 是 MCP 協定通道，log 一律寫 stderr（UTF-8）。設定錯誤時印出原因並以 2 結束。

### 設定

優先序同 `lore_vault.config`：環境變數 > 設定檔 `[mcp]` 區段 > 預設值（見 `config.example.toml`）。

| 項目 | 環境變數 | 預設 | 說明 |
|---|---|---|---|
| `mcp.base_url` | `LORE_VAULT_MCP_BASE_URL` | `http://127.0.0.1:5056` | 服務位址；遠端填 Cloudflare 子網域 |
| `mcp.timeout` | `LORE_VAULT_MCP_TIMEOUT` | 10 秒 | 每個請求逾時，逾時視為不可達 |
| `mcp.snapshot_dir` | `LORE_VAULT_MCP_SNAPSHOT_DIR` | 無 | 本地快照目錄；未設定＝不拉快照、不可達時無法降級 |
| `mcp.snapshot_interval` | `LORE_VAULT_MCP_SNAPSHOT_INTERVAL` | 900 秒 | 定期拉快照；0＝只在啟動時拉 |
| `mcp.snapshot_max_age_hours` | `LORE_VAULT_MCP_SNAPSHOT_MAX_AGE_HOURS` | 24 | doctor 快照年齡門檻 |
| `mcp.cf_access_env_file` | `LORE_VAULT_MCP_CF_ACCESS_ENV_FILE` | 無 | CF Access token 檔（格式同 `~/.cloudflared/pm-token.env`） |

密鑰只走環境變數或 `--env-file`，設定檔出現 token／secret 類的鍵會拒絕載入：

- `LORE_VAULT_API_TOKEN`（必填，本機也要帶）
- `CF_ACCESS_CLIENT_ID`／`CF_ACCESS_CLIENT_SECRET`（選用；兩個都有才加 `CF-Access-Client-*` header，
  只有一個直接報錯）。來源優先序：環境變數 > `--env-file` > `mcp.cf_access_env_file`

密鑰不會出現在 log、工具回傳與例外訊息（`Secret` 包裝；錯誤只寫要檢查哪個設定）。

### 工具

`vault_resolve(cwd?, create?, display?)`、`recall(query, vault, kinds?, limit?, budget?)`、
`get(vault, ids, budget?)`、`list(vault, since?, topics?, cursor?, limit?)`、
`write(vault, title, body, topics?, links?, supersedes?)`、
`update(vault, id, expected_updated, title?, body?, topics?, links?, supersedes?)`、`status(vault?)`。

- 建 vault 併入 `vault_resolve(create=True)`，沒有獨立工具。`cwd` 省略時用殼的工作目錄
  （Claude Code 啟動殼時的專案目錄）；key 由殼端 `lore_vault.binding` 從 git remote 算
- 成功回服務 JSON 原樣（緊湊、不縮排）；錯誤是工具錯誤，內容 `{"error": {...}, "hint", "http_status"}`。
  409 版本衝突附 `current`，以 `current.updated` 當 `expected_updated` 重試

### 快照與降級

- 服務端 `GET /v1/snapshot`（需 bearer）：同一讀取交易內把 `vaults`、`vault_aliases`、`notes`、`note_fts`
  複製到新檔（白名單；不含向量、episode、concept、injection），header 帶 schema 版本、產生時間、sha256、筆數
- 服務端快取最近一份快照：每次請求先算白名單資料表的內容指紋（vaults、別名、notes 全欄位的 sha256），
  未變就沿用、不重建。不用「max(updated) + 筆數」：背景補摘要不推進 `updated`；`PRAGMA data_version`
  只在同一連線內有效。快取目錄預設在系統暫存、服務關閉時刪除（`ApiSettings.snapshot_cache_dir` 可指定）
- ETag = 快照檔 sha256；`If-None-Match` 符合回 304（不傳檔，header 仍帶版本資訊）
- 殼端在啟動時（背景，不擋 initialize）與每 `snapshot_interval` 秒拉一次：寫同目錄暫存檔 → 驗 sha256、
  integrity、schema 版本、筆數 → `os.replace` 成 `snapshot.db` → 寫 `snapshot.json`（manifest）。
  本地快照與 manifest 一致時帶 `If-None-Match`；收到 304 就沿用舊快照、只更新 manifest 的 `checked_at`。
  失敗只記 log，暫存檔刪除、舊快照不動
- 降級矩陣：

| 服務回應 | `recall`／`get`／`list`／`vault_resolve` | `write`／`update`／建 vault | `status` |
|---|---|---|---|
| 連線失敗、逾時、協定錯誤、502／503／504 | 讀快照（只走 lexical），標 `degraded`、`degraded_reason: "service_unreachable"`、`snapshot.generated_at`／`checked_at` | 失敗，不排佇列 | 回殼端狀態、`ok: false` |
| 3xx（Access 導向登入）、401、403、其他 4xx、500 等其餘 5xx | 直接報錯（設定、請求或服務端資料錯誤，不降級） | 同左 | 同左 |

- 降級路徑沿用服務層函式對快照唯讀查詢，vault 硬範圍、別名、參數驗證與服務端一致
- doctor：`python -m lore_vault.doctor --category snapshot --snapshot-dir DIR [--snapshot-max-age-hours H]`。
  從未拉取、manifest 與快照檔 sha256 不一致、schema 版本（manifest 或檔案）與程式不符、超過年齡門檻皆為 fail。
  年齡以最近一次向服務確認的時間（`checked_at`，含 304）計，資料長期沒變不會誤紅

### Claude Code 設定範例

**只是文件範例；不要由 agent 修改 `~/.claude.json`**（切換見 docs/MIGRATION.md）。
token 放在 repo 外的 env 檔，不寫進 `.claude.json`：

```json
{
  "mcpServers": {
    "lore-vault": {
      "type": "stdio",
      "command": "C:/path/to/Lore-Vault/.venv/Scripts/python.exe",
      "args": [
        "-m", "lore_vault.mcp",
        "--config", "C:/Users/<you>/.lore-vault/mcp.toml",
        "--env-file", "C:/Users/<you>/.lore-vault/mcp.env"
      ]
    }
  }
}
```

`mcp.env`：`LORE_VAULT_API_TOKEN=...`（遠端機器另加 `CF_ACCESS_CLIENT_ID`／`CF_ACCESS_CLIENT_SECRET`，
或在 `mcp.toml` 設 `cf_access_env_file = "~/.cloudflared/pm-token.env"`）。
`mcp.toml` 至少設 `[mcp] snapshot_dir`，遠端再設 `base_url`。

## Open Notebook 匯入（T-33～T-37）

`python -m lore_vault.cli.import_on <export|map|import|estimate>`。所有輸出（匯出目錄、mapping、報告、
資料庫）含商業原文，程式拒絕寫進 repo 內；stdout 只印統計。

1. `export --out DIR [--base-url http://localhost:5055]`：只用 GET。ON 1.14.0 沒有分頁；
   `GET /api/notes?notebook_id=` 不回內容，筆數必須等於 `/api/notebooks` 的 `note_count`，
   內容逐筆 `GET /api/notes/{id}` 取（全量 `GET /api/notes` 能用時改用它，並可列出孤兒 note）。
   任何筆數不一致即失敗、不寫檔。產出 `notebooks.jsonl`、`notes.jsonl`、`manifest.json`（含檔案雜湊）。
2. `map --export DIR [--search-root DIR ...]`：產生 `mapping.json`。`[bind: key]` → 小寫 key；
   無標記的 `[PM] <name>` → `folder/<name>` 並標 `needs_review`（`--search-root` 會找同名 repo 附上
   git remote 建議 key）；`[PM] Global…` → key `global`、kind `global`；共用 key、多歸屬 note 也標待確認。
   人工改完把 `needs_review` 設 false；`skip: true` 可略過整本。
3. `import --export DIR --mapping FILE --db PATH [--allow-unreviewed]`：vault 不存在才建；note id 沿用
   ON 原 id（`note:xxxx`）、保留 created／updated（轉 `…sssZ`）；summary 與向量留空交給 enrich。
   先整批寫對帳清單（`import_sources`／`import_vault_counts`，schema v3）再寫 note。重跑冪等：
   未變跳過、來源變了且本地未改 → 更新、本地已改（updated 推進）→ 不覆寫並列報告。
   `[[標題]]` 同 vault 依標題解析進 `links`，歧義／跨 vault／解析不到保留原文並列在報告檔。
4. `estimate --db PATH [--config]`：待補摘要／向量筆數與 token、時間粗估，不呼叫任何 API。
   實際補算用 `python -m lore_vault.enrich`（或服務內 worker）。

doctor `import.on_reconcile`（分類 `import`）：清單有但 note 不在、或 `updated` 未推進而
(title, body) 雜湊不符 → fail；各 vault 來源筆數 ≠ 清單 ≠ 實際 → fail；匯入後正常修改與新系統新增
只計數。尚未匯入為 skipped。
