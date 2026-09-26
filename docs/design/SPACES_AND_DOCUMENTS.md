# 設計提案：space 分群（A18）與文件存儲檢索（D10）

> 狀態：設計草案，待艾斯維爾裁決「需裁決清單」後定案。定案前不實作。
> 依據：`docs/ARCHITECTURE.md`、`docs/DECISIONS.md`（A4、A5、A9、A10、A14、A15、A17、A18、D9、D10）。
> 現況基準：schema v6（`storage/migrate.py` `MIGRATIONS` 6 筆）。

---

## 0. 兩人一起看過一輪之後

**奈也**：space 這件事說到底是「同一個地方要住三種人」——開發用的、寫故事用的、私人的。
我覺得核心是「不要讓 dev 的查詢意外看到 lore 的內容」，其他都是細節。

**奈留**：對，但「不要意外看到」在系統裡只有一種寫法：跟 vault 範圍一樣，在儲存層擋，
不是在查詢時「順便」加個條件。А5 已經證明「靠參數自覺傳」這件事會被繞過（`notebook_id` 的教訓）。
所以 space 要嘛是 vault 的強制屬性、嘛就不要做。

兩人都同意：**space 是 vault 的欄位，不是新的一層容器**。下面按此展開。

---

## 1. space 資料模型

### 1.1 ADR-01：space 是 vault 的屬性，不是獨立實體

**背景**：A18 已定案 `dev`／`lore`／`personal` 三態，現有 vault 全歸 `dev`。要決定 space 與 vault
的資料關係：space 包住多個 vault（複合鍵 `(space, key)`），還是 space 只是 vault 的一個欄位。

**決策**：`space` 是 `vaults` 表新增的一個 `TEXT NOT NULL` 欄位，**`key` 維持全域唯一**（不做
`(space, key)` 複合鍵）。

**奈也觀點**：vault key 是「這是哪個 repo／哪個世界觀文件夾」，space 是「這東西擺在哪一格抽屜」，
两者本來就是不同維度，硬綁在一起會讓之後想把一個 vault 搬到別的抽屜變得很痛苦。

**奈留觀點**：更關鍵的是相依性。`notes`、`episodes`、`concepts`、`vault_aliases`、
`import_sources`、`import_vault_counts`、`note_tombstones` 全部用 `vault`／`key` 當外鍵或邏輯鍵，
沒有一處帶 space。改成複合鍵等於要動全部這些表與所有查詢的 join 條件，而且 `vault_aliases`
的別名解析（A7）、`import_on` 的 mapping、`admin.py` 的 CASCADE 刪除全部要重寫。
用單一欄位 + `UNIQUE(key)`（沿用現行 `PRIMARY KEY(key)`）沒有任何一段既有程式需要改動 schema
以外的東西，只有「哪些 vault 可見」這一個判斷點要插入 space 條件，而這個判斷點本來就已經
集中在 `storage/vaults.py` 的 `resolve_read` / `resolve_write` / `vault_clause`（T-16 建立的
唯一出口）。

**替代方案**：
- 複合鍵 `(space, key)`：允許同一個 key 字面值在不同 space 各自存在（例如 `lore` 也有一個
  `global`）。代價是所有下游表都要多帶一欄 `space` 才能正確 join，且外鍵/CASCADE 全部要重寫；
  對現有 6 個 migration 版本的資料是破壞性遷移。否決。
- space 當獨立表、vault 屬於一個 space_id：多一層間接，且 vault 搬 space 變成改外鍵而非改欄位，
  複雜度不對稱地高於好處（只有三個固定值，不需要一張表描述）。否決，但若之後 space 需要更多
  中繼資料（例如每個 space 各自的顯示名稱、建立時間），可以再引入一張 `spaces` 描述表，
  `vaults.space` 仍是外鍵，不影響本決策。

**後果**：
- `vault key` 在任何 space 都不可重複（見 1.3）。這是本提案對「vault key 在不同 space 是否可重複」
  這個問題的直接回答：**不可重複，全域唯一**，沿用現行 `PRIMARY KEY(key)` 不變。
- vault 要換 space 是改一個欄位值（管理操作，見風險清單 R-3），不是搬資料。

### 1.2 schema 變更（v7）

```sql
-- v7 migration（沿用既有 ALTER TABLE ADD COLUMN 慣例，見 migrate.py MIGRATIONS[5:6] 的先例）
ALTER TABLE vaults ADD COLUMN space TEXT NOT NULL DEFAULT 'dev';
```

- 不加 `CHECK (space IN (...))`：STRICT 表 + `CHECK` 在 SQLite 的 `ALTER TABLE ADD COLUMN` 上
  受限（無法對既有資料回填檢查、且新增 CHECK 約束需要重建表）。合法值集合 `SPACES = frozenset(
  {"dev", "lore", "personal"})` 放在 `lore_vault.schema.models`（比照 `VAULT_KINDS` 的做法），
  由 `Vault.__post_init__` 與 `storage/vaults.py` 的寫入路徑驗證。新增 doctor 對帳項
  `space.valid_values`：掃 `vaults.space` 有值不在 `SPACES` 內即 fail（防止手動改 DB 或未來
  新增 space 值時忘記同步程式碼白名單）。
- 既有資料遷移即預設值 `'dev'`，與 A18「現有 vault 全歸 dev」一致，不需要額外資料回填腳本。
- `Vault` dataclass（`schema/models.py`）新增欄位 `space: str = "dev"`，`REQUIRED` 不含它
  （沿用預設值語意，向後相容舊呼叫端建構 `Vault(...)` 不用改）。

### 1.3 `global` vault 在各 space 的定位

**決策**：每個 space 至多一個「跨 vault 觀察」用途的 global vault，key 依 space 命名，
**不是同一個 key 共用**：

| space | global vault key | 建立方式 |
|---|---|---|
| `dev` | `global`（沿用現行，A17 管線既有邏輯不動） | 既有：管線寫回 scope=null 的 concept 時自動建立（`origin='pipeline'`） |
| `lore` | `lore/global` | 不自動建；需要時由使用者／艾斯維爾透過 `vault_resolve(space="lore", key="lore/global", create=True, display=...)` 手動建 |
| `personal` | `personal/global` | 同上，手動建 |

**理由（奈留）**：因為 1.1 定案 key 全域唯一，`global` 這個字面值只能屬於一個 space（已被 dev 佔用），
其餘 space 若也想要一個「不屬於特定文件夾的雜項」容器，必須用不同的 key。用 `<space>/global`
延續現有 `folder/<name>`、`github.com/<owner>/<repo>` 的「路徑感」命名慣例，一眼看出屬於哪個
space，也避免與非 dev 的 vault key 前綴（見 1.4）混淆。

**理由（奈也）**：但我覺得沒必要自動幫 lore／personal 建 global——這兩個 space 的用途還很模糊，
搞不好使用者根本不需要一個「雜項抽屜」，先不建，需要的時候艾斯維爾自己開一個就好。

### 1.4 非 dev space 的 vault 命名與建立

`dev` 的 vault 由 `binding`（git remote → key）自動算出；`lore`／`personal` 沒有 repo，
**不透過 binding**。

**決策**：非 dev space 的 vault key 一律要求前綴 `<space>/`（例如 `lore/aeswir-arc`、
`personal/journal`），由 `vault_resolve` 的伺服端驗證強制（不合前綴直接拒絕，`400
space_key_prefix_required`），避免與 `folder/*`、`github.com/*`（dev 專用命名空間）撞名，
也讓「這個 vault 屬於哪個 space」在 key 字面上就可讀，即使脫離資料庫也能辨識。

建立方式：`vault_resolve(space, key, create=True, display?)`——**不提供 cwd 推算**（非 dev
沒有 repo root 概念），呼叫端（agent 或使用者）必須明確給 key 與 display。已存在時回傳既有
vault（等冪，比照現行 `vault_resolve(create=True)` 對 dev 的行為）。

新增 doctor 項 `space.key_prefix_agreement`：掃 `space != 'dev'` 的 vault，key 不是
`f"{space}/"` 開頭者 fail（防止手動改 DB 造出前綴不符的資料，或之後管理操作改了 space
卻忘記改 key）。

### 1.5 遷移方式（v7）小結

1. `ALTER TABLE vaults ADD COLUMN space TEXT NOT NULL DEFAULT 'dev'`（單一 migration 項）。
2. `schema.models.Vault` 加欄位、`SPACES` 常數、驗證。
3. `storage/vaults.py` 的 `_lookup`／`resolve_read`／`resolve_write`／`upsert_vault`／`ensure_vault`
   全部加 `space` 參數（見第 2 節契約），查詢條件從「key 存在即可」改成「key 存在且 space 相符」。
4. 快照白名單（`storage/snapshot.py`）的 `vaults` 表整表複製，`space` 欄位隨之免費納入
   （不需要改快照白名單本身，但 MCP 殼套用快照時要按目前 space 過濾，見 2.5）。
5. doctor 新增 `space.valid_values`、`space.key_prefix_agreement`（第 8 節任務卡對應）。

---

## 2. 「目前 space」語意

### 2.1 ADR-02：狀態放殼端，服務端無狀態、space 為必填參數

**決策**：MCP 殼（每個殼行程）持有「目前 space」，**不持久化**，新 session 一律回預設 `dev`；
服務端每個 `/v1/*` 請求都收到明確的 `space` 欄位（由殼注入），服務端**不設預設值**，
缺欄位直接 400（比照 A5 對 `vault` 的態度）。

**理由（奈留）**：A5 的教訓是「範圍靠參數自覺傳」會被繞過或忘記傳，所以 vault 在服務端是必填、
無預設。如果 space 在服務端有預設值（例如「沒帶 space 就當 dev」），那 A18「以 MCP 工具切換
目前 space、其他工具只回傳目前 space 內容」這句話在服務端會失效——任何直接打 HTTP（略過殼）
的呼叫端，只要忘記帶 space，就會安靜地看到 dev 內容而不自知，這正是 A5 想避免的靜默失效。
**服務端必須把「沒帶 space」當成錯誤，不是當成 dev。** 殼端才是「預設 dev」這句話生效的地方
——殼的初始狀態是 `dev`，殼把這個狀態放進它發的每個請求裡。

**理由（奈也）**：從 agent 的角度看，這樣切一次就好、之後每個工具呼叫都不用重複講「我要哪個
space」，體驗跟現在 vault 綁在殼的 cwd 很像，不會覺得多了一個要記的東西。

**替代方案**：
- 服務端 session／token 記憶目前 space：需要引入 session 概念（目前系統沒有登入態，bearer
  token 是共用密鑰不是使用者身分），也違反 A15「服務端無狀態、狀態在殼」的既有分工。否決。
- 服務端對缺 `space` 的請求預設 `dev`：如上所述，違反 A5 精神，且會讓「MCP 切換 space」這個
  設計看起來有效、實際上任何繞過殼的呼叫都感覺不到差異，屬於頭號敵人「靜默出錯」。否決。

**後果**：每個 `/v1/*` 端點的 request body 新增必填欄位 `space`；`/v1/vault_resolve` 的
`create=True` 建 vault 時也需要 `space`（見 1.4）。既有 HTTP 客戶端（若有，目前只有 MCP 殼）
全部要補這個欄位——因為服務尚未上線、目前只有殼一種客戶端，這是新增而非破壞性變更。

### 2.2 MCP 工具：新增 `space`

```
space(action: "get" | "set", value?: "dev" | "lore" | "personal") -> {space}
```

- `action="get"`：回傳殼目前持有的 space，不打服務端（純殼端狀態）。
- `action="set"`：驗證 `value` 在 `SPACES` 內，更新殼的記憶體狀態，回傳新值。**不打服務端**
  （space 合不合法是靜態集合，不需要查詢；vault 是否存在則留給個別工具在該 space 下驗證）。
- 每個殼行程各自一份狀態（`Shell.__init__` 新增 `self.space: str = "dev"`，比照現有
  `self._cwd`／`self._now` 這類殼端狀態），**不持久化到磁碟**——與艾斯維爾原提案「新 session
  回預設 dev」一致，也避免「殼重啟後停在使用者忘記自己切過的 space」這種隱性狀態。

其餘工具（`recall`／`get`／`list`／`write`／`update`／`status`）**不新增 space 參數**，改由殼
在轉發給服務時自動帶入 `self.space`，維持 A18「其他工具只回傳目前 space 的內容」——對 agent
而言，工具介面完全不變，只是行為受目前 space 影響，符合「工具數量刻意壓在個位數」的原則。

`vault_resolve` 例外：非 dev space 建立 vault 時需要指定 key（見 1.4），所以 `vault_resolve`
簽名新增可選參數 `space?`（省略時用殼目前 space；顯式傳入可以在不切換目前 space 的情況下
在其他 space 建 vault，例如使用者臨時要幫 lore 建一本，用完仍留在 dev 工作）。

### 2.3 `vault="*"` 在 space 範圍內的意義

**決策**：`vault="*"`（跨 vault 明示查詢，`storage/vaults.py` 的 `ALL_VAULTS`）語意變成
「目前 space 內的所有 vault」，而不是全資料庫所有 vault。`vault_clause` 對 `is_all` 的情況
從 `"1 = 1"` 改成 `f"{column} IN (SELECT key FROM vaults WHERE space = ?)"`，參數是殼傳入的
`space`。

**理由**：這是把 A5（vault 硬範圍）與 A18（space 硬範圍）疊加的必然結果——兩層範圍是 AND
關係：`vault="*"` 只解除 vault 這一層，space 那一層仍然生效，除非明確切換 space。這也回答了
「跨 space 查詢是否允許一次查多個」：**不允許**，`space` 只接受單一值，沒有 `"*"` 的等價物；
艾斯維爾原話「需要時可讀其他 space，但方式是切換」直接對應到「一次只能有一個目前 space，
要看別的就切換」，不開一次查多個 space 的模式，避免跨 space 結果混在同一份回應裡造成
「我以為都是 dev 的內容」的誤判——這正是現行 PM 的原始問題（`notebook_id` 過濾無效導致結果
混雜其他專案）在 space 維度上的重演。

**後果**：`storage/vaults.py` 的 `VaultScope` 新增欄位 `space: str`（連讀取跨 vault 時也要
記得是哪個 space 的跨 vault，供 `vault_clause` 使用）；`resolve_read`／`resolve_write` 簽名
新增必填 `space` 參數，`_lookup` 的查詢條件加上 `AND space = ?`——**未帶 space 或 key 存在
但屬於別的 space，一律回 `UnknownVault`**，不透露「這個 key 其實存在，只是在別的 space」，
避免跨 space 的存在性洩漏（等同 A5 對錯誤 vault 的態度：不回空結果、不自動建立、也不暗示）。

### 2.4 `vault_resolve` 在非 dev space 的運作

`vault_resolve(cwd?, create?, display?, space?)`：

- `space` 省略時用殼目前 space。
- `space="dev"`（或省略且殼目前為 dev）：行為不變，`cwd` 經 `lore_vault.binding` 算 key。
- `space in ("lore", "personal")`：**不接受 `cwd`**（傳了也忽略並在回應中註記
  `cwd_ignored: true`，不是報錯——避免 agent 忘記自己在非 dev space 時因為殼預設帶了 cwd
  而困惑地拿到錯誤）。必須帶 `key`（新參數，dev 也可選用來覆蓋 binding 推算，見下）與
  `display`；`create=True` 時依 1.4 的前綴規則建立。

同時，`vault_resolve` 新增可選參數 `key?`，讓 dev 之外的呼叫端可以直接指定 vault key
（不透過 cwd），這是本次順帶補的介面缺口——沒有它，非 dev space 就沒有任何方式指定要解析
哪個 vault。

### 2.5 快照（降級）與 concept 注入 hook 跟 space 的關係

**決策**：hook（`PreToolUse` concept 注入、`Stop` episode 收料）與 spike 管線**只屬於
`dev`**，不感知 space。

**理由**：這三條路徑的設計前提是「一個 repo 對應一個 vault」（binding 邏輯、`repo_root`
凍結欄位、A17 的 scope 比對），這個前提只在 dev space 成立——`lore`／`personal` 的 vault
沒有 repo，不會有 coding agent 在裡面編輯觸發 hook。具體：

| 路徑 | 與 space 的關係 |
|---|---|
| `POST /v1/episodes`（Stop hook 收料，自動建 vault） | 固定 `space="dev"`，不接受呼叫端指定；episode 本來就是「發生在某個 repo 的對話」，沒有非 dev 的語意 |
| A17 管線寫回 concept 的 scope 比對（步驟 B：比對 vault 顯示名／別名） | 只在 `space='dev'` 的 vault 中比對，避免 lore／personal 內剛好同名的 vault 被誤認命中 |
| `import_on`（Open Notebook 匯入） | 匯入結果固定進 `dev`（現有 15 本 notebook 全部是開發記憶），A18 已定案「現有 vault 全歸 dev」與此一致 |
| MCP 殼的 notes 快照（`GET /v1/snapshot`） | **快照本身不分 space**：白名單複製整張 `vaults` 表（含 space 欄位，見 1.5 第 4 點），但降級模式下的 `recall`／`get`／`list`／`vault_resolve` 查詢快照時，**仍要用殼目前的 space 過濾**——降級不代表放棄範圍保護，否則會出現「服務正常時看不到 lore 的東西、服務掛了反而看得到」的倒退。降級路徑沿用服務層函式（`ARCHITECTURE.md` 既有原則），只是資料來源換成 SQLite 唯讀快照檔，space 過濾邏輯不變 |
| concept 快照（`concepts.json`，PreToolUse 讀） | 不變，維持現行「不分 vault／space、由客戶端 scorer 判斷 scope」的設計——這條路徑本來就只服務 dev 的注入場景 |

**奈也**：也就是說使用者在 lore space 底下做世界觀筆記，不會被 Stop hook 誤記成一筆
episode，這樣才乾淨。

**奈留**：反過來也要注意——如果之後真的想在 lore/personal 也跑 agent 記憶（比如「艾斯維爾在
個人筆記空間也用 coding agent」），那是另一張需要重新設計 binding 的卡，不是本次 A18/D10
範圍內能悄悄長出來的功能，屆時要回頭改這裡「固定 dev」的假設。

---

## 3. 文件模型

### 3.1 ADR-03：document + chunk 兩層，chunk 有獨立索引表

**背景**：D1 實測（T-02）發現舊系統把長內容 mean-pool 成一個向量、從未落地成多筆 chunk，
是要改善的對象。新系統要能回「這段話出現在第幾頁／第幾張投影片」，所以 chunk 必須是
一等公民，有自己的 FTS 與向量索引，**不能沿用 note 的 `note_embeddings`／FTS 觸發器**
（那兩者是以 `note_seq` 為外鍵，見 `storage/vectors.py`／`storage/fts.py`）。

**決策**：新增三張表：`documents`、`document_chunks`、`document_chunk_embeddings`，
外加一張 FTS5 虛表 `chunk_fts`（比照 `note_fts` 的建法，同樣走 CJK bigram 展開）。

```sql
CREATE TABLE documents (
    id          TEXT PRIMARY KEY,       -- 'doc:' + uuid4
    vault       TEXT NOT NULL REFERENCES vaults(key),
    filename    TEXT NOT NULL,          -- 上傳時的原始檔名（顯示用）
    mime        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    sha256      TEXT NOT NULL,          -- 原始檔內容雜湊，去重鍵
    version     INTEGER NOT NULL DEFAULT 1,
    supersedes  TEXT REFERENCES documents(id),  -- 更新文件：新版本取代舊版本（比照 Note.supersedes）
    status      TEXT NOT NULL CHECK (status IN ('pending','extracting','ready','failed')),
    error_code  TEXT,                   -- 失敗原因分類，見 4.4
    error_detail TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,  -- 抽取完成後填，供對帳比對實際 chunk 列數
    created     TEXT NOT NULL,
    updated     TEXT NOT NULL
) STRICT;

CREATE TABLE document_chunks (
    seq         INTEGER PRIMARY KEY,    -- 內部序號，FTS／向量表外鍵用（比照 notes.seq 的模式）
    document_id TEXT NOT NULL REFERENCES documents(id),
    idx         INTEGER NOT NULL,       -- 文件內順序，從 0 起算
    text        TEXT NOT NULL,
    locator     TEXT NOT NULL,          -- JSON：{"kind": "page"|"slide"|"heading", "value": ...}
    UNIQUE (document_id, idx)
) STRICT;

CREATE TABLE document_chunk_embeddings (
    chunk_seq   INTEGER PRIMARY KEY REFERENCES document_chunks(seq),
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    model       TEXT,
    updated     TEXT NOT NULL
) STRICT;

-- chunk_fts：比照 note_fts 的 CJK bigram 展開內容欄，seq 對回 document_chunks.seq
CREATE VIRTUAL TABLE chunk_fts USING fts5(
    content, tokenize = "unicode61 tokenchars '_'", content=''
);
```

- **原始檔存放**：docker named volume 下 `/data/blobs/<sha256前2碼>/<sha256>`，內容定址、
  跨 vault／space 共用同一份 blob（去重全域生效，不分 vault）；`documents` 表每個 vault
  各自一列 metadata，指向同一個 sha256 時 blob 只存一份。
- **去重**：同一 vault 內上傳同雜湊 → 回傳既有 `document` id，不建新列、不重抽（等冪，
  比照 `POST /v1/episodes` 的 duplicate 語意）；不同 vault 上傳同雜湊 → blob 共用，
  但各自建一列 `documents`（因為 vault 是硬範圍，metadata 不能跨 vault 共用一列）。
- **版本**：更新文件＝新 `documents` 列，`supersedes` 指向舊列（不是原地覆寫，比照
  Note 的 `supersedes`——「更正關係凍結成欄位，不是另建更正篇」的精神在這裡是「新版本
  獨立一列，舊版本仍查得到，但檢索預設只回最新版」）。新版本進來後，舊版本從 `chunk_fts`
  與向量索引移除（見對帳項），blob 保留到有明確的 GC 操作（見 6.3、風險 R-5）。

### 3.2 刪除與墓碑

沿用 `note_tombstones` 的做法（`docs/DEVELOPMENT.md` 管理用刪除一節）：新增
`document_tombstones(id, document_id, vault, sha256, deleted_at, reason)`，刻意無外鍵。
`cli.admin` 新增 `delete-document --vault KEY --id DOC_ID`：單一交易內刪 `document_chunks`、
`chunk_fts` 對應列、`document_chunk_embeddings`、寫入墓碑；blob 本身**不刪**（可能被其他
vault 的 document 引用，或被同 vault 的舊版本引用），孤兒 blob 由對帳項回報、由另一個
明確的 GC 指令處理（不是刪 document 的副作用，避免「以為只刪一個 document 結果誤刪
共用檔案」）。

### 3.3 對帳項清單（doctor，分類 `documents`）

| 檢查 | fail 條件 |
|---|---|
| `documents.blob_exists` | `documents.status='ready'` 但 `/data/blobs/<sha256前2碼>/<sha256>` 不存在 |
| `documents.orphan_blobs` | blob 目錄下的檔案，其 sha256 不被任何非墓碑 `documents` 列引用 |
| `documents.chunk_count_matches` | `documents.chunk_count` ≠ 實際 `document_chunks` 列數（`status='ready'` 才檢查；`pending`/`extracting` 跳過並標 skipped） |
| `documents.fts_rows_match_chunks` | `chunk_fts` 列數 ≠ `document_chunks` 列數 |
| `documents.vector_rows_match_chunks` | `document_chunk_embeddings` 列數 ≠ `document_chunks` 列數（允許暫時性落後，門檻同 note 的補算延遲，見 T-19 前例） |
| `documents.stuck_processing` | `status IN ('pending','extracting')` 且 `updated` 超過門檻（預設 1 小時，比照 spool 的 warn/fail 兩階）仍未轉換 |
| `documents.superseded_chunks_removed` | `supersedes` 指向的舊 document，其 chunk 仍出現在 `chunk_fts`／向量表中（版本切換後索引未清乾淨） |

---

## 4. 抽文字與切段

### 4.1 ADR-04：抽取套件選型

**決策**（純 Python、授權寬鬆、免編譯或有 Windows/Linux wheel、體積小）：

| 格式 | 套件 | 授權 | 備註 |
|---|---|---|---|
| `.md`、`.txt` | 內建（讀檔即文字） | — | 不需要額外套件；`.md` 依標題（`#`）切段，見 4.2 |
| `.json`、`.yaml`、`.toml` | `json`／`tomllib`（stdlib）、`PyYAML` | stdlib／MIT | 結構化資料轉成可讀文字（鍵路徑 + 值）後索引，不是把原始檔當純文字塞給 FTS——否則巢狀結構的語意會消失 |
| `.pdf` | `pypdf` | BSD-3 | 純 Python、無編譯依賴、體積小（優於 `PyMuPDF`／`pdfplumber` 的 C 擴充體積與授權，`PyMuPDF` 為 AGPL 不適用於可能商業化的專案）；加密 PDF 明確處理見 4.4 |
| `.docx` | `python-docx` | MIT | 純 Python |
| `.pptx` | `python-pptx` | MIT | 純 Python |
| 圖片 OCR | 不做（D10 已排除） | — | — |

**風險標記（未驗證，建議列為 T-52 系任務前的 spike）**：`pypdf` 對中文字型（尤其掃描轉檔或
自訂編碼字型）的文字抽取品質未實測，可能出現亂碼或抽不出文字（此時視同抽取失敗，走 4.4
的失敗路徑，不會是靜默空字串——但需要先寫一支拋棄式腳本，拿實際會遇到的 PDF 樣本測過，
才能確定「pypdf 抽不出來」與「PDF 本身是掃描圖片、本來就沒有文字層」如何區分並各自標成
哪種 `error_code`）。

### 4.2 切段策略

**優先依結構**，找不到結構才退回定長：

| 格式 | 結構單位 | locator |
|---|---|---|
| `.md` | 標題（`#`~`######`）分段；同一標題下內容過長再依 4.3 定長切 | `{"kind": "heading", "value": "標題路徑，如 '設定 > 資料庫'"}` |
| `.pdf` | 頁 | `{"kind": "page", "value": 3}` |
| `.pptx` | 投影片 | `{"kind": "slide", "value": 5}` |
| `.docx` | 標題樣式（Heading 1/2/3）分段；無標題則整篇當一段再定長切 | `{"kind": "heading", "value": "..."}`，無標題退回 `{"kind": "offset", "value": 0}` |
| `.txt`、`.json`、`.yaml`、`.toml` | 無天然結構，直接定長切 | `{"kind": "offset", "value": 起始字元位置}` |

**定長切段參數**：長度上限與重疊需考量 bge-m3（`docs/DECISIONS.md` D7）——D1 實測記錄
「超過 400 token 的內容在記憶體分塊後 mean-pool」是舊系統的行為，新系統改成**每個 chunk
各自一個向量、不 mean-pool**，chunk 長度本身就要落在 embedding 效果好的範圍內：

- 上限：**每 chunk 約 400 token**（沿用舊系統觀察到的 400 token 分塊點作為經驗值，非
  bge-m3 官方硬限制——bge-m3 支援到 8192 token，但 embedding 品質隨長度增加而語意變稀釋，
  400 token 是舊系統實務上已驗證「不會太稀釋」的量級）。中文以「每字約 1.5–2 token」的
  粗估換算成字數上限（約 200–260 中文字／chunk），實際 token 數由呼叫 embedding 前的
  tokenizer 結果決定，不是估算值本身當硬限制。
- 重疊：約 10–15%（40–60 token），避免切點正好落在關鍵句子中間導致前後 chunk 都語意不完整。
- 結構單位本身超過上限時（例如一頁 PDF 塞了很長的文字）才在結構內再定長切，locator 附加
  `part` 資訊（如 `{"kind": "page", "value": 3, "part": 2}`）。
- 結構單位小於某個下限（例如空白投影片、只有標題沒有內文的段落）時不產生 chunk，避免
  索引大量空洞條目。

**中文處理**：切段本身按「字元數」而非「詞數」估算（中文沒有天然空白斷詞），與 FTS5
既有的 CJK bigram 展開（`storage/fts.py`）獨立——chunk 的切分只決定「這段文字要不要跟
下一段分開」，索引時仍走現有的 `tokens()` 展開邏輯，不需要額外做中文斷詞。

### 4.3 抽取失敗與加密 PDF

**決策**：`documents.status` 是明確狀態機，**任何抽取失敗都落地成 `status='failed'` +
`error_code`，不是空 chunk 或靜默略過**（呼應「靜默出錯是頭號敵人」）：

| `error_code` | 觸發情境 |
|---|---|
| `encrypted` | PDF 需要密碼（`pypdf` 的 `PdfReader.is_encrypted`） |
| `unsupported_format` | MIME／副檔名不在支援清單，或副檔名與實際內容不符（例如副檔名 `.docx` 但內容不是合法 zip） |
| `corrupt` | 套件解析拋例外（格式本身損毀） |
| `empty_extraction` | 解析成功但抽出 0 個非空 chunk（例如純圖片 PDF，沒有文字層）——這種情況**不是成功、也不是技術上的例外**，是「這份文件目前無法被檢索」，需要使用者知道而不是以為已經可搜尋 |
| `too_large` | 超過大小上限（見 4.5），在抽取前就擋下，不進佇列 |

失敗有上限重試（比照 D4 摘要補算的「有上限的重試，超過標記失敗、不無限重試」），
`status='failed'` 後不自動重試，需要重新上傳或明確的重試操作（管理指令，不開 MCP 工具）
才會再試一次。

### 4.4 非同步或同步

**決策**：**非同步**，沿用 enrich worker 模式（`storage/enrichment.py` + `enrich/worker.py`
的候選/嘗試/退避框架）。

**理由**：抽取 PDF／pptx 可能耗時數秒到數十秒（視頁數），若同步做會讓 `upload` 這個 HTTP
請求／MCP 工具呼叫的延遲不可預期，且違反現有「write 不等 LLM／embedding」（A14/D4）建立的
「落地快、補算慢」節奏。上傳完立即回 `document` id 與 `status='pending'`，抽取、切段、
embedding 三步都在背景 worker 完成，`status` 依序 `pending → extracting → ready`（或
`failed`）。

worker 佇列沿用 `storage/enrichment.py` 的「佇列是推導的」精神：待抽取＝
`documents.status = 'pending'`；待補 embedding＝`document_chunks` 存在但
`document_chunk_embeddings` 缺列，與 `note_enrichment` 平行的
`document_enrichment(document_id, kind, attempts, next_attempt, last_error)` 記錄嘗試
（`kind` 只有 `'extract'` 一種，embedding 沿用 chunk 層級的推導判斷，比照 note 的做法）。

### 4.5 大小上限建議

- 原始檔：**25 MB**（涵蓋絕大多數簡報／文件；世界觀文件、設定檔不太可能超過，若真的超過
  多半是夾帶了圖片或影片素材，不是本次要索引的內容）。超過在 `upload` 當下即拒絕
  （`413` / MCP 工具錯誤），不進佇列、不佔位。
- 抽出的文字總長：另設上限（建議 2,000,000 字元，約對應數百頁純文字），避免極端情況
  （例如一份 PDF 被誤判為文字層而其實是重複雜訊）耗盡 chunk 數量；超過時標記
  `error_code=too_large` 於抽取階段而非上傳階段（因為原始檔大小合規，但抽出結果異常大）。

以上兩個數字是**建議值，非既有實測**，需要艾斯維爾確認是否符合實際會用到的檔案規模
（見第 7 節需裁決清單 D10-3）。

---

## 5. 檢索整合

### 5.1 recall 同時回 note 與 document chunk

**決策**：`recall` 的 `kinds` 參數新增第三個值 `"chunk"`（`KNOWN_KINDS` 與
`SUPPORTED_KINDS` 同時擴充，比照現有 note／concept 的區分——`concept` 目前是「已知但未實作」，
`chunk` 實作後直接進 `SUPPORTED_KINDS`）。`kinds=None`（預設）**包含 note 與 chunk**，
不含 concept（維持現行預設語意，只是「note」這個預設集合擴大為「note + chunk」，因為兩者
都是「使用者主動要找的既有內容」，concept 是背景注入用途、語意不同、繼續排除在預設外）。

### 5.2 RRF 融合方式

**決策**：**note 與 chunk 各自跑一輪 lexical + vector，四路（note-lexical、note-vector、
chunk-lexical、chunk-vector）一起丟進同一個 `rrf_fuse`**，而不是先分別融合 note 和 chunk
再合併兩個已融合的排名。

**理由**：`rrf.py` 的融合公式只看排名不看原始分數，天然支援「多路」而不限定兩路；把
note 和 chunk 的四路一次融合，可以讓「這個 chunk 排名很前面」和「這個 note 排名很前面」
在同一個 RRF 分數尺度上比較，避免「先各自融合再合併」時引入的二次加權（那需要決定
note 融合結果與 chunk 融合結果之間的相對權重，是新的、缺乏依據的調參點；一次融合則沿用
`rrf.py` 現有「不需要跨路權重」的設計理由）。

`RecallItem` 新增可選欄位（`kind="chunk"` 才有值）：

```python
@dataclass(frozen=True)
class RecallItem:
    id: str  # note id 或 chunk 的 document_id（見下）
    kind: str  # "note" | "chunk"
    vault: str
    title: str  # note 用 Note.title；chunk 用 documents.filename
    summary: (
        str | None
    )  # note 用既有摘要；chunk 用該段文字的前 N 字（比照 summary_source="lead"）
    summary_source: str
    score: float
    updated: str
    # 以下僅 kind="chunk" 有值
    document_id: str | None = None
    chunk_id: str | None = None  # document_chunks.seq 轉成外部可用的穩定字串 id
    locator: dict | None = None  # 4.2 的 locator 結構
```

`id` 欄位對 chunk 而言用 `chunk_id`（而非 `document_id`），因為 `get` 要能精確取到「這一段」
而不是整份文件（見 5.3）；`document_id` 另外帶出，讓 agent 知道「這幾個 chunk 其實來自
同一份文件」以便判斷要不要改用 `get` 取整份。

「文件是否需要 LLM 摘要」：**建議不做（v1）**。理由：note 的摘要是對一則通常不太長的
結論做 1–2 句濃縮，成本與長度成正比且可預期；文件可能是數十頁的簡報，逐份做 LLM 摘要的
成本與延遲隨頁數線性成長、難以預期，且 chunk 層級已經有「前 N 字」可以當初步線索
（比照 note 缺摘要時的 `summary_source="lead"` 退路），第一版先不做，需要時再加
（見第 7 節 D10-5）。

### 5.3 `get` 與 `list`

- `get(vault, ids, budget?)`：`ids` 可混合 note id 與 chunk id（兩者 id 前綴不同，
  例如 note 用 `note:xxx`、chunk 用 `chunk:xxx`，服務端依前綴分派）。取 chunk id 回傳
  該段落全文；若要整份文件，`ids` 傳 `document_id`（`doc:xxx` 前綴）回傳全部 chunk 依
  `idx` 排序串接（大文件仍受 `budget` 截斷，截斷方式比照現有 recall 的裁切邏輯：優先保留
  前面的 chunk，並標記 `truncated`）。
- `list(vault, since?, topics?, cursor?)`：新增 `kinds?` 篩選（預設同時列 note 與
  document），document 一列顯示 `filename`、`status`、`chunk_count`、`updated`；
  `topics` 篩選對 document 暫不適用（document 目前沒有 topics 欄位，之後若要加標籤是
  獨立的小卡，不擋本次）。

---

## 6. MCP／HTTP 介面

### 6.1 新增／擴充的工具（維持個位數原則）

| 工具 | 型態 | 說明 |
|---|---|---|
| `space(action, value?)` | 新增 | 2.2 節 |
| `upload(path, vault, filename?, mime?)` | 新增 | 6.2 節 |
| `recall(query, vault, kinds?, ...)` | 擴充 | `kinds` 多一個 `"chunk"`（5.1） |
| `get(vault, ids, budget?)` | 擴充 | `ids` 接受 chunk／document id（5.3） |
| `list(vault, since?, topics?, cursor?, kinds?)` | 擴充 | 新增 `kinds?`（5.3） |
| `vault_resolve(cwd?, create?, display?, space?, key?)` | 擴充 | 2.4 節 |

共新增 2 個工具（`space`、`upload`），其餘 4 個既有工具擴充參數，符合「工具數量刻意壓在
個位數」——目前 7 個工具＋2 個＝9 個，仍是個位數。

### 6.2 `upload`：殼讀本機路徑，HTTP 走 multipart

```
upload(path, vault, filename?, mime?) -> {document_id, status, sha256, duplicate: bool}
```

- **殼負責讀取本機檔案**（MCP 工具參數是本機路徑，不是內容），比照 `vault_resolve` 的
  `cwd` 是殼端概念、不是服務端概念。殼把檔案內容以 **multipart/form-data** 轉送給服務端
  `POST /v1/documents`（不是 base64-in-JSON：文件可能到 25MB，base64 膨脹 33% 且 JSON
  parse 整包載入記憶體，multipart 可以串流）。
- **路徑安全**：殼只能讀取設定檔白名單內的目錄（新設定項 `mcp.upload_roots`，格式同
  `mcp.snapshot_dir` 這類路徑設定；未設定＝退回殼的目前工作目錄，即啟動殼時 Claude Code
  給的專案目錄）。要求絕對路徑且必須落在某個 `upload_roots` 之下（正規化後比對前綴，
  防止 `..` 跳出），否則工具回錯誤 `path_not_allowed`——這是本次唯一會讓 agent 讀取殼所在
  機器任意檔案的介面，安全邊界必須在殼端做，不能只靠「agent 應該不會亂傳路徑」。
- **大小與串流**：服務端 `/v1/documents` 依 `Content-Length` 先擋超過 4.5 節上限的請求
  （提早拒絕，不讀完整個 body 才發現太大）；FastAPI 的 multipart 讀取本身是串流處理，
  不需要額外處理。
- **重複上傳（同雜湊）**：計算 sha256 後在該 vault 內查已有 document——存在則回既有
  `document_id`、`duplicate: true`、**不重新排隊抽取**（比照 episode 的 duplicate 語意：
  同鍵同內容視為成功，不當錯誤也不當新工作）。
- **更新文件（新版本）**：`upload` 若帶 `supersedes?: document_id` 參數，即使雜湊不同
  也視為新版本（見 3.1）；不帶則單純以「同 vault 同雜湊」判斷是否重複，雜湊不同、未指定
  `supersedes` 時視為全新獨立文件（即使檔名相同）——**不用檔名判斷是否為更新**，檔名容易
  重複或誤判，版本關係必須由呼叫端明確表達。

### 6.3 HTTP 端點

```
POST /v1/documents           multipart: file, vault, space, filename?, mime?, supersedes?
                              -> {document_id, status, sha256, duplicate, vault, space}
GET  /v1/documents/{id}      ?vault=&space=  -> metadata + chunk 清單摘要（不含全文，比照 A4）
```

`recall`／`get`／`list`／`vault_resolve` 現有端點依 5、6.1 節擴充 body 欄位，不新增端點
（維持「端點與 MCP 工具一對一」的既有慣例）。

---

## 7. 需艾斯維爾裁決的選項

| # | 決策點 | 建議 | 影響 |
|---|---|---|---|
| **D-space-1** | `space` 是否需要 `CHECK` 約束或僅靠程式碼白名單 | 僅程式碼白名單 + doctor 對帳（1.2），因 STRICT 表對 `ALTER TABLE ADD COLUMN` 加 CHECK 的限制 | 影響 v7 migration 寫法 |
| **D-space-2** | `lore`／`personal` 是否要自動建立各自的 `global` vault | 不自動建，需要時手動（1.3） | 影響 v7 是否附帶資料寫入（建議不附） |
| **D-space-3** | vault 換 space 是否開放（例如某本一開始歸錯 space） | 開放，但只走 `cli.admin`（管理指令），不開 MCP 工具；換 space 後舊 space 的快照／降級查詢立即看不到它（下次快照更新後），需要在操作說明中提醒 | 影響 6.1 是否要多一個管理子指令（見任務卡 T-59） |
| **D10-1** | 支援格式優先序：是否第一版只做 `md/txt/pdf`，`docx/pptx/json/yaml/toml` 延後 | 建議全部一起做——抽取器彼此獨立（各自一個函式），拆批對整體風險降低有限，但會拉長第一次可用的時間；若要縮小首批範圍，建議留 `md/txt/json/yaml/toml/pdf`，`docx/pptx` 延後（世界觀文件更可能是 md／pdf，docx/pptx 的解析器複雜度較高） | 影響任務卡拆分（見 T-56、T-57 可獨立排序） |
| **D10-2** | chunk 長度上限（400 token 經驗值）與重疊比例是否需要先做一輪實測校準 | 建議先用經驗值上線，累積實際檢索效果後再調（比照向量不建 ANN 索引「10 萬條以上再重評」的務實態度） | 影響 4.2 是否需要前置 spike 卡 |
| **D10-3** | 大小上限（25MB／2M 字元）是否符合實際會用到的檔案規模 | 待艾斯維爾確認世界觀文件、簡報的實際大小分布 | 影響 4.5、`upload` 的拒絕門檻 |
| **D10-4** | `pypdf` 中文抽取品質是否需要先跑 spike 驗證，或直接上線靠對帳／使用回報抓問題 | 建議先跑一次性 spike（找幾份中文 PDF 樣本實測），成本低、能避免上線後才發現一整類文件抽出來是亂碼 | 對應任務卡 T-52（前置 spike） |
| **D10-5** | 文件是否需要 LLM 摘要 | 建議 v1 不做（5.2） | 若要做，需要另外設計「依頁數估算成本」與「多長算太長」的門檻，是獨立一張卡 |
| **D10-6** | `upload_roots` 預設值與是否要求使用者顯式設定（而非退回殼 cwd） | 建議退回殼 cwd 作為預設（與現有 `vault_resolve` 的 cwd 概念一致），但文件要明確寫清楚「殼能讀到你工作目錄下的任何檔案」 | 影響 6.2 的安全預設 |

**對既有功能的影響**：

- **樂觀鎖**：`documents` 目前設計沒有 `expected_updated` 樂觀鎖（不像 Note 支援併發
  `update`）——原因是文件更新走「建新版本」（`supersedes`）而非原地改欄位，天然沒有
  「兩個人同時改同一份」的衝突場景。若之後要支援改 metadata（例如改 `filename` 顯示名稱
  但不換版本），才需要補樂觀鎖，屬於範圍外的小卡。
- **查重**：note 的 `find_duplicates`（標題/內容相似度）邏輯不適用於文件（文件去重是
  sha256 精確比對，語意完全不同），本提案未觸碰 `notes/service.py` 的查重邏輯。
- **快照**：文件／chunk **不進 `GET /v1/snapshot` 白名單**（v1）——快照是給 hook 降級用的
  唯讀讀取，hook 只碰 note／concept，文件檢索走線上服務即可，不需要離線降級（服務不可達
  時 `recall(kinds=["chunk"])` 直接回「不支援降級」而非嘗試讀取不存在於快照裡的資料，
  需要在 `recall` 降級邏輯明確處理：降級模式下若 `kinds` 含 `"chunk"`，該 kind 出現在
  `unsupported_kinds` 而非安靜略過）。
- **A17（管線 vault 歸屬）**：不受影響，管線只處理 concept，不處理 document。
- **匯入對帳（`import_on`）**：不受影響，`import_on` 只處理 note；文件是全新資料流，
  有自己的對帳項（3.3），不共用 `import_sources`／`import_vault_counts`。

---

## 8. 實作拆卡

> 沿用 `docs/TASKS.md` 格式；編號接續現有最後一張 **T-51**。「阻塞：D-x」比照
> `docs/TASKS.md` 既有寫法，指本卡動工前該裁決項必須定案。每張卡標明對應 doctor 對帳項。

### 階段 A：space（可與階段 B 部分並行，但 B 的 vault 相關卡依賴 A 完成）

#### T-52: space schema 遷移與 Vault 型別擴充
- 阻塞：D-space-1
- 範圍：`storage/migrate.py` 新增 v7 migration（`ALTER TABLE vaults ADD COLUMN space
  TEXT NOT NULL DEFAULT 'dev'`）；`schema/models.py` 的 `Vault` 加 `space` 欄位、新增
  `SPACES = frozenset({"dev","lore","personal"})` 常數與驗證
- 涉及檔案：`storage/migrate.py`、`schema/models.py`
- 依賴：無（獨立於文件相關卡）
- 驗收標準：既有資料庫升級到 v7 後所有既有 vault `space='dev'`；`Vault(space="其他值")`
  建構拋錯；既有測試（134 項 + 現有 Lore Vault 測試）全數通過不受影響
- doctor：新增 `space.valid_values`（掃 `vaults.space` 不在白名單即 fail），測試需證明
  手動塞入非法值後該項變紅
- 預估：S

#### T-53: vault 範圍解析納入 space（`storage/vaults.py`）
- 阻塞：無（依賴 T-52）
- 範圍：`resolve_read`／`resolve_write`／`_lookup`／`upsert_vault`／`ensure_vault` 加
  `space` 參數；`_lookup` 查詢條件加 `AND space = ?`；`VaultScope` 新增 `space` 欄位；
  `vault_clause` 的 `is_all` 分支改成 `IN (SELECT key FROM vaults WHERE space = ?)`
  （2.3 節）
- 涉及檔案：`storage/vaults.py`
- 依賴：T-52
- 驗收標準：跨 space 查詢同名／同 key 不會誤命中（測試：在 `dev` 與 `lore` 各建一個
  vault，`vault="*"` 在 `space="dev"` 下只回 dev 的那個）；key 存在但屬於別的 space
  時 `resolve_read` 拋 `UnknownVault`，不洩漏存在性；所有既有呼叫端（`notes/service.py`、
  `recall/service.py`、`storage/vectors.py`、`storage/fts.py` 等）補上 space 參數並通過
  既有測試
- 預估：M

#### T-54: 非 dev vault 命名前綴驗證 + `vault_resolve` 擴充
- 阻塞：無（依賴 T-53）
- 範圍：`vault_resolve` 新增 `space?`、`key?` 參數（2.4 節）；服務端驗證非 dev space
  的 key 前綴（1.4 節），不合規直接 400 `space_key_prefix_required`；`global` vault
  命名規則（1.3）落地
- 涉及檔案：`api/routes.py`、`mcp/server.py`（`Shell` 或工具邏輯）、`notes/service.py`
  或新的 `vaults` 服務層（視現行 `vault_resolve` 邏輯所在位置）
- 依賴：T-53
- 驗收標準：`space="lore", key="aeswir-arc"`（無前綴）被拒；`space="lore",
  key="lore/aeswir-arc"` 成功建立；`space="dev"` 帶 `cwd` 行為不變（回歸測試既有
  binding 案例）
- doctor：新增 `space.key_prefix_agreement`（1.4 節），測試證明手動塞入不合前綴的
  vault 後該項變紅
- 預估：M

#### T-55: 殼端「目前 space」狀態與 `space` 工具
- 阻塞：無（依賴 T-53，介面上依賴 T-54 的 `vault_resolve` 擴充參數同時到位較合理）
- 範圍：`mcp/server.py` 的 `Shell` 新增 `self.space: str = "dev"`；新增 `space(action,
  value?)` 工具；既有工具（`recall`／`get`／`list`／`write`／`update`／`status`）轉發
  服務請求時自動附上 `self.space`；HTTP body（`api/routes.py` 各 `_Req` 子類）新增必填
  `space` 欄位，缺欄位 400（2.1 節）
- 涉及檔案：`mcp/server.py`、`mcp/client.py`（若請求組裝在這層）、`api/routes.py`
- 依賴：T-53、T-54
- 驗收標準：新殼行程 `space(action="get")` 回 `"dev"`；`space(action="set",
  value="lore")` 後 `recall` 只回 lore 內容；未帶 `space` 的裸 HTTP 請求（模擬繞過殼）
  一律 400，不落回 dev；殼重啟後回到 `"dev"`（不持久化）
- 預估：M

#### T-56: 降級快照的 space 過濾
- 阻塞：無（依賴 T-53、T-55）
- 範圍：`mcp/snapshot.py` 降級查詢路徑（`recall`／`get`／`list`／`vault_resolve` 讀
  `snapshot.db` 時）套用殼目前 space 過濾，比照線上路徑（2.5 節第 4 列）
- 涉及檔案：`mcp/snapshot.py`
- 依賴：T-53、T-55
- 驗收標準：測試模擬服務不可達＋殼切到 `lore`，降級查詢不會回出 dev 的內容
- 預估：S

---

### 階段 B：文件存儲與檢索

#### T-57: 前置 spike——`pypdf` 中文抽取品質
- 阻塞：D10-4
- 範圍：拋棄式腳本，用幾份中文 PDF 樣本（含至少一份純文字層、一份掃描圖片型）跑
  `pypdf` 抽取，記錄結果好壞與如何區分「抽不出來」與「本來就沒有文字層」
- 輸入/輸出：測試記錄，決定 4.4 節 `empty_extraction`／`corrupt` 的判斷條件是否要調整，
  結論寫回本文件
- 依賴：無
- 驗收標準：明確結論「pypdf 對中文文字層 PDF 是否可靠」，若不可靠需記錄替代方案
  （例如加一個備援套件）並更新 4.1 節選型
- 預估：S

#### T-58: document／chunk schema 遷移
- 阻塞：無（依賴 T-52，因 `documents.vault` 需要 vault 表已有 space 欄位以維持一致的
  遷移順序；技術上可並行但建議接續 v7 一起規劃 v8）
- 範圍：`storage/migrate.py` 新增 v8：`documents`、`document_chunks`、
  `document_chunk_embeddings`、`document_tombstones`、`document_enrichment` 建表，
  `chunk_fts` 虛表（3.1 節 DDL）
- 涉及檔案：`storage/migrate.py`、新模組 `storage/documents.py`（比照 `storage/notes.py`
  的分工：`_row_to_document`、`insert_document`、`get_document`、`list_documents` 等）
- 依賴：T-52
- 驗收標準：空資料庫可遷移到 v8；`documents.status` 的 CHECK 約束生效（非法值插入拋錯）；
  既有測試不受影響
- 預估：M

#### T-59: blob 儲存（內容定址、去重）
- 阻塞：無
- 範圍：`storage/documents.py`（或新模組 `storage/blobs.py`）實作 blob 寫入
  （`/data/blobs/<sha前2碼>/<sha>`，暫存檔 + `os.replace`，比照備份與快照的原子寫入
  慣例）、依 sha256 查重
- 涉及檔案：`storage/blobs.py`
- 依賴：T-58
- 驗收標準：同雜湊寫入兩次，磁碟只有一份檔案；寫入中途中斷（模擬）不留半檔
- doctor：`documents.blob_exists`、`documents.orphan_blobs`（3.3 節），測試證明刪除
  引用中的 blob 後 `blob_exists` 變紅、留下未引用的 blob 後 `orphan_blobs` 變紅
- 預估：M

#### T-60: 抽取器——md/txt/json/yaml/toml
- 阻塞：D10-1（若採建議的分批方案，本卡不受影響，屬第一批）
- 範圍：純文字與結構化格式的抽取＋切段（4.1、4.2 節），輸出統一的 `list[Chunk]`
  中介格式（text + locator）
- 涉及檔案：新模組 `lore_vault/documents/extract/text.py`、`.../structured.py`
- 依賴：T-58
- 驗收標準：每種格式至少 3 個樣本（含中文內容）抽取結果的 chunk 數與 locator 正確；
  `.md` 標題巢狀時 locator 的標題路徑正確組合
- 預估：M

#### T-61: 抽取器——pdf（依 T-57 結論調整）
- 阻塞：D10-1、D10-4（依賴 T-57 spike 結論）
- 範圍：`pypdf` 抽取＋依頁切段（4.1、4.2、4.3 節），加密 PDF 走 `error_code=encrypted`
- 涉及檔案：`lore_vault/documents/extract/pdf.py`
- 依賴：T-57、T-58
- 驗收標準：正常 PDF 依頁產出 chunk 且 locator 頁碼正確；加密 PDF 標記
  `status=failed, error_code=encrypted`，不拋未捕捉例外；純圖片 PDF 標記
  `error_code=empty_extraction`
- 預估：M

#### T-62: 抽取器——docx/pptx（依 D10-1 決定是否延後）
- 阻塞：D10-1
- 範圍：`python-docx`／`python-pptx` 抽取＋依標題／投影片切段
- 涉及檔案：`lore_vault/documents/extract/docx.py`、`.../pptx.py`
- 依賴：T-58
- 驗收標準：`.pptx` 每張投影片一個 chunk（或依 4.2 節再切）、locator 投影片編號正確；
  `.docx` 依 Heading 樣式分段，無標題文件退回單一區塊定長切
- 預估：M

#### T-63: 抽取 worker（非同步佇列）
- 阻塞：無
- 範圍：比照 `enrich/worker.py` 的模式，新增文件抽取的背景 worker：`pending →
  extracting → ready/failed` 狀態轉換、`document_enrichment` 嘗試/退避紀錄
  （4.4 節）
- 涉及檔案：新模組（`documents/worker.py` 或併入現有 `enrich/worker.py`，視現有 worker
  是否已支援多種 job 種類決定，需先讀 `enrich/worker.py` 現況再定案模組邊界）
- 依賴：T-59、T-60（至少一種抽取器就緒才能整合測試）
- 驗收標準：上傳後立即回 `pending`，背景轉為 `ready` 或 `failed`；模擬抽取拋例外時
  狀態正確轉 `failed` 並記錄 `error_detail`，不讓 worker 迴圈整個掛掉
- doctor：`documents.stuck_processing`（3.3 節），測試證明卡在 `extracting` 超過門檻
  變紅
- 預估：M

#### T-64: chunk 向量與 FTS 索引寫入
- 阻塞：無
- 範圍：抽取完成後，chunk 逐筆算 embedding（沿用 `recall/embedder.py` 的
  `Embedder`／bge-m3 呼叫）寫入 `document_chunk_embeddings`；`chunk_fts` 寫入
  （比照 `storage/fts.py` 的 CJK bigram 展開，新增 `chunk_fts` 對應的
  `search_chunks` 查詢函式）
- 涉及檔案：`storage/fts.py`（擴充或新增 `document_chunk_fts` 相關函式）、
  `storage/vectors.py`（擴充或新增 chunk 向量搜尋函式，注意 3.1 節已決定用獨立表，
  不與 note 共用 `search_vectors`）
- 依賴：T-63
- 驗收標準：chunk 完成 embedding 後可用 FTS 與向量各自查到；`documents.chunk_count`
  與實際 chunk 數一致
- doctor：`documents.chunk_count_matches`、`documents.fts_rows_match_chunks`、
  `documents.vector_rows_match_chunks`（3.3 節），三項都要有測試證明資料不一致時變紅
- 預估：M

#### T-65: recall 整合 chunk kind
- 阻塞：無
- 範圍：`recall/service.py` 的 `KNOWN_KINDS`／`SUPPORTED_KINDS` 加入 `"chunk"`；
  四路（note-lexical/vector、chunk-lexical/vector）一起送進 `rrf_fuse`（5.2 節）；
  `RecallItem` 擴充欄位（`document_id`／`chunk_id`／`locator`）
- 涉及檔案：`recall/service.py`
- 依賴：T-64
- 驗收標準：`recall(kinds=None)` 同時回 note 與 chunk 結果，排序由 RRF 融合決定
  （測試：構造一個查詢字面比對度低但向量相關度高的 chunk，驗證仍可能進前 N 名）；
  `recall(kinds=["chunk"])` 只回 chunk；降級模式（embedder 不可用）下 chunk 仍可靠
  lexical 命中
- 預估：M

#### T-66: `get`／`list` 支援 document／chunk
- 阻塞：無
- 範圍：`notes/service.py`（或抽出共用的 `get`/`list` 分派層）依 id 前綴分派到
  note 或 document 儲存層；`list` 新增 `kinds?` 參數（5.3 節）
- 涉及檔案：`notes/service.py`、`storage/documents.py`、`api/routes.py`
- 依賴：T-58、T-64
- 驗收標準：`get(ids=["chunk:xxx"])` 回單一段落全文；`get(ids=["doc:xxx"])` 回整份
  文件（依序串接、受 budget 截斷並標記 `truncated`）；`list(kinds=["document"])`
  只列文件
- 預估：M

#### T-67: `upload` 工具與 `POST /v1/documents`
- 阻塞：D10-3（大小上限）、D10-6（`upload_roots` 預設）
- 範圍：MCP `upload` 工具（讀本機路徑、路徑安全檢查）、HTTP multipart 端點
  （6.2、6.3 節），重複上傳（同雜湊）與更新版本（`supersedes`）邏輯
- 涉及檔案：`mcp/server.py`、`mcp/client.py`、`api/routes.py`、`storage/documents.py`
- 依賴：T-59、T-63
- 驗收標準：上傳合規檔案回 `document_id, status=pending`；超過大小上限在收到完整檔案
  前即拒絕；路徑在 `upload_roots` 之外的請求被拒；同雜湊二次上傳回
  `duplicate=true` 且不重新排隊；帶 `supersedes` 的上傳使舊版本從索引移除
  （對應 `documents.superseded_chunks_removed` 對帳項）
- doctor：`documents.superseded_chunks_removed`（3.3 節）
- 預估：L

#### T-68: 刪除與墓碑（`cli.admin` 擴充）
- 阻塞：無
- 範圍：`cli.admin` 新增 `delete-document --vault KEY --id DOC_ID`，比照
  `delete-note` 的 dry-run／`--yes`／單一交易慣例（3.2 節）
- 涉及檔案：`cli/admin.py`
- 依賴：T-58
- 驗收標準：刪除後 chunk／FTS／向量列一併清除，墓碑寫入；blob 不受影響（測試驗證
  同 sha 的其他 document 仍可正常 `get`）
- 預估：S

#### T-69: 快照白名單明確排除文件（不進降級）
- 阻塞：無
- 範圍：確認 `storage/snapshot.py` 白名單不含 `documents`／`document_chunks`；
  `recall` 降級模式下 `kinds` 含 `"chunk"` 時該 kind 落入 `unsupported_kinds`
  而非嘗試查詢不存在的資料（7 節「對既有功能的影響」）
- 涉及檔案：`recall/service.py`（降級分支）、`mcp/snapshot.py`
- 依賴：T-65
- 驗收標準：模擬服務不可達＋`recall(kinds=["chunk"])`，回應標示 `chunk` 在
  `unsupported_kinds`，不拋例外、不回誤導性空結果
- 預估：S

---

**任務卡總數：18 張**（階段 A 5 張：T-52～T-56；階段 B 13 張：T-57～T-69）。
