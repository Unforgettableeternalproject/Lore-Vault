# 架構

> 設計草案。標 **〔待定〕** 的項目尚未裁決，不要當成定案實作。

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
| `key` | 穩定識別，git remote 正規化（如 `github.com/owner/repo`），無 remote 時 `folder/<name>`；一律存小寫。`folder/<name>` 的 vault 只在明確建立（`/pm init`＝`vault_resolve(create=true)`）後存在；未建立前該位置的 episode 進雜項 vault `misc`（D14，見 `kind`） |
| `display` | 顯示名稱，保留大小寫 |
| `aliases` | 改名前的舊 key／舊 repo 名（取代 spike 的 `REPO_ALIASES`） |
| `kind` | `repo` / `global`（跨專案觀察）／`misc`（雜項，D14，schema v16）。雜項 vault 固定 key `misc`（不含 `/`，不會與 binding 算出的 key 或非 dev 前綴相撞）、只在 dev、只由 episode 收料自動建立：收容從**尚未建立 vault** 的位置（`folder/<name>`）擷取的 episode，與跨專案知識的 `global` 不同。保留規則（`storage.vaults.check_reserved`）：key `misc` ⇔ kind `misc`、不可有別名、`misc` 不可當別名，`POST /v1/vaults` 不可建 kind misc（400 `reserved_vault`），所以 `vault_resolve` 不會把任何位置解析到雜項。**優先度較低**：`recall`／`ask` 跨 vault（`vault="*"`）時雜項結果的 RRF 分數乘 `recall.service.MISC_SCORE_WEIGHT`（0.5）再排序（指定單一 vault 不降權）；注入快照（`GET /v1/concepts/export`）把雜項 vault 的 concept 排在最後，PreToolUse 同分時排後；A17 步驟 B 的 scope 比對不看雜項（同 global） |
| `space` | `dev`（預設）／`lore`／`personal`（A18，schema v7）。與 vault 為 AND 疊加的硬範圍，在儲存層強制；合法值由程式白名單與 doctor `space.valid_values` 把關（無 CHECK）。`key` 仍全域唯一；非 dev 的 key 與別名必須以 `<space>/` 開頭（`space.key_prefix_agreement`）。dev 的 `global` 由管線自動建；lore／personal 不自動建，需要時明確建 `<space>/global`。換 space 只走 `cli.admin set-space` |

### Note（寫下的結論）

| 欄位 | 說明 |
|---|---|
| `id`, `vault` | 必填，vault 為硬範圍 |
| `title` | 查詢結果的主要呈現 |
| `summary` | 1–2 句摘要，查詢時回傳這個而不是全文〔待定：誰產生，見 D4〕 |
| `body` | markdown 全文，只在 `get` 時回傳 |
| `topics` | 標籤 |
| `links` | 互連的 note id。`write`／`update` 自動把 body 的 `[[標題]]` 在**同一 vault** 內依標題解析（規則與舊 PM 匯入共用 `notes.links`：可含一層方括號、先比原文再比去掉 `\|別名`／`#段落` 的形式、壓空白 + casefold），唯一命中才寫入；解析不到（`unresolved`）或同 vault 多則同名（`ambiguous`，附候選 id）的保留原文不寫，回應列在 `unresolved_links`；別 vault／別 space 的同名 note 一律當不存在、不帶出 id；連到自己的略過。合併規則見下 |
| `supersedes` | 更正關係：新 note 取代舊 note 時標記，而不是另建更正篇 |
| `superseded_by` | 衍生欄位（不存 DB）：同 vault 內 `supersedes` 指向它的 note；多則時取 `updated` 最新者（同時間取 id 大者）。`get`／`list` 帶出 |
| `author` | 寫入者自報的身分名（A22，schema v12）：agent 用自己的角色名、UI 登入寫入帶 `Xavier (Bernie)`；未填存 null（對外顯示為未具名），服務**不代填**。單行、去前後空白後 1–64 字、控制字元規則同其他欄位；`legacy` 保留給舊 PM 匯入（API／MCP 自稱會被拒） |
| `principal` | 服務依憑證判定的主體（A22）：**不可由請求指定**（body 帶了 422）。Bearer 依 `api.principals` 的「憑證 → principal」對照，目前唯一的 token 對應 `LORE_VAULT_PRINCIPAL`（D12，預設 `owner`；本機現行部署設 `UEPBernie` 與 Eternity 帳號及 v13 遷移結果一致），日後一 token 一 principal；UI session 的 principal 是登入帳號的 username（A23；首次啟動建立的管理員預設同 principal）。doctor `notes.principal_agreement` 在既有 note 的 principal 不含設定值時 warn（漏設 env 的提醒）。DB 欄位可為 NULL、無 DEFAULT，儲存層 `insert_note` 拒收缺 principal，doctor `notes.attribution` 對帳 |
| `updated_by`, `updated_by_principal` | 最後一次寫入（建立或修改）者的自報名與 principal；建立時同 `author`／`principal`。`update` 的 `author` 參數寫進這裡（未填也記 null，不沿用上一位），原 `author` 不變 |
| `created`, `updated` | |
| `enqueued` | 內部欄位（schema v14，不對外回傳）：服務寫入目前版本的牆鐘時間（新增、更新、取消刪除都重設），當作背景補算的入列時間。不用 `updated`——舊 PM 匯入與還原保留原始 `updated`，doctor `enrich.backlog` 會算出極大的等待時間。v14 遷移回填：當下待補算的 note 為遷移時間（真正的入列時間已無從得知；原生 note 若真的卡住，等待時間在升級當下歸零一次），其餘沿用 `updated`。doctor `enrich.queue_time`：有 note 缺值為 fail |

links 合併規則（呼叫端明傳的 links 不驗存在性，維持舊行為）：
- `write`：links = 明傳值（在前）∪ body 解析出的 id，去重保序
- `update` 有傳 `links`：links = 傳入值 ∪ 正文（新 body，未傳則目前 body）解析出的 id
- `update` 沒傳 `links`、body 有變：links =（目前 links − 舊 body 解析出的 id）∪ 新 body 解析出的 id——
  正文刪掉 `[[x]]` 後自動連結跟著消失、明確加的保留（若它剛好也寫在舊 body 的 `[[ ]]` 裡則視為自動連結一併移除）；
  同一交易內讀舊版本計算
- `update` 兩者都沒有（例如只改 title）：links 不動、不重新解析（`unresolved_links` 為空）

v12 遷移回填：principal 全為 `xavier`（v13 再改寫為 `UEPBernie`，含 note 墓碑快照 JSON 內的 `principal`／`updated_by_principal`；不動 `updated`）；對帳清單 `import_sources.imported_updated` 非 NULL 的 note（舊 PM 匯入成功者）`author`／`updated_by` = `legacy`，其餘 `author` 維持 NULL。`import_on` 新寫入或依來源更新的 note 同樣標 `legacy`，重跑冪等。

### Episode（發生過的事）

沿用 spike 的 schema：每輪對話一筆，含 `session_id`、`prompt_id`、`turn_index`、`origin`、
`cwd`（清單，一輪可能跨多個目錄）、`repo`、`repo_root`（凍結）、`machine`（凍結）、`files_edited`、`files_read`、`symbols_edited`、
`tool_sequence`、`user_text`、`assistant_text`、`injected`。完整欄位以 `lore_vault.schema.Episode` 為準。

服務端另存 `origin_key`（schema v16，不在 Episode schema／`data` 內）：被改路由到雜項 vault 的 episode 記收料時客戶端送來的原 binding key（`folder/<name>`），其餘為 NULL；日後該位置正式註冊時據此追溯，但不自動搬移（A7）。doctor `vaults.misc_routing` 對帳：雜項 vault 唯一、key 為 `misc`、無別名；`origin_key IS NOT NULL` ⇔ 在雜項 vault 且以 `folder/` 開頭（fail）；仍有 episode 收料自動建立的 `folder/*` vault 為 warn；已正式註冊的來源位置數只列資訊。

v16 遷移把既有的「episode 收料自動建立的 `folder/*` vault」（如 `folder/desktop`）併入雜項：該 vault 有 note／文件／墓碑任一 → 完全不動（由 doctor warn 呈現）；否則 episode 改歸 `misc` 並記 `origin_key`，vault 已無 concept／injection／別名就刪除；搬移紀錄寫進雜項 vault 的 `origin_detail.migrated_from`。冪等（`storage.migrate.absorb_folder_vaults`）。

⚠️ 含商業專案原文。**資料目錄在 repo 外**，不進版控。

### Concept（蒸餾出的記憶）

沿用 spike：`statement`、`kind`（project-fact / belief-correction / user-stance）、
`scope`（三態：repo 名 / `None` = 跨專案通用 / 缺欄位）、`anchors`（檔案 + 符號）、
`cue`、`surprisal`、校準與收斂紀錄。

### Injection

side-car 紀錄：`{session_id, prompt_id, injected: [concept_id]}`，不含原文。
用來辨識哪些語料輪次被記憶影響過，避免污染後續校準。

### Document（上傳的文件，A19）

設定見 `config.example.toml` 的 `[documents]` 區段。

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

HTTP 契約為 `POST /v1/<工具名>` + JSON body，另有 `POST /v1/vaults` 建 vault（write 不自動建）；所有 `/v1` 需認證（bearer token，或 UI session cookie ＋ `X-Lore-Vault-UI: 1`，見下方「UI 認證」），`GET /healthz` 公開。`GET /v1/snapshot` 提供降級用唯讀快照（只含 vaults（含 `space`）、notes、FTS，不含向量與 episode／concept／injection，**明確排除文件**（T-69）；整庫不分 space，由殼端依目前 space 過濾）。

**文件上傳**（T-67）：`POST /v1/documents` 是唯一的 multipart 端點，欄位 `file`、`vault`、`space`（必填）、`filename?`、`mime?`（未知欄位 400）。大小在讀取 body 時就擋（413 `too_large`，單檔上限 `documents.max_file_bytes`，預設 25MB）；格式不支援 400 `unsupported_format`；服務未設 `documents.blob_dir` 500 `documents_not_configured`。回應 `{document_id, status, sha256, duplicate, retried, vault, space, filename, version, supersedes, size_bytes}`：新列或重試 201、`duplicate: true` 200。抽取在服務程序內的背景 worker，回應時 status 多為 `pending`。重複上傳（同 vault）：同內容且現行 → 回既有（不重新排隊）；同內容只有 failed → 沿用該列重跑（`retried: true`）；同檔名不同內容 → 新版本（`supersedes`）。

**space（A18）**：`/v1/vault_resolve`、`/v1/vaults`、`/v1/recall`、`/v1/get`、`/v1/list`、`/v1/write`、`/v1/update`、`/v1/status` 的 body 必帶 `space`（`dev`／`lore`／`personal`），**服務端無預設**：缺少、null 或空字串回 400 `space_required`，不在白名單回 400 `invalid_space`。唯一例外是無 body 的 `POST /v1/status`（純健康檢查，回應 `space: null`）。範圍語意：
- vault key（或別名）存在但屬於別的 space → 與不存在相同（404 `unknown_vault`），不透露存在性
- `vault="*"` 只解除 vault 這一層：代表「該 space 內的全部 vault」；沒有跨 space 查詢，要看別的 space 就切換
- `POST /v1/vaults`：非 dev 的 key／別名不以 `<space>/` 開頭回 400 `space_key_prefix_required`；key 已屬於其他 space 回 409（key 全域唯一）
- vault 相關回應（`vault_resolve`、`vaults`、`status.vault`）帶 `space` 欄位

**UI 認證（A21／A23）**：使用者 UI 由服務在 `/ui` 提供（Vite 建置的靜態檔，SPA fallback 到 `index.html`；`/ui`、`/ui/*` 本身免認證，資料一律走需認證的 `/v1`）。登入用 DB 內的 UI 帳號密碼（`ui_accounts`，scrypt 雜湊、參數存在列中），全域失敗 3 次即鎖定、需人工 `cli.admin ui-unlock --yes`（規則見 `storage.ui_login`）。本地登入端點（不列入 OpenAPI，皆要求 `X-Lore-Vault-UI: 1`）：

| 端點 | 契約 |
|---|---|
| `GET /ui/api/login` | 登入頁的公開狀態 `{account_configured, locked, remaining, max_failures, setup_command}`（`setup_command` 只在尚無帳號時非 null）；不含帳號名稱或任何機密 |
| `POST /ui/api/login` | body 恰為 `{"username": "...", "password": "..."}`（username 比對不分大小寫）。成功 204 + `Set-Cookie`：`__Host-lv_session`（`ui.cookie_secure=false` 時為 `lv_session`、不帶 Secure）、HttpOnly、SameSite=Strict、Path=/、Max-Age=絕對期限。失敗回應的 `error` 另帶 `remaining`、`max_failures`、`locked`：帳密錯（含不存在的帳號）401 `invalid_credentials`；第 3 次失敗與鎖定中一律 423 `locked`（正確密碼也擋、不比對密碼）；尚未設定帳號 409 `no_account`（附 `setup_command`，不計失敗）；body 格式錯 400 `invalid_request`、超過 4KB 413 `too_large`、缺標頭 403 `csrf_required`（三者都不計失敗）。每次嘗試記一列 `ui_login_log`（不含密碼） |
| `POST /ui/api/logout` | 註銷目前 session 並清 cookie；沒有 session 也回 204 |
| `GET /ui/api/session` | `{authenticated, principal, display_name, expires_at, idle_expires_at, limits}`（`principal` = 登入帳號、`display_name` = 前端署名）；無效或過期 401。`limits`＝前端需要的限制值，與服務端實際檢查同一來源：`max_file_bytes`、`max_chars`（`documents.*` 設定）、`author_max_chars`、`get_max_ids`、`get_default_budget`、`list_max_limit`、`list_default_limit`、`list_default_budget`、`recall_max_limit`、`recall_default_limit`、`recall_default_budget`。放 session 而非 `/v1/status`：UI 載入時本來就呼叫、便宜；`/v1/status` 每次都跑整套 doctor |

`/v1/*` 的認證：帶了 `Authorization` 標頭就只走 bearer（行為與 A15 相同，不看 cookie）；否則接受有效的 session cookie，但必須帶 `X-Lore-Vault-UI: 1`，缺少回 403 `csrf_required`。session 只存在服務記憶體，重啟即失效；有絕對與閒置兩種期限。登入失敗計數是全域的（不分來源），計數與鎖定存在 DB、重啟不解除；登入紀錄的來源 IP 只在直接連線位址屬於 `ui.trusted_proxies` 時才採信 `CF-Connecting-IP`。`/ui` 回應帶嚴格 CSP（無 inline、無第三方來源，字型自託管）與 `nosniff`、`no-referrer`、`frame-ancestors 'none'`。

spike 接入端點（階段 8，同樣需 bearer；每筆 body 項目 = schema dict 另加 `vault`）。**不帶 space、固定 `dev`**（episode／concept／injection 只屬於 dev；key 在其他 space 的 vault 對這些端點而言不存在，episode 收料遇到時該筆 `invalid`、不自動建）；A17 步驟 B 的 scope 比對只看 dev vault：

| 端點 | 契約 |
|---|---|
| `POST /v1/episodes` | `{"episodes": [Episode + vault]}`，每批 ≤ 200（超過 400）。回 `{accepted, duplicates, conflicts, invalid, created_vaults, results: [{index, key: [session_id, prompt_id, turn_index], status, vault?, error?}]}`；status：`accepted`／`duplicate`（同鍵同內容，成功）／`conflict`（同鍵不同內容或不同 vault，不覆寫，客戶端留 spool）／`invalid`。vault 決定（D14，`storage.vaults.route_episode_vault`）：key 或別名命中 dev 內既有 vault → 照舊（含 `/pm init` 建立的 `folder/*`）；不存在的 git remote key → 自動建立（kind=repo、display=episode.repo 或 key、`origin='episode'`＋觸發來源）；不存在的 `folder/<name>` → 雜項 vault `misc`（首次自動建立、kind=misc），逐筆結果另帶 `origin_key`（原 key）、`vault` 為 `misc`；客戶端直接送 `misc` 為 invalid。只有這條路徑自動建。客戶端不必更新：改路由完全在服務端，重送已改路由的舊筆仍得 `duplicate`。**收料開關**（D13）：`episodes.ingest` 為 false（預設）時整批 403 `episode_ingest_disabled`、不寫任何東西；客戶端 spool 依錯誤碼把檔案留在 pending、退避 6 小時（`hooks.spool.DISABLED_BACKOFF_SECONDS`），不移到 rejected |
| `GET /v1/episodes` | query `vault`（必填，跨 vault 明示 `*`）、`since`（started_at ≥）、`session_id`、`cursor`、`limit`（預設 200、上限 1000）。依 (started_at, seq) 由舊到新；回 `{items: [Episode + vault], next_cursor}`。`after_seq`（增量模式，不可與 `cursor`／`since`／`session_id` 併用）：只回 `seq > after_seq`、依 seq 排序，每筆另帶 `seq`，回應多 `next_after_seq`、`max_seq`、`total`（範圍內 `seq <= max_seq` 的筆數），主機管線的 episode 快取水位用。不受收料開關影響：已收進來的照常可讀（主機管線 `--pull-episodes` 要能拉完；`episode_summary` 同） |
| `GET /v1/concepts/export` | query `vault` 預設明示 `*`（scope 由客戶端 scorer 判斷），可指定單一 vault。body 與 spike `concepts.json` 同格式（頂層 list、欄位與順序同 spike；`usability` 只在有值時出現）；依寫入順序排序（不依 id），但雜項 vault 的 concept 一律排在最後（D14：PreToolUse scorer 穩定排序，同分時排後）。scope 缺欄位的 concept 不匯出（header `X-Lore-Vault-Excluded-Missing-Scope`）。ETag = body sha256，`If-None-Match` 符合回 304 |
| `POST /v1/concepts` | `{"vault": key 或 "*", "mode": "upsert"／"create"／"update", "concepts": [Concept + vault?], "delete": [id]}`，合計 ≤ 1000。**整批成功或整批不寫**：任一筆 invalid／conflict 回 400／409（`error.code = batch_rejected`，附逐筆結果）。歸屬：既有 id 沿用原 vault（凍結）；新 id 且 `scope=null` → `global`（kind=global，不存在時自動建、`origin='pipeline'`）；新 id 且 scope 為 repo 名 → 每筆 vault 或批次單一 vault；都沒帶時依 A17：(A) `source_turns` 的 `[prompt_id, turn_index]` 查 episodes 所屬 vault（部分查不到可，指向多個 vault 即歧義、不退 B）→ (B) scope 不分大小寫比對非 global vault 的 display、key／別名的整串、最後一段（repo）與最後兩段（`org/repo`），唯一命中才採用 → 都失敗該筆 invalid，逐筆帶 `code`＝`vault_unresolved`／`vault_ambiguous`（歧義另附 `candidates`，scope 撞名可改寫成 `org/repo`）；A、B 只解析到既有 vault，不自動建。成功的逐筆結果附 `vault` 與 `resolved_by`（`existing`／`global`／`explicit`／`source_turns`／`scope_match`）。scope 必須出現。新增排在匯出最後；`delete` 不存在回 `not_found`（冪等） |
| `POST /v1/injections` | `{"injections": [Injection + vault + recorded?]}`，每批 ≤ 500。status：`accepted`／`duplicate`（同 vault 內容全等的重送，不看 recorded）／`unknown_vault`（不自動建，稍後重送）／`invalid` |

**文件下載**：`POST /v1/document_download`，body `{space, vault, id, max_bytes?}`（未知欄位 422），是唯一回二進位的端點。範圍同 `get`：文件不在該 vault／space（含墓碑中的文件）一律 404 `not_found`，`vault="*"` 為目前 space 全部。成功回上傳時的原始位元組：`Content-Type` 為上傳時的 mime、`Content-Disposition: attachment`（ASCII 後備檔名＋RFC 5987 `filename*=UTF-8''…`）、`X-Lore-Vault-Sha256`、`X-Lore-Vault-Document-Id`、`Cache-Control: no-store`。`max_bytes` 給了而文件較大 → 413 `too_large`（依 metadata 判斷，不讀 blob）。讀 blob 時驗 sha256：遺失 500 `blob_missing`、雜湊不符 500 `blob_corrupt`（不回可能損毀的內容）；未設 `blob_dir` 500 `documents_not_configured`。bearer 與 UI session（cookie＋`X-Lore-Vault-UI`）皆可，由同一個中介層處理。不另加 doctor 檢查：這是唯讀資料流，blob 存在與雜湊已由 `documents.blob_exists` 對帳、讀取時再驗一次。

**執行期設定**（D13，`lore_vault.api.settings_admin`；白名單與驗證在 `lore_vault.runtime_settings`、儲存與稽核在 `storage.settings_store`，schema v15）。設定檔／環境變數是「預設值」，DB 的 `settings_overrides` 覆寫其上；讀取端一律經 `AppState.runtime.current()`（程序內快取，設定 API 寫入後立即失效；依據 A9 單一寫入程序，doctor CLI 等其他程序直接讀 DB）。**只允許 UI session**：bearer 請求一律 403 `ui_session_required`（在 body 驗證之前檢查）——bearer 由所有 agent／hook 共用，`/mcp` 也以 bearer 轉發 `/v1`，開放 bearer 等於任何 agent 都能翻隱私開關；其他管理端點開放 bearer 是因為有兩段式確認或墓碑可還原，設定沒有這層保護。

| 端點 | body | 回應 |
|---|---|---|
| `GET /v1/settings` | — | `{categories: [{id, label}], items: [{key, type (bool／int／float), category, label, description, min, max, unit, value（生效值）, default（設定檔／環境變數的值）, source (default／override), override: {updated, updated_by} 或 null}], invalid_overrides: [{key, reason, updated}], audit: [最近 20 筆]}` |
| `POST /v1/settings_update` | `{"values": {鍵: 值}}`（1 到白名單項數） | 同 GET，另附 `changed`（這次寫入的稽核列；與目前覆寫相同的鍵不寫、不記）。值只收 JSON 原型別：bool 必須是 true／false、int 不收小數、數字要在範圍內；任一鍵不合法整批不寫，400 `invalid_setting` 附 `errors: [{key, code (unknown_setting／invalid_value), message}]` |
| `POST /v1/settings_reset` | `{"keys": [鍵]}` | 同上；刪除覆寫、回到預設值（原本沒有覆寫的鍵不記） |

稽核列（`settings_audit`）：`{seq, at, key, action (set／reset), old_value, new_value（皆為生效值）, principal, display}`，與覆寫列同一交易寫入。

白名單（每一項都有測試證明不重建 app 即生效，`tests/api/test_runtime_settings.py`）：

| 鍵 | 型別／範圍 | 讀取端 |
|---|---|---|
| `episodes.ingest` | bool，預設 false | `POST /v1/episodes` 每次請求；`/v1/status` 的 `episodes.ingest_recency` |
| `ask.enabled` | bool，預設 true | `/v1/ask` 每次請求（關閉 403 `ask_disabled`，不檢索、不呼叫模型）；`ask.provider` 記為 skipped |
| `ask.snippet_max_chars` | int 500–50000 | `/v1/ask` 每次請求 |
| `mcp.http_download_max_bytes` | int 1024–25 MiB | HTTP MCP `download` 每次呼叫（`Shell(download_limit=...)`） |
| `backup.max_age_hours` | float 1–2160 | `/v1/status` 的 doctor 設定 |
| `documents.stuck_seconds` | float 60–604800 | 同上（只有 doctor 使用） |
| `database.tombstone_warn_age_days` | float 0–3650 | 同上 |
| `database.tombstone_warn_bytes` | int 0–2^40 | 同上 |
| `ui.login_log_retention_days` | float 1–3650 | 每次登入嘗試與啟動時清除登入紀錄 |

刻意不收：token／密碼／OpenAI key、資料庫與 blob 路徑、模型與 base_url（用戶端啟動時建立）、worker 參數與 session 期限（啟動時建構）、`documents.max_file_bytes`（MCP 請求大小上限在啟動時決定）等需重啟或涉及密鑰的項目。DB 裡不合法的覆寫（手動改 DB、白名單移除的鍵）執行期略過、改用預設值，不讓服務起不來；doctor `settings.overrides` 為 fail。

**UI 管理端點**（T-70～T-75，`lore_vault.api.manage`）。其中 `note_delete`／`document_delete`／`note_undelete`／`document_undelete` 另由 MCP 的 `delete`／`undelete` 工具原樣轉呼叫（殼不另寫刪除邏輯）；其餘（含 `vault_delete`、`vault_move_space`）不提供 MCP 工具。同 `/v1` 慣例：POST、bearer、未知欄位 422、body 必帶 `space`（缺 400 `space_required`）；vault 在別的 space 與不存在相同（404 `unknown_vault`），墓碑在別的 space 與不存在相同（404 `not_found`），錯誤訊息不帶出別 space 的 key。

`VaultSummary`：`{key, display, kind, space, origin (manual／episode／pipeline), aliases, note_count, document_count, created, last_updated}`（`last_updated`＝note／文件最大的 `updated`，都沒有為 null）。

| 端點 | 請求 | 回應 | 錯誤 |
|---|---|---|---|
| `vault_list` | `{space}` | `{space, vaults: [VaultSummary]}`（空 space 回空陣列） | |
| `vault_update` | `{space, vault, display}` | `VaultSummary`（只改 display） | 400 `invalid_request`、404 |
| `vault_alias_add` | `{space, vault, alias}` | `VaultSummary` | 409 `vault_exists`（`existing: {key}`；佔用者在別 space 時為 null）、400 `space_key_prefix_required`／`vault_required`（`*`） |
| `vault_alias_remove` | `{space, vault, alias}` | `VaultSummary` | 400 `cannot_remove_key`（正式 key）、404 `not_found`（不是這個 vault 的別名） |
| `vault_move_space` ⚠ | `{space, key, to_space, new_key?, confirm_token?}` | 規劃：`plan`＝`{key, new_key, from, to, aliases: {舊: 新}, counts: {"表.欄": 筆數}}`；執行另附 `vault: VaultSummary`（新 space） | 400 `space_change_refused`（A20：只允許 lore↔personal）、404（別名或不在該 space）、409 `vault_conflict` |
| `vault_delete` ⚠ | `{space, key, reason?, confirm_token?}` | `plan`＝`{target: "vault", vault, counts, note_ids, requires_force}`；確認即等同 `--force` | 404（只接受正式 key） |
| `note_delete` ⚠ | `{space, vault, id, reason?, confirm_token?}` | `plan`＝同上（`target: "note"`） | 404 `not_found` |
| `document_delete` ⚠ | `{space, vault, id, reason?, confirm_token?}` | `plan`＝`{target: "document", document_id, vault, sha256, filename, supersedes, relinked, counts, blob_still_referenced}` | 404 `not_found` |
| `tombstones` | `{space, vault, kinds?: ["note","document"], cursor?, limit? (≤500, 預設 50)}` | `{items, next_cursor}`；item＝`{kind, id, vault, vault_exists, deleted_at, reason}` + note：`{source, title, restorable, reimportable}`（`title` 取自內容快照，舊墓碑為 null；`restorable`＝有快照且 vault 還在）／document：`{sha256, filename, restorable}`。依刪除時間由新到舊；`vault="*"` 為 space 內全部，已刪的 vault 可用原 key 查 | 400 `vault_required`／`invalid_cursor`／`invalid_request` |
| `note_undelete` | `{space, id}` | `{undeleted: 墓碑, restored, reimportable, note}`。墓碑有內容快照（schema v12 起刪除的）→ 以原 id 與原內容（含作者欄位、created／updated）還原，`restored: true`、`note` 為 `{id, vault, title, author, updated}`；FTS 同交易重建，向量由背景補算。v12 前的舊墓碑 → 維持舊行為：只移除墓碑、`restored: false`、`note: null`，有匯入來源者（`reimportable`）重跑匯入才回來 | 404；409 `not_restorable`（`reason`：`vault_deleted` 所屬 vault 已刪除、墓碑保留，重建同 key 的 vault 後可還原／`exists`） |
| `document_undelete` | `{space, id}` | `{document: Document, space, tombstone}`；同一 id 重建、`status: "pending"` 重新抽取，版本鏈比照上傳（同檔名現行版本為 `supersedes`） | 409 `not_restorable`（`reason`：`incomplete` v11 前墓碑／`blob_missing`／`duplicate` 同內容已存在／`vault_deleted`／`exists`）、500 `documents_not_configured` |
| `document_retry` | `{space, vault, id}` | `{document, space, manual_retries, max_manual_retries}`；failed → pending（沿用上傳重試的 `reset_for_retry`） | 409 `not_failed`／`retry_limit`（每份 3 次） |
| `concept_query` | `{space, vault, scope?, scope_state?: repo／global／missing, kind?, since?, until?, cursor?, offset?, with_total?, limit? (≤200)}`（`since`／`until` 為 updated 區間、含端點；`offset` 頁碼分頁、與 `cursor` 擇一） | `{items, next_cursor, total?}`（`with_total` 時帶 `total`）；item＝`{id, vault, kind, scope, scope_state, statement, anchors, surprisal, usability_verdict, updated}`，依 updated 由新到舊。**不回** cue／probe／why／source_*／probe_result／usability 的 evidence | 400 `invalid_request`／`invalid_cursor` |
| `topics` | `{space, vault}` | `{space, vault, topics: [{topic, count}]}`：範圍內 note 的標籤與使用筆數，依筆數由多到少、同數依名稱；`vault="*"` 為目前 space 內全部 vault | 400 `vault_required`（缺 vault）、404 `unknown_vault` |
| `episode_summary` | `{space, vault}` | `{space, vault, total, last_recorded, by_machine: [{machine, episodes, last_recorded, last_started}], by_vault: [{vault, …}]}`；不讀 data 欄、不含任何對話原文 | |

`/v1/list` 另接受 `until`（updated 上界，含端點；與 `since` 一起做日期區間）、`offset`（頁碼分頁，與 `cursor` 擇一）與 `with_total`（回 `total`＝相同篩選下的總筆數，回應同時帶回 `offset`）；UI 的分頁元件使用，MCP 殼仍用 cursor。

`/v1/list` 的篩選參數（UI 筆記／文件頁；MCP 殼不帶）：

- `title`：note 標題／文件檔名含該字串（ASCII 不分大小寫，`%`、`_` 照字面比對）；兩種 kind 都適用。
- `author`：note 作者含該字串（同上）；`author_state`：`named`（有作者，含舊資料的 `legacy`）／`missing`（未具名）。與 `topics` 一樣只屬 note，指定時不列文件。
- `statuses`：文件抽取狀態清單（`pending`／`extracting`／`ready`／`failed`）；`extensions`：檔名副檔名清單（不含點、不分大小寫，只收英數字；依檔名字尾判斷，不看 mime）。只屬文件，指定時不列 note。
- note 專屬與文件專屬篩選同時指定時沒有符合的項目（空頁、`total: 0`）。
- 全部篩選都在 SQL 內、LIMIT 之前套用，`total` 與 `items` 用同一組條件；空字串、空清單、白名單外的值或超過 200 字回 400 `invalid_request`（該 kind 這次沒列也照樣驗證）。

concept／episode 只屬 dev：在 lore／personal 查詢 `vault="*"` 回空、指定 dev 的 key 為 404。

**兩段式確認**（⚠ 標記的端點）：不帶 `confirm_token` → 只規劃，回 `{executed: false, plan, confirm_token, expires_at}`；以**完全相同的參數**加上 token 再送一次 → `{executed: true, plan, …}`。token＝base64url(payload)．HMAC-SHA256，payload 綁定操作名、全部請求參數（含 space、reason）、規劃內容的 sha256 與到期時間（5 分鐘）；祕密每個服務程序隨機產生（重啟後舊 token 失效）。簽章不符、格式錯誤、參數或操作不符 → 400 `invalid_confirm_token`；過期 → 400 `confirm_token_expired`。執行時在同一個寫入交易內重新規劃並比對 digest，不符 → 409 `plan_changed`：`error.plan` 附目前規劃，另附綁定新規劃的 `error.confirm_token`／`error.expires_at`（同一操作、同一組參數）；**仍需使用者看過新規劃再確認一次**，以新 token 重送才執行，服務端不會自動執行。舊 token 綁的是舊規劃的 digest，只要資料維持新狀態，重送一律 409、不執行（token 無狀態：資料若恢復成舊規劃的樣子，舊 token 才又相符）。儲存層筆數核對失敗的 409 `plan_changed`（第二道防線）不附 plan／token。指紋除規劃本身外另含：note 的 `updated`、文件的 status／updated／supersedes、vault 內 note／文件最大的 `updated`。執行後目標已不存在，重送同一 token 得 404。

MCP 有兩種入口，共用同一份工具定義（名稱、參數、說明、回傳；`mcp.server.build_server` 只註冊一次）：

- **HTTP 端點 `/mcp`**（D12，`mcp.http`）：服務內建的 Streamable HTTP（JSON 回應、有 `Mcp-Session-Id` session；也接受 2026-07-28 無 session 的單次請求），客戶端 `claude mcp add --transport http <服務>/mcp --header "Authorization: Bearer <token>"` 直連。認證與 `/v1/*` 相同（外層 `BearerAuthMiddleware`）；工具在服務行程內經 **in-process ASGI** 轉發到同一個 app 的 `/v1/*`，並沿用呼叫端這次請求的 `Authorization`（或 UI cookie＋`X-Lore-Vault-UI`），principal 判定與直接打 `/v1/*` 相同，拿不到呼叫端 header 時不以服務 token 代打（內層 401）。差異：`vault_resolve` 不收 `cwd`（`cwd_not_supported`），改收 `remote_url`（`git remote get-url origin` 的輸出，以 `binding.normalize_remote` 正規化，與 cwd 解析同 key）或 `key`；`upload` 只收 `filename`＋`content_base64`（`path` 回 `path_not_supported`；base64 先以長度擋 `documents.max_file_bytes`，`/mcp` body 上限＝該值的 base64 長度＋1MB）；`download` 不收 `path`、改回 `content_base64`，上限 `mcp.http_download_max_bytes`（預設 1MB，超過回 `too_large` 並提示改用 stdio 殼或 UI）；沒有快照降級。DNS rebinding 保護關閉（經 tunnel／反向代理時 Host 不是 localhost；保護靠 bearer）
- **本地 stdio 殼**（`python -m lore_vault.mcp`，A15），完整客戶端：服務連線失敗、逾時或 502／503／504、Cloudflare 521–524／530 時，`recall`／`get`／`list`／`vault_resolve` 改讀本地快照、只走 lexical 並標 `degraded`；`write`／`update` 直接失敗不排佇列；401／403／其他 4xx 與 500 直接報錯不降級。降級查詢同樣以殼的目前 space 過濾（快照保留 `vaults.space`）。

殼持有「目前 space」：stdio 每個殼行程一份；HTTP 端點依 MCP session（`Mcp-Session-Id`）各一份（最多記 1024 個，淘汰最久沒用的）。只在記憶體、不持久化，新行程／新 session 一律 `dev`。HTTP 沒有 session 的連線（stateless、2026-07-28 單次請求）只能用 `dev`：`space(action="set")` 回 `session_required`，不寫進共用狀態（避免改到別的 agent 的 space）。其他工具沒有 space 參數，殼在每個 `/v1/*` 請求自動注入目前 space（唯一出口 `Shell._send`）。

目標是讓 agent 用最少的上下文拿到足夠決策的資訊。工具數量刻意壓低（目前 13 個；`ask` 依 D11 加入，`download`／`delete`／`undelete` 為 agent 的檔案與刪除操作）。

| 工具 | 回傳 | 說明 |
|---|---|---|
| `space(action, value?)` | `{space, spaces}` | `action="get"` 查詢、`"set"` 切換（`value` 為 `dev`／`lore`／`personal`）；純殼端狀態，不打服務；非法值回工具錯誤 `invalid_space`、狀態不變 |
| `vault_resolve(cwd?, create?, display?, space?, key?, remote_url?)` | vault key、display、space、note 數、binding（dev 由 cwd／remote_url 推算時；`source` 為 `git-remote`／`folder`／`remote_url`） | dev：key 省略時由 `remote_url`（兩種入口皆可）或 cwd（只限 stdio 殼，HTTP 回 `cwd_not_supported`；兩者都沒給時 HTTP 回 `remote_url_required`）以 `lore_vault.binding` 算 key，服務端做別名解析；lore／personal：沒有 repo，必須帶 `key`（`<space>/名稱`，缺少回 `key_required`），傳了 `cwd` 會忽略並回 `cwd_ignored: true`。`space` 省略用目前 space，顯式傳入只影響這一次。`create=True` 才建 vault（HTTP `POST /v1/vaults`）；取代 pm-bind 的手動步驟 |
| `recall(query, vault, kinds?, limit?, budget?)` | `[{id, kind, vault, title, summary, summary_source, score, updated}]`；note 另帶 `author`；chunk 另帶 `document_id`、`chunk_id`、`locator` | 統一檢索 note 與文件段落（`kinds` 預設 `["note", "chunk"]`；concept 未實作）；note 與 chunk 的 lexical／vector 四路一次 RRF。chunk 的 `title` 為檔名、`summary` 為段落摘錄（`summary_source: "excerpt"`），同樣受 `budget`；**預設不含全文**；`vault` 必填，跨範圍用 `vault="*"` 明示（此時雜項 vault 的結果降權，見 Vault 的 `kind`）。回應另有 `kinds`（實際查的）、`missing_chunk_embeddings`；降級時 `chunk` 列在 `unsupported_kinds` |
| `ask(question, vault, kinds?, k?)` | `status`（`answered`／`insufficient`）、`answer.points[{claim, note_ids, unsupported}]`、`dropped_citations`、`sources[{id, vault, title, updated, score, excerpt_truncated}]`、`degraded*`、`model`、`usage`、`latency_ms`、`notice` | D11：以 recall 同一條檢索取前 k 則 note（預設 10、上限 20），片段（標題、LLM 摘要、正文節錄、updated、supersedes）交問答模型（`[ask]`）結構化輸出；引用不在片段內的 note id 機械移除、無有效引用的點標 `unsupported`、`answered` 卻無任何有效引用時降為 `insufficient`（`status_downgraded`）；空輸出／截斷／解析失敗回 `ask_invalid_output`，不回假成功。**本輪只用 note**，文件段落另評估（`chunk` 列在 `unsupported_kinds`）。回答只是片段的整理、信心有限，關鍵事實以 `get` 核對；服務不可達不降級 |
| `get(vault, ids, budget?)` | 全文 | 可批次；`ids` 可混 note id、`doc:…`（整份文件文字，重疊段已去除）、`chunk:…`（單段，含 `locator` 與 `overlap`＝開頭與前一段重疊的字數，段落起頭為 0）；字數預算依 ids 順序分配，超過時截斷並標示（`truncated`、`body_chars`／`text_chars`）；文件文字依 chunk 順序逐段取、預算用完就停（不先串全文）；note 另帶 `superseded_by`；vault 必填（A5）。範圍外或不存在列在 `missing`，降級時文件 id 列在 `unavailable`。HTTP 另有 `fields: "full"（預設）／"meta"`：meta 只回 metadata（note 無 `body`、文件／chunk 無 `text`，`body_chars`／`text_chars` 照給、`truncated: false`、不佔預算、`used_chars` 為 0），不組裝全文；其他值 400。MCP 工具未開放此參數 |
| `list(vault, since?, topics?, cursor?, limit?, kinds?)` | 標題清單 | note 與文件合併分頁（`kinds` 預設兩者）；note 項含 `summary`／`summary_source`（規則同 recall：LLM 摘要，缺時首段頂替 `lead`，正文也空 `none`）、`supersedes`、`superseded_by`；文件項含 `status`、`error_code`、`version`、`supersedes`、`superseded_by`、`chunk_count`、`encoding`；指定 `topics` 時只列 note；降級時 `document` 列在 `unsupported_kinds`。摘要受 HTTP `budget`（預設 4000，本頁 note 摘要字數總和，title 不計）限制，在本頁有摘要文字的 note（LLM 摘要或首段頂替；`none` 與文件不佔預算）間**公平分配**：全部放得下就全給；否則依頁序納入 note，每則下限需求為 min(摘要長度, 40)，累加超過 `budget` 的那則起（尾端）`summary: null`、`summary_source: "omitted"`（第一則一律納入，至少截到 `budget`）；納入者以 water-filling 分配——配額 = floor(剩餘預算／剩餘人數)，短摘要全給、用不完的額度留給較長者，零頭依頁序各 +1。超過配額的摘要截短（結尾 `…`，含在配額內）並標 `summary_truncated: true`，不默默截斷；首段頂替同規則（首段本身 160 字上限是呈現規則，不算預算截短）。**項目與分頁不受預算影響**。每個 note 項目帶 `summary_truncated`；回應另有 `budget`、`used_chars`、`truncated`（有省略或截短）、`summaries_omitted`、`summaries_truncated`。要完整內容用 `get` |
| `write(vault, title, body, topics?, supersedes?, author?)` | id、`author`、`principal`、`links`、`unresolved_links`、疑似重複清單、`dry_run` | 寫入前自動查重，回傳相似 note 讓 agent 決定改用 `update`。`author` 填 agent 自己的角色名（工具描述明寫），不可代填別人。body 的 `[[標題]]` 自動解析進 `links`（見 Note 的 links 規則）。HTTP 另有 `dry_run: true`（**查重預覽**）：同一函式、同一套驗證／vault·space 範圍／supersedes 檢查／查重／連結解析，只在寫入前停下；回 200、無 `id`／`updated`／`author`／`principal`，其餘欄位同正式寫入（`vault`、`links` 為將會存下的值），不喚醒背景 worker。選 `dry_run` 而非獨立端點：範圍與驗證不可能與正式寫入分岔 |
| `update(id, body?, title?, topics?, author?)` | id、`author`、`updated_by`、`updated_by_principal`、`links`、`unresolved_links` | `author` 記為最後修改者（`updated_by`）；links 合併規則見 Note |
| `upload(path?, vault?, filename?, content_base64?)` | `document_id`、`status`、`duplicate`、`version`、`supersedes` | `path` 與 `filename`＋`content_base64` 擇一，轉送 `POST /v1/documents`。`path`：只限 stdio 殼讀本機檔案（只限殼工作目錄與 `mcp.upload_roots`；拒絕 `..` 與 symlink 逃逸）；內容上傳：檔名不可含路徑分隔或控制字元，base64 嚴格解碼、超過上限回 `too_large`。`vault` 省略時只在 stdio 的 dev 用殼工作目錄 binding（HTTP 必填）；服務不可達直接失敗 |
| `download(vault, id, path?, overwrite?)` | `document_id`、`filename`、`mime`、`size_bytes`、`sha256`；stdio 另帶 `path`（實際寫入路徑）、`overwritten`；HTTP 另帶 `content_base64` | 只接受 `doc:…`（note 用 `get`）。轉 `POST /v1/document_download`，殼帶 `max_bytes`＝自己的上限，收的時候再擋一次（`too_large`），寫檔前比對 `X-Lore-Vault-Sha256`（不符 `hash_mismatch`、不落地）。stdio：寫到本機 `path`（檔案或既有目錄；省略＝殼工作目錄＋原檔名），限殼工作目錄與 `mcp.upload_roots`（目標與父目錄都以 realpath 比對，拒絕 `..`、symlink 逃逸與 Windows 特殊路徑形式，`path_not_allowed`），父目錄須已存在（`parent_not_found`），既有檔預設不覆寫（`file_exists`；`overwrite=true` 才以暫存檔＋`os.replace` 取代）；服務給的檔名只當單純檔名，不合格時改用 `document-<id>`。上限為 `documents.max_file_bytes`。HTTP：`path` 回 `path_not_supported`，上限 `mcp.http_download_max_bytes`。服務不可達直接失敗 |
| `delete(vault, id, reason?, confirm_token?)` | 服務的兩段式回應＋`kind`（`note`／`document`）；規劃另帶 `next_step`，執行另帶 `undelete_hint` | 依 id 前綴分派：`doc:` → `document_delete`、`chunk:` 拒絕（`invalid_request`）、其餘 → `note_delete`；語義與錯誤同上方兩段式確認（`invalid_confirm_token`／`confirm_token_expired`／`plan_changed` 附新 plan 與 token）。工具說明要求 agent 把規劃給使用者看、取得明確同意才送第二步，不可自動連打。不能刪 vault。服務不可達直接失敗（不降級） |
| `undelete(id)` | 服務回應＋`kind` | 同樣依前綴分派到 `note_undelete`／`document_undelete`（範圍為目前 space，沒有 `vault` 參數，與端點一致）；`not_restorable` 附 `reason` |
| `status(vault?)` | 健康狀態、最近更新、管線狀態 | 合併 doctor 摘要與 health alert；stdio 殼另附 `shell`（本地快照對帳），HTTP 端點另附 `mcp: {mode, space}` |

刻意**不做**：chat（多輪對話）、model 管理、source 匯入；服務設定只經 UI（上方「執行期設定」，不提供 MCP 工具）。`ask` 是單次問答、只整理檢索片段（D11）。

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
