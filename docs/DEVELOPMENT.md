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

- 所有 `/v1/*`（含 `/v1/openapi.json`）都要認證，本機也一樣：`Authorization: Bearer <token>`，
  或 UI 的 session cookie ＋ `X-Lore-Vault-UI: 1`（見「UI 開發與建置」）。免認證的只有
  `GET /healthz`（只回 `{"ok": true}`，不碰資料庫）與 `/ui`、`/ui/*`（靜態檔與登入端點）。
- 端點為 RPC 式、一律 `POST` + JSON body，一對一對應 MCP 工具：
  `/v1/vault_resolve`、`/v1/recall`、`/v1/get`、`/v1/list`、`/v1/write`、`/v1/update`、
  `/v1/status`，另有 `/v1/vaults`（明確建 vault；`write` 不會自動建）。
  錯誤格式統一為 `{"error": {"code", "message", ...}}`。
- 作者（A22，schema v12）：`/v1/write`、`/v1/update` 接受 `author`（寫入者自報名，未填存 null、不代填）；
  `principal` 由認證中介層依憑證判定（`api.principals`，放進 ASGI scope，路由以 `principal_of` 取，缺少即 500），
  **body 帶 `principal`／`updated_by_principal` 一律 422**（`extra="forbid"`，不寫入）。
  服務層 `notes.write`／`notes.update` 的 `principal` 是必填 keyword；儲存層 `insert_note` 拒收缺 principal 的 Note，
  `update_note_if(editor=(名稱, principal))` 寫 `updated_by*`（省略 `editor`＝作者欄位不動，只給內部呼叫）
- 啟動時（lifespan）遷移資料庫，之後每個請求各開一條連線（WAL + busy_timeout）。
- 背景補算 worker 預設在同一程序內以執行緒執行（`[api] enrich_worker`）；
  關閉路徑：SIGTERM 經 tini 轉給 uvicorn → lifespan 結束 → worker `stop()`，
  等執行緒結束最多 5 秒（進行中的模型呼叫無法中斷；compose `stop_grace_period` 需大於此值）。
  `/v1/status` 的 `ok` 同時反映 doctor 與 worker 是否起得來（`fatal_error`）。
- recall 與 write 查重的 embedding 用短逾時 `embedding.query_timeout`（預設 3 秒），
  逾時即降級為只走 lexical 並標 `degraded`；背景補算仍用 `embedding.timeout`。
- 冷啟動：Ollama 載入 bge-m3 約 2 秒，逼近 `query_timeout`。兩道處理：
  - 每個 `/api/embed` 請求帶 `keep_alive`（`embedding.keep_alive`，預設 `"30m"`；純整數為秒數、
    負值＝常駐、空字串＝不送），閒置 30 分鐘內模型不會被卸載
  - 啟動時（lifespan，遷移之後）背景執行緒做一次暖機 embed（`api.embedding_warmup`，預設開），
    用完整 `embedding.timeout`，不阻擋啟動；失敗只記 log、不影響 `ok`。
    `/v1/status` 的 `embedding.warmup`：`status`（`pending`／`running`／`ok`／`failed`／`disabled`）、
    `started_at`、`finished_at`、`elapsed_ms`、`error`
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
| `mcp.concept_snapshot_path` | `LORE_VAULT_MCP_CONCEPT_SNAPSHOT_PATH` | `<snapshot_dir>/concepts.json` | PreToolUse 讀的 concept 快照（T-40）；snapshot_dir 也未設＝不拉 |
| `mcp.upload_roots` | `LORE_VAULT_MCP_UPLOAD_ROOTS` | 無 | `upload` 可讀的**額外**目錄（`os.pathsep` 分隔，Windows 為 `;`）。殼的工作目錄一律可讀——**殼能讀到工作目錄下的任何檔案** |

密鑰只走環境變數或 `--env-file`，設定檔出現 token／secret 類的鍵會拒絕載入：

- `LORE_VAULT_API_TOKEN`（必填，本機也要帶）
- `CF_ACCESS_CLIENT_ID`／`CF_ACCESS_CLIENT_SECRET`（選用；兩個都有才加 `CF-Access-Client-*` header，
  只有一個直接報錯）。來源優先序：環境變數 > `--env-file` > `mcp.cf_access_env_file`

密鑰不會出現在 log、工具回傳與例外訊息（`Secret` 包裝；錯誤只寫要檢查哪個設定）。

### 工具

`space(action, value?)`、`vault_resolve(cwd?, create?, display?, space?, key?)`、
`recall(query, vault, kinds?, limit?, budget?)`、
`get(vault, ids, budget?)`、`list(vault, since?, topics?, cursor?, limit?, kinds?)`、
`write(vault, title, body, topics?, links?, supersedes?, author?)`、
`update(vault, id, expected_updated, title?, body?, topics?, links?, supersedes?, author?)`、
`upload(path, vault?)`、`status(vault?)`（共 9 個）。

- **目前 space**（A18）：殼行程持有、只在記憶體，新行程一律 `dev`；`space(action="set", value=...)`
  切換（不打服務）。其他工具沒有 space 參數，殼在每個 `/v1/*` 請求自動注入（`Shell._send`）；
  服務端 space 必填、無預設，直接打 HTTP 的客戶端必須自己帶
- 建 vault 併入 `vault_resolve(create=True)`，沒有獨立工具。dev：`cwd` 省略時用殼的工作目錄
  （Claude Code 啟動殼時的專案目錄）；key 由殼端 `lore_vault.binding` 從 git remote 算。
  lore／personal：沒有 repo，必須帶 `key`（`<space>/名稱`，前綴不符服務端回 `space_key_prefix_required`），
  `cwd` 被忽略（回應 `cwd_ignored: true`）；不自動建 `<space>/global`
- 成功回服務 JSON 原樣（緊湊、不縮排）；錯誤是工具錯誤，內容 `{"error": {...}, "hint", "http_status"}`。
  409 版本衝突附 `current`，以 `current.updated` 當 `expected_updated` 重試
- `upload(path, vault?)`（T-67）：殼讀本機檔案，multipart 轉送 `POST /v1/documents`（帶目前 space）。
  `path` 可為絕對或相對殼工作目錄；任何一段是 `..` 直接拒絕；以 realpath（解開 symlink／junction）
  比對白名單（殼工作目錄＋`mcp.upload_roots`），逃出去回 `path_not_allowed`。Windows 上另拒絕
  （`path_not_allowed`）：`C:foo`（有磁碟代號但非絕對，會依該磁碟的目前目錄解析）、`\foo`（有根無磁碟代號）、
  UNC `\\server\share`、`\\?\`／`\\.\` 裝置前綴、檔名含 `:`（NTFS 替代資料流，如 `a.txt:stream`）；
  POSIX（容器）不套這組檢查（`/abs/path` 在 Windows 語意下是「有根無磁碟代號」）。殼端先擋大小
  （`documents.max_file_bytes`，同服務端上限）。`vault` 省略時只在 dev 以殼工作目錄的 binding 解析
  （不建 vault，回應 `vault_source: "cwd_binding"`）；lore／personal 必須帶。錯誤碼：
  `path_not_allowed`、`file_not_found`、`not_a_file`、`too_large`、`read_failed`、`vault_required`
- `recall` 預設同時查 note 與文件段落（`kinds` 預設 `["note", "chunk"]`）；`get` 的 `ids` 可混 note id、
  `doc:…`（整份文件文字）、`chunk:…`（單段）；`list` 預設同時列 note 與文件（`kinds: ["note"|"document"]`）

### 快照與降級

- 服務端 `GET /v1/snapshot`（需 bearer）：同一讀取交易內把 `vaults`（含 `space`）、`vault_aliases`、`notes`、`note_fts`
  複製到新檔（整庫、不分 space；殼降級查詢以目前 space 過濾）（白名單；不含向量、episode、concept、injection），header 帶 schema 版本、產生時間、sha256、筆數
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
| 連線失敗、逾時、協定錯誤、502／503／504 | 讀快照（只走 lexical），標 `degraded`、`degraded_reason: "service_unreachable"`、`snapshot.generated_at`／`checked_at`；快照不含文件：recall 的 `chunk` 列在 `unsupported_kinds`、get 的 `doc:`／`chunk:` id 列在 `unavailable`、list 的 `document` 列在 `unsupported_kinds`（不以空結果冒充「沒有」） | 失敗，不排佇列（含 `upload`） | 回殼端狀態、`ok: false` |
| 3xx（Access 導向登入）、401、403、其他 4xx、500 等其餘 5xx | 直接報錯（設定、請求或服務端資料錯誤，不降級） | 同左 | 同左 |

- 降級路徑沿用服務層函式對快照唯讀查詢，vault／space 硬範圍、別名、參數驗證與服務端一致
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

## 管理指令：刪除、換 space 與 blob 清理（不提供 MCP 工具）

`python -m lore_vault.cli.admin [--db PATH] [--config FILE] <子指令>`；`--db` 缺省走 `database.path`
（容器內即 `/data/lore.db`）。不遷移資料庫，schema 版本不符或 DB 檔不存在直接失敗（不建空檔）。

| 子指令 | 說明 |
|---|---|
| `delete-note --space SPACE --vault KEY --id NOTE_ID [--reason TEXT] [--yes]` | 刪單則 note（vault 在該 space 內解析，可用別名；`--space` 必填） |
| `delete-vault --key KEY [--force] [--reason TEXT] [--yes]` | 刪整個 vault；只接受正式 key。vault 內有 note、文件或 episode／concept／injection 時必須 `--force`；文件一併刪並各寫文件墓碑 |
| `delete-document --space SPACE --vault KEY --id DOC_ID [--reason TEXT] [--yes]` | 刪單份文件：chunk、chunk_fts、向量（CASCADE）、抽取／補算紀錄（CASCADE），寫 `document_tombstones`。blob 不刪（其他 vault／版本可能共用），沒人引用時 doctor `documents.orphan_blobs` 回報。指向它的新版本改指向它的前一版；刪的是現行版本時前一版同交易回到索引（向量由 worker 補）。CLI 不提供 undelete（HTTP `document_undelete` 可在原始檔仍在時復原，見下） |
| `undelete-note --id NOTE_ID [--yes]` | 取消刪除。墓碑有內容快照（schema v12 起刪除的）→ 以原 id、原內容還原（FTS 同交易重建，向量由服務背景補算），所屬 vault 已刪除則拒絕（先重建 vault）；v12 前的舊墓碑 → 只移除墓碑，下次重跑匯入時該 note 會匯回。dry-run 以 `has_snapshot` 顯示走哪條 |
| `gc-blobs [--blob-dir DIR] [--min-age-hours N] [--yes]` | 清理孤兒 blob 與中斷遺留的暫存檔，見下方「blob 清理」 |
| `set-space --key KEY --space SPACE [--yes]` | 把 vault 換到另一個 space（A19）；只接受正式 key。前綴規則與建立時相同：目標非 dev 時 key 與別名都必須以 `<space>/` 開頭，不合即拒（dry-run 就擋，不改 key）。換完後 MCP 殼的降級快照要等下次快照更新才反映 |

- 預設 dry-run：stdout 印 JSON（`mode`、`vault`、`counts`、`note_ids`、`requires_force`），
  只有 id 與筆數、不含標題與內文。加 `--yes` 才刪；exit code 0 成功、1 找不到／需要 `--force`／schema 不符、2 參數錯誤
- 單一交易：FTS 列、向量與補算紀錄（CASCADE）、別名（CASCADE）、episodes／concepts／injections
  一併刪；刪完核對實際筆數與規劃、檢查無孤兒向量／補算列，不符整段 rollback
- **墓碑**（schema v5 `note_tombstones`，刻意無外鍵）：每則被刪的 note 寫一筆（note id、vault、
  對帳清單記載的來源與來源 id、刪除時間、`--reason`）；`delete-vault --force` 為其下每則 note 各寫一筆。
  schema v12 起另存刪除當下的完整內容（`snapshot` JSON：title、body、summary、topics、links、supersedes、
  作者欄位、created、updated），供取消刪除還原；還原時 vault 以墓碑的 `vault` 欄為準（`set-space` 會改寫它，
  快照裡的 vault 不改）。墓碑內容永久保留在 DB。匯入對帳清單（`import_sources`／`import_vault_counts`）不動
- 重跑 `import_on import`：有墓碑的 note 跳過、不匯回，報告 `skipped.deleted` 與 `deleted_skipped` 列出；
  來源 note 全部有墓碑的 vault 不重建（`vaults.deleted_skipped`）。已知缺口：沒有 note 的空 vault 刪除不寫墓碑，
  若 mapping 仍有該本會被重建
- `import.on_reconcile`：清單有、note 沒有、有墓碑 → 刻意刪除（`counts.deleted`，只報告）；沒有墓碑 → 漏筆（fail）。
  來源筆數核對改為「實際 + 刻意刪除 = 來源」。舊墓碑 `undelete-note` 後、重匯前 doctor 會顯示漏筆，重匯即恢復綠；
  有快照的還原後 note 與匯入時相同（`updated` 未變），重匯視為 unchanged
- 服務執行中可直接用（WAL + busy_timeout；與匯入工具同樣直接寫 DB）。快照快取以內容指紋判斷，刪除後下次拉取即更新

容器內：

```bash
docker exec lore-vault python -m lore_vault.cli.admin delete-vault --key folder/x            # dry-run
docker exec lore-vault python -m lore_vault.cli.admin delete-vault --key folder/x --force --yes
docker exec lore-vault python -m lore_vault.cli.admin delete-note --space dev --vault folder/x --id note:abc --yes
```

UI 管理端點（`api.manage`，契約見 docs/ARCHITECTURE.md「UI 管理端點」）是上述指令的 HTTP 版，沿用
`storage.admin` 的規劃／執行函式，加上兩段式確認 token 與 space 硬範圍；另有 CLI 沒有的
`document_undelete`（schema v11：`document_tombstones` 補 filename／mime／size_bytes／version，
v11 前的舊墓碑不能復原）與 `document_retry`（`documents.manual_retries`，每份上限
`storage.manage.MAX_MANUAL_RETRIES` = 3）。對帳：

- `vaults.alias_integrity`：別名等於某 vault 的正式 key、或指向不存在的 vault 為 fail
- `tombstones.disjoint`：同一 id 同時在墓碑與現行 notes／documents 為 fail（undelete 必須同交易刪墓碑）
- `tombstones.note_snapshots`（schema v12）：note 墓碑的內容快照不是合法 JSON、`$.id` 與墓碑不符或缺還原
  必要欄位為 fail（不比對 `$.vault`，理由同上）
- `notes.attribution`（schema v12，A22）：有 note 缺 `principal`／`updated_by_principal` 為 fail；
  counts 另列未具名（`author` 為 null）筆數
- 前兩項有破壞資料變紅的測試（`tests/storage/test_manage_checks.py`），後兩項見
  `tests/api/test_authorship.py`；跨 space 洩漏與拿掉保護會紅見
  `tests/api/test_manage_leak.py`

space 對帳（分類 `space`，schema v7；DB 沒有 CHECK，只能靠對帳）：`space.valid_values`
（`vaults.space` 不在 `dev`／`lore`／`personal` 即 fail）、`space.key_prefix_agreement`（非 dev 的 key
或別名未以 `<space>/` 開頭即 fail）。兩項都有「手動改 DB 後變紅」的測試（`tests/storage/test_space_filter.py`）。

### blob 清理（`gc-blobs`）

`--blob-dir` 缺省走設定 `documents.blob_dir`（容器內 `/data/blobs`），兩者皆無或目錄不存在 → exit 1。
實作在 `storage/blobs.py`（`plan_gc`／`execute_gc`）。

- 孤兒判定與 doctor `documents.orphan_blobs` 共用 `scan_orphans`：沒有任何 `documents` 列引用
  （不分狀態，被取代、failed 都算引用；墓碑不算）。暫存檔只收符合 `<2 碼>/.<sha256>.<32 碼 hex>.tmp` 的
- 年齡門檻：只刪 mtime 超過 `--min-age-hours`（預設 1）的孤兒，避免刪到剛寫入、DB 交易還沒提交的 blob；
  暫存檔門檻取 `max(門檻, 1 小時)`，設 0 也不刪可能正在寫入的暫存檔。`BlobStore.put` 去重命中時刷新 mtime，
  舊孤兒被重新上傳時門檻才擋得住
- dry-run（預設）：印 JSON `counts`（孤兒筆數／位元組、未達門檻保留數、遺留暫存檔筆數／位元組、不明檔案數）、
  `orphans`（只列 sha256 前 12 碼，不讀內容）、`unexpected`（不符佈局檔案的相對路徑）。不開寫交易、不動檔
- `--yes`：重新規劃後持 DB 寫鎖（`BEGIN IMMEDIATE`），鎖內逐檔以 `path_for(sha)` 重算路徑、同一連線再確認仍無引用、
  重新 stat（mtime／大小變動或年齡不足就不刪）後才刪；持鎖期間其他寫者無法提交新的 documents 列。
  不符佈局的檔案一律不碰只報告。刪完移除空的 2 碼子目錄。輸出另附 `deleted`、`kept`（`referenced`／`changed`／
  `missing`）、`failed`
- 刪完 doctor `documents.orphan_blobs` 回 pass（不符佈局檔案或 1 小時內的孤兒仍會 warn，需人工處理或稍後再跑）
- 測試：`tests/storage/test_blob_gc.py`（含拿掉刪前再確認時會誤刪的反向測試）、`tests/cli/test_admin_gc_blobs.py`

```bash
docker exec lore-vault python -m lore_vault.cli.admin gc-blobs          # dry-run
docker exec lore-vault python -m lore_vault.cli.admin gc-blobs --yes
```

Git Bash 下帶容器內絕對路徑（如 `--db /data/lore.db`）會被 MSYS 轉成 Windows 路徑，前面加 `MSYS_NO_PATHCONV=1`。

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

### 孤兒 note（不屬任何 notebook）

ON 有 note 沒掛在任何 notebook 上；全量 `GET /api/notes` 因含 NUL 的那則回 500，孤兒無法經 REST
列舉，清單（id／標題／created）要另外從 SurrealDB 唯讀查詢取得。

1. `orphans-map --orphans FILE --mapping mapping.json --out orphans-map.json [--projects-dir DIR ...]
   [--exclude ID ...]`：孤兒清單可為 JSON／JSONL（id、title、created）或 TSV（第 1 欄 id、第 2 欄
   created、最後一欄 title）。掃 Claude Code transcript（預設 `~/.claude/projects`）與 Codex rollout
   （預設 `~/.codex/sessions`）裡的 `create_note` 呼叫（正規化標題比對；同標題指向多個 vault 時只採信與
   ON created 相差 15 分鐘內的那幾筆），取該訊息的 `cwd` → `lore_vault.binding` 算 key → 對 mapping 的
   vault key 與 `aliases` 得最終 vault；另以 `update_note` 的 `note_id` 精確比對當輔助證據。
   **只讀工具呼叫 input 的 title／note_id／notebook_id 與訊息的 cwd／timestamp**，不讀 tool_result。
   找不到、歧義、或 key 不在 mapping 的標 `needs_review`，人工填 `vault` 後設 false；`--exclude` 的
   id 標 `skip`（測試／佔位）。Claude Code 只保留約 30 天的 transcript，更早建立的 note 多半找不到
2. `export-orphans --orphans-map FILE --export DIR [--supplement FILE]`：未略過的孤兒逐筆
   `GET /api/notes/{id}`，寫 `<export>/orphans.jsonl` 與 `orphans-manifest.json`（雜湊、來源筆數、
   `unavailable`）。REST 取不到的（含 NUL 的那則逐筆端點也 500）用 `--supplement` 補，見下
3. `import ... --orphans-map FILE`：孤兒依 mapping 的 vault（可寫別名）匯入，同樣進對帳清單與
   vault 來源筆數。還有 `needs_review` 項時拒絕（`--allow-unreviewed` 下未指定 vault 的跳過並列
   `orphans.unassigned`）；指定了 vault 卻沒有內容紀錄、或 vault 不在 mapping → 直接失敗。
   **匯入過孤兒後每次重跑都要帶同一份 `--orphans-map`**：對帳清單以「這次來源」整批替換，
   沒帶的話孤兒會從清單移除（note 仍在，對帳改算「新系統新增」）

**含 NUL 的孤兒（手動步驟，不經現行 PM 容器）**：REST 取不到，改從 SurrealDB 的**唯讀副本**匯出。
不直接查現行容器（避免對執行中的 PM 做任何事）；複製 `surreal_data/` 到另一個目錄、另起一個
SurrealDB 指向副本（不同埠、不掛現行 compose），在資料庫端把 NUL 換成兩字元 `\0` 再取出：

```sql
SELECT meta::id(id) AS rid, title, string::replace(content, "\u{0}", "\\0") AS content,
       note_type, created, updated
FROM note WHERE id = note:7ha92hoelu2a4ajoxvmb;
```

（未在副本上實測：SurrealQL 的 NUL 跳脫寫法、以及 v2 取 record id 的函式名（`meta::id`／`record::id`）
執行前先確認；可用 `string::len` 比對替換前後長度。）
把結果整理成 JSONL（每行 `id`＝`note:<rid>`、`title`、`content`、`created`、`updated`）當 `--supplement`。
supplement 內仍有 NUL 也沒關係：匯入時一樣會清理並記在報告 `sanitized`。

## 控制字元防護（NUL 等）

舊 PM 有一則 note 內文夾了真正的 NUL 位元組，整則永久讀不出來、全量列表 500。定義集中在
`lore_vault.schema.chars`（純標準庫，hook 也 import）：禁用 C0 控制字元（tab、LF、CR 除外，共 29 個）
與孤立 surrogate。

| 路徑 | 行為 |
|---|---|
| notes 寫入（HTTP／MCP 的 write、update；title、body、topics、links、supersedes） | 拒收：400 `invalid_characters`，附 `field`（清單欄位帶索引，如 `topics[1]`）、`index`（字元索引，0 起算）、`codepoint`、`kind`；不回顯內容。最底層在 `storage.notes.insert_note`／`update_note_if`，繞過服務層直接呼叫也擋得住 |
| ON 匯入 | 不拒收：NUL → 兩字元 `\0`、其他 C0 → `\xNN`、孤立 surrogate → `\uXXXX`；報告 `sanitized` 逐則列替換數。清理在算內容雜湊之前，重跑冪等 |
| episode 收料 | 客戶端寫 spool 前清理（spool 檔 `sanitized` 記替換數）；服務端 `POST /v1/episodes` 收到仍含控制字元（舊客戶端）也清理，逐筆結果標 `sanitized: true`、`sanitized_chars`。清理冪等，新舊客戶端送同一輪會是 duplicate |
| LLM 摘要（背景補算） | 清理後寫入（衍生文字，拒收只會無限重試） |

doctor `storage.control_chars`（分類 `storage`）：掃 notes（title／body／summary／topics）、concepts、
episodes 的 data，有即 fail 並列 id（不列內容）。SQLite 對含 NUL 的 TEXT：Python sqlite3 能完整讀回、
FTS5 照樣索引，但 `length()`／`LIKE`／`substr()` 在 NUL 處截斷、JSON1 判為不合法（實測見
`tests/storage/test_control_chars.py`），所以掃描一律 `CAST(col AS BLOB)` 找位元組；JSON 欄另以
`\u00`／`\b`／`\f`／`\ud` 粗篩後在 Python 解析確認。

## spike 接入：hook 端 spool、推送與 concept 快照（T-38～T-40）

hook 進入點（`agent_memory_spike/hook_stop.py`、`hook_pretooluse.py`）依自身位置把 repo 的 `src/`
加進 `sys.path`，只 import 純標準庫的 `lore_vault.hooks`（`client_env`、`service`、`spool`、
`concept_snapshot`）與 `lore_vault.binding`／`lore_vault.schema`。doctor `hooks.stdlib_only` 掃描範圍
包含 spike `hook_*.py` 及其平鋪 import 的同目錄模組，並遞迴掃允許的子套件；docker 映像內沒有 spike
目錄時只掃 `lore_vault/hooks`。

**切換（改全域 hook、`~/.claude.json`、排程）是另一張需授權的卡，這裡的程式不會自己生效。**

### hook 端設定 `client.env`

預設 `~/.claude/agent-memory-spike/client.env`（`paths.CLIENT_ENV_PATH`；`LORE_VAULT_CLIENT_ENV` 可改位置），
KEY=VALUE、只用標準庫解析；行程環境變數中同名鍵優先。

| 鍵 | 說明 |
|---|---|
| `LORE_VAULT_URL` | 服務位址（本機 `http://127.0.0.1:5056`，遠端 `https://pm-api...`） |
| `LORE_VAULT_API_TOKEN` | bearer token |
| `CF_ACCESS_CLIENT_ID`／`CF_ACCESS_CLIENT_SECRET` | 遠端用；只有一個時視為設定錯誤、不推送 |
| `LORE_VAULT_PUSH_TIMEOUT`／`LORE_VAULT_PUSH_BATCH` | 推送逾時（預設 1 秒）／單次最多筆數（預設 20） |
| `LORE_VAULT_CONCEPT_SNAPSHOT` | PreToolUse 改讀的 concept 快照檔；未設＝沿用現行 `concepts.json` |

未設 URL 或 token＝推送未設定：Stop hook 只寫 spool。密鑰不進 log、例外訊息、spool 與 `push_state.json`。

### episode spool 與推送（T-38／T-39）

- Stop hook 照舊寫 `episodes/<session>.jsonl`（過渡期雙寫），新輪次另寫 `spool/pending/<id>.json`
  （每筆一檔、暫存檔 + `os.replace`），內容附寫入當下凍結的 `machine`（`platform.node()`）與
  `vault`（`lore_vault.binding` 依 `repo_root`；解析不到退回 `folder/<repo>`，連 repo 都沒有為
  `folder/unknown`）。推送、重播都原樣送出，不重算
- Stop hook 尾端推一批（`POST /v1/episodes`）：accepted／duplicate 刪檔；conflict／invalid 移到
  `spool/rejected/`；其他情況留在 pending。硬性時限＝逾時 + 0.5 秒（推送在 daemon 執行緒，DNS 卡住也不拖住
  Stop）；失敗後退避 60 秒內不再嘗試
- 手動／排程：`python agent_memory_spike/hook_stop.py --push`（推到清空或失敗為止，失敗 exit 1）；
  `--push --dry-run` 只印待推送數與設定狀態
- 量測（本機、系統 Python、每次 1 筆新輪次）：推送未設定時整支 Stop hook 比 HEAD 多約 100 ms
  （binding／schema import 25–45 ms、`git remote` 約 35 ms、fsync 約 12 ms）；服務不可達時多一次逾時
  （預設約 2–2.5 秒，之後 60 秒退避期內 < 1 ms）

### concept 快照（T-40）

- MCP 殼拉 notes 快照時一併拉 `GET /v1/concepts/export`（`If-None-Match` 帶本地 sha256，304 只更新
  `checked_at`），驗證是 concept 物件陣列且 sha256 等於 ETag 後原子寫成 `mcp.concept_snapshot_path`
  （同 `concepts.json` 格式）＋ `<檔名>.manifest.json`；失敗舊檔不動
- PreToolUse 在 `client.env` 設了 `LORE_VAULT_CONCEPT_SNAPSHOT` 才改讀快照（切換時兩邊指到同一個檔）；
  校準門檻與 scorer 不變（等價測試：`agent_memory_spike/test_service_bridge.py`）。快照缺失／損毀 →
  不注入、stderr 一行 `[inject] 降級：...`，不拋例外

### doctor

`python -m lore_vault.doctor --category spool --spool-dir DIR [--client-env FILE]
[--spool-warn-age-hours 1] [--spool-fail-age-hours 24]`、`--category concept_snapshot --concept-snapshot FILE
[--concept-snapshot-max-age-hours 24]`。

- `spool.pending`：最舊一筆待推送超過 fail 門檻為 fail、超過 warn 門檻為 warn；推送未設定為 warn
- `spool.conflicts`：`rejected/` 非零為 fail（服務拒收或本地檔損毀，需人工處理）
- `concept_snapshot.age`：從未拉取、manifest 與檔案 sha256 不一致、格式不符、超過年齡（以 `checked_at` 計）為 fail
- `concept_snapshot.path_agreement`：client.env 的 `LORE_VAULT_CONCEPT_SNAPSHOT` 與 MCP 快照路徑（`--mcp-concept-snapshot-path`，未給則由設定推導 `mcp.concept_snapshot_path`／`<snapshot_dir>/concepts.json`）不是同一檔為 fail；任一邊未設為 skipped

### 主機管線轉接（骨架，預設關閉）

`pipeline.py --pull-episodes OUT.jsonl [--since UTC]`（`GET /v1/episodes`，全部 vault、依 cursor 讀到底）、
`pipeline.py --push-concepts [--dry-run]`（`POST /v1/concepts` upsert；刪除＝上次推過、這次已不在池內的 id，
記在 `pipeline_state.json`）。尚未接進 STAGES，三個判卷階段不變。

## 文件存儲與檢索（T-58～T-69）

- schema v8：`documents`（必屬某 vault，查詢經 `vault_clause` 強制 space＋vault）、`document_chunks`、
  `chunk_fts`（比照 `note_fts`）、`document_chunk_embeddings`、`document_tombstones`、`document_enrichment`。
  v9：`documents.encoding`（文字檔偵測到的編碼）、`document_chunks.overlap`（與前一段重疊的字元數，
  `get(doc:…)` 串回全文時略過）、`document_enrichment.kind` 加 `'embedding'`（向量補算的嘗試紀錄）。
  v10：`documents.warnings`（抽取品質警示，JSON 陣列 `[{code, detail}]`，沒有為 NULL；get／list 回 `warnings`）。
  metadata 與版本在 `storage/documents.py`；chunk、索引同步、worker 佇列與對帳在 `storage/document_index.py`；
  chunk 向量在 `storage/chunk_vectors.py`
- blob：`storage/blobs.py` 的 `BlobStore`，`<documents.blob_dir>/<sha256 前 2 碼>/<sha256>`，同目錄暫存檔＋`os.replace`，
  讀取驗雜湊。容器內 `blob_dir = "/data/blobs"`（named volume），寫入路徑不刪 blob（孤兒由 doctor 回報，清理用管理指令 `gc-blobs`）
- 抽取器：`lore_vault.documents.extract.extract(data, filename, mime, limits=Limits.from_config(cfg.documents))`，
  成功回 `Extraction`（segments 非空、`encoding`），失敗拋 `ExtractionError(code, detail)`；格式判定與錯誤碼見模組 docstring。
  文字檔編碼依序：UTF-16 BOM → UTF-8（可帶 BOM）→ cp950（Big5）；cp950 須嚴格解碼成功且通過文字性檢查
  （非 ASCII 字元 ≥ 90% 為中文字／注音／CJK 標點、中文字 ≥ 80% 落在 Big5 常用字區——擋 GBK 等誤解），否則
  `unsupported_encoding`。`min_chars`（預設 50）只套 pdf（判掃描件），docx／pptx／文字格式只在完全沒字時 `empty_extraction`。
  cp950 判定通過但樣本小（非 ASCII 字元 < 50）或亂碼跡象 > 可見字元 1% → 仍成功，`Extraction.warnings` 帶
  `encoding_low_confidence`，存進 `documents.warnings`
- docx／pptx 容器檢查（`check_ooxml_container`，交給 python-docx／python-pptx 之前）：以 `ZipFile.open` 每 64KB
  串流解壓每個成員、累計**實際**位元組（不留內容，記憶體峰值與宣告值無關）。成員數 > `Limits.max_zip_members`
  （10,000）、宣告或實際解壓總量 > `max_bytes × unzip_ratio`（200MB）、單一成員實際解壓 > 1MB 且壓縮比 >
  `max_member_ratio`（200）→ `too_large`；實際解壓量與宣告的 `file_size` 不符或 CRC 錯誤 → `corrupt`。
  竄改宣告大小的 50MB 炸彈修前峰值約 134MB、修後約 0.3MB（`tests/documents/test_extract_hardening.py`）。
  兩個套件的 lxml parser 都是 `resolve_entities=False`：外部實體（XXE）不讀檔、billion laughs 不展開（同檔測試）
- 切段（`documents/chunking.py`）：先依結構段（標題／頁／投影片／整份），超過上限才段內定長切。
  預設每 chunk 估算 ≤ 400 token、重疊 50 token（12.5%）；token 以字元粗估（CJK 1.5、其他非空白 0.25、空白 0，
  約 266 個中文字／1600 個英文字元）。切點優先：空行 → 換行 → 句末標點 → 空白／逗號 → 硬切。
  locator：`offset` 類改成段內起始位置；其他類加 `part`（1 起算）。不設最小長度（短投影片照收）
- 上傳（`POST /v1/documents`，multipart：`file`、`vault`、`space` 必填，`filename?`、`mime?`）：
  大小在讀 body 時就擋（`Content-Length` 超過先拒；chunked 則邊讀邊數），413 `too_large`；格式不支援 400
  `unsupported_format`；兩者都不寫 blob、不建列。未設 `blob_dir` 回 500 `documents_not_configured`。
  回應 `{document_id, status, sha256, duplicate, retried, vault, space, filename, version, supersedes, size_bytes}`
  （新列或重試 201、duplicate 200）。重複上傳規則（同 vault 內）：
  - 同 sha256 且現行（ready／pending／extracting、未被取代）→ 回既有文件、`duplicate: true`、不重新排隊
  - 同 sha256 的現行列只有 failed → 沿用那一列改回 pending 重跑（檔名／MIME 換成這次的）、`retried: true`
  - 同檔名、內容不同 → 新版本：`supersedes` 指向同檔名的現行版本（非 failed、未被取代），`version` = 該檔名最大版本 + 1
  - 同 sha256 的列都已被取代（改回舊內容）→ 當新內容處理（新版本），blob 共用
- 版本與索引資格：「被取代」＝任一 ready 文件沿 `supersedes` 一路往前追到的版本（遞移；中間版本失敗也算）。
  被取代的文件在新版 ready 的**同一交易**退出 chunk_fts 與向量（新版沒 ready 前舊版照常可搜），但仍可
  `get`／`list`（標 `superseded_by`）
- 背景 worker（`documents/worker.py`，`api.background.BackgroundEnricher` 以 `name="lore-vault-documents"` 在服務程序內跑；
  `api.document_worker`，預設開，`blob_dir` 未設時不啟動；上傳時喚醒）：pending → extracting → ready／failed。
  `ExtractionError` 直接 failed（不重試）；其他例外（含 blob 遺失／損毀）有上限重試（`worker.max_attempts`、
  `worker.retry_backoff` 指數退避），達上限 failed（`error_code=corrupt`，detail 記原因）。程序中斷遺留的
  extracting 在 worker 第一輪收回 pending。pdf／docx／pptx 在子行程抽取（`documents/isolation.py`，一律 spawn）：
  子行程就緒後超過 `documents.extract_timeout`（60 秒）即 kill，標 failed `corrupt`（detail 含 `timeout`），佇列繼續；
  子行程異常結束（被殺、沒回結果）走上面的有上限重試；服務關閉時中止子行程，文件留在 extracting 待下次收回。
  子行程記憶體以 `RLIMIT_AS` 限制為 `documents.extract_memory_mb`（1024MB，0 = 不限；只在 Linux 生效，
  Windows 沒有此能力，只有逾時保護），超過時 `too_large`。文字格式照舊在行程內抽取（有字元上限）。
  ready 後補 chunk 向量（`embedding.*` 設定，含 keep_alive、每分鐘上限）；
  向量失敗以文件為單位記嘗試，達上限不再自動補（lexical 仍可命中）。每輪有動作印一行
  `文件本輪：抽取 完成 …／失敗 …／重試 …／放棄 …；向量 …。剩餘 待抽取 …、缺向量 chunk …`
- recall／get／list：見 docs/ARCHITECTURE.md「MCP 介面」。快照（`GET /v1/snapshot`）明確排除文件表
  （`storage.snapshot.SNAPSHOT_EXCLUDED_TABLES`，產生時核對為空，否則拒絕產生）
- 設定 `[documents]`：`blob_dir`、`max_file_bytes`（25MB）、`max_chars`（1000 萬）、`min_chars`（50，只套 pdf）、
  `chunk_max_tokens`（400）、`chunk_overlap_tokens`（50，不可超過上限一半）、`stuck_seconds`（3600）、
  `extract_timeout`（60 秒）、`extract_memory_mb`（1024，只在 Linux 生效）；
  `[api] document_worker`（true）；`[mcp] upload_roots`
- doctor 分類 `documents`（`--blob-dir`，未給則取設定 `documents.blob_dir`，都沒有時 blob 兩項為 skipped；
  沒有 documents 表的舊 schema 全部 skipped）：
  - `documents.blob_exists`：任何 document 引用的 blob 遺失或雜湊不符為 fail（不分狀態）
  - `documents.orphan_blobs`：無引用的 blob、不符佈局的檔案、超過 1 小時的遺留暫存檔為 warn（判定與 `gc-blobs` 共用）
  - `documents.chunk_count_matches`：ready 文件的 `chunk_count` ≠ 實際 chunk 數、或非 ready 文件有 chunk 為 fail
  - `documents.fts_rows_match_chunks`：chunk_fts 與可索引文件（ready、未被取代）的 chunk 不是一對一為 fail
  - `documents.superseded_chunks_removed`：被取代或非 ready 的文件仍有 FTS／向量列為 fail（recall 會回舊版）
  - `documents.vector_rows_match_chunks`：孤兒向量或維度不符為 fail；可索引 chunk 缺向量為 warn
  - `documents.stuck_processing`：extracting 超過 `documents_stuck_seconds`（預設 3600）為 fail
  - `documents.failed`：抽取失敗與向量補算放棄的文件數（附錯誤碼分布）為 warn
  - `documents.quality_warnings`：ready 文件帶抽取品質警示（目前只有 cp950 判定信心低 `encoding_low_confidence`）
    為 warn，列出文件與原因；schema 未到 v10 為 skipped
  - `documents.backlog`：待抽取文件與缺向量 chunk，最舊一筆等超過 1 小時為 warn
  - 以上每項都有「破壞資料後變紅／黃」的測試（`tests/storage/test_document_checks.py`）
- `/v1/status` 另附 `documents: {enabled, worker, backlog}`；文件 worker 起不來（fatal）時 `ok: false`

## UI 開發與建置（A21）

前端在 `ui/app/`（Preact + Vite + TypeScript），建置產物由服務在 `/ui` 提供（同源，無 CORS）。
設計稿與設計系統原檔在 `ui/design-source/`（僅供參考）；`ui/app/src/styles/ds/` 是設計系統
`tokens/*.css` 與 `components/components.css` 的**原樣複製**（檔頭註明來源；更新設計系統時整檔重新複製，
不直接修改），`tokens/fonts.css` 不複製——它 `@import` Google Fonts，改為 `src/fonts.ts` 以 fontsource
自託管（CSP 維持 `font-src 'self'`；只載用到的字重，拉丁字型只取 latin 子集，建置時濾掉 woff 只留 woff2）。

| 用途 | 指令（在 `ui/app/`） |
|---|---|
| 安裝相依（依 `package-lock.json`） | `npm ci` |
| 開發（Vite dev server，API 轉到本機服務） | `npm run dev` |
| 建置到 `ui/app/dist/` | `npm run build`（先跑 typecheck） |
| lint + typecheck | `npm run lint` |
| 單元測試（vitest） | `npm test` |
| E2E（Playwright） | `npm run build && npm run e2e`（首次需 `npx playwright install --only-shell chromium`） |

### 本機開發

1. 照「啟動 API（本機開發）」在 `127.0.0.1:8000` 跑服務（`ui.static_dir` 可不設）
2. `npm run dev`：`/v1`、`/ui/api` 由 Vite proxy 轉到 `LORE_VAULT_DEV_API`（預設 `http://127.0.0.1:8000`），
   瀏覽器開 `http://localhost:5173/ui/`。同源 proxy 讓 cookie 行為與正式部署一致
3. 或直接由服務提供建置產物：`npm run build` 後設 `LORE_VAULT_UI_STATIC_DIR=<repo>/ui/app/dist`，開
   `http://localhost:8000/ui/`

`ui.cookie_secure` 預設開（cookie 名 `__Host-lv_session`）；瀏覽器把 `http://localhost` 視為安全來源，
本機多半不用關。只有用非 localhost 的 http 位址開發時才設 `LORE_VAULT_UI_COOKIE_SECURE=false`
（cookie 名改為 `lv_session`）。

### 身分驗證

- 登入金鑰就是 `LORE_VAULT_API_TOKEN`（不另設密碼）。`POST /ui/api/login` 成功後發 session cookie：
  HttpOnly、SameSite=Strict、Path=/、Secure（可設定）、Max-Age = 絕對期限
- 作者（A22）：登入金鑰與 bearer 共用 `api.principals` 的「憑證 → principal」對照（目前唯一的 token → `xavier`），
  session 記住登入時的 principal（`GET /ui/api/session` 回 `principal`），UI 發出的寫入記在該 principal 下。
  寫入時前端在 body 帶 `author: "Xavier (Bernie)"`；服務端不強制、只記錄，也不代填
- session 只存在服務記憶體（以 sha256(session id) 為鍵）：**服務重啟即全部失效**，重新登入即可。
  期限：絕對 `ui.session_absolute_hours`（預設 12）、閒置 `ui.session_idle_minutes`（預設 60）；
  同時上限 `ui.max_sessions`（預設 32，超過淘汰最舊）
- CSRF：cookie 認證的請求（含登入、登出）一律要求 `X-Lore-Vault-UI: 1`，外加 SameSite=Strict；
  服務不回 CORS 標頭，跨站頁面帶不出自訂標頭。帶 `Authorization` 的請求只走 bearer、不看 cookie
- 登入限流：時間窗 `ui.login_failure_window_seconds`（900）內，同一來源失敗 `ui.login_max_failures_per_ip`（5）
  次或全域 `ui.login_max_failures_global`（20）次後回 429（`Retry-After`），退避 `ui.login_lockout_seconds`
  （60）× 2^(超出次數)，不超過時間窗；退避期間正確金鑰也擋。每次嘗試都記 log（來源 IP 與結果，不記金鑰）
- 來源 IP：預設用直接連線位址。`ui.trusted_proxies`（逗號分隔 IP／CIDR）設定後，只有來自這些位址的
  請求才採信 `CF-Connecting-IP`；不看 `X-Forwarded-For`。未設定時經 tunnel 進來的請求共用同一個來源，
  每來源上限實際上等同全域上限
- `/ui` 回應帶嚴格 CSP（`default-src 'self'`、無 `unsafe-inline`、`frame-ancestors 'none'`）、
  `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY` 等；
  `/ui/api/*` 另加 `Cache-Control: no-store`，`index.html` 為 `no-cache`

### API client 慣例（給畫面卡）

`src/lib/api.ts` 的 `createApiClient`：非 2xx 一律丟 `ApiError`（`status`、`code`、`message`、原始 `body`、
`retryAfter`），非 JSON 回應與網路失敗也轉成 `ApiError`；資料端點 401 會通知 App 回登入頁。成功回應附
`notices`（`degraded`／`truncated`／`omitted`／`unsupported_kinds`／`missing`／`unavailable`，含巢狀項目），
原始資料不刪改——**畫面必須呈現 notices**，不能只取 `data`。

### E2E

`playwright.config.ts` 以 `uv run uvicorn` 起一個臨時服務（系統暫存目錄的新資料庫、固定測試 token、
`LORE_VAULT_CONFIG` 清空、背景 worker 與暖機關閉、port 5199），提供 `dist/`；不碰執行中的服務。
登入 smoke 測試同時斷言頁面沒有任何 CSP 違規與 console 錯誤。

### docker

`Dockerfile` 多一個 `ui-builder` 階段（`node:22-slim`：`npm ci` → `npm run build`），執行階段只
`COPY --from=ui-builder /ui/dist /app/ui`，node 不進最終映像；`docker/config.toml` 設 `ui.static_dir = "/app/ui"`。
`.dockerignore` 放行 `ui/app/`，但排除 `node_modules`、`dist` 與測試產物。
