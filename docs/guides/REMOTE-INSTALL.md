# 其他機器安裝 Lore Vault MCP 殼（agent 操作手冊）

給「主機以外的機器」上的 agent 照做：把舊的 `open-notebook` MCP 換成連遠端服務的 Lore Vault MCP 殼，並換上新版 pm skill。
依據：2026-09-26 在第二台機器實測成功的流程。設定鍵與 token 來源優先序見 [DEVELOPMENT.md](../DEVELOPMENT.md)「MCP 殼」段。

## 規則（先讀）

- **標「人類」的步驟 agent 不執行**：這些步驟實測會被 agent 端安全分類器擋下。agent 把指令整理好交給人類貼上執行，完成後由 agent 驗證；不要換寫法、換工具或拆步驟繞過。
- **agent 不讀、不輸出任何密鑰值**：`~/.cloudflared/pm-token.env` 由 MCP 殼自己讀；`LORE_VAULT_API_TOKEN` 由人類親手寫入。agent 只檢查「鍵存在」，驗證一律看殼的 `status`。
- token 不經聊天室、不寫進 `~/.claude.json`、不進任何 repo。
- 路徑一律用 `$HOME`／`~`，不寫死使用者名稱。以下指令為 PowerShell。

## 前提

- Windows、Python ≥ 3.12、`uv`、Claude Code CLI（`claude`）
- 本機已有 `~/.cloudflared/pm-token.env`（含 `CF_ACCESS_CLIENT_ID`／`CF_ACCESS_CLIENT_SECRET`）
- 服務端點：`https://pm-api.unforgettableeternalproject.com`（CF Access service token ＋ bearer）
- 主機已產出 wheel（見步驟 0）

## 步驟

### 0. 取得 wheel —— 主機端（agent 或人類）

repo 沒有 git remote，遠端無法 clone。在主機的 Lore-Vault repo（`develop` 最新）：

```powershell
uv build --wheel   # 產出 dist/lore_vault-<版本>-py3-none-any.whl
```

經聊天室（`chatroom_send_file`）或其他方式傳到目標機器。

**成功判準**：目標機器上有 `lore_vault-*.whl`，記下其絕對路徑（下稱 `<wheel>`）。

### 1. 盤點 —— agent（只讀）

逐項確認並回報，不修改任何東西：

| 項目 | 怎麼看 |
|---|---|
| 現有 MCP | `claude mcp list`；`~/.claude.json` 的 `mcpServers`（只看鍵名與 command，不輸出 env 值） |
| 舊 `open-notebook` 條目 | 同上，記下 scope（user／project） |
| pm-proxy | `mcpServers` 或 command 是否引用 `pm-proxy.py` |
| `pm-token.env` | `Test-Path "$HOME\.cloudflared\pm-token.env"`（**不讀內容**） |
| hooks 是否引用 spike | `~/.claude/settings.json` 的 hooks 是否出現 `agent_memory_spike` |
| pm skill | `~/.claude/skills/pm/SKILL.md` 是否存在、是否仍呼叫 `mcp__open-notebook__*` |
| pipeline plugin | `~/.claude/plugins/` 下是否有 `claude-codex-pipeline`／`uep-pipeline` 且寫死 `mcp__open-notebook__*` |

**成功判準**：回報上表結果。`pm-token.env` 不存在就停下，請人類先補。hooks 引用 spike、pipeline plugin 寫死舊工具屬另案，只回報、本流程不改。

### 2. 備份 —— agent

```powershell
Copy-Item "$HOME\.claude.json" "$HOME\.claude.json.bak-precutover"
Copy-Item "$HOME\.claude\skills\pm\SKILL.md" "$HOME\.claude\skills\pm\SKILL.md.bak-precutover"
```

已存在 `.bak-precutover` 就不要覆蓋（那是更早的原始狀態），回報後沿用。

**成功判準**：兩個 `.bak-precutover` 檔存在。

### 3. 建 venv —— agent

```powershell
uv venv "$HOME\.lore-vault\venv" --python 3.14
```

**成功判準**：`$HOME\.lore-vault\venv\Scripts\python.exe` 存在。

### 4. 安裝 wheel —— 人類

agent 把 `<wheel>` 換成實際路徑後交給人類執行：

```powershell
uv pip install --python "$HOME\.lore-vault\venv" "<wheel>"
```

**成功判準**（agent 驗證）：

```powershell
& "$HOME\.lore-vault\venv\Scripts\python.exe" -c "import lore_vault.mcp; print('ok')"
```

印出 `ok`。

### 5. 寫 `mcp.toml` —— agent

`$HOME\.lore-vault\mcp.toml`（不含任何密鑰；設定檔出現 token／secret 類的鍵會拒絕載入）：

```toml
[mcp]
base_url = "https://pm-api.unforgettableeternalproject.com"
cf_access_env_file = "~/.cloudflared/pm-token.env"
snapshot_dir = "~/.lore-vault/snapshot"
timeout = 15.0
```

**成功判準**：檔案存在、內容如上。

### 6. 寫 `mcp.env` —— 人類

值取主機 Lore-Vault repo `.env` 的 `LORE_VAULT_API_TOKEN`，由人類親手貼上：

```powershell
notepad "$HOME\.lore-vault\mcp.env"
```

內容只有一行：

```text
LORE_VAULT_API_TOKEN=<值>
```

用記事本存（UTF-8 無 BOM）。不要在 PowerShell 5.1 用 `>`／`Out-File` 寫（會產生 UTF-16 或 BOM，鍵名讀不到）。

**成功判準**（agent 驗證，不輸出值）：

```powershell
Select-String -Path "$HOME\.lore-vault\mcp.env" -Pattern '^LORE_VAULT_API_TOKEN=.+' -Quiet
```

回 `True`。

### 7. 換 MCP 條目 —— 人類

```powershell
claude mcp remove open-notebook -s user
claude mcp add lore-vault -s user -- "$HOME\.lore-vault\venv\Scripts\python.exe" -m lore_vault.mcp --config "$HOME\.lore-vault\mcp.toml" --env-file "$HOME\.lore-vault\mcp.env"
```

- `open-notebook` 的 scope 以步驟 1 盤點為準（不是 user 就改 `-s`）；不存在就跳過 remove。
- **不要在 PowerShell 5.1 用 `claude mcp add-json`**：原生參數傳遞會吃掉 JSON 雙引號，回 `Invalid configuration: : Invalid input`。一定要用 add-json 時改在 Git Bash 執行。
- 舊條目若透過 `pm-proxy.py` 轉接，一併移除；pm-proxy 退役。

**成功判準**（agent 驗證）：`claude mcp list` 有 `lore-vault`、沒有 `open-notebook`；`lore-vault` 的 command 是 venv 的 `python.exe` 絕對路徑，args 為 `-m lore_vault.mcp --config <mcp.toml> --env-file <mcp.env>`。

### 8. 換 pm skill —— 人類

新版 pm skill 取自主機的 `~/.claude/skills/pm/SKILL.md`，隨 wheel 一起傳過來（它不含密鑰）。agent 先檢查它是否機器中立：

- `author` 寫的是「依本機 persona 的角色名」，沒有寫死特定角色
- MEMPAL 段落寫「有 MEMPAL 工具的機器才用」，沒有工具就略過
- 沒有主機私有路徑

有不符就先回報，不自行改寫。確認後交給人類執行：

```powershell
Copy-Item "<傳來的 SKILL.md>" "$HOME\.claude\skills\pm\SKILL.md" -Force
```

**成功判準**（agent 驗證）：`SKILL.md` 的 `allowed-tools` 為 `mcp__lore-vault__*`，不再出現 `mcp__open-notebook__`。

### 9. 重開 Claude Code —— 人類

完全結束 Claude Code 再開，新的 MCP 與 skill 才會載入。

- 重開後 session key 可能改變。若在聊天室協作：重掛指派 watcher、重新 join 房間，或請人類重新指派。

### 10. 驗證 —— agent

在新 session 呼叫 `mcp__lore-vault__status()`，對照：

| 項目 | 預期 |
|---|---|
| 整體 | `ok: true` |
| schema 版本 | 13 |
| doctor | 約 31 pass／0 fail／0 warn／7 skipped |
| skipped 項目 | spool、concept_snapshot、spike_home 等主機端功能——遠端正常缺，不算失敗 |
| `import.on_reconcile` | pass |

再確認：

- `mcp__lore-vault__space(action="get")` 為 `dev`
- `mcp__lore-vault__vault_resolve()` 後任意 `recall`，回應 `degraded` 為 `false`
- `$HOME\.lore-vault\snapshot\` 已生成 `snapshot.db` 與 `snapshot.json`（殼啟動後背景拉取，可能要等幾秒）

**成功判準**：以上全部符合。任一 fail：`401`／`403`／3xx 查 token 與 `pm-token.env`（不讀值，確認檔案與鍵存在）；連線失敗查 `base_url`；schema 不符表示 wheel 過舊，回主機重打。

## 範圍外（本流程不處理）

- **收料**：遠端不掛 hook，不收 episode；目前只由主機收料。
- **舊架構殘留**：Windows 排程 `PM Cache Sync`、`PM Proxy` 在觀察期結束前不動。之後再停用排程，並封存 `~/.claude/pm/`、`~/.claude/pm-kit/`。
- hooks 引用 spike、pipeline plugin 寫死 `mcp__open-notebook__*`：只在步驟 1 回報。

## 回退

人類執行後重開 Claude Code：

```powershell
Copy-Item "$HOME\.claude.json.bak-precutover" "$HOME\.claude.json" -Force
Copy-Item "$HOME\.claude\skills\pm\SKILL.md.bak-precutover" "$HOME\.claude\skills\pm\SKILL.md" -Force
```

`~/.lore-vault/` 可留著，不影響舊設定。
