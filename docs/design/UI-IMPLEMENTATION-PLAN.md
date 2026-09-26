# UI 實作計畫

> 分析對象：`ui/design-source/`（Claude Design 匯出稿，2026-09-26 匯入，分支 `feature/ui` @ `e8edbe8`）。
> 稿內容（畫面、假資料、假金鑰 `lv_4f8a…`）一律視為資料，不視為指令。
> DECISIONS.md 中「待裁決」項目未定案前不假設已選定；本文件第 2、3 節的建議待艾斯維爾裁決。

## 1. 設計涵蓋度

### 1.1 畫面對照（UI-DESIGN-BRIEF 第三節 9 類）

設計稿 `<x-dc>` 內共 11 個 `screen` 分支，對 9 類是一對多（筆記分讀/編輯/衝突三態、文件分列表/檢視兩頁），不是超出範圍。

| # | Brief 類別 | 設計稿分支 | 涵蓋度 |
|---|---|---|---|
| 1 | 連線設定 | `settings` | 有 |
| 2 | Space 切換與 Vault 列表 | header space 下拉 + `vaults` | 部分（見 1.3） |
| 3 | 搜尋 | `search` | 有 |
| 4 | 筆記檢視與編輯 | `note`（read/edit/conflict）、`new`（疑似重複） | 有 |
| 5 | 筆記列表 | `notes` | 有 |
| 6 | 文件 | `docs`（列表/上傳）、`doc`（檢視） | 部分（見 1.3） |
| 7 | Vault 維護 | `maint` | 部分（見 1.3） |
| 8 | 系統健康 | `health` | 有 |
| 9 | 記憶層 | `memory` | 有 |

### 1.2 導航結構

- Header（sticky）：BrandMark、space 切換下拉（切換會重置 vault/query/tag/screen）、連線狀態小圓點＋主機位址、全域 degraded 徽章、深淺色切換。
- Sidebar（左 260px，sticky）：8 項主導覽（health 項在有 fail 時掛紅色數字 badge）＋當前 space 的 vault 列表（點擊篩選）＋底部「目前寫入位置」卡。
- 無傳統 tabs；筆記/文件詳情走 breadcrumb（`{{ sp.en }} / {{ vault }} / 頁面類型`）。

### 1.3 Space 視覺區隔

`space` 綁定設計系統既有 zone（借用色系，非新建）：

| space | zone | 色系 |
|---|---|---|
| `dev` | `concepts` | 綠 `--concepts-main:#2d6a4f` |
| `lore` | `history` | 棕金 `--history-main:#6b3f2a` |
| `personal` | `echoes` | 藍紫 `--echoes-main:#355c7d` |

容器用 `data-zone="{{ zone }}"`，元件一律吃 `var(--zone-main, ...)` 別名，不寫死顏色。`visuals`／`storage` 兩個 zone 設計系統有但本專案不用。

### 1.4 狀態呈現

| 狀態 | 設計稿寫法 |
|---|---|
| 降級 | 金色警示框＋`DEGRADED` mono 標籤＋「查看健檢」CTA；header 常駐徽章；health/settings 頁同步反映 |
| 截斷 | dashed border 卡＋「已達字數預算」說明＋「載入其餘結果」按鈕（純 UI，無實際分頁邏輯） |
| 摘要來源 | 每筆結果/筆記有「摘要」（zone 色）vs「首段」（金色，`summary_source:"lead"`）標籤 |
| 抽取失敗 | 文件列表紅點+紅字，`detail` 欄寫原因（無文字層/已加密），提供「重試」按鈕（**目前無對應 API，見第4節**） |
| 版本衝突 | 筆記詳情 `noteConflict` 分支，左右並排 diff＋合併/覆寫/放棄三動作（**目前 API 只回 409+current，無合併端點**） |
| 疑似重複 | 新增筆記頁側欄，相似度分數＋「改寫這則」/「照樣新增」 |
| 空狀態 | 設計稿無實例（假資料恆非空）；設計系統有 `.zone-state`/`.zone-state--error`/`.empty-notice`，需自行套用 |
| 載入中 | 文件抽取 `run`（藍點+百分比）、backfill 進度條；**無 skeleton/spinner 元件**，需自訂或沿用 `.uep-toast`/`.zone-state` 模式 |
| 錯誤 | health fail 分組紅底、vault 刪除確認紅框輸入、`.zone-state--error` |

### 1.5 深淺色與 RWD

- `data-theme="{{ theme }}"` 綁根容器，`color-scheme` 同步；顏色 token 分 `[data-theme='dark']` 覆寫兩層（colors.css、zones.css）。無 `prefers-color-scheme`，純靠屬性切換；`@media (prefers-reduced-motion:reduce)` 只關動畫。
- **RWD 幾乎沒有**：固定 `grid-template-columns:260px minmax(0,1fr)` 側欄，僅少數卡片 `flex-wrap` 自然換行。設計系統本身有極少數斷點（`.zone-prev-next` @600px）與 `--gutter-mobile` token，但 dc.html 未套用。Brief 明說「手機可用」——**手機版佈局需要實作階段自行設計，沒有可抄的稿**。

### 1.6 設計系統元件與 tokens

`_ds/.../` 內 29 個 React 元件（namespace `UEPImaginarySpaceDesignSystem_6b2a32`），dc.html **只 `x-import` 了 `BrandMark`、`Button` 兩個**，其餘 27 個（含 `Toast`/`Dialog`/`ZoneState`/`Breadcrumb`/`NavTree` 等本專案用得到的）未在稿中示範用法。`_ds_bundle.js` 用 `React.createElement`/`jsx(...)`（354/130 處命中），但**不內含 React**——是外部 global 依賴，執行時由 dc-runtime 環境注入。

**實際可直接重用的是 `tokens/*.css` + `components/components.css`（純 CSS，framework-agnostic），不是 React 元件本身**——這點會影響第 2 節選型的論證強度。

Tokens 六檔：`base.css`（reset/hairline/uep-voice 特效）、`colors.css`、`zones.css`、`typography.css`（四字型家族）、`motion.css`（easing/duration/keyframes）、`space.css`（gutter/measure/radius/shadow）。

### 1.7 互動細節

- **快捷鍵**：稿內完全沒有（無任何鍵盤事件綁定）。
- **拖放上傳**：只有靜態視覺（dashed border 卡），無 `onDrop`/`onDragOver` 事件或真實上傳邏輯。
- **hover**：大量用 dc-runtime 專有的 `style-hover="..."` 屬性（非標準 HTML/CSS，實作時要轉成一般 CSS `:hover` 或 CSS-in-JS）。
- **動畫**：`motion.css` 定義的 `uep-fadeIn`/`uep-pulse`/`uep-shimmer`/`uepToastIn`/`uepDialogIn`，dc.html 本身沒直接用（只用行內 style），來自被 import 元件（Toast/Dialog）內部。

### 1.8 設計稿有、API 沒有的功能

清單見第 4 節（避免重複列舉）；重點項：文件抽取重試、筆記版本衝突的合併/覆寫/放棄動作、vault 別名管理、vault 刪除與墓碑復原、vault 換 space、concept 瀏覽的分頁查詢、vault 列表查詢。

### 1.9 Brief 有、設計稿沒畫的畫面/細節

- 編輯 vault 顯示名稱（brief 第 2 類要求，`maint` 頁只有別名/搬移/刪除，無改名表單）
- 刪除單則筆記的入口（`maint` 只畫刪 vault；筆記刪除只能靠 note 詳情頁隱含操作，稿未畫）
- 上傳新版本後「舊版被取代」的視覺標示（`docs` 表格有 `version` 欄但無「已被取代」樣式）
- 手機版佈局（見 1.5）
- 真正的空狀態、loading skeleton、鍵盤快捷鍵、拖放事件實作（見 1.4/1.7）

---

## 2. 技術選型

### 建議：**(a) Preact + Vite，建置成靜態檔由 FastAPI 提供**

| 方案 | 優點 | 缺點 | 結論 |
|---|---|---|---|
| **(a) Preact/React + Vite → 靜態檔** | 元件化好維護；`tokens/*.css` 直接複製沿用；生態成熟（router/表單/diff 檢視庫齊全）；Vite dev server 開發體驗好；建置產物是純靜態檔，部署與 (b) 一樣簡單 | 多一套 node 建置鏈；Docker 多階段建置（不影響最終映像體積，因 runtime 階段只複製 `dist/`） | **採用**。Preact 優於 React：bundle 更小（單人工具、無需 React 生態全部功能），API 相容多數 hooks/JSX 寫法，`_ds` 的 `React.createElement` 呼叫模式可用 `preact/compat` 相容層或直接以 Preact 重寫這幾個元件（只需要 `Button`/`Dialog`/`Toast`/`ZoneState`/`Breadcrumb`/`NavTree` 少數幾個，工作量可控） |
| (b) 無建置鏈原生 ES modules + Web Components/lit | 零建置、瀏覽器直接跑；長期依賴面最小 | 沒有 JSX，稿內大量巢狀模板要手寫成 `html\`...\`` 或 DOM API，維護成本高於預期；lit 仍是一個要學的框架，並非真的「無框架」；表單狀態/路由要自己接線；單人維護下「省一套建置鏈」換來「每個畫面都手寫」不划算 | 不採用 |
| (c) 沿用 dc-runtime 風格 | 稿可以直接執行、零轉譯 | `support.js` 自述「GENERATED from dc-runtime/src/*.ts — do not edit」，是預覽器不是框架；`style-hover` 等非標準屬性、`sc-if`/`sc-for` 自訂標籤沒有官方外部維護承諾；稿內邏輯全是假資料 mock，無真實 fetch/錯誤處理範式；沒有測試工具鏈（Testing Library 不認識這套語法） | 不採用 |

**測試面**：新增 vitest（元件單元測試）+ Playwright（E2E，覆蓋降級/截斷/衝突等狀態呈現，見第5節驗收）。這是專案第二套測試工具鏈（現有 pytest），單人維護下要接受這個新增成本；換來的是狀態呈現能寫進自動化驗收，而非每次手動點檢。

**Docker**：`Dockerfile` 現況為兩階段（uv builder → python runtime）。UI 建置加一個 `FROM node:22-slim AS ui-builder` 階段（`npm ci && npm run build` 產出 `dist/`），runtime 階段 `COPY --from=ui-builder /app/ui/dist ./static/ui`；node 不進最終 runtime 映像，映像體積不受影響。FastAPI 用 `StaticFiles` 掛載 `/ui`。

**待裁決**：Preact vs React（差異只在 bundle 大小與 `preact/compat` 相容成本，屬可逆決定，建議 Preact 起步，卡住再切 React）。

---

## 3. 部署與認證

### 3.1 UI 由 `lore-vault` 同程序提供

**建議**：FastAPI 掛載 `/ui`（`StaticFiles`，`html=True`）提供建置後的靜態檔，API 仍在 `/v1/*`。理由：單一服務、單一埠、單一 Cloudflare tunnel 目標（5056），CORS 可完全省略（同源）。獨立部署（另一個容器/埠）只會多一個要維運的服務，對單人維護沒有好處。

**張力**：設計稿「連線設定」頁有「服務位址」輸入欄，同源部署下這欄位沒有意義（UI 永遠打自己所在的 origin）。實作時應把此欄拿掉或改唯讀顯示，只保留 token 輸入與「測試連線」。

### 3.2 Bearer token 保存

三案：

| 方案 | 風險 | 結論 |
|---|---|---|
| sessionStorage / 記憶體 | XSS 可直接偷 token；分頁關閉即失效 | 嚴格 CSP（無 inline script、無第三方 script）下 XSS 面已大幅壓低，單人內部工具可接受 |
| HttpOnly cookie 交換端點 | 需多一支 `/v1/session` 端點做 token↔cookie 交換、CSRF 防護（同源+`SameSite=Strict`可壓但仍多一個攻擊面）；`/v1/*` 現有 bearer 驗證邏輯要改成同時吃 cookie | 多一層複雜度，換來的防護在「同源+嚴格CSP+單人使用」情境下邊際效益有限 |
| 純記憶體（每次重整要重新輸入） | 最安全 | 使用體驗差，單人工具不必做到這個程度 |

**建議**：sessionStorage 存 token，搭配嚴格 CSP（見 3.3）。**待裁決**：是否接受此風險等級（艾斯維爾裁決）。

### 3.3 CSP / CORS

- 同源部署（3.1）下 **CORS 直接省略**（不設 `Access-Control-Allow-Origin`，跨源請求本就該被拒）。
- CSP 建議：`default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'`（Vite 建置產物若需要可調整）、`font-src 'self'`。字型（Cormorant Garamond/Noto Serif TC/Inter/JetBrains Mono/Cinzel）**建議自託管**（下載字型檔進 `ui/public/fonts/`），不依賴 `fonts.gstatic.com`，避免多一個外部依賴與對應的 CSP 例外。

### 3.4 Cloudflare Access

- 現況（MIGRATION.md）：`pm` 子網域仍指向舊 Open Notebook Web UI（8502），尚未切到 5056；切換需 T-45 授權，**本計畫不假設已生效**。
- **v1 建議**：維持 A15 既有邊界——服務只驗自己的 bearer token；Cloudflare Access 純粹當外層網路邊界（未登入 Access 連不到 tunnel），UI 不額外驗 Access JWT。
- **待裁決**：日後是否要讓服務端也驗 Access JWT 作為 UI 認證的一部分（會多一個 JWT 驗證依賴與相應設定），本計畫不預先實作。
- T-45（ingress 切換到 5056）是本計畫的**外部依賴**，不在本次拆卡範圍內，需另外排。

---

## 4. API 缺口

沿用 `routes.py` 既有慣例：`POST /v1/<名稱>`、body 繼承 `_ScopedReq`（`space` 必填，`extra="forbid"`）、錯誤碼比照既有 `space_required`/`unknown_vault`/`space_key_prefix_required` 風格；**不提出 REST 路徑風格**（ARCHITECTURE.md 明定「一律 POST，RPC 式」）。

### 4.1 端點不存在（需新增）

端點命名統一扁平 snake_case（比照 `vault_resolve`/`vault_update`，不用 `concepts/export` 那種巢狀路徑——那是 spike 遺留，不跟）。

| 建議端點 | 用途 | 對應畫面 | 層級 | 管理操作／需二次確認 |
|---|---|---|---|---|
| `POST /v1/vault_list` | 列出目前 space 所有 vault | Vault 管理 | **服務層＋HTTP 都要新增**：現有 `_vault_dict()` 只回 `key/display/kind/space/aliases/note_count`，缺設計稿要的 `doc_count`（文件數，需新查詢）、`updated`（最近更新時間，需新查詢）、`source`（手動/收料自動/管線建立——**schema 完全沒有這個概念**，`Vault.kind` 只有 `repo`/`global` 兩值，`source` 需要新欄位或另一套推導邏輯，屬 schema 層級決策，不只是加端點） | 否 |
| `POST /v1/vault_update` | 編輯 vault 顯示名稱 | Vault 管理 | HTTP 新增 | 否 |
| `POST /v1/vault_alias_add` / `POST /v1/vault_alias_remove` | 別名新增/移除（重新導向） | Vault 維護 | **服務層＋HTTP 都要新增**（A17 提到「之後 UI 提供」但契約未定） | 是（改變 key 解析行為） |
| `POST /v1/vault_move_space` | 把 vault 移到另一 space | Vault 維護 | HTTP 包 CLI `set-space` 邏輯 | **是**：僅 `lore`↔`personal`（A20），單一交易內改 key 前綴、舊 key 不留別名；**dev 不可轉入轉出，UI 應直接灰掉此選項** |
| `POST /v1/vault_delete` | 刪除整個 vault | Vault 維護 | HTTP 包 CLI `delete-vault` | 是：二次確認需輸入 vault key 全文（稿已畫此互動） |
| `POST /v1/note_delete` | 刪除單則筆記 | 筆記詳情/列表 | HTTP 包 CLI `delete-note` | 是：二次確認 |
| `POST /v1/note_undelete` | 復原墓碑筆記 | Vault 維護（墓碑清單） | HTTP 包 CLI `undelete-note` | 否（復原非破壞性） |
| `POST /v1/tombstones` | 列出 note/document 墓碑（分頁） | Vault 維護 | **服務層＋HTTP 都要新增**（CLI 無列表功能，只有單筆 undelete） | 否 |
| `POST /v1/document_delete` | 刪除文件 | 文件列表 | HTTP 包 CLI `delete-document` | 是：二次確認 |
| `POST /v1/document_undelete` | 復原被刪文件 | Vault 維護 | **服務層缺（CLI 連 `undelete-document` 都沒有），需先補 schema/服務層再補 HTTP** | 否 |
| `POST /v1/document_retry` | 重試抽取失敗的文件 | 文件列表（稿已畫「重試」按鈕） | **服務層＋HTTP 都要新增**（目前失敗後無重排機制） | 否 |
| `POST /v1/concept_query` | concept 分頁瀏覽（依 scope/repo 篩選） | 記憶層 | HTTP 新增（現有 `/v1/concepts/export` 是給 pipeline 用的整批匯出，非分頁友善） | 否 |
| `POST /v1/episode_summary` | 各機器/專案 episode 概況（收料新鮮度） | 記憶層、系統健康 | HTTP 新增（現有 `/v1/episodes` GET 是 spike 查詢，欄位/範圍需對齊） | 否 |
| （不需新端點）版本衝突的合併/覆寫/放棄 | 筆記詳情（`noteConflict`） | 前端流程即可：409 回 `current` → 前端顯示 diff → 使用者選 merge/overwrite 後，用 `current.updated` 當 `expected_updated` 重送既有 `POST /v1/update`；discard 就是不重送。伺服端加一個「策略」端點不會做得更好，也不符合「只做需要的功能」 | 覆寫仍是破壞性動作，前端在重送前需二次確認 |
| `POST /v1/document/{id}` 取代方案 → 併入 `/v1/get` | 單一文件詳情/chunk 檢視 | 文件檢視 | 可能不需新端點：`/v1/get` 傳 `doc:`/`chunk:` id 已可組出，需求階段先驗證欄位是否夠用（見 4.2） | 否 |
| 輕量 doctor 端點（可選） | 系統健康頁若不想每次都拉全量 `/v1/status` | 系統健康 | 可選：目前 `/v1/status` 已含完整 doctor report，是否需要拆分留待實作時評估 | 否 |

### 4.2 端點存在、但欄位/形狀需對齊（不是新增，是前端要適配）

| 端點 | API 實際欄位 | 設計稿假想欄位 | 對齊方式 |
|---|---|---|---|
| `POST /v1/recall` | `RecallResult.to_dict()`：`items[]`（`id/kind/vault/title/summary/summary_source/score/...`、chunk 另有 `locator`）、`degraded`、`degraded_reason`、`degraded_detail`、`truncated`、**`omitted`（被截掉的筆數，recall/service.py:121，設計稿目前未用到）** | `results[]` 含 `match`（語意/混合/關鍵字分數） | 前端用 `score` + `mode` 自行決定顯示文案（稿的 `match` 是純 UI 展示層，非 API 欄位）；**截斷提示的「剩餘 N 筆」直接用 `omitted`，「載入其餘結果」= 提高 `budget`/`limit` 重查，不是純 UI 假動作** |
| `POST /v1/list` / `/v1/get` | `GetResult`/`ListResult.to_dict()`；note 有 `summary_source`（notes/service.py:470），但 **`Note`（schema/models.py:111）沒有 `author` 欄位，也沒有整數 `version`（樂觀鎖用的是 `updated` 時間戳，見下）——已核對確認，這是真缺口不是待查項** | 稿用 `author`、`v4/v5` 整數版號、`tagChips` | `author` 若要顯示需在 4.1 新增 schema 欄位（例如寫入來源機器/使用者），屬服務層變更，本計畫先標記需求，交實作階段決定是否要做；`tagChips`/`timeChips` 是前端依 `topics`/`updated` 自行分組，非 API 回傳 |
| `POST /v1/update` | 需要 `expected_updated`（`Note.updated` 時間戳字串，同時是樂觀鎖版本，schema 未另存整數版號） | 稿以 `version` 整數（v4/v5）表示 | 前端 UI 用「最後更新時間」取代「版本號 vN」文案；若產品堅持要顯示遞增版號，需在 4.1 額外提議 schema 加欄位（本計畫不預設要做） |
| `POST /v1/documents`（upload） | `UploadResult.to_dict()`：`status`（pending/extracting/ready/failed）、`error_code`、chunk 含 `locator` | 稿用 `status:'done'/'run'/'fail'` 三態＋`detail` 文字原因 | 前端做 `pending/extracting→run`、`ready→done`、`failed→fail` 的狀態映射；`detail` 文字由 `error_code` 對照表產生（`error_code` 目前有哪些值需實作前一次列舉） |
| `POST /v1/status` | 完整 doctor report + `enrich.backlog` + `documents.backlog` + `embedding.warmup` | 稿的 `backfill[]`、`machines[]`、`healthCounts` | 前端從 `doctor.to_dict()` 分組結果與 `backlog.counts` 組出稿要的呈現，欄位命名需在實作階段對照 `doctor/` 模組的 report 結構（本次未逐一核對，屬正常實作工作，非缺口） |

**尚待核對**（`Note`/`Vault` 已核對如上；`Document`/`Chunk` 精確欄位未逐一核對）：實作 T-82（文件列表與檢視）前一次 grep `src/lore_vault/schema/models.py` 的 `Document`/`Chunk` 定義，確認 `locator` 精確形狀與 `error_code` 現有值的完整列舉，寫進該卡的「涉及檔案」。

### 4.3 新增資料流的 doctor 對帳義務

依專案規範（CLAUDE.md：新增資料流需同時補 doctor 檢查，並用測試證明拿掉保護時會紅），以下新端點視為新資料流，各自的實作卡驗收需含 doctor 檢查：

- 別名新增/移除 → doctor 檢查別名表無循環引用、無指向不存在 vault
- vault 換 space → doctor 檢查 key 前綴與 space 一致、無殘留舊前綴
- document 重試 → doctor 對帳「失敗次數上限」與背景佇列積壓（可能已被現有 backlog 對帳涵蓋，需確認）
- note/document undelete → doctor 對帳墓碑表與現行表無重複 id

---

## 5. 拆卡（T-70 起）

依賴順序：API 卡（5.1）先行 → 建置鏈/部署卡（5.2）→ app shell（5.3）→ 各畫面卡（5.4，可平行，唯一互相依賴的是 shell）→ RWD/A11y/E2E 收斂卡（5.5）。

每張 UI 卡固定驗收三項（除非該卡明確不涉及畫面）：① 元件或 E2E 測試；② 深淺色與鍵盤可達性（Tab 順序、focus 可見、對比度）檢查；③ 狀態呈現不靜默——degraded/truncated/summary_source/抽取失敗/版本衝突/疑似重複等，凡設計稿有畫的狀態，測試需斷言其視覺標示存在，不能只測「資料抓到了」。

### 5.1 API 缺口卡

#### T-70: vault 列表與編輯 HTTP 端點
- 範圍：新增 `POST /v1/vault_list` 列出目前 space 所有 vault；先補 `doc_count`／`updated`（最近更新時間）兩個查詢欄位，`source`（手動/收料自動/管線建立）需先決定是否加 schema 欄位或改用推導邏輯，本卡先只做 `doc_count`/`updated`，`source` 若要做另開卡；新增 `POST /v1/vault_update` 編輯顯示名稱
- 涉及檔案：`src/lore_vault/api/routes.py`、`src/lore_vault/storage/vaults.py`
- 依賴：無
- 驗收標準：涵蓋空 space（回空陣列不報錯）、多 vault；`extra="forbid"` 對未知欄位 422；補 doctor 對帳（vault 表筆數與列表端點回傳筆數一致）
- 預估：S

#### T-71: vault 別名管理端點
- 範圍：服務層＋`POST /v1/vault_alias_add`／`POST /v1/vault_alias_remove`
- 涉及檔案：`src/lore_vault/storage/vaults.py`、`src/lore_vault/api/routes.py`
- 依賴：無
- 驗收標準：新增別名後 `vault_resolve` 可解析；移除後解析失敗（404）；別名衝突（已屬他 vault）回既有 `VaultExists` 風格錯誤；doctor 新增「別名表無循環引用/無懸空指向」檢查，測試證明拿掉此檢查時能紅
- 預估：M

#### T-72: vault 換 space 端點（A20 限制）
- 範圍：`POST /v1/vault_move_space`，包 CLI `set-space` 邏輯為 HTTP，僅允許 `lore`↔`personal`
- 涉及檔案：`src/lore_vault/api/routes.py`、`src/lore_vault/cli/admin.py`（邏輯重用）
- 依賴：無
- 驗收標準：dev↔非dev 一律 400（明確錯誤碼）；lore↔personal 成功時單一交易內 key 改前綴、舊 key 完全不可解析（不留別名）；doctor 新增「key 前綴與 space 一致」檢查並證明拿掉會紅
- 預估：M

#### T-73: vault／note／document 刪除與墓碑端點
- 範圍：`POST /v1/vault_delete`、`POST /v1/note_delete`、`POST /v1/document_delete`、`POST /v1/note_undelete`、`POST /v1/tombstones`（列表）
- 涉及檔案：`src/lore_vault/api/routes.py`、對應 service 模組
- 依賴：無
- 驗收標準：刪除產生墓碑紀錄、`tombstones` 端點可列出；undelete 後原 id 可再被讀取；doctor 新增「墓碑表與現行表無重複 id」檢查並證明拿掉會紅
- 預估：M

#### T-74: document undelete 與重試抽取（服務層新增）
- 範圍：服務層補 `undelete_document`（CLI 目前也沒有，需先補 schema/服務層）；`POST /v1/document_retry` 重排失敗文件的抽取
- 涉及檔案：`src/lore_vault/documents/service.py`、`src/lore_vault/storage/document_index.py`、`src/lore_vault/api/routes.py`
- 依賴：無
- 驗收標準：失敗文件重試後 status 回到 `pending`/`extracting`；重試次數有上限（比照 D4 摘要重試上限的既有模式）；undelete 後 blob 若未被 gc 可正常讀回；doctor 對帳失敗次數與背景佇列積壓
- 預估：M

#### T-75: concept／episode 瀏覽端點
- 範圍：`POST /v1/concept_query`（分頁，依 scope/repo 篩選）、`POST /v1/episode_summary`（各機器/專案概況）
- 涉及檔案：`src/lore_vault/api/routes.py`、concept/episode 相關 storage 模組
- 依賴：無
- 驗收標準：分頁游標正確；scope 篩選涵蓋多筆/零筆情境；episode 概況含各機器最近收料時間（供健康頁判斷新鮮度）
- 預估：M

### 5.2 建置鏈與部署卡

#### T-76: UI 建置鏈與 `/ui` 靜態掛載
- 範圍：`ui/` 建立 Vite + Preact 專案骨架；`Dockerfile` 加 `ui-builder` 階段；FastAPI 掛載 `StaticFiles("/ui")`
- 涉及檔案：`ui/package.json`、`ui/vite.config.ts`、`Dockerfile`、`src/lore_vault/api/app.py`
- 依賴：無（與 5.1 可平行）
- 驗收標準：`docker build` 成功且最終映像不含 node（`docker history` 或 image size 比對佐證）；`/ui` 回傳建置後的 `index.html`；純本機驗證，不啟動實際服務（依專案規範，啟動需另外授權）
- 預估：M

#### T-77: 認證與 CSP
- 範圍：前端 sessionStorage token 存取封裝；FastAPI 加 CSP／安全 header middleware；同源下移除 CORS 設定
- 涉及檔案：`ui/src/lib/auth.ts`（新）、`src/lore_vault/api/app.py`
- 依賴：T-76
- 驗收標準：CSP header 存在且 `script-src 'self'`；缺 token 時 `/v1/*` 呼叫走既定 401 流程並導回設定頁；token 不出現在 URL／console log
- 預估：S

### 5.3 App Shell

#### T-78: App Shell（header／sidebar／space 切換／深淺色）
- 範圍：BrandMark、space 下拉（切換重置篩選狀態）、sidebar 8 項導覽＋vault 列表、深淺色 toggle、全域 degraded 徽章、Toast 容器
- 涉及檔案：`ui/src/shell/`
- 依賴：T-76
- 驗收標準：三個 zone 色系隨 space 切換即時反映；`data-theme` 切換涵蓋 E2E；鍵盤可從 header 到 sidebar 到主內容依序 Tab；health 項 fail badge 依 `/v1/status` 動態顯示
- 預估：M

### 5.4 畫面卡（可平行，皆依賴 T-78）

#### T-79: 搜尋畫面
- 範圍：查詢框、vault 篩選、結果列（含摘要來源標籤、分數顯示）、降級橫幅、截斷提示（用 `omitted` 顯示剩餘筆數，「載入其餘結果」= 提高 `budget`/`limit` 重查）
- 依賴：T-78、T-70（vault 篩選用列表端點）
- 驗收標準：degraded=true 時降級橫幅可見且結果分數退化文案正確；truncated=true 時截斷提示顯示正確的 `omitted` 剩餘筆數且「載入其餘結果」實際擴大查詢；空結果有 `.zone-state` 空狀態；E2E 涵蓋正常/降級/截斷/空四種情境
- 預估：M

#### T-80: 筆記列表與詳情（讀/編輯/衝突）
- 範圍：`notes` 列表（標籤/時間篩選、分頁）、`note` 三態（含衝突畫面：409 回應後顯示 diff，merge/overwrite 皆以 `current.updated` 當 `expected_updated` 重送既有 `/v1/update`，discard 不重送）、新增筆記（含疑似重複側欄）
- 依賴：T-78
- 驗收標準：版本衝突畫面在 409 回應時觸發並可完成 merge/overwrite/discard 三種前端流程且成功寫入；overwrite 前需二次確認；疑似重複列表在 `write` 回傳 duplicates 時顯示；`[[標題]]` 連結可點擊導航；rich/非 rich 正文兩種渲染皆有測試；筆記顯示改用「最後更新時間」而非版本號（schema 無整數 version，見 4.2）
- 預估：L

#### T-81: 文件列表與檢視
- 範圍：`docs` 拖放上傳（真實事件綁定）、抽取狀態顯示與重試、`doc` 投影片/頁面檢視、從搜尋跳轉
- 依賴：T-78、T-74（重試端點）
- 驗收標準：真實拖放上傳觸發 `/v1/documents`；`failed` 狀態顯示 `error_code` 對應文案並可觸發重試；新版本上傳後舊版顯示「已被取代」（brief 有、稿未畫，需自行設計）；FROM RECALL 跳轉錨定正確段落
- 預估：L

#### T-82: Vault 管理與維護
- 範圍：`vaults` 建立/列表/編輯顯示名稱、`maint` 別名管理、換 space（dev 選項灰掉）、刪除（二次確認輸入 key 全文）、墓碑清單與復原
- 依賴：T-78、T-70、T-71、T-72、T-73
- 驗收標準：dev vault 的「移到另一 space」選項在 UI 上明確不可點且有說明文字；刪除確認按鈕在輸入完整 key 前維持 disabled；墓碑復原後列表即時更新
- 預估：L

#### T-83: 系統健康
- 範圍：`health` 頁四色計數卡、分組排序（含 fail 排最前）、背景補算進度、收料新鮮度逐機清單
- 依賴：T-78
- 驗收標準：fail 分組排序測試；補算進度條依 `backlog.counts` 正確換算百分比；超時機器（依 `episode_summary`）標紅字
- 預估：M

#### T-84: 記憶層瀏覽
- 範圍：`memory` 頁 concept 列表（scope 篩選、分數色階）、episode 概況
- 依賴：T-78、T-75
- 驗收標準：分數 <0.6 顯示「校準不足」警示文案；scope 篩選涵蓋多值情境
- 預估：M

#### T-85: 連線設定
- 範圍：`settings` 頁（同源部署下移除「服務位址」欄，只留 token＋測試連線）、逐項檢查結果顯示
- 依賴：T-77
- 驗收標準：token 錯誤時測試連線清楚顯示失敗原因；degraded 時語意模型檢查項顯示 ✕
- 預估：S

### 5.5 收斂卡

#### T-86: 手機版 RWD
- 範圍：全站補齊行動版斷點（sidebar 收合、卡片單欄化），設計稿無稿可抄，需自行制定斷點策略沿用 `--gutter-mobile` token
- 依賴：T-79～T-85 全部完成
- 驗收標準：至少覆蓋 375px/768px/1280px 三種寬度的視覺回歸測試（截圖比對或手動檢查記錄）；側欄在窄螢幕可收合且不遮蔽主內容
- 預估：L

#### T-87: 無障礙與快捷鍵
- 範圍：全站鍵盤可達性掃描（focus 順序、對比度 WCAG AA）；新增基本快捷鍵（設計稿完全沒有，需自行制定，例如搜尋頁 `/` 聚焦查詢框）
- 依賴：T-79～T-85 全部完成
- 驗收標準：自動化 a11y 掃描（如 axe）零嚴重違規；快捷鍵有對應測試；深淺色兩主題對比度皆過 AA
- 預估：M

#### T-88: E2E 測試骨架與 CI 接線
- 範圍：Playwright 設定、對接現有 CI（若有）、測試資料 fixture（覆蓋 degraded/truncated/conflict/duplicate 等狀態）
- 依賴：T-76
- 驗收標準：CI 可一鍵跑全部 E2E；狀態類 fixture 覆蓋第 1.4 節表格列出的每一種狀態
- 預估：M

---

## 附：需另外授權的外部依賴（不在本次拆卡範圍）

- T-45：Cloudflare `pm-api` ingress 切換到 5056（見第 3.4 節）
- 實作 T-81 前一次性核對 `schema/models.py` 的 `Document`／`Chunk` 精確欄位（`Note`／`Vault` 已於本文件核對，見 4.2）
