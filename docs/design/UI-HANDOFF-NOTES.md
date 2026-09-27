# UI 交接筆記

給接手 UI 的代理快速上手。設計來源與早期決策見 [UI-IMPLEMENTATION-PLAN.md](UI-IMPLEMENTATION-PLAN.md)、[UI-DESIGN-BRIEF.md](UI-DESIGN-BRIEF.md)；本文記錄三輪 UI／UX 修正後的現況、約束與坑。

## 1. 架構與檔案地圖

Preact + Vite，建置成 `ui/app/dist` 由 FastAPI 同源提供於 `/ui`（`src/lore_vault/api/ui.py` 的 `SpaStaticFiles`）。CSP 禁行內樣式：**不能寫 `style=`**，一律用 class。

| 位置 | 內容 |
|---|---|
| `ui/app/src/app.tsx` | 根元件：session 檢查、登入頁／Shell、收集回應裡的 `degraded` 標記（頂列徽章來源） |
| `ui/app/src/shell/Shell.tsx` | 頂列、側欄（導覽、vault 列表、寫入位置）、抽屜、快捷鍵說明、路由切換畫面 |
| `ui/app/src/screens/*.tsx` | 各畫面；`*.test.tsx` 為 vitest（happy-dom） |
| `ui/app/src/components/ui.tsx` | `Badge`、`EmptyState`、`Banner`、`ErrorState`、`Loading`、`Dialog`、兩段式確認 |
| `ui/app/src/components/VaultPicker.tsx` | 頁面內 vault 篩選器 |
| `ui/app/src/components/Pager.tsx` | 頁碼分頁 `Pager`、日期區間 `DateRange`、`rangeParams` |
| `ui/app/src/components/Filters.tsx` | 列表篩選共用：`FilterPanel`（摘要＋清除篩選）、`ChipGroup`、`TextFilter`（送出才套用）、網址同步 `useQuerySync`／`screenQuery` |
| `ui/app/src/lib/context.ts` | `AppEnv`（api、space、vault 篩選、toast、健檢徽章、最近一次檢索降級…） |
| `ui/app/src/lib/format.ts` | 文案與格式：錯誤碼、`formatTime`、`stripInternalRefs`、語意狀態說法 |
| `ui/app/src/lib/health.ts` | 健檢分組、客戶端檢查清單、備份明細解析 |
| `ui/app/src/lib/prefs.ts` | localStorage 偏好（主題、space、側欄 vault 收合），一律 try/catch |
| `ui/app/src/styles/app.css` | 專案樣式與 token 覆寫；`styles/ds/` 為設計系統原樣複製，**不改** |
| `ui/app/e2e/` | Playwright：臨時服務（暫存 DB）＋ 真實後端；`mobile.spec` 跑 Pixel 7 profile |

## 2. 共用元件與 token

- **token（`app.css` 開頭 `:root`）**
  - `--lv-content-max`（1600px）：主內容上限；檢視頁用兩欄 grid 吃滿，不留大片空白。
  - `--lv-radius-control`（2px）：按鈕、chip、徽章、輸入框的方形圓角。設計系統的 pill 在 `app.css`「按鈕形狀」段覆寫。圓點、space 圖示、品牌標記維持圓形。
  - `--lv-fs-meta`（12px）／`--lv-fs-body`（14px）：字級下限。新增樣式不要寫 9～13px，改用這兩個 token；`e2e/ui-text.spec.ts` 會掃可見文字 < 12px。
  - `--lv-*-text`：過 AA 的文字色別名（金、錯誤、成功、資訊、zone）。在 tint 底上的次要文字若不到 4.5:1，就在該元件把 `--ink-mute` 覆寫成 `--ink-soft` 或 `--ink`（`app.css` 開頭有既有清單）。
- **`VaultPicker`**：APG combobox（輸入框為主體＋listbox）。狀態就是 `AppEnv.vault`／`setVault`，與側欄共用。以輸入框為主體，是為了讓打字搜尋時全域單鍵快捷鍵不觸發。選完焦點留在輸入框（標準行為）。
- **`Badge`**：metadata 分色標籤。`tone` 決定顏色；`label` 只給螢幕閱讀器與 title；`testId` 放在值上（`textContent` 只含值）。
- **`EmptyState`**：空／不適用狀態一律用它，大字置中、可附 `action`；側欄等小區塊用 `size="sm"`。**不能放在 `role="table"` 內**（axe critical）。
- **`Pager`**：
  - 頁碼從 1 起算，每頁 10／30／50／100（預設 30）。
  - 對應 API 的 `offset`＋`with_total`，篩選變了要把頁碼重設為 1。
  - 載入中不停用按鈕：上一個請求會被 AbortController 取消，停用反而會閃成低對比。
- **`DateRange`**：本地日期轉 `since`＝當天 00:00、`until`＝23:59:59.999 的 UTC ISO，兩端都含。
- **API 分頁擴充**：`/v1/list`、`/v1/concept_query` 支援 `until`、`offset`（與 `cursor` 擇一）、`with_total`（回 `total`）。MCP 殼仍用 cursor。見 `docs/ARCHITECTURE.md`。
- **列表篩選**：記憶層、筆記、文件共用 `components/Filters.tsx`。
  - 筆記：vault、日期區間、標籤、標題、作者（部分符合）與作者狀態（已具名／未具名）；文件：vault、日期區間、類型（依檔名副檔名，`md` 含 `markdown`、`yaml` 含 `yml`）、抽取狀態（處理中＝排隊中＋抽取中）、檔名。對應 `/v1/list` 的 `title`／`author`／`author_state`／`statuses`／`extensions`。
  - 筆記、文件的篩選同步到網址查詢字串（`replaceState`，只在網址仍是該列表時寫，不新增歷史紀錄）；頁碼、每頁筆數與 vault 不進網址。記憶層沒有網址同步與「清除篩選」鈕（DateRange 自己的「清除」仍在）。
  - 文字篩選按鈕或 Enter 才送出，不每鍵發請求。

- **服務設定（`screens/Settings.tsx` 的 `ServiceSettings`）**：`GET /v1/settings` 回傳分類與每項的型別、範圍、單位、生效值、預設值與來源，畫面依 `categories` 順序以 `<fieldset>` 分組，不寫死項目。
  - 開關用原生 checkbox（label 包住、至少 44px 高），數字用 `type="number"`；編輯中的數字保留字串，儲存前才轉型。
  - 前端檢查與服務端同規則（型別、整數、範圍），錯誤以 `role="alert"` 顯示在欄位下並設 `aria-invalid`；服務端 400 `invalid_setting` 的 `errors[]` 逐項對回欄位。
  - 只送出有變動的項目（整批全成或全不改）；「還原預設」逐項送 `settings_reset`。來源用 `Badge`（預設＝plain、已覆寫＝warn），下方列預設值與覆寫者、時間；最近的修改列在區塊底部。
  - DB 裡不合法的覆寫以錯誤 `Banner` 提示（服務已略過）。
  - 只有 UI session 能讀寫；bearer 會 403 `ui_session_required`，e2e 要用登入後的頁面操作。

## 3. 本機 demo 與截圖／axe

e2e 已涵蓋功能與 axe；要看「像正式站」的畫面（長 GitHub key、十幾個 vault、concept），就另起一個 demo 服務：

1. 建置 UI：`cd ui/app && npm ci && npm run build`。
2. 在 repo 根目錄起服務，環境變數照 `ui/app/playwright.config.ts` 的 `webServer.env`，改用另一個埠（例如 5288），並把 `LORE_VAULT_DATABASE_PATH`／`HOME`／`LORE_VAULT_DOCUMENTS_BLOB_DIR` 指到一個暫存目錄：
   - 先 `uv run python ui/app/e2e/seed_account.py` 建 UI 帳號（帳密、token 用 `e2e/constants.ts` 的測試專用值），
   - 再 `uv run uvicorn --factory lore_vault.api.app:create_app --host 127.0.0.1 --port 5288`。
3. 灌資料（bearer 用同一個測試 token）：
   - `POST /v1/vaults` 建 15 個以上 vault，key 用 `github.com/<org>/<長名稱>` 並含「.」，例如 `u.e.p-s-core`；
   - `POST /v1/write` 寫長標題、長路徑正文的筆記；
   - `POST /v1/concepts`（`mode: upsert`）寫 concept；
   - `POST /v1/note_delete` 兩段式刪幾則，產生墓碑；
   - 要測日期區間就直接改 SQLite 的 `updated`（`created` 一起改，不可晚於 `updated`）。
4. 截圖與 axe：用 `@playwright/test` 的 `chromium` 寫一支小腳本登入後逐頁 `page.screenshot`；axe 用 `@axe-core/playwright` 的 `AxeBuilder`，tags 為 `wcag2a`、`wcag2aa`、`wcag21a`、`wcag21aa`。寫法照 `e2e/helpers.ts` 的 `axeViolations`。
5. **收工關服務**：背景 shell 停掉後 uvicorn 可能還在，要確認埠沒有程序在監聽（Windows 用 `Get-NetTCPConnection -LocalPort <埠>` 找出程序再停掉）。

## 4. 三輪踩過的坑

- **360px 橫向溢出**：根因是沒有空白的長字串，例如 vault key、錨點路徑、摘要裡的 `C:/...` 路徑。
  - 修法：`overflow-wrap: anywhere`，flex 子項加 `min-width: 0`。
  - 不要用 `overflow-x: hidden` 掩蓋。
  - e2e 必須用長 key，短 key 測不出來。
- **表頭與列錯位**：有 `auto` 欄（操作鈕）時，各列各自計算欄寬會錯位。欄寬定義在 `.lv-table--*` 容器，表頭與列用 `grid-template-columns: subgrid`；≤900px 改卡片。
- **SPA fallback**：舊邏輯看未解碼路徑的最後一段有沒有「.」，把 `/ui/maint/github.com%2F...` 誤判成靜態檔回 404。現在先解碼再看副檔名白名單；`assets/` 底下與資源副檔名缺檔仍回 404（`is_spa_route`）。
- **內部編號**：使用者看得到的文字（含 title、aria-label、空狀態、錯誤）不得出現 A22、T-86、v12 起這類代號。
  - 後端文案已改成白話；UI 端的 `stripInternalRefs` 保留當防線，只套在服務產生的說明文字。
  - `e2e/ui-text.spec.ts` 會掃描。
- **客戶端檢查**：snapshot／spool／concept_snapshot／concept_push 在服務端永遠略過。
  - UI 以 `lib/health.ts` 的 `CLIENT_CATEGORIES` 分類清單判斷，歸成收合的一組，不算進 SKIP。
  - 服務新增客戶端分類時要同步這份清單（裁決：不加後端欄位）。
- **語意檢索狀態**：頂列徽章＝**最近一次檢索**的 `degraded`，依原因分「逾時／離線／異常」。
  - 模型是否載入看 `/v1/status` 的 `embedding.model_loaded`（Ollama `/api/ps`）；啟動暖機只做一次，不代表模型仍在記憶體。
  - 後端冷啟動時改用 `embedding.cold_query_timeout`，詳見 `docs/DEVELOPMENT.md`。
- **焦點與快捷鍵**：
  - 全域單鍵快捷鍵在焦點位於輸入框、IME 組字或對話框開著時不觸發。
  - **不要用 `scrollIntoView`** 把清單項目捲進視野（側欄曾因此把瀏覽器的鍵盤起點移走，載入後第一個 Tab 不再是「跳到主內容」），改調整容器的 `scrollTop`。
  - 對話框關閉時，開啟前焦點若在 body，就把焦點交給 `#lv-main`。
- **e2e 選擇器**：
  - 頁面上有 `<select>`（每頁筆數、類型），`getByRole('option')` 會一併數到，要限定在 `getByRole('listbox')` 內。
  - 登入失敗鎖定是全域 3 次，新測試不要多一次錯誤登入。

## 5. 尚未處理或刻意保留

- HANDOFF 列過的已知問題仍在：抽屜沒有焦點陷阱、主內容沒設 `inert`、列表用 `<a role="row">` 蓋掉連結語意、Cinzel 字型未載入。
- 新增筆記的即時預覽不解析 `[[標題]]` 互連，只顯示排版結果。
- subgrid 需要較新的瀏覽器（Chrome 117+ 等）；太舊的瀏覽器會退回各列自行排版。
- 客戶端分類清單寫死在 UI（見上）。
- 記憶層、文件的每頁筆數不記住（每次進頁面回到預設 30）。
- 側欄 vault 清單限高以 1920×900 以上首屏看得到「目前寫入位置」為準；更矮的視窗會把側欄整體捲動。
