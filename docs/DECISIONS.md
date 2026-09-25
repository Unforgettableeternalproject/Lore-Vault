# 決策紀錄

## 已定案

| # | 決策 | 理由 |
|---|---|---|
| A1 | Lore Vault 不屬於 U.E.P 系列，不依賴 U.E.P 的程式與 Python 環境 | 艾斯維爾 2026-09-25 指示；之後以接口接入 |
| A2 | 從頭重建，不 fork open-notebook | 大半功能不需要，MCP 需要重新設計 |
| A3 | agent_memory_spike 遷入本 repo | 同一個記憶系統的兩半 |
| A4 | 查詢預設只回標題 / 摘要，全文另取 | 現行 PM 一次查詢灌入數千字 |
| A5 | vault 範圍在儲存層強制 | 現行 `notebook_id` 過濾無效 |
| A6 | 記憶價值以 surprisal 衡量、只能靠行為測試 | spike Phase 0 / 1.5 實證 |
| A7 | 歷史歸屬凍結成欄位，改名用別名表在讀取端接起來 | spike 2026-08-24 repo 改名事故 |
| A8 | 需要跨機器存取；服務以 docker image 常駐、隨系統啟動 | 艾斯維爾 2026-09-25（D3） |
| A9 | 儲存：服務端 SQLite（WAL）+ FTS5（CJK bigram）+ BLOB 向量／NumPy 暴力比對；客戶端唯讀快照 | 艾斯維爾 2026-09-25 採用 D1 建議，依據見 D1 |
| A10 | embedding 沿用 Ollama bge-m3；LLM 用 OpenAI API（key 走 `.env`）；模型寫進設定檔 | 艾斯維爾 2026-09-25（D7） |

## 待裁決

### D1 儲存引擎

**已定案（A9）：服務端 SQLite（WAL）+ FTS5 + 向量存 BLOB、NumPy 暴力比對；客戶端唯讀快照。**
2026-09-25 依 D3（跨機器、docker 常駐）重評。所有寫入都經服務這一個程序，SQLite 的單寫者限制不構成問題。

| 選項 | 結論 |
|---|---|
| **SQLite + FTS5 + NumPy**（建議） | 升級＝複製檔案；備份 `VACUUM INTO`；兩端同一引擎 |
| Postgres + pgvector | 可行但無必要：大版本升級要 dump/restore、pgvector 綁 PG 版本、中文分詞擴充要自建映像；能證成它的條件（多寫入程序、>GB 級資料、既有 PG 維運）都不成立 |
| 沿用 SurrealDB | 舊系統的坑在查詢／MCP 層，換不換引擎都要重寫；多背一套查詢語言 |

依據與實測：

- **中文檢索**：FTS5 trigram 對 2 字詞**靜默回 0 筆**（本機實測「記憶」→0、「記憶系統」→1），pg_trgm 同樣限制。過去 212 筆 PM 搜尋中 **35% 含 2 字中文詞**、58% 純英文、38% 中英混合、平均 6.8 詞的關鍵詞堆疊。解法在服務層：CJK 連續段切 overlapping bigram 後存索引欄，用 `unicode61 tokenchars '_'`（保住 snake_case 識別字）。實測 2 字、3 字、4 字、多詞、CamelCase 識別字皆命中
- **混合檢索**：BM25 + 向量在服務層做 RRF 融合，與引擎無關
- **向量**：不建 ANN 索引。數千條 × 1024 維 ≈ 20MB，暴力 cosine 為毫秒級，且先套 vault 過濾再算、不會有「ANN 候選被範圍過濾掉而少回結果」的靜默漏失（A5）。10 萬條以上再重評。也因此不需要 sqlite-vec
- **Docker 資料卷**：必須用 **named volume**。SQLite／Postgres 經 bind mount 到 NTFS 都有檔案鎖或權限問題。named volume 會隨 Docker Desktop 重置或 vhdx 損毀一起消失，**定期備份到主機是必要步驟**

隨之而來的設計約束：

- **服務不可達**（Docker Desktop 是登入後才啟動）：MCP `recall` 降級讀本地快照，回傳標示 `degraded`；`write` 回報失敗，不做離線寫入佇列
- **embedding 不可用**（主機 Ollama 不受 docker restart policy 管）：`recall` 退回只走 lexical 並標示 `degraded`，不能讓降級結果看起來像正常結果
- **`write` 不等 Ollama／OpenAI**：embedding 與 summary 允許為空、背景補（牽動 D4）
- **同時寫入**：`update` 帶 `expected_updated`，版本不符回衝突，不默默覆蓋
- **時間**：容器為 UTC，所有時間戳凍結成 ISO-8601 UTC；遷移前確認 spike episode 現有時間戳的時區
- **快照**：由主機端拉取、寫暫存檔再 rename，避免 PreToolUse 讀到半檔。現行 concepts.json（2MB）parse 實測 14.6ms，格式（JSON 或 SQLite）不急著換
- **spike 檢索拆分**：檔案／符號錨點比對留在客戶端 hook；BM25 移到服務端。⚠️ spike 的 precision 數字綁著舊 scorer，換排序後行為校準要重跑

要補的 doctor 對帳：每個 vault 的 note 數與內容雜湊（對舊系統匯出）、缺 embedding／summary 的 note 數、FTS 索引列數 vs note 數、快照版本 vs 服務端版本、spool 未推送筆數、最近一次備份時間。

仍需實測：named volume 上 SQLite WAL 在 `docker restart` 中斷下不丟資料；SurrealDB 內舊向量確為 bge-m3、1024 維，以及是一篇一向量還是分塊（決定能否直接搬、向量表形狀）。

### D2 語言與環境

建議 Python 3.12+，專案自有 `.venv`（uv 管理）。hook 路徑維持**只用標準庫**，讓系統 Python 可直接執行。

### D3 服務形態

- 本機單程序：MCP server 直接讀寫儲存（建議起步）
- 常駐 HTTP 服務 + MCP 作為薄殼：要跨機器存取時需要（現行 `pm-proxy.py` + Cloudflare Access 就是為此）

**2026-09-25 艾斯維爾：需要跨機器；維持以 docker image 隨系統啟動。** 服務形態往「常駐 HTTP 服務 + MCP 薄殼」收斂，Cloudflare tunnel 之後要重導到新服務。D1 要依此重評。

spike 接入跨機器架構——**艾斯維爾同意照以下草案試做**（校準能否在容器內跑 `claude -p` 待驗證）：

- **收料**：`Stop` hook 仍在各機器本地執行（標準庫），episode 先寫本地 spool，再非同步推給服務；服務不可達時不阻塞、不遺失，doctor 對帳 spool 與服務端
- **注入**：`PreToolUse` 每次編輯都跑，不能每次走網路——讀本地的 concept 快照，快照由服務定期同步下來
- **管線**：蒸餾／收斂／校準在服務端跑，但校準需要 headless `claude -p`，要確認容器內能不能跑，否則留在主機排程
- **歸屬**：episode 多一個 `machine` 欄位（凍結），`repo_root` 只在同一台機器上有意義

### D4 摘要（`summary`）由誰產生

**2026-09-25 艾斯維爾：傾向全部由 LLM 產生，細節待討論。** 要定的：

- 模型與額度（本機模型 or API）；與 D7 embedding 是否同一供應來源
- 同步還是非同步：寫入時等摘要，或先存、背景補（查詢時暫以正文首段頂替）
- 更新 note 時是否重算、舊 1300 則批次補的成本

### D5 spike 資料目錄

是否從 `~/.claude/agent-memory-spike/` 改名（例如 `~/.lore-vault/`）；實驗中間產物（`*_tasks.json`、`*_verdicts*/`、`consolidate_pairs*` 等）搬或封存。

### D6 對 U.E.P 的接口

MCP、HTTP、或 Python 函式庫形式。等 D3 定案後再決定。

### D7 Embedding 模型

現行設定（2026-09-25 經 MCP `get_default_models` 查得，只存在 SurrealDB，不在任何設定檔）：

| 用途 | 模型 | 來源 |
|---|---|---|
| embedding | `bge-m3:latest`（567M，F16，1024 維） | 本機 Ollama `host.docker.internal:11434` |
| chat / transformation / tools / large context | `gemma4:e4b-it-q4_K_M`（8B Q4） | 同上 |

Ollama 另裝了 `nomic-embed-text`，PM 未使用。

**艾斯維爾：embedding 沿用 bge-m3（沒有問題）；LLM 改用 GPT。** 新系統的模型寫進設定檔，不藏在 DB。

- 沿用 bge-m3 的好處：舊 note 的向量理論上可直接搬，不必重算（要驗證 SurrealDB 內存的是 bge-m3 產生的向量、維度一致）
- 服務在 docker 裡，Ollama 在主機——沿用 `host.docker.internal`；跨機器時 embedding 一律在服務端算，客戶端不需要 Ollama
- **GPT 走 OpenAI API key**（2026-09-25 艾斯維爾定案，key 已備妥）：服務端直接呼叫；key 只放 repo 外或 `.env`（已在 `.gitignore`），經 docker `env_file` 注入，不寫進設定檔與版控。具體模型名寫在設定檔，未定

### D8 跨機器的 MCP transport 與認證

D3 定為跨機器後新增。要定的：

- 遠端機器的 MCP 直接連服務的 streamable HTTP，或本地 stdio 殼轉發 HTTP（殼可順便負責快照拉取與降級）
- 認證：沿用 Cloudflare Access（service token）或服務自帶 token；本機連線是否免認證
- 現行 `pm-proxy.py` 的角色由誰接手

不阻擋 D1。
