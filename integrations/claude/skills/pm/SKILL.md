---
name: pm
description: 查詢與維護 Lore Vault 專案記憶；用於歷史決策、隱性限制、跨 session 接續及明確的 /pm 指令。
user-invocable: true
allowed-tools: Bash, Read, Grep, Glob, Agent, mcp__lore-vault__space, mcp__lore-vault__vault_resolve, mcp__lore-vault__recall, mcp__lore-vault__ask, mcp__lore-vault__get, mcp__lore-vault__list, mcp__lore-vault__write, mcp__lore-vault__update, mcp__lore-vault__upload, mcp__lore-vault__status
argument-hint: "[init|explore|query|note|sync|status] [args...]"
---

# 專案記憶

只取得本次決策需要的上下文。已有答案不重查；沒有值得保存的新知識不寫入。記憶在 Lore Vault，工具為 `mcp__lore-vault__*`；依指令或實際需求呼叫，不把記憶操作變成每輪固定流程。

## 綁定

每個 session 先呼叫一次 `vault_resolve()`，之後所有讀寫都帶回傳的 `key`；切換 repo 才重新解析。

- dev space（預設）：省略 `cwd` 時用殼啟動時的工作目錄；不在專案根時傳 `cwd=<專案目錄>`。key 由 git remote 正規化算出（例如 `github.com/<owner>/<repo>`），改名前的舊 key 由服務端別名接起來。
- 無 git remote 的專案 key 為 `folder/<資料夾名>`，不是跨機器穩定識別；之後新增 remote 時 key 會改變，要請服務管理者在 UI 補別名，不另建新 vault。
- 回 `unknown_vault` 表示此專案沒有記憶。`query`／`status` 就回報無記憶，不為查詢建 vault；`init` 或確實要保存時才用 `vault_resolve(create=true, display=...)`。
- **HTTP 連線**（`claude mcp get lore-vault` 顯示 type 為 http，服務端看不到本機檔案系統）：不能用 `cwd` 推算。先跑 `git remote get-url origin`，以 `vault_resolve(remote_url=<輸出>)` 解析；沒有 remote 的專案直接傳 `key="folder/<資料夾名>"`。`vault_resolve()` 回錯誤要求 `remote_url`／`key` 時也照此處理。
- 跨專案觀察在 vault `global`；只有跨專案知識才明確指定它。跨 vault 查詢必須明示 `vault="*"`，而且只涵蓋目前 space。
- space：`dev`（開發記憶）、`lore`（世界觀）、`personal`（私人）。新 session 一律 `dev`；只有任務明確需要時才 `space(action="set", value=...)`，lore／personal 的 vault 必須帶 `key="<space>/名稱"`。

## 查詢

用任務關鍵字 `recall(query, vault)`；只回 id、標題、摘要與 `updated`，需要全文再 `get(vault, ids)`。只讀足以支援決策的相關 notes。記憶是線索，易變的部署、分支、資料與完成狀態需現查。

- 範圍在儲存層強制，結果只會來自指定的 vault 與目前 space，不必再以列舉交叉比對。
- `score` 是排序融合分數，不是相似度，不用固定門檻；以標題與摘要判斷相關性。
- 回應有 `truncated`／`omitted` 時表示被預算裁掉；要瀏覽近期寫入用 `list(vault, since?, topics?)`，`has_more=true` 以 `next_cursor` 續頁，未讀完不宣稱完整。
- `ask(question, vault)` 把 recall 前 10 則 note 交模型整理成逐點回答（附 note_ids）；可參考但不可當唯一事實來源，關鍵事實以 `get` 核對。只涵蓋 note。
- `recall` 預設一併回已上傳文件的段落（`kind=chunk`），全文用 `get` 取 `doc:…`／`chunk:…`。
- `upload` 在 HTTP 連線下收檔名＋base64 內容，不收本機路徑；本機殼才可傳路徑。

無關或無結果就查程式碼，不反覆換詞湊答案。檔案搜尋優先 FFF，特殊查詢按工具能力選擇。

### 降級

HTTP 連線沒有本地快照：服務不可達時 MCP 工具直接失敗，照實回報，不改用其他記憶來源湊答案。

本機殼的回應 `degraded=true`（`degraded_reason: "service_unreachable"`）表示服務不可達、結果來自本地快照：可能過時、只有關鍵字檢索、不含文件。回報時標明降級與快照時間，歸屬不明的結果不採信。

`write`／`update` 在服務不可達時直接失敗，不建離線寫入佇列；需要保存的內容先留在對話。只有使用者要求診斷服務才用 `status()`，不自行重啟服務。

## 保存

保存明確需求、架構取捨、難以從程式碼重建的依賴，或有證據且會重現的限制。一般修 bug、工具語法、單次失敗、測試數量與流水帳不值得另存。

- **寫前查重**：先 `recall` 同主題。已有相關 note 就修正原 note：`get` 取全文與 `updated`，以 `update(vault, id, expected_updated=<讀到的 updated>, body=...)` 改寫；真正新增才 `write(vault, title, body, topics?)`。
- **更正走原 note**：知識修正一律 `update` 原 note，不另建更正篇。`supersedes` 只用在整篇被新 note 取代的情況。
- `write` 回傳的 `duplicates` 非空時，改為 `get` + `update` 既有 note，不留兩篇。
- `update` 回版本衝突時，錯誤內附 `current`：先確認最新內容，再以 `current.updated` 當 `expected_updated` 重試，不直接覆蓋他人修改。
- `author` 填自己的角色名（依本機 persona 的角色名；子代理用各自名稱）；不代填別人，不填 `legacy`，不確定就省略。
- 內容包含結論、適用範圍、必要證據與仍未確定的條件；推測不寫成規則，不把當次解法升格為所有專案的慣例。不要同步複製到 MEMORY.md、CLAUDE.md 與其他記憶庫。
- 語料可能含商業專案原文；除非任務需要，不把整篇 note 讀進上下文。

## 指令

- `init`：`vault_resolve()` 確認綁定；不存在才 `create=true` 建立並回報 key。不另建沒有內容的初始化筆記。
- `query <問題>`：`recall` → 必要時 `get`；回報答案、來源 note 及影響判斷的不確定性。
- `note <標題>`：從參數與對話整理內容，缺少關鍵內容才詢問；查重後 `write` 或 `update`。
- `sync`：篩選本次值得保存的結論並更新原 note；無新增即回報無需更新。
- `status`：`status(vault)` 取筆數與最近更新，`list(vault)` 看近期主題；分頁不完整時標明範圍。
- `explore [範圍]`：按要求調查；未指定時先建立專案概覽，再深入影響理解的缺口。可獨立的範圍才平行派遣，不固定六個 agent。子代理只調查與回報證據、不改檔或寫 notes；主代理彙整查重後保存有價值的結論。

## 協調

Lore Vault 負責記憶；有 MEMPAL 工具的機器才用 `mempal_cowork_push(content, cwd)` 通知已協作的 agent（沒有就略過），Chatroom 沿用房內協定。只傳交接、阻礙或影響對方決策的新資訊，不廣播逐步進度。
