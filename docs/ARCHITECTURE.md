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
| `key` | 穩定識別，git remote 正規化（如 `github.com/owner/repo`），無 remote 時 `folder/<name>`；一律存小寫 |
| `display` | 顯示名稱，保留大小寫 |
| `aliases` | 改名前的舊 key／舊 repo 名（取代 spike 的 `REPO_ALIASES`） |
| `kind` | `repo` / `global`（跨專案觀察） |
| `space` | `dev`（預設）／`lore`／`personal`（A18，schema v7）。與 vault 為 AND 疊加的硬範圍，在儲存層強制；合法值由程式白名單與 doctor `space.valid_values` 把關（無 CHECK）。`key` 仍全域唯一；非 dev 的 key 與別名必須以 `<space>/` 開頭（`space.key_prefix_agreement`）。dev 的 `global` 由管線自動建；lore／personal 不自動建，需要時明確建 `<space>/global`。換 space 只走 `cli.admin set-space` |

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
`cwd`（清單，一輪可能跨多個目錄）、`repo`、`repo_root`（凍結）、`machine`（凍結）、`files_edited`、`files_read`、`symbols_edited`、
`tool_sequence`、`user_text`、`assistant_text`、`injected`。完整欄位以 `lore_vault.schema.Episode` 為準。

⚠️ 含商業專案原文。**資料目錄在 repo 外**，不進版控。

### Concept（蒸餾出的記憶）

沿用 spike：`statement`、`kind`（project-fact / belief-correction / user-stance）、
`scope`（三態：repo 名 / `None` = 跨專案通用 / 缺欄位）、`anchors`（檔案 + 符號）、
`cue`、`surprisal`、校準與收斂紀錄。

### Injection

side-car 紀錄：`{session_id, prompt_id, injected: [concept_id]}`，不含原文。
用來辨識哪些語料輪次被記憶影響過，避免污染後續校準。

### Document（上傳的文件，A19）

設計見 `docs/design/SPACES_AND_DOCUMENTS.md` 第 3～6 節；實作細節見 docs/DEVELOPMENT.md「文件存儲與檢索」。

| 欄位 | 說明 |
|---|---|
| `id`, `vault` | `doc:<uuid>`；vault 為硬範圍（space 由 vault 決定），同 note |
| `filename`, `mime`, `size_bytes`, `sha256` | 原始檔 metadata；原始檔以 sha256 內容定址存 blob 目錄（跨 vault 共用、去重） |
| `version`, `supersedes` | 同 vault 同檔名、內容不同的上傳為新版本；被 ready 版本（遞移）取代的文件退出索引，但仍可 get／list（`superseded_by`） |
| `status`, `error_code`, `error_detail` | `pending → extracting → ready／failed`；failed 必有錯誤碼（`encrypted`、`corrupt`、`empty_extraction`、`too_large`、`unsupported_format`、`unsupported_encoding`） |
| `chunk_count`, `encoding` | ready 後的 chunk 數；文字檔偵測到的編碼（utf-8／utf-8-sig／utf-16／cp950） |

**Chunk**：文件內依結構（標題／頁／投影片）再定長切的段落（約 400 token、12.5% 重疊），外部 id
`chunk:<document uuid>:<idx>`；`locator` 為 `{"kind": "heading"|"page"|"slide"|"offset"|"header"|"footer",
"value": …, "part"?: n}`。chunk 有獨立的 FTS（CJK bigram）與向量（bge-m3）索引，只收可索引文件
（ready、未被取代）。

## MCP 介面（草案）

HTTP 契約為 `POST /v1/<工具名>` + JSON body，另有 `POST /v1/vaults` 建 vault（write 不自動建）；所有 `/v1` 需 bearer token，`GET /healthz` 公開。`GET /v1/snapshot` 提供降級用唯讀快照（只含 vaults（含 `space`）、notes、FTS，不含向量與 episode／concept／injection，**明確排除文件**（T-69）；整庫不分 space，由殼端依目前 space 過濾）。

**文件上傳**（T-67）：`POST /v1/documents` 是唯一的 multipart 端點，欄位 `file`、`vault`、`space`（必填）、`filename?`、`mime?`（未知欄位 400）。大小在讀取 body 時就擋（413 `too_large`，單檔上限 `documents.max_file_bytes`，預設 25MB）；格式不支援 400 `unsupported_format`；服務未設 `documents.blob_dir` 500 `documents_not_configured`。回應 `{document_id, status, sha256, duplicate, retried, vault, space, filename, version, supersedes, size_bytes}`：新列或重試 201、`duplicate: true` 200。抽取在服務程序內的背景 worker，回應時 status 多為 `pending`。重複上傳（同 vault）：同內容且現行 → 回既有（不重新排隊）；同內容只有 failed → 沿用該列重跑（`retried: true`）；同檔名不同內容 → 新版本（`supersedes`）。

**space（A18）**：`/v1/vault_resolve`、`/v1/vaults`、`/v1/recall`、`/v1/get`、`/v1/list`、`/v1/write`、`/v1/update`、`/v1/status` 的 body 必帶 `space`（`dev`／`lore`／`personal`），**服務端無預設**：缺少、null 或空字串回 400 `space_required`，不在白名單回 400 `invalid_space`。唯一例外是無 body 的 `POST /v1/status`（純健康檢查，回應 `space: null`）。範圍語意：
- vault key（或別名）存在但屬於別的 space → 與不存在相同（404 `unknown_vault`），不透露存在性
- `vault="*"` 只解除 vault 這一層：代表「該 space 內的全部 vault」；沒有跨 space 查詢，要看別的 space 就切換
- `POST /v1/vaults`：非 dev 的 key／別名不以 `<space>/` 開頭回 400 `space_key_prefix_required`；key 已屬於其他 space 回 409（key 全域唯一）
- vault 相關回應（`vault_resolve`、`vaults`、`status.vault`）帶 `space` 欄位

spike 接入端點（階段 8，同樣需 bearer；每筆 body 項目 = schema dict 另加 `vault`）。**不帶 space、固定 `dev`**（episode／concept／injection 只屬於 dev；key 在其他 space 的 vault 對這些端點而言不存在，episode 收料遇到時該筆 `invalid`、不自動建）；A17 步驟 B 的 scope 比對只看 dev vault：

| 端點 | 契約 |
|---|---|
| `POST /v1/episodes` | `{"episodes": [Episode + vault]}`，每批 ≤ 200（超過 400）。回 `{accepted, duplicates, conflicts, invalid, created_vaults, results: [{index, key: [session_id, prompt_id, turn_index], status, vault?, error?}]}`；status：`accepted`／`duplicate`（同鍵同內容，成功）／`conflict`（同鍵不同內容或不同 vault，不覆寫，客戶端留 spool）／`invalid`。vault 不存在時自動建立（kind=repo、display=episode.repo 或 key、`origin='episode'`＋觸發來源）；只有這條路徑自動建 |
| `GET /v1/episodes` | query `vault`（必填，跨 vault 明示 `*`）、`since`（started_at ≥）、`session_id`、`cursor`、`limit`（預設 200、上限 1000）。依 (started_at, seq) 由舊到新；回 `{items: [Episode + vault], next_cursor}` |
| `GET /v1/concepts/export` | query `vault` 預設明示 `*`（scope 由客戶端 scorer 判斷），可指定單一 vault。body 與 spike `concepts.json` 同格式（頂層 list、欄位與順序同 spike；`usability` 只在有值時出現）；依寫入順序排序（不依 id）。scope 缺欄位的 concept 不匯出（header `X-Lore-Vault-Excluded-Missing-Scope`）。ETag = body sha256，`If-None-Match` 符合回 304 |
| `POST /v1/concepts` | `{"vault": key 或 "*", "mode": "upsert"／"create"／"update", "concepts": [Concept + vault?], "delete": [id]}`，合計 ≤ 1000。**整批成功或整批不寫**：任一筆 invalid／conflict 回 400／409（`error.code = batch_rejected`，附逐筆結果）。歸屬：既有 id 沿用原 vault（凍結）；新 id 且 `scope=null` → `global`（kind=global，不存在時自動建、`origin='pipeline'`）；新 id 且 scope 為 repo 名 → 每筆 vault 或批次單一 vault；都沒帶時依 A17：(A) `source_turns` 的 `[prompt_id, turn_index]` 查 episodes 所屬 vault（部分查不到可，指向多個 vault 即歧義、不退 B）→ (B) scope 不分大小寫比對非 global vault 的 display、key／別名的整串、最後一段（repo）與最後兩段（`org/repo`），唯一命中才採用 → 都失敗該筆 invalid，逐筆帶 `code`＝`vault_unresolved`／`vault_ambiguous`（歧義另附 `candidates`，scope 撞名可改寫成 `org/repo`）；A、B 只解析到既有 vault，不自動建。成功的逐筆結果附 `vault` 與 `resolved_by`（`existing`／`global`／`explicit`／`source_turns`／`scope_match`）。scope 必須出現。新增排在匯出最後；`delete` 不存在回 `not_found`（冪等） |
| `POST /v1/injections` | `{"injections": [Injection + vault + recorded?]}`，每批 ≤ 500。status：`accepted`／`duplicate`（同 vault 內容全等的重送，不看 recorded）／`unknown_vault`（不自動建，稍後重送）／`invalid` |

MCP 為各機器本地 stdio 殼（`python -m lore_vault.mcp`，A15）：服務連線失敗、逾時或 502／503／504、Cloudflare 521–524／530 時，`recall`／`get`／`list`／`vault_resolve` 改讀本地快照、只走 lexical 並標 `degraded`；`write`／`update` 直接失敗不排佇列；401／403／其他 4xx 與 500 直接報錯不降級。降級查詢同樣以殼的目前 space 過濾（快照保留 `vaults.space`）。

殼持有「目前 space」：每個殼行程一份、只在記憶體、不持久化，新行程一律 `dev`。其他工具沒有 space 參數，殼在每個 `/v1/*` 請求自動注入目前 space（唯一出口 `Shell._send`）。

目標是讓 agent 用最少的上下文拿到足夠決策的資訊。工具數量刻意壓在個位數（目前 9 個）。

| 工具 | 回傳 | 說明 |
|---|---|---|
| `space(action, value?)` | `{space, spaces}` | `action="get"` 查詢、`"set"` 切換（`value` 為 `dev`／`lore`／`personal`）；純殼端狀態，不打服務；非法值回工具錯誤 `invalid_space`、狀態不變 |
| `vault_resolve(cwd?, create?, display?, space?, key?)` | vault key、display、space、note 數、binding（dev 由 cwd 推算時） | dev：key 省略時 MCP 殼以 `lore_vault.binding` 從 cwd 算 key，服務端做別名解析；lore／personal：沒有 repo，必須帶 `key`（`<space>/名稱`，缺少回 `key_required`），傳了 `cwd` 會忽略並回 `cwd_ignored: true`。`space` 省略用目前 space，顯式傳入只影響這一次。`create=True` 才建 vault（HTTP `POST /v1/vaults`）；取代 pm-bind 的手動步驟 |
| `recall(query, vault, kinds?, limit?, budget?)` | `[{id, kind, vault, title, summary, summary_source, score, updated}]`；chunk 另帶 `document_id`、`chunk_id`、`locator` | 統一檢索 note 與文件段落（`kinds` 預設 `["note", "chunk"]`；concept 未實作）；note 與 chunk 的 lexical／vector 四路一次 RRF。chunk 的 `title` 為檔名、`summary` 為段落摘錄（`summary_source: "excerpt"`），同樣受 `budget`；**預設不含全文**；`vault` 必填，跨範圍用 `vault="*"` 明示。回應另有 `kinds`（實際查的）、`missing_chunk_embeddings`；降級時 `chunk` 列在 `unsupported_kinds` |
| `get(vault, ids, budget?)` | 全文 | 可批次；`ids` 可混 note id、`doc:…`（整份文件文字，重疊段已去除）、`chunk:…`（單段，含 `locator`）；字數預算依 ids 順序分配，超過時截斷並標示（`truncated`、`body_chars`／`text_chars`）；vault 必填（A5）。範圍外或不存在列在 `missing`，降級時文件 id 列在 `unavailable` |
| `list(vault, since?, topics?, cursor?, limit?, kinds?)` | 標題清單 | note 與文件合併分頁（`kinds` 預設兩者）；文件項含 `status`、`error_code`、`version`、`supersedes`、`superseded_by`、`chunk_count`、`encoding`；指定 `topics` 時只列 note；降級時 `document` 列在 `unsupported_kinds` |
| `write(vault, title, body, topics?, supersedes?)` | id、疑似重複清單 | 寫入前自動查重，回傳相似 note 讓 agent 決定改用 `update` |
| `update(id, body?, title?, topics?)` | id | |
| `upload(path, vault?)` | `document_id`、`status`、`duplicate`、`version`、`supersedes` | 殼讀本機檔案（只限殼工作目錄與 `mcp.upload_roots`；拒絕 `..` 與 symlink 逃逸）轉送 `POST /v1/documents`；`vault` 省略時只在 dev 用殼工作目錄 binding；服務不可達直接失敗 |
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
