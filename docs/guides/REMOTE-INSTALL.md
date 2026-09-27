# 客戶端安裝（Claude Code 連 Lore Vault 服務）

把一台機器的 Claude Code 接上已架好的 Lore Vault 服務（架服務見 [SELF-HOST.md](SELF-HOST.md)），並裝上 pm skill。
以下 `<服務位址>` 指服務的根網址，例如 `https://vault.example.com` 或本機的 `http://127.0.0.1:5056`（不含 `/mcp`）。

## 兩種模式

| | HTTP 模式（建議） | 完整殼模式 |
|---|---|---|
| 連線 | Claude Code 直連服務的 `<服務位址>/mcp`（Streamable HTTP） | 本機 venv 跑 stdio 殼，殼再呼叫服務的 HTTP API |
| 客戶端需求 | Claude Code CLI | Claude Code CLI、Python ≥ 3.12、uv、kit 內的 wheel |
| token 存放 | `~/.claude.json`（`claude mcp add --header`） | `~/.lore-vault/mcp.env`，不進 `~/.claude.json` |
| `vault_resolve` | 由 agent 傳 `remote_url`（`git remote get-url origin`）或 `key` | 殼用工作目錄自動推算 |
| `upload` | 檔名＋base64 內容 | 本機路徑 |
| 服務斷線時 | 工具直接失敗 | 唯讀的本地快照（`degraded=true`） |
| 更新 | 服務端升級即可，客戶端 `/mcp` 重連 | 客戶端要重裝 wheel（`install.py --update`） |

兩種模式的工具定義相同；pm skill 已同時涵蓋兩者的差異。

## 最短路徑：只登記 HTTP MCP

```bash
claude mcp add --transport http -s user lore-vault <服務位址>/mcp \
  --header "Authorization: Bearer <token>"
```

- `--header` 可重複，而且會吞掉後面的位置參數，**一定放在名稱與網址之後**
- 服務前面有 Cloudflare Access 時再加兩個 header：
  `--header "CF-Access-Client-Id: <id>" --header "CF-Access-Client-Secret: <secret>"`
- token 會以明文存在 `~/.claude.json`；不要在共用帳號的機器上用
- 登記時 token 會出現在 `claude mcp add` 的命令列參數，執行期間同機其他使用者可從行程清單看到；多人共用的機器改用完整殼模式
- pm skill 另外複製 repo 的 `integrations/claude/skills/pm/SKILL.md` 到 `~/.claude/skills/pm/SKILL.md`

要一併備份設定、自檢連線、安裝 skill，用下面的安裝程式。

## 安裝程式

安裝程式只用 Python 標準庫，由目標機的**人類**執行（會以不回顯方式詢問 token）。

1. **服務主機**（Lore-Vault repo）打包 kit：

   ```bash
   uv run python scripts/build_remote_kit.py --out <輸出目錄>
   ```

   產出 `lore-vault-kit-<版本>-<日期>-<commit>/` 與同名 `.zip`：wheel、`SKILL.md`、`install.py`、`README.txt`（含 wheel sha256）。
   kit 不含任何密鑰與服務位址。未指定 `--out` 時輸出到系統暫存的 `lore-vault-kits/`；同名 kit 已存在要加 `--force`。
2. **傳到目標機**並解壓。
3. **目標機**在 kit 資料夾執行（先完全結束 Claude Code；PowerShell 5.1／cmd／bash 皆可）：

   ```bash
   python install.py --base-url <服務位址> --dry-run   # 先看會做什麼，不寫檔
   python install.py --base-url <服務位址>             # 互動安裝；會詢問模式與 token
   ```

   - **HTTP 模式**：備份 `~/.claude.json` 與 pm skill（`.bak-precutover`，已存在不覆蓋）→ 詢問 token
     → 直接呼叫一次 `<服務位址>/v1/status` 自檢（失敗就停下，不留下壞掉的條目）
     → `claude mcp add --transport http -s user lore-vault <服務位址>/mcp --header ...` → 顯示差異摘要後覆寫 pm skill
   - **完整殼模式**：備份 → 建 venv → `uv pip install --reinstall` wheel → 寫 `mcp.toml` → 寫 `mcp.env`（UTF-8 無 BOM）
     → `claude mcp add lore-vault -s user -- <venv python> -m lore_vault.mcp ...` → 覆寫 pm skill → 經殼自檢
   - 兩種模式都會移除舊的 `open-notebook` MCP 條目（user／local scope 才自動移除；project scope 只提示），並先移除既有的 user scope `lore-vault` 再重新登記
   - 自檢失敗分類：DNS／連線／TLS／逾時／轉址或 403（Cloudflare Access）／401（token）／服務錯誤
   - Git Bash（mintty）無法隱藏輸入：改用 PowerShell、`winpty python install.py`，或先設環境變數 `LORE_VAULT_API_TOKEN`
   - 任一步失敗會停下並說明處理方式；修正後重跑同一指令，已完成的步驟自動略過
4. **驗證報告**：最後印出報告（同時存到 `~/.lore-vault/install-report-<時間>.txt`），含模式、各步驟結果、wheel sha256、status 摘要、
   `claude mcp list` 的 lore-vault 條目；家目錄遮成 `~`，不含任何密鑰。重開 Claude Code 後照「驗證」一節確認。

旗標：

| 旗標 | 用途 |
|---|---|
| `--base-url URL` | 服務位址（必填；未給時互動詢問，`--yes` 下必須給，除非完整殼已有 `mcp.toml`） |
| `--mode http\|shell` | 指定模式；未給時互動詢問（預設 HTTP；已有殼設定時預設殼），`--yes` 下依既有設定 |
| `--cf-access-env FILE` | 選配：服務前面有 Cloudflare Access 時，含 `CF_ACCESS_CLIENT_ID`／`CF_ACCESS_CLIENT_SECRET` 的檔案；沒給時互動詢問是否使用 |
| `--update` | 完整殼：只重裝 wheel＋自檢，不動設定；之後 `/mcp` 重連即可。HTTP 模式不需要 |
| `--yes` | 非互動；token 取自環境變數 `LORE_VAULT_API_TOKEN`（完整殼：有設就覆寫 `mcp.env`，沒設則沿用既有），既有 `mcp.toml` 保留 |
| `--rollback` | 還原兩份 `.bak-precutover`（`~/.claude.json` 整份回到備份時狀態），之後重開 Claude Code；互動時會再確認（預設否），加 `--yes` 則直接還原 |
| `--no-mask` | 報告不把家目錄遮成 `~` |

以下手動步驟保留作為參考與除錯用；安裝程式失敗時可對照單步排查。

## 規則（agent 協助安裝時先讀）

- **標「人類」的步驟 agent 不執行**：這些步驟實測會被 agent 端安全分類器擋下。agent 把指令整理好交給人類貼上執行，完成後由 agent 驗證；不要換寫法、換工具或拆步驟繞過。
- **agent 不讀、不輸出任何密鑰值**：CF Access 憑證檔由殼或安裝程式自己讀；`LORE_VAULT_API_TOKEN` 由人類親手輸入。agent 只檢查「鍵存在」，驗證一律看 `status`。
- token 不經聊天室、不進任何 repo。完整殼模式下 token 也不寫進 `~/.claude.json`；HTTP 模式則必然存在 `~/.claude.json`。
- 路徑一律用 `$HOME`／`~`，不寫死使用者名稱。以下指令為 PowerShell。

## 手動安裝：完整殼模式

### 0. 取得 wheel —— 服務主機

在服務主機的 Lore-Vault repo：

```powershell
uv build --wheel   # 產出 dist/lore_vault-<版本>-py3-none-any.whl
```

傳到目標機器。**成功判準**：目標機器上有 `lore_vault-*.whl`，記下其絕對路徑（下稱 `<wheel>`）。

### 1. 盤點 —— agent（只讀）

逐項確認並回報，不修改任何東西：

| 項目 | 怎麼看 |
|---|---|
| 現有 MCP | `claude mcp list`；`~/.claude.json` 的 `mcpServers`（只看鍵名與 command，不輸出 env 值） |
| 舊 `open-notebook` 條目（若曾使用） | 同上，記下 scope（user／project） |
| CF Access 憑證檔（若服務有 Access） | `Test-Path <檔案>`（**不讀內容**） |
| pm skill | `~/.claude/skills/pm/SKILL.md` 是否存在、是否仍呼叫 `mcp__open-notebook__*` |

**成功判準**：回報上表結果。服務有 Cloudflare Access 但沒有憑證檔就停下，請人類先向服務管理者取得。

### 2. 備份 —— agent

```powershell
Copy-Item "$HOME\.claude.json" "$HOME\.claude.json.bak-precutover"
Copy-Item "$HOME\.claude\skills\pm\SKILL.md" "$HOME\.claude\skills\pm\SKILL.md.bak-precutover"
```

已存在 `.bak-precutover` 就不要覆蓋（那是更早的原始狀態），回報後沿用。

### 3. 建 venv —— agent

```powershell
uv venv "$HOME\.lore-vault\venv" --python 3.14
```

**成功判準**：`$HOME\.lore-vault\venv\Scripts\python.exe` 存在。

### 4. 安裝 wheel —— 人類

```powershell
uv pip install --python "$HOME\.lore-vault\venv" "<wheel>"
```

**成功判準**（agent 驗證）：`& "$HOME\.lore-vault\venv\Scripts\python.exe" -c "import lore_vault.mcp; print('ok')"` 印出 `ok`。

### 5. 寫 `mcp.toml` —— agent

`$HOME\.lore-vault\mcp.toml`（不含任何密鑰；設定檔出現 token／secret 類的鍵會拒絕載入）：

```toml
[mcp]
base_url = "<服務位址>"
snapshot_dir = "~/.lore-vault/snapshot"
timeout = 15.0
```

服務有 Cloudflare Access 時才加一行 `cf_access_env_file = "<憑證檔路徑>"`；**指向不存在的檔案會讓殼拒絕啟動**，沒用 Access 就不要寫。

### 6. 寫 `mcp.env` —— 人類

```powershell
notepad "$HOME\.lore-vault\mcp.env"
```

內容只有一行 `LORE_VAULT_API_TOKEN=<值>`，用記事本存（UTF-8 無 BOM）。不要在 PowerShell 5.1 用 `>`／`Out-File` 寫（會產生 UTF-16 或 BOM，鍵名讀不到）。

**成功判準**（agent 驗證，不輸出值）：`Select-String -Path "$HOME\.lore-vault\mcp.env" -Pattern '^LORE_VAULT_API_TOKEN=.+' -Quiet` 回 `True`。

### 7. 登記 MCP —— 人類

```powershell
claude mcp remove open-notebook -s user   # 只有曾用過舊 PM 才需要
claude mcp add lore-vault -s user -- "$HOME\.lore-vault\venv\Scripts\python.exe" -m lore_vault.mcp --config "$HOME\.lore-vault\mcp.toml" --env-file "$HOME\.lore-vault\mcp.env"
```

- **不要在 PowerShell 5.1 用 `claude mcp add-json`**：原生參數傳遞會吃掉 JSON 雙引號，回 `Invalid configuration: : Invalid input`。
- **成功判準**（agent 驗證）：`claude mcp list` 有 `lore-vault`；command 是 venv 的 `python.exe` 絕對路徑，args 為 `-m lore_vault.mcp --config <mcp.toml> --env-file <mcp.env>`。

### 8. 換 pm skill —— 人類

skill 取自 repo 的 `integrations/claude/skills/pm/SKILL.md`（kit 內的 `SKILL.md`）。agent 先確認它機器中立（`author` 寫「依本機 persona 的角色名」、沒有主機私有路徑），有不符就回報，不自行改寫。

```powershell
Copy-Item "<SKILL.md>" "$HOME\.claude\skills\pm\SKILL.md" -Force
```

**成功判準**：`allowed-tools` 為 `mcp__lore-vault__*`，不再出現 `mcp__open-notebook__`。

### 9. 重開 Claude Code —— 人類

完全結束 Claude Code 再開，新的 MCP 與 skill 才會載入。

## 驗證 —— agent

在新 session 呼叫 `mcp__lore-vault__status()`：

| 項目 | 預期 |
|---|---|
| 整體 | `ok: true` |
| schema 版本 | 與服務端預期一致 |
| doctor | 0 fail；spool、concept_snapshot 等只在服務主機存在的項目顯示 skipped，不算失敗 |

再確認：

- `mcp__lore-vault__space(action="get")` 為 `dev`
- HTTP 模式：先 `git remote get-url origin`，`vault_resolve(remote_url=...)` 後任意 `recall` 有回應
- 完整殼模式：`vault_resolve()` 後任意 `recall`，回應 `degraded` 為 `false`；`~/.lore-vault/snapshot/` 已生成 `snapshot.db` 與 `snapshot.json`（殼啟動後背景拉取，可能要等幾秒）

任一 fail：`401` 查 token；`403`／3xx 查 Cloudflare Access 憑證；連線失敗查服務位址；schema 不符（完整殼）表示 wheel 過舊，重打 kit 後 `--update`。

## 更新

- **HTTP 模式**：服務端升級即可，客戶端在 Claude Code 用 `/mcp` 重連。
- **完整殼模式**：取得新 kit 後 `python install.py --update`（等同 `uv pip install --reinstall --python ~/.lore-vault/venv <wheel>`；版本號未變時必須 `--reinstall`），再 `/mcp` 重連。

## 範圍外

- **收料**：本流程不安裝 hook，不收 episode。

## 回退

`python install.py --rollback`，或人類手動執行後重開 Claude Code：

```powershell
Copy-Item "$HOME\.claude.json.bak-precutover" "$HOME\.claude.json" -Force
Copy-Item "$HOME\.claude\skills\pm\SKILL.md.bak-precutover" "$HOME\.claude\skills\pm\SKILL.md" -Force
```

`~/.lore-vault/` 可留著，不影響舊設定。
