# 任務規劃

> 依 [ARCHITECTURE.md](ARCHITECTURE.md)、[DECISIONS.md](DECISIONS.md)、[MIGRATION.md](MIGRATION.md) 拆解。
> 「阻塞：Dx」＝該卡動工前 Dx 必須定案；「前置 spike」＝先寫拋棄式腳本量測，不進正式程式；
> 「需艾斯維爾明確授權」＝切換類動作，任何情況都不可由子代理自行執行。

## 開工前需裁決

> D2 已於 2026-09-25 定案（A11），相關卡解除阻塞。A12：使用者 UI 最後處理。docker 已授權實測，但不得動到現行 Open Notebook 容器與資料。

| # | 待裁決項 | 最晚要在哪張卡前定案 | 說明 |
|---|---|---|---|
| D4 | `summary` 由誰產生、同步或非同步 | T-19（`write`／`update` 摘要欄位邏輯） | 不擋儲存 schema——`summary` 本就因 A9「`write` 不等 Ollama／OpenAI」而必須是 nullable，T-11 直接照此設計；只擋摘要產生流程與 ON 匯入的 1300 則批次補（T-35） |
| D5 | spike 資料目錄是否改名、中間產物去留 | T-46（資料目錄搬遷，切換階段） | 不擋 MIGRATION 建議順序步驟 2「路徑常數集中成設定」（T-10）——常數先集中，值待 D5 定案再填 |
| D6 | 對 U.E.P 的接口形式（MCP／HTTP／函式庫） | T-32（U.E.P 接口卡） | D3 已定案（HTTP+MCP 薄殼），D6 只等這張卡本身，優先度最低，HTTP／MCP 上線後任何時間點都可定案 |
| D8 | 跨機器 MCP transport 與認證 | T-29（認證中介層）、T-30（MCP 薄殼 transport 選型）、T-45（Cloudflare tunnel 改導） | 不擋 HTTP API 端點本身（T-23~T-28）與快照格式／atomic 寫入（T-21）；只擋認證、transport 選型、tunnel 改導、`pm-proxy.py` 接手者、快照「由誰拉」的歸屬（殼端或獨立程序） |

## 第一批可立即開工（不受任何待裁決阻塞）

T-01、T-02、T-03（前置 spike 實測）、T-04、T-05（subtree 併入與既有測試）、T-10（路徑常數集中，值留白）、T-11（schema 設計文件與 dataclass，純標準庫）、T-12（binding 邏輯）

---

## 任務拆解

### 階段 0：前置 spike 實測（先於一切，驗證假設）

#### T-01: named volume 上 SQLite WAL 中斷測試
- 範圍：起一個臨時 SQLite（WAL 模式）容器化在 named volume，寫入中途 `docker restart`／`kill -9`，重啟後檢查資料完整性與 WAL 是否正確 replay
- 輸入/輸出：拋棄式腳本 + 測試記錄（結果寫回 DECISIONS.md D1「仍需實測」段落，不進正式程式）
- 依賴：無
- 驗收標準：至少 3 輪「寫入中斷」情境（寫入中途 kill、checkpoint 中途 kill、正常關閉）都驗證資料無遺失或明確記錄失敗模式；結論寫成一段文字附進 DECISIONS.md
- 預估：M

#### T-02: SurrealDB 舊向量規格檢查
- 範圍：連進現行 Open Notebook 的 SurrealDB，抽樣 note 的 embedding 欄位，確認維度（是否 1024）、產生模型（是否 bge-m3）、粒度（一篇一向量或分塊）
- 輸入/輸出：抽樣腳本 + 結論（決定 T-33 匯入卡能否直接搬向量、向量表形狀）
- 依賴：無
- 驗收標準：抽樣至少 20 篇 note，記錄向量維度、疑似來源模型比對（與已知 bge-m3 輸出比對或詢問 SurrealDB metadata）、是否每篇僅一筆向量；結論寫進 DECISIONS.md D1
- 預估：S

#### T-03: 容器內 headless `claude -p` 可行性測試
- 範圍：在候選 docker base image 內測試 `claude -p` 能否執行，對照 MIGRATION A.8 四個已知坑（吃全域 CLAUDE.md／SessionStart 注入、寫不進 `~/.claude/`、Bash allowlist 認字面路徑、prompt 走 stdin）
- 輸入/輸出：測試記錄，決定 T-41（校準管線容器化）走哪個分支
- 依賴：無
- 驗收標準：明確回答「容器內能/不能跑」，若能則記錄需要的映像調整（掛載、環境變數）；若不能，T-41 直接採用「留在主機排程」分支，不留待議
- 預估：M

---

### 階段 1：骨架與設定

#### T-04: git subtree split 出 spike 子樹
- 範圍：`git subtree split -P agent_memory_spike`（或 `filter-repo --path agent_memory_spike/`）從 `TestSeperateMemorySystem` 切出歷史子樹
- 涉及檔案：`TestSeperateMemorySystem/agent_memory_spike/` 全部 11 模組 + 8 測試檔
- 依賴：無
- 驗收標準：切出的子樹保留完整 commit 歷史（58 個相關 commit，其中 56 個只動 spike 路徑）；2 個只多動 `.gitignore` 的 commit 明確記錄為會被濾掉
- 預估：S

#### T-05: subtree 併入 Lore-Vault，跑通既有 134 項測試
- 範圍：把 T-04 產出的子樹合併進本 repo（未改任何路徑），在本 repo 環境下跑 `test_consolidate.py`／`test_distill.py`／`test_health_alert.py`／`test_inject.py`／`test_pipeline.py`／`test_prompt_inject.py`／`test_session_inject.py`／`test_transcript.py`
- 涉及檔案：併入後的 spike 模組樹（暫居原始相對路徑，不搬動）
- 依賴：T-04
- 驗收標準：134 項測試全數通過，且未修改任何模組內容或路徑（用 diff 對照 T-04 切出結果確認零改動）；記錄使用的直譯器版本（系統 Python 3.14）
- 預估：M

#### T-06: `.gitignore` 補規則
- 範圍：補回 T-04 濾掉的 2 個 `.gitignore` commit 的規則，含 `data/golden_memories.json` 白名單例外
- 涉及檔案：`.gitignore`
- 依賴：T-05
- 驗收標準：`git status` 在資料目錄有內容時不會誤把商業語料相關檔案加入追蹤；`data/golden_memories.json` 明確可被追蹤（如果它本就該進版控）
- 預估：S

#### T-07: 專案骨架與套件結構
- 範圍：建立 `pyproject.toml`（Python 版本、依賴群組）、`.venv`（uv 管理）、正式套件目錄骨架（`mcp/`、`hooks/`、`cli/`、`api/`、`notes/`、`recall/`、`inject/`、`pipeline/`、`doctor/`、`schema/`、`binding/`、`storage/`）
- 涉及檔案：`pyproject.toml`、`src/` 或等效佈局、`.venv`
- 依賴：T-05
- 驗收標準：`uv sync`（或裁定的等效指令）成功建立環境；套件目錄可被 import；hook 相關子套件（`hooks/`）明確標註「只用標準庫」約束並用測試證明 import 時不觸發任何第三方套件載入
- 預估：S

#### T-08: hook 路徑零依賴防呆
- 範圍：為 `hooks/` 子套件寫一個 import-time 檢查（doctor 對帳項的一部分），確保系統 Python 能直接執行 hook 進入點而不需要 `.venv`
- 涉及檔案：`hooks/`、`doctor/`
- 依賴：T-07
- 驗收標準：doctor 檢查「hook 模組是否 import 了 `.venv` 專屬套件」；測試證明故意在 hook 模組加一行 `import numpy` 後該檢查會變紅
- 預估：S

#### T-09: CI／本機測試腳本骨架
- 範圍：訂出測試指令（`pytest` 或延續 spike 原生 `unittest` 執行方式）、lint／格式化工具選型落地
- 涉及檔案：`pyproject.toml`、CI 設定（若有）
- 依賴：T-07
- 驗收標準：一鍵指令可跑完 134 項既有測試 + 任何新測試；文件記錄指令
- 預估：S

---

### 階段 2：spike 資料路徑整理

#### T-10: 資料路徑常數集中成設定（值留白，待 D5）
- 範圍：把散在 `transcript.py`／`hook_stop.py` 等處的資料目錄常數集中到單一設定模組／設定檔，讀取邏輯改為從設定取值，預設值先維持現行 `~/.claude/agent-memory-spike/` 不搬動實際資料
- 涉及檔案：`transcript.py`、`hook_stop.py`、其餘引用資料路徑常數的模組、新設定模組
- 依賴：T-05
- 驗收標準：`grep` 全 repo 確認資料路徑不再硬編碼分散於多檔；134 項測試仍全數通過；實際磁碟資料目錄未被搬動（真正改名留給 T-46，阻塞 D5）
- 預估：M

---

### 階段 3：核心 schema／binding

#### T-11: Vault／Note／Episode／Concept／Injection schema
- 範圍：依 ARCHITECTURE.md 資料模型定義 dataclass／pydantic-free 的純標準庫 schema（避免提前綁死儲存引擎），`summary` 欄位設計為 nullable（因 D1/A9「`write` 不等摘要」）
- 涉及檔案：`schema/`
- 依賴：T-05（不需 T-07，純標準庫可先行）
- 驗收標準：五個型別涵蓋 ARCHITECTURE.md 列出的全部欄位；`summary` 可為 `None`；`supersedes` 表達更正關係而非另建更正篇；單元測試涵蓋每個型別的建構與驗證
- 預估：M

#### T-12: binding 邏輯（git remote → vault key）
- 範圍：沿用 pm-bind 邏輯，把 git remote 正規化為 vault key（`github.com/owner/repo`），無 remote 時 `folder/<name>`；支援 `aliases`（改名前舊 key）
- 涉及檔案：`binding/`
- 依賴：T-11
- 驗收標準：對現行 pm-bind 已知的綁定案例（含至少一個曾改名的 repo）跑通並得到與現行一致或有意記錄差異的結果；alias 解析測試涵蓋「舊 key 查得到同一 vault」
- 預估：M

#### T-13: 歷史歸屬凍結測試（防 A7 repo 改名事故重演）
- 範圍：針對 Episode 的 `repo`、`repo_root`、`machine` 等凍結欄位寫測試，確認寫入當下固定值、不因環境變數／目前 cwd 改變而在讀取時重算
- 涉及檔案：`schema/`、`binding/`
- 依賴：T-11、T-12
- 驗收標準：測試模擬「repo 改名後再讀舊 episode」情境，凍結欄位維持寫入當時的值；拿掉凍結邏輯（改回讀取時重算）時此測試會紅
- 預估：S

#### T-14: doctor 骨架與對帳框架
- 範圍：建立 `doctor/` 模組的通用框架（檢查項註冊、報告格式、exit code 慣例），供後續各階段掛入具體對帳項
- 涉及檔案：`doctor/`
- 依賴：T-11
- 驗收標準：可註冊至少一個範例檢查項並產出結構化報告（通過／失敗／缺項）；空的 doctor 執行不報錯
- 預估：S

---

### 階段 4：儲存與檢索（SQLite + FTS5 + NumPy 向量）

#### T-15: SQLite schema 與遷移腳本
- 範圍：依 T-11 schema 建表（vaults／notes／episodes／concepts／injections），WAL 模式，向量存 BLOB
- 涉及檔案：`storage/schema.sql` 或等效、`storage/migrate.py`
- 依賴：T-07、T-11
- 驗收標準：可從空檔案建出完整 schema；`PRAGMA journal_mode=WAL` 生效；doctor 補「schema 版本 vs 程式預期版本」對帳項，並用測試證明手動改動 schema 版本號後該項變紅
- 預估：M

#### T-16: vault 硬過濾（服務端強制）
- 範圍：所有讀寫 API 在儲存層強制帶 vault 條件，未傳 vault 直接拋錯，跨 vault 查詢需明確 `vault="*"`
- 涉及檔案：`storage/`、`notes/`、`recall/`
- 依賴：T-15
- 驗收標準：查詢不傳 vault 時拋例外（不是靜默回全域結果）；`vault="*"` 明確可用；新增跨 vault 洩漏測試（故意繞過過濾層直接查 DB 驗證資料確實分 vault 儲存，再驗證正常 API 路徑不會洩漏）；測試證明拿掉過濾條件時洩漏測試會紅
- 預估：M

#### T-17: CJK bigram FTS5 索引
- 範圍：實作 CJK 連續段切 overlapping bigram 後存索引欄，FTS5 用 `unicode61 tokenchars '_'`（保住 snake_case 識別字）
- 涉及檔案：`storage/fts.py`
- 依賴：T-15
- 驗收標準：「記憶」（2字）、「記憶系統」（3字以上）、4字詞、多詞查詢、CamelCase 識別字、snake_case 識別字皆命中（依 DECISIONS.md D1 已列出的實測案例）；doctor 補「FTS 索引列數 vs note 數」對帳項，測試證明手動刪一筆 FTS 列後該項變紅
- 預估：M

#### T-18: NumPy 向量暴力比對
- 範圍：向量存 BLOB，查詢時先套 vault 過濾再算 cosine（暴力比對，不建 ANN 索引）
- 涉及檔案：`storage/vectors.py`
- 依賴：T-16
- 驗收標準：向量比對結果不因為 vault 過濾而漏掉候選（即「先過濾後算」而非「先算 ANN 候選再過濾」，避免 A5 提到的靜默漏失）；效能量測記錄（數千條 × 1024 維應為毫秒級）；doctor 補「缺 embedding 的 note 數」對帳項
- 預估：M

#### T-19: `write`／`update` 摘要欄位邏輯〔阻塞：D4〕
- 範圍：依 D4 裁決結果實作摘要產生（同步或非同步）、查詢時暫以正文首段頂替缺摘要的情況
- 涉及檔案：`notes/`、`storage/`
- 依賴：T-15、D4 定案
- 驗收標準：依 D4 最終方案訂（此處先列共同底線）：`summary` 缺值時查詢回傳正文首段頂替並標示來源非摘要；doctor 補「缺 summary 的 note 數」對帳項
- 預估：M

#### T-20: RRF 混合排序（BM25 + 向量）
- 範圍：服務層做 BM25 與向量分數的 RRF 融合，與儲存引擎無關
- 涉及檔案：`recall/`
- 依賴：T-17、T-18
- 驗收標準：混合排序結果對已知案例（如 DECISIONS.md 提到的中英混合查詢）給出合理排序；純 lexical 與純向量各自可獨立跑（供降級模式使用）；單元測試覆蓋兩路都空、只有一路有結果的邊界
- 預估：M

#### T-21: `write` 查重與 `update` 樂觀鎖
- 範圍：`write` 前自動查重回傳相似 note 清單；`update` 帶 `expected_updated`，版本不符回衝突不覆蓋
- 涉及檔案：`notes/`
- 依賴：T-16、T-20
- 驗收標準：查重命中已知相似 note 時回傳清單供 agent 決定；`update` 版本不符時明確回衝突錯誤而非靜默覆蓋；測試證明拿掉版本檢查後衝突測試會紅
- 預估：M

#### T-22: 服務不可達／embedding 不可用降級標示
- 範圍：`recall` 在服務不可達時讀本地快照並標 `degraded`；embedding 不可用時退回純 lexical 並標 `degraded`；`write` 服務不可達時回報失敗，不做離線寫入佇列
- 涉及檔案：`recall/`、`mcp/`
- 依賴：T-20
- 驗收標準：兩種降級情境都在回傳結構中明確帶 `degraded: true`（不能讓降級結果看起來像正常結果）；`write` 在服務不可達時的錯誤訊息明確、不靜默排入佇列；測試模擬兩種故障各自觸發對應降級路徑
- 預估：M

#### T-23: 時間戳一致性（ISO-8601 UTC）
- 範圍：所有時間戳欄位統一存 ISO-8601 UTC；遷移前確認 spike episode 現有時間戳的時區
- 涉及檔案：`schema/`、`storage/`
- 依賴：T-15
- 驗收標準：新寫入資料的時間戳格式測試；針對既有 spike episode 資料做時區抽樣確認（記錄結論，供 T-38 遷移引用）
- 預估：S

---

### 階段 5：HTTP 服務 + docker

#### T-24: HTTP API 端點骨架（不含認證）
- 範圍：依 MCP 介面草案對應的服務層功能建 HTTP 端點（`vault_resolve`／`recall`／`get`／`list`／`write`／`update`／`status`），先不含認證邏輯
- 涉及檔案：`api/`
- 依賴：T-16、T-20、T-21
- 驗收標準：各端點可本機呼叫並回傳結構符合 ARCHITECTURE.md 表格定義；`recall` 預設不含全文；`get` 才回全文；回應有字數預算上限且超過時標示截斷
- 預估：L

#### T-25: `status` 端點合併 doctor 摘要與健康告警
- 範圍：`status(vault?)` 彙整目前已有的 doctor 檢查項與（未來）健康告警資訊
- 涉及檔案：`api/`、`doctor/`
- 依賴：T-24、T-14
- 驗收標準：呼叫 `status` 能看到目前所有已註冊 doctor 檢查項的結果摘要
- 預估：S

#### T-26: Dockerfile 與 docker-compose（named volume）
- 範圍：把 T-24 的 HTTP 服務打包成 docker image，資料用 named volume 掛載（禁止 bind mount 到 NTFS）
- 涉及檔案：`Dockerfile`、`docker-compose.yml`
- 依賴：T-24
- 驗收標準：`docker compose up` 本機可跑（僅在明確被指示時才實際啟動驗證，設計期先以檔案審查與乾跑確認）；資料卷確實是 named volume；embedding 呼叫走 `host.docker.internal`
- 預估：M

#### T-27: 備份腳本（`VACUUM INTO`）與 doctor 對帳
- 範圍：定期把 SQLite 備份到主機（named volume 會隨 Docker Desktop 重置或 vhdx 損毀一起消失，備份是必要步驟）
- 涉及檔案：`storage/backup.py`、`doctor/`
- 依賴：T-15、T-26
- 驗收標準：`VACUUM INTO` 備份出可獨立開啟驗證的檔案；doctor 補「最近一次備份時間」對帳項，測試證明備份腳本沒跑時該項變紅（超過設定門檻時）
- 預估：M

#### T-28: 認證中介層〔阻塞：D8〕
- 範圍：依 D8 裁決結果（Cloudflare Access service token 或服務自帶 token；本機是否免認證）實作 HTTP API 認證
- 涉及檔案：`api/`
- 依賴：T-24、D8 定案
- 驗收標準：依最終方案訂；至少涵蓋「未認證請求被拒」與「本機請求（若裁定免認證）正常放行」兩類測試
- 預估：M

---

### 階段 6：MCP 薄殼

#### T-29: MCP transport 選型與實作〔阻塞：D8〕
- 範圍：依 D8 決定遠端機器 MCP 是直連服務的 streamable HTTP，或本地 stdio 殼轉發 HTTP
- 涉及檔案：`mcp/`
- 依賴：T-24、D8 定案
- 驗收標準：依最終方案訂；本機與（若適用）跨機器情境各有一條端到端測試（呼叫 MCP 工具 → 實際打到 HTTP API）
- 預估：M

#### T-30: 七個 MCP 工具實作
- 範圍：`vault_resolve`／`recall`／`get`／`list`／`write`／`update`／`status`，工具數量壓在個位數，刻意不做 chat/ask、model 管理、settings、source 匯入
- 涉及檔案：`mcp/`
- 依賴：T-29
- 驗收標準：七個工具皆可被 MCP client 呼叫並回傳 ARCHITECTURE.md 定義的結構；確認未額外暴露 chat/ask 等刻意排除的工具
- 預估：M

#### T-31: 快照拉取殼（讀）與降級讀取
- 範圍：殼負責從服務拉快照（若 D8 裁定殼端負責）、寫暫存檔再 rename（避免 PreToolUse 讀到半檔）；MCP `recall` 在服務不可達時走此快照
- 涉及檔案：`mcp/`、`storage/snapshot.py`
- 依賴：T-22、T-29
- 驗收標準：快照寫入是 tmp 寫檔 + rename 的原子操作；doctor 補「快照版本 vs 服務端版本」對帳項；測試模擬服務不可達時 MCP `recall` 確實吃到快照並標 `degraded`
- 預估：M

#### T-32: U.E.P 接口〔阻塞：D6〕
- 範圍：依 D6 裁決（MCP／HTTP／Python 函式庫）提供 U.E.P（Echo Memory 等）接入的明確介面；不 import U.E.P 程式碼，不依賴其 Python 環境
- 涉及檔案：視 D6 結果而定（`mcp/` 或 `api/` 或新的介面模組）
- 依賴：T-24 或 T-30（視 D6 結果）、D6 定案
- 驗收標準：依最終方案訂；至少一條端到端測試證明 U.E.P 端可用該介面查得資料，且 Lore Vault 端未反向依賴 U.E.P
- 預估：M

---

### 階段 7：Open Notebook 匯入與對帳

#### T-33: ON note 匯出（REST API／既有 pm-cache-sync 匯出）
- 範圍：透過 REST API 或既有 `pm-cache-sync` 的 markdown 匯出，取得每則 note 的 title／content／topics／created／updated 與所屬 notebook
- 涉及檔案：`cli/import_on.py`
- 依賴：T-11
- 驗收標準：15 本 notebook、1300+ 則 note 全數匯出成中繼格式；匯出筆數與 ON 端原始筆數一致
- 預估：M

#### T-34: notebook → vault 綁定轉換
- 範圍：notebook description 中的 `[bind: <key>]` 轉成 Vault `key`；沒有標記的舊 notebook 以名稱 `[PM] <display>` 對應，歧義者列清單供人工確認
- 涉及檔案：`cli/import_on.py`、`binding/`
- 依賴：T-12、T-33
- 驗收標準：每個 notebook 都能對到唯一 vault key 或被列入「需人工確認」清單；清單人工過一輪後零歧義
- 預估：M

#### T-35: `[[標題]]` 連結解析
- 範圍：把 `[[標題]]` 解析成 note id；解析不到的保留原文並列入報告
- 涉及檔案：`cli/import_on.py`
- 依賴：T-33
- 驗收標準：可解析連結全數轉成 note id；解析不到的清單完整列出（供人工判斷是否為死連結或跨 notebook 引用）
- 預估：S

#### T-36: 舊 note 摘要補齊〔阻塞：D4〕
- 範圍：1300 則舊 note 沒有 `summary`，依 D4 最終方案批次補
- 涉及檔案：`cli/import_on.py`
- 依賴：T-19、D4 定案
- 驗收標準：依最終方案訂；補齊後每則 note 的 `summary` 非空或明確標示待補；記錄批次補的實際成本（時間／API 用量）供對照 D4 討論時的估算
- 預估：M（若同步即時生成則偏 L，視 D4 結果）

#### T-37: 匯入對帳（逐 vault 比對筆數與內容雜湊）
- 範圍：匯入後逐 vault 比對 note 數與內容雜湊，doctor 可重跑
- 涉及檔案：`doctor/`、`cli/import_on.py`
- 依賴：T-33、T-34
- 驗收標準：doctor 報告每個 vault 的匯入前後筆數、內容雜湊比對結果；測試證明故意漏匯入一筆或竄改一筆內容雜湊時該項變紅
- 預估：M

---

### 階段 8：spike 接入（spool、快照、管線）

#### T-38: episode 本地 spool + 非同步推送
- 範圍：`Stop` hook 仍在各機器本地執行（標準庫），episode 先寫本地 spool，再非同步推給服務；服務不可達時不阻塞、不遺失
- 涉及檔案：`hooks/hook_stop.py`（延續 spike 版本改寫）、`storage/spool.py`
- 依賴：T-08、T-24
- 驗收標準：服務不可達時 hook 不阻塞（延遲測試）、episode 確實留在本地 spool；doctor 補「spool 未推送筆數」對帳項，測試證明推送邏輯故障時該項變紅
- 預估：M

#### T-39: episode `machine` 欄位
- 範圍：Episode 多一個 `machine` 欄位（凍結），`repo_root` 只在同一台機器上有意義
- 涉及檔案：`schema/`、`hooks/hook_stop.py`
- 依賴：T-13、T-38
- 驗收標準：跨機器情境下同一 repo 的 episode 能以 `machine` 欄位區分；凍結測試比照 T-13 模式
- 預估：S

#### T-40: PreToolUse 注入改讀本地快照
- 範圍：`PreToolUse` 每次編輯都跑，不能每次走網路——讀本地的 concept 快照，快照由服務定期同步下來
- 涉及檔案：`hooks/hook_pretooluse.py`（延續 spike 版本改寫）
- 依賴：T-08、T-31
- 驗收標準：注入路徑延遲量測（不因等網路而變慢）；快照缺失時明確降級（不注入或標示降級，不拋例外中斷編輯）；⚠️ 換排序後（RRF 取代舊 scorer）行為校準要重跑，此卡驗收不含 precision 數字比對
- 預估：M

#### T-41: 蒸餾／收斂／校準管線服務端化〔可能阻塞：D8，視 T-03 結果分支〕
- 範圍：蒸餾／收斂／校準在服務端跑；若 T-03 結論為「容器內可跑 `claude -p`」則管線容器化，否則留在主機排程
- 涉及檔案：`pipeline/`（延續 spike `distill.py`／`consolidate.py`／`calibrate.py`／`pipeline.py`）
- 依賴：T-03、T-26
- 驗收標準：依 T-03 分支結果擇一實作；lockfile 機制延續；⚠️ spike 的 precision 數字綁著舊 scorer，換排序後校準要重新跑一輪並記錄新基準，不得直接沿用舊數字宣稱效能持平
- 預估：L

---

### 階段 9：切換〔全部需艾斯維爾明確授權，任何子代理不得自行執行〕

#### T-42: 全域 hook 路徑整批替換
- 範圍：`~/.claude/settings.json` 的 `SessionStart`（health alert）、`Stop`、`PreToolUse` 三個 hook 一次整批替換為新路徑，確認舊路徑完全移除
- 涉及檔案：`~/.claude/settings.json`
- 依賴：T-38、T-40、T-08
- 驗收標準：新舊路徑不並存（並存會造成一輪注入雙倍條目）；替換後手動觸發一次三種 hook 各自正常
- 預估：S（需艾斯維爾明確授權）

#### T-43: Windows 排程重新註冊
- 範圍：`AgentMemoryPipeline`（每日 03:30）指向新的執行腳本，改用本專案自己的直譯器（不依賴 U.E.P env）；PowerShell 腳本保留兩個坑：UTF-8 with BOM、Python 輸出走 `cmd /c` 重導向不用 `*>>`
- 涉及檔案：Windows 工作排程器設定、`run_pipeline.ps1`（新版）
- 依賴：T-41
- 驗收標準：排程指向新腳本並成功跑過至少一次；BOM／重導向兩個坑各有明確驗證（中文註解未被吃掉、輸出正確落檔）
- 預估：M（需艾斯維爾明確授權）

#### T-44: `~/.claude.json` MCP 切換
- 範圍：新 MCP 上線並驗證後，才從 `~/.claude.json` 移除 `open-notebook`，加入新 Lore Vault MCP 設定
- 涉及檔案：`~/.claude.json`
- 依賴：T-30
- 驗收標準：切換後新 MCP 工具可用；舊 `open-notebook` 條目確實移除；舊 ON 容器保留唯讀一段時間當退路（不因這步而下線容器）
- 預估：S（需艾斯維爾明確授權）

#### T-45: Cloudflare tunnel 改導
- 範圍：`pm-proxy.py` 走 Cloudflare Access 的反向代理，改導到新服務
- 涉及檔案：Cloudflare tunnel 設定、`pm-proxy.py` 後繼
- 依賴：T-28、D8 定案
- 驗收標準：跨機器連線改走新服務且認證正常；決定 `pm-proxy.py` 角色由誰接手（沿用或汰換）並記錄
- 預估：M（需艾斯維爾明確授權）

#### T-46: spike 資料目錄搬遷〔阻塞：D5〕
- 範圍：依 D5 裁決決定是否改名（如 `~/.lore-vault/`）、實驗中間產物（`*_tasks.json`、`*_verdicts*/`、`consolidate_pairs*` 等）搬或封存
- 涉及檔案：資料目錄本身、T-10 建立的設定模組
- 依賴：T-10、D5 定案
- 驗收標準：搬遷後 T-10 設定模組指向新路徑，既有資料完整可讀；封存的中間產物有清單記錄去向
- 預估：M（需艾斯維爾明確授權）

#### T-47: claude-codex-pipeline plugin 改接新 MCP
- 範圍：`skills/dispatch/SKILL.md`、`skills/complete/SKILL.md`、`references/pm-integration.md` 目前直接寫死 `mcp__open-notebook__*`，改接新 MCP 工具；同步更新已安裝副本 `~/.claude/plugins/cache/uep-pipeline/`
- 涉及檔案：上述 plugin 檔案（repo 內與已安裝副本）
- 依賴：T-30、T-44
- 驗收標準：plugin 的 dispatch／complete 流程改用新 MCP 工具後端到端跑一次成功；確認已安裝副本與 repo 版本一致
- 預估：M（需艾斯維爾明確授權）

#### T-48: 各專案 CLAUDE.md 的 `[PM]` 綁定敘述更新
- 範圍：Chatroom、Echo-Stream、Eternity、Lore-Vault、TestSeperateMemorySystem、U.E.P-s-Core 六個專案的 CLAUDE.md 中 `[PM] <repo>` 綁定敘述改為指向新系統
- 涉及檔案：六個專案各自的 `CLAUDE.md`
- 依賴：T-44
- 驗收標準：六個檔案全數更新；每個專案下手動確認記憶協定描述與實際掛載的 MCP 一致
- 預估：M（需艾斯維爾明確授權）

#### T-49: pm skill 與全域 CLAUDE.md 記憶協定改寫
- 範圍：pm skill 內容與全域 CLAUDE.md「總是優先載入 PM」相關描述改為指向 Lore Vault
- 涉及檔案：pm skill、`~/.claude/CLAUDE.md`
- 依賴：T-44、T-48
- 驗收標準：pm skill 觸發後走新系統；全域 CLAUDE.md 描述與實際行為一致
- 預估：S（需艾斯維爾明確授權）

#### T-50: `~/.claude/pm/`、`~/.claude/pm-kit/` 去留裁定
- 範圍：`pm-bind`／`pm-cache-sync`／`pm-proxy` 及其在 `pm-kit/` 的第二份副本，決定保留、汰換或封存
- 涉及檔案：`~/.claude/pm/`、`~/.claude/pm-kit/`
- 依賴：T-44、T-45
- 驗收標準：兩個目錄的去留有明確記錄；若保留需說明理由與後續維護責任
- 預估：S（需艾斯維爾明確授權）

#### T-51: 移除 spike 於 TestSeperateMemorySystem（另開 PR）
- 範圍：確認排程與 hook 在 Lore Vault 上跑過 1–2 晚正常後，從 `TestSeperateMemorySystem` 移除 spike（不影響 echo_memory 主線，另開 PR）
- 涉及檔案：`TestSeperateMemorySystem/agent_memory_spike/`
- 依賴：T-42、T-43
- 驗收標準：Lore Vault 側連續 1–2 晚排程與 hook 正常運作後才執行；移除後 `TestSeperateMemorySystem` 的 echo_memory 主線測試不受影響
- 預估：S（需艾斯維爾明確授權）

---

## 執行順序（依相依關係，各階段內可再平行）

```
[T-01 + T-02 + T-03]（前置 spike，互相獨立）
        │
        ▼
T-04 → T-05 → T-06
        │
        ├──→ T-10（路徑常數集中，值留白）
        │
        ├──→ T-11 → T-12 → T-13
        │              │
        ▼              ▼
      T-07 → T-08 → T-09          T-14
        │
        ▼（阻塞：D2 定案後）
T-15 → T-16 → T-17
        │        │
        │        ▼
        │      T-18 → T-20 → T-21 → T-24 → T-25
        │        │                    │
        ▼        ▼                    ▼
      T-19*   T-22 → T-23           T-26 → T-27
   （*阻塞 D4）                        │
                                       ▼
                                T-28*（阻塞 D8）
                                       │
                                       ▼
                        T-29* → T-30 → T-31 → T-32*
                     （*阻塞 D8）      │    （*阻塞 D6）
                                       │
                          ┌────────────┴────────────┐
                          ▼                          ▼
              T-33 → T-34 → T-35 → T-36*      T-38 → T-39
                          │      （*阻塞 D4）    │
                          ▼                      ▼
                        T-37                   T-40
                                                 │
                                                 ▼
                                        T-41（依 T-03 分支）
                                                 │
                                                 ▼
                        ── 以下需艾斯維爾明確授權 ──
                    T-42 → T-43 → T-51
                    T-44 → T-45*（阻塞 D8）
                         → T-47 → T-48 → T-49
                    T-46*（阻塞 D5）
                    T-50
```

## 風險評估

- 🔴 D8（transport／認證）未定案會連帶卡住 T-29～T-32、T-45，是目前阻塞面最廣的待裁決項；建議優先排。
- 🔴 T-03（容器內 `claude -p`）若結論為「不可行」，T-41 整個管線容器化方向要重新設計成主機排程 + 服務端資料存取的混合形態，影響 T-43 排程重新註冊的腳本形態。
- 🟡 T-17（CJK bigram FTS5）與 T-20（RRF）換掉 spike 原本的 scorer，spike 既有 precision 數字（6.5%／28.1% 對照組）不能直接沿用，T-40／T-41 都需要重新校準才能宣稱新系統效能不劣於舊系統。
- 🟡 T-33～T-37（ON 匯入）規模上看 1300+ 則 note、15 本 notebook，歧義綁定與死連結需要人工介入，實際工時可能超出單卡 L 級估算，必要時再拆卡。
- 🟡 T-02（SurrealDB 向量規格）若結論是「分塊向量」而非「一篇一向量」，T-18 向量表形狀與 T-33 匯入邏輯都要重新設計，不能沿用「直接搬」的樂觀假設。
- 🟢 T-04／T-05（subtree 併入）風險低，spike 已有 134 項測試把關，且不改路徑即跑通。
