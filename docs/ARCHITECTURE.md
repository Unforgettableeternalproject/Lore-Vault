# 架構

> 設計草案。標 **〔待定〕** 的項目見 [DECISIONS.md](DECISIONS.md)，未裁決前不要當成定案實作。

## 設計原則

1. **範圍是硬邊界，不是提示。** 每一次讀寫都必須帶 vault（專案範圍），在儲存層強制過濾；跨 vault 查詢要明確指定，不能因為參數沒傳就變成全域。
2. **先索引、後全文（progressive disclosure）。** 查詢預設只回 id / 標題 / 摘要片段 / 分數 / 更新時間；全文要第二步明確取。任何回傳都有字數預算上限。
3. **記憶價值 = surprisal，不是 salience。** 沿用 spike 的實證結論：值得注入的是模型不知道或自信地相信錯誤的事，只能用行為測試量，不能用 LLM 自評。
4. **靜默寫錯資料是頭號敵人。** spike 踩過的坑全都不拋例外。所有資料流都要有對帳（doctor），而且對帳本身要有測試證明它會紅。
5. **歷史歸屬凍結成欄位。** repo 名、根目錄、scope 在寫入當下固定，不在讀取時靠環境重算（見 spike 的 repo 改名事故）。
6. **只做 agent 需要的功能。** 不做來源匯入、Podcast、對話介面、多 provider 管理。

## 分層

```
┌────────────────────────────────────────────────────────────┐
│  介面層                                                       │
│   mcp/      MCP server（agent 主動查詢與寫入）                 │
│   hooks/    Claude Code / Codex hook（收料、注入、健康告警）   │
│   cli/      人工操作、doctor、匯出                             │
│   api/      HTTP API（跨機器，docker 常駐，見 A8）             │
├────────────────────────────────────────────────────────────┤
│  服務層                                                       │
│   notes     Notes 的 CRUD、查重、版本                          │
│   recall    統一檢索：範圍過濾 → 候選 → 排序 → 預算裁切         │
│   inject    注入選擇（錨點比對、節流、side-car 紀錄）           │
│   pipeline  蒸餾 → 收斂 → 校準（離線、排程）                   │
│   doctor    全資料流對帳                                       │
├────────────────────────────────────────────────────────────┤
│  核心層                                                       │
│   schema    Vault / Note / Episode / Concept / Injection       │
│   binding   git remote → 穩定 vault key（沿用 pm-bind 邏輯）   │
├────────────────────────────────────────────────────────────┤
│  儲存層（SQLite + FTS5 + NumPy 向量，見 A9）                  │
│   服務端為唯一真源；客戶端唯讀快照供 hook 使用                 │
│   唯讀降級快取（沿用 pm-cache 思路：匯出 markdown + manifest）  │
└────────────────────────────────────────────────────────────┘
```

介面層之間不互相呼叫，都經過服務層。hook 對延遲敏感（PreToolUse 每次編輯都跑），
所以 hook 路徑必須能**不啟動重量級依賴**就完成——spike 目前是純標準庫，這個性質要保住。

## 資料模型

### Vault

一個記憶範圍，通常對應一個 repo。

| 欄位 | 說明 |
|---|---|
| `key` | 穩定識別，git remote 正規化（如 `github.com/owner/repo`），無 remote 時 `folder/<name>` |
| `display` | 顯示名稱，保留大小寫 |
| `aliases` | 改名前的舊 key／舊 repo 名（取代 spike 的 `REPO_ALIASES`） |
| `kind` | `repo` / `global`（跨專案觀察） |

### Note（寫下的結論）

| 欄位 | 說明 |
|---|---|
| `id`, `vault` | 必填，vault 為硬範圍 |
| `title` | 查詢結果的主要呈現 |
| `summary` | 1–2 句摘要，查詢時回傳這個而不是全文〔待定：誰產生，見 D4〕 |
| `body` | markdown 全文，只在 `get` 時回傳 |
| `topics` | 標籤 |
| `links` | `[[標題]]` 互連，解析成 note id |
| `supersedes` | 更正關係：新 note 取代舊 note 時標記，而不是另建更正篇 |
| `created`, `updated` | |

### Episode（發生過的事）

沿用 spike 的 schema：每輪對話一筆，含 `session_id`、`prompt_id`、`turn_index`、`origin`、
`cwd`、`repo`、`repo_root`（凍結）、`files_edited`、`files_read`、`symbols_edited`、
`tool_sequence`、`user_text`、`assistant_text`、`injected`。

⚠️ 含商業專案原文。**資料目錄在 repo 外**，不進版控。

### Concept（蒸餾出的記憶）

沿用 spike：`statement`、`kind`（project-fact / belief-correction / user-stance）、
`scope`（三態：repo 名 / `None` = 跨專案通用 / 缺欄位）、`anchors`（檔案 + 符號）、
`cue`、`surprisal`、校準與收斂紀錄。

### Injection

side-car 紀錄：`{session_id, prompt_id, injected: [concept_id]}`，不含原文。
用來辨識哪些語料輪次被記憶影響過，避免污染後續校準。

## MCP 介面（草案）

目標是讓 agent 用最少的上下文拿到足夠決策的資訊。工具數量刻意壓在個位數。

| 工具 | 回傳 | 說明 |
|---|---|---|
| `vault_resolve(cwd)` | vault key、display、note 數 | 取代 pm-bind 的手動步驟 |
| `recall(query, vault, kinds?, limit?, budget?)` | `[{id, kind, title, summary, score, updated}]` | 統一檢索 Notes 與 Concepts；**預設不含全文**；`vault` 必填，跨範圍用 `vault="*"` 明示 |
| `get(ids, budget?)` | 全文 | 可批次；超過預算時截斷並標示 |
| `list(vault, since?, topics?, cursor?)` | 標題清單 | 分頁，回傳是否還有下一頁 |
| `write(vault, title, body, topics?, supersedes?)` | id、疑似重複清單 | 寫入前自動查重，回傳相似 note 讓 agent 決定改用 `update` |
| `update(id, body?, title?, topics?)` | id | |
| `status(vault?)` | 健康狀態、最近更新、管線狀態 | 合併 doctor 摘要與 health alert |

刻意**不做**：chat / ask（把記憶包成對話）、model 管理、settings、source 匯入。

## Hook 整合

沿用 spike 已驗證的掛載點：

| 事件 | 用途 | 狀態 |
|---|---|---|
| `Stop` | 收料（episode 寫入） | spike 已上線 |
| `PreToolUse` (Edit/Write) | 依檔案 + 符號錨點注入 concept | spike 已上線，但與記憶價值時刻有結構性錯位 |
| `SessionStart` | 健康告警（`systemMessage` 給使用者） | spike 已上線 |
| `SessionStart` 注入 | 對照組，precision 6.5% | 刻意不掛 |
| `UserPromptSubmit` 注入 | 對照組，precision 28.1% | 刻意不掛 |

下一個要量的觸發點是 Read/Grep 期（閱讀期才是記憶最有用的時刻），見 spike 盲測紀錄。

## 對外接口

U.E.P（Echo Memory 等）之後透過明確的接口接入〔待定：D6〕。Lore Vault 不 import U.E.P 程式碼、不依賴 U.E.P 的 Python 環境。
