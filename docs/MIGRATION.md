# 遷移計畫

兩個來源要遷入：Open Notebook PM（資料）與 agent_memory_spike（程式 + 資料 + 掛載點）。
**遷移期間舊系統持續運作**，切換前新舊不並存掛載。

## A. agent_memory_spike

### 現況（2026-09-25）

- 位置：`TestSeperateMemorySystem/agent_memory_spike/`，分支 `feature/agent-memory-spike`
- 11 個模組約 6600 行 + 8 個測試檔 134 項測試，**純標準庫、零第三方依賴**
- flat import（`from transcript import ...`），無套件結構
- 與 `echo_memory/` **零耦合**
- git：58 個 commit 動過 spike，其中 56 個只動 `agent_memory_spike/`，另 2 個只多動 `.gitignore`

### 模組

| 模組 | 職責 |
|---|---|
| `transcript.py` | transcript → episode 解析（底層，其他都依賴它） |
| `hook_stop.py` | Stop hook 收料 + `--doctor` / `--repair` / `--sync-all` |
| `hook_pretooluse.py` | PreToolUse 注入（已掛載） |
| `hook_health_alert.py` | SessionStart 健康告警（已掛載，刻意不 import transcript） |
| `hook_session_start.py` / `hook_userpromptsubmit.py` | 注入對照組（刻意不掛載） |
| `distill.py` / `consolidate.py` / `calibrate.py` | 蒸餾 / 收斂 / 行為校準 |
| `retrieve.py` | BM25 + 檔案 / 符號訊號檢索 |
| `pipeline.py` | 收料 → 健檢 → 蒸餾 → 收斂 → 校準，lockfile |

### 必須處理的清單

1. **歷史保留**：`git subtree split -P agent_memory_spike`（或 `filter-repo --path agent_memory_spike/`）切出子樹再併入本 repo。那 2 個 `.gitignore` commit 的改動會被濾掉，要在本 repo 補規則（含 `data/golden_memories.json` 的白名單例外）。
2. **全域 hook 路徑**：`~/.claude/settings.json` 的 `SessionStart`（health alert）、`Stop`、`PreToolUse` 三個 hook 寫死舊絕對路徑。切換時**一次整批替換**，確認舊路徑完全移除——並存會造成一輪注入雙倍條目。
3. **排程**：Windows 工作排程器 `AgentMemoryPipeline`（每日 03:30）指向舊 `run_pipeline.ps1`，要重新註冊。腳本內假設 repo 與 `U.E.P-s-Core` 同層並使用 U.E.P env——**本 repo 不依賴 U.E.P env**，要改用自己的直譯器。
4. **PowerShell 腳本兩個坑要保留**：必須存成 UTF-8 with BOM（PS 5.1 會把無 BOM 的中文註解吃掉整行）；Python 輸出走 `cmd /c` 重導向，不用 `*>>`。
5. **資料目錄**：目前在 `~/.claude/agent-memory-spike/`（約 90MB，含多份 `.bak-*` 與實驗中間產物）。是否改名、中間產物是否搬〔待定：D5〕。資料路徑常數散在 `transcript.py` / `hook_stop.py` 等處。
6. **hook 直譯器**：目前是系統 Python 3.14（`pythoncore-3.14-64`），hook 必須維持零依賴或只依賴該直譯器可用的套件。
7. **對照組兩支**帶過來，README 延續「為什麼不掛」的說明，避免日後誤掛。
8. **headless `claude -p` 的四個坑**（pipeline 裁決用）：吃全域 CLAUDE.md 與 SessionStart 注入、寫不進 `~/.claude/`、Bash allowlist 認字面路徑、prompt 走 stdin。

### concept id 撞號（2026-09-26 發現）

- 根因：`distill.py` 以 `c-{len(concepts):03d}` 發號，收斂刪除後再新增會撞到仍存在的號碼。已改為高水位 sidecar（`<stem>.id_state.json`）+ 檔案鎖，號碼永不重用
- 現行資料：1538 筆只有 1273 個唯一 id（251 組重複）；197 筆注入紀錄中 75 筆指向撞號 id
- 切換時以 `agent_memory_spike/renumber_concepts.py` 重新編號（保留每組第一筆原號，其餘從 c-1809 起），注入紀錄加 `ambiguous_ids` 標記；sidecar 一併改名為 `concepts.id_state.json`。乾跑已驗證新檔 1538 筆經 `POST /v1/concepts` 全數建立、零衝突
- 無法回溯：已刪除的舊號若曾被重發，當時的注入紀錄指向另一條記憶，現行檔案看不出來

### 建議順序

1. subtree 併入 → 在本 repo 跑通 134 項測試（未改任何路徑）
2. 資料路徑常數集中成設定
3. 停排程 → 替換全域 hook → 重新註冊排程 → 跑一次 `--doctor` 與 pipeline
4. 確認 1–2 晚排程正常後，從 TestSeperateMemorySystem 移除 spike（另開 PR，不影響 echo_memory 主線）

## B. Open Notebook PM

### 現況

- 部署：`E:\ProgramFiles\Open-Notebook\docker-compose.yml`，`lfnovo/open_notebook:1.14.0` + `surrealdb/surrealdb:v2`（rocksdb，資料在 `surreal_data/`）
- API / MCP 端點 `http://localhost:5055`；Web UI 8502
- MCP：`Epochal-dev/open-notebook-mcp`（第三方，經 `uvx` 啟動，設定在 `~/.claude.json`）
- 本地小改：`~/.claude/pm/pm-bind.py`（remote → key）、`pm-proxy.py`（跨機器走 Cloudflare Access 的反向代理）、`pm-cache-sync.py`（匯出 markdown 唯讀快取到 `~/.claude/pm-cache/`）
- 規模：15 本 notebook、1300+ 則 note；Sources 使用數 0

### 遷移方式

- **匯出**：透過 REST API（或既有 `pm-cache-sync` 的 markdown 匯出）拿到每則 note 的 title / content / topics / created / updated 與所屬 notebook
- **綁定**：notebook description 中的 `[bind: <key>]` 轉成 Vault `key`；沒有標記的舊 notebook 以名稱 `[PM] <display>` 對應，歧義者人工確認
- **連結**：`[[標題]]` 解析成 note id；解析不到的保留原文並列入報告
- **摘要**：舊 note 沒有 `summary`，匯入時要補〔待定：D4〕
- **對帳**：匯入後逐 vault 比對 note 數與內容雜湊，doctor 能重跑
- **切換**：新 MCP 上線並驗證後，才從 `~/.claude.json` 移除 `open-notebook`；pm skill 與全域 CLAUDE.md 的記憶協定同步改寫。舊容器保留唯讀一段時間當退路

### 其他依賴者（2026-09-25 盤點）

切換 MCP 時要一起改，否則會靜默失效：

- **claude-codex-pipeline plugin**：`skills/dispatch/SKILL.md`、`skills/complete/SKILL.md`、`references/pm-integration.md` 直接寫死 `mcp__open-notebook__*`（`list_notebooks` / `search` / `create_note`），不經 pm skill；已安裝副本在 `~/.claude/plugins/cache/uep-pipeline/`
- **`~/.claude/pm-kit/`**：`pm-bind` / `pm-cache-sync` / `pm-proxy` 的第二份副本 + `setup.ps1`；pm skill 引用的是 `~/.claude/pm/`，切換時一併決定去留
- **各專案 CLAUDE.md** 的 `[PM] <repo>` 綁定敘述：Chatroom、Echo-Stream、Eternity、Lore-Vault、TestSeperateMemorySystem、U.E.P-s-Core
- **文件內引用 note 當決策出處**：如 `Chatroom/docs/REMOTE-OPS-PLAN.md:702`，匯入後需要能以舊標題找到新 note

### 實際使用頻率

從 `~/.claude/projects/**/*.jsonl` 統計 tool_use 呼叫：

| 工具 | 次數 |
|---|---|
| `create_note` | 269 |
| `search` | 206 |
| `list_notebooks` | 135 |
| `update_note` | 98 |
| `get_note` | 43 |
| `list_notes` | 27 |
| `create_notebook` / `update_notebook` / `delete_note` | 3 / 1 / 1 |
| 其餘 24 個工具 | 0 |

`list_notebooks` 的 135 次幾乎都是綁定查找（pm skill 的手動比對流程），由 `vault_resolve` 取代。

### Cloudflare 現況（2026-09-26 查）

- tunnel `pm`（`c70f36ba-…`），設定在 `~/.cloudflared/config.yml`：`pm.unforgettableeternalproject.com → localhost:8502`（Web UI）、`pm-api.unforgettableeternalproject.com → localhost:5055`（API）
- 遠端經 Cloudflare Access service token（`~/.cloudflared/pm-token.env`，由 `pm-proxy.py` 注入 header）
- 新服務試做期對外埠用 **5056**，不佔用 5055／8502；切換時把 `pm-api` ingress 改指 5056（需授權，T-45）。`pm` 子網域留給之後的 UI（A12）
- Access 應用與 policy 的實際設定未查（需 Cloudflare API 權限），切換前確認

### 已知坑（要在新系統避免重演）

- `search` 的 `notebook_id` 過濾無效，結果是全域的
- vector search 的 `matches` 帶回整篇全文
- text search 對中文幾乎搜不到（實測 0 筆）
- embedding / LLM 模型設定只在 Web UI，不在設定檔裡——不可版控、不可重現
